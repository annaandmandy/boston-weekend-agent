# Content schedule

All recurring times use `America/New_York`, so daylight-saving changes do not
shift the local publishing time.

| Local time | Workflow | Output |
| --- | --- | --- |
| Daily 06:00 | Event collector | Today through the next ten days in `events/latest.json` |
| Daily 07:00 | Daily social generator | One shared Threads/Xiaohongshu draft in `social/latest.json` and `.txt` |
| Thursday 07:15 | Full weekend workflow | Early Friday-Sunday report with weather |
| Friday 07:15 | Full weekend workflow | Revised report with fresh listings and weather |

The website continues to read `reports/weekend_summary.txt`. Each report is also
archived under `reports/archive/YYYY/MM/` with its edition name.

The Thursday run also stores `reports/baselines/weekend_YYYY-MM-DD.json`. The
Friday run compares against that Thursday baseline rather than the immediately
preceding daily collection, so its new and updated items reflect the actual
published preview.

Each report also creates an immutable analytics run containing the exact S3
object versions supplied to the LLM and a structured lineage manifest. See
[`analytics-history.md`](analytics-history.md).

## Change handling

The collector compares each run with the previous snapshot and writes
`events/changes/latest.json`:

- `new`: a stable event ID was not present in the previous snapshot;
- `updated`: a tracked field such as time, location, price, or description changed;
- `missing`: a listing disappeared once.

A missing listing is deliberately labelled `unconfirmed_missing`; one failed
provider response or removed search result is not evidence that an event was
cancelled. The report prompt is prohibited from calling it cancelled without an
explicit source status.

## Social cooldown

The social generator selects events occurring today through two days ahead and
excludes anything selected during the previous 48 hours. The generated title,
body, hashtags, links, and event selection are shared by both platforms.

Until the platform connections are completed:

- Threads status is `ready` in the campaign file.
- Xiaohongshu status is `manual_draft` because no general creator-note publish
  API is configured.

The history is stored at `social/history.json`. This is sufficient for one
scheduled writer; if multiple concurrent publishers are added later, migrate
the cooldown records to DynamoDB conditional writes.

## Failure handling

Each EventBridge Scheduler target retries transient delivery failures, then
sends an exhausted event to `boston-weekend-scheduler-dlq`. Messages are kept
for 14 days for diagnosis and replay. CloudWatch sends an alert through the
`boston-weekend-alerts` SNS topic when the DLQ has a visible message, a scheduled
Lambda reports an error, or the weekend Step Functions execution fails.

These controls intentionally cover two different failure stages: the DLQ
protects delivery from Scheduler to its target, while the alarms monitor work
that started successfully but failed during processing.
