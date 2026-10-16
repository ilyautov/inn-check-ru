---
status: in_progress
---
# Phase 6: Actual resolver evaluation, fault campaigns and replay Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Independent review is required before completion. Root coordinates shared paths and commits.

**Goal:** Measure final emitted claims and preserve point-in-time decision history without overstating synthetic or historical evidence.

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

**Dependencies:** Phases 1–5; current thresholds freeze before outcome evaluation.

**Scope ownership:** Own new evaluation/replay/benchmark files and research protocols; existing business rules change only through reviewed bug fixes coordinated by root.


## Task 6A: End-to-end scenario corpus and knowledge invariants

**Files:** Create `eval/run_workflow_e2e_eval.py`, `eval/run_knowledge_invariants_eval.py`, `eval/fixtures/workflow-scenarios.json`, `benchmark/decision_bench.py`, `benchmark/decision-rubric.json`, `benchmark/decision-corpus/manifest.json`, `benchmark/decision-benchmark.md`.

**Consumes:** actual score → resolve → workflow CLI/MCP → snapshot/dossier, existing recorded source fixtures. **Produces:** JSON report `schema_version,engine_hash,rules_hash,corpus_hash,seed,clock,counts,metrics,exclusions,cases`; denominator for each metric. `decision_bench.evaluate(cases: list[dict], *, now: str)->dict` must call production APIs, not calibration/measured_svetofor.

- [ ] Fix rubric and hashes before run. Cases cover full/simplified/IP/bank/NPO/restricted, known positive/complete negative/unknown, current/repaid debt, bankruptcy subject roles, conflicts, mixed periods, missing required risk, manual refusal, incomplete pages/index and raw loss. Case stores source hashes/licence, expected atomic claims, evidence scope, oracle status, independent review/adjudication fields.
- [ ] Add mutation tests for removed necessary leaf, mirrored source duplication, provider order reversal, consistent unit conversion, reload of annual data, timeout, truncation, unknown enum and incomplete index. Keep minimal failing input with fixed seed/clock.

```python
from benchmark.decision_bench import evaluate
report = evaluate(cases, now="2026-10-16T09:00:00Z")
assert report["counts"]["evaluated_cases"] == len(cases)
assert report["metrics"]["unsupported_assertions"]["numerator"] == 0
assert report["metrics"]["false_green_required_unknown"]["numerator"] == 0
assert report["metrics"]["coverage"]["denominator"] > 0
assert "abstention" in report["metrics"]
```

`cases` comes from the frozen manifest; exclusions are separately counted, not silently removed from denominator. Add a deliberate runtime mutation adapter that converts unknown to absent and one dropping evidence: each must make the above gate fail. Restore production logic after mutation test automatically.

- [ ] Run all new evals red, then implement harness and actual serialized-output assertions. Interpret “less evidence cannot increase certainty” correctly: red may become unknown; do not require a colour monotonicity false invariant. Match each affirmative recommendation sentence to claim ids in rendered dossier model and text. Distinguish transport errors/unsupported features/missing oracle/factual disagreement.
- [ ] Run new evals, existing calibration/benchmark/svetofor evals to preserve old published results. Publish counts, exclusions and matched-input coverage/abstention comparison, not only a zero-error headline. Synthetic target corpus is separate from representative measured frequency.

## Task 6B: Historical vintages and two replay modes

**Files:** Create `scripts/historical_replay.py`, `eval/run_historical_replay_eval.py`, `references/historical-replay.md`; adapt `scripts/retro_verdict.py` to label current-rules behavior; add CLI replay and MCP `counterparty_run_replay` read-only or export-specific according to behavior.

**Consumes:** immutable run revisions, manifest/raw hashes, published_at/first_observed_at, current rules. **Produces:** `replay(run_id: str, *, mode: str, cutoff: str, root=None)->dict`; mode `original|current_rules`. Result includes source_revision, original_rules_version, applied_rules_version, cutoff, exclusions, data and point_in_time_status.

- [ ] Add synthetic 2021 financial form first observed after 2022 cutoff; it must not enter eligible historic data. Add later corrected edition; original replay remains byte-equal. Change threshold in temporary fixture resource and assert new current-rules manifest differs without rewriting original.

```python
from historical_replay import replay
original = replay(run_id, mode="original", cutoff="2026-10-16T09:00:00Z", root=root)
current = replay(run_id, mode="current_rules", cutoff="2026-10-16T09:00:00Z", root=root)
assert original["data"] == saved_decision
assert original["source_revision"] == current["source_revision"]
assert original["original_rules_version"] == saved_rules_hash
assert original["point_in_time_status"] in {"verified", "partial", "unverified"}
assert network_calls == 0
```

- [ ] Run red. Do not equate reporting year/actualBfoDate to proven public availability without documented semantics. Original returns stored decision; current_rules is an explicit replay artifact, not original historical outcome. Claims with unknown publication/observation are excluded or clearly unverified. Persist later vintages prospectively; no fictional reconstruction of missing archive.
- [ ] Run replay/retro/snapshot/evidence evals with no-network audit hook. Obtain two genuinely dated editions when available, retain permission/hash provenance; until then mark real-vintage validation awaiting_external_evidence, not complete from synthetic correction.

## Task 6C: Independent reference corpus and prospective outcome protocol

**Files:** Create `benchmark/reference-review.schema.json`, `benchmark/outcome-study-protocol.md`, `benchmark/outcome-cohort.schema.json`, `benchmark/evaluate_outcomes.py`, `eval/run_outcome_protocol_eval.py`; update `references/kalibrovka.md` only with scope clarification, not invented findings.

**Consumes:** fixed engine/rules manifests and voluntarily acquired primary documents. **Produces:** `validate_cohort(rows: list[dict], protocol: dict)->list[dict]`, `evaluate_outcomes(rows: list[dict], protocol: dict)->dict`. Outcome labels distinct: case_filed/procedure_opened/liquidation/client_loss, timestamps, horizon_end, censoring, reference evidence. Report two different metrics P(outcome|green) and P(green|outcome), never relabel one as the other.

- [ ] Register sampling frame, target CI width/event requirement, confidence method, freeze date/hashes, disjoint subject/group split, attrition, weights, form/size/sector/no-reporting strata and adjudication before reading outcomes. Seed fixtures have synthetic:true and cannot be included in real report. Require two independent reviewers for disputed critical facts and adjudicator when disagreeing.

```python
from benchmark.evaluate_outcomes import validate_cohort
rows = [{"inn": "7707083893", "split": "train", "group_id": "g1"},
        {"inn": "7707083893", "split": "test", "group_id": "g1"}]
errors = validate_cohort(rows, protocol)
assert any(error["code"] == "subject_split_overlap" for error in errors)
```

- [ ] Add censored horizon cannot be marked survived; missing outcome oracle excluded with reason; leakage cutoff and repeated group split fail. Run `python3 eval/run_outcome_protocol_eval.py` red then implement validator/metrics with stdlib math/random, documented uncertainty and no training on held-out labels.
- [ ] Implement schema/protocol and synthetic evaluator tests now. Actual independent labels, sufficient events, elapsed horizons and external reviewers remain E-ORACLE/E-OUTCOME experiments in ledger. Empty/immature input emits `insufficient_evidence`, never zero default risk. No live bankruptcies, interviews or consent are fabricated by tests.
- [ ] Run focused eval and independent review of statistical definitions. Publish old calibration and new prospective mechanism separately; numerical transferability is not claimed until external evidence exists.

## Exit evidence

Declared corpus gates final statements, unknown preservation and traceability; intentional faults are detected. Historical replay preserves bytes and dates, and outcome instrumentation enforces no leakage/censoring mistakes. Independent empirical accuracy is separately pending until required observations and references are obtained.
