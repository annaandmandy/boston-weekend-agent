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

    def test_unlike_transaction_does_not_send_unused_zero_value(self):
        original_dynamodb = MODULE.DYNAMODB
        original_loader = MODULE.load_current_activity
        mock = MagicMock()
        mock.batch_get_item.return_value = {
            "Responses": {MODULE.TABLE_NAME: []}
        }
        MODULE.DYNAMODB = mock
        MODULE.load_current_activity = lambda event_id: (
            {
                "event_id": event_id,
                "title": "Test Event",
                "city": "Boston",
                "category": "Community",
                "source": "Test",
                "price_type": "free",
            },
            "2026-09-18T07:00:00-04:00",
        )
        try:
            MODULE.toggle_feedback(
                "event_12345678",
                "a" * 64,
                "unlike",
                "77777777-7777-4777-8777-777777777777",
            )
        finally:
            MODULE.DYNAMODB = original_dynamodb
            MODULE.load_current_activity = original_loader

        update = mock.transact_write_items.call_args.kwargs["TransactItems"][1][
            "Update"
        ]
        self.assertNotIn(":zero", update["ExpressionAttributeValues"])


if __name__ == "__main__":
    unittest.main()
