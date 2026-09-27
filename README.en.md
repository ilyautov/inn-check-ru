# inn-check-ru: company data by Russian INN for your AI agent

> 🇷🇺 [Русская версия](README.md)

<p align="center">
  <a href="https://inn-check-ru.aifrontier.tech/">
    <img src="assets/readme-banner.jpg" alt="inn-check-ru: company data by INN for an AI agent" width="720">
  </a>
</p>

> **Give your agent an INN — get a company dossier.** inn-check-ru connects tools that collect facts about a Russian company from open sources: status, financials, debts, court cases, owners and connections. The dossier helps you study a company, check its connections and compare saved snapshots. Unchecked sources stay visible. A deal check (risk traffic light 🟢/🟡/🔴 for given terms) is an extra mode. Open source, Apache-2.0; the model and third-party APIs may be billed separately.

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
