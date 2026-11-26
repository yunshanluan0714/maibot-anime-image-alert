"""SDK-facing tests: all sends and model calls are mocks, never a real gateway."""

import asyncio
import importlib.util
import logging
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parent
PACKAGE = "anime_alert_sdk_test"
spec = importlib.util.spec_from_file_location(PACKAGE, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
package = importlib.util.module_from_spec(spec)
sys.modules[PACKAGE] = package
spec.loader.exec_module(package)
plugin_spec = importlib.util.spec_from_file_location(PACKAGE + ".plugin", ROOT / "plugin.py")
plugin_module = importlib.util.module_from_spec(plugin_spec)
sys.modules[plugin_spec.name] = plugin_module
plugin_spec.loader.exec_module(plugin_module)
Plugin = plugin_module.AnimeImageAlertPlugin
SourceInfo = plugin_module.SourceInfo


def message(message_id="12345", group="200000001", image_hash="image-hash"):
    return {
        "message_id": message_id,
        "timestamp": str(time.time()),
        "session_id": "test-stream-not-a-real-group",
        "platform": "qq",
        "account_id": "test-bot",
        "scope": "group",
        "message_info": {
            "group_info": {"group_id": group, "group_name": "test"},
            "user_info": {"user_id": "test-sender", "user_nickname": "test"},
        },
        "raw_message": [{"type": "image", "hash": image_hash, "data": "test description"}],
    }


class PluginTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.send = AsyncMock(return_value={"sent": True, "message_id": "mock-sent-id"})
        self.plugin = Plugin()
        cfg = Plugin.build_default_config()
        cfg["alert"]["reply_grace_seconds"] = 0
        self.plugin.set_plugin_config(cfg)
        self.plugin._set_context(SimpleNamespace(
            paths=SimpleNamespace(data_dir=Path(self.directory.name)),
            send=SimpleNamespace(hybrid=self.send),
            logger=logging.getLogger("anime-alert-test"),
        ))
        await self.plugin.on_load()

    async def asyncTearDown(self):
        await self.plugin.on_unload()
        self.directory.cleanup()

    async def test_defaults_and_native_webui_schema(self):
        defaults = Plugin.build_default_config()
        self.assertEqual(defaults["plugin"]["config_version"], "1.4.0")
        self.assertEqual(defaults["alert"]["reply_grace_seconds"], 6)
        self.assertTrue(defaults["alert"]["skip_when_replied_without_source"])
        self.assertTrue(defaults["source"]["enable_planner_tool"])
        self.assertEqual(self.plugin.config.alert.groups, ["200000001", "200000002"])
        self.assertEqual(self.plugin.config.alert.target_user_id, "100000001")
        self.assertEqual(self.plugin.config.alert.cooldown_seconds, 0)
        self.assertEqual(self.plugin.config.alert.hourly_limit, 0)
        self.assertTrue(self.plugin.config.source.enabled)
        self.assertTrue(self.plugin.config.source.include_link)
        self.assertTrue(self.plugin.config.source.localize_names)
        schema = Plugin.build_config_schema(plugin_id="local.anime-image-alert")
        self.assertTrue(schema["sections"])
        self.assertIn("source", schema["sections"])
        self.assertEqual(Plugin.config_reload_subscriptions, ())

    async def test_native_reply_at_and_confirmed_send(self):
        self.assertTrue(await self.plugin._send_alert(message()))
        args, kwargs = self.send.await_args
        self.assertEqual(args[1], "test-stream-not-a-real-group")
        self.assertEqual(args[0][0], {"type": "reply", "data": {"target_message_id": "12345"}})
        self.assertEqual(args[0][1], {"type": "at", "data": {"target_user_id": "100000001"}})
        self.assertEqual(args[0][2]["type"], "text")
        self.assertTrue(kwargs["return_details"])
        self.assertFalse(kwargs["show_log"])
        self.assertNotIn("set_reply", kwargs)

    async def test_synthetic_id_keeps_native_at_without_invalid_quote(self):
        for identifier in ("qq-internal-id", "0"):
            self.assertTrue(await self.plugin._send_alert(message(message_id=identifier)))
            self.assertEqual([part["type"] for part in self.send.await_args.args[0]], ["at", "text"])

    async def test_send_failure_is_not_success(self):
        for result in ({"sent": False}, {"success": True}, True, None):
            self.send.return_value = result
            self.assertFalse(await self.plugin._send_alert(message()))

    async def test_unapproved_group_and_bot_message_never_enter_queue(self):
        await self.plugin.observe_message(message=message(group="not-approved"))
        own = message()
        own["message_info"]["user_info"]["user_id"] = "test-bot"
        await self.plugin.observe_message(message=own)
        self.assertEqual(self.plugin._queue.qsize(), 0)
        self.send.assert_not_awaited()

    async def test_queue_is_bounded_and_drops_new_work(self):
        self.plugin._worker.cancel()
        await asyncio.gather(self.plugin._worker, return_exceptions=True)
        self.plugin._worker = None
        for i in range(21):
            await self.plugin.observe_message(message=message(message_id=str(i + 1)))
        self.assertEqual(self.plugin._queue.qsize(), 20)
        self.send.assert_not_awaited()

    async def test_worker_runs_one_candidate_at_a_time(self):
        entered = asyncio.Event()
        release = asyncio.Event()
        active = 0
        maximum = 0
        async def process(*args):
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            entered.set()
            await release.wait()
            active -= 1
            return "mocked"
        with patch.object(self.plugin._service, "process", side_effect=process) as mocked:
            await self.plugin.observe_message(message=message())
            await self.plugin.observe_message(message=message(message_id="12346"))
            await asyncio.wait_for(entered.wait(), 1)
            self.assertEqual(mocked.await_count, 1)
            release.set()
            await asyncio.wait_for(self.plugin._queue.join(), 1)
            self.assertEqual(mocked.await_count, 2)
            self.assertEqual(maximum, 1)

    async def test_hot_disable_cancels_classifier_preserves_db_and_clears_queue(self):
        service = self.plugin._service
        database = service.db
        started = asyncio.Event()
        async def classify(*args):
            started.set()
            await asyncio.Event().wait()
        with patch.object(self.plugin, "_classify_message", side_effect=classify):
            await self.plugin.observe_message(message=message())
            await asyncio.wait_for(started.wait(), 1)
            await self.plugin.observe_message(message=message(message_id="12346", image_hash="another"))
            config = Plugin.build_default_config()
            config["plugin"]["enabled"] = False
            self.plugin.set_plugin_config(config)
            await self.plugin.on_config_update("self", config, "updated")
        self.assertIs(self.plugin._service, service)
        self.assertIs(service.db, database)
        self.assertFalse(service.enabled)
        self.assertEqual(self.plugin._queue.qsize(), 0)
        self.assertEqual(database.execute("SELECT COUNT(*) FROM sends").fetchone()[0], 0)
        self.send.assert_not_awaited()
        self.assertFalse(await self.plugin._send_alert(message()))

    async def test_hot_target_change_uses_new_native_at(self):
        original = self.plugin._service
        config = Plugin.build_default_config()
        config["alert"]["target_user_id"] = "123456789"
        config["alert"]["reply_grace_seconds"] = 0
        self.plugin.set_plugin_config(config)
        await self.plugin.on_config_update("self", config, "updated")
        self.assertIs(self.plugin._service, original)
        self.assertEqual(original.target_user_id, "123456789")
        self.assertTrue(await self.plugin._send_alert(message()))
        self.assertEqual(self.send.await_args.args[0][1]["data"]["target_user_id"], "123456789")

    async def test_classifier_delegates_to_injected_sdk_context(self):
        with patch.object(plugin_module, "classify", AsyncMock(return_value=True)) as classify:
            candidate = message()
            self.assertTrue(await self.plugin._classify_message(candidate))
            classify.assert_awaited_once_with(candidate, self.plugin.ctx, logger=self.plugin.ctx.logger)

    async def test_unload_cancels_worker_and_closes_db(self):
        database = self.plugin._service.db
        sources_db = self.plugin._finder.db
        await self.plugin.on_unload()
        self.assertIsNone(self.plugin._worker)
        self.assertIsNone(self.plugin._service)
        self.assertIsNone(self.plugin._finder)
        with self.assertRaises(sqlite3.ProgrammingError):
            database.execute("SELECT 1")
        with self.assertRaises(sqlite3.ProgrammingError):
            sources_db.execute("SELECT 1")
        await self.plugin.observe_message(message=message())
        self.send.assert_not_awaited()

    async def test_generated_wording_keeps_native_at_and_quote(self):
        with patch.object(plugin_module, "compose_reminder", AsyncMock(return_value="这张配色挺舒服，来看看")) as compose:
            candidate = message()
            self.assertTrue(await self.plugin._send_alert(candidate))
            compose.assert_awaited_once_with(candidate, self.plugin.ctx, logger=self.plugin.ctx.logger)
        segments = self.send.await_args.args[0]
        self.assertEqual([s["type"] for s in segments], ["reply", "at", "text"])
        self.assertEqual(segments[1]["data"]["target_user_id"], "100000001")
        self.assertEqual(segments[2]["data"], " 这张配色挺舒服，来看看")

    async def test_alert_attaches_localized_source_line_and_passes_source_to_wording(self):
        found = SourceInfo(
            engine="saucenao",
            similarity=93.6,
            work="zenless zone zero",
            characters=["aria", "nangong yu"],
            artist="ohichi16",
            url="https://www.pixiv.net/artworks/152247186",
        )
        with patch.object(self.plugin, "_lookup_source", AsyncMock(return_value=found)), \
             patch.object(plugin_module, "localize_names", AsyncMock(return_value=["爱芮", "南宫羽"])), \
             patch.object(plugin_module, "compose_reminder", AsyncMock(return_value="妄想天使这张绝了，快来瞅瞅")) as compose:
            candidate = message()
            self.assertTrue(await self.plugin._send_alert(candidate))
            compose.assert_awaited_once_with(
                candidate,
                self.plugin.ctx,
                logger=self.plugin.ctx.logger,
                source={"work": "绝区零", "characters": ["爱芮", "南宫羽"], "artist": "ohichi16"},
            )
            segments = self.send.await_args.args[0]
            self.assertEqual([s["type"] for s in segments], ["reply", "at", "text"])
            self.assertEqual(
                segments[2]["data"],
                " 妄想天使这张绝了，快来瞅瞅\nhttps://www.pixiv.net/artworks/152247186",
            )
            self.assertNotIn("出处：", segments[2]["data"])
            self.assertNotIn("画师", segments[2]["data"])
            # Bot memory keeps the verified source so later chat replies know the picture.
            self.assertEqual(
                self.send.await_args.kwargs["processed_plain_text"],
                "[二次元图片提醒] 妄想天使这张绝了，快来瞅瞅（出处：《绝区零》爱芮、南宫羽 · 画师 ohichi16）",
            )

            # Respect include_link=False on config hot update.
            cfg = Plugin.build_default_config()
            cfg["alert"]["reply_grace_seconds"] = 0
            cfg["source"]["include_link"] = False
            cfg["source"]["saucenao_api_key"] = "SECRET_KEY"
            cfg["source"]["min_similarity"] = 88.0
            cfg["source"]["timeout_seconds"] = 18
            self.plugin.set_plugin_config(cfg)
            await self.plugin.on_config_update("self", cfg, "updated")
            self.assertEqual(self.plugin._finder.saucenao_api_key, "SECRET_KEY")
            self.assertEqual(self.plugin._finder.min_similarity, 88.0)
            self.assertEqual(self.plugin._finder.timeout_seconds, 18.0)

            self.assertTrue(await self.plugin._send_alert(candidate))
            self.assertEqual(
                self.send.await_args.args[0][2]["data"],
                " 妄想天使这张绝了，快来瞅瞅",
            )

            # Empty URL with valid work/characters still feeds source to wording without trailing blank line.
            cfg["source"]["include_link"] = True
            self.plugin.set_plugin_config(cfg)
            await self.plugin.on_config_update("self", cfg, "updated")
            found_no_url = SourceInfo(
                engine="saucenao",
                similarity=91.0,
                work="zenless zone zero",
                characters=["aria"],
                artist="ohichi16",
                url="",
            )
            with patch.object(self.plugin, "_lookup_source", AsyncMock(return_value=found_no_url)):
                compose.reset_mock()
                self.assertTrue(await self.plugin._send_alert(candidate))
                compose.assert_awaited_once_with(
                    candidate,
                    self.plugin.ctx,
                    logger=self.plugin.ctx.logger,
                    source={"work": "绝区零", "characters": ["爱芮", "南宫羽"], "artist": "ohichi16"},
                    recent_reminders=["妄想天使这张绝了，快来瞅瞅", "妄想天使这张绝了，快来瞅瞅"],
                )
                self.assertEqual(
                    self.send.await_args.args[0][2]["data"],
                    " 妄想天使这张绝了，快来瞅瞅",
                )

    async def test_disabled_during_wording_does_not_send(self):
        async def disable(*args, **kwargs):
            self.plugin._service.enabled = False
            return "这张配色挺舒服"
        with patch.object(plugin_module, "compose_reminder", side_effect=disable):
            self.assertFalse(await self.plugin._send_alert(message()))
        self.send.assert_not_awaited()

    async def test_fallback_wording_still_sends_native_at(self):
        # A context with no image database simulates a failed wording backend.
        self.assertTrue(await self.plugin._send_alert(message()))
        self.assertEqual(self.send.await_args.args[0][1]["type"], "at")
        self.assertIn(self.send.await_args.args[0][-1]["data"], {" " + item for item in plugin_module.compose_reminder.__globals__["FALLBACK_POOL"]})

    async def test_planner_before_request_injects_nudge_for_any_qq_group(self):
        await self.plugin.observe_message(message=message(message_id="777", group="other-qq-group", image_hash="h777"))
        items = [
            {"item_type": "UserMessageItem", "parts": [{"type": "text", "text": '<message msg_id="777" user="u1" is_self_message="false">发张图'}]},
            {"item_type": "UserMessageItem", "parts": [{"type": "text", "text": '<message msg_id="778" user="u2" is_self_message="false">这谁画的求出处'}]},
            {"item_type": "UserMessageItem", "parts": [{"type": "text", "text": "<instruction>思考</instruction>"}]},
        ]
        result = await self.plugin.on_planner_before_request(
            items=items,
            tool_definitions=[{"function": {"name": "lookup_image_source"}}],
        )
        new_items = result["modified_kwargs"]["items"]
        self.assertEqual(len(new_items), 4)
        self.assertIn("lookup_image_source", new_items[2]["parts"][0]["text"])
        self.assertIn("777", new_items[2]["parts"][0]["text"])

    async def test_tool_lookup_image_source_and_planner_dedup_skip(self):
        msg = message(message_id="888", image_hash="h888")
        await self.plugin.observe_message(message=msg)
        self.plugin._drain_queue()

        found = SourceInfo(
            engine="saucenao",
            similarity=94.2,
            work="zenless zone zero",
            characters=["aria"],
            artist="ohichi16",
            url="https://www.pixiv.net/artworks/152247186",
        )
        with patch.object(self.plugin, "_lookup_source_by_hashes", AsyncMock(return_value=found)) as lookup, \
             patch.object(plugin_module, "localize_names", AsyncMock(return_value=["爱芮"])):
            res = await self.plugin.tool_lookup_image_source(msg_id="888")
        lookup.assert_awaited_once_with(["h888"])
        self.assertTrue(res["success"])
        self.assertTrue(res["found"])
        self.assertEqual(res["work"], "绝区零")
        self.assertEqual(res["characters"], ["爱芮"])
        self.assertIn("https://www.pixiv.net/artworks/152247186", res["content"])

        await self.plugin.on_reply_before_post_process(
            session_id="test-stream-not-a-real-group",
            reply_message_id="888",
            response="这是《绝区零》的爱芮，画师 ohichi16，原帖 https://www.pixiv.net/artworks/152247186",
        )
        self.assertEqual(await self.plugin._send_alert(msg), "skipped")
        self.send.assert_not_awaited()

        # Unknown msg_id and empty lookup miss both fail gracefully.
        missing = await self.plugin.tool_lookup_image_source(msg_id="nonexistent")
        self.assertTrue(missing["success"])
        self.assertFalse(missing["found"])
        with patch.object(self.plugin, "_lookup_source_by_hashes", AsyncMock(return_value=None)):
            miss = await self.plugin.tool_lookup_image_source(msg_id="888")
        self.assertTrue(miss["success"])
        self.assertFalse(miss["found"])

    async def test_alert_skips_when_replied_without_source_and_follows_up_with_source(self):
        # Case A: Planner praised the image, reverse search found nothing -> skip quietly.
        msg_a = message(message_id="901", image_hash="h901")
        await self.plugin.observe_message(message=msg_a)
        self.plugin._drain_queue()
        await self.plugin.on_reply_before_post_process(
            session_id="test-stream-not-a-real-group",
            reply_message_id="901",
            response="这张光影和眼神好戳我！",
        )
        with patch.object(self.plugin, "_lookup_source", AsyncMock(return_value=None)):
            self.assertEqual(await self.plugin._send_alert(msg_a), "skipped")
        self.send.assert_not_awaited()

        # Case B: Planner praised the image, reverse search found source -> natural follow-up without repeat praise.
        msg_b = message(message_id="902", image_hash="h902")
        await self.plugin.observe_message(message=msg_b)
        self.plugin._drain_queue()
        await self.plugin.on_reply_before_post_process(
            session_id="test-stream-not-a-real-group",
            reply_message_id="902",
            response="这构图和配色也太绝了吧！",
        )
        found = SourceInfo(
            engine="saucenao",
            similarity=95.0,
            work="zenless zone zero",
            characters=["aria"],
            artist="ohichi16",
            url="https://www.pixiv.net/artworks/152247186",
        )
        with patch.object(self.plugin, "_lookup_source", AsyncMock(return_value=found)), \
             patch.object(plugin_module, "localize_names", AsyncMock(return_value=["爱芮"])), \
             patch.object(plugin_module, "compose_reminder", AsyncMock(return_value="对了顺手喊你来看，是绝区零的爱芮")) as compose:
            self.assertTrue(await self.plugin._send_alert(msg_b))
        compose.assert_awaited_once_with(
            msg_b,
            self.plugin.ctx,
            logger=self.plugin.ctx.logger,
            source={"work": "绝区零", "characters": ["爱芮"], "artist": "ohichi16"},
            previous_bot_reply="这构图和配色也太绝了吧！",
            mode="follow_up",
        )
        self.assertEqual(
            self.send.await_args.args[0][2]["data"],
            " 对了顺手喊你来看，是绝区零的爱芮\nhttps://www.pixiv.net/artworks/152247186",
        )

    async def test_reply_grace_period_waits_for_planner_reply(self):
        cfg = Plugin.build_default_config()
        cfg["alert"]["reply_grace_seconds"] = 2
        self.plugin.set_plugin_config(cfg)
        await self.plugin.on_config_update("self", cfg, "updated")

        now = 1000.0
        self.plugin._clock = lambda: now
        msg = message(message_id="950", image_hash="h950")
        msg["timestamp"] = str(now)
        await self.plugin.observe_message(message=msg)
        self.plugin._drain_queue()

        async def fake_sleep(delay):
            nonlocal now
            now += delay
            await self.plugin.on_reply_before_post_process(
                session_id="test-stream-not-a-real-group",
                reply_message_id="950",
                response="刚看到，好好看！",
            )

        with patch.object(plugin_module.asyncio, "sleep", side_effect=fake_sleep) as slept, \
             patch.object(self.plugin, "_lookup_source", AsyncMock(return_value=None)):
            self.assertEqual(await self.plugin._send_alert(msg), "skipped")
        slept.assert_awaited_once()
        self.send.assert_not_awaited()



    async def test_consecutive_alerts_pass_recent_wordings_per_stream(self):
        with patch.object(
            plugin_module,
            "compose_reminder",
            AsyncMock(side_effect=["第一句文案", "第二句文案", "另一群文案"]),
        ) as compose:
            msg1 = message(group="200000001", message_id="m1", image_hash="h1")
            msg2 = message(group="200000001", message_id="m2", image_hash="h2")
            msg3 = message(group="200000002", message_id="m3", image_hash="h3")
            msg3["session_id"] = "other-stream-id"
            self.assertTrue(await self.plugin._send_alert(msg1))
            self.assertTrue(await self.plugin._send_alert(msg2))
            self.assertTrue(await self.plugin._send_alert(msg3))

        self.assertEqual(compose.await_count, 3)
        self.assertNotIn("recent_reminders", compose.await_args_list[0].kwargs)
        self.assertEqual(compose.await_args_list[1].kwargs["recent_reminders"], ["第一句文案"])
        self.assertNotIn("recent_reminders", compose.await_args_list[2].kwargs)


if __name__ == "__main__":
    unittest.main()
