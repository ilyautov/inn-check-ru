---
status: completed
---
# Phase 5: Optional documents, provider policy and comparison Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Independent review is required before completion. Root coordinates shared paths and commits.

**Goal:** Complete the approved convenience features while preserving explicit identity selection, provider limits and evidence explanations.

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

**Dependencies:** Phases 1–4 stable run/evidence contracts; each task can be reviewed separately.

**Scope ownership:** Document worker: document_input/extract_inn; provider worker: provider_policy/fetch aggregator; graph worker: compare_counterparties/affiliates_graph. Shared CLI/MCP edits are sequential under root.


## Task 5A: PDF/OCR and explicit side selection

**Files:** Create `scripts/document_input.py`, `eval/run_document_input_eval.py`, `references/document-input.md`; modify `scripts/extract_inn.py` at `прочитать_документ` and check dispatch; add optional extra in `pyproject.toml` via packaging owner. Create approved anonymized fixtures under `eval/fixtures/document-input/` with license/consent manifest; synthetic fixtures are labelled synthetic.

**Consumes:** `extract_inn.разобрать_текст(текст, источник, предел=...)`, existing checksum/context extraction. **Produces:** `extract(data: bytes, *, filename: str, ocr=False)->dict`, `select(candidates: list[dict], *, role: str, candidate_id: str | None = None)->dict`. Extraction schema: `{schema_version,status,candidates,pages,method,limitations}`; candidate: `{candidate_id,inn,kpp,ogrn,address,context,page,offsets,role_hints}`. Selection returns `{status,selected,candidates,reason_code}`; ambiguous -> needs_input/selected null. Role hints never authorize automatic network checking.

- [x] Write tests for text PDF, image-only PDF, encrypted/broken PDF, missing extra/Tesseract, overlarge/too-many-page input, OCR timeout, checksum mismatch and multiple own-company/bank/supplier identifiers.

```python
from document_input import select
candidates = [
    {"candidate_id": "supplier-1", "inn": "7707083893", "role_hints": ["supplier"]},
    {"candidate_id": "bank-1", "inn": "7702070139", "role_hints": ["bank"]},
]
ambiguous = select(candidates, role="supplier")
assert ambiguous["status"] == "needs_input"
assert ambiguous["selected"] is None
chosen = select(candidates, role="supplier", candidate_id="supplier-1")
assert chosen["status"] == "ok"
assert chosen["selected"]["inn"] == "7707083893"
```

- [x] Run `python3 eval/run_document_input_eval.py` red. Implement lazy optional imports; stdlib extraction unchanged. `pdf` extra uses a compatible bounded pypdf version selected against official documentation at execution; OCR invokes explicitly configured local renderer/Tesseract via argv list with timeout and descendant cancellation. Fixed limits: 20 MiB, 100 pages, 60 seconds. No network OCR; output content is untrusted data.
- [x] Add CLI `document extract <file> [--ocr] --json` and `document select <candidates.json> --role supplier --candidate <id> --json`. MCP accepts text or existing managed artifact id; arbitrary page-controlled filesystem paths remain forbidden. A selected subject can start a run only through explicit run start/check command, not extraction side effect.
- [x] Run extract/document/process safety evals both with and without extras. Measure candidate recall, silent wrong-party checks (target 0 on declared corpus), manual selection rate and extraction failures separately. The 20 real consented documents require actual acquisition/review in Phase 7; 20 synthetic PDFs do not fulfil that experiment.

## Task 5B: Provider allowlists, budget and fallback

**Files:** Create `scripts/provider_policy.py`, `data/provider_policy.schema.json`, `eval/run_provider_policy_eval.py`, `references/provider-policy.md`; modify `scripts/fetch_counterparty.py` at `_ключ_агрегатора`/`fetch_aggregator`, relevant graph calls; integrate export filtering in `scripts/dossier.py`/`scripts/evidence_pack.py` through Phase 2 owner.

**Consumes:** Existing Checko/DaData parsers and key configuration; no extra provider required. **Produces:** `validate_policy(policy: dict)->list[dict]`, `fetch_with_policy(inn: str, fields: list[str], policy: dict, *, request, ledger: dict)->dict`, `export_allowed(payload: dict, policy: dict)->dict`. `request(provider: str, inn: str, fields: list[str])->dict` returns `{status, data, reason_code, requests_used}` and is injected in tests. Return `{status,data,attempts,budget,transfers,limitations}`; ledger includes reserved/used counts and integer cost units.

- [x] Add refusal/exhaustion tests before adapter implementation:

```python
from provider_policy import fetch_with_policy
calls = []
def request(provider, inn, fields):
    calls.append(provider)
    return {"status": "error", "data": None, "reason_code": "quota_exhausted", "requests_used": 1}
policy = {"schema_version": 1, "total_cost_units": 1, "providers": [
    {"id": "checko", "enabled": True, "allowed_fields": ["name"], "export_fields": [],
     "request_limit": 1, "request_cost_units": 1, "fallback_on": []},
    {"id": "dadata", "enabled": False, "allowed_fields": ["name"], "export_fields": [],
     "request_limit": 1, "request_cost_units": 1, "fallback_on": []}]}
result = fetch_with_policy("7707083893", ["name"], policy, request=request, ledger={})
assert calls == ["checko"]
assert result["status"] == "partial"
assert result["budget"]["used_cost_units"] == 1
assert result["data"] is None
```

- [x] Run red. Validate all requested/exportable fields against explicit allowlists. Reserve before dispatch and count ambiguous timeout conservatively. Implement one explicit quota/unavailability fallback from Checko to DaData when enabled, field supported and total request/cost budget remains. 401/403 no retries; configured transient retry at most 1 and inside budget. Persist safe transfer history (provider/field names/time), never key or HTTP auth. No policy -> existing single-provider selection remains with no surprise fallback.
- [x] Apply budget to graph calls as well; preserve <=25 graph ceiling. Export filtering applies to derived documents and raw package (redacted omissions are labelled; original restricted local raw is not copied into permitted export). Provenance retains provider/origin distinction; inability to export does not fabricate missingness or erase original restricted run internally.
- [x] Run provider, fetch, graph, dossier/evidence pack evals; concurrent reservation test must not overspend. Secret sentinel and forbidden field tests scan all output channels. Document user-configured policy vs actual provider legal terms; do not invent commercial price/licence claims.

## Task 5C: Explainable graph and 2–5-company comparison

**Files:** Modify `scripts/affiliates_graph.py` parse_card/build_graph/normalize_offline and `scripts/dossier.py` graph presentation; create `scripts/compare_counterparties.py`, `eval/run_compare_eval.py`; extend `eval/run_graph_eval.py`; coordinate `counterparty_compare` MCP and CLI `compare` command.

**Consumes:** Saved runs/evidence, existing graph. **Produces:** `compare(run_ids: list[str], *, as_of: str, root=None)->dict`; edge metadata `edge_type,evidence_ids,effective_at,origin_family`, unexplored frontier/stop_reason. Result `{schema_version,profile,as_of,subjects,criteria,coverage_differences,limitations}`; no universal ranked risk score.

- [x] Add mismatched profile/date fixtures, incomplete company vs complete company, common address vs ownership, and depth/request truncation. Refuse one or six subjects, duplicate subject IDs and future-only revisions.

```python
from compare_counterparties import compare
result = compare([first_run_id, second_run_id],
                 as_of="2026-10-16T09:00:00Z", root=run_root)
assert result["profile"] == "предоплата"
assert len(result["subjects"]) == 2
assert result["coverage_differences"]
for criterion in result["criteria"]:
    assert "evidence_ids" in criterion
    assert "unknown" not in criterion.get("favourable_states", [])
```

The harness creates both saved runs with the existing `предоплата` profile; mismatched profiles have a separate rejection case.

- [x] Run red. Compare saved evidence only, select revisions available by as_of, surface actual fact periods and coverage. Different goals reject rather than compare unlike decisions. Unknown cannot appear as a favourable metric or better rank. Graph associations remain named: address never ownership; causal/beneficial ownership conclusions require supported ownership chain.
- [x] Run compare/graph/dossier/profiles evals; no network for comparison. Record frontier when max_nodes/max_depth/provider budget stops traversal. Real users explaining a choice are empirical Phase 7 evidence, not inferred from correct JSON.

## Exit evidence

Optional extraction does not burden core install; ambiguous role triggers selection; denied/budget-exhausted providers issue no extra calls; comparisons expose coverage and graph uncertainty. No claim of OCR accuracy on real documents or user preference without the recorded experiment.

## Как читать отметки о выполнении

Отметки фиксируют программную реализацию, проверенные контракты и написанные протоколы. Они не означают набора участников, получения реальных документов, независимой разметки, завершённых горизонтов наблюдения или работы на реальном хосте. Синтетические текстовые документы, PDF и сканы проверяют механизм и выбор стороны; согласия владельцев, 20 реальных документов и оценки качества OCR на них не получены. MCP принимает текст или доступный управляемый артефакт согласно матрице возможностей; публичного ввода PDF по пути или загрузкой у него нет.

Итоговые команды, снимки исходных файлов, область ревью, хеши артефактов и фактические результаты на платформах: [acceptance.md](acceptance.md), [acceptance.json](acceptance.json).
