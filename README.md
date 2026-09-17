# Boston Weekend Mood Agent

A serverless AWS workflow that collects upcoming Boston events, combines them
with weather context, and generates a concise weekend guide with an LLM.

The project originally used an EC2-hosted scraper and an ECR-backed report
Lambda. This version removes the always-on EC2 dependency and packages both the
event collector and report generator as reproducible Lambda container images.

## Architecture

```text
EventBridge Scheduler
        |
        v
AWS Step Functions
        |
        +--> Event collector Lambda
        |      +-- Ticketmaster API
        |      +-- Eventbrite API
        |      +-- The Boston Calendar
        |      +-- Secrets Manager
        |      `-- S3 events/latest.json
        |
        +--> Weather pipeline
        |
        `--> Report Lambda
               +-- Secrets Manager
               +-- OpenAI API
               `-- S3 reports/weekend_summary.txt
```

## Repository layout

```text
services/
  collect-events/       Event aggregation and normalization Lambda
  langchain-report/     LLM report-generation Lambda
infrastructure/
  iam/                  Least-privilege policy templates
  step-functions/       Target state-machine definition
docs/
  deployment.md         Manual deployment and verification procedure
  migration-checklist.md
tests/                  Offline parser and prioritization tests
```

## Security model

No provider credential is stored in source code or in a Lambda environment
variable. Lambda configuration stores only Secrets Manager ARNs:

- Event collector: `TICKETMASTER_API_KEY`, `EVENTBRITE_TOKEN`
- Report generator: `OPENAI_API_KEY`

Each function receives permission to read only its own secret. See
[`SECURITY.md`](SECURITY.md) for repository rules.

## Local checks

Use Python 3.11:

```bash
python -m pip install \
  -r services/collect-events/requirements.txt \
  -r services/langchain-report/requirements.txt \
  boto3

AWS_EC2_METADATA_DISABLED=true \
AWS_ACCESS_KEY_ID=test \
AWS_SECRET_ACCESS_KEY=test \
AWS_DEFAULT_REGION=us-east-1 \
python -m unittest discover -s tests -v
```

Build both Linux images without publishing them:

```bash
docker buildx build --platform linux/amd64 --provenance=false --load \
  -t boston-weekend-events:local services/collect-events

docker buildx build --platform linux/amd64 --provenance=false --load \
  -t boston-weekend-langchain:local services/langchain-report
```

## Deployment

Follow [`docs/deployment.md`](docs/deployment.md). The rollout intentionally
tests each Lambda independently before changing the production state machine.

## Operational behavior

- Provider failures are isolated so one unavailable source does not discard
  usable events from other sources.
- Boston Calendar detail requests are capped and rate-limited.
- Event and report snapshots are timestamped while stable `latest` keys support
  the website.
- The report Lambda reads source data directly from S3, keeping Step Functions
  payloads small.
