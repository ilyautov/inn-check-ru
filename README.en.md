# inn-check-ru: agent tools to look up a Russian company by INN

> 🇷🇺 [Русская версия](README.md)

<p align="center">
  <a href="https://inn-check-ru.aifrontier.tech/">
    <img src="assets/readme-banner.jpg" alt="inn-check-ru: company data by INN for an AI agent" width="720">
  </a>
</p>

A skill, MCP server and CLI for Claude, Codex and other agents. Give it a Russian company's INN and the agent queries the state register (EGRUL), the tax service's open data, filed financial statements (GIR BO), Fedresurs notices, Bank of Russia lists, sanctions lists and about fifteen more registers. The result is a dossier: is the company alive, who owns and runs it, revenue and debts, signs of bankruptcy, related companies. Courts, bailiffs (FSSP) and the bankruptcy register sit behind captchas: the agent opens them in your browser and you solve the captcha.

The point: numbers are computed by code, not by the model, and every register's answer is labelled — found, empty, or not checked. "Found nothing" and "could not check" never merge, so a captcha or a site outage does not turn into "the company is clean".

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

## Where data goes

No servers, no telemetry. Only the INN being checked leaves your machine, to open registries: Federal Tax Service sites (egrul, pb, bo, rmsp, service, npd .nalog.ru / .nalog.gov.ru), zakupki.gov.ru, pd.rkn.gov.ru, digital.mchs.gov.ru, websbor.rosstat.gov.ru, bankrot.fedresurs.ru and fedresurs.ru, clearspending.ru, reestr.nostroy.ru and reestr.nopriz.ru; with your own key also api.checko.ru and suggestions.dadata.ru. Sanctions lists and open-data dumps are downloaded whole and matched locally in `~/.cache/inn-check-ru/`. A proxy you configure sees which INNs you check. Details: [PRIVACY_POLICY.md](PRIVACY_POLICY.md).

## License

Apache-2.0. Extracted from [small-business-ru](https://github.com/ilyautov/small-business-ru) — 34 AI skills for Russian small-business operations.

Built by [Ilya Utov](https://github.com/ilyautov) / [AI Frontier](https://aifrontier.tech). If it saved you a bad deal — star the repo, that's how others find it.
