# Статус реализации и границы проверки

Обновлено 2026-10-16. Цель — полностью реализовать программные фазы0–7 текущего open-source инструмента для агентов. Версия1.13.0 не опубликована. Рабочая ветка `feat/reproducible-agent-workflow`, draft PR [#10](https://github.com/ilyautov/inn-check-ru/pull/10). Последний отправленный commit `170a0e0b7302bd0fe7026650511aca66da92d00c`; последующая реализация пока находится в рабочем дереве и проходит заключительный review. Не считать этот commit проверкой новых возможностей.

## Программная реализация

| Фаза | Реализовано и проверено в пределах отдельных задач | Оставшаяся приёмка |
|---|---|---|
| 0 | Исправления финансовой неизвестности/смежных лет, MCP input bounds, отмена дерева процессов, данные ручной установки. Независимые замечания закрыты | Итоговые regression/platform проверки после интеграции |
| 1 | Versioned evidence, реальные raw locators/hashes, temporal/applicability/conflict/family contract, sufficient financial support, frozen decision/rendering. Независимые замечания закрыты | Финальный whole-branch и output gate |
| 2 | Immutable revisions/CAS/idempotency, ручное дополнение/resume, CLI/native extension handoff. Проверены18mock состояний интерфейса в светлой/тёмной теме при360px | Новое замечание review: сохранять acquisition dates неизменённых unmapped claims при manual/selected refresh; исправление назначено. Real installed host отдельно |
| 3 | Все14updaters в policy, offline status/plan, bounded transactional cache update, doctor, per-TTL schedules. Шесть прежних замечаний закрыты; named environment fix прошёл Python3.10/3.12 и skill gate | Standalone ZIP process dependency fix, итоговый review/installed run |
| 4 | 33 MCP tools/schema/effects, parent-owned legacy async/cancellation, durable event claim/ack/retry, coverage-loss events, host plans. Scoped security/process reviews выполнены | Shared process runtime move и финальные stdio/OS проверки |
| 5 | Optional PDF/local OCR, explicit candidate+role selection, immutable provider budget/export policy, graph frontier/explainable comparison. Scoped findings закрыты | Итоговый packaging/whole-branch review; real20consented documents не получены |
| 6 | Frozen29-case synthetic corpus, final-output gate, knowledge mutations, original/current_rules replay, outcome protocol/validators. Financial/replay/protocol closure одобрен | Benchmark closure выявил self-oracle prose и неполную expiry/heading metric проверку; fresh fixer работает. Полный29/E2E/invariants ждёт runtime freeze |
| 7 | Packaging/capability/settings matrix,6OS×Python jobs, все29новых eval включены в CI, public docs/recovery, opt-in metrics. Четыре intermediate installed variants3.10/3.12 core/SDK и отдельный installedPDF smoke прошли | Final coherent wheel/ZIP, whole-branch code/security review, полный suite, фактический новый CI, requirement ledger |

Новые интеграционные замечания являются блокирующими, а не принятыми ограничениями. Whole-branch review продолжается. Source ZIP проверялся по inventory, но его самостоятельное исполнение выявило зависимость document/doctor/monitor от отсутствующего `mcp/`; общий stdlib process runtime и настоящий unpacked-ZIP test назначены отдельному исполнителю. Root render реального DOCX показал выход колонок таблиц за страницу; DOCX-only layout fix также входит в обязательную приёмку.

## Сохранённые свидетельства

Подробные red/green logs, hashes и независимые отчёты пока лежат вне репозитория в родительском workspace `work/implementation-reports/`. Они не являются переносимыми ссылками репозитория; итоговый acceptance record будет собран после всех проверок.

- Baseline `3e06046`:43совместимых eval PASS; Windows-only не выполнялся на локальном macOS.
- Phase0 immutable `cacfc2c`:45совместимых eval PASS; source unchanged, actual MCP stdio PASS; Ubuntu wheel/MCPB/Docker CI PASS.
- Actual Windows `170a0e0`:native host и34process-safety tests PASS, затем упал nested profiles CLI CP1252. Boundary UTF-8 fix воспроизведён локально; нужен новый Windows CI.
- Intermediate installed routes на macOS Python3.10/3.12, безSDK и сSDK1.30:4/4PASS, включая actual33-tool stdio там, где SDK установлен. Это четыре конкретных WIP-снимка, не итоговая сборка после более поздних изменений.
- Installed PDF extra6.21.0 smoke:реальный текстовый synthetic PDF, два ожидаемых ИНН; core/noSDK и OCR отдельно.
- Public docs gate:66parser examples/125relative links PASS. Это проверка команд/ссылок, не подтверждение успешного live-сбора каждым примером.
- Hermes environment fix:cache16,doctor9,actualSDKfeature13 на каждом из Python3.10/3.12;16skill gates PASS. Ограниченные env и strict version banners, ещё требуется финальный review после переноса runtime.
- Metrics:независимо проверены100000-event cap и отказ повреждённому storage, disabled-default/no-write, aggregate-only explicit CLI. Никакой автоматической телеметрии.
- Root DOCX render:7страниц,40evidence rows; FAIL layout — raw/period/scope колонки за границей страницы. Исправление и повторный render обязательны.

Security CI на `170a0e0` находил4unverified URI matches/0verified в reserved-domain synthetic fixtures. Только четыре точные строки получили документированный `trufflehog:ignore` после реального3.99.2positive-control proof (4unannotated→4,4annotated→0,mixed8→4controls,actual2files→0). Детекторы и workflow не ослаблены. Старые commits ещё содержат неаннотированные примеры; итоговый history scan остаётся открытым до безопасного сохранения/очистки только собственной unmerged истории и нового CI.

## Внешние эксперименты

Все8эмпирических gates остаются с нулём новых наблюдений: E-HUMAN, E-DOCS, E-EXPLAIN, E-FORMS, E-ORACLE, E-VINTAGE, E-OUTCOME — `awaiting_external_evidence`; E-HOST — `not_run`. Протоколы, валидаторы и добровольные локальные метрики реализованы. Синтетические случаи не заменяют реальные документы, независимых reviewers, dated editions, matured outcomes или работу конкретного agent client.

Actual scheduler installation, external notification, publication, merge и релизные uploads этой реализацией не выполняются. Generated cron/plist/XML — только план. Нет вывода о live-доступности всех реестров, качестве конкретного контрагента или превосходстве над коммерческими продуктами из offline tests.

## Финальный gate

1. Закрыть все подтверждённые integration/review findings и повторно проверить затронутые контракты.
2. На неизменном source snapshot выполнить каждый совместимый eval,29-case report/E2E/invariants, ruff/version/skill/workflow/capability/docs gates и actual stdio.
3. Построить финальные wheel/ZIP, реально исполнить оба вне checkout; проверить inventories/resources/hashes/secrets/recovery/downgrade.
4. Получить новый фактический Linux/macOS/Windows×3.10/3.12 CI и существующие distribution/security checks.
5. Закрыть41software requirement row ссылками на проверки и конкретный итоговый evidence record; сохранить внешний empirical status без вымышленных наблюдений.
