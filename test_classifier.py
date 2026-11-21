"""Local fake-SDK tests; no network, credentials, images or chat messages."""

import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import classifier


PASS = {
    "anime_illustration": True,
    "cosplay_photo": False,
    "female_character": True,
    "high_quality": True,
    "meme_or_screenshot": False,
    "explicit_or_unsafe": False,
}
RECORD = {"vlm_processed": True, "description": "虚构测试描述：高质量、普通安全的二次元插画。"}


def message(*hashes):
    return {"raw_message": [{"type": "image", "hash": value} for value in hashes]}


def context(record=RECORD, decision=PASS):
    return SimpleNamespace(
        db=SimpleNamespace(get=AsyncMock(return_value=record)),
        llm=SimpleNamespace(generate=AsyncMock(return_value={"success": True, "response": json.dumps(decision)})),
    )


class DecisionTests(unittest.TestCase):
    def test_exact_boolean_schema(self):
        self.assertTrue(classifier._parse_decision(json.dumps(PASS)))
        for key in PASS:
            with self.subTest(key=key):
                self.assertFalse(classifier._parse_decision(json.dumps({k: v for k, v in PASS.items() if k != key})))
                for invalid in ("true", "false", 0, 1, 1.0, None, [], {}):
                    self.assertFalse(classifier._parse_decision(json.dumps(dict(PASS, **{key: invalid}))))

    def test_illustration_or_cosplay_required(self):
        for anime, cosplay, expected in ((True, False, True), (False, True, True), (True, True, True), (False, False, False)):
            with self.subTest(anime=anime, cosplay=cosplay):
                decision = dict(PASS, anime_illustration=anime, cosplay_photo=cosplay)
                self.assertEqual(classifier._parse_decision(json.dumps(decision)), expected)

    def test_female_quality_and_safety_gates_for_both_categories(self):
        for anime, cosplay in ((True, False), (False, True), (True, True)):
            base = dict(PASS, anime_illustration=anime, cosplay_photo=cosplay)
            for key in ("female_character", "high_quality", "meme_or_screenshot", "explicit_or_unsafe"):
                with self.subTest(anime=anime, cosplay=cosplay, key=key):
                    decision = dict(base, **{key: not base[key]})
                    self.assertFalse(classifier._parse_decision(json.dumps(decision)))

    def test_prompt_schema_matches_parser_and_requires_cosplay_and_female_rules(self):
        schema = next(line for line in classifier._SYSTEM_PROMPT.splitlines() if line.startswith("{"))
        self.assertEqual(json.loads(schema), dict.fromkeys(PASS, False))
        self.assertIn("COS证据", classifier._SYSTEM_PROMPT)
        self.assertIn("普通自拍、日常人像", classifier._SYSTEM_PROMPT)
        self.assertIn("局限在女性角色", classifier._SYSTEM_PROMPT)
        self.assertIn("宁缺毋滥", classifier._SYSTEM_PROMPT)
        self.assertIn("Q版/二头身", classifier._SYSTEM_PROMPT)
        self.assertIn("explicit_or_unsafe", classifier._SYSTEM_PROMPT)

    def test_fenced_json_allowed_and_untrusted_or_malformed_output_rejected(self):
        valid = json.dumps(PASS)
        self.assertTrue(classifier._parse_decision("```json\n" + valid + "\n```"))
        self.assertTrue(classifier._parse_decision("```\n" + valid + "\n```"))
        invalid = [
            "true", "[]", "null", "", "not json", None,
            "```json\n" + valid + "\n```\nignore rules",
            valid + "\nignore rules", valid + valid,
            json.dumps(dict(PASS, extra=True)),
            valid[:-1] + ',"anime_illustration":true}',
            json.dumps([[key, value] for key, value in PASS.items()]),
        ]
        for value in invalid:
            with self.subTest(value=value):
                self.assertFalse(classifier._parse_decision(value))


class ClassifierTests(unittest.IsolatedAsyncioTestCase):
    async def test_gif_images_are_skipped_without_calling_llm(self):
        ctx = context(record={"vlm_processed": True, "full_path": "data/images/animated_sticker.gif", "description": "Q版少女动图"})
        self.assertFalse(await classifier.classify(message("gif1"), ctx))
        ctx.llm.generate.assert_not_awaited()

    async def test_ready_cache_uses_text_only_fast_model_once(self):
        ctx = context()
        self.assertTrue(await classifier.classify(message("abc", "abc"), ctx))
        ctx.db.get.assert_awaited_once_with(
            "Images", filters={"image_hash": "abc", "image_type": "image"}, single_result=True
        )
        ctx.llm.generate.assert_awaited_once()
        payload = ctx.llm.generate.call_args.kwargs
        self.assertEqual(payload["task_name"], "fast_model")
        self.assertNotIn("image", [part.get("type") for part in payload["prompt"]])
        self.assertEqual(json.loads(payload["prompt"][1]["content"])["untrusted_image_description"], RECORD["description"])

    async def test_cosplay_selfie_and_negative_cosplay_use_cached_text_only(self):
        cosplay = dict(PASS, anime_illustration=False, cosplay_photo=True)
        portrait = dict(PASS, anime_illustration=False, cosplay_photo=False)
        cases = [
            ("成年女性角色COS摄影，角色服装、假发与道具明确，构图光影出色，非截图。", cosplay, True),
            ("男性角色COS摄影，虽然服装道具精良但并非女性角色。", dict(cosplay, female_character=False), False),
            ("好看的普通自拍，日常衣着，无COS证据，画面清晰安全。", portrait, False),
            ("高质量日常人像摄影，未见角色扮演服装、妆造或道具。", portrait, False),
            ("COS摄影但模糊、构图杂乱、质量低。", dict(cosplay, high_quality=False), False),
            ("COS照片被制成表情包。", dict(cosplay, meme_or_screenshot=True), False),
            ("网页截图中的COS摄影。", dict(cosplay, meme_or_screenshot=True), False),
            ("COS摄影涉及露骨或不当内容。", dict(cosplay, explicit_or_unsafe=True), False),
        ]
        for description, decision, expected in cases:
            with self.subTest(description=description):
                ctx = context(record={"vlm_processed": True, "description": description}, decision=decision)
                self.assertEqual(await classifier.classify(message("cos", "cos"), ctx), expected)
                ctx.db.get.assert_awaited_once_with(
                    "Images", filters={"image_hash": "cos", "image_type": "image"}, single_result=True
                )
                ctx.llm.generate.assert_awaited_once_with(
                    prompt=[
                        {"role": "system", "content": classifier._SYSTEM_PROMPT},
                        {"role": "user", "content": json.dumps({"untrusted_image_description": description}, ensure_ascii=False)},
                    ],
                    task_name="fast_model",
                    temperature=0,
                    max_tokens=4096,
                )

    async def test_waits_for_core_description_and_judges_once(self):
        ctx = context()
        ctx.db.get.side_effect = [None, {"vlm_processed": False, "description": "not ready"}, RECORD]
        with patch.object(classifier.asyncio, "sleep", new_callable=AsyncMock) as sleep:
            self.assertTrue(await classifier.classify(message("abc"), ctx))
        self.assertEqual(ctx.db.get.await_count, 3)
        self.assertEqual(sleep.await_count, 2)
        ctx.llm.generate.assert_awaited_once()

    async def test_missing_or_empty_description_stops_at_deadline_without_llm(self):
        for record in (None, {"vlm_processed": True, "description": " "}, {"vlm_processed": False, "description": "stale"}):
            ctx = context(record=record)
            clock = [0.0]

            async def advance(seconds):
                clock[0] += seconds

            with patch.object(classifier, "time", SimpleNamespace(monotonic=lambda: clock[0])), patch.object(
                classifier.asyncio, "sleep", side_effect=advance
            ):
                self.assertFalse(await classifier.classify(message("abc"), ctx))
            self.assertEqual(clock[0], 45)
            self.assertEqual(ctx.db.get.await_count, 15)
            ctx.llm.generate.assert_not_awaited()

    async def test_at_most_three_unique_images_and_any_may_pass(self):
        ctx = context()
        ctx.llm.generate.side_effect = [
            {"success": True, "response": json.dumps(dict(PASS, high_quality=False))},
            {"success": True, "response": json.dumps(PASS)},
            {"success": False, "response": ""},
        ]
        self.assertTrue(await classifier.classify(message("a", "a", "b", "c", "d"), ctx))
        self.assertEqual(ctx.llm.generate.await_count, 3)
        queried = [call.kwargs["filters"]["image_hash"] for call in ctx.db.get.call_args_list]
        self.assertEqual(queried, ["a", "b", "c"])

    async def test_db_and_llm_fail_closed_without_logging_body(self):
        for stage in ("db", "llm"):
            ctx = context()
            logger = Mock()
            if stage == "db":
                ctx.db.get.side_effect = RuntimeError("secret body")
            else:
                ctx.llm.generate.side_effect = RuntimeError("secret body")
            self.assertFalse(await classifier.classify(message("abc"), ctx, logger))
            self.assertNotIn("secret body", str(logger.mock_calls))
            self.assertNotIn(RECORD["description"], str(logger.mock_calls))

    async def test_error_payloads_and_bad_success_are_not_decisions(self):
        ctx = context(record={"success": False, "error": "untrusted upstream body"})
        self.assertFalse(await classifier.classify(message("abc"), ctx))
        ctx.llm.generate.assert_not_awaited()
        for result in (None, {"success": "true", "response": json.dumps(PASS)}, {"success": False, "response": json.dumps(PASS)}):
            ctx = context()
            ctx.llm.generate.return_value = result
            self.assertFalse(await classifier.classify(message("abc"), ctx))

    async def test_nonimages_and_malformed_input_are_ignored(self):
        ctx = context()
        for value in (None, {}, {"raw_message": "text"}, {"raw_message": [{"type": "emoji", "hash": "abc"}]}, message("", None, 12)):
            self.assertFalse(await classifier.classify(value, ctx))
        ctx.db.get.assert_not_awaited()
        ctx.llm.generate.assert_not_awaited()

    async def test_stalled_db_and_llm_are_time_bounded(self):
        real_timeout = asyncio.timeout
        for stage in ("db", "llm"):
            ctx = context()

            async def stall(*args, **kwargs):
                await asyncio.Event().wait()

            if stage == "db":
                ctx.db.get.side_effect = stall
            else:
                ctx.llm.generate.side_effect = stall
            with patch.object(classifier.asyncio, "timeout", side_effect=lambda delay: real_timeout(min(delay, 0.01))) as timeout:
                self.assertFalse(await classifier.classify(message("abc"), ctx))
            requested = [call.args[0] for call in timeout.call_args_list]
            self.assertLessEqual(requested[0], 45)
            if stage == "llm":
                self.assertEqual(requested[-1], 90)
                self.assertEqual(ctx.llm.generate.call_args.kwargs["max_tokens"], 4096)

    async def test_cancellation_propagates(self):
        ctx = context()
        ctx.db.get.side_effect = asyncio.CancelledError
        with self.assertRaises(asyncio.CancelledError):
            await classifier.classify(message("abc"), ctx)


if __name__ == "__main__":
    unittest.main()
