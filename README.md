# Boston Weekend Mood Agent

A serverless AWS workflow that collects upcoming Boston events, combines them
with weather context, and generates a concise weekend guide with an LLM.

## Meet Bo / 認識波波

波波（Bo）是一台住在 Boston 雲端地圖裡的黃色探路機器人，也是這個專案的
production editorial agent。波波不是替活動做關鍵字排序的吉祥物：它會讀取經過
驗證的活動資料、角色設定與版本化偏好記憶，理解年度節慶、在地文化、稀有性與
交通距離之間的取捨，再說明每項推薦為什麼值得去。距離是方便程度，不是硬性門檻。

Bo is the project's production editorial agent: a cheerful yellow map robot
that lives in the Boston cloud. Bo semantically ranks verified Greater Boston
events, explains the decision, and writes a Traditional Chinese and English
daily note plus a Thursday/Friday weekend letter. Deterministic code still owns
hard facts such as dates, cancellations, sold-out status, cooldowns, and links.

Bo's core identity is versioned with the Lambda images. Its reviewed long-term
preference memory lives in S3, with immutable history and the memory version
recorded in every ranking run. Runtime models cannot silently rewrite either.

The project originally used an EC2-hosted scraper and an ECR-backed report
Lambda. This version removes the always-on EC2 dependency and packages both the
event collector and report generator as reproducible Lambda container images.

Eventbrite's city-wide Event Search endpoint was retired in 2019, so an
Eventbrite token cannot provide public Boston discovery. The collector uses the
City of Boston's official events RSS feed instead of scraping a site that blocks
cloud-hosted requests.

## Architecture

```text
EventBridge Scheduler
        |
        v
AWS Step Functions
        |
        +--> CloudWatch Logs (ERROR, 30-day retention)
        |
        +--> Event collector Lambda
        |      +-- Ticketmaster Greater Boston radius search
        |      +-- City of Boston events RSS
        |      +-- Official municipal iCalendar feeds
        |      +-- Revere and Discover Quincy calendars
        |      +-- Secrets Manager
        |      `-- S3 events/latest.json
        |
        +--> Weather pipeline
        |
        `--> Report Lambda
               +-- Secrets Manager
               +-- OpenAI API
               `-- S3 reports/weekend_summary.txt

Daily social Lambda
        +-- Reads the same normalized event snapshot
        +-- Enforces a 48-hour event cooldown
        +-- Calls OpenAI once for semantic ranking and once for shared copy
        +-- Validates Bo's bilingual voice; one repair call is allowed if needed
        +-- Loads Bo's versioned persona and reviewed S3 memory
        +-- S3 social/latest.json and social/latest.txt
        `-- Threads API (optional guarded auto-publish)
```

Both ranking agents compare the complete eligible candidate set in one request
and return only Bo's global top 10. A conservative offline token upper bound
must remain below 50,000 before the request is sent; the current GPT-5.6 Luna
context window is much larger, while ranking output is capped at 7,000 tokens.
Malformed rankings may be retried once and every attempt is recorded.

## Repository layout

```text
services/
  collect-events/       Event aggregation and normalization Lambda
  langchain-report/     LLM report-generation Lambda
  daily-social/         Shared Threads/Xiaohongshu content Lambda
infrastructure/
  iam/                  Least-privilege policy templates
  step-functions/       Target state-machine definition
  cloudformation/       Timezone-aware EventBridge Scheduler resources
docs/
  deployment.md         Manual deployment and verification procedure
  persona.md            Voice, editorial style, and kaomoji identity for Bo
  recommendation-scoring.md  Explainable BU-centered event ranking
  event-sources.md       Active and candidate Greater Boston sources
  analytics-history.md   Immutable report inputs and lineage manifest
  agent-memory.md        Reviewed, versioned long-term memory policy for Bo
  migration-checklist.md
tests/                  Offline parser and prioritization tests
```

## Security model

No provider credential is stored in source code or in a Lambda environment
variable. Lambda configuration stores only Secrets Manager ARNs:

- Event collector: `TICKETMASTER_API_KEY`
- Report generator: `OPENAI_API_KEY`
- Daily social generator: `OPENAI_API_KEY`
- Daily social publisher: long-lived Threads token and Threads user ID

Each function receives permission to read only its own secret. See
[`SECURITY.md`](SECURITY.md) for repository rules.

## Local checks

Use Python 3.11:

```bash
python -m pip install \
  -r services/collect-events/requirements.txt \
  -r services/langchain-report/requirements.txt \
  -r services/daily-social/requirements.txt \
  boto3

AWS_EC2_METADATA_DISABLED=true \
AWS_ACCESS_KEY_ID=test \
AWS_SECRET_ACCESS_KEY=test \
AWS_DEFAULT_REGION=us-east-1 \
python -m unittest discover -s tests -v
```

Build the Linux images without publishing them:

```bash
docker buildx build --platform linux/amd64 --provenance=false --load \
  -t boston-weekend-events:local services/collect-events

docker buildx build --platform linux/amd64 --provenance=false --load \
  -t boston-weekend-langchain:local services/langchain-report

docker buildx build --platform linux/amd64 --provenance=false --load \
  -t boston-weekend-daily-social:local services/daily-social
```

## Deployment

Follow [`docs/deployment.md`](docs/deployment.md). The rollout intentionally
tests each Lambda independently before changing the production state machine.

## Operational behavior

- Provider failures are isolated so one unavailable source does not discard
  usable events from other sources.
- Community events come from official or city-affiliated calendars across
  Greater Boston. See [`docs/event-sources.md`](docs/event-sources.md).
- Ticket listings retain availability state; sold-out, canceled, postponed,
  rescheduled, and off-sale events remain in history but are not recommended.
- Bo ranks events with an explainable BU-centered 100-point model and archives
  the full candidate decision trail for later feedback analysis.
- Event and report snapshots are timestamped while stable `latest` keys support
  the website.
- Each report archives the exact versioned inputs it used and writes a structured
  analytics manifest with model, prompt, token, input, and output lineage.
- CloudFront exposes only `reports/weekend_summary.txt`; the S3 bucket and all
  event, social, and analytics history remain private.
- Events are collected daily for a ten-day window. The full weekend workflow
  runs Thursday for an early planning edition and Friday for a refreshed edition.
- One bilingual social post (Traditional Chinese first, English second) is generated daily
  and reused unchanged for Threads and Xiaohongshu, with a 48-hour event
  cooldown.
- Both social copy and weekend letters keep Bo's fixed opening and signoff while
  placing varied contextual kaomoji inside the prose. The normal OpenAI call
  count is unchanged; a failed voice contract triggers at most one auditable
  repair call.
- Bo's first Threads post uses a dedicated zero-LLM introduction mode with a
  separate idempotency key. Preview it with `{"mode":"introduction"}`; actual
  publication additionally requires `{"mode":"introduction","publish":true}`
  and `THREADS_PUBLISH_ENABLED=true`.
- The report Lambda reads source data directly from S3, keeping Step Functions
  payloads small.

See [`docs/content-schedule.md`](docs/content-schedule.md) for the schedule and
change-detection behavior.
