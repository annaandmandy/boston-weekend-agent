# Meet Boston ingestion bridge

Meet Boston's public RSS and event detail pages are reachable from a GitHub-hosted
runner but currently return HTTP 403 from the collector Lambda. The bridge keeps
the transformation deterministic and moves only the network fetch outside AWS:

```text
GitHub Actions -> Meet Boston RSS/detail pages -> S3 staging -> collector Lambda
```

No long-lived AWS access key is stored in GitHub. The workflow uses GitHub's OIDC
token to assume a role that can only put objects below
`ingestion/meet-boston/` in the report bucket.

## One-time AWS console setup

1. Open **IAM → Identity providers**.
2. If `token.actions.githubusercontent.com` is not present, add an OpenID Connect
   provider with:
   - Provider URL: `https://token.actions.githubusercontent.com`
   - Audience: `sts.amazonaws.com`
3. Create a role named `boston-weekend-meet-boston-staging-write` for Web
   identity and use
   `infrastructure/iam/github-meet-boston-trust-policy.json` as its trust policy.
4. Add an inline permissions policy using
   `infrastructure/iam/github-meet-boston-s3-policy.json`.
5. Copy the role ARN.
6. In GitHub, open **Settings → Secrets and variables → Actions → Variables** and
   create `MEET_BOSTON_INGEST_ROLE_ARN` with that ARN.

The trust policy accepts only the repository's immutable GitHub identity and its
`main` branch. The permissions policy cannot read, delete, or overwrite objects
outside the Meet Boston staging prefix.

## Schedule and activation

The workflow runs at 5:00 AM `America/New_York`, one hour before the 6:00 AM AWS
collector schedule. GitHub cron expressions use UTC, so the workflow declares
both 09:00 and 10:00 UTC and uses a local-time gate to run only the matching one.
Manual `workflow_dispatch` runs always bypass the gate.

Before enabling this schedule, the full manual path was verified: GitHub assumed
the AWS role through OIDC, wrote both the immutable archive and `latest.json`, and
the deployed collector loaded 30 Meet Boston events from staging.

The collector accepts staging data only when it has schema version `1`, identifies
the source as `Meet Boston`, contains usable events, and is no more than 30 hours
old. It falls back to direct collection if any check fails.
