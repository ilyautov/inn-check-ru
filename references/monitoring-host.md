# Host-owned monitoring and event receipts

Читать перед настройкой host-owned мониторинга, расписания ОС и обработкой claim/ack/retry событий.

`workflow_cli watch plan` returns configuration; it does not install a service, edit
crontab, run launchctl/schtasks or send a notification. Close the chat after the
host scheduler has been configured: saved runs and the event outbox remain local.
Python 3.10+ and the standard library suffice, including the shared process-tree
helper packaged in `inn_check_ru_mcp.tools_impl`; the optional MCP SDK is not loaded.

## Dry run, then operator-owned installation

```sh
python3 scripts/workflow_cli.py watch plan --profile нейтрально --platform linux --store /absolute/monitor --json
python3 scripts/watchlist.py --добавить 7707083893 --файл /absolute/monitor/watchlist.json
python3 scripts/workflow_cli.py watch monitor --profile нейтрально --store /absolute/monitor --max-subjects 50 --json
```

Use a real local directory, without symlinks/reparse points. `--store` on `watch`
means a host workspace containing `watchlist.json`, `runs/`, `events/` and the
monitor journal. On `events`, `--store` means the **events directory itself**.
The default workspace is `~/.local/share/inn-check-ru`, so default `runs/` and
`events/` match workflow and MCP reads. If using an explicit workspace, configure
the MCP host `INN_CHECK_EVENTS_DIR` to that workspace/events; the MCP API accepts
IDs, never caller-provided paths.
Profiles explicitly attached to other watchlist entries are skipped. An oversized
selected list is rejected before fetching; it is never silently truncated.

The plan provides exact argv, next_run_at, limits, event_output, cron, plist and
task_xml. Cache freshness is inspected from the same shared cache used by the
collector. The plan does not update caches: explicit `cache update` remains a
separate budgeted operation. Missing required caches make monitoring due now;
otherwise the next cache due time, capped at one day, informs the schedule.
Calendar times are rounded up to the next minute. Windows StartBoundary has a UTC
offset; cron and launchd hours use the host timezone (so rerender after changing
that timezone). Cron/launchd templates are daily; install the template appropriate
to this host and review its timezone. The first due check can also be run using
argv. A next_run_at already in the past means the command is due now.

The operator saves/installs the returned template using their OS scheduler.
Windows XML specifies StartWhenAvailable, IgnoreNew, 6060 seconds total execution
and two retries at one-minute intervals. Each collector subprocess has a 120-second
process-tree timeout; a batch is at most 50 subjects and graph limits remain 25.
The monitor command uses a workspace OS lock; do not configure overlapping cron
invocations. Successful template parsing is **not** evidence that the OS registered
or executed it. Native Windows execution, real scheduler installation and external
notification delivery require separate operator acceptance.

## Event and crash contract

The normal path is saved run -> existing fingerprint/diff -> pending journal ->
outbox commit -> comparison baseline commit. On restart the pending journal is
replayed before any new fetch. A crash after outbox commit but before baseline
commit reuses event_id; acknowledged events are never reset by repeated ingest.

Events have schema_version=1, event_id (SHA-256), subject_inn, profile,
from_revision, to_revision, kind, change_digest, observed_at, data, origin and
delivery. Identity hashes the subject, profile, revision pair, change and origin.
Saved origins include real from/to run IDs and the immutable payload hashes.
Legacy reports and failed workers that return no saved revision use null run IDs,
null revisions and `origin.kind=legacy_report` with report_observed_at. No saved
run is inferred from a filename or invented for a failed worker.

Kinds distinguish company_changed, coverage_lost, coverage_restored and
refresh_failed. Incomplete checks can emit both a proven change and refresh_failed.
A timeout, unavailable source or lost coverage never becomes “no changes”. The
first complete observation establishes a baseline rather than claiming a change.

Saved runs additionally compare `canonical_evidence` support by logical source/field
and applicable decision rule. The projection reads stored claims and decision
coverage only; monitoring does not re-score, re-fetch raw responses or re-evaluate
TTL. A stored known-to-stale, conflict, incomplete or missing-proof transition
emits `coverage_lost`; recovered support emits `coverage_restored`. Reason codes
remain in the report and event. Retrieval timestamps, claim IDs, response hashes
and rules-version hashes alone cannot create a coverage or company change.
`coverage_changes` is separate from the existing `изменения` company/source diff.
A canonical coverage deficit makes the monitor envelope `partial`, even when
legacy source availability is `ok`; it is not itself a failed refresh. Repeated
unchanged deficits stay visible in `canonical_evidence.incomplete` without another
coverage event. A real worker/source failure still emits `refresh_failed`.
Browser-only sources remain explicitly listed as unsupported; their unchanged
unknown claims alone do not mark a script refresh as failed. Required decision
rules and loss of previously observed support remain visible independently.

`watchlist.прогон(..., events_root=...)` can explicitly ingest legacy reports;
omitting events_root preserves the original one-shot behavior and report shape.
`watch monitor` always ingests durably and includes events in its JSON envelope.

## Claim, action, acknowledge

```sh
python3 scripts/workflow_cli.py events list --store /absolute/monitor/events --json
python3 scripts/workflow_cli.py events claim --consumer-id local-n8n --limit 50 --lease-seconds 300 --store /absolute/monitor/events --json
# Perform the configured host action, using event_id as its idempotency key.
python3 scripts/workflow_cli.py events ack EVENT_ID --consumer-id local-n8n --store /absolute/monitor/events --json
# If the action failed while the lease is live:
python3 scripts/workflow_cli.py events retry EVENT_ID --consumer-id local-n8n --store /absolute/monitor/events --json
```

`list` is a pure read: it neither creates files nor claims events. `claim` records
transport delivery to the named consumer and increments attempts. It does not
acknowledge the host action. `ack` must follow confirmed host action success; a
repeated ack by the same consumer succeeds. Another consumer or an expired lease
is rejected. Consumers must use stable, unique IDs and serialize work for each ID;
lease duration must exceed the action duration. There is no lease renewal API.

A crash before ack allows reclaim after lease expiry with the same event_id.
Retry uses exponential delay (30, 60, 120, 240 seconds), capped at 3600 seconds,
and at most five claims. An exhausted retry or expired fifth lease becomes
`dead_letter`; inspect it with `events list --state dead_letter`. Pending lists
include delayed retries, while claim honors next_retry_at. `--state all` includes
receipts. No automatic deletion/reset of terminal receipts is provided. The queue
has a 32 MiB storage bound and fails explicitly on overflow; keep and archive
receipts under an operator retention policy, never silently discard them.

Storage is a single atomic JSON generation, serialized by a local OS lock, with
anchored file opens and version increments. This supports local filesystems, not
network-share locking guarantees. Delivery is **at least once**. The receiver must
persist its own event_id deduplication atomically with the host action; a crash
between action and ack can repeat delivery. Exactly-once external effects are not
promised.

## n8n configuration sample

Import `examples/n8n-monitoring.json` only after configuring the local file path,
consumer ID and your host action adapter. It is inactive and has a Manual Trigger.
The input is a local JSON envelope written from `events claim`, not an unclaimed
outbox dump. The reader rejects foreign/unleased events and missing, non-string, invalid or expired lease timestamps before yielding items to the action adapter. The action adapter node
fails closed until explicitly implemented; no recipient, secret, endpoint or
outbound-send node is preconfigured. Use event_id for durable receiver idempotency.
After an adapter returns `{host_action_succeeded: true, event_id, consumer_id}`, the
last node produces an explicit ack argv for the host to execute. It does not
execute a shell command. Configure your host bridge to execute that argv after
success, or run the ack CLI yourself. Importing the sample alone is not a working
notification integration. An action timeout or failure goes to retry, not ack.
