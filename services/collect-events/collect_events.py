"""Collect upcoming Boston events and persist a normalized snapshot to S3."""

from __future__ import annotations

import json
import logging
import os
import re
import time
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from typing import Any, Callable
from zoneinfo import ZoneInfo

import boto3
import requests
from bs4 import BeautifulSoup


LOGGER = logging.getLogger(__name__)
LOGGER.setLevel(os.environ.get("LOG_LEVEL", "INFO").upper())

AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
BUCKET_NAME = os.environ.get("REPORT_BUCKET", "boston-weekend-agent-reports")
DAYS_AHEAD = int(os.environ.get("DAYS_AHEAD", "3"))
MAX_EVENTS_PER_SOURCE = int(os.environ.get("MAX_EVENTS_PER_SOURCE", "10"))
REQUEST_TIMEOUT_SECONDS = int(os.environ.get("REQUEST_TIMEOUT_SECONDS", "15"))
MAX_RETRIES = int(os.environ.get("MAX_RETRIES", "3"))

EASTERN = ZoneInfo("America/New_York")
S3 = boto3.client("s3", region_name=AWS_REGION)
SECRETS = boto3.client("secretsmanager", region_name=AWS_REGION)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


def _now() -> datetime:
    return datetime.now(EASTERN)


@lru_cache(maxsize=1)
def get_api_credentials() -> dict[str, str]:
    """Load event-provider credentials without placing values in Lambda config."""
    secret_id = os.environ.get("EVENTS_SECRET_ID")
    if secret_id:
        response = SECRETS.get_secret_value(SecretId=secret_id)
        values = json.loads(response["SecretString"])
    else:
        # Local development only; production should always configure EVENTS_SECRET_ID.
        values = {
            "TICKETMASTER_API_KEY": os.environ.get("TICKETMASTER_API_KEY", ""),
        }

    missing = [
        key
        for key in ("TICKETMASTER_API_KEY",)
        if not values.get(key)
    ]
    if missing:
        raise RuntimeError(f"Missing event API credentials: {', '.join(missing)}")
    return values


def request_with_retry(
    request: Callable[[], requests.Response], *, source: str
) -> requests.Response:
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = request()
            response.raise_for_status()
            return response
        except requests.RequestException:
            if attempt == MAX_RETRIES:
                raise
            delay = 2 ** (attempt - 1)
            LOGGER.warning("%s request failed; retrying in %ss", source, delay)
            time.sleep(delay)
    raise RuntimeError(f"{source} request failed")


def clean_text(value: Any) -> str | None:
    if value is None:
        return None
    cleaned = " ".join(str(value).split()).strip()
    return cleaned or None


def parse_event_date(value: str | None) -> date | None:
    if not value:
        return None
    normalized = value.split("T", 1)[0]
    for pattern in ("%Y-%m-%d", "%m/%d/%Y", "%B %d, %Y"):
        try:
            return datetime.strptime(normalized, pattern).date()
        except ValueError:
            continue
    return None


def is_in_collection_window(value: str | None, *, today: date | None = None) -> bool:
    event_date = parse_event_date(value)
    if event_date is None:
        return False
    start = today or _now().date()
    return start <= event_date <= start + timedelta(days=DAYS_AHEAD)


def calculate_event_score(event: dict[str, Any]) -> float:
    score = 0.0
    score += 2 if event.get("name") else 0
    score += 2 if parse_event_date(event.get("date")) else 0
    score += 1 if event.get("location") else 0
    score += 1 if event.get("time") else 0
    score += 1 if len(event.get("description") or "") > 50 else 0
    score += 1 if event.get("price") else 0
    score += 1 if event.get("link") else 0
    score += 0.5 if event.get("image_url") else 0
    score += 0.5 if "free" in str(event.get("price") or "").lower() else 0
    return score


def fetch_ticketmaster_events() -> list[dict[str, Any]]:
    LOGGER.info("Fetching Ticketmaster events")
    api_key = get_api_credentials()["TICKETMASTER_API_KEY"]
    now_utc = datetime.now(timezone.utc)
    response = request_with_retry(
        lambda: requests.get(
            "https://app.ticketmaster.com/discovery/v2/events.json",
            params={
                "apikey": api_key,
                "city": "Boston",
                "stateCode": "MA",
                "size": MAX_EVENTS_PER_SOURCE,
                "sort": "date,asc",
                "startDateTime": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "endDateTime": (now_utc + timedelta(days=DAYS_AHEAD)).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"
                ),
            },
            timeout=REQUEST_TIMEOUT_SECONDS,
        ),
        source="Ticketmaster",
    )

    events = []
    for raw in response.json().get("_embedded", {}).get("events", []):
        venue = raw.get("_embedded", {}).get("venues", [{}])[0]
        prices = (raw.get("priceRanges") or [{}])[0]
        minimum, maximum = prices.get("min"), prices.get("max")
        price = None
        if minimum is not None and maximum is not None:
            price = f"${minimum:g}-${maximum:g}"
        elif minimum is not None:
            price = f"From ${minimum:g}"
        events.append(
            {
                "name": clean_text(raw.get("name")),
                "date": raw.get("dates", {}).get("start", {}).get("localDate"),
                "time": raw.get("dates", {}).get("start", {}).get("localTime"),
                "location": venue.get("name") or "Boston, MA",
                "address": venue.get("address", {}).get("line1"),
                "category": (
                    raw.get("classifications", [{}])[0]
                    .get("segment", {})
                    .get("name", "Event")
                ),
                "price": price,
                "description": clean_text(raw.get("info")),
                "image_url": (raw.get("images") or [{}])[0].get("url"),
                "source": "Ticketmaster",
                "link": raw.get("url"),
            }
        )
    LOGGER.info("Ticketmaster returned %s events", len(events))
    return events


def parse_boston_gov_rss(
    xml_text: str, *, today: date | None = None
) -> list[dict[str, Any]]:
    """Normalize City of Boston's official public events RSS feed."""
    root = ET.fromstring(xml_text)
    events = []
    for item in root.findall("./channel/item"):
        title = clean_text(item.findtext("title"))
        link = clean_text(item.findtext("link"))
        description_html = item.findtext("description") or ""
        soup = BeautifulSoup(description_html, "html.parser")
        start = soup.select_one("time[datetime]")
        start_value = start.get("datetime", "") if start else ""
        event_date = start_value.split("T", 1)[0] if "T" in start_value else None
        start_text = clean_text(start.get_text()) if start else None
        event_time = start_text.rsplit(" - ", 1)[-1] if start_text else None
        if not is_in_collection_window(event_date, today=today):
            continue

        address_node = soup.select_one("p.address")
        address = (
            clean_text(address_node.get_text(" ", strip=True))
            if address_node
            else None
        )
        location_node = soup.select_one(".address-line1")
        location = (
            clean_text(location_node.get_text())
            if location_node
            else "Boston, MA"
        )
        paragraphs = []
        for paragraph in soup.find_all("p"):
            text = clean_text(paragraph.get_text(" ", strip=True))
            if not text or "address" in paragraph.get("class", []):
                continue
            if text.startswith(("Event Date:", "Address:", "Contact Department:", "Publish Date:")):
                continue
            paragraphs.append(text)

        events.append(
            {
                "name": title,
                "date": event_date,
                "time": event_time,
                "location": location,
                "address": address,
                "category": "Community",
                "price": "Free"
                if re.search(r"\bfree\b", description_html, re.IGNORECASE)
                else None,
                "description": clean_text(" ".join(paragraphs)),
                "image_url": None,
                "source": "Boston.gov",
                "link": link,
            }
        )
        if len(events) >= MAX_EVENTS_PER_SOURCE:
            break
    return events


def fetch_boston_gov_events() -> list[dict[str, Any]]:
    LOGGER.info("Fetching City of Boston events")
    response = request_with_retry(
        lambda: requests.get(
            "https://www.boston.gov/rss/events",
            headers=HEADERS,
            timeout=REQUEST_TIMEOUT_SECONDS,
        ),
        source="Boston.gov",
    )
    events = parse_boston_gov_rss(response.text)
    LOGGER.info("Boston.gov returned %s relevant events", len(events))
    return events


def deduplicate_and_rank(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    unique: dict[tuple[str, str, str], dict[str, Any]] = {}
    for event in events:
        event["quality_score"] = calculate_event_score(event)
        key = (
            (event.get("name") or "").lower().strip(),
            event.get("date") or "unknown",
            (event.get("location") or "").lower().strip()[:40],
        )
        if key not in unique or event["quality_score"] > unique[key]["quality_score"]:
            unique[key] = event
    return sorted(
        (event for event in unique.values() if event["quality_score"] >= 4),
        key=lambda event: (event.get("date") or "9999-12-31", -event["quality_score"]),
    )


def collect_events() -> dict[str, Any]:
    sources = (
        ("Ticketmaster", fetch_ticketmaster_events),
        ("Boston.gov", fetch_boston_gov_events),
    )
    collected = []
    failures = []
    for name, fetcher in sources:
        try:
            collected.extend(fetcher())
        except Exception as error:  # Keep one provider outage from killing the report.
            LOGGER.exception("%s collection failed", name)
            failures.append({"source": name, "error": type(error).__name__})

    events = deduplicate_and_rank(collected)
    if not events:
        raise RuntimeError("No usable events were collected from any source")

    source_counts: dict[str, int] = {}
    for event in events:
        source = event.get("source", "Unknown")
        source_counts[source] = source_counts.get(source, 0) + 1

    return {
        "timestamp": _now().isoformat(),
        "days_ahead": DAYS_AHEAD,
        "total_events": len(events),
        "events": events,
        "summary": {"by_source": source_counts, "failures": failures},
    }


def write_snapshot(snapshot: dict[str, Any]) -> dict[str, str]:
    now = _now()
    body = json.dumps(snapshot, ensure_ascii=False, indent=2).encode("utf-8")
    timestamped_key = (
        f"events/{now:%Y-%m}/events_{now:%Y%m%d_%H%M%S}.json"
    )
    latest_key = "events/latest.json"
    for key in (timestamped_key, latest_key):
        S3.put_object(
            Bucket=BUCKET_NAME,
            Key=key,
            Body=body,
            ContentType="application/json",
        )
    return {"latest": latest_key, "timestamped": timestamped_key}


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    started = time.monotonic()
    snapshot = collect_events()
    keys = write_snapshot(snapshot)
    result = {
        "success": True,
        "bucket": BUCKET_NAME,
        "s3_keys": keys,
        "event_count": snapshot["total_events"],
        "source_counts": snapshot["summary"]["by_source"],
        "source_failures": snapshot["summary"]["failures"],
        "duration_seconds": round(time.monotonic() - started, 2),
    }
    LOGGER.info("Collection complete: %s", json.dumps(result))
    return result


if __name__ == "__main__":
    print(json.dumps(lambda_handler({}, None), indent=2))
