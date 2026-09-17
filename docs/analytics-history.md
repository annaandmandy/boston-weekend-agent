# Analytics history and data lineage

The operational `latest` objects remain convenient inputs for scheduled
workflows, while immutable analytics objects preserve exactly what each report
used.

## Report-run layout

Every report invocation creates one run directory:

```text
analytics/report_runs/
  year=2026/month=09/day=18/run_id=20260918T071500123456-0400/
    events.json
    event_changes.json
    weather.json
    weather_summary.json
    effective_event_changes.json
    thursday_baseline.json       # Friday only, when available
    report.txt
    report.json
```

The first four objects are S3 server-side copies. The Lambda first reads each
source object's `VersionId` and ETag, then copies that exact version into the
run. Report generation reads the archived copies rather than mutable `latest`
keys.

`effective_event_changes.json` is the change set actually supplied to the LLM.
On Friday this is recalculated against the archived Thursday baseline.

## Manifest

`report.json` is the lineage manifest. It records:

- schema version and run ID;
- generation time and edition;
- OpenAI model and prompt version;
- token usage when returned by the model;
- Lambda request ID;
- source keys, source VersionIds, ETags, and archived keys;
- derived change and baseline objects;
- website, timestamped, archive, and analytics report outputs;
- the generated report text.

## Other immutable history

- Event snapshots: `events/YYYY-MM/events_YYYYMMDD_HHMMSS_microseconds.json`
- Event changes:
  `events/changes/YYYY-MM/changes_YYYYMMDD_HHMMSS_microseconds.json`
- Social campaigns:
  `social/campaigns/YYYY/MM/YYYY-MM-DD_HHMMSS_microseconds.json`
- Text reports: `reports/YYYY-MM/report_YYYYMMDD_HHMMSS.txt`

S3 Versioning provides a second recovery layer for mutable operational keys,
but immutable timestamped objects remain the primary analytics record.

## Next analytics layer

The raw snapshots deliberately preserve source fidelity. A later transform can
write one event observation per row as JSON Lines or Parquet under a separate
`analytics/event_observations/` prefix. Glue and Athena should query that
flattened layer rather than repeatedly unnesting the raw event arrays.
