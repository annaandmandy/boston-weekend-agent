import base64
import hashlib
import json
import os
import re
import uuid
from datetime import datetime, timezone
from typing import Any

import boto3
from botocore.exceptions import ClientError


TABLE_NAME = os.environ.get("FEEDBACK_TABLE", "boston-weekend-event-feedback")
REPORT_BUCKET = os.environ.get("REPORT_BUCKET", "boston-weekend-agent-reports")
REPORT_KEY = os.environ.get("REPORT_KEY", "reports/weekend_summary.json")
MAX_QUERY_EVENTS = 50
EVENT_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{8,128}$")
VISITOR_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{16,128}$")

DYNAMODB = boto3.client("dynamodb")
S3 = boto3.client("s3")


def response(status_code: int, body: dict[str, Any]) -> dict[str, Any]:
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body, ensure_ascii=False),
    }


def parse_body(event: dict[str, Any]) -> dict[str, Any]:
    raw = event.get("body") or "{}"
    if event.get("isBase64Encoded"):
        raw = base64.b64decode(raw).decode("utf-8")
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise ValueError("Request body must be a JSON object")
    return parsed


def route_key(event: dict[str, Any]) -> str:
    if event.get("routeKey"):
        return event["routeKey"]
    method = event.get("requestContext", {}).get("http", {}).get("method", "")
    path = event.get("rawPath", "")
    return f"{method} {path}".strip()


def normalize_event_ids(values: Any) -> list[str]:
    if not isinstance(values, list):
        raise ValueError("event_ids must be an array")
    unique = []
    for value in values:
        event_id = str(value or "").strip()
        if not EVENT_ID_PATTERN.fullmatch(event_id):
            raise ValueError("event_ids contains an invalid value")
        if event_id not in unique:
            unique.append(event_id)
    if not unique or len(unique) > MAX_QUERY_EVENTS:
        raise ValueError(f"event_ids must contain 1-{MAX_QUERY_EVENTS} values")
    return unique


def normalize_visitor_id(value: Any) -> str:
    visitor_id = str(value or "").strip()
    if not VISITOR_ID_PATTERN.fullmatch(visitor_id):
        raise ValueError("visitor_id is invalid")
    return visitor_id


def visitor_hash(visitor_id: str) -> str:
    return hashlib.sha256(visitor_id.encode("utf-8")).hexdigest()


def event_pk(event_id: str) -> str:
    return f"EVENT#{event_id}"


def summary_key(event_id: str) -> dict[str, dict[str, str]]:
    return {"pk": {"S": event_pk(event_id)}, "sk": {"S": "SUMMARY"}}


def voter_key(event_id: str, hashed_visitor: str) -> dict[str, dict[str, str]]:
    return {
        "pk": {"S": event_pk(event_id)},
        "sk": {"S": f"VOTER#{hashed_visitor}"},
    }


def load_current_activity(event_id: str) -> tuple[dict[str, Any], str]:
    payload = json.loads(
        S3.get_object(Bucket=REPORT_BUCKET, Key=REPORT_KEY)["Body"]
        .read()
        .decode("utf-8")
    )
    for activity in payload.get("activities", []):
        if activity.get("event_id") == event_id:
            return activity, str(payload.get("generated_at") or "")
    raise LookupError("Activity is not in the current report")


def query_feedback(event_ids: list[str], hashed_visitor: str) -> dict[str, Any]:
    keys = []
    for event_id in event_ids:
        keys.extend([summary_key(event_id), voter_key(event_id, hashed_visitor)])
    result = DYNAMODB.batch_get_item(
        RequestItems={TABLE_NAME: {"Keys": keys, "ConsistentRead": True}}
    )
    items = result.get("Responses", {}).get(TABLE_NAME, [])
    counts = {event_id: 0 for event_id in event_ids}
    liked = {event_id: False for event_id in event_ids}
    for item in items:
        pk = item.get("pk", {}).get("S", "")
        event_id = pk.removeprefix("EVENT#")
        sk = item.get("sk", {}).get("S", "")
        if event_id not in counts:
            continue
        if sk == "SUMMARY":
            counts[event_id] = max(0, int(item.get("likes", {}).get("N", "0")))
        elif sk.startswith("VOTER#"):
            liked[event_id] = True
    return {
        "feedback": {
            event_id: {"likes": counts[event_id], "liked": liked[event_id]}
            for event_id in event_ids
        }
    }


def string_attribute(value: Any, limit: int = 500) -> dict[str, str]:
    return {"S": str(value or "")[:limit]}


def toggle_feedback(
    event_id: str,
    hashed_visitor: str,
    action: str,
    request_id: str,
) -> dict[str, Any]:
    activity, report_generated_at = load_current_activity(event_id)
    now = datetime.now(timezone.utc).isoformat()
    action_key = {
        "pk": {"S": event_pk(event_id)},
        "sk": {"S": f"ACTION#{now}#{request_id}"},
    }
    action_item = {
        **action_key,
        "entity_type": {"S": "feedback_action"},
        "action": {"S": action},
        "visitor_hash": {"S": hashed_visitor},
        "created_at": {"S": now},
        "report_generated_at": string_attribute(report_generated_at, 100),
        "title": string_attribute(activity.get("title")),
        "city": string_attribute(activity.get("city"), 120),
        "category": string_attribute(activity.get("category"), 120),
        "source": string_attribute(activity.get("source"), 120),
        "price_type": string_attribute(activity.get("price_type"), 40),
    }
    summary_names = {
        "#likes": "likes",
        "#updated_at": "updated_at",
        "#title": "title",
        "#city": "city",
        "#category": "category",
        "#source": "source",
    }
    summary_values = {
        ":zero": {"N": "0"},
        ":one": {"N": "1"},
        ":now": {"S": now},
        ":title": string_attribute(activity.get("title")),
        ":city": string_attribute(activity.get("city"), 120),
        ":category": string_attribute(activity.get("category"), 120),
        ":source": string_attribute(activity.get("source"), 120),
    }

    if action == "like":
        voter_operation = {
            "Put": {
                "TableName": TABLE_NAME,
                "Item": {
                    **voter_key(event_id, hashed_visitor),
                    "entity_type": {"S": "voter_state"},
                    "created_at": {"S": now},
                },
                "ConditionExpression": "attribute_not_exists(pk)",
            }
        }
        count_expression = (
            "SET #likes = if_not_exists(#likes, :zero) + :one, "
            "#updated_at = :now, #title = :title, #city = :city, "
            "#category = :category, #source = :source"
        )
    else:
        voter_operation = {
            "Delete": {
                "TableName": TABLE_NAME,
                "Key": voter_key(event_id, hashed_visitor),
                "ConditionExpression": "attribute_exists(pk)",
            }
        }
        count_expression = (
            "SET #likes = #likes - :one, #updated_at = :now, "
            "#title = :title, #city = :city, #category = :category, "
            "#source = :source"
        )

    try:
        DYNAMODB.transact_write_items(
            ClientRequestToken=request_id,
            TransactItems=[
                voter_operation,
                {
                    "Update": {
                        "TableName": TABLE_NAME,
                        "Key": summary_key(event_id),
                        "UpdateExpression": count_expression,
                        "ExpressionAttributeNames": summary_names,
                        "ExpressionAttributeValues": summary_values,
                        **(
                            {"ConditionExpression": "#likes >= :one"}
                            if action == "unlike"
                            else {}
                        ),
                    }
                },
                {
                    "Put": {
                        "TableName": TABLE_NAME,
                        "Item": action_item,
                        "ConditionExpression": "attribute_not_exists(pk)",
                    }
                },
            ],
        )
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") != "TransactionCanceledException":
            raise
        cancellation_codes = {
            reason.get("Code")
            for reason in error.response.get("CancellationReasons", [])
            if reason.get("Code") not in {None, "None"}
        }
        if not cancellation_codes.issubset({"ConditionalCheckFailed"}):
            raise

    return query_feedback([event_id], hashed_visitor)


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    try:
        body = parse_body(event)
        hashed_visitor = visitor_hash(normalize_visitor_id(body.get("visitor_id")))
        route = route_key(event)
        if route == "POST /feedback/query":
            return response(
                200, query_feedback(normalize_event_ids(body.get("event_ids")), hashed_visitor)
            )
        if route == "POST /feedback/toggle":
            event_id = normalize_event_ids([body.get("event_id")])[0]
            action = str(body.get("action") or "")
            if action not in {"like", "unlike"}:
                raise ValueError("action must be like or unlike")
            request_id = str(body.get("request_id") or uuid.uuid4())
            if not re.fullmatch(r"[A-Za-z0-9-]{16,36}", request_id):
                raise ValueError("request_id is invalid")
            return response(
                200,
                toggle_feedback(event_id, hashed_visitor, action, request_id),
            )
        return response(404, {"error": "Route not found"})
    except (ValueError, json.JSONDecodeError) as error:
        return response(400, {"error": str(error)})
    except LookupError as error:
        return response(404, {"error": str(error)})
    except Exception as error:
        print(f"Feedback request failed: {type(error).__name__}: {error}")
        return response(500, {"error": "Unable to update feedback right now"})
