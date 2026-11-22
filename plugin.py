"""Observe processed messages, coordinate with Maisaka Planner, and alert cleanly."""

from __future__ import annotations

import asyncio
import time
from typing import Any

from maibot_sdk import HookHandler, MaiBotPlugin, Tool
from maibot_sdk.types import ErrorPolicy, HookMode, HookOrder, ToolParameterInfo, ToolParamType

from .classifier import classify
from .config import AnimeAlertConfig
from .coordination import (
    SOURCE_TOOL_NAME,
    InteractionTracker,
    build_reminder_item,
    build_source_nudge_text,
    decide_alert_action,
    detect_source_inquiry,
    inject_reminder,
    tool_names,
)
from .core import AlertService, image_segments, message_scope, unique_image_hashes
from .source_lookup import SourceFinder, SourceInfo, dedupe, format_source_line, localize_work, resolve_image_path
from .wording import compose_reminder, localize_names

QUEUE_SIZE = 20
_FRAME_HINTS = ("动画截图", "番剧", "动画画面", "截图", "剧照", "动画镜头", "动画片段")


class AnimeImageAlertPlugin(MaiBotPlugin):
    config_model = AnimeAlertConfig

    def __init__(self) -> None:
        super().__init__()
        self._service: AlertService | None = None
        self._finder: SourceFinder | None = None
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=QUEUE_SIZE)
        self._worker: asyncio.Task[None] | None = None
        self._clock = time.time
        self._tracker = InteractionTracker(clock=lambda: self._clock())
        self._running = False

    async def on_load(self) -> None:
        self._service = AlertService(
            self.ctx.paths.data_dir,
            groups={str(group).strip() for group in self.config.alert.groups if str(group).strip()},
            target_user_id=self.config.alert.target_user_id,
            cooldown_seconds=self.config.alert.cooldown_seconds,
            hourly_limit=self.config.alert.hourly_limit,
            clock=lambda: self._clock(),
        )
        self._finder = SourceFinder(self.ctx.paths.data_dir)
        self._tracker = InteractionTracker(clock=lambda: self._clock())
        self._apply_config()
        self._running = True
        self._worker = asyncio.create_task(self._run_queue(), name="anime-image-alert-worker")
        if not self._service.groups or not self._service.target_user_id:
            self.ctx.logger.info(
                "二次元图片提醒插件已加载，但尚未配置 alert.groups 或 alert.target_user_id，当前处于待命状态"
            )

    async def on_unload(self) -> None:
        self._running = False
        if self._worker is not None:
            self._worker.cancel()
            await asyncio.gather(self._worker, return_exceptions=True)
            self._worker = None
        self._drain_queue()
        if self._service is not None:
            self._service.close()
            self._service = None
        if self._finder is not None:
            self._finder.close()
            self._finder = None

    async def on_config_update(self, scope: str, config_data: dict[str, Any], version: str) -> None:
        # Runner injects the new typed self.config before this callback.
        del config_data, version
        if scope == "self":
            # Cancel old-target work before changing the shared service configuration.
            if self._worker is not None:
                self._worker.cancel()
                await asyncio.gather(self._worker, return_exceptions=True)
                self._worker = None
            self._drain_queue()
            self._apply_config()
            if self._running:
                self._worker = asyncio.create_task(self._run_queue(), name="anime-image-alert-worker")

    def _apply_config(self) -> None:
        if self._service is None:
            return
        self._service.enabled = self.config.plugin.enabled
        self._service.groups = {str(group).strip() for group in self.config.alert.groups if str(group).strip()}
        self._service.target_user_id = str(self.config.alert.target_user_id or "").strip()
        self._service.cooldown_seconds = self.config.alert.cooldown_seconds
        self._service.hourly_limit = self.config.alert.hourly_limit
        if self._finder is not None:
            src = self.config.source
            self._finder.saucenao_api_key = str(src.saucenao_api_key or "").strip()
            self._finder.serpapi_api_key = str(src.serpapi_api_key or "").strip()
            self._finder.allow_temp_image_upload = bool(src.allow_temp_image_upload)
            self._finder.proxy_url = str(src.proxy_url or "").strip()
            self._finder.min_similarity = float(src.min_similarity)
            self._finder.timeout_seconds = float(src.timeout_seconds)

    def _drain_queue(self) -> None:
        while True:
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            self._queue.task_done()

    @HookHandler(
        "chat.receive.after_process",
        name="anime_image_alert_observe",
        description="记录 QQ 图片哈希并观察白名单群图片；后台识别后智能协同原生 @ 指定用户",
        mode=HookMode.OBSERVE,
        order=HookOrder.NORMAL,
        timeout_ms=5000,
        error_policy=ErrorPolicy.SKIP,
    )
    async def observe_message(self, **kwargs: Any) -> None:
        message = kwargs.get("message")
        if not self._running or self._service is None or not isinstance(message, dict):
            return
        if (
            message.get("platform") == "qq"
            and not message.get("is_notify")
            and not message.get("is_command")
        ):
            _, sender = message_scope(message)
            account = str(message.get("account_id") or "")
            if account and sender and sender != account and image_segments(message):
                self._tracker.record_image_message(message)

        if not self._service.eligible(message):
            return
        try:
            self._queue.put_nowait(dict(message))
        except asyncio.QueueFull:
            self.ctx.logger.warning("二次元图片提醒队列已满，跳过新消息")

    @HookHandler(
        "maisaka.planner.before_request",
        name="anime_image_source_inquiry_nudge",
        description="当群友询问图片出处、画师或角色时，提醒主脑先调用 lookup_image_source 搜图核实",
        mode=HookMode.BLOCKING,
        order=HookOrder.NORMAL,
        timeout_ms=2000,
        error_policy=ErrorPolicy.SKIP,
    )
    async def on_planner_before_request(self, **kwargs: Any) -> dict[str, Any]:
        unchanged = {"action": "continue", "modified_kwargs": kwargs}
        if (
            not self._running
            or not self.config.plugin.enabled
            or not self.config.source.enabled
            or not self.config.source.enable_planner_tool
        ):
            return unchanged
        items = kwargs.get("items")
        if not isinstance(items, list) or SOURCE_TOOL_NAME not in tool_names(kwargs.get("tool_definitions") or []):
            return unchanged

        target_msg_id = detect_source_inquiry(items, known_image_msg_ids=self._tracker.known_image_msg_ids())
        if not target_msg_id:
            return unchanged

        updated = dict(kwargs)
        updated["items"] = inject_reminder(items, build_reminder_item(build_source_nudge_text(target_msg_id)))
        self.ctx.logger.info("已注入以图搜图提示: msg_id=%s", target_msg_id)
        return {"action": "continue", "modified_kwargs": updated}

    @HookHandler(
        "maisaka.reply.before_post_process",
        name="anime_image_reply_observe",
        description="旁路记录主脑回复，协调后台美图提醒避免重复夸赞或抢答",
        mode=HookMode.OBSERVE,
        order=HookOrder.NORMAL,
        timeout_ms=2000,
        error_policy=ErrorPolicy.SKIP,
    )
    async def on_reply_before_post_process(self, **kwargs: Any) -> None:
        if not self._running:
            return
        session_id = str(kwargs.get("session_id") or "").strip()
        reply_message_id = str(kwargs.get("reply_message_id") or "").strip()
        response = str(kwargs.get("response") or "").strip()
        if session_id and response:
            self._tracker.record_bot_reply(session_id, reply_message_id, response)

    async def _run_queue(self) -> None:
        while True:
            message = await self._queue.get()
            try:
                if self._service is not None:
                    outcome = await self._service.process(message, self._classify_message, self._send_alert)
                    self.ctx.logger.info("二次元图片提醒处理结果: %s", outcome)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Never include message text, image bytes, model output or exception payloads.
                self.ctx.logger.error("二次元图片提醒处理失败: %s", type(exc).__name__)
            finally:
                self._queue.task_done()

    async def _classify_message(self, message: dict[str, Any]) -> bool:
        return await classify(message, self.ctx, logger=self.ctx.logger)

    async def _lookup_source_by_hashes(self, image_hashes: list[str]) -> SourceInfo | None:
        """Reverse-search the first resolvable image hash; never raises."""
        if self._finder is None or not self.config.source.enabled or not hasattr(self.ctx, "db"):
            return None
        try:
            data_dir = getattr(getattr(self.ctx, "paths", None), "data_dir", None)
            for raw_hash in image_hashes:
                image_hash = str(raw_hash or "").strip()
                if not image_hash:
                    continue
                record = await self.ctx.db.get(
                    "Images",
                    filters={"image_hash": image_hash, "image_type": "image"},
                    single_result=True,
                )
                if not isinstance(record, dict) or record.get("success") is False:
                    continue
                path = resolve_image_path(str(record.get("full_path") or ""), data_dir=data_dir)
                if path is None:
                    continue
                raw = await asyncio.to_thread(path.read_bytes)
                description = str(record.get("description") or "")
                allow_frame = any(hint in description for hint in _FRAME_HINTS)
                allow_cosplay = "角色扮演" in description or "cos" in description.lower()
                info = await self._finder.find(
                    image_hash,
                    raw,
                    allow_anime_frame=allow_frame,
                    allow_cosplay_search=allow_cosplay,
                    logger=self.ctx.logger,
                )
                if info is not None:
                    return info
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.ctx.logger.warning("二次元图片出处查找失败: %s", type(exc).__name__)
        return None

    async def _lookup_source(self, message: dict[str, Any]) -> SourceInfo | None:
        """Reverse-search the first resolvable image; never raises."""
        return await self._lookup_source_by_hashes([seg["hash"] for seg in image_segments(message)])

    async def _resolve_hashes_for_msg_id(self, msg_id: str) -> list[str]:
        hashes = self._tracker.resolve_hashes(msg_id)
        if hashes or not hasattr(self.ctx, "db"):
            return hashes
        try:
            record = await self.ctx.db.get(
                "Messages",
                filters={"platform": "qq", "message_id": msg_id},
                single_result=True,
            )
            if isinstance(record, dict):
                raw_content = record.get("raw_content")
                if isinstance(raw_content, (bytes, bytearray)):
                    import msgpack

                    unpacked = msgpack.unpackb(raw_content, raw=False)
                    if isinstance(unpacked, list):
                        return unique_image_hashes({"raw_message": unpacked}, limit=3, max_len=256)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        return []

    @Tool(
        SOURCE_TOOL_NAME,
        platforms="qq",
        description=(
            "对聊天中的某张图片执行以图搜图，查明作品、角色、画师和原帖链接（Pixiv / X / Danbooru / 番剧等）。"
            "当群友询问“这是谁”“出处是什么”“求原图/画师/哪部番”或你想核实图片出处时务必先调用此工具，不要凭空猜测或直接说不知道。"
        ),
        parameters=[
            ToolParameterInfo(
                name="msg_id",
                param_type=ToolParamType.STRING,
                description="包含待查图片的消息 ID（聊天记录中的 msg_id）。",
                required=True,
            ),
            ToolParameterInfo(
                name="index",
                param_type=ToolParamType.INTEGER,
                description="同一条消息含多张图片时指定第几张，从 0 开始，默认 0。",
                required=False,
            ),
        ],
        visibility="visible",
    )
    async def tool_lookup_image_source(self, msg_id: Any = "", index: Any = 0, **kwargs: Any) -> dict[str, Any]:
        del kwargs
        if (
            not self._running
            or not self.config.plugin.enabled
            or not self.config.source.enabled
            or not self.config.source.enable_planner_tool
            or self._finder is None
        ):
            return {"success": False, "error": "以图搜图功能当前未启用"}

        norm_msg_id = str(msg_id or "").strip()
        if not norm_msg_id:
            return {"success": False, "error": "请提供包含图片的消息 msg_id"}
        try:
            norm_idx = max(0, int(index or 0))
        except (TypeError, ValueError):
            norm_idx = 0

        hashes = await self._resolve_hashes_for_msg_id(norm_msg_id)
        if not hashes:
            self._tracker.mark_planner_lookup(msg_id=norm_msg_id, found=False)
            return {
                "success": True,
                "found": False,
                "content": f"未在消息 {norm_msg_id} 中找到可检索的图片，可能消息过旧或不含图片。",
            }
        if norm_idx >= len(hashes):
            return {
                "success": True,
                "found": False,
                "content": f"消息 {norm_msg_id} 共包含 {len(hashes)} 张图片，索引 {norm_idx} 超出范围（从 0 开始）。",
            }

        target_hash = hashes[norm_idx]
        source = await self._lookup_source_by_hashes([target_hash])
        self._tracker.mark_planner_lookup(msg_id=norm_msg_id, image_hashes=[target_hash], found=source is not None)
        if source is None:
            return {
                "success": True,
                "found": False,
                "content": "已通过以图搜图引擎检索，暂未查到确切出处，可直接告诉群友没搜到，不要猜测编造。",
            }

        zh_names: list[str] = []
        if source.characters and self.config.source.localize_names:
            zh_names = await localize_names(source.work, source.characters, self.ctx, logger=self.ctx.logger)
        deduped_chars = dedupe(zh_names if zh_names else [c.title() for c in source.characters], limit=3)
        work_zh = localize_work(source.work) or source.work
        summary = format_source_line(
            source,
            include_link=self.config.source.include_link,
            zh_characters=deduped_chars or None,
        )
        lines = [f"已查到图片出处（{source.engine}，相似度 {source.similarity:.1f}%）：{summary}"]
        if work_zh:
            lines.append(f"作品：{work_zh}")
        if deduped_chars:
            lines.append(f"角色：{'、'.join(deduped_chars)}")
        if source.artist:
            lines.append(f"画师：{source.artist}")
        if source.url:
            lines.append(f"原帖链接：{source.url}")
        return {
            "success": True,
            "found": True,
            "content": "\n".join(lines),
            "work": work_zh,
            "characters": deduped_chars,
            "artist": source.artist,
            "url": source.url,
            "engine": source.engine,
            "similarity": round(float(source.similarity), 1),
        }

    async def _wait_reply_grace(
        self,
        message: dict[str, Any],
        stream_id: str,
        message_id: str,
        hashes: list[str],
        msg_ts: float,
    ) -> bool:
        grace = max(0, int(self.config.alert.reply_grace_seconds))
        elapsed = self._clock() - msg_ts
        if 0 <= elapsed < grace:
            await asyncio.sleep(grace - elapsed)
            if not self._running or self._service is None or not self._service.eligible(message):
                return False

        state = self._tracker.inspect(
            session_id=stream_id,
            msg_id=message_id,
            image_hashes=hashes,
            message_ts=msg_ts,
        )
        if state.planner_looked_up and not state.bot_replied:
            # Planner already queried the source; give it a few seconds to finish composing its reply.
            for _ in range(12):
                await asyncio.sleep(0.5)
                if not self._running or self._service is None or not self._service.eligible(message):
                    return False
                state = self._tracker.inspect(
                    session_id=stream_id,
                    msg_id=message_id,
                    image_hashes=hashes,
                    message_ts=msg_ts,
                )
                if state.bot_replied:
                    break
        return True

    async def _send_alert(self, message: dict[str, Any]) -> bool | str:
        # Re-check after classification: the user may have disabled/changed scope meanwhile.
        if not self._running or self._service is None or not self._service.eligible(message):
            return False
        stream_id = str(message.get("session_id") or "").strip()
        message_id = str(message.get("message_id") or "").strip()
        if not stream_id or not message_id:
            return False

        hashes = unique_image_hashes(message, limit=3, max_len=256)
        try:
            msg_ts = float(message.get("timestamp", 0))
        except (TypeError, ValueError, OverflowError):
            msg_ts = self._clock()

        if not await self._wait_reply_grace(message, stream_id, message_id, hashes, msg_ts):
            return False

        pre_state = self._tracker.inspect(
            session_id=stream_id,
            msg_id=message_id,
            image_hashes=hashes,
            message_ts=msg_ts,
        )
        if pre_state.planner_looked_up and pre_state.bot_replied:
            self.ctx.logger.info("二次元图片提醒跳过: reason=skip_handled_by_planner msg_id=%s", message_id)
            return "skipped"

        source = await self._lookup_source(message)
        state = self._tracker.inspect(
            session_id=stream_id,
            msg_id=message_id,
            image_hashes=hashes,
            message_ts=msg_ts,
        )
        decision = decide_alert_action(
            state,
            has_source=(source is not None),
            skip_when_replied_without_source=bool(self.config.alert.skip_when_replied_without_source),
        )
        if decision.startswith("skip_"):
            self.ctx.logger.info("二次元图片提醒跳过: reason=%s msg_id=%s", decision, message_id)
            return "skipped"

        link_line = ""
        memory_note = ""
        source_payload: dict[str, Any] | None = None
        if source is not None:
            zh_names: list[str] = []
            if source.characters and self.config.source.localize_names:
                zh_names = await localize_names(source.work, source.characters, self.ctx, logger=self.ctx.logger)
            deduped_chars = dedupe(zh_names if zh_names else [c.title() for c in source.characters], limit=3)
            source_summary = format_source_line(
                source,
                include_link=self.config.source.include_link,
                zh_characters=deduped_chars or None,
            )
            link_line = source.url if (self.config.source.include_link and source.url) else ""
            source_payload = {
                "work": localize_work(source.work) or source.work,
                "characters": deduped_chars,
            }
            if source.artist:
                source_payload["artist"] = source.artist
            memory_note = format_source_line(source, include_link=False, zh_characters=deduped_chars or None)
            self.ctx.logger.info("二次元图片出处: engine=%s sim=%.1f %s", source.engine, source.similarity, source_summary)

        if decision == "send_follow_up":
            wording = await compose_reminder(
                message,
                self.ctx,
                logger=self.ctx.logger,
                source=source_payload,
                previous_bot_reply=state.bot_reply_text,
                mode="follow_up",
            )
        elif source_payload is None:
            wording = await compose_reminder(message, self.ctx, logger=self.ctx.logger)
        else:
            wording = await compose_reminder(message, self.ctx, logger=self.ctx.logger, source=source_payload)

        # Wording may await a model; a disabled, expired, or newly replied alert must not send blindly.
        if not self._running or self._service is None or not self._service.eligible(message):
            return False
        if decision == "send_normal":
            latest_state = self._tracker.inspect(
                session_id=stream_id,
                msg_id=message_id,
                image_hashes=hashes,
                message_ts=msg_ts,
            )
            latest_decision = decide_alert_action(
                latest_state,
                has_source=(source is not None),
                skip_when_replied_without_source=bool(self.config.alert.skip_when_replied_without_source),
            )
            if latest_decision.startswith("skip_"):
                self.ctx.logger.info("二次元图片提醒跳过: reason=%s msg_id=%s", latest_decision, message_id)
                return "skipped"

        text = " " + wording + (("\n" + link_line) if link_line else "")
        segments: list[dict[str, Any]] = []
        if message_id != "0" and not message_id.startswith("qq-"):
            segments.append({"type": "reply", "data": {"target_message_id": message_id}})
        segments.extend([
            {"type": "at", "data": {"target_user_id": self.config.alert.target_user_id}},
            {"type": "text", "data": text},
        ])
        result = await self.ctx.send.hybrid(
            segments,
            stream_id,
            return_details=True,
            typing=False,
            storage_message=True,
            processed_plain_text=f"[二次元图片提醒] {wording}" + (f"（{memory_note}）" if memory_note else ""),
            show_log=False,
        )
        return isinstance(result, dict) and result.get("sent") is True


def create_plugin() -> AnimeImageAlertPlugin:
    return AnimeImageAlertPlugin()
