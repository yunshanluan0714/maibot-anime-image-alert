"""Observe processed messages without delaying MaiBot's ordinary replies."""

from __future__ import annotations

import asyncio
from typing import Any

from maibot_sdk import HookHandler, MaiBotPlugin
from maibot_sdk.types import ErrorPolicy, HookMode, HookOrder

from .classifier import classify
from .config import AnimeAlertConfig
from .core import AlertService, image_segments
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
        self._running = False

    async def on_load(self) -> None:
        self._service = AlertService(
            self.ctx.paths.data_dir,
            groups={str(group).strip() for group in self.config.alert.groups if str(group).strip()},
            target_user_id=self.config.alert.target_user_id,
            cooldown_seconds=self.config.alert.cooldown_seconds,
            hourly_limit=self.config.alert.hourly_limit,
        )
        self._finder = SourceFinder(self.ctx.paths.data_dir)
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
        description="仅观察白名单群图片；后台识别后原生 @ 指定用户",
        mode=HookMode.OBSERVE,
        order=HookOrder.NORMAL,
        timeout_ms=5000,
        error_policy=ErrorPolicy.SKIP,
    )
    async def observe_message(self, **kwargs: Any) -> None:
        message = kwargs.get("message")
        if not self._running or self._service is None or not isinstance(message, dict):
            return
        if not self._service.eligible(message):
            return
        try:
            self._queue.put_nowait(dict(message))
        except asyncio.QueueFull:
            self.ctx.logger.warning("二次元图片提醒队列已满，跳过新消息")

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

    async def _lookup_source(self, message: dict[str, Any]) -> SourceInfo | None:
        """Reverse-search the first resolvable image; never raises."""
        if self._finder is None or not self.config.source.enabled or not hasattr(self.ctx, "db"):
            return None
        try:
            for segment in image_segments(message):
                image_hash = segment["hash"].strip()
                record = await self.ctx.db.get(
                    "Images",
                    filters={"image_hash": image_hash, "image_type": "image"},
                    single_result=True,
                )
                if not isinstance(record, dict) or record.get("success") is False:
                    continue
                path = resolve_image_path(str(record.get("full_path") or ""), data_dir=self.ctx.paths.data_dir)
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

    async def _send_alert(self, message: dict[str, Any]) -> bool:
        # Re-check after classification: the user may have disabled/changed scope meanwhile.
        if not self._running or self._service is None or not self._service.eligible(message):
            return False
        stream_id = str(message.get("session_id") or "").strip()
        message_id = str(message.get("message_id") or "").strip()
        if not stream_id or not message_id:
            return False
        source = await self._lookup_source(message)
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
        if source_payload is None:
            wording = await compose_reminder(message, self.ctx, logger=self.ctx.logger)
        else:
            wording = await compose_reminder(message, self.ctx, logger=self.ctx.logger, source=source_payload)
        # Wording may await a model; a disabled or expired alert must not be sent afterwards.
        if not self._running or self._service is None or not self._service.eligible(message):
            return False
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
