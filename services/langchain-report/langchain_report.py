"""Generate and store the Boston weekend report from S3 snapshots."""

from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime, timedelta
from functools import lru_cache
from typing import Any
from zoneinfo import ZoneInfo

import boto3


LOGGER = logging.getLogger(__name__)
LOGGER.setLevel(os.environ.get("LOG_LEVEL", "INFO").upper())

AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
BUCKET_NAME = os.environ.get("REPORT_BUCKET", "boston-weekend-agent-reports")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o")
EASTERN = ZoneInfo("America/New_York")

S3 = boto3.client("s3", region_name=AWS_REGION)
SECRETS = boto3.client("secretsmanager", region_name=AWS_REGION)


@lru_cache(maxsize=1)
def get_openai_api_key() -> str:
    secret_id = os.environ.get("OPENAI_SECRET_ID")
    if secret_id:
        response = SECRETS.get_secret_value(SecretId=secret_id)
        values = json.loads(response["SecretString"])
        api_key = values.get("OPENAI_API_KEY", "")
    else:
        # Local development only; production should configure OPENAI_SECRET_ID.
        api_key = os.environ.get("OPENAI_API_KEY", "")

    api_key = api_key.strip()
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is missing")
    return api_key


def load_json(key: str) -> dict[str, Any]:
    response = S3.get_object(Bucket=BUCKET_NAME, Key=key)
    return json.loads(response["Body"].read().decode("utf-8"))


def load_optional_json(key: str) -> dict[str, Any]:
    try:
        return load_json(key)
    except Exception as error:
        if getattr(error, "response", {}).get("Error", {}).get("Code") in {
            "NoSuchKey",
            "404",
        }:
            return {}
        raise


def determine_edition(now: datetime, override: str | None = None) -> str:
    if override:
        return override
    if now.weekday() == 3:
        return "thursday-preview"
    if now.weekday() == 4:
        return "friday-update"
    return "weekend-update"


def weekend_start(now: datetime) -> date:
    days_until_friday = (4 - now.weekday()) % 7
    return now.date() + timedelta(days=days_until_friday)


def weekend_baseline_key(now: datetime) -> str:
    return f"reports/baselines/weekend_{weekend_start(now).isoformat()}.json"


def _fallback_event_id(event: dict[str, Any]) -> str:
    return "|".join(
        str(event.get(field) or "").lower().strip()
        for field in ("source", "link", "name", "city")
    )


def compare_event_snapshots(
    baseline: dict[str, Any], current: dict[str, Any]
) -> dict[str, Any]:
    before = {
        event.get("event_id") or _fallback_event_id(event): event
        for event in baseline.get("events", [])
    }
    after = {
        event.get("event_id") or _fallback_event_id(event): event
        for event in current.get("events", [])
    }
    tracked_fields = ("date", "time", "location", "address", "price", "description", "link")
    new_events = [after[event_id] for event_id in after.keys() - before.keys()]
    updated = []
    for event_id in after.keys() & before.keys():
        changes = {
            field: {"before": before[event_id].get(field), "after": after[event_id].get(field)}
            for field in tracked_fields
            if before[event_id].get(field) != after[event_id].get(field)
        }
        if changes:
            updated.append(
                {
                    "event_id": event_id,
                    "name": after[event_id].get("name"),
                    "date": after[event_id].get("date"),
                    "changes": changes,
                }
            )
    missing = [
        {
            "event_id": event_id,
            "name": before[event_id].get("name"),
            "date": before[event_id].get("date"),
            "source": before[event_id].get("source"),
            "status": "unconfirmed_missing",
        }
        for event_id in before.keys() - after.keys()
    ]
    return {"new": new_events, "updated": updated, "missing": missing}


def store_weekend_baseline(events_data: dict[str, Any], now: datetime) -> str:
    key = weekend_baseline_key(now)
    S3.put_object(
        Bucket=BUCKET_NAME,
        Key=key,
        Body=json.dumps(events_data, ensure_ascii=False, indent=2).encode("utf-8"),
        ContentType="application/json",
    )
    return key


def upcoming_holidays(today: date, days_ahead: int = 7) -> list[str]:
    fixed = {
        (1, 1): "New Year's Day",
        (2, 14): "Valentine's Day",
        (3, 17): "St. Patrick's Day",
        (7, 4): "Independence Day",
        (10, 31): "Halloween",
        (12, 24): "Christmas Eve",
        (12, 25): "Christmas Day",
        (12, 31): "New Year's Eve",
    }
    results = []
    for offset in range(days_ahead + 1):
        candidate = today + timedelta(days=offset)
        name = fixed.get((candidate.month, candidate.day))
        if name:
            results.append(f"{name} ({'today' if offset == 0 else f'in {offset} days'})")

    first_november = date(today.year, 11, 1)
    first_thursday = first_november + timedelta(
        days=(3 - first_november.weekday()) % 7
    )
    thanksgiving = first_thursday + timedelta(weeks=3)
    offset = (thanksgiving - today).days
    if 0 <= offset <= days_ahead:
        results.append(
            f"Thanksgiving ({'today' if offset == 0 else f'in {offset} days'})"
        )
    return results


def build_time_context(now: datetime) -> dict[str, Any]:
    if now.weekday() >= 4:
        weekend_status = "It is the weekend."
    else:
        days = 4 - now.weekday()
        weekend_status = f"The weekend starts in {days} day{'s' if days != 1 else ''}."

    if 5 <= now.hour < 12:
        time_of_day = "morning"
    elif 12 <= now.hour < 17:
        time_of_day = "afternoon"
    elif 17 <= now.hour < 21:
        time_of_day = "evening"
    else:
        time_of_day = "night"

    return {
        "day_name": now.strftime("%A"),
        "date": now.strftime("%B %d, %Y"),
        "time": now.strftime("%I:%M %p"),
        "time_of_day": time_of_day,
        "weekend_status": weekend_status,
        "holidays": upcoming_holidays(now.date()),
    }


def filter_and_prioritize_events(
    events_data: dict[str, Any], now: datetime
) -> list[dict[str, Any]]:
    today = now.date()
    if today.weekday() >= 4:
        start = today
        end = today + timedelta(days=6 - today.weekday())
    else:
        start = today + timedelta(days=4 - today.weekday())
        end = start + timedelta(days=2)
    selected = []
    for original in events_data.get("events", []):
        raw_date = original.get("date")
        try:
            event_date = datetime.strptime(raw_date, "%Y-%m-%d").date()
        except (TypeError, ValueError):
            continue
        if not start <= event_date <= end:
            continue

        event = dict(original)
        offset = (event_date - today).days
        event["time_label"] = (
            "TODAY"
            if offset == 0
            else "TOMORROW"
            if offset == 1
            else event_date.strftime("%A")
        )
        score = float(event.get("quality_score", 5))
        if "free" in str(event.get("price") or "").lower():
            score += 2
        event_text = " ".join(
            str(event.get(field) or "") for field in ("name", "description", "category")
        ).lower()
        if any(term in event_text for term in ("festival", "fitness", "tour", "music", "dance")):
            score += 2
        if any(term in event_text for term in ("abutters meeting", "office hours", "public meeting")):
            score -= 3
        event["priority_score"] = score
        selected.append(event)
    return sorted(selected, key=lambda item: item["priority_score"], reverse=True)


def format_events(events: list[dict[str, Any]], limit: int = 8) -> str:
    if not events:
        return "No structured events were available; focus on weather-safe local activities."
    lines = []
    for index, event in enumerate(events[:limit], start=1):
        lines.extend(
            [
                f"{index}. [{event.get('time_label', '')}] {event.get('name', 'Unknown')}",
                f"   Location: {event.get('location', 'Boston')}",
                f"   Price: {event.get('price') or 'See event page'}",
                f"   Time: {event.get('time') or 'See event page'}",
                f"   Source: {event.get('source') or 'Unknown'}",
            ]
        )
    return "\n".join(lines)


def format_event_changes(changes: dict[str, Any], limit: int = 6) -> str:
    lines = []
    for event in changes.get("new", [])[:limit]:
        lines.append(
            f"NEW: {event.get('name')} on {event.get('date')} "
            f"({event.get('source', 'unknown source')})"
        )
    for event in changes.get("updated", [])[:limit]:
        fields = ", ".join(event.get("changes", {}).keys()) or "details"
        lines.append(
            f"UPDATED: {event.get('name')} on {event.get('date')} changed {fields}"
        )
    missing = changes.get("missing", [])[:limit]
    if missing:
        lines.append(
            "UNCONFIRMED MISSING (do not call these cancelled): "
            + ", ".join(event.get("name", "Unknown") for event in missing)
        )
    return "\n".join(lines) or "No material event-listing changes were detected."


def build_prompt():
    from langchain_core.prompts import ChatPromptTemplate

    return ChatPromptTemplate.from_messages(
        [
            (
                "system",
                """You are the Boston Weekend Mood Agent, a careful local curator.

Combine the supplied event and weather data with broadly known Boston seasonal
activities. Clearly distinguish listed events from general local suggestions.
Do not invent event dates, prices, locations, or opening hours.

Current context:
- Day: {day_name}
- Date: {date}
- Time: {time} ({time_of_day})
- Weekend status: {weekend_status}
- Upcoming holidays: {holiday_context}
- Edition: {edition}

For a Thursday preview, present this as an early planning edition and say that
weather and event details will be checked again Friday morning. For a Friday
update, naturally call out material new or changed listings and refreshed
weather. Never describe an event as cancelled merely because it is listed as
unconfirmed missing.

Write 300-400 words with this structure:

Weekend headline
Weather Snapshot
What's Happening
One free option
Insider Tip

Use a friendly neighborly tone. Mention source uncertainty when event details are incomplete.
Return Markdown only. Do not wrap the report in JSON, quotes, or code fences.""",
            ),
            (
                "human",
                """Generate this week's Boston weekend report.

Weather:
- Best outdoor day: {best_day}
- Best time: {best_time}
- Temperature: {temperature}
- Rain chance: {rain_chance}

Upcoming events:
{events}

Changes since the previous collection:
{event_changes}
""",
            ),
        ]
    )


def normalize_report_text(content: Any) -> str:
    report = str(content).strip()
    if report.startswith('"') and report.endswith('"'):
        report = report[1:-1].strip()
    if report.endswith('"}'):
        report = report[:-2].rstrip()
    return report


def generate_report(
    events_data: dict[str, Any],
    weather_data: dict[str, Any],
    changes_data: dict[str, Any] | None = None,
    *,
    edition_override: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    from langchain_openai import ChatOpenAI

    now = now or datetime.now(EASTERN)
    context = build_time_context(now)
    edition = determine_edition(now, edition_override)
    events = filter_and_prioritize_events(events_data, now)
    recommendations = (weather_data.get("summary") or {}).get("recommendations") or {}
    top_pick = recommendations.get("top_pick") or {}

    model = ChatOpenAI(
        model=OPENAI_MODEL,
        temperature=0.4,
        api_key=get_openai_api_key(),
    )
    result = (build_prompt() | model).invoke(
        {
            **context,
            "edition": edition,
            "holiday_context": ", ".join(context["holidays"]) or "None",
            "best_day": top_pick.get("date", "Unknown"),
            "best_time": top_pick.get("time_window", "Unknown"),
            "temperature": top_pick.get("temperature", "Unknown"),
            "rain_chance": top_pick.get("rain_chance", "Unknown"),
            "events": format_events(events),
            "event_changes": format_event_changes(changes_data or {}),
        }
    )
    report = normalize_report_text(result.content)
    return {
        "report": report,
        "generated_at": now.isoformat(),
        "events_count": len(events),
        "edition": edition,
        "context": context,
    }


def store_report(result: dict[str, Any]) -> dict[str, str]:
    now = datetime.now(EASTERN)
    body = result["report"].encode("utf-8")
    timestamped_key = f"reports/{now:%Y-%m}/report_{now:%Y%m%d_%H%M%S}.txt"
    archive_key = (
        f"reports/archive/{now:%Y/%m}/"
        f"{now:%Y-%m-%d}_{result['edition']}.txt"
    )
    latest_key = "reports/weekend_summary.txt"
    for key in (timestamped_key, archive_key, latest_key):
        S3.put_object(
            Bucket=BUCKET_NAME,
            Key=key,
            Body=body,
            ContentType="text/plain; charset=utf-8",
        )
    return {
        "latest": latest_key,
        "timestamped": timestamped_key,
        "archive": archive_key,
    }


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    now = datetime.now(EASTERN)
    events_data = load_json("events/latest.json")
    weather_data = load_json("weather/summary.json")
    changes_data = load_optional_json("events/changes/latest.json")
    edition_override = event.get("edition") if isinstance(event, dict) else None
    edition = determine_edition(now, edition_override)
    baseline_key = weekend_baseline_key(now)
    if edition == "thursday-preview":
        store_weekend_baseline(events_data, now)
    elif edition == "friday-update":
        baseline = load_optional_json(baseline_key)
        if baseline:
            changes_data = compare_event_snapshots(baseline, events_data)
    result = generate_report(
        events_data,
        weather_data,
        changes_data,
        edition_override=edition_override,
        now=now,
    )
    keys = store_report(result)
    response = {
        "success": True,
        "generated_at": result["generated_at"],
        "events_count": result["events_count"],
        "edition": result["edition"],
        "baseline_key": baseline_key,
        "bucket": BUCKET_NAME,
        "s3_keys": keys,
    }
    LOGGER.info("Report generation complete: %s", json.dumps(response))
    return response
