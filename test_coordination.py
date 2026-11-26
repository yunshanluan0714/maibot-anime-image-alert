"""Tests for source-inquiry detection and Planner/alert coordination."""

import unittest

import coordination as coord


def user_item(msg_id: str, user: str, text: str, *, is_self: bool = False, has_image: bool = False):
    parts = [{"type": "text", "text": f'<message msg_id="{msg_id}" user="{user}" is_self_message="{str(is_self).lower()}">{text}'}]
    if has_image:
        parts.append({"type": "image", "image_hash": "hash-" + msg_id})
    return {"item_type": "UserMessageItem", "parts": parts}


def call_item(func_name: str):
    return {"item_type": "FunctionCallItem", "tool_call": {"func_name": func_name}}


def instruction_item():
    return {"item_type": "UserMessageItem", "parts": [{"type": "text", "text": "<instruction>请思考下一步</instruction>"}]}


class InquiryDetectionTests(unittest.TestCase):
    def test_detects_same_message_and_followup_source_questions(self):
        same_msg = [user_item("101", "u1", "这是谁画的？求出处", has_image=True), instruction_item()]
        self.assertEqual(coord.detect_source_inquiry(same_msg), "101")

        followup = [
            user_item("102", "u1", "发一张好看的图", has_image=True),
            user_item("103", "u2", "好看诶"),
            user_item("104", "u3", "求问这张的画师和原帖链接"),
            instruction_item(),
        ]
        self.assertEqual(coord.detect_source_inquiry(followup), "102")

    def test_supports_text_only_known_image_msg_ids(self):
        items = [
            user_item("201", "u1", "一名蓝发少女站在雪地里"),
            user_item("202", "u2", "这谁啊求出处"),
        ]
        self.assertIsNone(coord.detect_source_inquiry(items))
        self.assertEqual(coord.detect_source_inquiry(items, known_image_msg_ids={"201"}), "201")

    def test_skips_when_already_called_or_already_replied_or_not_asking(self):
        not_asking = [user_item("301", "u1", "这张真不错", has_image=True)]
        self.assertIsNone(coord.detect_source_inquiry(not_asking))

        already_called = [
            user_item("302", "u1", "求出处", has_image=True),
            call_item(coord.SOURCE_TOOL_NAME),
        ]
        self.assertIsNone(coord.detect_source_inquiry(already_called))

        already_replied = [
            user_item("303", "u1", "求出处", has_image=True),
            user_item("304", "bot", "这是 Pixiv 画师 xxx 的作品", is_self=True),
            user_item("305", "u1", "谢谢"),
        ]
        self.assertIsNone(coord.detect_source_inquiry(already_replied))

        reasked_after_reply = [
            user_item("306", "u1", "发图", has_image=True),
            user_item("307", "bot", "挺好看的", is_self=True),
            user_item("308", "u1", "所以这到底是哪部番？"),
        ]
        self.assertEqual(coord.detect_source_inquiry(reasked_after_reply), "306")

    def test_inject_reminder_stays_before_trailing_instruction(self):
        items = [user_item("401", "u1", "求出处", has_image=True), instruction_item()]
        reminder = coord.build_reminder_item(coord.build_source_nudge_text("401"))
        injected = coord.inject_reminder(items, reminder)
        self.assertEqual(len(injected), 3)
        self.assertIn(coord.SOURCE_NUDGE_MARK, injected[1]["parts"][0]["text"])
        self.assertEqual(injected[2], items[1])


class TrackerAndDecisionTests(unittest.TestCase):
    def _msg(self, mid: str, image_hash: str = "h1", ts: float = 1000.0, text: str = ""):
        raw = [{"type": "image", "hash": image_hash}]
        if text:
            raw.append({"type": "text", "data": text})
        return {
            "platform": "qq",
            "account_id": "bot",
            "session_id": "s1",
            "message_id": mid,
            "timestamp": str(ts),
            "message_info": {
                "group_info": {"group_id": "200000001"},
                "user_info": {"user_id": "u1"},
            },
            "raw_message": raw,
        }

    def test_exact_and_window_reply_matching(self):
        now = 1000.0
        tracker = coord.InteractionTracker(clock=lambda: now)
        tracker.record_image_message(self._msg("10", "h10", ts=1000.0, text="分享美图"), now=1000.0)
        tracker.record_bot_reply("s1", "10", "光影好棒", now=1003.0)
        state = tracker.inspect(session_id="s1", msg_id="10", image_hashes=["h10"], message_ts=1000.0, now=1005.0)
        self.assertTrue(state.bot_replied)
        self.assertEqual(state.bot_reply_text, "光影好棒")
        self.assertFalse(state.bot_shared_link)
        self.assertEqual(state.sender_text, "分享美图")

        # Window matching when Planner replies without quoting msg_id, interrupted by newer image.
        tracker.record_image_message(self._msg("20", "h20", ts=1010.0), now=1010.0)
        tracker.record_bot_reply("s1", "", "这张也不错", now=1012.0)
        state_20 = tracker.inspect(session_id="s1", msg_id="20", image_hashes=["h20"], message_ts=1010.0, now=1013.0)
        self.assertTrue(state_20.bot_replied)

        tracker.record_image_message(self._msg("30", "h30", ts=1020.0), now=1020.0)
        tracker.record_image_message(self._msg("31", "h31", ts=1022.0), now=1022.0)
        tracker.record_bot_reply("s1", "", "这是31的回复", now=1025.0)
        state_30 = tracker.inspect(session_id="s1", msg_id="30", image_hashes=["h30"], message_ts=1020.0, now=1026.0)
        self.assertFalse(state_30.bot_replied)

    def test_message_ids_aliases_and_lookup_status_stay_in_their_session(self):
        tracker = coord.InteractionTracker(clock=lambda: 1000.0)
        mid = "same-long-message-id"
        tracker.record_image_message(dict(self._msg(mid, "first", ts=1000), session_id="s1"))
        tracker.record_image_message(dict(self._msg(mid, "second", ts=1000), session_id="s2"))
        alias = coord.to_display_message_id(mid)
        self.assertEqual(tracker.resolve_hashes("s1", alias), ["first"])
        self.assertEqual(tracker.resolve_hashes("s2", mid), ["second"])
        self.assertEqual(tracker.resolve_hashes("s3", alias), [])
        tracker.mark_planner_lookup(session_id="s1", msg_id=alias, image_hashes=["shared"], found=True)
        state = tracker.inspect(session_id="s2", msg_id=mid, image_hashes=["shared"], message_ts=1000)
        self.assertFalse(state.planner_looked_up)

    def test_long_message_id_alias_resolution(self):
        long_id = "ROBOT1.0_very_long_message_id_123456"
        alias = coord.to_display_message_id(long_id)
        self.assertTrue(alias.startswith("m") and len(alias) == 7)
        tracker = coord.InteractionTracker(clock=lambda: 1000.0)
        tracker.record_image_message(self._msg(long_id, "hlong", ts=1000.0), now=1000.0)
        self.assertIn(alias, tracker.known_image_msg_ids("s1", now=1001.0))
        self.assertEqual(tracker.resolve_hashes("s1", alias, now=1001.0), ["hlong"])
        tracker.mark_planner_lookup(session_id="s1", msg_id=alias, image_hashes=["hlong"], found=True, now=1002.0)
        tracker.record_bot_reply("s1", alias, "https://www.pixiv.net/artworks/1", now=1003.0)
        state = tracker.inspect(session_id="s1", msg_id=long_id, image_hashes=["hlong"], message_ts=1000.0, now=1004.0)
        self.assertTrue(state.planner_looked_up)
        self.assertTrue(state.bot_replied)
        self.assertTrue(state.bot_shared_link)

    def test_all_five_decision_branches(self):
        self.assertEqual(
            coord.decide_alert_action(
                coord.InteractionState(planner_looked_up=True, bot_replied=True),
                has_source=True,
            ),
            "skip_handled_by_planner",
        )
        self.assertEqual(
            coord.decide_alert_action(
                coord.InteractionState(bot_replied=True, bot_shared_link=True),
                has_source=True,
            ),
            "skip_already_shared",
        )
        self.assertEqual(
            coord.decide_alert_action(
                coord.InteractionState(bot_replied=True, bot_shared_link=False),
                has_source=False,
                skip_when_replied_without_source=True,
            ),
            "skip_replied_no_new_source",
        )
        self.assertEqual(
            coord.decide_alert_action(
                coord.InteractionState(bot_replied=True, bot_shared_link=False),
                has_source=True,
            ),
            "send_follow_up",
        )
        self.assertEqual(
            coord.decide_alert_action(
                coord.InteractionState(bot_replied=True, bot_shared_link=False),
                has_source=False,
                skip_when_replied_without_source=False,
            ),
            "send_follow_up",
        )
        self.assertEqual(
            coord.decide_alert_action(
                coord.InteractionState(bot_replied=False),
                has_source=False,
            ),
            "send_normal",
        )


if __name__ == "__main__":
    unittest.main()
