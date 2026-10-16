---
status: completed
---
# Phase 2: Persisted runs and human handoff Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Independent review is required before completion. Root coordinates shared paths and commits.

**Goal:** Resume the same materialized check after process restart, import valid manual work and export derived artifacts without hidden recollection.

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

**Dependencies:** Phase 1 contracts; Phase 3 readiness fields integrate after its merge.

**Scope ownership:** Own run_store.py/run_workflow.py/workflow_cli.py and extension/native-host workflow wiring. Phase 1 owns financial rules; Phase 4 owns MCP transport.


## Task 2A: Immutable local store and concurrency

**Files:** Create `scripts/run_store.py`, `eval/run_run_store_eval.py`. Later packaging owner adds these modules to wheel.

**Consumes:** Materialized `{fetch, fin, assessment, metadata}`. **Produces:** `create(payload, *, root=None, now=None)`, `read(run_id, *, revision=None, root=None)`, `append(run_id, payload, *, expected_revision, operation_id, root=None, now=None)` and `RunStoreError(code, message)`.

- [x] Add an isolated test with no network or global HOME mutation:

```python
import tempfile
import uuid
from pathlib import Path
from run_store import create, read, append, RunStoreError

with tempfile.TemporaryDirectory() as temporary:
    root = Path(temporary).resolve()  # configured storage uses a physical path
    payload = {"fetch": {"инн": "7707083893"}, "fin": {}, "assessment": {},
               "metadata": {"schema_version": 1}}
    one = create(payload, root=root, now="2026-10-16T09:00:00Z")
    op = str(uuid.uuid4())
    two = append(one["run_id"], payload, expected_revision=1,
                 operation_id=op, root=root, now="2026-10-16T09:01:00Z")
    duplicate = append(one["run_id"], payload, expected_revision=1,
                       operation_id=op, root=root, now="2026-10-16T09:01:00Z")
    assert two == duplicate
    assert read(one["run_id"], revision=1, root=root)["payload"] == payload
    assert read(one["run_id"], root=root)["revision"] == 2
    try:
        append(one["run_id"], payload, expected_revision=1,
               operation_id=str(uuid.uuid4()), root=root)
    except RunStoreError as error:
        assert error.code == "revision_conflict"
    else:
        raise AssertionError("stale writer was accepted")
```

- [x] Run `python3 eval/run_run_store_eval.py` red; add subprocess restart, simultaneous writers (barrier), killed writer before/after revision replace and before HEAD replace, corrupt HEAD/hash, operation id collision with different bytes, oversized JSON and traversal/symlink tests.
- [x] Implement UUID validation, root confinement, stdlib OS locks, canonical redaction, temp/fsync/replace and hash-linked revisions exactly as design. read never mkdir/writes. CAS errors return no partial visible HEAD; orphan revisions stay uncommitted. Retry matching operation succeeds even with stale expected revision. Keep operation index derived from committed history.
- [x] Run focused eval on Linux/macOS/Windows CI; record local platform only until CI exists. Verify redaction sentinels in every file, including `_token_pb`, Authorization and proxy URL userinfo. Root coordinates review/commit.

## Task 2B: Workflow orchestration and explicit exports

**Files:** Create `scripts/run_workflow.py`, `scripts/workflow_cli.py`, `eval/run_run_workflow_eval.py`; modify selected-source collection helper in `scripts/fetch_counterparty.py` only after Phase 1 handoff; extend `scripts/snapshot.py`/`scripts/evidence_pack.py` only at persistence adapters.

**Consumes:** store and evaluate contracts. **Produces:** start/resume/import_manual/refresh/render/export/save_snapshot signatures in design; JSON envelope with `data=materialized payload` for start/resume/import/refresh, `data={format, content, source_revision}` for render, `data={artifact_id, format, sha256, source_revision}` for export.

- [x] Test a collector stub returning recorded source data with raw capture. Persist start, exit child, import existing manual validator output in a new child, render and snapshot. Patch all network entry points to raise during resume/render/import. Assert 0 calls, unchanged original raw/evidence digests and retained run id.

```python
import run_workflow
from unittest.mock import patch

def test_resume_never_collects(run_id, root):
    with patch("fetch_counterparty.collect", side_effect=AssertionError("unexpected recollect")):
        result = run_workflow.resume(run_id, root=root)
        document = run_workflow.render(run_id, format="markdown", root=root)
    assert result["run_id"] == run_id
    assert result["data"]["fetch"]["инн"] == "7707083893"
    assert document["data"]["source_revision"] == result["revision"]
```

The eval harness creates run_id/root before calling this function; no pytest fixture dependency is introduced.

- [x] Run new eval red. Implement orchestration via actual score/resolve/manual/dossier, not subprocess recollection for every operation. start persists collecting then completion/cancellation; existing raw capture writes a package under managed storage with real response ids. refresh resolves requested sources plus explicit prerequisites and returns their list; never silently calls full collect. Declined/incomplete actions persist as new revision but remain gaps.
- [x] Add CLI commands and validate public usage below in an isolated temp store; an id is data, never an argv fragment.

```bash
python3 -m workflow_cli doctor --profile нейтрально --json
python3 -m workflow_cli run start 7707083893 --profile нейтрально --json
python3 -m workflow_cli run show "$RUN_ID" --json
python3 -m workflow_cli run resume "$RUN_ID" --json
python3 -m workflow_cli run manual "$RUN_ID" --block-json manual.json --expected-revision 2 --operation-id "$OPERATION_ID" --json
python3 -m workflow_cli run render "$RUN_ID" --format markdown --json
python3 -m workflow_cli run export "$RUN_ID" --format docx --json
python3 -m workflow_cli run snapshot "$RUN_ID" --json
python3 -m workflow_cli run refresh "$RUN_ID" --source риски --expected-revision 3 --operation-id "$REFRESH_ID" --json
```

`RUN_ID`, `OPERATION_ID`, `REFRESH_ID` are outputs/new UUIDs; examples in docs explain obtaining them from JSON. Tests invoke argv lists directly, not shell substitution. `--store` is optional local CLI root; MCP never exposes it. Existing `inn-check-ru` entry point remains unchanged.

- [x] Run new eval, existing manual/evidence_pack/dossier/watchlist/retro evals. Test render no artifact write, explicit export atomic write, cancel leaves recoverable state, stale run shows refresh action without refreshing. Review integration.

## Task 2C: Shared pending actions and browser run binding

**Files:** Modify `scripts/native_host.py`, `extension/popup.html`, `extension/popup.js`, `extension/lib.js`, `extension/background.js`, `extension/README.md`; add `eval/run_manual_handoff_eval.py`; extend native-host/extension evals.

**Consumes:** run id/revision, pending_actions, native host allowlisted URL/INN validation. **Produces:** native commands `run_read` and `run_manual`, fixed field allowlists; existing commands remain. `run_manual` takes run_id, expected_revision, operation_id and validated block payload, never root/path/executable.

- [x] Add tests for unknown run, mismatch INN, disallowed URL, explicitly expired expires_at, incomplete pagination, refusal, retry identical operation and revision conflict. Page content including shell/HTML instructions is rendered as text and rejected as command fields.

```python
request = {"команда": "run_read", "run_id": run_id}
# The host test harness frames request with читать_кадр/писать_кадр.
response = roundtrip(request)
assert response["run_id"] == run_id
assert response["data"]["fetch"]["инн"] == expected_inn
assert "root" not in response
```

`roundtrip` is the existing native-host framed test helper or a local eval wrapper; it uses actual host subprocess stdin/stdout.

- [x] Run manual handoff eval red. Show selected run subject/name/profile/revision and source URL before the user confirms attachment. On conflict reload and ask explicit run selection in UI, never silently attach to latest unrelated run. Use storage fallback outside extension context. Keep PNG manifest icons.
- [x] Run native-host/extension evals plus real browser check when available: start from copied extension/native package, attach a block, close client, reopen and resume via MCP/CLI. Record real UI evidence separately from JS/headless checks. Visually review popup layout, long names, readable error/conflict states and light/dark consistency; report observed limits.

## Exit evidence

The five scripted courts/FSSP resume cases survive restart without lost work or hidden fetch; wrong INN refused, partial results remain partial, original evidence hash unchanged. Five actual user scenarios are a separate empirical gate in Phase 7. No claim of real Chrome/MCP host acceptance from framed-protocol tests alone.

## Как читать отметки о выполнении

Отметки фиксируют программную реализацию, проверенные контракты и написанные протоколы. Они не означают набора участников, получения реальных документов, независимой разметки, завершённых горизонтов наблюдения или работы на реальном хосте. Проверены пять сценариев передачи ручных результатов и 18 имитируемых состояний popup в светлой/тёмной теме при ширине 360px. Фактический путь через установленный Chrome, native-host и агентский клиент не выполнялся: E-HOST not_run; пользовательские задачи E-HUMAN ожидают внешних свидетельств.

Итоговые команды, снимки исходных файлов, область ревью, хеши артефактов и фактические результаты на платформах: [acceptance.md](acceptance.md), [acceptance.json](acceptance.json).
