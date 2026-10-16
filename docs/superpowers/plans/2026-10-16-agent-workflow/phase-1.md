---
status: in_progress
---
# Phase 1: Evidence, time, applicability and decisions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Independent review is required before completion. Root coordinates shared paths and commits.

**Goal:** Trace every decision-relevant statement to real inputs, dates, applicability and authoritative scorer/resolver outcomes.

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

**Dependencies:** Phase 0 financial fixes; Phase 0 process changes may finish independently.

**Scope ownership:** Own evidence_contract.py, evidence_policy.json and evidence integration into fetch/scorer/resolver/snapshot/dossier; coordinate profiles.py/fin_scoring.py with Phase 0 before editing.


## Task 1A: Canonical field evidence and real raw references

**Files:** Create only `scripts/evidence_contract.py`, `data/evidence_policy.json`, `eval/run_evidence_contract_eval.py`. Do not edit existing production modules until Phase 0 review completes. Raw-capture integration into `scripts/fetch_counterparty.py` and `scripts/evidence_pack.py` belongs to Task 1C after that gate; 1A consumes recorded raw_refs directly.

**Consumes:** Existing raw recording and `evidence_pack.записать_пакет(каталог, inn, fetch, ответы, штамп=None, now=None)`; `fin['оценки_сигналов']` and `версия_оценок=1` from Phase 0.

**Produces:** `enrich(fetch: dict, *, now: str, raw_refs: dict | None = None) -> dict`; `validate_evidence(fetch: dict) -> list[dict]`. Full Claim schema is fixed in design; new fields cannot silently change it.

- [ ] Add this independent missingness test plus a recorded-response test that resolves the locator against saved raw bytes and checks its transformation; seed raw hash mismatch and a locator pointing to another company.

```python
import copy
import unittest
from evidence_contract import enrich, validate_evidence

class EvidenceTests(unittest.TestCase):
    def test_zero_is_a_fact_but_null_is_not(self):
        source = {"инн": "7707083893", "дампы_фнс": {"численность_сотрудников": 0},
                  "_доступность": {"дампы_фнс": {"состояние": "ok"}}}
        original = copy.deepcopy(source)
        out = enrich(source, now="2026-10-16T09:00:00Z")
        claims = list(out["_evidence"]["claims"].values())
        staff = [c for c in claims if c["field"] == "дампы_фнс.численность_сотрудников"]
        self.assertEqual(staff[0]["value"], 0)
        self.assertEqual(staff[0]["knowledge"], "known")
        self.assertEqual(staff[0]["raw_ref"]["availability"], "raw_unavailable")
        self.assertEqual(source, original)
        self.assertEqual(validate_evidence(out), [])
```

- [ ] Run `python3 eval/run_evidence_contract_eval.py`; first run must fail because the contract is absent, not because imports accidentally resolve another checkout. Record command/hash/output in `work/implementation-reports/phase-1.json`.
- [ ] Implement canonical JSON hashing, typed dictionaries, deep-copy adaptation and checked raw lookup. Map real response ids by captured source/subject/request, then verify every locator resolves; do not fabricate `/field` pointers from normalized JSON. Preserve raw_unavailable for older files. Enforce max dependencies, detect dangling refs/cycles, reject foreign subject refs.
- [ ] Migrate status, unreliability, bankruptcy roles, current/repaid debt, sanctions, capital, revenue, staff, financial leaf fields and every decisive path in `data/profiles_ru.json`. Add a coverage test enumerating profile paths and rule dependencies; an unsupported path fails the gate or returns explicit unknown, never drops from the matrix.
- [ ] Run the focused eval and `python3 eval/run_evidence_pack_eval.py`; require real raw hashes/locators, original input unmodified, corrupt refs diagnosed and old packs still verified. Request review; root coordinates commit after approval.

## Task 1B: Time, lineage, conflicts and applicability

**Files:** Continue `scripts/evidence_contract.py`, `data/evidence_policy.json`, `eval/run_evidence_contract_eval.py`; modify `scripts/sources.py` only for lineage/coverage metadata. Do not redefine source dependency ordering as origin_family.

**Consumes:** Claim schema from 1A, real source date fields and existing TTL constants. **Produces:** `_evidence.conflicts` and `_evidence.applicability`; `reason_code` semantics fixed by design.

- [ ] Add frozen-clock cases from this table before implementation.

| Input | Required result |
|---|---|
| Reload 2025 annual staff today | period unchanged, only retrieved_at changes |
| Future passport label | raw preserved, not published_at/known-at |
| 2025 revenue + 2023 staff | period_mismatch for the derived rule |
| TTL boundary at exact expiration | stale at/after expiration; UTC comparison |
| Missing date | null/unknown, no current freshness assertion |
| Two mirrors of same EGRUL record | one known origin family, not two confirmations |
| Reversed provider order with conflicting values | same conflict set and decision |
| Old positive bankruptcy + fresh missing source | historical positive retained; current coverage incomplete |
| Full LLC / simplified / IP / bank / NPO / restricted | explicit applicability and correct denominator |

```python
from evidence_contract import enrich

def test_reloading_does_not_rename_the_period():
    fetch = {"инн": "7707083893", "дампы_фнс": {
        "численность_сотрудников": 3, "год": 2025},
        "_доступность": {"дампы_фнс": {"состояние": "ok"}}}
    first = enrich(fetch, now="2026-10-16T09:00:00Z")
    second = enrich(fetch, now="2026-11-16T09:00:00Z")
    def staff(result):
        return next(c for c in result["_evidence"]["claims"].values()
                    if c["field"] == "дампы_фнс.численность_сотрудников")
    assert staff(first)["period"] == staff(second)["period"]
    assert staff(first)["retrieved_at"] != staff(second)["retrieved_at"]
```

- [ ] Run focused eval red. Implement policy metadata with version and source rationale. Freshness is claim-specific; no single TTL for all financial and registry facts. Unknown family is not independent. Conflict resolution records all candidate ids and selected id/reason or null.
- [ ] Add form applicability without inventing bank ratios or inferring form solely from OKVED. `not_measurable_for_form`, `not_applicable`, missing publication and unsupported form are separate. Publish support matrix in `references/evidence-contract.md`; flag any form lacking checked real sample as unverified.
- [ ] Run focused eval, `eval/run_fin_eval.py`, `eval/run_profiles_eval.py`; compare reversal tests, UTC boundaries and all six form segments. Review before marking complete.

## Task 1C: Authoritative decision metadata and preservation to final output

**Files:** Modify `scripts/fin_scoring.py`, `scripts/profiles.py`, `scripts/snapshot.py`, `scripts/dossier.py`, `scripts/fetch_counterparty.py`, `scripts/evidence_pack.py`; continue `scripts/evidence_contract.py`; create `eval/run_decision_contract_eval.py`, `references/evidence-contract.md`.

**Consumes:** Existing score/resolve APIs and Phase 0 statuses. **Produces:** `evaluate(fetch: dict, *, profile: str, now: str) -> dict` with exactly `{fetch, fin, assessment, metadata}`. `metadata` contains schema_version, engine_version, rules_version, evaluated_at, decision. `fetch._decision == assessment._decision == metadata.decision`. No second risk computation.

- [ ] Add a regression that removes the only mass-head field from the existing clean profile fixture; assert the final recommendation does not say “не массовый”, the required gap appears in metadata, and the dossier preserves it. Test transit lists both staff and revenue dependencies and their years. Neutral stays facts_only.

```python
from evidence_contract import evaluate
from snapshot import fingerprint

def test_neutral_is_not_a_deal_recommendation():
    materialized = evaluate({"инн": "7707083893"},
                            profile="нейтрально", now="2026-10-16T09:00:00Z")
    assert set(materialized) == {"fetch", "fin", "assessment", "metadata"}
    decision = materialized["metadata"]["decision"]
    assert decision["decision_status"] == "facts_only"
    assert decision["can_recommend"] is False
    assert materialized["fetch"]["_decision"] == decision
    assert materialized["assessment"]["_decision"] == decision
    snap = fingerprint(materialized["fetch"], дата="2026-10-16T09:00:00Z")
    assert snap["_decision"] == decision
    assert snap["_evidence"]["schema_version"] == 1
```

- [ ] Run `python3 eval/run_decision_contract_eval.py` red. Add optional metadata to existing results and snapshot fingerprints; older snapshots remain readable. Stable rules_version hashes actual profile/canon/policy contents.
- [ ] Render decisive findings and decisive absences with evidence ids, periods, scope, conflicts and gaps in Markdown/DOCX model. A recommendation may only use asserted supported facts. `can_recommend` must depend on applicable required rules, including severity overrides, not total source count.
- [ ] Run both new evals plus existing fin/profiles/dossier/diff/retro evals and ruff on changed files; compare same inputs before/after for coverage and abstention. Preserve legacy API shape and explicit red Phase 0 regressions.

## Exit evidence

All decisive profile paths are represented; every supported claim traces to all inputs or explicitly declares unavailable raw; negative claims require complete scope; unknown survives scorer → resolver → snapshot → dossier. This verifies the declared contract, not factual truth of registries or legal validity of unreviewed reporting forms.
