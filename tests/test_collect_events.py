import importlib.util
import pathlib
import sys
import unittest
from datetime import date


MODULE_PATH = (
    pathlib.Path(__file__).parents[1]
    / "services"
    / "collect-events"
    / "collect_events.py"
)
SPEC = importlib.util.spec_from_file_location("collect_events", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class CollectEventsTests(unittest.TestCase):
    def test_date_window(self):
        self.assertTrue(
            MODULE.is_in_collection_window("2026-09-18", today=date(2026, 9, 17))
        )
        self.assertFalse(
            MODULE.is_in_collection_window("2026-09-25", today=date(2026, 9, 17))
        )

    def test_parse_boston_calendar_detail(self):
        html = """
        <div id="content-event-left">
          <div id="event_info">
            <h1 itemprop="name">Boston Test Event</h1>
            <span itemprop="startDate" content="2026-09-18T18:00:00"></span>
            <span id="starting_time">6:00 PM</span>
            <p itemprop="location">
              <span itemprop="name">Boston Common</span>
              <span itemprop="address">139 Tremont St, Boston</span>
            </p>
            <p><strong>Admission:</strong> Free</p>
          </div>
        </div>
        """
        event = MODULE.parse_boston_calendar_detail(
            html, "https://www.thebostoncalendar.com/events/test"
        )
        self.assertEqual(event["name"], "Boston Test Event")
        self.assertEqual(event["date"], "2026-09-18")
        self.assertEqual(event["location"], "Boston Common")
        self.assertEqual(event["price"], "Free")


if __name__ == "__main__":
    unittest.main()
