# Boston Weekend Agent contributor instructions

波波 Bo is the production editorial agent, not only a signature. Preserve these
runtime guarantees when changing collectors, ranking, reports, or social copy:

- Keep hard factual safeguards deterministic: unavailable events, valid dates,
  cooldowns, source links, and schema validation must not be delegated to an LLM.
- Use Bo's versioned persona and S3 memory in semantic ranking and writing calls.
- Distance is a soft convenience signal. Never introduce a fixed local quota.
- Rare annual, culturally significant, visually distinctive, and
  community-defining Greater Boston events may outrank routine nearby events.
- Store model, prompt version, memory version, component scores, reasons, and
  final rank for auditability.
- Runtime models must not edit their own core persona or memory. Memory changes
  require evidence, a new version, an immutable archive, and human review.
- Use natural Taiwan Traditional Chinese first and natural English second. Do
  not use emoji; Bo's deterministic kaomoji is added by application code.
- Never publish to Threads while `THREADS_PUBLISH_ENABLED=false`, and never turn
  it on without explicit authorization.

The executable persona is copied into each Lambda image. The mutable production
memory lives at `s3://boston-weekend-agent-reports/agent/bobo-memory.json`; the
checked-in `memory.default.json` is only a safe bootstrap fallback.
