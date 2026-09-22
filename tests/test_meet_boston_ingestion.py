import importlib.util
import pathlib
import sys
import unittest
from datetime import datetime, timezone


MODULE_PATH = (
    pathlib.Path(__file__).parents[1] / "scripts" / "ingest_meet_boston.py"
)
SPEC = importlib.util.spec_from_file_location("ingest_meet_boston", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class MeetBostonIngestionTests(unittest.TestCase):
    def test_build_payload_has_versioned_source_and_utc_timestamp(self):
        fetched_at = datetime(2026, 9, 17, 5, 30, tzinfo=timezone.utc)
        events = [{"name": "Harbor Arts Festival"}]

        payload = MODULE.build_payload(events, fetched_at)

        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(payload["source"], "Meet Boston")
        self.assertEqual(payload["fetched_at"], "2026-09-17T05:30:00+00:00")
        self.assertEqual(payload["event_count"], 1)
        self.assertEqual(payload["events"], events)


if __name__ == "__main__":
    unittest.main()
