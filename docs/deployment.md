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
- `OPENAI_MODEL`: a model available to the configured OpenAI project

Remove the plaintext `OPENAI_API_KEY` Lambda environment variable.

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
- `OPENAI_MODEL`: a model available to the configured OpenAI project
- `SOCIAL_COOLDOWN_HOURS`: `48`
- `MAX_SOCIAL_EVENTS`: `5`
- `WEBSITE_URL`: `https://www.hsiangyuhuang.com/weekend_report`

Attach the standard Lambda basic execution policy and the scoped statements in
`infrastructure/iam/daily-social-policy.json` after replacing the placeholder.

Invoke the function directly before creating its schedule. Confirm that these
objects exist and contain the same shared copy:

- `social/latest.json`
- `social/latest.txt`
- `social/campaigns/YYYY/MM/YYYY-MM-DD.json`
- `social/history.json`

## Install the schedules

Deploy `infrastructure/cloudformation/schedules.yaml` with the collector Lambda
ARN, daily-social Lambda ARN, and existing weekend state-machine ARN. The stack
requires `CAPABILITY_NAMED_IAM` because it creates one scheduler execution role.

The new schedules use `America/New_York` and are documented in
`content-schedule.md`. After all three targets have passed independent tests:

1. Deploy the schedule stack.
2. Run each new schedule target manually once.
3. Confirm the Thursday/Friday schedule is enabled.
4. Disable `boston-weekend-agent-daily-run-rule` so the old daily full workflow
   cannot continue making unnecessary LLM calls.
5. Do not delete the old rule until the new schedules have run successfully.

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
