"""Generate one shared bilingual daily post for Threads and Xiaohongshu."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
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
RANKING_PROMPT_VERSION = "ai-global-top-10-v2"
RANKING_TOP_K = 10
RANKING_INPUT_TOKEN_BUDGET = 50_000
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
THREADS_REPLY_SETTLE_SECONDS = float(
    os.environ.get("THREADS_REPLY_SETTLE_SECONDS", "2")
)
THREADS_REPLY_CREATE_ATTEMPTS = int(
    os.environ.get("THREADS_REPLY_CREATE_ATTEMPTS", "4")
)
THREADS_TOKEN_REFRESH_DAYS = int(
    os.environ.get("THREADS_TOKEN_REFRESH_DAYS", "7")
)
BOBO_MEMORY_KEY = os.environ.get("BOBO_MEMORY_KEY", "agent/bobo-memory.json")
THREADS_INTRODUCTION_KEY = "social/publications/threads/introduction-v2.json"
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
KAOMOJI_PATTERNS = (
    re.compile(r"〔[^〕\n]{2,40}〕[^\s\w]{0,3}"),
    re.compile(
        r"\([^()\n]{0,24}[▽▼△▲ωᴗ∀Дд益﹏・･ー^＾´`ﾟ°•ಠಥ≧≦><＞＜]"
        r"[^()\n]{0,24}\)[^\s\w]{0,3}"
    ),
)


@lru_cache(maxsize=1)
def load_persona() -> dict[str, Any]:
    path = Path(__file__).with_name("persona.json")
    return json.loads(path.read_text(encoding="utf-8"))


def load_default_memory() -> dict[str, Any]:
    path = Path(__file__).with_name("memory.default.json")
    return json.loads(path.read_text(encoding="utf-8"))


def load_bobo_memory() -> dict[str, Any]:
    try:
        return load_json(BOBO_MEMORY_KEY, default=load_default_memory())
    except Exception as error:
        if getattr(error, "response", {}).get("Error", {}).get("Code") in {
            "AccessDenied",
            "AccessDeniedException",
        }:
            LOGGER.warning("Using bundled Bo memory until IAM read access is granted")
            return load_default_memory()
        raise


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

        base_score = float(
            event.get("recommendation_score", event.get("quality_score", 5))
        )
        adjustments = {
            "timing_bonus": {0: 4, 1: 2, 2: 1}.get(offset, 0),
            "free_bonus": (
                1 if "free" in str(event.get("price") or "").lower() else 0
            ),
        }
        score = base_score + sum(adjustments.values())
        event["event_id"] = event_id
        event["day_offset"] = offset
        event["social_adjustments"] = adjustments
        event["social_score"] = score
        candidates.append(event)

    candidates.sort(key=lambda item: item["social_score"], reverse=True)
    return candidates


def build_ranking_prompt():
    from langchain_core.prompts import ChatPromptTemplate

    return ChatPromptTemplate.from_messages(
        [
            (
                "system",
                """You are the semantic ranking editor for Boston Weekend Agent.
Compare all supplied events together in one global decision for a person based near Boston University who
wants worthwhile leisure plans around Greater Boston. Python has already removed
unavailable, out-of-window, and cooldown events; do not reverse those hard rules.

Use the event's supplied name, description, category, source, price, date,
location, and distance evidence. Understand meaning rather than keyword counts.
A rare annual or culturally significant destination event—such as Revere's sand
sculpting festival—may outrank an ordinary nearby event when the trip is worth it.
Proximity is useful but never a quota or veto. Do not invent facts or claim an
event is annual unless the supplied data supports that inference.

Evaluate every candidate comparatively, then score each selected top event from
0 to 100 using these dimensions, whose values must sum to the final score:
leisure_appeal 0-25, local_significance 0-25, rarity 0-20,
value 0-10, proximity_fit 0-10, information_confidence 0-10. Return only the
strongest {selection_count} events, ordered best to worst. Every returned event_id
must come from the candidates and appear once. Do not return scores for candidates
outside the selected top group. Give concise Traditional Chinese and English
reasons, plus destination_worthy and significance_signals.

Return strict JSON only:
{{"rankings":[{{"event_id":"...","score":0,"dimensions":{{"leisure_appeal":0,
"local_significance":0,"rarity":0,"value":0,"proximity_fit":0,
"information_confidence":0}},"destination_worthy":false,
"significance_signals":[],"reason_zh":"...","reason_en":"..."}}]}}""",
            ),
            (
                "human",
                """Runtime identity for the ranking judge:
{persona_json}

Versioned long-term preference memory:
{memory_json}

Rank these verified candidate events:
{events_json}

Candidate count: {candidate_count}
Required ranking count: {selection_count}""",
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
        "day_offset",
    )
    compact = []
    for event in events:
        item = {field: event.get(field) for field in fields if event.get(field) is not None}
        item["distance_miles"] = (event.get("recommendation") or {}).get(
            "distance_miles"
        )
        if "description" in item:
            item["description"] = str(item["description"])[:700]
        compact.append(item)
    return json.dumps(compact, ensure_ascii=False)


def parse_ai_rankings(
    content: Any,
    candidates: list[dict[str, Any]],
    top_k: int = RANKING_TOP_K,
) -> list[dict[str, Any]]:
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", str(content).strip(), flags=re.I)
    payload = json.loads(text)
    rankings = payload.get("rankings")
    if not isinstance(rankings, list):
        raise ValueError("AI ranking response did not contain a rankings array")

    by_id = {str(event["event_id"]): event for event in candidates}
    returned_ids = [str(item.get("event_id")) for item in rankings]
    expected_count = min(top_k, len(by_id))
    if len(returned_ids) != expected_count:
        raise ValueError(
            f"AI ranking response must contain exactly {expected_count} events"
        )
    if len(returned_ids) != len(set(returned_ids)):
        raise ValueError("AI ranking response contained duplicate event IDs")
    unknown_ids = set(returned_ids) - set(by_id)
    if unknown_ids:
        raise ValueError(
            "AI ranking response contained unknown event IDs: "
            + ", ".join(sorted(unknown_ids))
        )

    dimensions = {
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
        scores = item.get("dimensions")
        if not isinstance(scores, dict):
            raise ValueError(f"AI ranking dimensions missing for {event_id}")
        raw_scores = {}
        normalized = {}
        warnings = []
        for name, maximum in dimensions.items():
            if name not in scores:
                raise ValueError(f"AI ranking dimension {name} is missing for {event_id}")
            raw_value = float(scores[name])
            raw_scores[name] = raw_value
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
            "raw_dimensions": raw_scores,
            "normalization_warnings": warnings,
            "destination_worthy": bool(item.get("destination_worthy")),
            "significance_signals": [
                str(signal) for signal in item.get("significance_signals", [])
            ],
            "reason_zh": str(item.get("reason_zh") or ""),
            "reason_en": str(item.get("reason_en") or ""),
        }
        event["social_score"] = calculated_score
        ranked.append(event)
    return sorted(ranked, key=lambda item: item["social_score"], reverse=True)


def estimate_ranking_input_tokens(
    candidates: list[dict[str, Any]], memory: dict[str, Any]
) -> int:
    prompt_text = "\n".join(
        (
            str(build_ranking_prompt()),
            json.dumps(load_persona(), ensure_ascii=False),
            json.dumps(memory, ensure_ascii=False),
            compact_ranking_events(candidates),
        )
    )
    # Byte-level tokenizers cannot produce more content tokens than UTF-8 bytes.
    # Add a fixed allowance for chat-message framing so this remains a safe,
    # offline upper bound even when tiktoken has not learned a new model name.
    return len(prompt_text.encode("utf-8")) + 1024


def invoke_ranking_attempt(
    model: Any,
    candidates: list[dict[str, Any]],
    memory: dict[str, Any],
    attempt: int,
) -> tuple[list[dict[str, Any]] | None, dict[str, Any]]:
    selection_count = min(RANKING_TOP_K, len(candidates))
    result = (build_ranking_prompt() | model).invoke(
        {
            "persona_json": json.dumps(load_persona(), ensure_ascii=False),
            "memory_json": json.dumps(memory, ensure_ascii=False),
            "events_json": compact_ranking_events(candidates),
            "candidate_count": len(candidates),
            "selection_count": selection_count,
        }
    )
    usage = getattr(result, "usage_metadata", None) or getattr(
        result, "response_metadata", {}
    ).get("token_usage", {})
    stage = {
        "stage": "global-top-10",
        "attempt": attempt,
        "candidate_count": len(candidates),
        "requested_count": selection_count,
        "token_usage": usage or {},
    }
    try:
        ranked = parse_ai_rankings(result.content, candidates)
    except (ValueError, TypeError, json.JSONDecodeError) as error:
        stage.update(
            {
                "status": "invalid",
                "validation_error": str(error),
                "response_excerpt": str(result.content)[:2000],
            }
        )
        LOGGER.warning("Ranking attempt %d failed validation: %s", attempt, error)
        return None, stage
    stage["status"] = "valid"
    return ranked, stage


def ai_rank_social_events(
    candidates: list[dict[str, Any]],
    memory: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    from langchain_openai import ChatOpenAI

    if not candidates:
        return [], {
            "model": OPENAI_MODEL,
            "prompt_version": RANKING_PROMPT_VERSION,
            "candidate_count": 0,
            "selected_count": 0,
            "openai_call_count": 0,
            "token_usage": {},
            "status": "skipped_no_candidates",
        }

    estimated_input_tokens = estimate_ranking_input_tokens(candidates, memory)
    if estimated_input_tokens > RANKING_INPUT_TOKEN_BUDGET:
        raise ValueError(
            "Global ranking input exceeds the application safety budget: "
            f"{estimated_input_tokens} > {RANKING_INPUT_TOKEN_BUDGET} tokens"
        )

    model = ChatOpenAI(
        **chat_model_options(get_openai_api_key(), 0.2),
        max_tokens=7000,
    )
    stages = []
    ranked = None
    for attempt in (1, 2):
        ranked, stage = invoke_ranking_attempt(model, candidates, memory, attempt)
        stages.append(stage)
        if ranked is not None:
            break
    if ranked is None:
        LOGGER.error("Global ranking failed twice: %s", stages)
        raise ValueError("AI global top-10 ranking failed validation twice")

    token_keys = ("input_tokens", "output_tokens", "total_tokens")
    token_usage = {
        key: sum(
            int((stage["token_usage"] or {}).get(key, 0) or 0)
            for stage in stages
        )
        for key in token_keys
    }
    return ranked, {
        "model": OPENAI_MODEL,
        "prompt_version": RANKING_PROMPT_VERSION,
        "strategy": "single-global-top-10",
        "openai_call_count": len(stages),
        "token_usage": token_usage,
        "candidate_count": len(candidates),
        "selected_count": len(ranked),
        "estimated_input_token_upper_bound": estimated_input_tokens,
        "input_token_budget": RANKING_INPUT_TOKEN_BUDGET,
        "stages": stages,
        "memory_version": memory.get("memory_version"),
    }


def select_social_events(
    events_data: dict[str, Any],
    history: dict[str, Any],
    now: datetime,
) -> list[dict[str, Any]]:
    return choose_social_events(rank_social_events(events_data, history, now))


def choose_social_events(ranked: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Select freely by final score; distance and uniqueness are soft signals."""
    selected = ranked[:MAX_SOCIAL_EVENTS]
    for event in selected:
        destination_worthy = (event.get("ai_ranking") or {}).get(
            "destination_worthy", False
        )
        event["selection_lane"] = (
            "ai_destination_worthy" if destination_worthy else "ai_semantic_rank"
        )
    return selected


def format_events(events: list[dict[str, Any]]) -> str:
    labels = {0: "今天", 1: "明天", 2: "後天"}
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
                    f"波波評分 {(event.get('ai_ranking') or {}).get('score', 'N/A')}",
                    str((event.get("ai_ranking") or {}).get("reason_zh") or ""),
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
activities and must not introduce facts that appear in only one language. Write
each version as a conversational tiny story from a Boston friend: open with a
small everyday scene, mood, or question, use 3-5 short paragraphs, and weave the
activities into possible ways the day could unfold. Avoid a compressed summary
or a repetitive numbered list. Aim for 350-500 Traditional Chinese characters
and 220-300 English words when enough verified activities are available.

Mention 3-5 activities when available, explain why each fits the day's story,
preserve their source links, and end each body with the weekend-report URL. Use
plain text and raw URLs; do not use Markdown
link syntax because the same copy is published directly to both platforms. Do not
claim that an event is recommended from personal experience. Never use Unicode
emoji. In each language body, naturally place 2-3 varied kaomoji inside sentences
at real emotional turns: delight at a rare find, playful indecision, weather
relief, or a cautious aside when details are incomplete. Treat them like emotional
punctuation, not standalone decorations or paragraph prefixes. Include at most
one conversational parenthetical aside per language. Do not put kaomoji in titles
and do not generate a signature; the application separately preserves Bo's
fixed top expression and fixed signed expression.

The Traditional Chinese body may occasionally use one short Taiwan-style zhuyin
character or playful internet spelling when it arises naturally in the sentence.
This is optional flavor, not a quota: never force it, repeat it mechanically, or
let it reduce clarity. Do not imitate or translate this typography in English.

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


def parse_model_json(
    content: Any, *, sanitize_emoji: bool = False
) -> dict[str, Any]:
    text = str(content).strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text)
    # Preserve raw kaomoji backslashes while making them valid JSON escapes.
    text = re.sub(r'\\(?!["\\/bfnrtu])', r"\\\\", text)
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
        if not sanitize_emoji:
            raise ValueError("Model response contained emoji")
        for language in ("zh", "en"):
            for field in ("title", "body"):
                result[language][field] = EMOJI_PATTERN.sub(
                    "", result[language][field]
                )
        result["hashtags"] = [
            EMOJI_PATTERN.sub("", tag).strip()
            for tag in result["hashtags"]
            if EMOJI_PATTERN.sub("", tag).strip()
        ]
    return result


def extract_contextual_kaomoji(text: str) -> list[str]:
    """Find expressive kaomoji without mistaking ordinary asides for faces."""
    matches: list[str] = []
    for pattern in KAOMOJI_PATTERNS:
        matches.extend(match.group(0).strip() for match in pattern.finditer(text))
    return matches


def validate_contextual_kaomoji(content: dict[str, Any], minimum: int = 2) -> None:
    for language in ("zh", "en"):
        matches = extract_contextual_kaomoji(content[language]["body"])
        if len(matches) < minimum or len(set(matches)) < minimum:
            raise ValueError(
                f"{language} body needs at least {minimum} varied contextual "
                f"kaomoji; found {len(matches)} ({len(set(matches))} unique)"
            )


def build_kaomoji_repair_prompt():
    from langchain_core.prompts import ChatPromptTemplate

    return ChatPromptTemplate.from_messages(
        [
            (
                "system",
                """You are editing one bilingual social post written by 波波 Bo.
Return strict JSON with exactly `zh`, `en`, and `hashtags`. Preserve every fact,
event, date, time, price, venue, URL, hashtag, language, and overall meaning from
the draft. Do not add or remove an event. Do not add a signature or Unicode emoji.

Repair only the conversational voice: each language body must contain 2-3
different kaomoji naturally inside sentences at genuine emotional turns. They
must not be standalone lines, paragraph prefixes, or titles. Vary their shapes;
examples of the range include 〔•̀ᴗ•́〕و, (≧▽≦)ノ, and (((o(*ﾟ▽ﾟ*)o))). Keep
natural Taiwan Traditional Chinese in `zh` and natural English in `en`.""",
            ),
            ("human", "Repair this draft JSON:\n{draft_json}"),
        ]
    )


def generate_content(
    events: list[dict[str, Any]], now: datetime
) -> tuple[dict[str, Any], dict[str, Any]]:
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
    responses = [response]
    try:
        content = parse_model_json(response.content)
        validate_contextual_kaomoji(content)
    except (ValueError, json.JSONDecodeError) as error:
        LOGGER.warning("Retrying social format/voice contract: %s", error)
        repaired = (build_kaomoji_repair_prompt() | model).invoke(
            {"draft_json": str(response.content)}
        )
        responses.append(repaired)
        content = parse_model_json(repaired.content, sanitize_emoji=True)
        validate_contextual_kaomoji(content)

    stages = []
    for index, item in enumerate(responses, start=1):
        usage = getattr(item, "usage_metadata", None) or getattr(
            item, "response_metadata", {}
        ).get("token_usage", {})
        stages.append(
            {
                "stage": "draft" if index == 1 else "kaomoji-repair",
                "token_usage": usage or {},
            }
        )
    token_keys = ("input_tokens", "output_tokens", "total_tokens")
    token_usage = {
        key: sum(
            int((stage["token_usage"] or {}).get(key, 0) or 0)
            for stage in stages
        )
        for key in token_keys
    }
    return content, {
        "model": OPENAI_MODEL,
        "prompt_version": "bobo-social-story-v5-kaomoji-contract",
        "openai_call_count": len(responses),
        "retried": len(responses) > 1,
        "stages": stages,
        "token_usage": token_usage,
    }


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


def render_threads_introduction() -> str:
    """Return Bo's stable bilingual launch post without spending an LLM call."""
    return (
        "嗨，我是波波，一台住在 Boston 雲端地圖裡的黃色探路機器人。"
        "我每天替你尋找大波士頓值得出門的理由：先看 BU 與市區，"
        "也為一年一度、值得專程去的活動多走幾站。"
        "這裡有每日靈感，以及週四先行、週五更新的週末來信。\n\n"
        "完整 Weekly Report：\n"
        f"{WEBSITE_URL}\n\n"
        "—— English ——\n\n"
        "Hi, I'm Bo, a little yellow map robot in the Boston cloud. "
        "I scout things worth doing around Greater Boston—starting near BU, but "
        "traveling farther for rare local traditions. Follow for daily ideas and "
        "a Thursday weekend letter, refreshed Friday.\n\n"
        "— 波波 Bo ⌖ˎˊ˗ 〔•ᴗ•〕ゞ"
    )


def split_threads_text(text: str, limit: int = THREADS_MAX_POST_LENGTH) -> list[str]:
    """Split shared copy on paragraph/word boundaries without changing its text."""
    if limit < 1:
        raise ValueError("Threads post length limit must be positive")
    chunks: list[str] = []
    current = ""

    def append_piece(piece: str, separator: str) -> None:
        nonlocal current
        while piece:
            joiner = separator if current else ""
            available = limit - len(current) - len(joiner)
            if len(piece) <= available:
                current = f"{current}{joiner}{piece}" if current else piece
                return

            # Fill a mostly empty remainder instead of emitting a short post that
            # contains only a link or language heading.
            if current and available >= 80:
                boundary = piece.rfind(" ", 0, available + 1)
                if boundary > 0:
                    current = f"{current}{joiner}{piece[:boundary].rstrip()}"
                    chunks.append(current)
                    current = ""
                    piece = piece[boundary:].lstrip()
                    separator = ""
                    continue

            if current:
                chunks.append(current)
                current = ""
                continue

            boundary = piece.rfind(" ", 0, limit + 1)
            if boundary <= 0:
                boundary = limit
            chunks.append(piece[:boundary].rstrip())
            piece = piece[boundary:].lstrip()
            separator = ""

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


def create_threads_container_with_retry(
    user_id: str,
    token: str,
    text: str,
    reply_to_id: str | None = None,
) -> str:
    """Create a container, allowing a new parent post time to become replyable."""
    attempts = THREADS_REPLY_CREATE_ATTEMPTS if reply_to_id else 1
    if reply_to_id and THREADS_REPLY_SETTLE_SECONDS > 0:
        time.sleep(THREADS_REPLY_SETTLE_SECONDS)

    for attempt in range(1, attempts + 1):
        try:
            return create_threads_container(user_id, token, text, reply_to_id)
        except RuntimeError as error:
            message = str(error)
            transient = any(
                marker in message
                for marker in ("(code 1)", "(code 2)", "(code 500)")
            )
            if not reply_to_id or not transient or attempt >= attempts:
                raise
            delay = THREADS_REPLY_SETTLE_SECONDS * (2 ** (attempt - 1))
            LOGGER.warning(
                "Threads reply container was not ready; retrying in %.1fs "
                "(attempt %s/%s)",
                delay,
                attempt + 1,
                attempts,
            )
            if delay > 0:
                time.sleep(delay)

    raise RuntimeError("Threads reply container retries were exhausted")


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


def publish_threads_text(
    text: str,
    credentials: dict[str, str],
    on_post_published: Any | None = None,
) -> list[str]:
    chunks = split_threads_text(text)
    post_ids: list[str] = []
    root_post_id = None
    for index, chunk in enumerate(chunks, start=1):
        reply_to_id = root_post_id
        try:
            post_id = auto_publish_threads_text_with_retry(
                chunk, credentials, reply_to_id
            )
        except Exception:
            LOGGER.exception(
                "Threads publication failed at chunk %s/%s", index, len(chunks)
            )
            raise
        post_ids.append(post_id)
        if on_post_published:
            on_post_published(post_ids.copy(), len(chunks))
        LOGGER.info("Published Threads chunk %s/%s", index, len(chunks))
        if root_post_id is None:
            root_post_id = post_id
    return post_ids


def auto_publish_threads_text(
    text: str,
    credentials: dict[str, str],
    reply_to_id: str | None = None,
) -> str:
    """Publish one text post atomically using Meta's text-only API option."""
    data = {
        "media_type": "TEXT",
        "text": text,
        "auto_publish_text": "true",
        "access_token": credentials["THREADS_ACCESS_TOKEN"],
    }
    if reply_to_id:
        data["reply_to_id"] = reply_to_id
    response = threads_request_json(
        f"{THREADS_API_BASE}/me/threads",
        method="POST",
        data=data,
    )
    post_id = str(response.get("id", ""))
    if not post_id:
        raise RuntimeError("Threads did not return an auto-published post ID")
    return post_id


def auto_publish_threads_text_with_retry(
    text: str,
    credentials: dict[str, str],
    reply_to_id: str | None = None,
) -> str:
    """Auto-publish text, allowing a new root post time to become replyable."""
    attempts = THREADS_REPLY_CREATE_ATTEMPTS if reply_to_id else 1
    if reply_to_id and THREADS_REPLY_SETTLE_SECONDS > 0:
        time.sleep(THREADS_REPLY_SETTLE_SECONDS)

    for attempt in range(1, attempts + 1):
        try:
            return auto_publish_threads_text(text, credentials, reply_to_id)
        except RuntimeError as error:
            transient = any(
                marker in str(error)
                for marker in ("(code 1)", "(code 2)", "(code 24)", "(code 500)")
            )
            if not reply_to_id or not transient or attempt >= attempts:
                raise
            delay = THREADS_REPLY_SETTLE_SECONDS * (2 ** (attempt - 1))
            LOGGER.warning(
                "Threads reply auto-publish was not ready; retrying in %.1fs "
                "(attempt %s/%s)",
                delay,
                attempt + 1,
                attempts,
            )
            if delay > 0:
                time.sleep(delay)

    raise RuntimeError("Threads reply auto-publish retries were exhausted")


def publication_key(now: datetime) -> str:
    return f"social/publications/threads/{now:%Y-%m-%d}.json"


def handle_introduction(event: dict[str, Any], now: datetime) -> dict[str, Any]:
    text = render_threads_introduction()
    chunks = split_threads_text(text)
    should_publish = bool(event.get("publish"))
    if not should_publish:
        return {
            "success": True,
            "dry_run": True,
            "mode": "introduction",
            "generated_at": now.isoformat(),
            "openai_call_count": 0,
            "shared_text": text,
            "threads_chunks": chunks,
            "threads": {"status": "disabled_dry_run"},
        }
    if not THREADS_PUBLISH_ENABLED:
        raise RuntimeError(
            "Introduction publishing requires THREADS_PUBLISH_ENABLED=true"
        )

    claim = {
        "campaign_id": "bobo-introduction-v1",
        "status": "publishing",
        "updated_at": now.isoformat(),
        "content_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
    }
    write_publication_state(THREADS_INTRODUCTION_KEY, claim, claim=True)
    try:
        credentials = refresh_threads_token_if_needed(get_threads_credentials(), now)
        post_ids = [auto_publish_threads_text(text, credentials)]
    except Exception as error:
        write_publication_state(
            THREADS_INTRODUCTION_KEY,
            {
                **claim,
                "status": "failed",
                "updated_at": datetime.now(EASTERN).isoformat(),
                "error_type": type(error).__name__,
            },
        )
        raise

    published = {
        **claim,
        "status": "published",
        "post_ids": post_ids,
        "username": credentials["THREADS_USERNAME"],
        "updated_at": datetime.now(EASTERN).isoformat(),
    }
    write_publication_state(THREADS_INTRODUCTION_KEY, published)
    return {
        "success": True,
        "mode": "introduction",
        "openai_call_count": 0,
        "threads": {
            "status": "published",
            "post_ids": post_ids,
            "username": credentials["THREADS_USERNAME"],
        },
    }


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
    ranking_metadata: dict[str, Any] | None = None,
    writing_metadata: dict[str, Any] | None = None,
) -> dict[str, str]:
    campaign_id = now.strftime("%Y-%m-%d")
    event_ids = [event["event_id"] for event in events]
    candidate_events = ranked_events or events
    base_rank = {
        event["event_id"]: rank
        for rank, event in enumerate(
            sorted(
                candidate_events,
                key=lambda item: float(
                    item.get("recommendation_score", item.get("quality_score", 0))
                ),
                reverse=True,
            ),
            start=1,
        )
    }
    final_rank = {
        event_id: rank for rank, event_id in enumerate(event_ids, start=1)
    }
    campaign = {
        "campaign_id": campaign_id,
        "generated_at": now.isoformat(),
        "content": content,
        "shared_text": render_shared_text(content, mood_kaomoji),
        "selected_event_ids": event_ids,
        "ranking_policy": {
            "version": RANKING_PROMPT_VERSION,
            "method": "bobo_ai_semantic_ranking",
            "max_events": MAX_SOCIAL_EVENTS,
            "home_base": "Boston University Charles River Campus",
            "fixed_local_quota": False,
        },
        "ranking_run": ranking_metadata or {},
        "writing_run": writing_metadata or {},
        "openai_call_count": (ranking_metadata or {}).get(
            "openai_call_count", 1
        )
        + (writing_metadata or {}).get("openai_call_count", 1),
        "selection_decisions": [
            {
                "event_id": event["event_id"],
                "base_score_rank": base_rank[event["event_id"]],
                "final_score_rank": rank,
                "final_selection_rank": final_rank.get(event["event_id"]),
                "selected": event["event_id"] in event_ids,
                "selection_lane": event.get("selection_lane"),
                "recommendation_score": event.get("recommendation_score"),
                "recommendation": event.get("recommendation"),
                "social_adjustments": event.get("social_adjustments"),
                "social_score": event.get("social_score"),
                "ai_ranking": event.get("ai_ranking"),
            }
            for rank, event in enumerate(candidate_events, start=1)
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


def retry_failed_threads_publication(
    now: datetime,
    publish_key: str,
    existing: dict[str, Any],
) -> dict[str, Any]:
    """Retry an all-or-nothing failed publication using its stored copy."""
    post_ids = existing.get("post_ids") or []
    published_chunk_count = int(existing.get("published_chunk_count") or 0)
    if existing.get("status") != "failed":
        raise RuntimeError("Only a failed Threads publication can be retried")
    if post_ids or published_chunk_count:
        raise RuntimeError(
            "Threads publication already has published chunks; refusing retry"
        )

    campaign = load_json("social/latest.json")
    expected_campaign_id = now.strftime("%Y-%m-%d")
    if campaign.get("campaign_id") != expected_campaign_id:
        raise RuntimeError("Latest social campaign is not today's failed campaign")
    shared_text = str(campaign.get("shared_text") or "")
    if not shared_text:
        raise RuntimeError("Today's social campaign has no stored shared text")
    content_sha256 = hashlib.sha256(shared_text.encode("utf-8")).hexdigest()
    if content_sha256 != existing.get("content_sha256"):
        raise RuntimeError("Stored campaign copy does not match publication state")

    claim = {
        "campaign_id": expected_campaign_id,
        "status": "publishing",
        "updated_at": now.isoformat(),
        "content_sha256": content_sha256,
        "retry_of": existing.get("updated_at"),
    }
    write_publication_state(publish_key, claim)
    published_post_ids: list[str] = []
    chunk_count = len(split_threads_text(shared_text))

    def record_publish_progress(ids: list[str], total: int) -> None:
        published_post_ids[:] = ids
        write_publication_state(
            publish_key,
            {
                **claim,
                "status": "publishing",
                "updated_at": datetime.now(EASTERN).isoformat(),
                "post_ids": ids,
                "published_chunk_count": len(ids),
                "chunk_count": total,
            },
        )

    try:
        credentials = refresh_threads_token_if_needed(
            get_threads_credentials(), now
        )
        post_ids = publish_threads_text(
            shared_text,
            credentials,
            on_post_published=record_publish_progress,
        )
    except Exception as error:
        write_publication_state(
            publish_key,
            {
                **claim,
                "status": "failed",
                "updated_at": datetime.now(EASTERN).isoformat(),
                "error_type": type(error).__name__,
                "error_message": str(error)[:500],
                "post_ids": published_post_ids,
                "published_chunk_count": len(published_post_ids),
                "chunk_count": chunk_count,
            },
        )
        raise

    published = {
        **claim,
        "status": "published",
        "updated_at": datetime.now(EASTERN).isoformat(),
        "post_ids": post_ids,
        "published_chunk_count": len(post_ids),
        "chunk_count": chunk_count,
        "username": credentials["THREADS_USERNAME"],
    }
    write_publication_state(publish_key, published)
    return {
        "success": True,
        "generated_at": published["updated_at"],
        "threads_publish_status": "published_retry",
        "thread_post_ids": post_ids,
    }


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    now = datetime.now(EASTERN)
    if isinstance(event, dict) and event.get("mode") == "introduction":
        return handle_introduction(event, now)

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
            if isinstance(event, dict) and event.get("retry_failed_publication"):
                return retry_failed_threads_publication(
                    now, publish_key, existing
                )
            raise RuntimeError(
                f"Threads publication state is {status}; inspect {publish_key} "
                "before retrying to avoid a duplicate post"
            )

    events_data = load_json("events/latest.json")
    history = load_json("social/history.json", default={})
    memory = load_bobo_memory()
    candidates = rank_social_events(events_data, history, now)
    if not candidates:
        raise RuntimeError("No eligible events remain after the 48-hour cooldown")
    ranked, ranking_metadata = ai_rank_social_events(candidates, memory)
    selected = choose_social_events(ranked)
    if not selected:
        raise RuntimeError("AI ranking returned no selectable events")
    mood_kaomoji = select_kaomoji(selected, now)
    content, writing_metadata = generate_content(selected, now)
    if isinstance(event, dict) and event.get("dry_run"):
        return {
            "success": True,
            "dry_run": True,
            "generated_at": now.isoformat(),
            "openai_call_count": ranking_metadata.get("openai_call_count", 1)
            + writing_metadata.get("openai_call_count", 1),
            "ranking_run": ranking_metadata,
            "writing_run": writing_metadata,
            "selected_event_ids": [item["event_id"] for item in selected],
            "ranked_events": [
                {
                    "event_id": item["event_id"],
                    "name": item.get("name"),
                    "final_rank": rank,
                    "ai_ranking": item.get("ai_ranking"),
                }
                for rank, item in enumerate(ranked, start=1)
            ],
            "shared_text": render_shared_text(content, mood_kaomoji),
            "threads": {"status": "disabled_dry_run"},
        }
    keys = store_campaign(
        content,
        selected,
        history,
        now,
        mood_kaomoji=mood_kaomoji,
        ranked_events=ranked,
        ranking_metadata=ranking_metadata,
        writing_metadata=writing_metadata,
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
        published_post_ids: list[str] = []
        chunk_count = len(split_threads_text(shared_text))

        def record_publish_progress(post_ids: list[str], total: int) -> None:
            published_post_ids[:] = post_ids
            write_publication_state(
                publish_key,
                {
                    **claim,
                    "status": "publishing",
                    "updated_at": datetime.now(EASTERN).isoformat(),
                    "post_ids": post_ids,
                    "published_chunk_count": len(post_ids),
                    "chunk_count": total,
                },
            )

        try:
            credentials = refresh_threads_token_if_needed(
                get_threads_credentials(), now
            )
            post_ids = publish_threads_text(
                shared_text,
                credentials,
                on_post_published=record_publish_progress,
            )
        except Exception as error:
            failed = {
                **claim,
                "status": "failed",
                "updated_at": datetime.now(EASTERN).isoformat(),
                "error_type": type(error).__name__,
                "error_message": str(error)[:500],
                "post_ids": published_post_ids,
                "published_chunk_count": len(published_post_ids),
                "chunk_count": chunk_count,
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
                ranking_metadata=ranking_metadata,
                writing_metadata=writing_metadata,
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
