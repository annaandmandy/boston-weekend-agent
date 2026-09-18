"""Generate and store the Boston weekend report from S3 snapshots."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from datetime import date, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import boto3


LOGGER = logging.getLogger(__name__)
LOGGER.setLevel(os.environ.get("LOG_LEVEL", "INFO").upper())

AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
BUCKET_NAME = os.environ.get("REPORT_BUCKET", "boston-weekend-agent-reports")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-5.6-luna")
OPENAI_REASONING_EFFORT = os.environ.get("OPENAI_REASONING_EFFORT", "none")
PROMPT_VERSION = os.environ.get("REPORT_PROMPT_VERSION", "v3.1-bobo-bilingual")
RANKING_PROMPT_VERSION = "ai-semantic-ranking-v1"
BOBO_MEMORY_KEY = os.environ.get("BOBO_MEMORY_KEY", "agent/bobo-memory.json")
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


def load_default_memory() -> dict[str, Any]:
    path = Path(__file__).with_name("memory.default.json")
    return json.loads(path.read_text(encoding="utf-8"))


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


def load_json(key: str) -> dict[str, Any]:
    response = S3.get_object(Bucket=BUCKET_NAME, Key=key)
    return json.loads(response["Body"].read().decode("utf-8"))


def is_missing_s3_error(error: Exception) -> bool:
    return getattr(error, "response", {}).get("Error", {}).get("Code") in {
        "NoSuchKey",
        "404",
    }


def load_optional_json(key: str) -> dict[str, Any]:
    try:
        return load_json(key)
    except Exception as error:
        if is_missing_s3_error(error):
            return {}
        raise


def load_bobo_memory() -> dict[str, Any]:
    try:
        memory = load_optional_json(BOBO_MEMORY_KEY)
        return memory or load_default_memory()
    except Exception as error:
        if getattr(error, "response", {}).get("Error", {}).get("Code") in {
            "AccessDenied",
            "AccessDeniedException",
        }:
            LOGGER.warning("Using bundled Bo memory until IAM read access is granted")
            return load_default_memory()
        raise


def analytics_run_prefix(now: datetime) -> str:
    """Build an immutable, Athena-friendly partition for one report run."""
    run_id = now.strftime("%Y%m%dT%H%M%S%f%z")
    return (
        f"analytics/report_runs/"
        f"year={now:%Y}/"
        f"month={now:%m}/"
        f"day={now:%d}/"
        f"run_id={run_id}"
    )


def archive_input_object(
    source_key: str,
    destination_key: str,
) -> dict[str, Any]:
    """Copy one exact S3 input version into the immutable analytics run."""
    head = S3.head_object(
        Bucket=BUCKET_NAME,
        Key=source_key,
    )

    copy_source = {
        "Bucket": BUCKET_NAME,
        "Key": source_key,
    }

    source_version_id = head.get("VersionId")
    if source_version_id:
        copy_source["VersionId"] = source_version_id

    copied = S3.copy_object(
        Bucket=BUCKET_NAME,
        Key=destination_key,
        CopySource=copy_source,
        MetadataDirective="COPY",
    )

    return {
        "status": "archived",
        "source_key": source_key,
        "source_version_id": source_version_id,
        "source_etag": str(head.get("ETag", "")).strip('"'),
        "archive_key": destination_key,
        "archive_version_id": copied.get("VersionId"),
    }


def archive_optional_input_object(
    source_key: str,
    destination_key: str,
) -> dict[str, Any]:
    try:
        return archive_input_object(source_key, destination_key)
    except Exception as error:
        if is_missing_s3_error(error):
            return {
                "status": "missing",
                "source_key": source_key,
                "archive_key": None,
            }
        raise


def archive_report_inputs(
    now: datetime,
) -> tuple[str, dict[str, dict[str, Any]]]:
    """Archive every input used by one report and return its lineage."""
    prefix = analytics_run_prefix(now)

    input_objects = {
        "events": (
            "events/latest.json",
            f"{prefix}/events.json",
        ),
        "event_changes": (
            "events/changes/latest.json",
            f"{prefix}/event_changes.json",
        ),
        "weather": (
            "weather/latest.json",
            f"{prefix}/weather.json",
        ),
        "weather_summary": (
            "weather/summary.json",
            f"{prefix}/weather_summary.json",
        ),
    }

    archived = {
        name: archive_input_object(source_key, destination_key)
        for name, (source_key, destination_key) in input_objects.items()
    }

    return prefix, archived


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
        if str(original.get("availability_status") or "").lower() in {
            "canceled",
            "cancelled",
            "offsale",
            "postponed",
            "rescheduled",
            "sold_out",
        }:
            continue
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
        score = float(
            event.get("recommendation_score", event.get("quality_score", 5))
        )
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


def build_ranking_prompt():
    from langchain_core.prompts import ChatPromptTemplate

    return ChatPromptTemplate.from_messages(
        [
            (
                "system",
                """You are Bo, the semantic ranking judge for a Greater Boston
weekend letter. Rank every supplied, eligible event together. Python has already
enforced availability and date rules; your role is to understand cultural and
leisure meaning that simple code cannot.

Use only supplied evidence. Recognize when an annual, culturally significant,
visually distinctive, or community-defining event merits a trip. A landmark
event such as Revere's sand sculpting festival may outrank routine nearby plans.
Proximity is useful but is never a quota or veto. Do not invent significance.

Score each event from 0 to 100. These dimensions must sum to the final score:
leisure_appeal 0-25, local_significance 0-25, rarity 0-20, value 0-10,
proximity_fit 0-10, information_confidence 0-10. Return each event_id exactly
once, best to worst, with Traditional Chinese and English reasons.

Return strict JSON only:
{{"rankings":[{{"event_id":"...","score":0,"dimensions":{{"leisure_appeal":0,
"local_significance":0,"rarity":0,"value":0,"proximity_fit":0,
"information_confidence":0}},"destination_worthy":false,
"significance_signals":[],"reason_zh":"...","reason_en":"..."}}]}}""",
            ),
            (
                "human",
                """Bo's versioned identity:
{persona_json}

Bo's reviewed long-term memory:
{memory_json}

Weekend candidates:
{events_json}""",
            ),
        ]
    )


def compact_ranking_events(events: list[dict[str, Any]]) -> str:
    fields = (
        "event_id",
        "name",
        "description",
        "category",
        "source",
        "date",
        "time",
        "city",
        "location",
        "price",
        "time_label",
    )
    compact = []
    for index, event in enumerate(events):
        item = {field: event.get(field) for field in fields if event.get(field) is not None}
        item["event_id"] = str(event.get("event_id") or f"weekend-{index}")
        item["distance_miles"] = (event.get("recommendation") or {}).get(
            "distance_miles"
        )
        if "description" in item:
            item["description"] = str(item["description"])[:700]
        compact.append(item)
    return json.dumps(compact, ensure_ascii=False)


def parse_ai_rankings(content: Any, candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", str(content).strip(), flags=re.I)
    payload = json.loads(text)
    rankings = payload.get("rankings")
    if not isinstance(rankings, list):
        raise ValueError("AI ranking response did not contain a rankings array")

    by_id = {}
    for index, event in enumerate(candidates):
        event_id = str(event.get("event_id") or f"weekend-{index}")
        normalized_event = dict(event)
        normalized_event["event_id"] = event_id
        by_id[event_id] = normalized_event
    returned_ids = [str(item.get("event_id")) for item in rankings]
    if len(returned_ids) != len(set(returned_ids)) or set(returned_ids) != set(by_id):
        raise ValueError("AI ranking response must contain every candidate exactly once")

    limits = {
        "leisure_appeal": 25,
        "local_significance": 25,
        "rarity": 20,
        "value": 10,
        "proximity_fit": 10,
        "information_confidence": 10,
    }
    ranked = []
    for item in rankings:
        event_id = str(item["event_id"])
        dimensions = item.get("dimensions")
        if not isinstance(dimensions, dict):
            raise ValueError(f"AI ranking dimensions missing for {event_id}")
        raw_dimensions = {}
        normalized = {}
        warnings = []
        for name, maximum in limits.items():
            if name not in dimensions:
                raise ValueError(f"AI ranking dimension {name} is missing for {event_id}")
            raw_value = float(dimensions[name])
            raw_dimensions[name] = raw_value
            value = min(max(raw_value, 0), maximum)
            if value != raw_value:
                warnings.append(
                    f"{name} clamped from {raw_value:g} to {value:g}"
                )
            normalized[name] = value
        calculated_score = round(sum(normalized.values()), 2)
        supplied_score = float(item.get("score", calculated_score))
        if abs(calculated_score - supplied_score) > 0.01:
            warnings.append(
                f"total recomputed from {supplied_score:g} to {calculated_score:g}"
            )

        event = dict(by_id[event_id])
        event["ai_ranking"] = {
            "score": calculated_score,
            "dimensions": normalized,
            "raw_dimensions": raw_dimensions,
            "normalization_warnings": warnings,
            "destination_worthy": bool(item.get("destination_worthy")),
            "significance_signals": [
                str(signal) for signal in item.get("significance_signals", [])
            ],
            "reason_zh": str(item.get("reason_zh") or ""),
            "reason_en": str(item.get("reason_en") or ""),
        }
        event["priority_score"] = calculated_score
        ranked.append(event)
    return sorted(ranked, key=lambda item: item["priority_score"], reverse=True)


def ai_rank_weekend_events(
    candidates: list[dict[str, Any]], memory: dict[str, Any]
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    from langchain_openai import ChatOpenAI

    if not candidates:
        return [], {
            "model": OPENAI_MODEL,
            "prompt_version": RANKING_PROMPT_VERSION,
            "memory_version": memory.get("memory_version"),
            "candidate_count": 0,
            "token_usage": {},
            "status": "skipped_no_candidates",
        }

    model = ChatOpenAI(
        **chat_model_options(get_openai_api_key(), 0.2),
        max_tokens=7000,
    )
    result = (build_ranking_prompt() | model).invoke(
        {
            "persona_json": json.dumps(load_persona(), ensure_ascii=False),
            "memory_json": json.dumps(memory, ensure_ascii=False),
            "events_json": compact_ranking_events(candidates),
        }
    )
    usage = getattr(result, "usage_metadata", None) or getattr(
        result, "response_metadata", {}
    ).get("token_usage", {})
    return parse_ai_rankings(result.content, candidates), {
        "model": OPENAI_MODEL,
        "prompt_version": RANKING_PROMPT_VERSION,
        "memory_version": memory.get("memory_version"),
        "candidate_count": len(candidates),
        "token_usage": usage or {},
    }


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
                f"   Link: {event.get('link') or 'Not provided'}",
                f"   Bo ranking score: {(event.get('ai_ranking') or {}).get('score', 'Unknown')}",
                f"   Bo ranking reason: {(event.get('ai_ranking') or {}).get('reason_zh', 'Not provided')}",
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

Combine the supplied event and weather data with broadly known Boston seasonal
activities. Clearly distinguish listed events from general local suggestions.
Do not invent event dates, prices, locations, opening hours, availability, or
personal experiences. You live in Boston as a fictional editorial character,
but you must never claim that you personally attended an event.

Current context:
- Day: {{day_name}}
- Date: {{date}}
- Time: {{time}} ({{time_of_day}})
- Weekend status: {{weekend_status}}
- Upcoming holidays: {{holiday_context}}
- Edition: {{edition}}

For a Thursday preview, present this as an early planning edition and say that
weather and event details will be checked again Friday morning. For a Friday
update, naturally call out material new or changed listings and refreshed
weather. Never describe an event as cancelled merely because it is listed as
unconfirmed missing.

Write two independent versions of a generous bilingual weekend letter, not a
database summary or a tourism brochure. `zh` must use natural Taiwan Traditional
Chinese and temperatures only in °C. `en` must use natural English and
temperatures only in °F. Both languages must contain the same event facts and
links, although the English should be a natural adaptation rather than a literal
translation. Never use Simplified Chinese.

Aim for 700-1000 Traditional Chinese characters and 450-650 English words when
enough verified material is available. In each language:
- Begin with an expressive bold heading and a 2-3 sentence Boston weekend scene.
- Move through weather, 5-8 event possibilities, one free option when available,
  and an insider-style practical note using warm narrative transitions.
- End with a small morning-to-evening route or two alternative moods, written as
  prose rather than a repetitive numbered list.
- Use short paragraphs and bold section headings that this website can render.
- Preserve only the supplied event URLs. Write them as raw URLs, not Markdown
  links, because the current website renders only bold Markdown. Do not invent or
  append a weekend-report URL; this report is already displayed on that page.

Use a friendly, lively, lightly playful voice, like a local friend thinking
through the weekend aloud. Sensory language may set a mood, but every factual
claim must stay grounded in the supplied data. Mention source uncertainty when
details are incomplete. Do not output emoji, kaomoji, or a signature; the
application adds Bo's expression and signoff deterministically.

Return strict JSON only, without code fences:
{{"zh":{{"title":"...","body":"Markdown..."}},
"en":{{"title":"...","body":"Markdown..."}}}}

Keep the title separate from the Markdown body. Do not add a language divider,
emoji, kaomoji, or signature; the application renders each language separately
and adds Bo's identity deterministically.""",
            ),
            (
                "human",
                """Generate this week's Boston weekend report.

Weather for the Traditional Chinese version:
- Best outdoor day: {best_day}
- Best time: {best_time}
- Temperature: {temperature_zh}
- Rain chance: {rain_chance}

Weather for the English version:
- Best outdoor day: {best_day}
- Best time: {best_time}
- Temperature: {temperature_en}
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


def fahrenheit_temperature_text(value: Any) -> str:
    text = str(value or "Unknown")
    if "°C" not in text:
        return text

    def convert(match: re.Match[str]) -> str:
        fahrenheit = float(match.group(1)) * 9 / 5 + 32
        return f"{fahrenheit:.1f}"

    return re.sub(r"((?<![\d.])-?\d+(?:\.\d+)?)", convert, text).replace(
        "°C", "°F"
    )


def parse_bilingual_report(content: Any) -> dict[str, dict[str, str]]:
    text = re.sub(
        r"^```(?:json)?\s*|\s*```$", "", str(content).strip(), flags=re.I
    )
    payload = json.loads(text)
    parsed: dict[str, dict[str, str]] = {}
    for language in ("zh", "en"):
        section = payload.get(language)
        if not isinstance(section, dict):
            raise ValueError(f"Report response is missing {language}")
        title = str(section.get("title") or "").strip()
        body = str(section.get("body") or "").strip()
        if not title or not body:
            raise ValueError(f"Report response has incomplete {language} content")
        parsed[language] = {"title": title, "body": body}
    return parsed


def select_report_kaomoji(events: list[dict[str, Any]], now: datetime) -> str:
    persona = load_persona()
    event_names = sorted(str(event.get("name") or "") for event in events)
    seed_text = "|".join([now.strftime("%Y-%m-%d"), "weekend-report", *event_names])
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


def finalize_report_text(content: Any, mood_kaomoji: str) -> str:
    persona = load_persona()
    report = EMOJI_PATTERN.sub("", normalize_report_text(content)).strip()
    signoff = persona["signoff"]
    report = report.replace(signoff, "").strip()
    return f"{mood_kaomoji}\n\n{report}\n\n{signoff}"


def finalize_language_report(
    section: dict[str, str], mood_kaomoji: str
) -> str:
    persona = load_persona()
    title = EMOJI_PATTERN.sub("", section["title"]).strip()
    body = EMOJI_PATTERN.sub("", section["body"]).strip()
    signoff = persona["signoff"]
    body = body.replace(signoff, "").strip()
    return f"{mood_kaomoji}\n\n**{title}**\n\n{body}\n\n{signoff}"


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
    candidates = filter_and_prioritize_events(events_data, now)
    memory = load_bobo_memory()
    events, ranking_metadata = ai_rank_weekend_events(candidates, memory)
    recommendations = (weather_data.get("summary") or {}).get("recommendations") or {}
    top_pick = recommendations.get("top_pick") or {}

    model = ChatOpenAI(**chat_model_options(get_openai_api_key(), 0.4))
    result = (build_prompt() | model).invoke(
        {
            **context,
            "edition": edition,
            "holiday_context": ", ".join(context["holidays"]) or "None",
            "best_day": top_pick.get("date", "Unknown"),
            "best_time": top_pick.get("time_window", "Unknown"),
            "temperature_zh": top_pick.get("temperature", "Unknown"),
            "temperature_en": fahrenheit_temperature_text(
                top_pick.get("temperature", "Unknown")
            ),
            "rain_chance": top_pick.get("rain_chance", "Unknown"),
            "events": format_events(events),
            "event_changes": format_event_changes(changes_data or {}),
        }
    )
    mood_kaomoji = select_report_kaomoji(events, now)
    parsed_report = parse_bilingual_report(result.content)
    report_zh = finalize_language_report(parsed_report["zh"], mood_kaomoji)
    report_en = finalize_language_report(parsed_report["en"], mood_kaomoji)
    report = f"{report_zh}\n\n—— English ——\n\n{report_en}"
    usage_metadata = getattr(result, "usage_metadata", None)
    if not usage_metadata:
        usage_metadata = getattr(result, "response_metadata", {}).get(
            "token_usage", {}
        )
    return {
        "report": report,
        "languages": {
            "zh": {
                "locale": "zh-TW",
                "temperature_unit": "C",
                "markdown": report_zh,
            },
            "en": {
                "locale": "en-US",
                "temperature_unit": "F",
                "markdown": report_en,
            },
        },
        "generated_at": now.isoformat(),
        "events_count": len(events),
        "edition": edition,
        "model": OPENAI_MODEL,
        "prompt_version": PROMPT_VERSION,
        "persona": {
            "id": load_persona()["id"],
            "version": load_persona()["version"],
            "mood_kaomoji": mood_kaomoji,
        },
        "ranking": ranking_metadata,
        "openai_call_count": 2 if candidates else 1,
        "ranked_events": [
            {
                "event_id": event.get("event_id"),
                "name": event.get("name"),
                "final_rank": rank,
                "ai_ranking": event.get("ai_ranking"),
            }
            for rank, event in enumerate(events, start=1)
        ],
        "token_usage": usage_metadata or {},
        "context": context,
    }


def store_report(result: dict[str, Any], now: datetime) -> dict[str, str]:
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
    json_payload = {
        "schema_version": 1,
        "generated_at": result["generated_at"],
        "edition": result["edition"],
        "languages": result["languages"],
    }
    json_body = json.dumps(json_payload, ensure_ascii=False, indent=2).encode("utf-8")
    timestamped_json_key = (
        f"reports/{now:%Y-%m}/report_{now:%Y%m%d_%H%M%S}.json"
    )
    archive_json_key = (
        f"reports/archive/{now:%Y/%m}/"
        f"{now:%Y-%m-%d}_{result['edition']}.json"
    )
    latest_json_key = "reports/weekend_summary.json"
    for key in (timestamped_json_key, archive_json_key, latest_json_key):
        S3.put_object(
            Bucket=BUCKET_NAME,
            Key=key,
            Body=json_body,
            ContentType="application/json; charset=utf-8",
        )
    return {
        "latest": latest_key,
        "timestamped": timestamped_key,
        "archive": archive_key,
        "latest_json": latest_json_key,
        "timestamped_json": timestamped_json_key,
        "archive_json": archive_json_key,
    }


def store_analytics_json(key: str, value: dict[str, Any]) -> str:
    S3.put_object(
        Bucket=BUCKET_NAME,
        Key=key,
        Body=json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8"),
        ContentType="application/json",
    )
    return key


def store_analytics_manifest(
    *,
    prefix: str,
    result: dict[str, Any],
    archived_inputs: dict[str, dict[str, Any]],
    report_keys: dict[str, str],
    effective_changes_key: str,
    baseline_lineage: dict[str, Any] | None,
    lambda_request_id: str | None,
) -> dict[str, str]:
    report_text_key = f"{prefix}/report.txt"
    S3.put_object(
        Bucket=BUCKET_NAME,
        Key=report_text_key,
        Body=result["report"].encode("utf-8"),
        ContentType="text/plain; charset=utf-8",
    )

    manifest_key = f"{prefix}/report.json"
    manifest = {
        "schema_version": 1,
        "run_id": prefix.rsplit("run_id=", 1)[-1],
        "generated_at": result["generated_at"],
        "edition": result["edition"],
        "model": result["model"],
        "prompt_version": result["prompt_version"],
        "persona": result.get("persona"),
        "ranking": result.get("ranking"),
        "ranked_events": result.get("ranked_events"),
        "openai_call_count": result.get("openai_call_count"),
        "token_usage": result["token_usage"],
        "events_count": result["events_count"],
        "lambda_request_id": lambda_request_id,
        "input_objects": archived_inputs,
        "derived_objects": {
            "effective_event_changes": effective_changes_key,
            "weekend_baseline": baseline_lineage,
        },
        "output_objects": {
            **report_keys,
            "analytics_report_text": report_text_key,
        },
        "report_text": result["report"],
    }
    store_analytics_json(manifest_key, manifest)
    return {"manifest": manifest_key, "report_text": report_text_key}


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    now = datetime.now(EASTERN)
    analytics_prefix, archived_inputs = archive_report_inputs(now)
    events_data = load_json(archived_inputs["events"]["archive_key"])
    weather_data = load_json(
        archived_inputs["weather_summary"]["archive_key"]
    )
    changes_data = load_json(
        archived_inputs["event_changes"]["archive_key"]
    )
    edition_override = event.get("edition") if isinstance(event, dict) else None
    edition = determine_edition(now, edition_override)
    baseline_key = weekend_baseline_key(now)
    baseline_lineage = None
    if edition == "thursday-preview":
        store_weekend_baseline(events_data, now)
        baseline_lineage = {
            "status": "created",
            "role": "created_for_friday_comparison",
            "key": baseline_key,
        }
    elif edition == "friday-update":
        baseline_lineage = archive_optional_input_object(
            baseline_key,
            f"{analytics_prefix}/thursday_baseline.json",
        )
        if baseline_lineage["status"] == "archived":
            baseline = load_json(baseline_lineage["archive_key"])
            changes_data = compare_event_snapshots(baseline, events_data)

    effective_changes_key = store_analytics_json(
        f"{analytics_prefix}/effective_event_changes.json",
        changes_data,
    )
    result = generate_report(
        events_data,
        weather_data,
        changes_data,
        edition_override=edition_override,
        now=now,
    )
    keys = store_report(result, now)
    analytics_keys = store_analytics_manifest(
        prefix=analytics_prefix,
        result=result,
        archived_inputs=archived_inputs,
        report_keys=keys,
        effective_changes_key=effective_changes_key,
        baseline_lineage=baseline_lineage,
        lambda_request_id=getattr(context, "aws_request_id", None),
    )
    response = {
        "success": True,
        "generated_at": result["generated_at"],
        "events_count": result["events_count"],
        "edition": result["edition"],
        "baseline_key": baseline_key,
        "analytics_prefix": analytics_prefix,
        "archived_inputs": archived_inputs,
        "analytics_keys": analytics_keys,
        "bucket": BUCKET_NAME,
        "s3_keys": keys,
    }
    LOGGER.info("Report generation complete: %s", json.dumps(response))
    return response
