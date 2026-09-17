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

    def test_parse_boston_gov_rss(self):
        rss = """<?xml version="1.0"?>
        <rss version="2.0"><channel><item>
          <title>Boston Test Event</title>
          <link>https://www.boston.gov/calendar/test</link>
          <description><![CDATA[
            <div class="date-recur-date">
              <time datetime="2026-09-18T18:00:00Z">Fri, 09/18/2026 - 6:00pm</time>
            </div>
            <p class="address"><span class="address-line1">Boston Common</span>
              <span class="locality">Boston</span>, <span>MA</span></p>
            <p>Join this free community event.</p>
          ]]></description>
        </item></channel></rss>"""
        events = MODULE.parse_boston_gov_rss(rss, today=date(2026, 9, 17))
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event["name"], "Boston Test Event")
        self.assertEqual(event["date"], "2026-09-18")
        self.assertEqual(event["time"], "6:00pm")
        self.assertEqual(event["location"], "Boston Common")
        self.assertEqual(event["price"], "Free")


if __name__ == "__main__":
    unittest.main()
