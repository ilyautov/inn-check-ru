---
status: completed
---
# Phase 3: Installed diagnostics and cache policies Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Independent review is required before completion. Root coordinates shared paths and commits.

**Goal:** Produce a safe, executable preparation plan for an installed package and selected profile, with per-dataset freshness.

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

**Dependencies:** Phase 1 policy semantics; can run alongside Phase 2 until CLI integration.

**Scope ownership:** Own cache_policy.py/workflow_doctor.py/cache_policy.json and refresh_indexes scheduling; coordinate workflow_cli.py edits with Phase 2.


## Task 3A: Dataset inventory and TTL-aware plan

**Files:** Create `scripts/cache_policy.py`, `data/cache_policy.json`, `eval/run_cache_policy_eval.py`; modify `scripts/refresh_indexes.py`, resource readers in `scripts/sources.py` as necessary.

**Consumes:** Existing 14 updater definitions and actual cache metadata/TTL constants, profile source requirements. **Produces:** `status(profile, *, now, root=None)->dict`, `plan(profile, *, now, root=None)->dict`, `execute(dataset_ids, *, profile, max_bytes, timeout_seconds, root=None)->dict`.

- [x] Add a table-driven inventory test for every registered updater: state, metadata source, TTL rationale/version, size known or unknown, RF network requirement, applicable profiles and exact argv. Unknown values remain null with reason.

```python
from cache_policy import status, plan
from unittest.mock import patch
import tempfile

with tempfile.TemporaryDirectory() as root:
    with patch("urllib.request.urlopen", side_effect=AssertionError("status used network")):
        report = status("нейтрально", now="2026-10-16T09:00:00Z", root=root)
        preparation = plan("нейтрально", now="2026-10-16T09:00:00Z", root=root)
    assert report["schema_version"] == 1
    assert all(item["state"] in {"missing", "fresh", "stale", "unknown", "disabled"}
               for item in report["datasets"])
    assert preparation["executes_updates"] is False
    assert all(isinstance(action["argv"], list) for action in preparation["actions"])
```

- [x] Run focused eval red. Implement pure reads and profile-specific plans. Keep large optional datasets separate from minimal scenario; plan never enables a disabled source. TTL sanctions <=7d, schedule with 1d margin; probe 24h and network fingerprint stays separate. Annual observation period is unaffected by update frequency. Unknown size must not bypass max_bytes during download.
- [x] Test boundary times, repeated status no filesystem changes, mixed 7d/monthly datasets and explicit stale current negatives. Run `eval/run_refresh_indexes_eval.py`, sanctions eval and cache eval. Review policy rationales; no unsupported freshness number is invented.

## Task 3B: Transactional updates and schedule generation

**Files:** Continue `scripts/cache_policy.py`, `scripts/refresh_indexes.py`; modify individual updater only if its existing atomic contract fails (coordinate ownership); create `eval/run_cache_update_eval.py`.

**Consumes:** exact selected dataset ids and max_bytes/timeout_seconds. **Produces:** update report `datasets, bytes_downloaded, elapsed_seconds, preserved_previous, errors`; generated cron/plist/Windows XML through shared schedule helper used in Phase 4.

- [x] Add download fault at byte boundary, schema validation failure, disk full simulation, timeout and two competing updates. Last-known-good cache digest must remain unchanged; orphan staging directories are not treated as valid.

```python
before = cache_file.read_bytes()
report = execute([dataset_id], profile="нейтрально", max_bytes=1024,
                 timeout_seconds=1, root=cache_root)
assert report["errors"]
assert report["preserved_previous"] is True
assert cache_file.read_bytes() == before
```

The eval creates cache_file/cache_root/dataset_id with a registered updater fixture and injects a bounded failing local transport; it never contacts a live registry.

- [x] Run red. Reuse updater functions with staging destination, validate then atomic promotion. root must route both status reads and every updater write to that root through supported flags or a narrowly scoped child environment; never mutate process HOME or silently use the user cache. Add a sentinel test proving no writes escape the temp root; enforce total and per-dataset budgets and cancellation. No shell command from URL/metadata. Schedule output only, no actual scheduler registration. Earliest due time accounts for TTL margin, not a universal monthly day.
- [x] Run focused eval and existing affected updater evals once; verify schedule next occurrence precedes sanctions expiration across a month boundary. Record emitted commands for Linux/macOS/Windows and review escaping.

## Task 3C: Doctor from installed resources and CLI

**Files:** Create `scripts/workflow_doctor.py`, `eval/run_workflow_doctor_eval.py`; adapt `mcp/doctor.py` to delegate; coordinate `scripts/workflow_cli.py` subcommands.

**Consumes:** cache status/plan, importlib.resources and package metadata, sources/profile registry. **Produces:** `inspect(profile="нейтрально", *, probe=False, now=None)->dict`; `doctor`, `cache status`, `cache plan`, `cache update` CLI subcommands.

- [x] Add no-SDK/core-only test, optional missing key, incompatible SDK and broken FastMCP import. Create isolated wheel environment outside checkout; report core_ready independently from sdk_ready.

```python
from workflow_doctor import inspect
report = inspect("нейтрально", probe=False, now="2026-10-16T09:00:00Z")
assert report["schema_version"] == 1
assert set(report["sections"]) == {
    "installation", "core", "sdk", "cache", "automatic", "manual", "optional", "network"}
assert report["network"]["probed"] is False
assert "next_actions" in report
```

- [x] Run red; implement explicit SDK version/import checks without importing SDK for core diagnostics. Use actual installed resources, not `repo/scripts` assumptions. Report presence/source of key only; redact sentinel key values and proxy userinfo even in exception messages. `--probe` alone enables network and cache writing.
- [x] Verify commands below in clean HOME/temp venv Python 3.10/3.12, with/without extra and incompatible SDK fixture. Core functionality passes without API key; executable next actions identify OS and missing optional binary.

```bash
python3 -m workflow_cli doctor --profile нейтрально --json
python3 -m workflow_cli cache status --profile нейтрально --json
python3 -m workflow_cli cache plan --profile нейтрально --json
python3 -m workflow_cli cache update --dataset sanctions --profile нейтрально --max-bytes 50000000 --timeout-seconds 120 --json
```

Dataset tokens must come from plan output; the `sanctions` example is bound to the canonical id in cache_policy.json, with aliases to existing updater names if needed. Tests compare emitted argv against parser acceptance. Do not run example update on user's actual cache just to verify syntax.

## Exit evidence

Installed package can explain minimal readiness, optional gaps and exact safe preparation commands. Interrupted update preserves cache; per-dataset schedule respects TTL. This proves local preparation policy, not registry publication latency or live geographic reachability.

## Как читать отметки о выполнении

Отметки фиксируют программную реализацию, проверенные контракты и написанные протоколы. Они не означают набора участников, получения реальных документов, независимой разметки, завершённых горизонтов наблюдения или работы на реальном хосте. Правила охватывают все 14 зарегистрированных обновляющих скриптов. Проверка готовности и плана без сети и транзакционное обновление не устанавливают задержку публикации реестров или доступность из конкретной географии.

Итоговые команды, снимки исходных файлов, область ревью, хеши артефактов и фактические результаты на платформах: [acceptance.md](acceptance.md), [acceptance.json](acceptance.json).
