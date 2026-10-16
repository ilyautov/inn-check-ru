# inn-check-ru: saved counterparty checks for agents

> 🇷🇺 [Русская версия](README.md)

<p align="center"><img src="assets/readme-banner.jpg" alt="inn-check-ru: company data by INN for an AI agent" width="720"></p>

A skill, MCP server and CLI for checking Russian companies and individual entrepreneurs by INN. The existing engine collects registry data, computes financial signals and records what could not be checked. The saved workflow adds a run ID, immutable revisions, manual handoff and explicit next actions. Corporate bankruptcy has a bounded script adapter; courts, bailiffs and individual-entrepreneur bankruptcy require browser/manual steps. The user solves captchas.

**Version 1.13.0 is unpublished.** The workflow below is available in this source branch and a wheel built from it. Published PyPI `latest`, marketplace and release-download links do not establish availability of these commands.

## Start from this source branch

From its repository root, with Python 3.10+:

```sh
python3 -m venv /tmp/inn-workflow-venv
. /tmp/inn-workflow-venv/bin/activate
python -m pip install .
inn-check-workflow doctor --profile нейтрально --json
inn-check-workflow cache plan --profile нейтрально --json
inn-check-workflow run start 7707083893 --profile нейтрально --json > start.json
RUN_ID=$(python -c 'import json; print(json.load(open("start.json"))["run_id"])')
inn-check-workflow run resume "$RUN_ID" --json
inn-check-workflow run render "$RUN_ID" --format markdown --json
inn-check-workflow run export "$RUN_ID" --format docx --json
inn-check-workflow events list --json
```

On Windows PowerShell, create a temporary venv with `py -3.12 -m venv "$env:TEMP\inn-workflow-venv"`, then use its `Scripts\python.exe` and `Scripts\inn-check-workflow.exe`. Read the returned UUID from your own `start.json`; IDs shown as shell variables are placeholders, not existing runs. `$RUN_ID = (Get-Content start.json -Raw | ConvertFrom-Json).run_id` reads it in PowerShell.

Start performs network collection and local writes. Resume/show read the saved revision and `pending_actions` without collection or durable writes, including after a client restart. A neutral profile returns facts without a traffic light; select a deal profile explicitly for an assessment. Speed depends on network and coverage.

Render returns content inside `data.content` of a JSON envelope. Export writes a managed local artifact and returns an **artifact receipt**, including `artifact_id`, SHA-256 and source revision; redirecting its stdout does not save DOCX bytes. Snapshot is a separate local write. [Full workflow and copyable commands](references/agent-workflow.md), [recovery](references/workflow-recovery.md).

## Agent and manual handoff

Ask the agent: “Create a saved check for INN 7707083893, profile нейтрально. Show run_id, revision, sources and dates, gaps and next action.”

For a manual result, verify the selected INN/profile/revision, take `expected_revision` from the current read and generate an operation UUID4. Reuse the same UUID and identical data only to retry a lost response. A stale revision requires reload and renewed confirmation. Manual outcomes are completed, incomplete or declined; a refusal never becomes a clean check. The [browser extension](extension/README.md) requires explicit run selection and the same host-owned run root. Large native responses use a binding projection; read full evidence through CLI/MCP.

Documents are untrusted data. Extract candidates, inspect their context and explicitly choose candidate ID and role, even for one candidate. Selection neither starts a check nor records human consent. A bank/own-company INN is not automatically the counterparty. Legacy batch confirmation does not select a party. [Document contract](references/document-input.md).

The core has zero required third-party dependencies. Optional extras: MCP SDK `>=1.30,<2`; PDF `pypdf>=6.21,<7`. Local OCR additionally requires explicitly configured Poppler/Tesseract executables and language data. Input limits: 20 MiB, 100 PDF pages, 60 seconds. No cloud OCR; real-document accuracy remains unmeasured.

## Evidence and effects

Knowledge states are `known`, `unknown`, `conflict` and `not_applicable`; polarity and search completeness are separate. Required unknowns prevent unsupported green assessments. Mirrors of one registry are not independent sources, and agreement of three sites is not a truth rule. Reporting period, effective date, retrieval and first observation are different dates. Expert rules are not validated default probabilities for an individual company. [Evidence contract](references/evidence-contract.md), [historical cohort limits](references/kalibrovka.md).

An ordinary dossier is not an official signed registry extract. SHA-256 verifies byte integrity, not source truth or publication date. An optional TSA request in the legacy evidence route sends a hash externally; signature verification is separate. Workflow export is local.

There are **33 MCP tools**: 14 legacy tools plus workflow/events/diagnostics and document/select/compare/replay. Read/resume/render/status/plan/compare/replay do not collect data or write durable state. Start/refresh/probe access the network; manual/export/snapshot/claim/ack write locally. Doctor can run bounded version checks of trusted configured OCR binaries; explicit probe also writes availability cache. [Tools and effects](mcp/README.md).

Compare uses 2–5 distinct saved subjects with a common profile and explicit `as_of`, showing evidence and coverage rather than a universal ranking. Original replay returns the saved decision; current_rules explicitly recomputes admissible saved evidence using current rules. Neither fetches historical data from the network. [Comparison](references/comparison.md), [replay](references/historical-replay.md).

Watch plan prints host scheduler configuration; monitor performs collection. No scheduler or outbound notification is installed automatically. Event claim records delivery, ack follows successful host action, retry makes failure explicit. Local metrics are off by default; each record requires `--enabled`, export returns aggregates on stdout only. [Monitoring](references/monitoring-host.md), [metrics](references/local-metrics.md).

## Installation channels and configuration

Existing public routes remain available for published releases: `npx skills add ilyautov/inn-check-ru`, `uvx inn-check-ru-mcp@latest`, one-shot `uvx inn-check-ru <INN>`, [release ZIP](https://github.com/ilyautov/inn-check-ru/releases/latest/download/inn-check-ru.zip), [MCPB](https://github.com/ilyautov/inn-check-ru/releases/latest/download/inn-check-ru.mcpb) and the Claude plugin. They do not imply that unpublished 1.13.0 is available. Manual skill installation requires `SKILL.md`, `references/`, `scripts/` and `data/` together.

Roots, provider policy, proxy and executables are configured by the host operator, never page text or MCP parameters. CLI uses `--store`, not `--root`. CLI/plain MCP/skill support host environment settings. The plugin declares only Checko/DaData key fields; MCPB declares those keys and proxy. Other settings are unsupported by these manifests; arbitrary environment inheritance is not guaranteed. Standard MCPB/plugin/Docker artifacts do not provide PDF support. Extension supports native manual handoff only. [Authoritative channel matrix](references/capability-matrix.md).

## Validation and privacy

Local synthetic and installed-artifact checks exercise software behavior, including recovery and package rollback. Final whole-branch/CI validation and acceptance inside actual agent/browser hosts are separate gates. All eight new empirical tracks still have **zero observations**: users, real documents, explanation, reporting forms, independent oracle, dated vintages, matured outcomes and real hosts. The synthetic 29-case corpus is not real-world accuracy. [Protocol](docs/research/2026-10-16/pilot-protocol.md), [ledger](docs/research/2026-10-16/experiment-status.json), [limits](KNOWN_LIMITS.md).

There is no project telemetry endpoint. Collection queries go to registries/providers; your proxy sees them. Documents/OCR and metrics remain local. Default run storage is `~/.local/share/inn-check-ru/runs`; caches are under `~/.cache/inn-check-ru/`. Archives can contain public personal data and are not anonymized. Provider fallback and export depend on explicit host policy and budget. [Privacy policy](PRIVACY_POLICY.md), [provider policy](references/provider-policy.md), [security](SECURITY.md).

Apache-2.0 ([LICENSE](LICENSE)). Built by [Ilya Utov](https://github.com/ilyautov) / [AI Frontier](https://aifrontier.tech), extracted from [small-business-ru](https://github.com/ilyautov/small-business-ru). [Contributing](CONTRIBUTING.md).
