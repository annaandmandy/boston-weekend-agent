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
    events_data: dict[str, Any], now: datetime, days_ahead: int = 3
) -> list[dict[str, Any]]:
    start = now.date()
    end = start + timedelta(days=days_ahead)
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
        offset = (event_date - start).days
        event["time_label"] = (
            "TODAY" if offset == 0 else "TOMORROW" if offset == 1 else f"In {offset} days"
        )
        score = float(event.get("quality_score", 5))
        if "free" in str(event.get("price") or "").lower():
            score += 2
        score += 3 if offset == 0 else 2 if offset == 1 else 0
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

Write 300-400 words with this structure:

Weekend headline
Weather Snapshot
What's Happening
One free option
Insider Tip

Use a friendly neighborly tone. Mention source uncertainty when event details are incomplete.""",
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
""",
            ),
        ]
    )


def generate_report(events_data: dict[str, Any], weather_data: dict[str, Any]) -> dict[str, Any]:
    from langchain_openai import ChatOpenAI

    now = datetime.now(EASTERN)
    context = build_time_context(now)
    events = filter_and_prioritize_events(events_data, now)
    recommendations = (weather_data.get("summary") or {}).get("recommendations") or {}
    top_pick = recommendations.get("top_pick") or {}

    model = ChatOpenAI(
        model=OPENAI_MODEL,
        temperature=0.7,
        api_key=get_openai_api_key(),
    )
    result = (build_prompt() | model).invoke(
        {
            **context,
            "holiday_context": ", ".join(context["holidays"]) or "None",
            "best_day": top_pick.get("date", "Unknown"),
            "best_time": top_pick.get("time_window", "Unknown"),
            "temperature": top_pick.get("temperature", "Unknown"),
            "rain_chance": top_pick.get("rain_chance", "Unknown"),
            "events": format_events(events),
        }
    )
    return {
        "report": result.content,
        "generated_at": now.isoformat(),
        "events_count": len(events),
        "context": context,
    }


def store_report(result: dict[str, Any]) -> dict[str, str]:
    now = datetime.now(EASTERN)
    body = result["report"].encode("utf-8")
    timestamped_key = f"reports/{now:%Y-%m}/report_{now:%Y%m%d_%H%M%S}.txt"
    latest_key = "reports/weekend_summary.txt"
    for key in (timestamped_key, latest_key):
        S3.put_object(
            Bucket=BUCKET_NAME,
            Key=key,
            Body=body,
            ContentType="text/plain; charset=utf-8",
        )
    return {"latest": latest_key, "timestamped": timestamped_key}


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    events_data = load_json("events/latest.json")
    weather_data = load_json("weather/summary.json")
    result = generate_report(events_data, weather_data)
    keys = store_report(result)
    response = {
        "success": True,
        "generated_at": result["generated_at"],
        "events_count": result["events_count"],
        "bucket": BUCKET_NAME,
        "s3_keys": keys,
    }
    LOGGER.info("Report generation complete: %s", json.dumps(response))
    return response
