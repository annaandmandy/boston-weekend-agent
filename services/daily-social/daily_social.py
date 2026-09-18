"""Generate one shared bilingual daily post for Threads and Xiaohongshu."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import boto3
from botocore.exceptions import ClientError


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
THREADS_SECRET_ID = os.environ.get("THREADS_SECRET_ID", "")
THREADS_PUBLISH_ENABLED = os.environ.get(
    "THREADS_PUBLISH_ENABLED", "false"
).lower() in {"1", "true", "yes"}
THREADS_API_BASE = "https://graph.threads.net/v1.0"
THREADS_MAX_POST_LENGTH = 500
THREADS_TOKEN_REFRESH_DAYS = int(
    os.environ.get("THREADS_TOKEN_REFRESH_DAYS", "7")
)
UNAVAILABLE_EVENT_STATUSES = {
    "canceled",
    "cancelled",
    "offsale",
    "postponed",
    "rescheduled",
    "sold_out",
}
EASTERN = ZoneInfo("America/New_York")

S3 = boto3.client("s3", region_name=AWS_REGION)
SECRETS = boto3.client("secretsmanager", region_name=AWS_REGION)

EMOJI_PATTERN = re.compile(
    "[\\u2600-\\u27BF\\U0001F000-\\U0001FAFF\\uFE0F]"
)


@lru_cache(maxsize=1)
def load_persona() -> dict[str, Any]:
    path = Path(__file__).with_name("persona.json")
    return json.loads(path.read_text(encoding="utf-8"))


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


def get_threads_credentials() -> dict[str, str]:
    if not THREADS_SECRET_ID:
        raise RuntimeError("THREADS_SECRET_ID is missing")
    response = SECRETS.get_secret_value(SecretId=THREADS_SECRET_ID)
    values = json.loads(response["SecretString"])
    required = ("THREADS_ACCESS_TOKEN", "THREADS_USER_ID", "THREADS_USERNAME")
    missing = [field for field in required if not str(values.get(field, "")).strip()]
    if missing:
        raise RuntimeError(f"Threads secret is missing: {', '.join(missing)}")
    return {str(key): str(value) for key, value in values.items()}


def threads_request_json(
    url: str,
    *,
    method: str = "GET",
    data: dict[str, str] | None = None,
) -> dict[str, Any]:
    encoded = urllib.parse.urlencode(data).encode("utf-8") if data else None
    request = urllib.request.Request(url, data=encoded, method=method)
    request.add_header("Accept", "application/json")
    if encoded:
        request.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        try:
            payload = json.loads(error.read().decode("utf-8"))
            api_error = payload.get("error", {})
            message = api_error.get("message", "Threads API error")
            code = api_error.get("code", error.code)
        except (json.JSONDecodeError, AttributeError):
            message = "Threads API error"
            code = error.code
        raise RuntimeError(f"Threads API request failed: {message} (code {code})") from None


def refresh_threads_token_if_needed(
    credentials: dict[str, str], now: datetime
) -> dict[str, str]:
    expires_at = _parse_datetime(credentials.get("TOKEN_EXPIRES_AT"))
    if not expires_at:
        LOGGER.warning("Threads token expiry is missing; continuing without refresh")
        return credentials
    if expires_at - now.astimezone(EASTERN) > timedelta(
        days=THREADS_TOKEN_REFRESH_DAYS
    ):
        return credentials

    query = urllib.parse.urlencode(
        {
            "grant_type": "th_refresh_token",
            "access_token": credentials["THREADS_ACCESS_TOKEN"],
        }
    )
    refreshed = threads_request_json(
        f"https://graph.threads.net/refresh_access_token?{query}"
    )
    token = str(refreshed.get("access_token", ""))
    expires_in = int(refreshed.get("expires_in", 0))
    if not token or not expires_in:
        raise RuntimeError("Threads token refresh returned an incomplete response")

    issued_at = now.astimezone(ZoneInfo("UTC"))
    credentials.update(
        {
            "THREADS_ACCESS_TOKEN": token,
            "TOKEN_TYPE": str(refreshed.get("token_type", "bearer")),
            "TOKEN_ISSUED_AT": issued_at.isoformat(),
            "TOKEN_EXPIRES_AT": (
                issued_at + timedelta(seconds=expires_in)
            ).isoformat(),
        }
    )
    SECRETS.put_secret_value(
        SecretId=THREADS_SECRET_ID,
        SecretString=json.dumps(credentials),
    )
    LOGGER.info("Refreshed the Threads long-lived token")
    return credentials


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


def rank_social_events(
    events_data: dict[str, Any],
    history: dict[str, Any],
    now: datetime,
) -> list[dict[str, Any]]:
    """Rank today through two days ahead after enforcing the cooldown."""
    cutoff = now - timedelta(hours=COOLDOWN_HOURS)
    last_selected = history.get("last_selected", {})
    candidates = []
    for original in events_data.get("events", []):
        if str(original.get("availability_status") or "").lower() in UNAVAILABLE_EVENT_STATUSES:
            continue
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

        score = float(
            event.get("recommendation_score", event.get("quality_score", 5))
        )
        score += {0: 4, 1: 2, 2: 1}.get(offset, 0)
        if "free" in str(event.get("price") or "").lower():
            score += 1
        event["event_id"] = event_id
        event["day_offset"] = offset
        event["social_score"] = score
        candidates.append(event)

    candidates.sort(key=lambda item: item["social_score"], reverse=True)
    return candidates


def select_social_events(
    events_data: dict[str, Any],
    history: dict[str, Any],
    now: datetime,
) -> list[dict[str, Any]]:
    return rank_social_events(events_data, history, now)[:MAX_SOCIAL_EVENTS]


def format_events(events: list[dict[str, Any]]) -> str:
    labels = {0: "今天", 1: "明天", 2: "后天"}
    lines = []
    for index, event in enumerate(events, start=1):
        lines.append(
            " | ".join(
                (
                    f"{index}. {labels.get(event['day_offset'], event.get('date'))}",
                    str(event.get("name") or "Unknown"),
                    str(event.get("time") or "時間見活動頁面"),
                    str(event.get("location") or event.get("city") or "Greater Boston"),
                    str(event.get("price") or "價格見活動頁面"),
                    str(event.get("link") or ""),
                )
            )
        )
    return "\n".join(lines)


def build_prompt():
    from langchain_core.prompts import ChatPromptTemplate

    persona = load_persona()
    voice = "; ".join(persona["voice"])
    content_style = "; ".join(persona["content_style"])
    forbidden = "; ".join(persona["forbidden"])

    return ChatPromptTemplate.from_messages(
        [
            (
                "system",
                f"""You are {persona['display_name']}: {persona['role']}

Voice: {voice}
Editorial style: {content_style}
Never do any of the following: {forbidden}

You write one shared bilingual social post that will be used
unchanged on both Threads and Xiaohongshu. Use only facts supplied below. Never
invent dates, times, prices, venues, availability, or cancellation status.

Write the Traditional Chinese version first, using natural Taiwan-style wording,
and a natural English version second. Never use Simplified Chinese characters or
Mainland-China-specific wording. Both versions must describe the same selected
activities and must not introduce facts that appear in only one language. Keep
each body concise, friendly, and useful to people living around Greater Boston.
Mention 3-5 activities when available, preserve their source links, and end each
body with the weekend-report URL. Use plain text and raw URLs; do not use Markdown
link syntax because the same copy is published directly to both platforms. Do not
claim that an event is recommended from personal experience. Do not place emoji
or kaomoji in the generated fields; the application adds Bo's chosen expressions.

Return strict JSON with exactly these top-level keys: zh, en, hashtags. `zh` and
`en` must each contain exactly `title` and `body` strings. `hashtags` must be an
array of language-neutral or bilingual strings without leading # characters.""",
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
    for language in ("zh", "en"):
        localized = result.get(language)
        if not isinstance(localized, dict) or not isinstance(
            localized.get("title"), str
        ) or not isinstance(localized.get("body"), str):
            raise ValueError(
                f"Model response did not contain {language} title and body strings"
            )
    hashtags = result.get("hashtags")
    if not isinstance(hashtags, list):
        raise ValueError("Model response did not contain a hashtags array")
    result["hashtags"] = [str(tag).lstrip("#") for tag in hashtags]
    generated_text = json.dumps(result, ensure_ascii=False)
    if EMOJI_PATTERN.search(generated_text):
        raise ValueError("Model response contained emoji")
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


def select_kaomoji(events: list[dict[str, Any]], now: datetime) -> str:
    persona = load_persona()
    seed_text = "|".join(
        [now.strftime("%Y-%m-%d"), "daily-social"]
        + sorted(str(event.get("event_id") or _fallback_event_id(event)) for event in events)
    )
    seed = int(hashlib.sha256(seed_text.encode("utf-8")).hexdigest()[:16], 16)
    searchable = json.dumps(events, ensure_ascii=False).lower()
    themed: list[str] = []
    for group in persona.get("themed_expressions", []):
        if any(str(keyword).lower() in searchable for keyword in group["keywords"]):
            themed.extend(str(expression) for expression in group["expressions"])

    common = [str(expression) for expression in persona["common_expressions"]]
    common_weight = int(persona.get("common_expression_weight", 70))
    pool = themed if themed and seed % 100 >= common_weight else common
    return pool[(seed // 100) % len(pool)]


def render_shared_text(
    content: dict[str, Any], mood_kaomoji: str | None = None
) -> str:
    persona = load_persona()
    hashtags = " ".join(f"#{tag}" for tag in content["hashtags"])
    title = content["zh"]["title"]
    if mood_kaomoji:
        title = f"{title} {mood_kaomoji}"
    return (
        f"{title}\n\n"
        f"{content['zh']['body']}\n\n"
        "—— English ——\n\n"
        f"{content['en']['title']}\n\n"
        f"{content['en']['body']}\n\n"
        f"{hashtags}\n\n"
        f"{persona['signoff']}"
    ).strip()


def split_threads_text(text: str, limit: int = THREADS_MAX_POST_LENGTH) -> list[str]:
    """Split shared copy on paragraph/word boundaries without changing its text."""
    if limit < 1:
        raise ValueError("Threads post length limit must be positive")
    chunks: list[str] = []
    current = ""

    def append_piece(piece: str, separator: str) -> None:
        nonlocal current
        candidate = f"{current}{separator}{piece}" if current else piece
        if len(candidate) <= limit:
            current = candidate
            return
        if current:
            chunks.append(current)
            current = ""
        while len(piece) > limit:
            boundary = piece.rfind(" ", 0, limit + 1)
            if boundary <= 0:
                boundary = limit
            chunks.append(piece[:boundary].rstrip())
            piece = piece[boundary:].lstrip()
        current = piece

    for paragraph in text.split("\n\n"):
        append_piece(paragraph, "\n\n")
    if current:
        chunks.append(current)
    return chunks


def create_threads_container(
    user_id: str,
    token: str,
    text: str,
    reply_to_id: str | None = None,
) -> str:
    data = {
        "media_type": "TEXT",
        "text": text,
        "access_token": token,
    }
    if reply_to_id:
        data["reply_to_id"] = reply_to_id
    response = threads_request_json(
        f"{THREADS_API_BASE}/{user_id}/threads",
        method="POST",
        data=data,
    )
    creation_id = str(response.get("id", ""))
    if not creation_id:
        raise RuntimeError("Threads did not return a creation container ID")
    return creation_id


def publish_threads_container(user_id: str, token: str, creation_id: str) -> str:
    response = threads_request_json(
        f"{THREADS_API_BASE}/{user_id}/threads_publish",
        method="POST",
        data={"creation_id": creation_id, "access_token": token},
    )
    post_id = str(response.get("id", ""))
    if not post_id:
        raise RuntimeError("Threads did not return a published post ID")
    return post_id


def publish_threads_text(text: str, credentials: dict[str, str]) -> list[str]:
    post_ids: list[str] = []
    reply_to_id = None
    for chunk in split_threads_text(text):
        creation_id = create_threads_container(
            credentials["THREADS_USER_ID"],
            credentials["THREADS_ACCESS_TOKEN"],
            chunk,
            reply_to_id,
        )
        post_id = publish_threads_container(
            credentials["THREADS_USER_ID"],
            credentials["THREADS_ACCESS_TOKEN"],
            creation_id,
        )
        post_ids.append(post_id)
        reply_to_id = post_id
    return post_ids


def publication_key(now: datetime) -> str:
    return f"social/publications/threads/{now:%Y-%m-%d}.json"


def write_publication_state(key: str, state: dict[str, Any], *, claim: bool = False) -> None:
    kwargs: dict[str, Any] = {
        "Bucket": BUCKET_NAME,
        "Key": key,
        "Body": json.dumps(state, ensure_ascii=False, indent=2).encode("utf-8"),
        "ContentType": "application/json; charset=utf-8",
    }
    if claim:
        kwargs["IfNoneMatch"] = "*"
    try:
        S3.put_object(**kwargs)
    except ClientError as error:
        if claim and error.response.get("Error", {}).get("Code") in {
            "PreconditionFailed",
            "412",
        }:
            raise RuntimeError(
                "Threads publication was already claimed; refusing a duplicate post"
            ) from None
        raise


def store_campaign(
    content: dict[str, Any],
    events: list[dict[str, Any]],
    history: dict[str, Any],
    now: datetime,
    threads_state: dict[str, Any] | None = None,
    mood_kaomoji: str | None = None,
    ranked_events: list[dict[str, Any]] | None = None,
) -> dict[str, str]:
    campaign_id = now.strftime("%Y-%m-%d")
    event_ids = [event["event_id"] for event in events]
    campaign = {
        "campaign_id": campaign_id,
        "generated_at": now.isoformat(),
        "content": content,
        "shared_text": render_shared_text(content, mood_kaomoji),
        "selected_event_ids": event_ids,
        "selection_decisions": [
            {
                "event_id": event["event_id"],
                "rank": rank,
                "selected": event["event_id"] in event_ids,
                "recommendation_score": event.get("recommendation_score"),
                "recommendation": event.get("recommendation"),
                "social_score": event.get("social_score"),
            }
            for rank, event in enumerate(ranked_events or events, start=1)
        ],
        "persona": {
            "id": load_persona()["id"],
            "version": load_persona()["version"],
            "mood_kaomoji": mood_kaomoji,
        },
        "platforms": {
            "threads": threads_state or {"status": "ready"},
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
    publish_key = publication_key(now)
    if THREADS_PUBLISH_ENABLED:
        existing = load_json(publish_key, default={})
        if existing:
            status = existing.get("status", "unknown")
            if status == "published":
                return {
                    "success": True,
                    "generated_at": existing.get("updated_at"),
                    "threads_publish_status": "already_published",
                    "thread_post_ids": existing.get("post_ids", []),
                }
            raise RuntimeError(
                f"Threads publication state is {status}; inspect {publish_key} "
                "before retrying to avoid a duplicate post"
            )

    events_data = load_json("events/latest.json")
    history = load_json("social/history.json", default={})
    ranked = rank_social_events(events_data, history, now)
    selected = ranked[:MAX_SOCIAL_EVENTS]
    if not selected:
        raise RuntimeError("No eligible events remain after the 48-hour cooldown")
    mood_kaomoji = select_kaomoji(selected, now)
    content = generate_content(selected, now)
    keys = store_campaign(
        content,
        selected,
        history,
        now,
        mood_kaomoji=mood_kaomoji,
        ranked_events=ranked,
    )
    threads_result: dict[str, Any] = {"status": "disabled"}
    if THREADS_PUBLISH_ENABLED:
        shared_text = render_shared_text(content, mood_kaomoji)
        claim = {
            "campaign_id": now.strftime("%Y-%m-%d"),
            "status": "publishing",
            "updated_at": now.isoformat(),
            "content_sha256": hashlib.sha256(shared_text.encode("utf-8")).hexdigest(),
        }
        write_publication_state(publish_key, claim, claim=True)
        try:
            credentials = refresh_threads_token_if_needed(
                get_threads_credentials(), now
            )
            post_ids = publish_threads_text(shared_text, credentials)
        except Exception as error:
            failed = {
                **claim,
                "status": "failed",
                "updated_at": datetime.now(EASTERN).isoformat(),
                "error_type": type(error).__name__,
            }
            write_publication_state(publish_key, failed)
            store_campaign(
                content,
                selected,
                history,
                now,
                threads_state={"status": "failed"},
                mood_kaomoji=mood_kaomoji,
                ranked_events=ranked,
            )
            raise
        threads_result = {
            "status": "published",
            "post_ids": post_ids,
            "username": credentials["THREADS_USERNAME"],
        }
        published = {
            **claim,
            **threads_result,
            "updated_at": datetime.now(EASTERN).isoformat(),
        }
        write_publication_state(publish_key, published)
        store_campaign(
            content,
            selected,
            history,
            now,
            threads_state=threads_result,
            mood_kaomoji=mood_kaomoji,
            ranked_events=ranked,
        )
    result = {
        "success": True,
        "generated_at": now.isoformat(),
        "selected_event_ids": [event["event_id"] for event in selected],
        "platform_content": "shared",
        "threads": threads_result,
        "s3_keys": keys,
    }
    LOGGER.info("Daily social campaign generated: %s", json.dumps(result))
    return result
