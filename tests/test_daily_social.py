import importlib.util
import pathlib
import sys
import unittest
from datetime import datetime, timedelta
from unittest.mock import MagicMock
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
            "title": "Boston 今日活动",
            "body": "今天可以去公园听音乐。",
            "hashtags": ["Boston", "波士顿生活"],
        }
        rendered = MODULE.render_shared_text(content)
        self.assertIn("Boston 今日活动", rendered)
        self.assertIn("#Boston #波士顿生活", rendered)

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
                    "title": "Boston 今日活动",
                    "body": "今天的活动。",
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


if __name__ == "__main__":
    unittest.main()
