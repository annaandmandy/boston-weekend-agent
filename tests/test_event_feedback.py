import importlib.util
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock


MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "services"
    / "event-feedback"
    / "feedback.py"
)
SPEC = importlib.util.spec_from_file_location("event_feedback", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class EventFeedbackTests(unittest.TestCase):
    def test_normalize_event_ids_deduplicates(self):
        self.assertEqual(
            MODULE.normalize_event_ids(["event_12345678", "event_12345678"]),
            ["event_12345678"],
        )

    def test_normalize_event_ids_rejects_invalid_values(self):
        with self.assertRaises(ValueError):
            MODULE.normalize_event_ids(["../unsafe"])

    def test_query_feedback_returns_counts_and_current_vote(self):
        original = MODULE.DYNAMODB
        hashed = MODULE.visitor_hash("visitor_1234567890")
        mock = MagicMock()
        mock.batch_get_item.return_value = {
            "Responses": {
                MODULE.TABLE_NAME: [
                    {
                        "pk": {"S": "EVENT#event_12345678"},
                        "sk": {"S": "SUMMARY"},
                        "likes": {"N": "7"},
                    },
                    {
                        "pk": {"S": "EVENT#event_12345678"},
                        "sk": {"S": f"VOTER#{hashed}"},
                    },
                ]
            }
        }
        MODULE.DYNAMODB = mock
        try:
            result = MODULE.query_feedback(["event_12345678"], hashed)
        finally:
            MODULE.DYNAMODB = original
        self.assertEqual(
            result["feedback"]["event_12345678"], {"likes": 7, "liked": True}
        )

    def test_handler_rejects_invalid_action(self):
        result = MODULE.lambda_handler(
            {
                "routeKey": "POST /feedback/toggle",
                "body": json.dumps(
                    {
                        "event_id": "event_12345678",
                        "visitor_id": "visitor_1234567890",
                        "action": "maybe",
                    }
                ),
            },
            None,
        )
        self.assertEqual(result["statusCode"], 400)


if __name__ == "__main__":
    unittest.main()
