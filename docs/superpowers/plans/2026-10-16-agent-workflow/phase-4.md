---
status: in_progress
---
# Phase 4: Stable agent contracts and monitoring delivery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Independent review is required before completion. Root coordinates shared paths and commits.

**Goal:** Expose resumable operations with truthful side effects and deliver durable change events to a host-owned scheduler.

**Architecture:** Reuse the existing engine and the exact contracts in [design](../../specs/2026-10-16-agent-workflow-design.md). This phase adds compatible metadata or focused adapters, without a second scorer, database, service, or daemon.

**Tech Stack:** Python >=3.10 standard-library runtime; existing MCP >=1.30,<2 optional extra; extension JavaScript. Eval scripts use unittest/stdlib and current eval/run_*.py conventions.

## Global Constraints

- Existing JSON keys and one-shot CLI/MCP calls remain compatible; new schemas are additive and versioned.
- No new required runtime dependency for the CLI core; PDF/OCR dependencies are optional.
- No secrets in persisted runs, evidence, logs, reports or diagnostics; reuse canonical redaction.
- No hidden network calls in read/resume/render/status/replay; paid fallback requires explicit policy.
- Unavailable, stale, not applicable, conflict and proven absence remain distinct through final output.
- All code steps include a representative test or public usage example below; implementations follow exact contracts and acceptance tables, not speculative replacement engines.
- Root coordinates commits; do not commit during the current parallel implementation session.

**Dependencies:** Phases 0–3; events consume saved revisions and existing watchlist diff.

**Scope ownership:** Own MCP schemas/annotations and monitor_events.py/watchlist event integration; coordinate native host with Phase 2 and scheduler helper with Phase 3.


## Task 4A: MCP schema snapshots and actual side effects

**Files:** Modify `mcp/tools_impl.py`, `mcp/server.py`, `mcp/test_server.py`, `eval/mcp_handshake.py`; create `eval/run_workflow_mcp_eval.py`, `eval/fixtures/workflow-tools.json`.

**Consumes:** workflow envelopes and run APIs. **Produces:** new tools `counterparty_run_start`, `counterparty_run_read`, `counterparty_run_resume`, `counterparty_run_manual`, `counterparty_run_refresh`, `counterparty_run_render`, `counterparty_run_export`, `counterparty_run_snapshot`, `counterparty_doctor`, `counterparty_cache_status`, `counterparty_cache_plan`. Names are added to EXPECTED_TOOLS; no legacy tool removed.

Tool args mirror Python API except root omitted, format literals enforced, revision optional positive int; manual receives blocks object plus expected_revision/operation_id. Doctor tool is offline; separate `counterparty_probe` performs explicit network diagnostics. Existing access_check retains behavior and writable annotation. Errors are envelope, not string traceback.

- [ ] Snapshot tools/list names, input schemas and annotations, including legacy optional save/export effects. Assert pure tools create no files. Run actual stdio initialize → tools/list → run read → render; fake SDK/unit decorator tests alone do not satisfy protocol gate.

```python
request = {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
    "name": "counterparty_run_read", "arguments": {"run_id": run_id}}}
response = client.call(request)
payload = decode_tool_json(response)
assert payload["schema_version"] == 1
assert payload["run_id"] == run_id
assert payload["data"]["metadata"]["decision"] == expected_decision
```

client/decode_tool_json are explicit eval helpers over the existing handshake framing; the saved run comes from Phase 2 fixture factory.

- [ ] Run red; implement transport wrappers, input validation and ToolAnnotations supported by pinned SDK1. No dynamic readOnly claim for optional save; read-only tools read, write tools mutate local state explicitly. Reuse Phase 0 subprocess group cancellation; async wrapper cancellation must terminate child work, not merely stop waiting for a background thread.
- [ ] Add real notifications/cancelled test with descendant marker delayed past cancel; assert no marker, no orphan fetch and cancelled/interrupted run. Run mcp/test_server.py plus MCP sdk/handshake/phase0 safety evals. No SDK2 or Tasks prerequisite.

## Task 4B: Durable events, leases and idempotent receipts

**Files:** Create `scripts/monitor_events.py`, `eval/run_monitor_events_eval.py`; modify `scripts/watchlist.py` at result/diff integration; coordinate CLI commands in workflow_cli.py.

**Consumes:** existing watchlist reports, run revision ids and coverage diff. **Produces:** `ingest(report: dict, *, root=None)->list[dict]`, list_events/claim_events/ack_events/retry_events signatures in design. Add MCP `counterparty_events_list`, `counterparty_events_claim`, `counterparty_events_ack` with matching annotations; ack accepts ids, never file paths.

- [ ] Add ten synthetic events covering company_changed, coverage_lost, coverage_restored and refresh_failed. Test ingest twice, claim then crash, lease expiry, retry, duplicate ack, foreign consumer and concurrent consumers.

```python
from monitor_events import ingest, list_events, claim_events, ack_events
first = ingest(watch_report, root=queue_root)
second = ingest(watch_report, root=queue_root)
assert [e["event_id"] for e in first] == [e["event_id"] for e in second]
assert len(list_events(root=queue_root)) == len(first)
leased = claim_events("agent-a", root=queue_root, limit=50, lease_seconds=300)
ack_events([e["event_id"] for e in leased], consumer_id="agent-a", root=queue_root)
assert list_events(state="pending", root=queue_root) == []
```

- [ ] Run red. Persist stable content IDs and delivery states atomically. Separate transport delivered/read ack; list does not consume. Failures do not emit “no changes”; lost coverage is its own event. Preserve event data for next process even after closed chat. Include run origin/hash and configured retry cap/backoff.
- [ ] Run monitor/watchlist/diff evals; verify same event seen after crash with same id, never counted as new, and no lost acknowledged event in declared fixture campaign. Document at-least-once transport and receiver idempotency; do not claim exactly-once external delivery.

## Task 4C: Host plan, OS schedulers and sample integration

**Files:** Modify `scripts/watchlist.py`, `scripts/refresh_indexes.py`; create `references/monitoring-host.md`, `examples/n8n-monitoring.json`, `eval/run_schedule_plan_eval.py`; add CLI `watch plan`, `events list/claim/ack/retry`.

**Consumes:** cache next_due_at and fetch/batch/graph budgets. **Produces:** `host_plan(profile: str, *, now: str, platform: str, root=None)->dict` in monitor_events; contains argv, next_run_at, timeout, max_subjects, event output and cron/plist/task_xml strings as applicable. Never installs schedule.

- [ ] Parse generated XML/plist with stdlib, round-trip quoting for paths with spaces/ampersands, validate no credentials and no environment dump; mock scheduler commands to raise if called. Windows Task Scheduler has StartWhenAvailable, bounded execution time and IgnoreNew multiple-instance policy.

```python
from monitor_events import host_plan
import xml.etree.ElementTree as ET
plan = host_plan("нейтрально", now="2026-10-16T09:00:00Z", platform="windows")
ET.fromstring(plan["task_xml"])
assert plan["installed"] is False
assert isinstance(plan["argv"], list)
assert plan["limits"]["fetch_timeout_seconds"] <= 120
```

- [ ] Run red; extend existing schedule generators instead of daemon. Example n8n reads local JSON event, idempotency event_id and explicit ack only after host action; placeholders are input configuration fields, no embedded secrets or preconfigured recipients. Documentation shows dry-run then user-owned install; no auto notification.
- [ ] Run scheduler eval on all OS CI, Windows native-host process tests, event queue E2E. Actual OS registration/run is a separate operator acceptance row; generated valid XML alone is not that evidence.

## Exit evidence

Stable schemas, correct error distinctions and cancellation pass actual stdio. Ten-event campaign has zero lost events or duplicate event identities under retries; host plan and Windows XML are validated without installation. External delivery and real scheduler execution remain explicitly recorded separately.
