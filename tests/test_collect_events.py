import importlib.util
import io
import json
import pathlib
import sys
import unittest
from datetime import date, datetime, timezone
from unittest.mock import MagicMock


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
    def test_writes_immutable_event_and_change_snapshots(self):
        original_s3 = MODULE.S3
        mock_s3 = MagicMock()
        MODULE.S3 = mock_s3
        try:
            keys = MODULE.write_snapshot(
                {"timestamp": "2026-09-18T06:00:00-04:00", "events": []},
                {"new": [], "updated": [], "missing": []},
            )
        finally:
            MODULE.S3 = original_s3

        self.assertEqual(keys["changes"], "events/changes/latest.json")
        self.assertRegex(
            keys["changes_timestamped"],
            r"^events/changes/\d{4}-\d{2}/changes_\d{8}_\d{6}_\d{6}\.json$",
        )
        self.assertRegex(
            keys["timestamped"],
            r"^events/\d{4}-\d{2}/events_\d{8}_\d{6}_\d{6}\.json$",
        )
        written_keys = [
            call.kwargs["Key"] for call in mock_s3.put_object.call_args_list
        ]
        self.assertIn("events/latest.json", written_keys)
        self.assertIn("events/changes/latest.json", written_keys)
        self.assertEqual(len(written_keys), 4)

    def test_date_window(self):
        self.assertTrue(
            MODULE.is_in_collection_window("2026-09-18", today=date(2026, 9, 17))
        )
        self.assertFalse(
            MODULE.is_in_collection_window("2026-09-29", today=date(2026, 9, 17))
        )

    def test_change_set_tracks_new_updated_and_missing_without_cancelling(self):
        previous = {
            "timestamp": "2026-09-17T06:00:00-04:00",
            "events": [
                {
                    "event_id": "same",
                    "name": "Harbor Music",
                    "date": "2026-09-19",
                    "time": "18:00:00",
                    "source": "Boston.gov",
                    "link": "https://example.org/music",
                },
                {
                    "event_id": "gone",
                    "name": "Old Listing",
                    "date": "2026-09-20",
                    "source": "Boston.gov",
                },
            ],
        }
        current = {
            "timestamp": "2026-09-18T06:00:00-04:00",
            "events": [
                {
                    "event_id": "same",
                    "name": "Harbor Music",
                    "date": "2026-09-19",
                    "time": "19:00:00",
                    "source": "Boston.gov",
                    "link": "https://example.org/music",
                    "content_hash": "changed",
                },
                {
                    "event_id": "new",
                    "name": "New Festival",
                    "date": "2026-09-20",
                    "source": "Revere Community",
                },
            ],
        }
        changes = MODULE.build_change_set(previous, current)
        self.assertEqual(changes["counts"], {"new": 1, "updated": 1, "missing": 1})
        self.assertEqual(changes["updated"][0]["changes"]["time"]["after"], "19:00:00")
        self.assertEqual(changes["missing"][0]["status"], "unconfirmed_missing")

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

    def test_parse_meet_boston_rss(self):
        rss = """<?xml version="1.0"?>
        <rss version="2.0"><channel><item>
          <title>Harbor Arts Festival</title>
          <link>https://www.meetboston.com/event/harbor-arts-festival/12345/</link>
          <category>Festivals</category>
          <category>Free</category>
          <pubDate>Sat, 19 Sep 2026 00:00:00 -0400</pubDate>
          <description><![CDATA[
            <img src="https://assets.example.org/harbor.jpg" />
            <p>A free annual waterfront celebration with art and music.</p>
          ]]></description>
        </item></channel></rss>"""
        events = MODULE.parse_meet_boston_rss(
            rss, today=date(2026, 9, 17)
        )
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event["name"], "Harbor Arts Festival")
        self.assertEqual(event["date"], "2026-09-19")
        self.assertEqual(event["category"], "Festivals, Free")
        self.assertEqual(event["price"], "Free")
        self.assertEqual(event["source"], "Meet Boston")
        self.assertEqual(
            event["image_url"], "https://assets.example.org/harbor.jpg"
        )

    def test_parse_meet_boston_detail_json_ld(self):
        html = """
        <html><head><script type="application/ld+json">
        {
          "@context": "https://schema.org",
          "@type": "Event",
          "name": "Harbor Arts Festival",
          "startDate": "2026-09-19T18:30:00-04:00",
          "description": "An annual waterfront celebration.",
          "image": ["https://assets.example.org/detail.jpg"],
          "isAccessibleForFree": true,
          "location": {
            "@type": "Place",
            "name": "Harbor Park",
            "address": {
              "streetAddress": "1 Harbor Way",
              "addressLocality": "Boston",
              "addressRegion": "MA"
            },
            "geo": {
              "latitude": 42.36,
              "longitude": -71.05
            }
          }
        }
        </script></head></html>
        """
        detail = MODULE.parse_meet_boston_detail(html)
        self.assertEqual(detail["time"], "18:30:00")
        self.assertEqual(detail["location"], "Harbor Park")
        self.assertEqual(detail["address"], "1 Harbor Way, Boston, MA")
        self.assertEqual(detail["city"], "Boston")
        self.assertEqual(detail["price"], "Free")
        self.assertEqual(detail["latitude"], 42.36)

    def test_meet_boston_query_is_bounded_to_collection_window(self):
        params = MODULE.build_meet_boston_rss_params(today=date(2026, 9, 17))
        event_filter = json.loads(params["filter"])
        date_filter = event_filter["dates"]["$elemMatch"]["eventDate"]
        self.assertEqual(
            date_filter["$gte"]["$date"], "2026-09-17T04:00:00.000Z"
        )
        self.assertEqual(
            date_filter["$lte"]["$date"], "2026-09-28T03:59:59.999Z"
        )

    def test_loads_fresh_meet_boston_staging_snapshot(self):
        original_s3 = MODULE.S3
        mock_s3 = MagicMock()
        mock_s3.get_object.return_value = {
            "Body": io.BytesIO(
                json.dumps(
                    {
                        "schema_version": 1,
                        "source": "Meet Boston",
                        "fetched_at": "2026-09-17T09:30:00+00:00",
                        "events": [
                            {
                                "name": "Harbor Arts Festival",
                                "date": "2026-09-19",
                                "source": "Meet Boston",
                                "link": "https://www.meetboston.com/event/harbor/1/",
                            }
                        ],
                    }
                ).encode("utf-8")
            )
        }
        MODULE.S3 = mock_s3
        try:
            events = MODULE.load_staged_meet_boston_events(
                now=datetime(2026, 9, 17, 10, tzinfo=timezone.utc)
            )
        finally:
            MODULE.S3 = original_s3

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["name"], "Harbor Arts Festival")
        mock_s3.get_object.assert_called_once_with(
            Bucket="boston-weekend-agent-reports",
            Key="ingestion/meet-boston/latest.json",
        )

    def test_rejects_stale_meet_boston_staging_snapshot(self):
        original_s3 = MODULE.S3
        mock_s3 = MagicMock()
        mock_s3.get_object.return_value = {
            "Body": io.BytesIO(
                json.dumps(
                    {
                        "schema_version": 1,
                        "source": "Meet Boston",
                        "fetched_at": "2026-09-15T00:00:00+00:00",
                        "events": [
                            {
                                "name": "Old Event",
                                "date": "2026-09-19",
                                "link": "https://www.meetboston.com/event/old/1/",
                            }
                        ],
                    }
                ).encode("utf-8")
            )
        }
        MODULE.S3 = mock_s3
        try:
            events = MODULE.load_staged_meet_boston_events(
                now=datetime(2026, 9, 17, 10, tzinfo=timezone.utc)
            )
        finally:
            MODULE.S3 = original_s3

        self.assertIsNone(events)

    def test_meet_boston_prefers_staging_before_direct_fetch(self):
        original_staging = MODULE.load_staged_meet_boston_events
        original_direct = MODULE.fetch_meet_boston_direct_events
        staged = [{"name": "Staged Festival"}]
        MODULE.load_staged_meet_boston_events = MagicMock(return_value=staged)
        MODULE.fetch_meet_boston_direct_events = MagicMock()
        try:
            events = MODULE.fetch_meet_boston_events()
        finally:
            direct_mock = MODULE.fetch_meet_boston_direct_events
            MODULE.load_staged_meet_boston_events = original_staging
            MODULE.fetch_meet_boston_direct_events = original_direct

        self.assertEqual(events, staged)
        direct_mock.assert_not_called()

    def test_ticketmaster_geohash_has_expected_precision(self):
        geohash = MODULE.encode_geohash(42.3601, -71.0589)
        self.assertEqual(geohash, "drt2zp2")

    def test_ticketmaster_zero_range_is_unknown_not_free(self):
        self.assertIsNone(MODULE.normalize_ticket_price(0, 0))
        self.assertEqual(MODULE.normalize_ticket_price(22, 27), "$22-$27")

    def test_detects_sold_out_ticket_page(self):
        html = """
        <main><h1>Chxrry</h1>
        <p>SOLD OUT — EVERY TICKET HAS BEEN SOLD</p></main>
        """
        self.assertEqual(MODULE.detect_ticket_page_status(html), "sold_out")

    def test_ticket_page_without_warning_has_no_override(self):
        html = "<main><h1>Community Concert</h1><p>Tickets available</p></main>"
        self.assertIsNone(MODULE.detect_ticket_page_status(html))

    def test_generic_sold_out_question_does_not_mark_event_unavailable(self):
        html = "<footer><p>What should I do if another event is sold out?</p></footer>"
        self.assertIsNone(MODULE.detect_ticket_page_status(html))

    def test_recommendation_prefers_bu_area(self):
        today = date(2026, 9, 17)
        near = {
            "name": "Neighborhood activity",
            "date": "2026-09-18",
            "time": "18:00:00",
            "location": "Kenmore Square",
            "city": "Boston",
            "link": "https://example.org/near",
            "source": "Boston.gov",
        }
        far = {**near, "location": "Town Common", "city": "Natick"}
        near_score = MODULE.calculate_recommendation(near, today=today)
        far_score = MODULE.calculate_recommendation(far, today=today)
        self.assertGreater(near_score["score"], far_score["score"])
        self.assertEqual(near_score["components"]["proximity"], 30)

    def test_distinctive_outer_event_can_compete_on_other_components(self):
        today = date(2026, 9, 17)
        nearby_generic = {
            "name": "Community activity",
            "date": "2026-09-18",
            "time": "18:00:00",
            "location": "Boston Common",
            "city": "Boston",
            "link": "https://example.org/near",
            "source": "Boston.gov",
        }
        outer_festival = {
            **nearby_generic,
            "name": "Annual seasonal arts festival",
            "location": "Natick Common",
            "city": "Natick",
            "price": "Free",
            "description": "A distinctive annual festival with local artists and performances.",
        }
        nearby_score = MODULE.calculate_recommendation(nearby_generic, today=today)
        outer_score = MODULE.calculate_recommendation(outer_festival, today=today)
        self.assertGreaterEqual(outer_score["score"], nearby_score["score"])

    def test_unavailable_event_keeps_components_but_scores_zero(self):
        event = {
            "name": "Sold out show",
            "date": "2026-09-18",
            "location": "Kenmore Square",
            "city": "Boston",
            "availability_status": "sold_out",
        }
        result = MODULE.calculate_recommendation(
            event, today=date(2026, 9, 17)
        )
        self.assertFalse(result["eligible"])
        self.assertEqual(result["score"], 0)

    def test_revere_sand_sculpting_is_destination_worthy(self):
        event = {
            "name": "Revere International Sand Sculpting Festival",
            "date": "2026-09-19",
            "city": "Revere",
            "source": "Revere Community",
        }
        result = MODULE.calculate_recommendation(
            event, today=date(2026, 9, 17)
        )
        self.assertTrue(result["destination_worthy"])
        self.assertIn("sand sculpture festival", result["destination_reasons"])

    def test_regattabar_does_not_trigger_regatta_boost(self):
        event = {
            "name": "George Coleman Quintet",
            "description": "Contact regattabar@example.com for group tickets.",
            "category": "Music",
            "location": "Regattabar",
        }
        worthy, reasons = MODULE.destination_worthiness(event)
        self.assertFalse(worthy)
        self.assertNotIn("regatta", reasons)

    def test_parse_ical_events_unfolds_and_filters_meetings(self):
        calendar = """BEGIN:VCALENDAR\r
BEGIN:VEVENT\r
DESCRIPTION:Shop from local artists at this free event. https://example.org/\r
 open-studios\r
DTSTART:20260919T160000Z\r
LOCATION:Central Square\r
SUMMARY:Open Studios\r
URL:/calendar/open-studios\r
END:VEVENT\r
BEGIN:VEVENT\r
DTSTART;TZID=America/New_York:20260919T190000\r
LOCATION:City Hall\r
SUMMARY:Planning Board Meeting\r
URL:/calendar/meeting\r
END:VEVENT\r
END:VCALENDAR\r
"""
        events = MODULE.parse_ical_events(
            calendar,
            source="Cambridge Arts",
            city="Cambridge",
            base_url="https://example.org",
            today=date(2026, 9, 17),
        )
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event["name"], "Open Studios")
        self.assertEqual(event["date"], "2026-09-19")
        self.assertEqual(event["time"], "12:00:00")
        self.assertEqual(event["city"], "Cambridge")
        self.assertEqual(event["link"], "https://example.org/open-studios")
        self.assertEqual(event["price"], "Free")

    def test_parse_revere_official_calendar(self):
        html = """
        <div class="CalendarFeed-event">
          <div class="u-fontSizeH5">
            <a href="/calendar/event/123">Revere Beach Sand Festival</a>
          </div>
          <div class="Arrange-sizeFill">September 19, 2026</div>
          <div class="Arrange-sizeFill">11:00 AM to 3:00 PM</div>
          <div class="Arrange-sizeFill">Revere Beach</div>
        </div>
        """
        events = MODULE.parse_revere_events(html, today=date(2026, 9, 17))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["date"], "2026-09-19")
        self.assertEqual(events[0]["city"], "Revere")
        self.assertEqual(
            events[0]["link"], "https://www.revere.org/calendar/event/123"
        )

    def test_parse_discover_quincy_calendar(self):
        html = """
        <article class="mec-event-article">
          <div class="mec-topsec">
            <h3 class="mec-event-title">
              <a href="https://discoverquincy.com/events/festival/">Fall Festival</a>
            </h3>
            <div class="mec-event-description">A free afternoon of music and food.</div>
            <span class="mec-start-date-label">19 Sep</span>
            <span class="mec-start-time">3:00 pm</span>
            <span class="mec-end-time">7:00 pm</span>
            <div class="mec-venue-details"><span>Kilroy Square</span>
              <address class="mec-event-address">25 Cottage Avenue</address>
            </div>
            <div class="mec-event-image"><img src="https://example.org/event.jpg"></div>
          </div>
        </article>
        """
        events = MODULE.parse_quincy_events(html, today=date(2026, 9, 17))
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event["date"], "2026-09-19")
        self.assertEqual(event["time"], "3:00 pm - 7:00 pm")
        self.assertEqual(event["location"], "Kilroy Square")
        self.assertEqual(event["price"], "Free")

    def test_repairs_implausible_quincy_am_pm_range(self):
        self.assertEqual(
            MODULE.normalize_time_range(["3:00 am", "7:00 pm"]),
            ["3:00 pm", "7:00 pm"],
        )
        self.assertEqual(
            MODULE.normalize_time_range(["8:00 am", "5:00 pm"]),
            ["8:00 am", "5:00 pm"],
        )

    def test_deduplicates_same_event_from_multiple_calendars(self):
        common = {
            "name": "Community Arts Festival",
            "date": "2026-09-19",
            "time": "12:00:00",
            "category": "Community",
            "price": None,
            "image_url": None,
        }
        ranked = MODULE.deduplicate_and_rank(
            [
                {
                    **common,
                    "location": "Greater Boston",
                    "address": None,
                    "description": None,
                    "source": "Calendar A",
                    "link": "https://example.org/a",
                },
                {
                    **common,
                    "location": "Town Common",
                    "address": "1 Main Street",
                    "description": (
                        "A free community festival with live music, local food, "
                        "family activities, and art."
                    ),
                    "price": "Free",
                    "source": "Calendar B",
                    "link": "https://example.org/b",
                },
            ]
        )
        self.assertEqual(len(ranked), 1)
        self.assertEqual(ranked[0]["source"], "Calendar B")

    def test_global_ranking_excludes_government_meetings(self):
        ranked = MODULE.deduplicate_and_rank(
            [
                {
                    "name": "Neighborhood Abutters Meeting",
                    "date": "2026-09-19",
                    "time": "18:00:00",
                    "location": "City Hall",
                    "description": "Public discussion about a local project.",
                    "price": "Free",
                    "image_url": None,
                    "source": "Official Calendar",
                    "link": "https://example.org/meeting",
                },
                {
                    "name": "Waterfront Music Festival",
                    "date": "2026-09-19",
                    "time": "12:00:00",
                    "location": "The Park",
                    "description": "Free music and food for the whole family by the water.",
                    "price": "Free",
                    "image_url": None,
                    "source": "Official Calendar",
                    "link": "https://example.org/festival",
                },
            ]
        )
        self.assertEqual([event["name"] for event in ranked], ["Waterfront Music Festival"])


if __name__ == "__main__":
    unittest.main()
