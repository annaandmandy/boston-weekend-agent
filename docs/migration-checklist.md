# Migration checklist

This checklist moves the legacy EC2/ECR workflow to two container-based Lambda
functions without changing the historical S3 data.

## Observed legacy state

- The state machine exists and is scheduled daily.
- The event Lambda is a 128 MB, 3-second ZIP function that reads an existing S3
  object; it is not the original scraper.
- The report Lambda is an inactive x86_64 image function because its ECR image
  no longer exists.
- The state machine invokes a generic upload function after the report function,
  even though the report function already writes its own S3 objects.
- The event Lambda still has a plaintext provider token environment variable.
- The report Lambda still has a plaintext OpenAI key environment variable.
- Step Functions execution logging and tracing are disabled.

## Safety gates

- [ ] Use a non-root deployment identity.
- [ ] Rotate credentials that previously appeared in source code.
- [ ] Create `boston-weekend-agent/events-api` with provider keys.
- [ ] Create `boston-weekend-agent/openai` with the OpenAI key.
- [ ] Remove plaintext credential environment variables from Lambda.
- [ ] Run a repository secret scan before the first commit.

## Collector rollout

- [ ] Build `services/collect-events` for `linux/amd64`.
- [ ] Push a versioned image to a new ECR repository.
- [ ] Create `collect_events_v2` as a container-image Lambda.
- [ ] Configure 512 MB memory and a 300-second timeout.
- [ ] Set `EVENTS_SECRET_ID`, `REPORT_BUCKET`, and a Boston Calendar limit of 5.
- [ ] Attach CloudWatch Logs, scoped Secrets Manager, and scoped S3 permissions.
- [ ] Invoke the Lambda directly with `{}`.
- [ ] Confirm `events/latest.json` receives a new timestamp.
- [ ] Confirm provider failures are reported without exposing credential values.
- [ ] Increase the Boston Calendar limit only after observing runtime and status codes.

## Report rollout

- [ ] Build `services/langchain-report` for `linux/amd64`.
- [ ] Push a versioned image to ECR.
- [ ] Update the existing image Lambda to the new image URI.
- [ ] Replace `OPENAI_API_KEY` with `OPENAI_SECRET_ID`.
- [ ] Attach scoped secret-read and S3 read/write permissions.
- [ ] Invoke the Lambda directly with `{}`.
- [ ] Confirm `reports/weekend_summary.txt` receives a new timestamp.

## Workflow rollout

- [ ] Export and archive the current state-machine definition.
- [ ] Point `CollectEvents` at `collect_events_v2`.
- [ ] Make `LangChainReport` the terminal state.
- [ ] Remove the duplicate final upload state.
- [ ] Add retries for transient Lambda service failures.
- [ ] Enable Step Functions logging after verifying the log group and IAM policy.
- [ ] Run one manual execution and inspect every state.
- [ ] Choose the intended schedule: daily or weekly Friday.

## Rollback

- Keep the previous state-machine definition locally.
- Use versioned, immutable image tags.
- Do not delete the old ZIP event function until the new workflow has completed
  successfully at least twice.
