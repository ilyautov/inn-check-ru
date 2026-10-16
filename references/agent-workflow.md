# Сохранённая проверка: маршрут агента

Читать перед новой проверкой, продолжением после закрытия клиента или ручным дополнением. Версия исходников 1.13.0 **не опубликована**. Команды ниже относятся к этой ветке и колесу, собранному из неё; `uvx ...@latest` не подтверждает их наличие.

## Запустить распакованный ZIP скилла

В релизном ZIP находятся `SKILL.md`, `scripts/`, `data/` и `references/`; `pyproject.toml` и консольный executable в него не входят. Из корня распакованного ZIP используйте Python3.10+ напрямую. Во всех дальнейших примерах заменяйте `inn-check-workflow` на `python3 scripts/workflow_cli.py`:

```sh
python3 scripts/workflow_cli.py doctor --profile нейтрально --json
python3 scripts/workflow_cli.py cache plan --profile нейтрально --json
python3 scripts/workflow_cli.py run resume "$RUN_ID" --json
```

Ядру не нужны SDK или установка пакета. Для optional PDF нужен совместимый pypdf в выбранном Python-окружении, для OCR — явно настроенные локальные бинарники. Отсутствующая optional зависимость не делает core doctor неисправным. Следующий раздел относится к полному Git checkout или готовому wheel, а не к ZIP скилла.

## Установить эту версию локально

Из корня полученной ветки, Python 3.10+; временное окружение можно удалить после проверки. Команда `pip install .` устанавливает текущие исходники, а не релиз с PyPI.

```sh
python3 -m venv /tmp/inn-workflow-venv
. /tmp/inn-workflow-venv/bin/activate
python -m pip install .
inn-check-workflow doctor --profile нейтрально --json
inn-check-workflow cache plan --profile нейтрально --json
```

На Windows PowerShell: `py -3.12 -m venv "$env:TEMP\inn-workflow-venv"`, затем `& "$env:TEMP\inn-workflow-venv\Scripts\Activate.ps1"` и `python -m pip install .`. Активация не обязательна: можно вызвать полный путь `...\Scripts\inn-check-workflow.exe`. Для готового локального колеса установите его точный путь через `python -m pip install /path/to/inn_check_ru-1.13.0-py3-none-any.whl`.

Ядро не имеет обязательных внешних зависимостей. MCP: `python -m pip install '.[mcp]'` (`mcp>=1.30,<2`); PDF: `python -m pip install '.[pdf]'` (`pypdf>=6.21,<7`). OCR требует отдельно настроенных локальных Poppler/Tesseract. [Документы](document-input.md), [каналы установки](capability-matrix.md).

`doctor` читает локальное состояние, может запускать ограниченные команды версии доверенных OCR-бинарников, но не делает сетевой probe. `core_ready` не означает доступность реестров или точность OCR. Для явной сетевой диагностики используйте `doctor --probe`; она обращается к источникам и записывает кэш доступности.

## Настроить хост

Пути выбирает оператор, до запуска агента. Страницы, документы и параметры MCP не задают корень, executable или policy-файл.

```sh
export INN_CHECK_RUNS_DIR=/absolute/private/inn/runs
export INN_CHECK_EVENTS_DIR=/absolute/private/inn/events
# Необязательно: ключи задаются вне истории команд и чата.
# CHECKO_API_KEY, DADATA_API_KEY — окружение процесса хоста.
# INN_CHECK_PROXY — URL своей ноды; она видит запросы.
# INN_CHECK_PROVIDER_POLICY — абсолютный путь к проверенному policy JSON.
```

Без переопределения runs лежат в `~/.local/share/inn-check-ru/runs`. CLI допускает доверенный `--store`, **не `--root`**. Значение зависит от команды: `run`/`compare` — каталог runs; `events` — каталог events; `watch` — workspace с `watchlist.json`, `runs/`, `events/`; `metrics` — отдельный каталог метрик. `cache`/`doctor` используют настройки своего общего кэша, `--store` его не переносит. Для extension native host задайте тот же `INN_CHECK_RUNS_DIR`, что у CLI/MCP; `host.json` «каталог» относится к прежним отдельным ручным блокам. [Матрица](capability-matrix.md), [политика провайдеров](provider-policy.md).

## Начать и продолжить

```sh
inn-check-workflow run start 7707083893 --profile нейтрально --json > start.json
```

Это явный сетевой сбор с локальной записью. `нейтрально` выдаёт факты без светофора; цель сделки выбирается профилем (`отсрочка`, `предоплата` и другие). Quick-режим задаётся `--mode quick`, полная проверка — `--mode полный`. Длительность зависит от сети и покрытия.

Каждая команда выводит один JSON envelope: `schema_version`, `status`, `run_id`, `revision`, `data`, `error`, `warnings`, `pending_actions`. `status=partial` или `needs_input` — сохранённый промежуточный результат, который нужно прочитать; это не подтверждение надёжности. Проверяйте также код завершения команды и `error.code`.

Получите **реальный** UUID из `start.json.run_id`; придуманный UUID не создаёт проверку. В POSIX shell:

```sh
RUN_ID=$(python -c 'import json; print(json.load(open("start.json"))["run_id"])')
inn-check-workflow run show "$RUN_ID" --json > current.json
inn-check-workflow run resume "$RUN_ID" --json
```

`show` и `resume` читают сохранённый HEAD, не повторяют сбор и не пишут новую ревизию. В PowerShell: `$RUN_ID = (Get-Content start.json -Raw | ConvertFrom-Json).run_id`. После перезапуска клиента достаточно этого ID и того же хранилища.

`pending_actions` описывает следующие действия: `source_id`, `kind`, `reason_code`, URL, query и нужные ручные поля. Капча, гео, недостающий кэш, ключ, устаревший факт и конфликт требуют разных действий. Не превращайте «недоступно» в «ничего нет». Само чтение очереди шагов не даёт разрешения на платный fallback, обновление или браузерный поиск.

## Явный ручной шаг и обновление

Сверьте ИНН, профиль, текущую ревизию и источник. Поиск в браузере и капчу выполняет пользователь в рамках согласованного действия. `blocks.json` — объект с ключами источников, например `фссп`, по [контракту ручного блока](brauzer.md). Для каждого блока нужны тот же ИНН, официальный URL, дата наблюдения, параметры поиска и исход:

- `completed`: поиск завершён, результаты заполнены, полнота и просмотр всех страниц заявлены явно;
- `incomplete`: капча, недоступность, часть страниц или незавершённый поиск; пробел остаётся;
- `declined`: человек отказался; отказ сохраняется, проверка не считается выполненной.

`completed` не подписывает достоверность страницы. Отрицательные результаты без подтверждённого охвата остаются неизвестными в решении. Не заполняйте пустые обязательные поля нулями.

```sh
REVISION=$(python -c 'import json; print(json.load(open("current.json"))["revision"])')
OPERATION_ID=$(python -c 'import uuid; print(uuid.uuid4())')
inn-check-workflow run manual "$RUN_ID" --expected-revision "$REVISION" --operation-id "$OPERATION_ID" --block-json blocks.json --json
```

`operation_id` — новый UUID4 для нового намерения, `expected_revision` — целое из **последнего чтения**. При потере ответа повторяйте тот же UUID с тем же содержимым; одинаковая операция не создаёт дубль. UUID нельзя переиспользовать с другими блоками. `revision_conflict` требует перечитать run, сверить изменения и заново подтвердить привязку; не подставляйте новую ревизию молча.

Выборочное обновление — отдельная сетевая запись. Сначала снова выполните `run show ... > current.json`, получите `REVISION` и новый `OPERATION_ID`; источник берите из реестра и своего `pending_actions`, а не из имени файла:

```sh
inn-check-workflow run refresh "$RUN_ID" --source егрюл --expected-revision "$REVISION" --operation-id "$OPERATION_ID" --json
```

Обновление может сохранять промежуточные ревизии; зависимые источники включаются по реестру. Прерванная операция и причины неполноты не теряются.

В [расширении](../extension/README.md) явно выберите сохранённый `run_id`, сверьте показанные ИНН/профиль/ревизию и подтвердите привязку. Последняя быстрая проверка не выбирается автоматически. Большой native-ответ может быть `native_projection.kind=binding, complete=false`; полный результат читайте CLI/MCP. Это предел передачи, отдельный от полноты проверки.

## Получить отчёт и артефакт

```sh
inn-check-workflow run render "$RUN_ID" --format markdown --json > rendered.json
inn-check-workflow run export "$RUN_ID" --format docx --json > artifact-receipt.json
inn-check-workflow run snapshot "$RUN_ID" --json
```

`render` возвращает содержимое в `data.content`, без записи в хранилище. `rendered.json` — envelope, а не готовый Markdown; текст можно извлечь явно: `python -c 'import json; print(json.load(open("rendered.json"))["data"]["content"])' > dossier.md`. `--format json` возвращает модель досье.

`export` создаёт управляемый локальный артефакт, возвращает **квитанцию** `data.{artifact_id,format,sha256,source_revision}`, а не байты DOCX и не произвольный выходной путь. Чтение байтов — API `run_store.read_artifact(run_id, artifact_id, root=...)` с проверкой registry/digest. Snapshot также пишет управляемый артефакт отпечатка. `--revision N` фиксирует нужную ревизию. Сохранённая provider policy ограничивает производную выдачу; snapshot для такого run может явно отказать.

Обычное досье не является подписанной выпиской госоргана. SHA-256 архива проверяет целостность байтов, не достоверность источника и не дату публикации. Отдельный старый режим TSA передаёт внешней службе хеш; подпись метки требует внешней проверки. Это отдельный сетевой эффект, который нельзя подразумевать из локального `export`.

## Документ, сравнение, история

```sh
inn-check-workflow document extract invoice.pdf --json > candidates.json
inn-check-workflow document select candidates.json --role supplier --candidate "$CANDIDATE_ID" --json
inn-check-workflow compare "$RUN_ID" "$OTHER_RUN_ID" --as-of 2026-10-16T09:00:00Z --json
inn-check-workflow run replay "$RUN_ID" --mode original --cutoff 2026-10-16T09:00:00Z --json
```

`CANDIDATE_ID` берётся из собственного extraction (`data.candidates[].candidate_id`), после чтения контекста/страницы и выбора стороны. Явные ID и роль обязательны даже для одного кандидата; `select` не начинает проверку и не сохраняет согласие. Затем отдельно запустите `run start` для выбранного ИНН. Старое `extract_inn --проверить --подтверждаю` не выбирает сторону. PDF/OCR: 20 МиБ, 100 страниц, 60 секунд, без облачного OCR; качество реальных документов ещё не измерено. [Полный контракт](document-input.md).

`OTHER_RUN_ID` — другая сохранённая компания: всего 2–5 разных субъектов, общий профиль и явный `as_of`. Сравнение офлайн, показывает покрытия, периоды и ограничения, не универсальный рейтинг. [Сравнение](comparison.md).

`original` возвращает сохранённое решение; cutoff должен допускать существующую ревизию. `current_rules` явно пересчитывает допустимые сохранённые данные нынешними правилами и не является первоначальным историческим исходом. Нет скрытого запроса прошлого в сеть. Отчётный период, дата действия, retrieval и первое наблюдение не взаимозаменяемы. [Replay](historical-replay.md).

## Мониторинг, события, кэши и метрики

```sh
inn-check-workflow watch plan --profile нейтрально --platform linux --json
inn-check-workflow watch monitor --profile нейтрально --max-subjects 50 --json
inn-check-workflow events list --state pending --json
inn-check-workflow events claim --consumer-id local-agent --limit 10 --lease-seconds 300 --json
inn-check-workflow events ack "$EVENT_ID" --consumer-id local-agent --json
inn-check-workflow events retry "$EVENT_ID" --consumer-id local-agent --json
inn-check-workflow cache status --profile нейтрально --json
inn-check-workflow cache plan --profile нейтрально --json
inn-check-workflow cache update --profile нейтрально --dataset "$DATASET_ID" --max-bytes 10485760 --timeout-seconds 30 --json
inn-check-workflow doctor --profile нейтрально --probe --json
inn-check-workflow metrics record EVENT.json --enabled --store /absolute/private/inn/metrics --json
inn-check-workflow metrics summarize --store /absolute/private/inn/metrics --json
inn-check-workflow metrics export --store /absolute/private/inn/metrics --json
```

`watch plan` только печатает план, `watch monitor` реально собирает и пишет локальное состояние по заранее подготовленному watchlist. Настройку ОС делает оператор отдельно. Никакие расписания и уведомления не устанавливаются сами. `EVENT_ID` возьмите из результата claim: ack подтверждает **успешно выполненное хостом действие**, retry освобождает неудачное для повторной попытки; не вызывайте оба для одного успешного действия. Уведомления отправляет отдельно настроенный потребитель с event_id как ключом идемпотентности. [Мониторинг и очередь](monitoring-host.md).

`DATASET_ID` берите из собственного плана кэша. Обновление явно ограничено байтами и временем; получение плана ничего не скачивает. Бюджет не является гарантией обновления всех датасетов.

Метрики выключены по умолчанию; `EVENT.json` подготовьте по [строгой схеме](local-metrics.md). Без `--enabled` запись не читает даже файл события. Summarize/export возвращают только агрегаты в stdout, не исходные события и не отправку. `--store` здесь — отдельный каталог метрик.

## Как формулировать результат

Состояния знания `known`, `unknown`, `conflict`, `not_applicable`, полярность сигнала и полнота поиска — разные оси. Зеркала одного первичного реестра не независимые подтверждения. Нужный неизвестный факт запрещает неподдержанный зелёный вывод; найденный стоп-сигнал не исчезает из-за неполноты других источников. Экспертные пороги не являются вероятностью дефолта отдельной компании. [Доказательства](evidence-contract.md), [калибровка](kalibrovka.md).

Завершите ответ: какой run/revision прочитан, какие решающие факты и raw-ссылки доступны, что неизвестно/конфликтует, какое следующее действие нужно и где квитанция артефакта. Синтетические тесты подтверждают механизм, но все восемь новых эмпирических направлений пока имеют 0 наблюдений: [протокол](../docs/research/2026-10-16/pilot-protocol.md), [журнал](../docs/research/2026-10-16/experiment-status.json).
