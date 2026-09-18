# Changelog

Формат — [Keep a Changelog](https://keepachangelog.com/ru/1.1.0/), версии — semver.

## [1.0.0] — 2026-09-19

Выделение в самостоятельный репозиторий из скилла `counterparty-guard` пака [small-business-ru](https://github.com/ilyautov/small-business-ru).

### Added
- Правила круговой сверки источников встроены в SKILL.md — companion-скилл больше не нужен для standalone-установки.
- README на русском и английском, CI (ruff + проверка frontmatter).

### Changed
- Скилл переименован: `counterparty-guard` → `inn-check-ru`.

История до выделения (counterparty-guard): 0.3.1 (16.06.2026) — единый формат killer-карточки; 0.3 (15.06.2026) — живой тест 5 агрегаторов, режимы quick-scan/досье/мониторинг, TLS по умолчанию; 0.2 — каскад транспортов, severity-resolver, numerical-manifest; 0.1 — сбор зелёной зоны ФНС по ИНН.
