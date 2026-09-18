import importlib.util
import json
import pathlib
import sys
import unittest
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo


MODULE_PATH = (
    pathlib.Path(__file__).parents[1]
    / "services"
    / "daily-social"
    / "daily_social.py"
)
SPEC = importlib.util.spec_from_file_location("daily_social", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class DailySocialTests(unittest.TestCase):
    def test_luna_uses_reasoning_effort_without_temperature(self):
        original_model = MODULE.OPENAI_MODEL
        original_effort = MODULE.OPENAI_REASONING_EFFORT
        MODULE.OPENAI_MODEL = "gpt-5.6-luna"
        MODULE.OPENAI_REASONING_EFFORT = "none"
        try:
            options = MODULE.chat_model_options("test-key", 0.3)
        finally:
            MODULE.OPENAI_MODEL = original_model
            MODULE.OPENAI_REASONING_EFFORT = original_effort

        self.assertEqual(options["reasoning_effort"], "none")
        self.assertNotIn("temperature", options)

    def test_selects_today_and_future_two_days(self):
        now = datetime(2026, 9, 17, 7, tzinfo=ZoneInfo("America/New_York"))
        data = {
            "events": [
                {"event_id": "today", "name": "Today", "date": "2026-09-17"},
                {"event_id": "tomorrow", "name": "Tomorrow", "date": "2026-09-18"},
                {"event_id": "later", "name": "Later", "date": "2026-09-20"},
            ]
        }
        selected = MODULE.select_social_events(data, {}, now)
        self.assertEqual({event["event_id"] for event in selected}, {"today", "tomorrow"})

    def test_keeps_full_candidate_ranking_for_analysis(self):
        now = datetime(2026, 9, 17, 7, tzinfo=ZoneInfo("America/New_York"))
        data = {
            "events": [
                {
                    "event_id": f"event-{index}",
                    "name": f"Event {index}",
                    "date": "2026-09-17",
                    "recommendation_score": 80 - index,
                }
                for index in range(7)
            ]
        }
        ranked = MODULE.rank_social_events(data, {}, now)
        selected = MODULE.select_social_events(data, {}, now)
        self.assertEqual(len(ranked), 7)
        self.assertEqual(len(selected), MODULE.MAX_SOCIAL_EVENTS)
        self.assertEqual(
            [event["event_id"] for event in selected],
            [event["event_id"] for event in ranked[: MODULE.MAX_SOCIAL_EVENTS]],
        )

    def test_ai_can_rank_destination_event_above_nearby_routine_event(self):
        candidates = [
            {
                "event_id": "local",
                "name": "Routine nearby activity",
                "recommendation_score": 90,
            },
            {
                "event_id": "revere-sand-festival",
                "name": "Revere Sand Sculpting Festival",
                "recommendation_score": 80,
            },
        ]
        response = json.dumps(
            {
                "rankings": [
                    {
                        "event_id": "revere-sand-festival",
                        "score": 90,
                        "dimensions": {
                            "leisure_appeal": 24,
                            "local_significance": 24,
                            "rarity": 19,
                            "value": 8,
                            "proximity_fit": 6,
                            "information_confidence": 9,
                        },
                        "destination_worthy": True,
                        "significance_signals": ["annual", "community landmark"],
                        "reason_zh": "年度代表性活動，值得專程前往。",
                        "reason_en": "A distinctive annual destination event.",
                    },
                    {
                        "event_id": "local",
                        "score": 66,
                        "dimensions": {
                            "leisure_appeal": 20,
                            "local_significance": 10,
                            "rarity": 8,
                            "value": 8,
                            "proximity_fit": 10,
                            "information_confidence": 10,
                        },
                        "destination_worthy": False,
                        "significance_signals": [],
                        "reason_zh": "方便但較日常。",
                        "reason_en": "Convenient but routine.",
                    },
                ]
            }
        )
        ranked = MODULE.parse_ai_rankings(response, candidates)
        selected = MODULE.choose_social_events(ranked)
        self.assertEqual(selected[0]["event_id"], "revere-sand-festival")
        self.assertEqual(selected[0]["selection_lane"], "ai_destination_worthy")
        self.assertEqual(selected[0]["ai_ranking"]["dimensions"]["rarity"], 19)

    def test_campaign_stores_auditable_free_ranking(self):
        original_s3 = MODULE.S3
        MODULE.S3 = MagicMock()
        now = datetime(2026, 9, 17, 7, tzinfo=ZoneInfo("America/New_York"))
        ranked = [
            {
                "event_id": "revere-festival",
                "recommendation_score": 80,
                "social_score": 90,
                "ai_ranking": {"score": 90, "destination_worthy": True},
                "selection_lane": "ai_destination_worthy",
            },
            {
                "event_id": "bu-concert",
                "recommendation_score": 90,
                "social_score": 66,
                "ai_ranking": {"score": 66, "destination_worthy": False},
                "selection_lane": "ai_semantic_rank",
            },
        ]
        try:
            MODULE.store_campaign(
                {
                    "zh": {"title": "今日活動", "body": "今天的活動。"},
                    "en": {"title": "Boston today", "body": "Today's events."},
                    "hashtags": ["Boston"],
                },
                ranked,
                {},
                now,
                ranked_events=ranked,
            )
            campaign = json.loads(MODULE.S3.put_object.call_args_list[0].kwargs["Body"])
        finally:
            MODULE.S3 = original_s3

        self.assertFalse(campaign["ranking_policy"]["fixed_local_quota"])
        decisions = {
            decision["event_id"]: decision
            for decision in campaign["selection_decisions"]
        }
        self.assertEqual(decisions["revere-festival"]["base_score_rank"], 2)
        self.assertEqual(decisions["revere-festival"]["final_score_rank"], 1)
        self.assertEqual(decisions["revere-festival"]["final_selection_rank"], 1)
        self.assertEqual(decisions["revere-festival"]["ai_ranking"]["score"], 90)
        self.assertEqual(
            campaign["ranking_policy"]["method"], "bobo_ai_semantic_ranking"
        )
        self.assertEqual(campaign["openai_call_count"], 2)

    def test_ai_ranking_clamps_dimension_overflow_and_records_warning(self):
        response = json.dumps(
            {
                "rankings": [
                    {
                        "event_id": "event-1",
                        "score": 110,
                        "dimensions": {
                            "leisure_appeal": 30,
                            "local_significance": 25,
                            "rarity": 20,
                            "value": 10,
                            "proximity_fit": 10,
                            "information_confidence": 15,
                        },
                        "destination_worthy": True,
                        "significance_signals": [],
                        "reason_zh": "測試",
                        "reason_en": "Test",
                    }
                ]
            }
        )
        ranked = MODULE.parse_ai_rankings(
            response, [{"event_id": "event-1", "name": "Event"}]
        )
        self.assertEqual(ranked[0]["ai_ranking"]["score"], 100)
        self.assertEqual(
            ranked[0]["ai_ranking"]["raw_dimensions"]["leisure_appeal"], 30
        )
        self.assertTrue(ranked[0]["ai_ranking"]["normalization_warnings"])

    def test_enforces_48_hour_cooldown(self):
        eastern = ZoneInfo("America/New_York")
        now = datetime(2026, 9, 17, 7, tzinfo=eastern)
        data = {
            "events": [
                {"event_id": "recent", "name": "Recent", "date": "2026-09-17"},
                {"event_id": "old", "name": "Old", "date": "2026-09-17"},
            ]
        }
        history = {
            "last_selected": {
                "recent": (now - timedelta(hours=47)).isoformat(),
                "old": (now - timedelta(hours=49)).isoformat(),
            }
        }
        selected = MODULE.select_social_events(data, history, now)
        self.assertEqual([event["event_id"] for event in selected], ["old"])

    def test_excludes_unavailable_events(self):
        now = datetime(2026, 9, 17, 7, tzinfo=ZoneInfo("America/New_York"))
        data = {
            "events": [
                {
                    "event_id": "sold-out",
                    "name": "Sold out show",
                    "date": "2026-09-17",
                    "availability_status": "sold_out",
                },
                {
                    "event_id": "available",
                    "name": "Available show",
                    "date": "2026-09-17",
                    "availability_status": "onsale",
                },
            ]
        }
        selected = MODULE.select_social_events(data, {}, now)
        self.assertEqual([event["event_id"] for event in selected], ["available"])

    def test_renders_one_identical_shared_post(self):
        content = {
            "zh": {
                "title": "Boston 今日活動",
                "body": "今天可以去公園聽音樂。",
            },
            "en": {
                "title": "What's on in Boston today",
                "body": "Listen to live music in the park today.",
            },
            "hashtags": ["Boston", "波士頓生活"],
        }
        rendered = MODULE.render_shared_text(content)
        self.assertIn("Boston 今日活動", rendered)
        self.assertLess(
            rendered.index("Boston 今日活動"),
            rendered.index("What's on in Boston today"),
        )
        self.assertIn("—— English ——", rendered)
        self.assertIn("#Boston #波士頓生活", rendered)
        self.assertIn("— 波波 Bo ⌖ˎˊ˗ 〔•ᴗ•〕ゞ", rendered)

    def test_parses_structured_bilingual_content(self):
        content = MODULE.parse_model_json(
            """{
                "zh": {"title": "今日活動", "body": "中文內容"},
                "en": {"title": "Today's events", "body": "English copy"},
                "hashtags": ["#Boston", "週末去哪"]
            }"""
        )
        self.assertEqual(content["zh"]["body"], "中文內容")
        self.assertEqual(content["en"]["body"], "English copy")
        self.assertEqual(content["hashtags"], ["Boston", "週末去哪"])

    def test_parser_preserves_raw_kaomoji_backslash_as_valid_json(self):
        content = MODULE.parse_model_json(
            r'''{
                "zh": {"title": "今日活動", "body": "出門 \(≧▽≦)/"},
                "en": {"title": "Today", "body": "Let's go \(≧▽≦)/"},
                "hashtags": ["Boston"]
            }'''
        )
        self.assertIn(r"\(≧▽≦)/", content["zh"]["body"])

    def test_prompt_requires_traditional_chinese(self):
        prompt_text = str(MODULE.build_prompt())
        self.assertIn("Traditional Chinese", prompt_text)
        self.assertIn("Never use Simplified Chinese", prompt_text)
        self.assertIn("instead of a numbered or repetitive list", prompt_text)
        self.assertIn("conversational tiny story", prompt_text)
        self.assertIn("350-500 Traditional Chinese characters", prompt_text)
        self.assertIn("220-300 English words", prompt_text)
        self.assertIn("2-3 varied kaomoji", prompt_text)
        self.assertIn("emotional punctuation", prompt_text)
        self.assertIn("fixed top expression", prompt_text.replace("\n", " "))
        self.assertIn("optional flavor, not a quota", prompt_text.replace("\n", " "))
        self.assertIn("Taiwan-style zhuyin", prompt_text)

    def test_ranking_prompt_uses_identity_memory_and_no_distance_quota(self):
        prompt = MODULE.build_ranking_prompt()
        prompt_text = str(prompt)
        self.assertIn("Runtime identity", prompt_text)
        self.assertIn("long-term preference memory", prompt_text)
        self.assertIn("never a quota or veto", prompt_text)
        self.assertIn("local_significance", prompt_text)
        rendered = prompt.format(
            persona_json="{}",
            memory_json="{}",
            events_json="[]",
            candidate_count=0,
            selection_count=0,
        )
        self.assertIn('{"rankings"', rendered)

    def test_rejects_model_generated_emoji(self):
        with self.assertRaisesRegex(ValueError, "contained emoji"):
            MODULE.parse_model_json(
                """{
                    "zh": {"title": "今日活動", "body": "出門走走☀️"},
                    "en": {"title": "Today", "body": "Go outside"},
                    "hashtags": ["Boston"]
                }"""
            )

    def test_generate_content_repairs_emoji_validation_failure_once(self):
        responses = iter(
            [
                MagicMock(
                    content=json.dumps(
                        {
                            "zh": {"title": "今日", "body": "先出門☀️"},
                            "en": {"title": "Today", "body": "Go outside"},
                            "hashtags": ["Boston"],
                        },
                        ensure_ascii=False,
                    ),
                    usage_metadata={"total_tokens": 10},
                ),
                MagicMock(
                    content=json.dumps(
                        {
                            "zh": {
                                "title": "今日",
                                "body": "先出門 〔•̀ᴗ•́〕و，再散步 (≧▽≦)。",
                            },
                            "en": {
                                "title": "Today",
                                "body": "Head out 〔´ᴗ`〕～ then wander (•̀ᴗ•́).",
                            },
                            "hashtags": ["Boston"],
                        },
                        ensure_ascii=False,
                    ),
                    usage_metadata={"total_tokens": 12},
                ),
            ]
        )

        class FakePrompt:
            def __or__(self, model):
                return self

            def invoke(self, values):
                return next(responses)

        with patch("langchain_openai.ChatOpenAI"), patch.object(
            MODULE, "get_openai_api_key", return_value="test-key"
        ), patch.object(MODULE, "build_prompt", return_value=FakePrompt()), patch.object(
            MODULE, "build_kaomoji_repair_prompt", return_value=FakePrompt()
        ):
            content, metadata = MODULE.generate_content(
                [], datetime(2026, 9, 18, 7, tzinfo=ZoneInfo("America/New_York"))
            )

        self.assertNotIn("☀️", json.dumps(content, ensure_ascii=False))
        self.assertTrue(metadata["retried"])
        self.assertEqual(metadata["openai_call_count"], 2)
        self.assertEqual(metadata["token_usage"]["total_tokens"], 22)

    def test_accepts_contextual_kaomoji_inside_both_language_bodies(self):
        content = MODULE.parse_model_json(
            """{
                "zh": {"title": "今日活動", "body": "天氣很配合 〔•̀ᴗ•́〕و 可以出門。"},
                "en": {"title": "Today's events", "body": "The weather cooperates 〔´ᴗ`〕～ so let's go."},
                "hashtags": ["Boston"]
            }"""
        )
        self.assertIn("〔•̀ᴗ•́〕و", content["zh"]["body"])
        self.assertIn("〔´ᴗ`〕～", content["en"]["body"])

    def test_detects_varied_contextual_kaomoji_without_counting_asides(self):
        text = (
            "天氣很配合 〔•̀ᴗ•́〕و，可以出門（但記得帶外套）。"
            "看到年度活動時真的會 \\(≧▽≦)/，最後再開心一下 "
            "(((o(*ﾟ▽ﾟ*)o)))。"
        )
        matches = MODULE.extract_contextual_kaomoji(text)
        self.assertEqual(len(matches), 3)
        self.assertFalse(MODULE.extract_contextual_kaomoji("先散步（但記得帶外套）。"))

    def test_contextual_kaomoji_contract_requires_two_varied_faces_per_language(self):
        valid = {
            "zh": {"title": "今日活動", "body": "出門 〔•̀ᴗ•́〕و，選擇困難 (≧▽≦)。"},
            "en": {"title": "Today", "body": "Let's go 〔´ᴗ`〕～ or wander (•̀ᴗ•́)."},
            "hashtags": ["Boston"],
        }
        MODULE.validate_contextual_kaomoji(valid)
        invalid = {
            **valid,
            "en": {"title": "Today", "body": "Only a normal aside (bring a coat)."},
        }
        with self.assertRaisesRegex(ValueError, "en body needs at least 2"):
            MODULE.validate_contextual_kaomoji(invalid)

    def test_kaomoji_is_deterministic_for_same_campaign(self):
        now = datetime(2026, 9, 17, 7, tzinfo=ZoneInfo("America/New_York"))
        events = [
            {
                "event_id": "music-1",
                "name": "Live jazz by the harbor",
                "date": "2026-09-17",
            }
        ]
        first = MODULE.select_kaomoji(events, now)
        second = MODULE.select_kaomoji(events, now)
        self.assertEqual(first, second)
        self.assertTrue(first.startswith("⌖ˎˊ˗"))

    def test_render_adds_one_mood_face_and_fixed_signature(self):
        content = {
            "zh": {"title": "今晚去哪裡", "body": "散步看表演。"},
            "en": {"title": "Tonight in Boston", "body": "Take a walk."},
            "hashtags": ["Boston"],
        }
        mood = "⌖ˎˊ˗ 〔✦ᴗ✦〕ノ"
        rendered = MODULE.render_shared_text(content, mood)
        self.assertEqual(rendered.count(mood), 1)
        self.assertTrue(rendered.endswith("— 波波 Bo ⌖ˎˊ˗ 〔•ᴗ•〕ゞ"))

    def test_threads_introduction_is_bilingual_single_post_with_report_link(self):
        text = MODULE.render_threads_introduction()
        self.assertIn("嗨，我是波波", text)
        self.assertIn("Hi, I'm Bo", text)
        self.assertTrue(text.endswith("— 波波 Bo ⌖ˎˊ˗ 〔•ᴗ•〕ゞ"))
        self.assertIn(MODULE.WEBSITE_URL, text)
        self.assertIn("—— English ——", text)
        self.assertEqual(len(MODULE.split_threads_text(text)), 1)
        self.assertFalse(MODULE.EMOJI_PATTERN.search(text))

    def test_introduction_preview_is_safe_and_uses_no_openai_call(self):
        now = datetime(2026, 9, 18, 8, tzinfo=ZoneInfo("America/New_York"))
        result = MODULE.handle_introduction(
            {"mode": "introduction", "dry_run": True}, now
        )
        self.assertTrue(result["dry_run"])
        self.assertEqual(result["openai_call_count"], 0)
        self.assertEqual(result["threads"]["status"], "disabled_dry_run")

    def test_introduction_publish_requires_environment_guard(self):
        original = MODULE.THREADS_PUBLISH_ENABLED
        MODULE.THREADS_PUBLISH_ENABLED = False
        try:
            with self.assertRaisesRegex(RuntimeError, "THREADS_PUBLISH_ENABLED"):
                MODULE.handle_introduction(
                    {"mode": "introduction", "publish": True},
                    datetime(2026, 9, 18, 8, tzinfo=ZoneInfo("America/New_York")),
                )
        finally:
            MODULE.THREADS_PUBLISH_ENABLED = original

    def test_introduction_claim_does_not_require_s3_list_or_read(self):
        original = MODULE.THREADS_PUBLISH_ENABLED
        MODULE.THREADS_PUBLISH_ENABLED = True
        credentials = {
            "THREADS_USER_ID": "user-1",
            "THREADS_ACCESS_TOKEN": "token",
            "THREADS_USERNAME": "bostonweekendagent",
        }
        try:
            with patch.object(MODULE, "load_json") as load_json, patch.object(
                MODULE, "write_publication_state"
            ) as write_state, patch.object(
                MODULE, "get_threads_credentials", return_value=credentials
            ), patch.object(
                MODULE, "refresh_threads_token_if_needed", return_value=credentials
            ), patch.object(
                MODULE, "auto_publish_threads_text", return_value="post-1"
            ):
                result = MODULE.handle_introduction(
                    {"mode": "introduction", "publish": True},
                    datetime(2026, 9, 18, 8, tzinfo=ZoneInfo("America/New_York")),
                )
        finally:
            MODULE.THREADS_PUBLISH_ENABLED = original

        load_json.assert_not_called()
        self.assertTrue(write_state.call_args_list[0].kwargs["claim"])
        self.assertEqual(result["threads"]["status"], "published")

    def test_introduction_uses_meta_text_auto_publish(self):
        credentials = {"THREADS_ACCESS_TOKEN": "secret-token"}
        with patch.object(
            MODULE, "threads_request_json", return_value={"id": "post-1"}
        ) as request:
            post_id = MODULE.auto_publish_threads_text("Hello", credentials)

        self.assertEqual(post_id, "post-1")
        self.assertEqual(request.call_args.args[0], f"{MODULE.THREADS_API_BASE}/me/threads")
        self.assertEqual(request.call_args.kwargs["data"]["auto_publish_text"], "true")
        self.assertEqual(request.call_args.kwargs["data"]["media_type"], "TEXT")

    def test_campaign_archive_key_is_immutable_for_retries(self):
        original_s3 = MODULE.S3
        MODULE.S3 = MagicMock()
        now = datetime(
            2026,
            9,
            17,
            7,
            0,
            0,
            123456,
            tzinfo=ZoneInfo("America/New_York"),
        )
        try:
            keys = MODULE.store_campaign(
                {
                    "zh": {"title": "Boston 今日活動", "body": "今天的活動。"},
                    "en": {"title": "Boston today", "body": "Today's events."},
                    "hashtags": ["Boston"],
                },
                [{"event_id": "event-1"}],
                {},
                now,
            )
        finally:
            MODULE.S3 = original_s3

        self.assertEqual(
            keys["archive"],
            "social/campaigns/2026/09/2026-09-17_070000_123456.json",
        )

    def test_splits_long_threads_copy_within_platform_limit(self):
        text = "中文活動" * 140 + "\n\n" + "English event " * 60
        chunks = MODULE.split_threads_text(text)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(0 < len(chunk) <= 500 for chunk in chunks))
        self.assertIn("中文活動", chunks[0])
        self.assertIn("English event", chunks[-1])

    def test_publishes_followup_chunks_as_replies(self):
        credentials = {
            "THREADS_USER_ID": "user-1",
            "THREADS_ACCESS_TOKEN": "secret-token",
        }
        with patch.object(
            MODULE,
            "split_threads_text",
            return_value=["first", "second"],
        ), patch.object(
            MODULE,
            "create_threads_container",
            side_effect=["container-1", "container-2"],
        ) as create, patch.object(
            MODULE,
            "publish_threads_container",
            side_effect=["post-1", "post-2"],
        ):
            post_ids = MODULE.publish_threads_text("copy", credentials)

        self.assertEqual(post_ids, ["post-1", "post-2"])
        self.assertEqual(create.call_args_list[0].args[-1], None)
        self.assertEqual(create.call_args_list[1].args[-1], "post-1")

    def test_publication_key_is_one_per_local_day(self):
        now = datetime(2026, 9, 17, 7, tzinfo=ZoneInfo("America/New_York"))
        self.assertEqual(
            MODULE.publication_key(now),
            "social/publications/threads/2026-09-17.json",
        )


if __name__ == "__main__":
    unittest.main()
