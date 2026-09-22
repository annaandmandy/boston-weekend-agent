#!/usr/bin/env python3
"""Probe Meet Boston from a CI runner without changing production data."""

from __future__ import annotations

import importlib.util
import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import requests


ROOT = Path(__file__).resolve().parents[1]
COLLECTOR_PATH = ROOT / "services" / "collect-events" / "collect_events.py"


def load_collector():
    spec = importlib.util.spec_from_file_location("meet_boston_probe_collector", COLLECTOR_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load collector from {COLLECTOR_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def response_summary(response: requests.Response) -> dict[str, Any]:
    return {
        "status": response.status_code,
        "content_type": response.headers.get("content-type"),
        "bytes": len(response.content),
        "akamai_grn": response.headers.get("akamai-grn"),
        "x_sv_edge": response.headers.get("x-sv-edge"),
    }


def main() -> int:
    collector = load_collector()
    session = requests.Session()
    session.headers.update(collector.MEET_BOSTON_HEADERS)
    timeout = collector.REQUEST_TIMEOUT_SECONDS
    results: dict[str, Any] = {}

    events_page = session.get(
        "https://www.meetboston.com/events/",
        timeout=timeout,
    )
    results["events_page"] = response_summary(events_page)

    rss = session.get(
        "https://www.meetboston.com/event/rss/",
        params=collector.build_meet_boston_rss_params(),
        timeout=timeout,
    )
    results["filtered_rss"] = response_summary(rss)

    item_count = 0
    detail_link = None
    if rss.ok:
        root = ET.fromstring(rss.text)
        items = root.findall("./channel/item")
        item_count = len(items)
        detail_link = items[0].findtext("link") if items else None
    results["filtered_rss"]["items"] = item_count

    if detail_link:
        detail = session.get(detail_link, timeout=timeout)
        results["first_detail"] = {
            **response_summary(detail),
            "url": detail_link,
            "has_event_json_ld": '"@type": "Event"' in detail.text
            or '"@type":"Event"' in detail.text,
        }
    else:
        results["first_detail"] = {"status": None, "url": None}

    print(json.dumps(results, indent=2, sort_keys=True))

    successful = (
        events_page.ok
        and rss.ok
        and item_count > 0
        and results["first_detail"].get("status") == 200
    )
    return 0 if successful else 1


if __name__ == "__main__":
    raise SystemExit(main())
