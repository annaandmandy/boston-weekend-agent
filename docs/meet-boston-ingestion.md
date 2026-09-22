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
3. Create a role named `boston-weekend-github-meet-boston-ingestion` for Web
   identity and use
   `infrastructure/iam/github-meet-boston-trust-policy.json` as its trust policy.
4. Add an inline permissions policy using
   `infrastructure/iam/github-meet-boston-s3-policy.json`.
5. Copy the role ARN.
6. In GitHub, open **Settings → Secrets and variables → Actions → Variables** and
   create `MEET_BOSTON_INGEST_ROLE_ARN` with that ARN.

The trust policy accepts only the repository's `main` branch. The permissions
policy cannot read, delete, or overwrite objects outside the Meet Boston staging
prefix.

## Activation sequence

1. Run **Meet Boston ingestion** manually in GitHub Actions.
2. Confirm both the immutable archive and `latest.json` exist under the staging
   prefix.
3. Deploy the collector image containing the staging reader.
4. Invoke the collector once and confirm `summary.by_source` contains
   `Meet Boston` with no Meet Boston source failure.
5. Add the two DST-safe GitHub cron triggers only after the manual path succeeds.

The collector accepts staging data only when it has schema version `1`, identifies
the source as `Meet Boston`, contains usable events, and is no more than 30 hours
old. It falls back to direct collection if any check fails.
