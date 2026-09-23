# ANA — Observability (roadmap stage 4)

Date: 2026-09-22
Status: implementing

## Goal

See what the orchestrator actually does, in two layers the roadmap names:

- **Traces** (self-hosted Langfuse): one trace per turn — which rung ran it,
  the RAG/handoff prompt build, the CLI call, gate decisions, tokens and cost.
- **Metrics** (Prometheus + Grafana): turn counts and latency, gate verdicts by
  rule, failover trips, approval outcomes, so a dashboard answers "is anything
  being blocked / retried / hanging" without reading logs.

Everything is self-hosted: trace data never leaves the machine, matching the
internal-network-only posture the roadmap sets.

## Design

### Metrics (`packages/observability/metrics.py`)

`prometheus_client` registry exposed at `GET /metrics`:

| Metric | Type | Labels |
|---|---|---|
| `ana_turns_total` | counter | agent, outcome (ok/error/cancelled) |
| `ana_turn_duration_seconds` | histogram | agent |
| `ana_turn_cost_usd_total` | counter | agent |
| `ana_turn_tokens_total` | counter | agent |
| `ana_gate_decisions_total` | counter | verdict, decided_by, rule_id |
| `ana_gate_wait_seconds` | histogram | — (time a human took to answer) |
| `ana_failovers_total` | counter | from_rung, to_rung |
| `ana_active_sessions` | gauge | — |

Rule ids are bounded by the YAML file, so they are safe as a label; command
text never becomes one.

### Traces (`packages/observability/tracing.py`)

A thin wrapper so the app never imports the SDK directly: when
`LANGFUSE_HOST`/keys are set it emits a trace per turn with spans for prompt
build, each CLI attempt, and each gate decision; when they are not, every call
is a no-op. That keeps the default deployment dependency-free at runtime and
the tests hermetic.

### Compose

- `langfuse` (v2 image) plus its own `langfuse-postgres`, on 3100.
- `prometheus` scraping `server:8000/metrics`, on 9090.
- `grafana` with a provisioned datasource and one ANA dashboard, on 3200.

All bound to localhost, like the existing Postgres.

## What the build turned up

- **Metrics were being lost on disconnect.** `record_turn` sat after the
  node's try/finally, but a client that disconnects right after the result
  frame cancels the coroutine while its tail (embedding + storage) is still
  running — so real turns went uncounted. Recording moved into the `finally`,
  where a cancelled turn is counted under its own `cancelled` outcome
  (`asyncio.CancelledError` is a `BaseException`, so it shows up in
  `sys.exc_info()` there rather than in an `except Exception`).
- **Content type has to match the body.** Serving `generate_latest()` output
  under the OpenMetrics content type makes Prometheus refuse the scrape with
  "data does not end with # EOF" — the target just sits there `down`.
- **Langfuse v2 validates its init email**, so `ana@localhost` aborts startup
  with "Invalid environment variables"; the default is now `ana@example.com`.

## Verification

- `pytest`: metric values change on turn/gate/failover paths; tracer is a
  no-op without configuration and records spans with a fake client.
- Live: `/metrics` exposes the families after a real turn; Prometheus shows
  the target up; Grafana loads the dashboard; Langfuse shows the trace.
