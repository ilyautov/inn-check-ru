---
status: in_progress
---
# Phase 0 Audit regressions and execution safety

## Task 0A Financial correctness

Ownership: scripts/fin_scoring.py, scripts/profiles.py, their existing evals and new focused regression eval. Other files require coordination.

- [x] Reproduce audit H2/H3/H4 against current source before edits. Consider data-shape mismatch, missingness propagation and recommendation generation separately.
- [x] Add failing tests for 2025/2023 not being consecutive; duplicate years and yearly growth needing Y-1.
- [x] Preserve unknown for absent staff, charter capital and previous equity; zero staff from verified FNS dumps is recognized with its year/provenance.
- [x] Apply applicability and required-signal/override policy consistently for manual and automated unknown fields.
- [x] Generate recommendations only from checked facts; a missing mass-head fact cannot yield "не массовый" or green in the affected required profile.
- [x] Run covering existing and new evals and ruff; record red/green evidence and changed interface decisions for later contracts.
- [x] Independent code review before completion.

## Task 0B MCP and test gate safety

Ownership: mcp/tools_impl.py, mcp test files, scripts/check_access.py, eval/run_access_eval.py, eval/run_fetch_eval.py, README.md, README.en.md, and a new safety regression eval. Do not edit the financial ownership files.

- [ ] Reproduce option/path injection and cap bypass with disposable files; add failing tests that require no unexpected writes, file expansion or network calls.
- [ ] Validate every batch input as an INN before invocation; distinguish API misuse from an ordinary invalid-ID response. Enforce the real input count limit.
- [ ] Cancel/reap the complete subprocess group on timeout, with Windows handling and no surviving fetch child.
- [ ] Fix parallel spy to retain actual opener references, preventing identity reuse false positives.
- [ ] Respect cache/source availability in canaries: absent index is an explicit non-run/coverage reason, schema violation is failure; preserve meaningful failure for sources that should have answered.
- [ ] Correct manual installation to include data/ and test copied installation outside checkout.
- [ ] Run relevant evals and ruff; record negative-path evidence.
- [ ] Independent security/code review before completion.

## Exit evidence

Both tasks independently reviewed, all prior compatible evals pass, all original audit repros fail to reproduce the defect, no regression of wire contracts or offline guarantees. CI edits, if required, must include the new regressions. Full objective continues with Phase 1.
