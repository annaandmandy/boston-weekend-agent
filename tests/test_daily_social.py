import importlib.util
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

    def test_renders_one_identical_shared_post(self):
        content = {
            "zh": {
                "title": "Boston 今日活动",
                "body": "今天可以去公园听音乐。",
            },
            "en": {
                "title": "What's on in Boston today",
                "body": "Listen to live music in the park today.",
            },
            "hashtags": ["Boston", "波士顿生活"],
        }
        rendered = MODULE.render_shared_text(content)
        self.assertIn("Boston 今日活动", rendered)
        self.assertLess(
            rendered.index("Boston 今日活动"),
            rendered.index("What's on in Boston today"),
        )
        self.assertIn("—— English ——", rendered)
        self.assertIn("#Boston #波士顿生活", rendered)

    def test_parses_structured_bilingual_content(self):
        content = MODULE.parse_model_json(
            """{
                "zh": {"title": "今日活动", "body": "中文内容"},
                "en": {"title": "Today's events", "body": "English copy"},
                "hashtags": ["#Boston", "周末去哪"]
            }"""
        )
        self.assertEqual(content["zh"]["body"], "中文内容")
        self.assertEqual(content["en"]["body"], "English copy")
        self.assertEqual(content["hashtags"], ["Boston", "周末去哪"])

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
                    "zh": {"title": "Boston 今日活动", "body": "今天的活动。"},
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
        text = "中文活动" * 140 + "\n\n" + "English event " * 60
        chunks = MODULE.split_threads_text(text)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(0 < len(chunk) <= 500 for chunk in chunks))
        self.assertIn("中文活动", chunks[0])
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
