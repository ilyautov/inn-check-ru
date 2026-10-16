# Полный реестр требований и ожидаемых свидетельств

Статус программной реализации и проверенная ревизия зафиксированы в [acceptance.md](acceptance.md) и [acceptance.json](acceptance.json); ход выполнения — в [status.md](status.md). `planned` означает конкретная задача задана, а не выполнена. `in_progress` допустим только по сообщению исполнителя; `verified` требует сохранённой команды/результата на конкретном revision/platform. `awaiting_external_evidence` относится к экспериментам, не освобождает от реализации инструментария. Ни один программный пункт не исключается как «следующий релиз».

Источники сохранены в репозитории: [основное исследование](../../../research/2026-10-16/inn-check-ru-improvement-research.md) (M), [достоверность](../../../research/2026-10-16/research-notes/reliability.md) (R), [платформа](../../../research/2026-10-16/research-notes/platform-workflow.md) (W), [продукт](../../../research/2026-10-16/research-notes/product-market.md) (P), [аудит](../../../research/2026-10-16/inn-check-ru-review.md) (A). Ссылки на §/пункт ниже обозначают разделы этих документов, а не новые неподтверждённые источники.

## Восемь основных инициатив

| ID | Источник / обязательное поведение | Задачи | Проверяемое свидетельство | Статус |
|---|---|---|---|---|
| M1 | M §1: единый сохранённый run_id, профиль, версии, исходный сбор, ручные блоки, документы | 2A–2C | New process → same run → manual → dossier/snapshot, 0 повторных сетевых вызовов, immutable original hash; [отчёт о приёмке](acceptance.md) | verified |
| M2 | M §2: происхождение/время/зависимости каждого решающего поля; конфликты и семейства | 1A–1C | Каждый решающий путь профиля представлен; raw locator/hash разрешается; все листья derived claim известны или причина unknown; [отчёт о приёмке](acceptance.md) | verified |
| M3 | M §3: исполнимое закрытие gaps, applicability, pending_actions | 1B–1C, 2B–2C, 3C | Капча → конкретный доступный шаг; отказ/чужой ИНН/неполнота сохраняются; unavailable action честно помечен; [отчёт о приёмке](acceptance.md) | verified |
| M4 | M §4: установленный doctor, cache status/plan, TTL по набору | 3A–3C, 7A | Clean wheel/HOME, no SDK/key base works, status offline, interrupted update retains previous, sanctions due <7d; [отчёт о приёмке](acceptance.md) | verified |
| M5 | M §5: реальный resolver и полный published workflow, инварианты, эталон и temporal design | 6A–6C, 7A | CLI/MCP/dossier assertions + denominator; deliberate bad mutations fail; historical/outcome protocols отдельно; [отчёт о приёмке](acceptance.md) | verified; E-ORACLE,E-OUTCOME pending |
| M6 | M §6: исправления аудита, stable schema, effects/cancellation, version/channel parity | 0A–0B, 4A, 7A–7B | Negative repros, tools/list snapshot, no descendant after cancel, installed artifact parity; [отчёт о приёмке](acceptance.md) | verified |
| M7 | M §7: durable events + host follow-up plan, change vs coverage failure | 4B–4C | Ten-event retry/crash campaign; no new event identity on replay, persisted next-agent report, no installed schedule; [отчёт о приёмке](acceptance.md) | verified |
| M8 | M §8: весь optional PDF/OCR+role, provider policy, explainable graph/compare | 5A–5C | Optional-dependency matrix, no wrong-side auto-fetch, bounded fallback, same-goal compare and frontier explanations; [отчёт о приёмке](acceptance.md) | verified; E-DOCS,E-EXPLAIN pending |

## Достоверность R1–R9

| ID | Источник / обязательное поведение | Задачи | Проверяемое свидетельство | Статус |
|---|---|---|---|---|
| R1 | R §R1: schema-versioned sidecar, claim ids, subject/value/unit/source/raw/transform/all deps; negative scope | 1A, 1C | Transit has revenue+staff deps; no raw substitution; raw_unavailable explicit; final chain retains refs; [отчёт о приёмке](acceptance.md) | verified |
| R2 | R §R2: retrieved/published/first observed/effective/period/valid_until separate; frozen clock | 1B, 3A | Reload changes retrieval only; future passport not known-at; missing/time-zone/TTL bounds; stale negative vs historic positive; [отчёт о приёмке](acceptance.md) | verified |
| R3 | R §R3: origin_family/upstream/copy vs independent; symmetric conflicts | 1B, 5B | Mirror duplication no independence increase; order reversal same conflict; selection reason or unknown; [отчёт о приёмке](acceptance.md) | verified |
| R4 | R §R4: subject/form/version/source coverage applicability; simplified/IP/bank/NPO/restricted | 1B–1C | Six-segment matrix; no invented bank ratios; denominator and unsupported_form explicit; checked real forms separate; [отчёт о приёмке](acceptance.md) | verified; E-FORMS pending |
| R5 | R §R5: immutable editions, cutoff/versions; original vs current rules replay | 2A, 6B | Late revision excluded, old manifest unchanged, changed threshold new replay; actual dated editions separate; [отчёт о приёмке](acceptance.md) | verified; E-VINTAGE pending |
| R6 | R §R6: independent temporal holdout, subject/group separation, horizon/censoring, precise outcomes/CI | 6C | Frozen protocol and validators; no train/test overlap or immature survived; observed holdout outcome report only after real data; [отчёт о приёмке](acceptance.md) | verified; E-OUTCOME pending |
| R7 | R §R7 A: actual scorer/resolver/CLI/MCP/dossier unsupported phrases, false green required unknown, abstention/coverage | 1C, 6A | Frozen cases, output assertions, counts/denominators/reasons/next-check executability; matched-input coverage comparison; [отчёт о приёмке](acceptance.md) | verified |
| R7-B | R §R7 B: distinguish P(outcome\|green) and P(green\|outcome) | 6C | Separate labelled matured outcomes and uncertainty; evaluator tested synthetically; real result not fabricated; [отчёт о приёмке](acceptance.md) | verified; E-OUTCOME pending |
| R8 | R §R8: decision-grade reference corpus, independent reviewers/adjudication and negative completeness | 6A, 6C | Rubric/hash/scope/date, two reviews for disputed critical facts, exclusions by missing oracle, adversarial vs representative reports separate; [отчёт о приёмке](acceptance.md) | verified; E-ORACLE pending |
| R9 | R §R9: knowledge invariants under removal/duplicate/order/unit/time/network/schema faults | 6A | Seed/frozen clock/minimal failure artifact; injected unknown→absent and lost-provenance detected at final JSON/text; [отчёт о приёмке](acceptance.md) | verified |

## Платформа W1–W8

| ID | Источник / обязательное поведение | Задачи | Проверяемое свидетельство | Статус |
|---|---|---|---|---|
| W1 | W §1: run locality/confinement/atomicity/restart; one materialized result | 2A–2B | UUID/path/symlink/JSON limits, CAS two writers, crash phases, idempotency, resume without network; [отчёт о приёмке](acceptance.md) | verified |
| W2 | W §2: cache readiness/minimal-vs-large and per-dataset schedule | 3A–3B | Inventory all 14 existing updaters; unknown size labelled; selected execution and download bounds; last-known-good; [отчёт о приёмке](acceptance.md) | verified |
| W3 | W §3: installed doctor Python3.10/3.12 + SDK absent/allowed/incompatible | 3C, 7A | Outside checkout resource lookup, exact OS next actions, key-presence only, no secret values; [отчёт о приёмке](acceptance.md) | verified |
| W4 | W §4: shared manual JSON, URL/query/required fields; Chrome confirms run binding | 2B–2C | Native framing/allowlist + UI evidence; incomplete/refusal/date/subject cases; no captcha automation; [отчёт о приёмке](acceptance.md) | verified; E-HOST pending |
| W5 | W §5: specific raw response pointer and derived evidence version | 1A, 2A–2B | Saved response resolved by id/hash/locator; original evidence pack verifies after manual addition/export; [отчёт о приёмке](acceptance.md) | verified |
| W6 | W §6: actual ToolAnnotations/effects, structured outcomes, process safety | 0B, 4A | Real stdio tools/list and cancel, no hidden write/export, descendants stopped and status durable; [отчёт о приёмке](acceptance.md) | verified |
| W7 | W §7: monitoring result and host plan incl Windows, bounded existing budgets | 4B–4C | Report survives closed chat, coverage loss distinct, XML/plist/cron parse and quoting, no registration; [отчёт о приёмке](acceptance.md) | verified |
| W8 | W §8: actual installed synthetic E2E all channels/OS, settings parity, live canary separate | 7A–7B | Build/install artifacts outside checkout; exact release/version/schema resources; geography and untested real-host status; [отчёт о приёмке](acceptance.md) | verified; E-HOST pending |

## Продукт P1–P7

| ID | Источник / обязательное поведение | Задачи | Проверяемое свидетельство | Статус |
|---|---|---|---|---|
| P1 | P п.1: next action understandable without author; ten new installs/target eight | 2B, 3C, 7C | Software actions schema and parser validity; human explanation scored from observed sessions; [отчёт о приёмке](acceptance.md) | verified; E-HUMAN pending |
| P2 | P п.2: one check/run across five manual court/FSSP scenarios | 2A–2C, 7C | Five synthetic restart cases plus separate five observed user tasks; [отчёт о приёмке](acceptance.md) | verified; E-HUMAN pending |
| P3 | P п.3: reviewer reconstructs each pilot dossier claim without model | 1A–1C, 7C | Automated reference integrity and recorded independent reconstruction result; [отчёт о приёмке](acceptance.md) | verified; E-EXPLAIN pending |
| P4 | P п.4: fields/cost/quota/freshness/export/availability and explicit fallback/transfer | 5B | Quota/429/401/timeout matrix, concurrent budget reservation, forbidden export scan, no silent provider switch; [отчёт о приёмке](acceptance.md) | verified |
| P5 | P п.5: real document input with role/INN/KPP/OGRN/address and optional PDF/OCR | 5A, 7C | Core no extra; deliberate selection on ambiguity; twenty consented anonymized documents measured separately; [отчёт о приёмке](acceptance.md) | verified; E-DOCS pending |
| P6 | P п.6: stable durable event id/retry/delivery/read ack, stdout/file+n8n | 4B–4C | Ten synthetic events and crash/retry/ack receipts; config-only sample no outbound notifications; [отчёт о приёмке](acceptance.md) | verified |
| P7 | P п.7: compare 2–5 same scenario/date and explicit graph edge/frontier | 5C, 7C | Unknown not favourable, goals mismatch rejected, ownership vs address test; user explanation separate; [отчёт о приёмке](acceptance.md) | verified; E-EXPLAIN pending |

## Audit baseline regressions

| ID | Источник / обязательное поведение | Задачи | Свидетельство | Статус |
|---|---|---|---|---|
| A-H1 | A H1: MCP list cannot inject argv/path or expand beyond 50 INNs | 0B | Temp negative tests no extra reads/writes/network; 51 input rejected; [отчёт о приёмке](acceptance.md) | verified |
| A-H2 | A H2: missing staff/capital/prior equity unknown; FNS zero recognized | 0A | Original seven finance repros and `оценки_сигналов` contract; [отчёт о приёмке](acceptance.md) | verified |
| A-H3 | A H3: mandatory/red-override unknown forbids green/unfounded phrase | 0A | Final resolver/dossier strings including mass-head deletion; [отчёт о приёмке](acceptance.md) | verified |
| A-H4 | A H4: Y and Y−1 only, duplicates conflict conservative | 0A | 2025/2023, duplicate revisions, previous-year missing, genuine consecutive controls; [отчёт о приёмке](acceptance.md) | verified |
| A-H5 | A H5: manual install includes data | 0B, 7A | Copied installation outside checkout profiles/list works; [отчёт о приёмке](acceptance.md) | verified |
| A-M1 | A M1: timeout/cancel kills and reaps descendants across OS | 0B, 4A, 7A | Delayed descendant marker absent, no surviving child; Windows live runner evidence; [отчёт о приёмке](acceptance.md) | verified |
| A-M2 | A M2: parallel spy holds object references | 0B | False-positive regression with independently created retained openers; [отчёт о приёмке](acceptance.md) | verified |
| A-M3 | A M3: cache unavailable/network vs schema contract | 0B, 7A | Clean cache canary skip with reason; actual malformed prepared input fails schema; [отчёт о приёмке](acceptance.md) | verified |

## Empirical gates and evidence limits

| ID | Required protocol/instrumentation | Required observed evidence | Наблюдения / статус |
|---|---|---|---|
| E-HUMAN | Opt-in local metrics; eight observed interviews, four-week pilot; all install starts/dropoffs; 10 installs/8 unaided next-step target, 5 real manual resume tasks, 3 independent second-task repeats | Consent-safe sessions, active/wait time, switches/manual work, final artifact or stopping reason, per-user repeat counts | 0 / awaiting_external_evidence |
| E-DOCS | 20 consented anonymized real documents, role rubric before extraction; no synthetic substitution | Intended-party independent labels, OCR/text method, silent wrong-party count, selection rate and errors | 0 / awaiting_external_evidence |
| E-EXPLAIN | Independent reconstruction of all decisive pilot dossier facts and comparative choice explanation | Reviewer sees raw evidence without model; success/failure per claim, coverage reasoning | 0 / awaiting_external_evidence |
| E-FORMS | Full/simplified/IP/bank/NPO/restricted applicability support matrix | Checked real anonymized forms and official schema/coverage rationale; unsupported samples labelled | 0 newly validated / awaiting_external_evidence |
| E-ORACLE | Frozen reference rubric/sampling, negative scope/fullness, two independent reviewers and adjudication | Primary-document hashes/dates/permitted use and completed independent labels; missing oracle exclusions | 0 new decision-grade labels / awaiting_external_evidence |
| E-VINTAGE | Prospective version capture and cutoff mechanism | Two real dated editions with provable availability and permission; synthetic late revision is only mechanism evidence | 0 pairs / awaiting_external_evidence |
| E-OUTCOME | Frozen engine/sampling/CI method; disjoint subjects/groups; precise outcomes and censoring | Independent temporal holdout with mature horizons, event count/attrition/weights and report with uncertainty | 0 new held-out matured outcomes / awaiting_external_evidence |
| E-HOST | Manual checklist for actual browser/native-host/MCP client, scheduler dry-run then separately authorized installation | Real host versions, screenshots/logs of full resumed task; actual OS scheduled run only if performed | 0 new sessions / not_run |

Software acceptance requires every software row verified or an explicit blocking defect. Full empirical acceptance requires the respective observed evidence; passing evals is never sufficient to change these rows to verified. Live registry correctness and commercial competitor superiority are not inferred from this plan.
