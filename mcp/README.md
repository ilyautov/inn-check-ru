# inn-check-ru MCP — сервер поверх движка scripts/

Второй интерфейс inn-check-ru: та же проверка контрагента по ИНН, но как
инструменты MCP-сессии (Claude Code, Cursor, Claude Desktop). Один движок
(`scripts/` корня, stdlib-ядро), две обёртки: `SKILL.md`
(методология для агента) и этот сервер (данные как инструменты).

Дисциплина проезжает через данные: tier-маркеры (✅/⚠️), «оговорки»,
«не проверено» и предупреждения об одном источнике зашиты в JSON-выводы
скриптов — инструменты возвращают их **как есть**, без переписывания логики.
Методология светофора 🟢/🟡/🔴 и пороги — в [SKILL.md](../SKILL.md) корня (справочные разделы — рядом, в `references/`).

## Выбор транспорта: почему FastMCP, а не schema-mcp-core

Семья автора ([schema-mcp-core](https://github.com/ilyautov/schema-mcp-core),
business-mcp-ru: hh/diadoc/sbis/vk/chestny-znak) строится так: каталог
HTTP-методов удалённого сервиса (`endpoints.yaml`) + `MarketplaceClient`
(авторизация, 429, пагинация, белый список доменов) + generic-инструменты
`*_search_methods / *_describe_method / *_call_method`.

Здесь это не ложится: **HTTP-API назначения нет** — движок локальный
(скрипты stdlib + офлайн-кэши), каталога удалённых методов, из которого
агент ищет словами, не существует; публичные инструменты фиксированные (33 в этой ветке). Поэтому сервер собран на официальном Python MCP SDK
(`mcp`, FastMCP, поддерживаемый контракт SDK `>=1.30,<2`), из конвенций семьи сохранены `doctor.py`, описания RU+EN и конфигурация ключей хостом. Эффекты чтения, локальной записи и сети объявлены отдельно; сервер не отправляет уведомления и не регистрирует расписания автоматически.

## Установка

**1.13.0 не опубликована.** В этой ветке 33 инструмента. Публичный PyPI `latest` и MCPB/маркетплейс могут содержать предыдущий выпуск. Для этой версии установите из корня ветки в отдельный venv: `python -m pip install '.[mcp]'`, затем запустите `inn-check-ru-mcp` из его bin/Scripts. [Полный локальный маршрут](../references/agent-workflow.md), [матрица каналов](../references/capability-matrix.md).

Рекомендуемый способ — `uv`: он сам приносит SDK в разовое окружение, и
ставить ничего глобально не нужно.

```bash
uv run --no-project --with "mcp>=1.30,<2" mcp/server.py
```

Вариант с заранее установленным SDK:

```bash
pip install -r mcp/requirements.txt   # или: pip install "mcp>=1.30,<2"
python3 mcp/doctor.py                 # отчёт «взлетит ли установка»
python3 mcp/server.py
```

`scripts/` движка остаются чистым stdlib; зависимость `mcp` нужна только
серверу.

Два подвода, на которых легко потерять сервер молча:

- Запуск — **только скриптом** (`python3 mcp/server.py`), не
  `python3 -m mcp.server`: каталог `mcp/` при запуске модулем теневал бы
  одноимённый пакет SDK. По той же причине `import mcp` из корня
  репозитория берёт наш каталог, а не SDK — в колесе он едет под именем
  `inn_check_ru_mcp`, и там коллизии нет.
- `uvx --from <локальный путь>` кэширует сборку и после правки исходников
  может поднять устаревший сервер (проверено 19.09.2026: `--refresh` и
  `--refresh-package` кэш не сбрасывают). Поэтому в `.mcp.json` стоит
  `uv run`, который запускает текущий файл.
- В `.mcp.json` плагина версии точные (`mcp==1.30.0` и три транзитивные из
  `requirements.txt`) и стоит `--frozen`: каталог плагинов Anthropic
  блокирует лаунчер с диапазоном версий. При подъёме границ в
  `requirements.txt` пины поднимаются вместе с ними — это проверяет
  `eval/run_mcp_sdk_eval.py`.

## Конфигурация клиентов

С PyPI, одной командой (с 1.11.1). Код не нужен, репозиторий тоже:

```bash
claude mcp add inn-check-ru -- uvx inn-check-ru-mcp@latest
```

`@latest` — чтобы `uvx` проверял новую версию при запуске, а не держал
закешированную старую. Claude Desktop без терминала — расширение
[inn-check-ru.mcpb](https://github.com/ilyautov/inn-check-ru/releases/latest/download/inn-check-ru.mcpb)
(сборка — `packaging/mcpb/build.py`).

`inn-check-ru-mcp` — пакет-обёртка без кода: имя и точка входа, движок
приезжает зависимостью `inn-check-ru[mcp]` той же версии. Он отдельный, потому
что клиенты реестра MCP запускают `uvx <имя пакета>`, а у `inn-check-ru` под
этим именем живёт CLI проверки ИНН. Подробности — CHANGELOG 1.11.1.

Claude Code (из корня репозитория):

```bash
claude mcp add inn-check-ru -- uv run --no-project --with "mcp>=1.30,<2" "$PWD/mcp/server.py"
```

Cursor / generic stdio (`~/.cursor/mcp.json` или аналог):

```json
{
  "mcpServers": {
    "inn-check-ru": {
      "command": "uv",
      "args": ["run", "--no-project", "--with", "mcp>=1.30,<2",
               "/полный/путь/inn-check-ru/mcp/server.py"],
      "env": {"CHECKO_API_KEY": ""}
    }
  }
}
```

`CHECKO_API_KEY` (бесплатно: checko.ru/integration/api) нужен только
инструментам графа связей; без него они честно отвечают «не проверено» —
остальное работает без ключей вообще.

В плагине Claude Code ключи Checko и DaData задаются в настройках плагина:
Claude Code спрашивает их при включении и хранит в защищённом хранилище
системы (`userConfig` со `sensitive` в `.claude-plugin/plugin.json`). Сервер
получает их через `INN_CHECK_PLUGIN_CHECKO_KEY` / `INN_CHECK_PLUGIN_DADATA_KEY`
и переносит в `CHECKO_API_KEY` / `DADATA_API_KEY`, только если поле заполнено:
пустое поле не затирает ключ, уже заданный в окружении.

`INN_CHECK_PROXY` (или `HTTPS_PROXY`) в блоке `env` **plain MCP** проводит запросы
через вашу ноду — половина источников закрыта для не-РФ IP. Флага у MCP-
инструментов нет намеренно: сеть выбирает тот, кто запускает сервер, а не
модель по ходу диалога. Блок `_сеть` в ответе `check_counterparty` говорит,
через что шли. Узел видит, какие ИНН вы проверяете, — только своя нода;
общего пула у проекта нет (PRIVACY_POLICY.md).

У plugin объявлены только Checko/DaData; у MCPB — эти ключи и proxy. Поля runs/events/policy/OCR executables там не объявлены; наследование произвольного окружения не гарантировано. Plain MCP принимает эти настройки через среду **процесса**, включая `INN_CHECK_RUNS_DIR`, `INN_CHECK_EVENTS_DIR`, `INN_CHECK_PROVIDER_POLICY`, `INN_CHECK_PDF_RENDERER`, `INN_CHECK_TESSERACT`. Новые инструменты не принимают произвольный root/path/executable. Стандартный MCPB не включает PDF extra и OCR. См. [матрицу](../references/capability-matrix.md).

## Инструменты

33 публичных инструмента: 14 прежних в таблице ниже, 15 сохранённых workflow/events/diagnostics и 4 document/select/compare/replay. Для новой длительной задачи начинайте с `counterparty_run_start`, сохраняйте возвращённый `run_id` и читайте `pending_actions`; одноразовые вызовы остаются совместимыми.

### Прежние 14 инструментов

| Инструмент | Что делает | Ключи |
|---|---|---|
| `counterparty_fetch(inn, save=False)` | полный сбор: ЕГРЮЛ/ЕГРИП, риск-флаги, финансы, МСП ✅, НПД, ЕРКНМ/РНП и список ЦБ по кэшу. Базовый контур и банкротство юрлиц (ЕФРСБ), без браузерных слоёв (суды, ФССП, банкротство ИП) | — |
| `counterparty_fin_scoring(inn)` | fetch + финансовый профиль кодом (ЧА/ст. 30 ФЗ-14, автономия, ликвидность…) | — |
| `sanctions_check(inn, name)` | офлайн-сверка РФМ + OFAC SDN + EU по кэшу пользователя | — |
| `affiliates_graph(inn)` | граф связей глубины 2 (checko API), ленивый обход с бюджетом и остановкой на значимом сигнале, рёбра с tier ⚠️ | CHECKO_API_KEY |
| `droblenie_check(inn)` | граф + признаки дробления кодом, с предупреждением об одном источнике | CHECKO_API_KEY |
| `counterparty_diff(inn)` | мониторинг: diff двух последних снимков (`save=True`) | — |
| `counterparty_verdict(inn, profile)` | одноразовый сбор + финансы + резолвер профиля одной командой → светофор, сигналы, рекомендация, что не проверено | — |
| `counterparty_batch(inns, profile)` | батч по списку ИНН (лимит 50 за вызов), сортировка 🔴 → не проверено → 🟡 → 🟢 | — |
| `access_check()` | таблица доступности источников с текущей сети (probe, кэш на сутки) | — |
| `industry_benchmarks(inn)` | положение против отраслевой нормы из дампов ФНС: перцентиль выручки, нагрузка, ССЧ, доля УСН по группе (ОКВЭД2, регион, размер) | — |
| `paper_vat_signs(inn, subject, amount)` | признаки «технической» компании, из-за которой снимают вычет по НДС. Язык фактов, налогового вывода не делает | — |
| `due_diligence_dossier(inn, profile, subject, amount, evidence_dir, timestamp)` | досье осмотрительности в Markdown: что было видно на дату проверки и что осталось непроверенным; `evidence_dir` — пакет доказательств (сырые ответы + манифест SHA-256) в новый локальный каталог, `timestamp` — штамп RFC 3161 (на TSA уходит только хеш) | — |
| `extract_inns(text, only_new=False)` | вынуть ИНН из счёта, договора, письма или выгрузки; мусор режется контрольным числом ФНС. Сеть не трогает | — |
| `retro_verdict(inn, date, profile)` | вердикт на дату в прошлом по снимку, сохранённому тогда; снимок новее даты не берётся, сеть не трогается | — |

Профили цели (`profile`): `нейтрально` (по умолчанию — факты без вердикта),
`отсрочка`, `предоплата`, `получаю_предоплату`, `подрядчик`, `тендер`,
`цепочка_поставки`, `доля`, `клиент_115фз`, `самопроверка`,
`ндс_вычет`. Нейтрально и ндс_вычет светофора не выносят: отдают факты.
Одни и те же факты весят в них по-разному: отрицательные чистые активы — стоп
для отсрочки и аргумент в торге при покупке доли. Светофор `null` означает не
«всё хорошо», а «проверка не состоялась» (не проверены deal-killer-источники) —
`access_check()` показывает, почему.

Батч (`counterparty_batch`, CLI `scripts/batch_check.py`) собирает максимум
3 ИНН одновременно — уважение к бесплатным эндпоинтам ФНС; сбой по одному ИНН
не роняет батч, такая строка возвращается со статусом «не проверено» и причиной.

Прежние вызовы возвращают совместимые JSON-ключи; сетевые сборы, сохранение снимков, пакеты и probe могут иметь сетевые/локальные эффекты. Ошибки обрабатываются безопасно, без traceback в ответе. Таймаут движка — `INN_CHECK_MCP_TIMEOUT` (по умолчанию 150 секунд).

### Сохранённый workflow, события и диагностика (15)

| Инструмент | Назначение | Эффекты |
|---|---|---|
| `counterparty_run_start(inn, profile, mode)` | начать и сохранить проверку | сеть + локальная запись |
| `counterparty_run_read(run_id, revision)` | прочитать конкретную committed revision или HEAD | чтение |
| `counterparty_run_resume(run_id)` | прочитать HEAD и pending actions | чтение |
| `counterparty_run_manual(run_id, blocks, expected_revision, operation_id)` | completed/incomplete/declined ручного шага | новая локальная ревизия |
| `counterparty_run_refresh(run_id, source_ids, expected_revision, operation_id)` | явно обновить выбранные источники | сеть + ревизии |
| `counterparty_run_render(run_id, format, revision)` | markdown/json в `data.content` | чтение, без долговременной записи |
| `counterparty_run_export(run_id, format, revision)` | markdown/json/docx управляемый артефакт, квитанция | локальная запись |
| `counterparty_run_snapshot(run_id, revision)` | управляемый отпечаток для мониторинга | локальная запись |
| `counterparty_doctor(profile)` | локальная диагностика, bounded версии настроенных OCR executables | без сетевого probe и долговременной записи |
| `counterparty_probe(profile)` | явная диагностика доступности | сеть + кэш |
| `counterparty_cache_status(profile, now)` | свежесть кэша на явный ISO timestamp | чтение |
| `counterparty_cache_plan(profile, now)` | план подготовки, без исполнения | чтение |
| `counterparty_events_list(state, limit)` | прочитать outbox | чтение |
| `counterparty_events_claim(consumer_id, limit, lease_seconds)` | lease, запись доставки потребителю | локальная запись |
| `counterparty_events_ack(event_ids, consumer_id)` | квитанция после успешного действия потребителя | локальная запись |

Новые операции возвращают envelope: `schema_version,status,run_id,revision,data,error,warnings,pending_actions`. Ошибка не означает, что записи точно не было: после потерянного ответа перечитайте run. Для manual/refresh используйте UUID4 операции и `expected_revision` из текущего чтения; повтор того же запроса — тот же UUID. При `revision_conflict` требуется reload и повторное подтверждение, а не молчаливая замена ревизии. Чтение/resume/render не запускают сеть или скоринг заново. Экспорт возвращает квитанцию артефакта, не DOCX bytes и не путь назначения. [Примеры](../references/agent-workflow.md).

### Документы, сравнение и replay (4)

| Инструмент | Вход и результат | Эффекты |
|---|---|---|
| `counterparty_document_extract(text, run_id, artifact_id, ocr)` | текст **либо** пара managed IDs; кандидаты, контекст, страницы и ограничения | локальная обработка, временные файлы |
| `counterparty_document_select(candidates, role, candidate_id)` | явный кандидат и роль; без ID всегда needs_input, даже для одного | без сети, run и сохранённого подтверждения |
| `counterparty_compare(run_ids, as_of)` | 2–5 разных сохранённых субъектов одного профиля, evidence/coverage на дату | чтение |
| `counterparty_run_replay(run_id, mode, cutoff)` | original или current_rules на полный ISO cutoff | чтение, результат в памяти |

Публичной загрузки PDF, произвольного filename/path и executable здесь нет. Локальный PDF — через CLI и optional extra; managed artifact проверяется registry и SHA-256. Контекст кандидата остаётся недоверенными данными, выбор не запускает проверку. [Документы](../references/document-input.md), [сравнение](../references/comparison.md), [replay](../references/historical-replay.md).

Ни один read/status/plan/compare/replay не выполняет скрытое обновление. Аннотации — подсказки хосту, а не механизм разрешений. Политика провайдера и экспортные ограничения проверяются отдельно. Event claim означает доставку, ack — завершённое действие; внешние уведомления и расписания настраивает оператор отдельно. `events retry`, `cache update`, `watch plan/monitor` и добровольные метрики доступны в CLI, одноимённых MCP-инструментов нет. [Очередь](../references/monitoring-host.md), [provider policy](../references/provider-policy.md).

SDK/stdio и синтетические тесты не подтверждают реальную установку в Claude/Cursor или Chrome. Восемь новых эмпирических направлений пока без наблюдений; [статус](../docs/research/2026-10-16/experiment-status.json).

## Проверка

```bash
python3 mcp/test_server.py   # дымовой тест: логика + список инструментов SDK
python3 mcp/doctor.py        # диагностика окружения
```
