#!/usr/bin/env python3
"""Fetch Meet Boston outside AWS and publish a normalized staging snapshot."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import boto3


ROOT = Path(__file__).resolve().parents[1]
COLLECTOR_PATH = ROOT / "services" / "collect-events" / "collect_events.py"


def load_collector():
    spec = importlib.util.spec_from_file_location(
        "meet_boston_ingestion_collector", COLLECTOR_PATH
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load collector from {COLLECTOR_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def build_payload(events: list[dict], fetched_at: datetime) -> dict:
    return {
        "schema_version": 1,
        "source": "Meet Boston",
        "fetched_at": fetched_at.astimezone(timezone.utc).isoformat(),
        "event_count": len(events),
        "events": events,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--bucket",
        default=os.environ.get("REPORT_BUCKET", "boston-weekend-agent-reports"),
    )
    parser.add_argument(
        "--prefix",
        default="ingestion/meet-boston",
    )
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "us-east-1"))
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch and validate without writing to S3.",
    )
    args = parser.parse_args()

    collector = load_collector()
    events = collector.fetch_meet_boston_direct_events()
    if not events:
        raise RuntimeError("Meet Boston returned no usable events")

    fetched_at = datetime.now(timezone.utc)
    payload = build_payload(events, fetched_at)
    body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    latest_key = f"{args.prefix.rstrip('/')}/latest.json"
    archive_key = (
        f"{args.prefix.rstrip('/')}/archive/{fetched_at:%Y/%m}/"
        f"meet_boston_{fetched_at:%Y%m%d_%H%M%S}.json"
    )

    if not args.dry_run:
        s3 = boto3.client("s3", region_name=args.region)
        for key in (archive_key, latest_key):
            s3.put_object(
                Bucket=args.bucket,
                Key=key,
                Body=body,
                ContentType="application/json; charset=utf-8",
            )

    print(
        json.dumps(
            {
                "success": True,
                "dry_run": args.dry_run,
                "event_count": len(events),
                "bucket": args.bucket,
                "latest_key": latest_key,
                "archive_key": archive_key,
                "fetched_at": payload["fetched_at"],
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
