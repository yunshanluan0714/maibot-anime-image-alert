"""Persistent alert eligibility, deduplication and send reservations."""
from __future__ import annotations

import asyncio
import hashlib
import sqlite3
import time
from pathlib import Path


def image_segments(message: dict) -> list[dict]:
    raw = message.get("raw_message")
    if not isinstance(raw, list):
        return []
    return [
        s
        for s in raw
        if isinstance(s, dict)
        and s.get("type") == "image"
        and isinstance(s.get("hash"), str)
        and 0 < len(s["hash"]) <= 256
    ][:3]


def unique_image_hashes(message: dict, *, limit: int = 3, max_len: int | None = 256) -> list[str]:
    if not isinstance(message, dict) or not isinstance(message.get("raw_message"), list):
        return []
    hashes: list[str] = []
    for segment in message["raw_message"]:
        if not isinstance(segment, dict) or segment.get("type") != "image":
            continue
        raw_hash = segment.get("hash")
        if not isinstance(raw_hash, str):
            continue
        image_hash = raw_hash.strip()
        if not image_hash or (max_len is not None and len(image_hash) > max_len):
            continue
        if image_hash not in hashes:
            hashes.append(image_hash)
        if len(hashes) >= limit:
            break
    return hashes


def message_scope(message: dict):
    try:
        info = message["message_info"]
        return (str(info["group_info"]["group_id"]), str(info["user_info"]["user_id"]))
    except (KeyError, TypeError):
        return "", ""


class AlertService:
    def __init__(self, data_dir, groups: set[str], target_user_id: str,
                 cooldown_seconds: int = 0, hourly_limit: int = 0, clock=time.time):
        Path(data_dir).mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(Path(data_dir) / "alerts.sqlite3"), timeout=5)
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS seen (key TEXT PRIMARY KEY, created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS sends (
                key TEXT PRIMARY KEY, group_id TEXT NOT NULL,
                created REAL NOT NULL, status TEXT NOT NULL);
        """)
        self.lock = asyncio.Lock()
        self.clock = clock
        self.enabled = True
        self.groups = set(groups)
        self.target_user_id = str(target_user_id or "").strip()
        self.cooldown_seconds = cooldown_seconds
        self.hourly_limit = hourly_limit

    def close(self):
        self.db.close()

    def eligible(self, message: dict) -> bool:
        target = str(self.target_user_id or "").strip()
        if not isinstance(message, dict) or not self.enabled or not target:
            return False
        group, sender = message_scope(message)
        account = str(message.get("account_id") or "")
        if (message.get("platform") != "qq" or group not in self.groups
                or not account or not sender or sender == account or sender == target
                or message.get("is_notify") or message.get("is_command")
                or not message.get("session_id") or not message.get("message_id")):
            return False
        try:
            age = self.clock() - float(message.get("timestamp", 0))
        except (ValueError, TypeError, OverflowError):
            return False
        return 0 <= age <= 300 and bool(image_segments(message))

    def _image_key(self, message, segment):
        # Same image in either enabled group should not ping the same recipient twice.
        parts = (str(message["account_id"]), self.target_user_id, segment["hash"])
        return hashlib.sha256("\0".join(parts).encode()).hexdigest()

    def _message_key(self, message):
        group, _ = message_scope(message)
        return hashlib.sha256("\0".join((str(message["account_id"]), group,
            str(message["message_id"]), self.target_user_id)).encode()).hexdigest()

    def _limited(self, group, now):
        if self.cooldown_seconds == 0 and self.hourly_limit == 0:
            return False
        row = self.db.execute("SELECT MAX(created), COUNT(*) FROM sends WHERE group_id=? AND created>?",
                              (group, now - 3600)).fetchone()
        return ((row[0] is not None and now - row[0] < self.cooldown_seconds)
                or (self.hourly_limit > 0 and row[1] >= self.hourly_limit))

    async def process(self, message: dict, classify, send) -> str:
        async with self.lock:
            if not self.eligible(message):
                return "ineligible"
            now = self.clock()
            group, _ = message_scope(message)
            with self.db:
                self.db.execute("DELETE FROM seen WHERE created<?", (now - 86400,))
                self.db.execute("DELETE FROM sends WHERE created<?", (now - 86400,))
            if self._limited(group, now):
                return "rate_limited"
            key = self._message_key(message)
            if self.db.execute("SELECT 1 FROM sends WHERE key=?", (key,)).fetchone():
                return "duplicate"
            images = [s for s in image_segments(message) if not self.db.execute(
                "SELECT 1 FROM seen WHERE key=?", (self._image_key(message, s),)).fetchone()]
            if not images:
                return "duplicate"
            with self.db:
                for segment in images:
                    self.db.execute("INSERT OR IGNORE INTO seen VALUES (?,?)",
                                    (self._image_key(message, segment), now))
            filtered = dict(message, raw_message=images)
            try:
                matched = await classify(filtered)
            except Exception:
                return "classification_error"
            if matched is not True:
                return "not_matched"
            # Recheck current settings after awaiting a model and before committing a send.
            if not self.eligible(message):
                return "ineligible"
            now = self.clock()
            if self._limited(group, now):
                return "rate_limited"
            with self.db:
                self.db.execute("INSERT INTO sends VALUES (?,?,?,?)", (key, group, now, "pending"))
            # Reservation precedes outbound I/O: an ambiguous send must never be retried automatically.
            try:
                success = await send(message)
            except Exception:
                status = "unknown"
            else:
                status = "sent" if success is True else "failed"
            with self.db:
                self.db.execute("UPDATE sends SET status=? WHERE key=?", (status, key))
            return status
