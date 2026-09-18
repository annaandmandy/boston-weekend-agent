# Deployment guide

Examples use the named AWS CLI profile `boston-deployer` and `us-east-1`.
Perform deployments with a non-root identity.

## Prerequisites

```bash
/opt/homebrew/bin/aws sts get-caller-identity --profile boston-deployer
docker version
```

The account in the identity response must be the account that owns the state
machine and S3 bucket. Docker Desktop must be running.

## Build the event collector

```bash
cd services/collect-events
docker buildx build --platform linux/amd64 --provenance=false --load \
  -t boston-weekend-events:v1 .
```

Create the ECR repository once:

```bash
/opt/homebrew/bin/aws ecr create-repository \
  --repository-name boston-weekend-events \
  --image-tag-mutability IMMUTABLE \
  --image-scanning-configuration scanOnPush=true \
  --region us-east-1 \
  --profile boston-deployer
```

Authenticate Docker without placing a password in a command argument:

```bash
/opt/homebrew/bin/aws ecr get-login-password \
  --region us-east-1 \
  --profile boston-deployer \
| docker login \
  --username AWS \
  --password-stdin ACCOUNT_ID.dkr.ecr.us-east-1.amazonaws.com
```

Tag and push using the repository URI returned by ECR:

```bash
docker tag boston-weekend-events:v1 \
  ACCOUNT_ID.dkr.ecr.us-east-1.amazonaws.com/boston-weekend-events:v1
docker push ACCOUNT_ID.dkr.ecr.us-east-1.amazonaws.com/boston-weekend-events:v1
```

Create `collect_events_v2` as an x86_64 image Lambda. Configure:

- Memory: 512 MB
- Timeout: 300 seconds
- `EVENTS_SECRET_ID`: full secret ARN
- `REPORT_BUCKET`: `boston-weekend-agent-reports`
- `MAX_EVENTS_PER_SOURCE`: `10`
- `MAX_CITY_EVENTS`: `30`
- `DAYS_AHEAD`: `10`
- `VERIFY_TICKET_PAGE_STATUS`: `true`

Attach the standard Lambda basic execution policy and the scoped statements in
`infrastructure/iam/collect-events-policy.json` after replacing the placeholder.

## Build the report generator

```bash
cd services/langchain-report
docker buildx build --platform linux/amd64 --provenance=false --load \
  -t boston-weekend-langchain:v1 .
```

Create an immutable, scan-on-push ECR repository named
`boston-weekend-langchain`, then tag and push as above. Update the existing
image Lambda to the new image URI. Configure:

- Memory: 1024 MB
- Timeout: 300 seconds
- `OPENAI_SECRET_ID`: full secret ARN
- `REPORT_BUCKET`: `boston-weekend-agent-reports`
- `OPENAI_MODEL`: `gpt-5.6-luna`
- `OPENAI_REASONING_EFFORT`: `none`

Remove the plaintext `OPENAI_API_KEY` Lambda environment variable.

Before deploying analytics history, enable Versioning on the report bucket.
Update the report role with `infrastructure/iam/langchain-report-policy.json`,
which adds exact-version reads and scoped `analytics/*` access. Also configure:

- `REPORT_PROMPT_VERSION`: increment this whenever the prompt contract changes

After direct invocation, verify that one new directory exists under
`analytics/report_runs/year=YYYY/month=MM/day=DD/`. It must contain archived
inputs, `effective_event_changes.json`, `report.txt`, and `report.json`. Inspect
the manifest's source VersionIds and ETags before enabling the schedule.

## Public report delivery

Keep the report bucket private. The website reads only the current report
through a CloudFront distribution with an S3 Origin Access Control (OAC):

```text
Browser -> CloudFront -> reports/weekend_summary.txt
```

The production distribution is `EBSSO01S4DVXI` at
`d2ugiuoady5eh5.cloudfront.net`. It uses the managed `CachingDisabled` policy so
new Thursday and Friday reports are visible immediately, plus the managed
`SimpleCORS` response headers policy for browser access.

Apply `infrastructure/cloudfront/report-bucket-policy.json` after replacing
`${CLOUDFRONT_DISTRIBUTION_ARN}`. The S3 resource must remain the single exact
`reports/weekend_summary.txt` object; do not widen it to `reports/*` or the
whole bucket. Verify the report path returns HTTP 200 and paths under `events/`,
`social/`, and `analytics/` return HTTP 403 through the distribution.

## Build the daily social generator

```bash
cd services/daily-social
docker buildx build --platform linux/amd64 --provenance=false --load \
  -t boston-weekend-daily-social:v1 .
```

Create an immutable, scan-on-push ECR repository named
`boston-weekend-daily-social`. Create a separate image Lambda named
`daily_social` with:

- Memory: 1024 MB
- Timeout: 300 seconds
- `OPENAI_SECRET_ID`: the same scoped OpenAI secret ARN used by the report Lambda
- `REPORT_BUCKET`: `boston-weekend-agent-reports`
- `OPENAI_MODEL`: `gpt-5.6-luna`
- `OPENAI_REASONING_EFFORT`: `none`
- `SOCIAL_COOLDOWN_HOURS`: `48`
- `MAX_SOCIAL_EVENTS`: `5`
- `WEBSITE_URL`: `https://www.hsiangyuhuang.com/weekend_report`
- `THREADS_SECRET_ID`: the ARN for `boston-weekend-agent/threads`
- `THREADS_PUBLISH_ENABLED`: `false` during the first deployment
- `THREADS_TOKEN_REFRESH_DAYS`: `7`

Attach the standard Lambda basic execution policy and the scoped statements in
`infrastructure/iam/daily-social-policy.json` after replacing both secret ARN
placeholders. The Threads statement permits `PutSecretValue` so the Lambda can
refresh the long-lived token before it expires.

Invoke the function directly before creating its schedule. Confirm that these
objects exist and contain the same shared copy:

- `social/latest.json`
- `social/latest.txt`
- `social/campaigns/YYYY/MM/YYYY-MM-DD.json`
- `social/history.json`

### Connect Threads publishing

Install the extra AWS login credential dependency once in the local virtual
environment:

```bash
.venv/bin/pip install 'botocore[crt]'
```

Run `scripts/setup_threads_oauth.py` and follow its hidden prompts to exchange
the callback authorization code. The script validates the expected Threads
username and writes the long-lived token to `boston-weekend-agent/threads`
without printing it.

After the Lambda image and IAM policy are updated, keep
`THREADS_PUBLISH_ENABLED=false` for one direct invocation and inspect
`social/latest.txt`. Confirm that the Chinese section uses Traditional Chinese.
When the copy is acceptable, change the flag to `true` and
invoke once. A successful run creates
`social/publications/threads/YYYY-MM-DD.json` with status `published` and the
Threads post IDs.

Before enabling the daily schedule, preview Bo's fixed bilingual introduction:

```bash
/opt/homebrew/bin/aws lambda invoke \
  --function-name daily_social \
  --payload '{"mode":"introduction"}' \
  --cli-binary-format raw-in-base64-out \
  --profile boston-deployer \
  --region us-east-1 \
  /tmp/bobo-introduction.json
```

It uses zero OpenAI calls and includes the public Weekly Report link. After
review, set `THREADS_PUBLISH_ENABLED=true` and invoke with
`{"mode":"introduction","publish":true}`. Its separate idempotency record is
`social/publications/threads/introduction.json`, so it cannot collide with a
daily post. Publish the introduction before enabling the recurring social
schedule.

That publication object is an idempotency guard. A second invocation on the
same local date returns `already_published`. If its status is `publishing` or
`failed`, inspect the Threads account and CloudWatch logs before removing or
changing it; blindly retrying could duplicate a partially published thread.

## Install the schedules

Deploy `infrastructure/cloudformation/schedules.yaml` with the collector Lambda
ARN, daily-social Lambda ARN, existing weekend state-machine ARN, and the email
address that should receive failure alerts. The stack requires
`CAPABILITY_NAMED_IAM` because it creates one scheduler execution role.

The same stack also creates:

- `boston-weekend-scheduler-dlq`, an SQS dead-letter queue with SQS-managed
  encryption and 14-day retention;
- `boston-weekend-alerts`, an SNS topic with an optional email subscription;
- CloudWatch alarms for all three Lambdas, failed Step Functions executions,
  and visible messages in the scheduler DLQ.

After updating the stack, confirm the SNS subscription from the email sent by
AWS. Until that link is confirmed, the subscription remains `PendingConfirmation`
and alarms cannot deliver email to it.

The new schedules use `America/New_York` and are documented in
`content-schedule.md`. After all three targets have passed independent tests:

1. Deploy the schedule stack.
2. Run each new schedule target manually once.
3. Confirm the Thursday/Friday schedule is enabled.
4. Disable `boston-weekend-agent-daily-run-rule` so the old daily full workflow
   cannot continue making unnecessary LLM calls.
5. Do not delete the old rule until the new schedules have run successfully.

### Failure boundaries

The scheduler DLQ receives an event only when EventBridge Scheduler exhausts
its retries while trying to invoke its target. It does not receive a Lambda
exception after a successful invocation, nor a later failure inside the Step
Functions workflow. Those failures are covered separately by the Lambda and
Step Functions CloudWatch alarms.

To inspect a delivery failure in the AWS Console, open **SQS**, choose
`boston-weekend-scheduler-dlq`, and use **Send and receive messages** to poll for
messages. After resolving and replaying the failure, delete only the messages
you have verified; otherwise the queue alarm remains in `ALARM`.

To inspect processing failures, open **CloudWatch > Alarms > All alarms**, then
follow the affected resource to its CloudWatch Logs or Step Functions execution
history. The five alarm names all begin with `boston-weekend-`.

## Enable Step Functions execution logging

Create `/aws/vendedlogs/states/BostonWeekendAgentWorkflow` as a Standard
CloudWatch Log Group with a 30-day retention policy. Attach
`infrastructure/iam/stepfunctions-logging-policy.json` to the state machine's
execution role as an inline policy named `BostonWeekendStepFunctionsLogging`.

Configure the Standard state machine with:

- log level: `ERROR`;
- include execution data: enabled;
- destination: the Log Group ARN ending in `:*`.

CloudWatch Logs delivery APIs require `Resource: "*"`; keep these permissions on
the state machine execution role rather than the deployment user. A successful
workflow produces no error events at `ERROR` level. Use the Step Functions
execution history for successful runs and the Log Group for centralized failure
details.

## Verification order

1. Invoke the collector directly.
2. Inspect CloudWatch Logs.
3. Check the `LastModified` metadata for `events/latest.json`.
4. Invoke the report generator directly.
5. Check `reports/weekend_summary.txt`.
6. Update the state machine only after both functions pass independently.
7. Start one manual state-machine execution.
8. Confirm the Thursday run writes a weekend baseline and the Friday run compares
   against it.
