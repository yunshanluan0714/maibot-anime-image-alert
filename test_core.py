import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from core import AlertService


class AlertTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.now = 200000.0
        self.s = AlertService(self.temp.name, {"200000001", "200000002"}, "100000001", cooldown_seconds=600, hourly_limit=3, clock=lambda: self.now)
        self.classify = AsyncMock(return_value=True)
        self.send = AsyncMock(return_value=True)

    async def asyncTearDown(self):
        self.s.close()
        self.temp.cleanup()

    def message(self, mid="1", image_hash="abc", group="200000001", sender="123"):
        return dict(platform="qq", account_id="999", session_id="stream1", message_id=mid,
            timestamp=str(self.now), raw_message=[dict(type="image", hash=image_hash)],
            message_info=dict(group_info=dict(group_id=group), user_info=dict(user_id=sender)))

    async def process(self, message):
        return await self.s.process(message, self.classify, self.send)

    async def test_sends_once_across_groups(self):
        self.assertEqual(await self.process(self.message()), "sent")
        self.assertEqual(await self.process(self.message(mid="2", group="200000002")), "duplicate")
        self.send.assert_awaited_once()
        self.classify.assert_awaited_once()

    async def test_scope_bot_commands_emoji_and_old(self):
        for msg in [self.message(group="1"), self.message(sender="999"),
                    dict(self.message(), is_command=True), dict(self.message(), is_notify=True),
                    dict(self.message(), platform="other"), dict(self.message(), timestamp="invalid"),
                    dict(self.message(), timestamp=str(self.now - 301)),
                    dict(self.message(), raw_message=[dict(type="emoji", hash="a")])]:
            self.assertEqual(await self.process(msg), "ineligible")
        self.classify.assert_not_awaited()

    async def test_cooldown_can_exceed_hourly_window(self):
        self.s.cooldown_seconds = 7200
        self.assertEqual(await self.process(self.message()), "sent")
        self.now += 3601
        self.assertEqual(await self.process(self.message("2", "new")), "rate_limited")
        self.now += 3599
        self.assertEqual(await self.process(self.message("2", "new")), "sent")

    async def test_cooldown_and_hourly_quota(self):
        self.assertEqual(await self.process(self.message()), "sent")
        self.assertEqual(await self.process(self.message("2", "def")), "rate_limited")
        for i in range(2):
            self.now += 601
            self.assertEqual(await self.process(self.message(str(i+2), str(i))), "sent")
        self.now += 601
        self.assertEqual(await self.process(self.message("5", "zzz")), "rate_limited")
        self.now += 1800
        self.assertEqual(await self.process(self.message("6", "xxx")), "sent")

    async def test_restart_keeps_dedup_and_quota(self):
        self.assertEqual(await self.process(self.message()), "sent")
        self.s.close()
        self.s = AlertService(self.temp.name, {"200000001", "200000002"}, "100000001", cooldown_seconds=600, hourly_limit=3, clock=lambda: self.now)
        self.assertEqual(await self.process(self.message("2", "new")), "rate_limited")
        self.now += 601
        self.assertEqual(await self.process(self.message("3")), "duplicate")

    async def test_disable_while_classifying(self):
        async def classify(_):
            self.s.enabled = False
            return True
        self.classify.side_effect = classify
        self.assertEqual(await self.process(self.message()), "ineligible")
        self.send.assert_not_awaited()

    async def test_uncertain_classification_is_no_send(self):
        self.classify.return_value = "true"
        self.assertEqual(await self.process(self.message()), "not_matched")
        self.send.assert_not_awaited()

    async def test_unknown_send_is_not_retried(self):
        self.send.side_effect = TimeoutError()
        self.assertEqual(await self.process(self.message()), "unknown")
        self.now += 601
        self.assertEqual(await self.process(self.message()), "duplicate")
        self.send.assert_awaited_once()

    async def test_multimage_filters_already_seen(self):
        self.classify.return_value = False
        self.assertEqual(await self.process(self.message()), "not_matched")
        msg = self.message("2")
        msg["raw_message"].append(dict(type="image", hash="new"))
        self.classify.return_value = True
        self.assertEqual(await self.process(msg), "sent")
        self.assertEqual(self.classify.await_args.args[0]["raw_message"], [dict(type="image", hash="new")])

    async def test_concurrent_messages_no_double_send(self):
        a, b = await asyncio.gather(self.process(self.message()), self.process(self.message("2")))
        self.assertEqual(a, "sent")
        self.assertIn(b, ("rate_limited", "duplicate"))
        self.send.assert_awaited_once()

    async def test_seen_expires_in_24_hours(self):
        self.assertEqual(await self.process(self.message()), "sent")
        self.now += 86401
        self.assertEqual(await self.process(self.message("2")), "sent")

    async def test_unlimited_allows_back_to_back_and_keeps_dedup(self):
        self.s.cooldown_seconds = 0
        self.s.hourly_limit = 0
        for i in range(8):
            self.assertEqual(await self.process(self.message(str(i), str(i))), "sent")
        self.assertEqual(await self.process(self.message("again", "0")), "duplicate")
        self.assertEqual(self.send.await_count, 8)

    async def test_cooldown_only_with_no_hourly_cap(self):
        self.s.hourly_limit = 0
        self.assertEqual(await self.process(self.message()), "sent")
        self.assertEqual(await self.process(self.message("2", "new")), "rate_limited")
        self.now += 601
        self.assertEqual(await self.process(self.message("2", "new")), "sent")

    async def test_default_unlimited_survives_restart(self):
        self.s.close()
        self.s = AlertService(self.temp.name, {"200000001"}, "100000001", clock=lambda: self.now)
        self.assertEqual(await self.process(self.message()), "sent")
        self.s.close()
        self.s = AlertService(self.temp.name, {"200000001"}, "100000001", clock=lambda: self.now)
        self.assertEqual(await self.process(self.message("2", "next")), "sent")
        self.assertEqual(await self.process(self.message("3", "abc")), "duplicate")

    async def test_target_user_own_image_is_skipped(self):
        own_msg = self.message(sender="100000001")
        self.assertFalse(self.s.eligible(own_msg))
        self.assertEqual(await self.process(own_msg), "ineligible")
        self.classify.assert_not_awaited()
        self.send.assert_not_awaited()

    async def test_skipped_send_frees_quota_reservation_but_keeps_seen_dedup(self):
        self.send.return_value = "skipped"
        self.assertEqual(await self.process(self.message("1", "abc")), "skipped")
        self.assertEqual(self.s.db.execute("SELECT COUNT(*) FROM sends").fetchone()[0], 0)
        # Same image remains deduplicated across 24h, while a new image is not blocked by cooldown.
        self.assertEqual(await self.process(self.message("2", "abc")), "duplicate")
        self.send.return_value = True
        self.assertEqual(await self.process(self.message("3", "def")), "sent")


if __name__ == "__main__":
    unittest.main()
