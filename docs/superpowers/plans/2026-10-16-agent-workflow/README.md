# Reproducible Agent Workflow Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Follow test-driven-development and obtain independent code review before completing each task.

**Goal:** Implement the complete approved improvement research for the existing open-source agent toolkit, including the audit fixes and all eight numbered initiatives, preserving existing interfaces and stdlib core.

**Architecture:** A versioned field evidence/decision contract and local persisted run connect existing collection, manual validation, calculation, reports and monitoring. Source preparation and per-dataset freshness are shared by CLI/MCP/skill; optional document adapters and provider policies stay outside the minimal dependency core.

**Tech Stack:** Python >=3.10 standard-library core, existing MCP >=1.30,<2 optional extra, browser extension JavaScript; TypeScript where already used by the site. Tests follow the repository eval/run_*.py conventions.

## Global Constraints

- Preserve the full scope in the research; do not call a partial milestone complete as the whole goal.
- User-facing copy and documentation are Russian; existing JSON keys remain compatible through additive versioned metadata.
- No new required runtime dependency for the CLI core; PDF/OCR dependencies are optional.
- No API keys, proxy credentials or process environment in persisted run, evidence, diagnostics, events or reports.
- Network access, paid provider fallbacks and updates obey explicit user configuration; no scheduler or notification channel is installed implicitly.
- A missing necessary fact cannot become a proved absence; stale and inapplicable data remain distinct.
- Preserve original evidence packages; manual additions produce a derived version.
- Run IDs resolve inside configured storage; no arbitrary paths/CLI arguments may be smuggled through data inputs.
- Atomic writes, concurrency protection, cancellation of subprocess descendants, bounded query/update budgets.
- Source state, applicability, signal evaluation and workflow progress are separate concepts.
- New API contracts include schema/rule versions and explicit partial/unknown/errors; ToolAnnotations are hints, not enforcement.
- Tests must demonstrate red before fixing each behavior and verify final emitted CLI/MCP/document output where relevant.
- Do not overwrite others' work. Commit only assigned paths after verification and review; root coordinates commits.

## Source requirements

The authoritative research and audit are now preserved under [docs/research/2026-10-16](../../../research/2026-10-16/inn-check-ru-improvement-research.md), including all three research notes and audit repro evidence. [The design specification](../../specs/2026-10-16-agent-workflow-design.md) fixes interfaces and data contracts. [requirements.md](requirements.md) maps every M1–M8, R1–R9, W1–W8 and P1–P7 requirement plus audit fixes to tasks and acceptance evidence. [status.md](status.md) is the durable execution ledger, including baseline results and separate empirical gates.

The complete software scope includes all seven subsequent phases. Human adoption, independently labelled truth and mature prospective outcomes require real observations: their tooling/protocol is implemented in this scope, their empirical success remains unclaimed until observed evidence exists.

## Phases

- [x] [Phase 0: Audit regressions and execution safety](phase-0.md)
- [x] [Phase 1: Evidence, time, applicability and decision contracts](phase-1.md)
- [x] [Phase 2: Persisted runs and human handoff](phase-2.md)
- [x] [Phase 3: Installed diagnostics and cache policies](phase-3.md)
- [x] [Phase 4: Agent contracts and monitoring events](phase-4.md)
- [x] [Phase 5: Optional PDF/OCR, provider policy and explainable comparison](phase-5.md)
- [x] [Phase 6: End-to-end evaluation, historical replay and extended benchmark](phase-6.md)
- [x] [Phase 7: Packaging, documentation, independent review and acceptance](phase-7.md)

## Dependency order and ownership

Phase 0 review gates changes to its financial/safety modules. Phase 1A may begin in new evidence files; Phase 2A storage can proceed independently against the fixed materialized payload schema. Phase 1 integration follows review. Phases 2 and 3 converge on the installed readiness/resume path; Phase 4 adds transport/events; Phase 5 completes document/provider/comparison features; Phase 6 validates final claims/replay; Phase 7 packages and records full acceptance.

Each phase file defines exact file ownership, public interfaces, executable usage and representative red/green tests. Shared CLI/MCP/manifests are coordinated by root; workers do not revert another worker's changes. No production changes or commits were made by the specification owner.

## Ход выполнения

2026-10-16: статус программной части `completed`; проверенный коммит реализации `6d4310efb7169cb358fa111d5741866fb8f7df96`. [Отчёт о приёмке](acceptance.md) содержит все 41 программное требование, общий локальный прогон на зафиксированных файлах, окончательные проверки изменённых областей, хеши установленных артефактов и исходных файлов, области независимого ревью и ссылки на фактические запуски/задания CI. Во всех восьми эмпирических проверках новых наблюдений 0; E-HOST `not_run`, остальные семь `awaiting_external_evidence`. Версия 1.13.0 не опубликована; PR #10 остаётся в состоянии draft.
