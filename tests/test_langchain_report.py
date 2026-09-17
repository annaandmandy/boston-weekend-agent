import importlib.util
import pathlib
import sys
import unittest
from datetime import datetime
from zoneinfo import ZoneInfo


MODULE_PATH = (
    pathlib.Path(__file__).parents[1]
    / "services"
    / "langchain-report"
    / "langchain_report.py"
)
SPEC = importlib.util.spec_from_file_location("langchain_report", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class LangChainReportTests(unittest.TestCase):
    def test_prioritizes_free_event_today(self):
        now = datetime(2026, 9, 18, 9, tzinfo=ZoneInfo("America/New_York"))
        data = {
            "events": [
                {
                    "name": "Paid Tomorrow",
                    "date": "2026-09-19",
                    "price": "$20",
                    "quality_score": 8,
                },
                {
                    "name": "Free Today",
                    "date": "2026-09-18",
                    "price": "Free",
                    "quality_score": 7,
                },
            ]
        }
        events = MODULE.filter_and_prioritize_events(data, now)
        self.assertEqual(events[0]["name"], "Free Today")
        self.assertEqual(events[0]["time_label"], "TODAY")

    def test_thursday_selects_friday_through_sunday(self):
        now = datetime(2026, 9, 17, 17, tzinfo=ZoneInfo("America/New_York"))
        data = {
            "events": [
                {"name": "Thursday Meeting", "date": "2026-09-17"},
                {"name": "Friday Music", "date": "2026-09-18"},
                {"name": "Sunday Tour", "date": "2026-09-20"},
                {"name": "Monday Event", "date": "2026-09-21"},
            ]
        }
        events = MODULE.filter_and_prioritize_events(data, now)
        self.assertEqual(
            {event["name"] for event in events}, {"Friday Music", "Sunday Tour"}
        )

    def test_normalizes_accidental_json_suffix(self):
        self.assertEqual(
            MODULE.normalize_report_text("Enjoy your weekend!\"}"),
            "Enjoy your weekend!",
        )


if __name__ == "__main__":
    unittest.main()
