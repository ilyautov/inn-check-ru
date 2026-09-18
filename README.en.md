# inn-check-ru: Russian counterparty due-diligence by INN, inside your AI agent

> 🇷🇺 [Русская версия](README.md)

> **Don't ship on credit blind.** An open AI skill that takes a Russian company tax ID (INN) and compiles open registries (EGRUL company registry, bailiffs FSSP, arbitration courts, bankruptcy register, financial statements) into a **risk traffic light 🟢/🟡/🔴** with a recommendation: credit terms, prepayment only, or walk away. For Claude Code, Cursor, Codex, ChatGPT and Gemini. Free, Apache-2.0, data from real registries — not from the model's imagination.

**Quick start:**

```bash
npx skills add ilyautov/inn-check-ru
```

Then just tell your agent: **"проверь контрагента по ИНН …"** (the skill speaks Russian — its data sources are Russian registries).

## Why

"Check a counterparty by INN" is one of the highest-volume business searches in the Russian web. Aggregator services exist, but they share a disease we measured live:

> **Live test (2026-06-15).** One company, 5 free aggregators. Court-case counts diverged almost 3× (~500 / ~1000 / ~1500 — different counting methodologies). One aggregator mixed in someone else's bankruptcy "intentions". Meanwhile revenue and enforcement-proceeding counts matched everywhere.

Core principle: **never trust a single aggregator — counters lie confidently. A fact is what ≥3 sources agree on; a divergence is a flag, not a reason to pick the prettiest number.**

## How it works

- **Two speeds.** Quick-scan first (~2 min): only deal-killer signals — liquidation, bankruptcy, EGRUL unreliability flag, disqualified director, debts larger than the deal. One hit → 🔴, stop there. Clean → full dossier on request.
- **Source cascade by cost:** free FNS scripts (EGRUL / Transparent Business / GIR BO financials — no keys, `scripts/fetch_counterparty.py`) → FSSP token API → aggregators via browser (checko, list-org, Saby, audit-it, rusprofile — no captcha, no signup) → manual input as last resort.
- **Every number carries source + date + tier** (✅ confirmed / ⚠️ single source / ❌ unverified). Missing data is reported as "not checked", never as a plausible fabrication.

The traffic light is an assessment for the owner, not a verdict on the company: the decision is always yours. For large or irreversible deals, treat it as a worksheet for your lawyer.

## Not a replacement for

Legal or credit guarantee. It evaluates risk from open data; professionals close the deal.

## License

Apache-2.0. Extracted from [small-business-ru](https://github.com/ilyautov/small-business-ru) — 34 AI skills for Russian small-business operations.

Built by [Ilya Utov](https://github.com/ilyautov) / [AI Frontier](https://aifrontier.tech). If it saved you a bad deal — star the repo, that's how others find it.
