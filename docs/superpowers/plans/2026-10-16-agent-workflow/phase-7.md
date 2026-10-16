---
status: completed
---
# Phase 7: Packaging, documentation and full acceptance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Independent review is required before completion. Root coordinates shared paths and commits.

**Goal:** Ship one coherent capability set across documented channels and distinguish verified software from pending human/outcome experiments.

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

**Dependencies:** Phases 0–6; all integrations reviewed; full matrix traced.

**Scope ownership:** Own pyproject/package manifests/CI/docs/version parity and acceptance ledger; coordinate existing README changes with Phase 0 safety worker. Production bug fixes return to feature owner.


## Task 7A: Installed artifact parity and platform matrix

**Files:** Modify `pyproject.toml`, `packaging/inn-check-ru-mcp/pyproject.toml`, `packaging/mcpb/manifest.template.json`, `.claude-plugin/plugin.json`, `.mcp.json`, `server.json`, `Dockerfile`, `scripts/build_release_zip.py`, `eval/run_version_gate.py`, `.github/workflows/ci.yml`, `.github/workflows/canary.yml`; create `data/workflow_capabilities.json`, `eval/run_installed_workflow_eval.py`, `eval/run_capability_parity_eval.py`. Modify only manifests that actually exist; confirm with rg before editing.

**Consumes:** modules/entry points and schema snapshots from earlier phases. **Produces:** core wheel includes all new flat modules/data; `inn-check-workflow = "workflow_cli:cli"`; MCP optional extra pinned SDK1; optional pdf extra; matching version/channel feature matrix. No publish action implicit.

- [x] Add packaging test that builds wheel and release ZIP, installs outside checkout with empty HOME and asserts module origins under venv. Confirm every documented command is available through installed executable, not PYTHONPATH backdoor.

```python
import importlib.util
import json
import subprocess
import sys
from importlib.resources import files
spec = importlib.util.find_spec("run_workflow")
assert spec and "site-packages" in spec.origin
caps = json.loads(files("inn_check_ru_data").joinpath("workflow_capabilities.json").read_text())
assert caps["schema_version"] == 1
result = subprocess.run([sys.executable, "-m", "workflow_cli", "doctor", "--json"],
                        text=True, capture_output=True, check=False)
assert json.loads(result.stdout)["schema_version"] == 1
```

- [x] Run red; include new modules/data and entry point. Align Checko/DaData/proxy/run-store config across plugin, MCPB, plain MCP, Docker and CLI, or mark channel's unsupported setting explicitly in matrix. Secret placeholders never become persisted values. Version derives from the current project source; use next version chosen by release owner, not a fabricated published release.
- [x] Test installed synthetic route: doctor/cache plan → start with injected recorded transport → new process → manual → resume → document → snapshot → selected refresh → diff → event ack/retry → replay. Installed transport fixture is a test-only adapter, not public arbitrary executable/config escape. Test CLI and stdio tools; verify read/resume have 0 source calls and same materialized revision.
- [x] Add Linux/macOS/Windows × Python 3.10/3.12 core matrix; bounded MCP SDK supported/no-SDK/incompatible matrix. Existing wheel/uvx/MCPB/Docker checks remain. Windows timeout descendant test must run on Windows. Canary is separate live job with geographic/source coverage and skip reasons; network failure does not masquerade as schema failure or offline pass.
- [x] Run new evals, complete compatible existing eval suite, ruff, skill/workflow/version gates once after final changes. Build artifacts, inspect names/content/hashes and scan secret sentinels. Record local vs CI vs real-host evidence; missing platforms remain not_run, not assumed pass.

## Task 7B: Documentation, security boundaries and recovery

**Files:** Modify `README.md`, `README.en.md`, `SKILL.md`, `mcp/README.md`, `extension/README.md`, `PUBLISHING.md`, `KNOWN_LIMITS.md`, `CHANGELOG.md`, relevant `commands/`; create `references/agent-workflow.md`, `references/workflow-recovery.md`, `references/capability-matrix.md`.

**Consumes:** actual installed commands and capability matrix. **Produces:** copyable Russian main path, configuration examples and security/recovery runbook. Existing English documentation mirrors actual scope, not invented claims.

- [x] Add doc gate using command parser checks and link resolution. Remove “fact = three sources” and universal “all tools read-only”; explain primary/mirror lineage, partial decisions and actual writes. Distinguish ordinary report, signed official extract and byte-integrity archive. Explicitly label expert rules vs validated probabilities.
- [x] Verify examples below end to end with real output ids in temporary installation; document retrieval of ids, operation UUID creation, safe optional keys and blocked source next action.

```bash
inn-check-workflow doctor --profile нейтрально --json
inn-check-workflow cache plan --profile нейтрально --json
inn-check-workflow run start 7707083893 --profile нейтрально --json
inn-check-workflow run resume "$RUN_ID" --json
inn-check-workflow run render "$RUN_ID" --format markdown --json
inn-check-workflow events list --json
```

- [x] Run recovery drill: copy a quiescent run directory with committed HEAD, restore into new root, verify all revision/raw/artifact hashes, replay original without network. Roll back the package in a temporary venv and prove the store remains byte-identical. The previous 1.12.3 source release has no workflow API: verify the old one-shot CLI remains usable and the workflow entry point is unavailable, reporting unsupported-client rather than inventing an unsupported_schema response. Separately verify a schema-capable reader rejects an unknown future schema with unsupported_schema and no mutation. Record the real downgrade and any simulated older-schema reader as distinct evidence. Document retained raw vs retention TTL, concurrent-writer constraints, storage failure and lock cleanup.
- [x] Independent code/security review must cover path/argv injection, secrets, hostile document/page text, paid budget and descendant cancellation. Native host owns filesystem root, not page; annotations are hints. Address findings, run affected checks once, update acceptance evidence. No unsolicited schedule, external notification, publication or release upload.

## Task 7C: Local voluntary metrics and empirical acceptance protocol

**Files:** Create `scripts/workflow_metrics.py`, `eval/run_workflow_metrics_eval.py`, `docs/research/2026-10-16/pilot-protocol.md`, `docs/research/2026-10-16/experiment-status.json`; plan owner/root update requirements/status ledger.

**Consumes:** stage events only with explicit local opt-in; never raw documents/INN/keys. **Produces:** `record(event: dict, *, enabled: bool=False, root=None)->dict`, `summarize(*, root=None)->dict`; event schema `{session_id,stage,active_seconds,waiting_seconds,manual_steps,outcome,reason_code}`. Storage/export is local and separately enabled; no telemetry endpoint.

- [x] Test disabled record produces zero file writes; unexpected fields containing subject/credentials rejected; duration nonnegative and active/wait separate; failed install and abandonment included in denominator.

```python
import tempfile
from pathlib import Path
from workflow_metrics import record
with tempfile.TemporaryDirectory() as root:
    report = record({"session_id": "pilot-01", "stage": "install", "active_seconds": 60,
                     "waiting_seconds": 30, "manual_steps": 1, "outcome": "abandoned",
                     "reason_code": "missing_optional_dependency"}, enabled=False, root=root)
    assert report["status"] == "disabled"
    assert list(Path(root).iterdir()) == []
```

- [x] Run red; implement strict allowlist, append-safe local metrics, pseudonymous session IDs and aggregate export only by explicit command. No automatic enrolment. Freeze pilot protocol and content-review rubric before sessions.
- [x] Protocol E-HUMAN: eight observed recent-task interviews, voluntary four-week use; count all starts/dropoffs. Ten new installations target eight users explaining refusal/next step unaided; five actual courts/FSSP resume scenarios after client close; three independent users repeat on a second real task. Record active vs wait time, app switches/manual transfers, final artifact/reasons stopped, traceable claims, coverage and refusals. Target values are organizational acceptance hypotheses, not powered population estimates.
- [x] Protocol E-DOCS: 20 consented anonymized real invoices/contracts; two reviewers identify intended side, per-page candidate truth and roles; report silent wrong-subject checks (target 0 on corpus), extraction errors and manual-selection fraction. Test output does not attest consent or supply reviewers.
- [x] Protocol E-EXPLAIN: reviewers recover every decisive dossier signal from raw refs without model assistance; users explain comparison choice with evidence and coverage. Do not infer human comprehension from presence of fields.
- [x] E-ORACLE/E-VINTAGE/E-OUTCOME follow Phase 6. Save consent-safe observed records when real sessions occur; until then experiment status is `awaiting_external_evidence` with zero observed count. Software can be complete while full empirical validation remains pending; final report states both without collapsing them.

## Final release gate and ledger update

- [x] Requirements matrix has a test/evidence link for every M1–M8, R1–R9, W1–W8, P1–P7, H1–H5/M1–M3 audit row, with no hidden deferred software.
- [x] Full software validation done on final revision; each unsupported/unrun platform/host explicitly listed. Code review findings resolved or recorded as blocking.
- [x] Installed artifacts and readable documents inspected; extension UI reviewed for readable labels/errors, themes and no clipping. Site claims match actual capabilities; no unrelated visual redesign.
- [x] Update `status.md` with commands, exact git hash, timestamps, artifact SHA-256, platform, observed result and limitations. Mark software tasks complete only with execution evidence; keep real-world experiments separate.
- [x] Root supplies final user report of changes, relevant verification, remaining empirical evidence and artifact/branch location. Publishing/installing schedulers or sending third-party messages requires its own already-authorized action; this plan does not perform them.

## Как читать отметки о выполнении

Отметки фиксируют программную реализацию, проверенные контракты и написанные протоколы. Они не означают набора участников, получения реальных документов, независимой разметки, завершённых горизонтов наблюдения или работы на реальном хосте. Отметки у Protocol E-* означают готовый протокол и проверенный инструментарий. Во всех восьми направлениях эмпирической проверки новых наблюдений 0; E-HOST not_run, остальные awaiting_external_evidence. Версия 1.13.0 не опубликована; PR остаётся в состоянии draft; регистрация планировщика, уведомления и загрузка релизных файлов не выполнялись.

Итоговые команды, снимки исходных файлов, область ревью, хеши артефактов и фактические результаты на платформах: [acceptance.md](acceptance.md), [acceptance.json](acceptance.json).
