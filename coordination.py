"""Planner source-inquiry detection and background-alert coordination."""

from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import datetime
import hashlib
import re
import time
from typing import Any, Callable, Iterable
from uuid import uuid4

try:
    from .core import unique_image_hashes
except ImportError:
    from core import unique_image_hashes

SOURCE_TOOL_NAME = "lookup_image_source"
SOURCE_NUDGE_MARK = "[以图搜图提示]"

_MAX_PLAIN_MESSAGE_ID_LENGTH = 12
_ALIAS_PREFIX = "m"
_ALIAS_HASH_LENGTH = 6

_ATTR_RE = re.compile(r'(\w+)="([^"]*)"')
_HEADER_RE = re.compile(r"^\s*<message\b([^>]*)>", re.S)
_SOURCE_ASK_RE = re.compile(
    r"(?:出处|哪部|哪个番|什么番|啥番|谁画|画师|哪位|这是谁|这谁|求图|求原图|原图|原帖|"
    r"p站|pixiv|pid|指路|搜图|识图|查图|source|sauce|coser|出自|哪来的|什么角色|哪个角色)",
    re.IGNORECASE,
)
_URL_RE = re.compile(
    r"(?:https?://|pixiv\.net|x\.com|twitter\.com|danbooru\.donmai\.us|skeb\.jp|bilibili\.com|miyoushe\.com)",
    re.IGNORECASE,
)


def to_display_message_id(message_id: Any) -> str:
    """Mirror MaiBot's message_id alias shortening for long IDs."""
    normalized = str(message_id or "").strip()
    if len(normalized) <= _MAX_PLAIN_MESSAGE_ID_LENGTH:
        return normalized
    digest = hashlib.sha1(normalized.encode("utf-8")).digest()
    return _ALIAS_PREFIX + base64.b32encode(digest).decode("ascii").lower()[:_ALIAS_HASH_LENGTH]


@dataclass(frozen=True)
class PlannerMessage:
    msg_id: str
    user: str
    is_self: bool
    body: str
    has_image: bool


@dataclass(frozen=True)
class ImageRecord:
    msg_id: str
    display_msg_id: str
    session_id: str
    group_id: str
    sender_id: str
    image_hashes: tuple[str, ...]
    text: str
    timestamp: float
    recorded_at: float


@dataclass(frozen=True)
class BotReplyRecord:
    session_id: str
    reply_message_id: str
    text: str
    has_link: bool
    created_at: float


@dataclass(frozen=True)
class LookupRecord:
    found: bool
    created_at: float


@dataclass(frozen=True)
class InteractionState:
    planner_looked_up: bool = False
    planner_found: bool = False
    bot_replied: bool = False
    bot_reply_text: str = ""
    bot_shared_link: bool = False
    sender_text: str = ""


def tool_names(tool_definitions: Iterable[Any]) -> set[str]:
    names: set[str] = set()
    for item in tool_definitions or []:
        if not isinstance(item, dict):
            continue
        fn = item.get("function")
        name = fn.get("name") if isinstance(fn, dict) else item.get("name")
        if isinstance(name, str) and name.strip():
            names.add(name.strip())
    return names


def _text_parts(item: dict[str, Any]) -> list[str]:
    return [
        str(p.get("text") or "")
        for p in item.get("parts") or []
        if isinstance(p, dict) and p.get("type") == "text"
    ]


def parse_planner_message(item: dict[str, Any]) -> PlannerMessage | None:
    if not isinstance(item, dict) or item.get("item_type") != "UserMessageItem":
        return None
    texts = _text_parts(item)
    if not texts:
        return None
    match = _HEADER_RE.match(texts[0])
    if match is None:
        return None
    attrs = dict(_ATTR_RE.findall(match.group(1)))
    body = "".join([texts[0][match.end():], *texts[1:]])
    has_image = any(isinstance(p, dict) and p.get("type") == "image" for p in item.get("parts") or [])
    return PlannerMessage(
        msg_id=attrs.get("msg_id", "").strip(),
        user=attrs.get("user", "").strip(),
        is_self=attrs.get("is_self_message", "").lower() == "true",
        body=body,
        has_image=has_image,
    )


def _called_tool(item: dict[str, Any]) -> str:
    if not isinstance(item, dict) or item.get("item_type") != "FunctionCallItem":
        return ""
    call = item.get("tool_call")
    return str(call.get("func_name") or "").strip() if isinstance(call, dict) else ""


def detect_source_inquiry(
    items: list[dict[str, Any]],
    *,
    known_image_msg_ids: Iterable[str] = (),
    window: int = 8,
) -> str | None:
    """Return the target image msg_id when recent chat asks for an image's source."""
    known_ids = {str(mid).strip() for mid in known_image_msg_ids if str(mid).strip()}
    entries: list[tuple[int, PlannerMessage]] = []
    calls: list[tuple[int, str]] = []
    for idx, item in enumerate(items):
        msg = parse_planner_message(item)
        if msg is not None:
            entries.append((idx, msg))
            continue
        name = _called_tool(item)
        if name:
            calls.append((idx, name))

    recent = entries[-max(1, window):]
    others = [(i, m) for i, m in recent if not m.is_self]
    if not others:
        return None

    latest_other_idx, _ = others[-1]
    if any(i > latest_other_idx and name == SOURCE_TOOL_NAME for i, name in calls):
        return None

    def _is_image_msg(m: PlannerMessage) -> bool:
        return bool(m.msg_id) and (m.has_image or m.msg_id in known_ids or "[图片" in m.body)

    img_idx = -1
    img_msg: PlannerMessage | None = None
    for i, m in reversed(others):
        if _is_image_msg(m):
            img_idx = i
            img_msg = m
            break
    if img_msg is None:
        return None

    # Ignore images that are already buried under more than 4 subsequent user messages.
    if sum(1 for i, _ in others if i > img_idx) > 4:
        return None

    # Do not trigger again if lookup_image_source already ran after this image appeared.
    if any(i > img_idx and name == SOURCE_TOOL_NAME for i, name in calls):
        return None

    last_self_idx = max((i for i, m in recent if m.is_self), default=-1)
    candidates = [(i, m) for i, m in others if i >= img_idx and i > last_self_idx]
    if any(_SOURCE_ASK_RE.search(m.body) for _, m in candidates):
        return img_msg.msg_id
    return None


def build_source_nudge_text(msg_id: str) -> str:
    return (
        f'{SOURCE_NUDGE_MARK} 群友正在询问图片（msg_id={msg_id}）的出处、角色或画师。'
        f'请先调用 {SOURCE_TOOL_NAME}(msg_id="{msg_id}") 以图搜图核实后再回复，'
        "不要在未查前直接说不知道或瞎猜；若查到原帖链接请一并自然告知。"
    )


def build_reminder_item(text: str, *, now: datetime | None = None) -> dict[str, Any]:
    stamp = (now or datetime.now()).isoformat()
    return {
        "item_type": "UserMessageItem",
        "meta": {"item_id": uuid4().hex, "logical_turn_id": None, "timestamp": stamp},
        "parts": [{"type": "text", "text": text}],
    }


def inject_reminder(items: list[dict[str, Any]], reminder: dict[str, Any]) -> list[dict[str, Any]]:
    """Insert before the trailing planner instruction so the final instruction stays last."""
    updated = list(items)
    position = len(updated)
    if updated and updated[-1].get("item_type") == "UserMessageItem" and parse_planner_message(updated[-1]) is None:
        position -= 1
    updated.insert(position, reminder)
    return updated


def _extract_message_text(message: dict[str, Any]) -> str:
    raw = message.get("raw_message")
    if isinstance(raw, list):
        parts: list[str] = []
        for seg in raw:
            if not isinstance(seg, dict) or seg.get("type") != "text":
                continue
            val = seg.get("data") if isinstance(seg.get("data"), str) else seg.get("text")
            if isinstance(val, str) and val.strip():
                parts.append(val.strip())
        if parts:
            return " ".join(parts)
    plain = message.get("processed_plain_text")
    return plain.strip() if isinstance(plain, str) else ""


class InteractionTracker:
    """In-memory short-term coordination state between Planner and background alerts."""

    def __init__(
        self,
        *,
        ttl_seconds: float = 900.0,
        max_entries: int = 256,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._ttl = max(60.0, float(ttl_seconds))
        self._max_entries = max(32, int(max_entries))
        self._clock = clock
        self._images_by_msg: dict[tuple[str, str], ImageRecord] = {}
        self._replies_by_session: dict[str, list[BotReplyRecord]] = {}
        self._lookups_by_msg: dict[tuple[str, str], LookupRecord] = {}
        self._lookups_by_hash: dict[tuple[str, str], LookupRecord] = {}

    def _prune(self, now: float) -> None:
        cutoff = now - self._ttl

        for key in [key for key, image in self._images_by_msg.items() if image.recorded_at < cutoff]:
            del self._images_by_msg[key]
        for sid in list(self._replies_by_session):
            kept = [r for r in self._replies_by_session[sid] if r.created_at >= cutoff][-32:]
            if kept:
                self._replies_by_session[sid] = kept
            else:
                self._replies_by_session.pop(sid, None)

        for key in [k for k, v in self._lookups_by_msg.items() if v.created_at < cutoff]:
            self._lookups_by_msg.pop(key, None)
        while len(self._lookups_by_msg) > self._max_entries:
            self._lookups_by_msg.pop(next(iter(self._lookups_by_msg)))

        for key in [k for k, v in self._lookups_by_hash.items() if v.created_at < cutoff]:
            self._lookups_by_hash.pop(key, None)
        while len(self._lookups_by_hash) > self._max_entries:
            self._lookups_by_hash.pop(next(iter(self._lookups_by_hash)))

    def _canonical_msg_id(self, session_id: str, msg_id: str) -> str:
        clean = str(msg_id or "").strip()
        if not clean or (session_id, clean) in self._images_by_msg:
            return clean
        matches = [image.msg_id for image in self._images_by_msg.values()
                   if image.session_id == session_id and image.display_msg_id == clean]
        return matches[0] if len(matches) == 1 else clean

    def record_image_message(self, message: dict[str, Any], now: float | None = None) -> ImageRecord | None:
        if not isinstance(message, dict):
            return None
        msg_id = str(message.get("message_id") or "").strip()
        session_id = str(message.get("session_id") or "").strip()
        hashes = tuple(unique_image_hashes(message, limit=3, max_len=256))
        if not session_id or not msg_id or not hashes:
            return None

        t = self._clock() if now is None else float(now)
        self._prune(t)

        info = message.get("message_info") if isinstance(message.get("message_info"), dict) else {}
        group_info = info.get("group_info") if isinstance(info.get("group_info"), dict) else {}
        user_info = info.get("user_info") if isinstance(info.get("user_info"), dict) else {}
        try:
            msg_ts = float(message.get("timestamp") or t)
        except (TypeError, ValueError, OverflowError):
            msg_ts = t

        display_id = to_display_message_id(msg_id)
        rec = ImageRecord(
            msg_id=msg_id,
            display_msg_id=display_id,
            session_id=session_id,
            group_id=str(group_info.get("group_id") or "").strip(),
            sender_id=str(user_info.get("user_id") or "").strip(),
            image_hashes=hashes,
            text=_extract_message_text(message),
            timestamp=msg_ts,
            recorded_at=t,
        )
        key = (session_id, msg_id)
        self._images_by_msg.pop(key, None)
        self._images_by_msg[key] = rec
        if len(self._images_by_msg) > self._max_entries:
            del self._images_by_msg[next(iter(self._images_by_msg))]
        return rec

    def known_image_msg_ids(self, session_id: str, now: float | None = None) -> set[str]:
        t = self._clock() if now is None else float(now)
        self._prune(t)
        ids: set[str] = set()
        for rec in self._images_by_msg.values():
            if rec.session_id != session_id:
                continue
            ids.add(rec.msg_id)
            if rec.display_msg_id:
                ids.add(rec.display_msg_id)
        return ids

    def resolve_hashes(self, session_id: str, msg_id: str, now: float | None = None) -> list[str]:
        t = self._clock() if now is None else float(now)
        self._prune(t)
        canonical = self._canonical_msg_id(session_id, msg_id)
        rec = self._images_by_msg.get((session_id, canonical))
        return list(rec.image_hashes) if rec is not None else []

    def mark_planner_lookup(
        self,
        *,
        session_id: str,
        msg_id: str = "",
        image_hashes: Iterable[str] = (),
        found: bool = False,
        now: float | None = None,
    ) -> None:
        t = self._clock() if now is None else float(now)
        self._prune(t)
        rec = LookupRecord(found=bool(found), created_at=t)
        canonical = self._canonical_msg_id(session_id, msg_id)
        if canonical:
            prev = self._lookups_by_msg.get((session_id, canonical))
            self._lookups_by_msg[session_id, canonical] = LookupRecord(
                found=bool(found or (prev and prev.found)),
                created_at=t,
            )
        for image_hash in image_hashes:
            h = str(image_hash or "").strip()
            if not h:
                continue
            prev = self._lookups_by_hash.get((session_id, h))
            self._lookups_by_hash[session_id, h] = LookupRecord(
                found=bool(found or (prev and prev.found)),
                created_at=t,
            )

    def record_bot_reply(
        self,
        session_id: str,
        reply_message_id: str,
        response_text: str,
        now: float | None = None,
    ) -> None:
        sid = str(session_id or "").strip()
        text = str(response_text or "").strip()
        if not sid or not text:
            return
        t = self._clock() if now is None else float(now)
        self._prune(t)
        target_id = self._canonical_msg_id(sid, reply_message_id)
        rec = BotReplyRecord(
            session_id=sid,
            reply_message_id=target_id,
            text=text,
            has_link=bool(_URL_RE.search(text)),
            created_at=t,
        )
        self._replies_by_session.setdefault(sid, []).append(rec)

    def inspect(
        self,
        *,
        session_id: str,
        msg_id: str,
        image_hashes: Iterable[str],
        message_ts: float,
        now: float | None = None,
        reply_window_seconds: float = 120.0,
    ) -> InteractionState:
        t = self._clock() if now is None else float(now)
        self._prune(t)

        canonical = self._canonical_msg_id(session_id, msg_id)
        img_rec = self._images_by_msg.get((session_id, canonical))
        sender_text = img_rec.text if img_rec is not None else ""

        planner_looked_up = False
        planner_found = False
        if canonical and (session_id, canonical) in self._lookups_by_msg:
            lk = self._lookups_by_msg[session_id, canonical]
            planner_looked_up = True
            planner_found = planner_found or lk.found
        for image_hash in image_hashes:
            lk = self._lookups_by_hash.get((session_id, str(image_hash or "").strip()))
            if lk is not None:
                planner_looked_up = True
                planner_found = planner_found or lk.found

        replies = self._replies_by_session.get(str(session_id or "").strip(), [])
        matched_reply: BotReplyRecord | None = None

        if canonical:
            for reply in reversed(replies):
                if reply.reply_message_id and reply.reply_message_id == canonical:
                    matched_reply = reply
                    break

        if matched_reply is None:
            anchor = img_rec.recorded_at if img_rec is not None else float(message_ts)
            newer_images = [
                r
                for r in self._images_by_msg.values()
                if r.session_id == session_id and r.msg_id != canonical and r.recorded_at > anchor
            ]
            other_image_ids = {r.msg_id for r in newer_images}
            for reply in reversed(replies):
                if reply.created_at < anchor - 1.0:
                    continue
                if reply.created_at - anchor > reply_window_seconds:
                    continue
                if reply.reply_message_id and reply.reply_message_id in other_image_ids:
                    continue
                if any(anchor < r.recorded_at < reply.created_at for r in newer_images):
                    continue
                matched_reply = reply
                break

        if matched_reply is None:
            return InteractionState(
                planner_looked_up=planner_looked_up,
                planner_found=planner_found,
                sender_text=sender_text,
            )
        return InteractionState(
            planner_looked_up=planner_looked_up,
            planner_found=planner_found,
            bot_replied=True,
            bot_reply_text=matched_reply.text,
            bot_shared_link=matched_reply.has_link,
            sender_text=sender_text,
        )


def decide_alert_action(
    state: InteractionState,
    *,
    has_source: bool,
    skip_when_replied_without_source: bool = True,
) -> str:
    """Choose whether to skip, send a natural follow-up, or send a normal alert."""
    if state.planner_looked_up and state.bot_replied:
        return "skip_handled_by_planner"
    if state.bot_replied and state.bot_shared_link:
        return "skip_already_shared"
    if state.bot_replied and not has_source and skip_when_replied_without_source:
        return "skip_replied_no_new_source"
    if state.bot_replied:
        return "send_follow_up"
    return "send_normal"


__all__ = [
    "BotReplyRecord",
    "ImageRecord",
    "InteractionState",
    "InteractionTracker",
    "LookupRecord",
    "PlannerMessage",
    "SOURCE_NUDGE_MARK",
    "SOURCE_TOOL_NAME",
    "build_reminder_item",
    "build_source_nudge_text",
    "decide_alert_action",
    "detect_source_inquiry",
    "inject_reminder",
    "parse_planner_message",
    "to_display_message_id",
    "tool_names",
]
