import importlib.util
import pathlib
import sys
import unittest
from datetime import datetime
from unittest.mock import MagicMock
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
    def test_weekend_prompt_is_bilingual_bobo_letter(self):
        prompt_text = str(MODULE.build_prompt())
        self.assertIn("波波 Bo", prompt_text)
        self.assertIn("natural Taiwan Traditional", prompt_text)
        self.assertIn("700-1000 Traditional Chinese characters", prompt_text)
        self.assertIn("450-650 English words", prompt_text)
        self.assertIn("weekend letter", prompt_text)
        self.assertIn("repetitive numbered list", prompt_text)
        self.assertIn("Write them as raw URLs", prompt_text)
        self.assertIn("Do not invent or", prompt_text)
        self.assertIn("temperatures only in °C", prompt_text)
        self.assertIn("temperatures only in °F", prompt_text)
        self.assertIn('"zh"', prompt_text)

    def test_daily_and_weekend_persona_files_match(self):
        daily_persona_path = (
            pathlib.Path(__file__).parents[1]
            / "services"
            / "daily-social"
            / "persona.json"
        )
        self.assertEqual(
            __import__("json").loads(daily_persona_path.read_text()),
            MODULE.load_persona(),
        )

    def test_daily_and_weekend_default_memory_files_match(self):
        daily_memory_path = (
            pathlib.Path(__file__).parents[1]
            / "services"
            / "daily-social"
            / "memory.default.json"
        )
        self.assertEqual(
            __import__("json").loads(daily_memory_path.read_text()),
            MODULE.load_default_memory(),
        )

    def test_ranking_prompt_uses_bobo_identity_and_memory(self):
        prompt = MODULE.build_ranking_prompt()
        prompt_text = str(prompt)
        self.assertIn("Bo's versioned identity", prompt_text)
        self.assertIn("reviewed long-term memory", prompt_text)
        self.assertIn("never a quota or veto", prompt_text)
        rendered = prompt.format(
            persona_json="{}", memory_json="{}", events_json="[]"
        )
        self.assertIn('{"rankings"', rendered)

    def test_report_finalizer_removes_emoji_and_adds_bobo_identity(self):
        rendered = MODULE.finalize_report_text(
            "**週末來信** ☀️\n\n先去散步。",
            "⌖ˎˊ˗ 〔✦ᴗ✦〕ノ",
        )
        self.assertNotIn("☀️", rendered)
        self.assertTrue(rendered.startswith("⌖ˎˊ˗ 〔✦ᴗ✦〕ノ"))
        self.assertTrue(rendered.endswith("— 波波 ⌖ˎˊ˗ 〔•ᴗ•〕ゞ"))

    def test_parses_independent_language_reports(self):
        parsed = MODULE.parse_bilingual_report(
            __import__("json").dumps(
                {
                    "zh": {"title": "週末來信", "body": "氣溫 20°C。"},
                    "en": {"title": "Weekend Letter", "body": "It is 68°F."},
                }
            )
        )
        self.assertEqual(parsed["zh"]["title"], "週末來信")
        self.assertEqual(parsed["en"]["body"], "It is 68°F.")

    def test_converts_celsius_weather_text_to_fahrenheit(self):
        self.assertEqual(
            MODULE.fahrenheit_temperature_text("16.4°C (13.7-20.8°C)"),
            "61.5°F (56.7-69.4°F)",
        )

    def test_store_report_writes_localized_json(self):
        original_s3 = MODULE.S3
        mock_s3 = MagicMock()
        MODULE.S3 = mock_s3
        now = datetime(2026, 9, 18, 7, tzinfo=ZoneInfo("America/New_York"))
        result = {
            "report": "legacy combined report",
            "generated_at": now.isoformat(),
            "edition": "friday-update",
            "languages": {
                "zh": {
                    "locale": "zh-TW",
                    "temperature_unit": "C",
                    "markdown": "氣溫 20°C",
                },
                "en": {
                    "locale": "en-US",
                    "temperature_unit": "F",
                    "markdown": "Temperature 68°F",
                },
            },
        }
        try:
            keys = MODULE.store_report(result, now)
        finally:
            MODULE.S3 = original_s3

        self.assertEqual(keys["latest_json"], "reports/weekend_summary.json")
        latest_json_call = next(
            call
            for call in mock_s3.put_object.call_args_list
            if call.kwargs["Key"] == "reports/weekend_summary.json"
        )
        payload = __import__("json").loads(latest_json_call.kwargs["Body"])
        self.assertEqual(payload["languages"]["zh"]["temperature_unit"], "C")
        self.assertEqual(payload["languages"]["en"]["temperature_unit"], "F")

    def test_luna_uses_reasoning_effort_without_temperature(self):
        original_model = MODULE.OPENAI_MODEL
        original_effort = MODULE.OPENAI_REASONING_EFFORT
        MODULE.OPENAI_MODEL = "gpt-5.6-luna"
        MODULE.OPENAI_REASONING_EFFORT = "none"
        try:
            options = MODULE.chat_model_options("test-key", 0.4)
        finally:
            MODULE.OPENAI_MODEL = original_model
            MODULE.OPENAI_REASONING_EFFORT = original_effort

        self.assertEqual(options["reasoning_effort"], "none")
        self.assertNotIn("temperature", options)

    def test_legacy_model_keeps_temperature(self):
        original_model = MODULE.OPENAI_MODEL
        MODULE.OPENAI_MODEL = "gpt-4o"
        try:
            options = MODULE.chat_model_options("test-key", 0.4)
        finally:
            MODULE.OPENAI_MODEL = original_model

        self.assertEqual(options["temperature"], 0.4)
        self.assertNotIn("reasoning_effort", options)

    def test_analytics_prefix_is_partitioned_and_unique(self):
        now = datetime(
            2026,
            9,
            18,
            7,
            15,
            12,
            345678,
            tzinfo=ZoneInfo("America/New_York"),
        )
        prefix = MODULE.analytics_run_prefix(now)
        self.assertEqual(
            prefix,
            "analytics/report_runs/year=2026/month=09/day=18/"
            "run_id=20260918T071512345678-0400",
        )

    def test_archive_input_uses_the_exact_s3_version(self):
        original_s3 = MODULE.S3
        mock_s3 = MagicMock()
        mock_s3.head_object.return_value = {
            "VersionId": "source-version",
            "ETag": '"source-etag"',
        }
        mock_s3.copy_object.return_value = {"VersionId": "archive-version"}
        MODULE.S3 = mock_s3
        try:
            lineage = MODULE.archive_input_object(
                "events/latest.json",
                "analytics/run/events.json",
            )
        finally:
            MODULE.S3 = original_s3

        mock_s3.copy_object.assert_called_once_with(
            Bucket=MODULE.BUCKET_NAME,
            Key="analytics/run/events.json",
            CopySource={
                "Bucket": MODULE.BUCKET_NAME,
                "Key": "events/latest.json",
                "VersionId": "source-version",
            },
            MetadataDirective="COPY",
        )
        self.assertEqual(lineage["source_etag"], "source-etag")
        self.assertEqual(lineage["archive_version_id"], "archive-version")

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

    def test_weekend_report_excludes_unavailable_event(self):
        now = datetime(2026, 9, 17, 17, tzinfo=ZoneInfo("America/New_York"))
        data = {
            "events": [
                {
                    "name": "Sold out concert",
                    "date": "2026-09-18",
                    "availability_status": "sold_out",
                },
                {
                    "name": "Open concert",
                    "date": "2026-09-18",
                    "availability_status": "onsale",
                },
            ]
        }
        events = MODULE.filter_and_prioritize_events(data, now)
        self.assertEqual([event["name"] for event in events], ["Open concert"])

    def test_ai_can_rank_destination_event_above_nearby_routine_event(self):
        candidates = [
                {
                    "event_id": "nearby",
                    "name": "Nearby activity",
                    "recommendation_score": 90,
                },
                {
                    "event_id": "revere",
                    "name": "Revere Sand Sculpting Festival",
                    "recommendation_score": 80,
                },
        ]
        response = __import__("json").dumps(
            {
                "rankings": [
                    {
                        "event_id": "revere",
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
                        "significance_signals": ["annual"],
                        "reason_zh": "值得專程前往。",
                        "reason_en": "Worth the trip.",
                    },
                    {
                        "event_id": "nearby",
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
                        "reason_zh": "方便但日常。",
                        "reason_en": "Convenient but routine.",
                    },
                ]
            }
        )
        events = MODULE.parse_ai_rankings(response, candidates)
        self.assertEqual(events[0]["event_id"], "revere")
        self.assertEqual(events[0]["ai_ranking"]["dimensions"]["rarity"], 19)

    def test_normalizes_accidental_json_suffix(self):
        self.assertEqual(
            MODULE.normalize_report_text("Enjoy your weekend!\"}"),
            "Enjoy your weekend!",
        )

    def test_report_edition_follows_thursday_and_friday(self):
        eastern = ZoneInfo("America/New_York")
        self.assertEqual(
            MODULE.determine_edition(datetime(2026, 9, 17, 7, tzinfo=eastern)),
            "thursday-preview",
        )
        self.assertEqual(
            MODULE.determine_edition(datetime(2026, 9, 18, 7, tzinfo=eastern)),
            "friday-update",
        )

    def test_missing_listing_is_explicitly_not_a_cancellation(self):
        formatted = MODULE.format_event_changes(
            {
                "missing": [
                    {"name": "Harbor Festival", "status": "unconfirmed_missing"}
                ]
            }
        )
        self.assertIn("do not call these cancelled", formatted)

    def test_friday_compares_against_thursday_weekend_baseline(self):
        baseline = {
            "events": [
                {
                    "event_id": "music",
                    "name": "Harbor Music",
                    "date": "2026-09-19",
                    "time": "18:00:00",
                },
                {"event_id": "missing", "name": "Old Event", "date": "2026-09-20"},
            ]
        }
        current = {
            "events": [
                {
                    "event_id": "music",
                    "name": "Harbor Music",
                    "date": "2026-09-19",
                    "time": "19:00:00",
                },
                {"event_id": "new", "name": "New Event", "date": "2026-09-20"},
            ]
        }
        changes = MODULE.compare_event_snapshots(baseline, current)
        self.assertEqual(changes["updated"][0]["changes"]["time"]["after"], "19:00:00")
        self.assertEqual(changes["new"][0]["name"], "New Event")
        self.assertEqual(changes["missing"][0]["status"], "unconfirmed_missing")

    def test_analytics_manifest_records_inputs_model_and_outputs(self):
        original_s3 = MODULE.S3
        mock_s3 = MagicMock()
        MODULE.S3 = mock_s3
        try:
            keys = MODULE.store_analytics_manifest(
                prefix=(
                    "analytics/report_runs/year=2026/month=09/day=18/"
                    "run_id=test-run"
                ),
                result={
                    "report": "Weekend report",
                    "generated_at": "2026-09-18T07:15:00-04:00",
                    "edition": "friday-update",
                    "model": "gpt-4o",
                    "prompt_version": "v2",
                    "token_usage": {"total_tokens": 123},
                    "events_count": 8,
                },
                archived_inputs={"events": {"archive_key": "events.json"}},
                report_keys={"latest": "reports/weekend_summary.txt"},
                effective_changes_key="effective_event_changes.json",
                baseline_lineage={"archive_key": "thursday_baseline.json"},
                lambda_request_id="request-123",
            )
        finally:
            MODULE.S3 = original_s3

        self.assertEqual(keys["manifest"].rsplit("/", 1)[-1], "report.json")
        self.assertEqual(keys["report_text"].rsplit("/", 1)[-1], "report.txt")
        manifest_call = mock_s3.put_object.call_args_list[-1].kwargs
        manifest = __import__("json").loads(manifest_call["Body"].decode("utf-8"))
        self.assertEqual(manifest["model"], "gpt-4o")
        self.assertEqual(manifest["token_usage"]["total_tokens"], 123)
        self.assertEqual(manifest["lambda_request_id"], "request-123")


if __name__ == "__main__":
    unittest.main()
