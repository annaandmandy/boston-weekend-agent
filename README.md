# Boston Weekend Mood Agent

A serverless AWS workflow that collects upcoming Boston events, combines them
with weather context, and generates a concise weekend guide with an LLM.

## Meet Bo / 認識波波 
Threads([`@bostonweekendagent`](https://www.threads.com/@bostonweekendagent))

Weekend Report: https://www.hsiangyuhuang.com/weekend_report

波波（Bo）是一台住在 Boston 雲端地圖裡的黃色探路機器人，也是這個專案的
production editorial agent。波波不是替活動做關鍵字排序的吉祥物：它會讀取經過
驗證的活動資料、角色設定與版本化偏好記憶，理解年度節慶、在地文化、稀有性與
交通距離之間的取捨，再說明每項推薦為什麼值得去。距離是方便程度，不是硬性門檻。

Bo is the project's production editorial agent: a cheerful yellow map robot
that lives in the Boston cloud. Bo semantically ranks verified Greater Boston
events, explains the decision, and writes a Traditional Chinese and English
daily note plus a rolling website letter with richer Thursday/Friday editions.
Deterministic code still owns
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
        |      +-- Meet Boston public RSS + bounded JSON-LD enrichment
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

Website activity feedback
        +-- React Activities explorer (anonymous reversible Like)
        +-- API Gateway HTTP API with origin-restricted CORS and throttling
        +-- Feedback Lambda validates IDs against the current S3 report
        `-- DynamoDB stores atomic totals, browser vote state, and action history
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
  event-feedback/       Zero-LLM anonymous activity feedback Lambda
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

Public activity feedback uses a random browser-local ID rather than a name,
email address, or social account. The Lambda stores only its SHA-256 hash. One
browser can hold one reversible vote per event; every state change is retained
as an analytics action. This is a lightweight preference signal, not strong
identity or anti-fraud verification.

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

docker buildx build --platform linux/amd64 --provenance=false --load \
  -t boston-weekend-event-feedback:local services/event-feedback
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
- CloudFront exposes only the public report text/JSON objects used by the
  website; the S3 bucket and all event, social, feedback, and analytics history
  remain private.
- Activity Like totals use DynamoDB transactional writes so the voter state,
  aggregate count, and immutable action record change together. The feature
  does not call an LLM.
- Events are collected daily for a ten-day window. A rolling website report runs
  every morning, with a Thursday planning edition and a Friday baseline-aware
  refresh. Weekend editions keep the current day useful and look ahead without
  asking the LLM to reproduce the complete Activities table.
- Meet Boston uses a small GitHub Actions ingestion bridge because its public
  feed currently blocks the Lambda egress path. GitHub assumes a narrowly scoped
  AWS role through OIDC, writes a versioned S3 staging snapshot, and the collector
  accepts it only while fresh. See `docs/meet-boston-ingestion.md`.
- One bilingual social edition is generated daily and reused for Threads and
  Xiaohongshu, with a 48-hour event cooldown. Threads separates Traditional
  Chinese and English first, then splits either language independently only when
  its copy exceeds the platform's 500-character limit.
- Both social copy and weekend letters keep Bo's fixed opening and signoff while
  placing varied contextual kaomoji inside the prose. The normal OpenAI call
  count is unchanged; emoji are removed deterministically, and a failed kaomoji
  contract triggers at most one auditable repair call before publishing the
  last parseable draft.
- Bo's first Threads post uses a dedicated zero-LLM introduction mode with a
  separate idempotency key. Preview it with `{"mode":"introduction"}`; actual
  publication additionally requires `{"mode":"introduction","publish":true}`
  and `THREADS_PUBLISH_ENABLED=true`.
- The report Lambda reads source data directly from S3, keeping Step Functions
  payloads small.

See [`docs/content-schedule.md`](docs/content-schedule.md) for the schedule and
change-detection behavior.
