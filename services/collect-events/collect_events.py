"""Collect upcoming Boston events and persist a normalized snapshot to S3."""

from __future__ import annotations

import json
import hashlib
import logging
import math
import os
import re
import time
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from typing import Any, Callable
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo

import boto3
import requests
from bs4 import BeautifulSoup


LOGGER = logging.getLogger(__name__)
LOGGER.setLevel(os.environ.get("LOG_LEVEL", "INFO").upper())

AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
BUCKET_NAME = os.environ.get("REPORT_BUCKET", "boston-weekend-agent-reports")
DAYS_AHEAD = int(os.environ.get("DAYS_AHEAD", "10"))
MAX_EVENTS_PER_SOURCE = int(os.environ.get("MAX_EVENTS_PER_SOURCE", "10"))
MAX_CITY_EVENTS = int(os.environ.get("MAX_CITY_EVENTS", "30"))
TICKETMASTER_RADIUS_MILES = int(os.environ.get("TICKETMASTER_RADIUS_MILES", "25"))
REQUEST_TIMEOUT_SECONDS = int(os.environ.get("REQUEST_TIMEOUT_SECONDS", "15"))
MAX_RETRIES = int(os.environ.get("MAX_RETRIES", "3"))
VERIFY_TICKET_PAGE_STATUS = os.environ.get(
    "VERIFY_TICKET_PAGE_STATUS", "true"
).lower() in {"1", "true", "yes"}

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

BOSTON_LATITUDE = 42.3601
BOSTON_LONGITUDE = -71.0589
HOME_BASE = {
    "name": "Boston University Charles River Campus",
    "latitude": 42.3505,
    "longitude": -71.1054,
}
NEAR_BU_KEYWORDS = (
    "boston university",
    "kenmore",
    "fenway",
    "allston",
    "brighton",
    "back bay",
    "brookline",
    "commonwealth ave",
    "charles river esplanade",
)
INNER_RING_CITIES = {
    "boston",
    "brookline",
    "cambridge",
    "chelsea",
    "everett",
    "medford",
    "newton",
    "quincy",
    "revere",
    "somerville",
}

# Official, structured community calendars. CivicPlus feeds use the same
# iCalendar format, so additional nearby towns can be added without another
# scraper.
ICAL_SOURCES = (
    {
        "name": "Cambridge Arts",
        "city": "Cambridge",
        "url": "https://www.cambridgema.gov/arts/Calendar.ics",
        "base_url": "https://www.cambridgema.gov",
    },
    {
        "name": "Malden Community",
        "city": "Malden",
        "url": (
            "https://www.cityofmalden.org/common/modules/iCalendar/"
            "iCalendar.aspx?catID=14&feed=calendar"
        ),
        "base_url": "https://www.cityofmalden.org",
    },
    {
        "name": "Natick Community",
        "city": "Natick",
        "url": (
            "https://natickma.gov/common/modules/iCalendar/"
            "iCalendar.aspx?catID=117&feed=calendar"
        ),
        "base_url": "https://natickma.gov",
    },
    {
        "name": "Brookline Community",
        "city": "Brookline",
        "url": (
            "https://www.brooklinema.gov/common/modules/iCalendar/"
            "iCalendar.aspx?catID=107&feed=calendar"
        ),
        "base_url": "https://www.brooklinema.gov",
    },
)

NON_LEISURE_PATTERN = re.compile(
    r"\b(?:board|commission|committee|council|meeting|public hearing|"
    r"town meeting|office hours|caucus|licensing|zoning|planning meeting|"
    r"flu clinic|vaccination clinic|closed|closure)\b",
    re.IGNORECASE,
)

TICKET_PAGE_DOMAINS = ("ticketmaster.com", "ticketweb.com")


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


def _normalized_identity(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


def make_event_id(event: dict[str, Any]) -> str:
    """Create an ID that remains stable when an event time or date changes."""
    link = str(event.get("link") or "").strip().lower().rstrip("/")
    if link:
        identity = f"{event.get('source', '')}|{link}"
    else:
        identity = "|".join(
            (
                str(event.get("source") or ""),
                _normalized_identity(event.get("name")),
                _normalized_identity(event.get("city")),
            )
        )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]


def event_content_hash(event: dict[str, Any]) -> str:
    fields = (
        "name",
        "date",
        "time",
        "location",
        "address",
        "city",
        "category",
        "price",
        "availability_status",
        "description",
        "link",
    )
    canonical = json.dumps(
        {field: event.get(field) for field in fields},
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


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


def distance_miles(
    latitude: float,
    longitude: float,
    base_latitude: float = HOME_BASE["latitude"],
    base_longitude: float = HOME_BASE["longitude"],
) -> float:
    """Calculate straight-line distance from Bo's home base."""
    earth_radius_miles = 3958.8
    lat1, lon1, lat2, lon2 = map(
        math.radians,
        (base_latitude, base_longitude, latitude, longitude),
    )
    delta_latitude = lat2 - lat1
    delta_longitude = lon2 - lon1
    haversine = (
        math.sin(delta_latitude / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin(delta_longitude / 2) ** 2
    )
    return earth_radius_miles * 2 * math.asin(math.sqrt(haversine))


def proximity_component(event: dict[str, Any]) -> tuple[float, float | None, str]:
    searchable = " ".join(
        str(event.get(field) or "")
        for field in ("location", "address", "city")
    ).lower()
    if any(keyword in searchable for keyword in NEAR_BU_KEYWORDS):
        return 30.0, None, "BU-area location"

    try:
        latitude = float(event["latitude"])
        longitude = float(event["longitude"])
    except (KeyError, TypeError, ValueError):
        latitude = longitude = None
    if latitude is not None and longitude is not None:
        miles = distance_miles(latitude, longitude)
        if miles <= 2.5:
            score = 30
        elif miles <= 5:
            score = 26
        elif miles <= 10:
            score = 20
        elif miles <= 15:
            score = 15
        elif miles <= 25:
            score = 10
        else:
            score = 5
        return float(score), round(miles, 1), f"{miles:.1f} miles from BU"

    city = str(event.get("city") or "").lower()
    if city == "boston":
        return 24.0, None, "Boston location"
    if city in {"brookline", "cambridge"}:
        return 22.0, None, "adjacent to Boston/BU"
    if city in INNER_RING_CITIES:
        return 16.0, None, "nearby Greater Boston city"
    if city == "natick":
        return 8.0, None, "outer Greater Boston"
    return 10.0, None, "distance not precisely known"


def calculate_recommendation(event: dict[str, Any], *, today: date | None = None) -> dict[str, Any]:
    """Produce an explainable 100-point score for recommendation analysis."""
    proximity, miles, proximity_reason = proximity_component(event)

    quality_fields = ("name", "date", "time", "location", "link")
    data_quality = sum(3 for field in quality_fields if event.get(field))
    data_quality += 3 if len(str(event.get("description") or "")) > 50 else 0
    data_quality += 2 if event.get("price") else 0
    data_quality = min(float(data_quality), 20.0)

    text = " ".join(
        str(event.get(field) or "")
        for field in ("name", "description", "category")
    ).lower()
    if any(term in text for term in ("festival", "seasonal", "sand sculpt", "parade")):
        interest = 20.0
        interest_reason = "distinctive or seasonal event"
    elif any(
        term in text
        for term in (
            "art",
            "concert",
            "dance",
            "fitness",
            "food",
            "jazz",
            "market",
            "museum",
            "music",
            "outdoor",
            "tour",
        )
    ):
        interest = 15.0
        interest_reason = "strong leisure fit"
    else:
        interest = 9.0
        interest_reason = "general leisure event"

    price_text = str(event.get("price") or "")
    if "free" in price_text.lower():
        affordability = 10.0
    else:
        price_match = re.search(r"\$(\d+(?:\.\d+)?)", price_text)
        if price_match and float(price_match.group(1)) <= 25:
            affordability = 7.0
        elif price_match:
            affordability = 4.0
        else:
            affordability = 5.0

    event_date = parse_event_date(event.get("date"))
    start = today or _now().date()
    offset = (event_date - start).days if event_date else DAYS_AHEAD + 1
    timeliness = 10.0 if offset == 0 else 9.0 if offset == 1 else 8.0 if offset == 2 else 5.0 if offset <= 7 else 3.0

    source = str(event.get("source") or "")
    source_confidence = 10.0 if source != "Ticketmaster" else 8.0
    status = str(event.get("availability_status") or "unknown").lower()
    eligible = status not in {
        "canceled",
        "cancelled",
        "offsale",
        "postponed",
        "rescheduled",
        "sold_out",
    }
    components = {
        "proximity": proximity,
        "data_quality": data_quality,
        "interest": interest,
        "affordability": affordability,
        "timeliness": timeliness,
        "source_confidence": source_confidence,
    }
    total = round(sum(components.values()), 1) if eligible else 0.0
    return {
        "schema_version": "1.0",
        "score": total,
        "eligible": eligible,
        "home_base": HOME_BASE["name"],
        "distance_miles": miles,
        "components": components,
        "reasons": [proximity_reason, interest_reason],
    }


def normalize_ticket_price(minimum: Any, maximum: Any) -> str | None:
    """Treat a synthetic zero range as unknown, not as a free ticket."""
    if minimum is None:
        return None
    if float(minimum) == 0 and maximum is not None and float(maximum) == 0:
        return None
    if maximum is not None:
        return f"${float(minimum):g}-${float(maximum):g}"
    return f"From ${float(minimum):g}"


def detect_ticket_page_status(text: str) -> str | None:
    soup = BeautifulSoup(text, "html.parser")
    for element in soup.select("footer, nav, script, style"):
        element.decompose()
    normalized = clean_text(soup.get_text(" "))
    normalized = normalized or ""
    lower = normalized.lower()
    sold_out_patterns = (
        r"\bsold out every ticket\b",
        r"\btickets? (?:are|is|have been) sold out\b",
        r"\bevent (?:is|has been) sold out\b",
    )
    if "SOLD OUT" in normalized or any(
        re.search(pattern, lower) for pattern in sold_out_patterns
    ):
        return "sold_out"
    if re.search(r"\b(?:event )?cancell?ed\b", lower):
        return "canceled"
    if re.search(r"\b(?:event )?postponed\b", lower):
        return "postponed"
    return None


def fetch_ticket_page_status(url: str | None) -> str | None:
    if not VERIFY_TICKET_PAGE_STATUS or not url:
        return None
    hostname = (urlparse(url).hostname or "").lower()
    if not any(
        hostname == domain or hostname.endswith(f".{domain}")
        for domain in TICKET_PAGE_DOMAINS
    ):
        return None
    try:
        response = requests.get(
            url,
            headers=HEADERS,
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
    except requests.RequestException as error:
        LOGGER.warning(
            "Ticket page status check failed for %s: %s",
            hostname,
            type(error).__name__,
        )
        return None
    return detect_ticket_page_status(response.text)


def encode_geohash(latitude: float, longitude: float, precision: int = 7) -> str:
    """Encode coordinates for Ticketmaster's preferred geoPoint parameter."""
    alphabet = "0123456789bcdefghjkmnpqrstuvwxyz"
    latitude_range = [-90.0, 90.0]
    longitude_range = [-180.0, 180.0]
    bits = (16, 8, 4, 2, 1)
    result: list[str] = []
    value = bit_index = 0
    use_longitude = True

    while len(result) < precision:
        bounds = longitude_range if use_longitude else latitude_range
        coordinate = longitude if use_longitude else latitude
        midpoint = (bounds[0] + bounds[1]) / 2
        if coordinate >= midpoint:
            value |= bits[bit_index]
            bounds[0] = midpoint
        else:
            bounds[1] = midpoint
        use_longitude = not use_longitude
        if bit_index < 4:
            bit_index += 1
        else:
            result.append(alphabet[value])
            bit_index = value = 0
    return "".join(result)


def fetch_ticketmaster_events() -> list[dict[str, Any]]:
    LOGGER.info("Fetching Ticketmaster events")
    api_key = get_api_credentials()["TICKETMASTER_API_KEY"]
    now_utc = datetime.now(timezone.utc)
    response = request_with_retry(
        lambda: requests.get(
            "https://app.ticketmaster.com/discovery/v2/events.json",
            params={
                "apikey": api_key,
                "geoPoint": encode_geohash(BOSTON_LATITUDE, BOSTON_LONGITUDE),
                "radius": TICKETMASTER_RADIUS_MILES,
                "unit": "miles",
                "countryCode": "US",
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
        price = normalize_ticket_price(minimum, maximum)
        link = raw.get("url")
        api_status = str(
            raw.get("dates", {}).get("status", {}).get("code") or "unknown"
        ).lower()
        availability_status = fetch_ticket_page_status(link) or api_status
        events.append(
            {
                "name": clean_text(raw.get("name")),
                "date": raw.get("dates", {}).get("start", {}).get("localDate"),
                "time": raw.get("dates", {}).get("start", {}).get("localTime"),
                "location": venue.get("name") or "Boston, MA",
                "address": venue.get("address", {}).get("line1"),
                "city": venue.get("city", {}).get("name") or "Boston",
                "latitude": venue.get("location", {}).get("latitude"),
                "longitude": venue.get("location", {}).get("longitude"),
                "category": (
                    raw.get("classifications", [{}])[0]
                    .get("segment", {})
                    .get("name", "Event")
                ),
                "price": price,
                "availability_status": availability_status,
                "description": clean_text(raw.get("info")),
                "image_url": (raw.get("images") or [{}])[0].get("url"),
                "source": "Ticketmaster",
                "link": link,
            }
        )
    LOGGER.info("Ticketmaster returned %s events", len(events))
    return events


def _unfold_ical_lines(text: str) -> list[str]:
    """Join RFC 5545 folded lines before parsing individual properties."""
    unfolded: list[str] = []
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if line.startswith((" ", "\t")) and unfolded:
            unfolded[-1] += line[1:]
        else:
            unfolded.append(line)
    return unfolded


def _unescape_ical(value: str | None) -> str | None:
    if value is None:
        return None
    return clean_text(
        value.replace("\\n", " ")
        .replace("\\N", " ")
        .replace("\\,", ",")
        .replace("\\;", ";")
        .replace("\\\\", "\\")
    )


def _parse_ical_start(value: str | None) -> tuple[str | None, str | None]:
    if not value:
        return None, None
    raw = value.strip()
    try:
        if len(raw) == 8:
            parsed = datetime.strptime(raw, "%Y%m%d")
            return parsed.date().isoformat(), None
        if raw.endswith("Z"):
            parsed = datetime.strptime(raw, "%Y%m%dT%H%M%SZ").replace(
                tzinfo=timezone.utc
            )
            local = parsed.astimezone(EASTERN)
        else:
            local = datetime.strptime(raw[:15], "%Y%m%dT%H%M%S").replace(
                tzinfo=EASTERN
            )
        return local.date().isoformat(), local.strftime("%H:%M:%S")
    except ValueError:
        return None, None


def parse_ical_events(
    text: str,
    *,
    source: str,
    city: str,
    base_url: str,
    today: date | None = None,
) -> list[dict[str, Any]]:
    """Normalize events from an official iCalendar feed."""
    events: list[dict[str, Any]] = []
    current: dict[str, str] | None = None
    for line in _unfold_ical_lines(text):
        if line == "BEGIN:VEVENT":
            current = {}
            continue
        if line == "END:VEVENT" and current is not None:
            event_date, event_time = _parse_ical_start(current.get("DTSTART"))
            title = _unescape_ical(current.get("SUMMARY"))
            description = _unescape_ical(current.get("DESCRIPTION"))
            if (
                title
                and is_in_collection_window(event_date, today=today)
                and not NON_LEISURE_PATTERN.search(title)
            ):
                raw_url = current.get("URL") or ""
                description_url = re.search(r"https?://[^\s]+", description or "")
                link = (
                    description_url.group(0).rstrip(".,)")
                    if description_url
                    else urljoin(base_url, raw_url)
                )
                location = _unescape_ical(current.get("LOCATION"))
                location = re.sub(r"^\s*-\s*", "", location or "") or f"{city}, MA"
                events.append(
                    {
                        "name": title,
                        "date": event_date,
                        "time": event_time,
                        "location": location,
                        "address": location,
                        "city": city,
                        "category": "Community",
                        "price": "Free"
                        if re.search(r"\bfree\b", description or "", re.IGNORECASE)
                        else None,
                        "description": description,
                        "image_url": None,
                        "source": source,
                        "link": link,
                    }
                )
            current = None
            if len(events) >= MAX_CITY_EVENTS:
                break
            continue
        if current is None or ":" not in line:
            continue
        key_with_params, value = line.split(":", 1)
        key = key_with_params.split(";", 1)[0]
        if key in {"SUMMARY", "DESCRIPTION", "DTSTART", "LOCATION", "URL"}:
            current[key] = value
    return events


def fetch_ical_events(config: dict[str, str]) -> list[dict[str, Any]]:
    LOGGER.info("Fetching %s events", config["name"])
    response = request_with_retry(
        lambda: requests.get(
            config["url"], headers=HEADERS, timeout=REQUEST_TIMEOUT_SECONDS
        ),
        source=config["name"],
    )
    events = parse_ical_events(
        response.text,
        source=config["name"],
        city=config["city"],
        base_url=config["base_url"],
    )
    LOGGER.info("%s returned %s relevant events", config["name"], len(events))
    return events


def parse_revere_events(
    html: str, *, today: date | None = None
) -> list[dict[str, Any]]:
    """Normalize leisure events from the City of Revere official calendar."""
    soup = BeautifulSoup(html, "html.parser")
    events: list[dict[str, Any]] = []
    for card in soup.select(".CalendarFeed-event"):
        link_node = card.select_one(".u-fontSizeH5 a[href]")
        details = [
            clean_text(node.get_text(" ", strip=True))
            for node in card.select(".Arrange-sizeFill")
        ]
        details = [detail for detail in details if detail]
        if not link_node or len(details) < 2:
            continue
        title = clean_text(link_node.get_text(" ", strip=True))
        event_date = None
        for detail in details:
            parsed_date = parse_event_date(detail)
            if parsed_date:
                event_date = parsed_date.isoformat()
                break
        if (
            not title
            or not is_in_collection_window(event_date, today=today)
            or NON_LEISURE_PATTERN.search(title)
        ):
            continue
        event_time = next((value for value in details if re.search(r"\d:\d{2}", value)), None)
        location = details[-1] if len(details) >= 3 else "Revere, MA"
        events.append(
            {
                "name": title,
                "date": event_date,
                "time": event_time,
                "location": location,
                "address": location,
                "city": "Revere",
                "category": "Community",
                "price": None,
                "description": None,
                "image_url": None,
                "source": "Revere Community",
                "link": urljoin("https://www.revere.org", link_node["href"]),
            }
        )
    return events[:MAX_CITY_EVENTS]


def fetch_revere_events() -> list[dict[str, Any]]:
    LOGGER.info("Fetching City of Revere events")
    response = request_with_retry(
        lambda: requests.get(
            "https://www.revere.org/calendar/category/events",
            headers=HEADERS,
            timeout=REQUEST_TIMEOUT_SECONDS,
        ),
        source="Revere Community",
    )
    events = parse_revere_events(response.text)
    LOGGER.info("Revere Community returned %s relevant events", len(events))
    return events


def parse_quincy_events(
    html: str, *, today: date | None = None
) -> list[dict[str, Any]]:
    """Normalize Discover Quincy, the city's visitor event calendar."""
    soup = BeautifulSoup(html, "html.parser")
    events: list[dict[str, Any]] = []
    reference_date = today or _now().date()
    for card in soup.select("article.mec-event-article"):
        top_section = card.find("div", class_="mec-topsec", recursive=False)
        if top_section is None:
            continue
        link_node = top_section.select_one(".mec-event-title a[href]")
        date_node = top_section.select_one(".mec-start-date-label")
        if not link_node or not date_node:
            continue
        title = clean_text(link_node.get_text(" ", strip=True))
        date_label = clean_text(date_node.get_text(" ", strip=True))
        event_date = None
        if date_label:
            for year in (reference_date.year, reference_date.year + 1):
                try:
                    candidate = datetime.strptime(
                        f"{date_label} {year}", "%d %b %Y"
                    ).date()
                    if candidate >= reference_date - timedelta(days=7):
                        event_date = candidate.isoformat()
                        break
                except ValueError:
                    continue
        if (
            not title
            or title.startswith("Item detailsDate Name")
            or not is_in_collection_window(event_date, today=reference_date)
            or NON_LEISURE_PATTERN.search(title)
        ):
            continue

        start_time = top_section.select_one(".mec-start-time")
        end_time = top_section.select_one(".mec-end-time")
        time_parts = [
            clean_text(node.get_text(" ", strip=True))
            for node in (start_time, end_time)
            if node
        ]
        time_parts = normalize_time_range(time_parts)
        venue_node = top_section.select_one(".mec-venue-details > span")
        address_node = top_section.select_one(".mec-event-address")
        description_node = top_section.select_one(".mec-event-description")
        image_node = top_section.select_one(".mec-event-image img[src]")
        venue = clean_text(venue_node.get_text(" ", strip=True)) if venue_node else None
        address = (
            clean_text(address_node.get_text(" ", strip=True))
            if address_node
            else None
        )
        description = (
            clean_text(description_node.get_text(" ", strip=True))
            if description_node
            else None
        )
        events.append(
            {
                "name": title,
                "date": event_date,
                "time": " - ".join(time_parts) or None,
                "location": venue or "Quincy, MA",
                "address": address,
                "city": "Quincy",
                "category": "Community",
                "price": "Free"
                if re.search(r"\bfree\b", description or "", re.IGNORECASE)
                else None,
                "description": description,
                "image_url": image_node["src"] if image_node else None,
                "source": "Discover Quincy",
                "link": link_node["href"],
            }
        )
    return events[:MAX_CITY_EVENTS]


def normalize_time_range(parts: list[str]) -> list[str]:
    """Repair an implausible AM/PM marker emitted by some calendar list views."""
    if len(parts) != 2:
        return parts
    parsed = []
    for value in parts:
        match = re.fullmatch(r"(\d{1,2}):(\d{2})\s*([ap]m)", value, re.IGNORECASE)
        if not match:
            return parts
        hour = int(match.group(1)) % 12
        if match.group(3).lower() == "pm":
            hour += 12
        parsed.append(hour * 60 + int(match.group(2)))
    duration = parsed[1] - parsed[0]
    if duration < 0:
        duration += 24 * 60
    if duration > 14 * 60 and parts[0].lower().endswith("am") and parts[1].lower().endswith("pm"):
        corrected = re.sub(r"am$", "pm", parts[0], flags=re.IGNORECASE)
        return [corrected, parts[1]]
    return parts


def fetch_quincy_events() -> list[dict[str, Any]]:
    LOGGER.info("Fetching Discover Quincy events")
    response = request_with_retry(
        lambda: requests.get(
            "https://discoverquincy.com/event-calendar/",
            headers=HEADERS,
            timeout=REQUEST_TIMEOUT_SECONDS,
        ),
        source="Discover Quincy",
    )
    events = parse_quincy_events(response.text)
    LOGGER.info("Discover Quincy returned %s relevant events", len(events))
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
                "city": "Boston",
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
        if len(events) >= MAX_CITY_EVENTS:
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
    unique: dict[tuple[str, str], dict[str, Any]] = {}
    for event in events:
        if NON_LEISURE_PATTERN.search(event.get("name") or ""):
            continue
        event["quality_score"] = calculate_event_score(event)
        event["recommendation"] = calculate_recommendation(event)
        event["recommendation_score"] = event["recommendation"]["score"]
        key = (
            (event.get("name") or "").lower().strip(),
            event.get("date") or "unknown",
        )
        if key not in unique or event["recommendation_score"] > unique[key]["recommendation_score"]:
            unique[key] = event
    ranked = sorted(
        (event for event in unique.values() if event["quality_score"] >= 4),
        key=lambda event: (
            event.get("date") or "9999-12-31",
            -event["recommendation_score"],
        ),
    )
    for event in ranked:
        event["event_id"] = make_event_id(event)
        event["content_hash"] = event_content_hash(event)
    return ranked


def build_change_set(
    previous: dict[str, Any] | None,
    current: dict[str, Any],
) -> dict[str, Any]:
    """Compare snapshots without treating one missing scrape as a cancellation."""
    previous_events = {
        event.get("event_id") or make_event_id(event): event
        for event in (previous or {}).get("events", [])
    }
    current_events = {
        event.get("event_id") or make_event_id(event): event
        for event in current.get("events", [])
    }
    tracked_fields = (
        "name",
        "date",
        "time",
        "location",
        "address",
        "price",
        "availability_status",
        "description",
        "link",
    )

    new_events = [
        current_events[event_id]
        for event_id in current_events.keys() - previous_events.keys()
    ]
    updated_events = []
    for event_id in current_events.keys() & previous_events.keys():
        before = previous_events[event_id]
        after = current_events[event_id]
        if (before.get("content_hash") or event_content_hash(before)) == after.get(
            "content_hash"
        ):
            continue
        changed_fields = {
            field: {"before": before.get(field), "after": after.get(field)}
            for field in tracked_fields
            if before.get(field) != after.get(field)
        }
        updated_events.append(
            {
                "event_id": event_id,
                "name": after.get("name"),
                "date": after.get("date"),
                "changes": changed_fields,
            }
        )

    missing_events = [
        {
            "event_id": event_id,
            "name": previous_events[event_id].get("name"),
            "date": previous_events[event_id].get("date"),
            "source": previous_events[event_id].get("source"),
            "status": "unconfirmed_missing",
        }
        for event_id in previous_events.keys() - current_events.keys()
    ]
    return {
        "generated_at": current.get("timestamp"),
        "baseline_timestamp": (previous or {}).get("timestamp"),
        "new": sorted(new_events, key=lambda item: item.get("date") or ""),
        "updated": sorted(updated_events, key=lambda item: item.get("date") or ""),
        "missing": sorted(missing_events, key=lambda item: item.get("date") or ""),
        "counts": {
            "new": len(new_events),
            "updated": len(updated_events),
            "missing": len(missing_events),
        },
    }


def collect_events() -> dict[str, Any]:
    sources: list[tuple[str, Callable[[], list[dict[str, Any]]]]] = [
        ("Ticketmaster", fetch_ticketmaster_events),
        ("Boston.gov", fetch_boston_gov_events),
        ("Revere Community", fetch_revere_events),
        ("Discover Quincy", fetch_quincy_events),
    ]
    for config in ICAL_SOURCES:
        sources.append(
            (
                config["name"],
                lambda config=config: fetch_ical_events(config),
            )
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


def load_previous_snapshot() -> dict[str, Any] | None:
    try:
        response = S3.get_object(Bucket=BUCKET_NAME, Key="events/latest.json")
    except S3.exceptions.NoSuchKey:
        return None
    except Exception as error:
        if getattr(error, "response", {}).get("Error", {}).get("Code") in {
            "NoSuchKey",
            "404",
        }:
            return None
        raise
    return json.loads(response["Body"].read().decode("utf-8"))


def write_snapshot(
    snapshot: dict[str, Any], changes: dict[str, Any]
) -> dict[str, str]:
    now = _now()
    body = json.dumps(snapshot, ensure_ascii=False, indent=2).encode("utf-8")
    timestamped_key = (
        f"events/{now:%Y-%m}/events_{now:%Y%m%d_%H%M%S_%f}.json"
    )
    latest_key = "events/latest.json"
    for key in (timestamped_key, latest_key):
        S3.put_object(
            Bucket=BUCKET_NAME,
            Key=key,
            Body=body,
            ContentType="application/json",
        )
    changes_key = "events/changes/latest.json"
    changes_timestamped_key = (
        f"events/changes/{now:%Y-%m}/changes_{now:%Y%m%d_%H%M%S_%f}.json"
    )
    changes_body = json.dumps(changes, ensure_ascii=False, indent=2).encode("utf-8")
    for key in (changes_key, changes_timestamped_key):
        S3.put_object(
            Bucket=BUCKET_NAME,
            Key=key,
            Body=changes_body,
            ContentType="application/json",
        )
    return {
        "latest": latest_key,
        "timestamped": timestamped_key,
        "changes": changes_key,
        "changes_timestamped": changes_timestamped_key,
    }


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    started = time.monotonic()
    previous = load_previous_snapshot()
    snapshot = collect_events()
    changes = build_change_set(previous, snapshot)
    keys = write_snapshot(snapshot, changes)
    result = {
        "success": True,
        "bucket": BUCKET_NAME,
        "s3_keys": keys,
        "event_count": snapshot["total_events"],
        "source_counts": snapshot["summary"]["by_source"],
        "source_failures": snapshot["summary"]["failures"],
        "change_counts": changes["counts"],
        "duration_seconds": round(time.monotonic() - started, 2),
    }
    LOGGER.info("Collection complete: %s", json.dumps(result))
    return result


if __name__ == "__main__":
    print(json.dumps(lambda_handler({}, None), indent=2))
