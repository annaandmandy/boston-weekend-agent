"""Generate one shared daily post for Threads and Xiaohongshu."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from datetime import datetime, timedelta
from functools import lru_cache
from typing import Any
from zoneinfo import ZoneInfo

import boto3


LOGGER = logging.getLogger(__name__)
LOGGER.setLevel(os.environ.get("LOG_LEVEL", "INFO").upper())

AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
BUCKET_NAME = os.environ.get("REPORT_BUCKET", "boston-weekend-agent-reports")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-5.6-luna")
OPENAI_REASONING_EFFORT = os.environ.get("OPENAI_REASONING_EFFORT", "none")
WEBSITE_URL = os.environ.get(
    "WEBSITE_URL", "https://www.hsiangyuhuang.com/weekend_report"
)
COOLDOWN_HOURS = int(os.environ.get("SOCIAL_COOLDOWN_HOURS", "48"))
MAX_SOCIAL_EVENTS = int(os.environ.get("MAX_SOCIAL_EVENTS", "5"))
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
        api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key.strip():
        raise RuntimeError("OPENAI_API_KEY is missing")
    return api_key.strip()


def chat_model_options(api_key: str, temperature: float) -> dict[str, Any]:
    """Use reasoning controls for current models and sampling for legacy ones."""
    options: dict[str, Any] = {
        "model": OPENAI_MODEL,
        "api_key": api_key,
    }
    if OPENAI_MODEL.startswith(("gpt-5", "gpt-6", "o1", "o3", "o4")):
        options["reasoning_effort"] = OPENAI_REASONING_EFFORT
    else:
        options["temperature"] = temperature
    return options


def load_json(key: str, default: dict[str, Any] | None = None) -> dict[str, Any]:
    try:
        response = S3.get_object(Bucket=BUCKET_NAME, Key=key)
    except Exception as error:
        if getattr(error, "response", {}).get("Error", {}).get("Code") in {
            "NoSuchKey",
            "404",
        }:
            return default or {}
        raise
    return json.loads(response["Body"].read().decode("utf-8"))


def _fallback_event_id(event: dict[str, Any]) -> str:
    identity = "|".join(
        str(event.get(field) or "").lower().strip()
        for field in ("source", "link", "name", "city")
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=EASTERN)
    return parsed.astimezone(EASTERN)


def select_social_events(
    events_data: dict[str, Any],
    history: dict[str, Any],
    now: datetime,
) -> list[dict[str, Any]]:
    """Pick today through two days ahead, enforcing a 48-hour cooldown."""
    cutoff = now - timedelta(hours=COOLDOWN_HOURS)
    last_selected = history.get("last_selected", {})
    candidates = []
    for original in events_data.get("events", []):
        try:
            event_date = datetime.strptime(original.get("date"), "%Y-%m-%d").date()
        except (TypeError, ValueError):
            continue
        offset = (event_date - now.date()).days
        if not 0 <= offset <= 2:
            continue

        event = dict(original)
        event_id = event.get("event_id") or _fallback_event_id(event)
        previous_time = _parse_datetime(last_selected.get(event_id))
        if previous_time and previous_time > cutoff:
            continue

        score = float(event.get("quality_score", 5))
        score += {0: 4, 1: 2, 2: 1}.get(offset, 0)
        if "free" in str(event.get("price") or "").lower():
            score += 1
        event["event_id"] = event_id
        event["day_offset"] = offset
        event["social_score"] = score
        candidates.append(event)

    candidates.sort(key=lambda item: item["social_score"], reverse=True)
    return candidates[:MAX_SOCIAL_EVENTS]


def format_events(events: list[dict[str, Any]]) -> str:
    labels = {0: "今天", 1: "明天", 2: "后天"}
    lines = []
    for index, event in enumerate(events, start=1):
        lines.append(
            " | ".join(
                (
                    f"{index}. {labels.get(event['day_offset'], event.get('date'))}",
                    str(event.get("name") or "Unknown"),
                    str(event.get("time") or "时间见活动页面"),
                    str(event.get("location") or event.get("city") or "Greater Boston"),
                    str(event.get("price") or "价格见活动页面"),
                    str(event.get("link") or ""),
                )
            )
        )
    return "\n".join(lines)


def build_prompt():
    from langchain_core.prompts import ChatPromptTemplate

    return ChatPromptTemplate.from_messages(
        [
            (
                "system",
                """You write one shared Chinese-language social post that will be used
unchanged on both Threads and Xiaohongshu. Use only facts supplied below. Never
invent dates, times, prices, venues, availability, or cancellation status.

Keep the body concise, friendly, and useful to people living around Greater
Boston. Mention 3-5 activities when available, preserve their source links, and
end with the weekend-report URL. Use plain text and raw URLs; do not use Markdown
link syntax because the same copy is pasted directly into both platforms. Do
not claim that an event is recommended from personal experience. Return strict
JSON with exactly these keys:
title (string), body (string), hashtags (array of strings).""",
            ),
            (
                "human",
                """Today is {date} ({day_name}) in Boston.

Verified event candidates:
{events}

Weekend report: {website_url}
""",
            ),
        ]
    )


def parse_model_json(content: Any) -> dict[str, Any]:
    text = str(content).strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text)
    result = json.loads(text)
    if not isinstance(result.get("title"), str) or not isinstance(
        result.get("body"), str
    ):
        raise ValueError("Model response did not contain title and body strings")
    hashtags = result.get("hashtags")
    if not isinstance(hashtags, list):
        raise ValueError("Model response did not contain a hashtags array")
    result["hashtags"] = [str(tag).lstrip("#") for tag in hashtags]
    return result


def generate_content(events: list[dict[str, Any]], now: datetime) -> dict[str, Any]:
    from langchain_openai import ChatOpenAI

    model = ChatOpenAI(**chat_model_options(get_openai_api_key(), 0.3))
    response = (build_prompt() | model).invoke(
        {
            "date": now.strftime("%Y-%m-%d"),
            "day_name": now.strftime("%A"),
            "events": format_events(events),
            "website_url": WEBSITE_URL,
        }
    )
    return parse_model_json(response.content)


def render_shared_text(content: dict[str, Any]) -> str:
    hashtags = " ".join(f"#{tag}" for tag in content["hashtags"])
    return f"{content['title']}\n\n{content['body']}\n\n{hashtags}".strip()


def store_campaign(
    content: dict[str, Any],
    events: list[dict[str, Any]],
    history: dict[str, Any],
    now: datetime,
) -> dict[str, str]:
    campaign_id = now.strftime("%Y-%m-%d")
    event_ids = [event["event_id"] for event in events]
    campaign = {
        "campaign_id": campaign_id,
        "generated_at": now.isoformat(),
        "content": content,
        "shared_text": render_shared_text(content),
        "selected_event_ids": event_ids,
        "platforms": {
            "threads": {"status": "ready"},
            "xiaohongshu": {"status": "manual_draft"},
        },
    }
    last_selected = history.setdefault("last_selected", {})
    for event_id in event_ids:
        last_selected[event_id] = now.isoformat()
    history["updated_at"] = now.isoformat()

    campaign_body = json.dumps(campaign, ensure_ascii=False, indent=2).encode("utf-8")
    history_body = json.dumps(history, ensure_ascii=False, indent=2).encode("utf-8")
    keys = {
        "latest": "social/latest.json",
        "archive": (
            f"social/campaigns/{now:%Y/%m}/"
            f"{campaign_id}_{now:%H%M%S_%f}.json"
        ),
        "history": "social/history.json",
    }
    for key in (keys["latest"], keys["archive"]):
        S3.put_object(
            Bucket=BUCKET_NAME,
            Key=key,
            Body=campaign_body,
            ContentType="application/json; charset=utf-8",
        )
    S3.put_object(
        Bucket=BUCKET_NAME,
        Key=keys["history"],
        Body=history_body,
        ContentType="application/json; charset=utf-8",
    )
    S3.put_object(
        Bucket=BUCKET_NAME,
        Key="social/latest.txt",
        Body=campaign["shared_text"].encode("utf-8"),
        ContentType="text/plain; charset=utf-8",
    )
    return keys


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    now = datetime.now(EASTERN)
    events_data = load_json("events/latest.json")
    history = load_json("social/history.json", default={})
    selected = select_social_events(events_data, history, now)
    if not selected:
        raise RuntimeError("No eligible events remain after the 48-hour cooldown")
    content = generate_content(selected, now)
    keys = store_campaign(content, selected, history, now)
    result = {
        "success": True,
        "generated_at": now.isoformat(),
        "selected_event_ids": [event["event_id"] for event in selected],
        "platform_content": "shared",
        "s3_keys": keys,
    }
    LOGGER.info("Daily social campaign generated: %s", json.dumps(result))
    return result
