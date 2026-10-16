# Документы: локальное извлечение и явный выбор стороны

Читать перед извлечением реквизитов из документа, настройкой PDF/OCR или выбором стороны договора.

`document extract` читает реквизиты, `document select` возвращает явно выбранного кандидата. Выбор не сохраняется как отдельное подтверждение человека.
Ни одна из этих команд не обращается к реестрам и не создаёт run.
Подсказки `supplier`, `buyer`, `bank`, `own_company` не являются подтверждением роли.
Даже один найденный кандидат требует явного ID: документ может содержать только реквизиты банка.

```sh
inn-check-workflow document extract invoice.pdf --json > candidates.json
inn-check-workflow document select candidates.json --role supplier --candidate candidate-EXACT-ID --json
inn-check-workflow run start 7707083893 --profile предоплата --json
```

Последняя команда — отдельное явное действие с выбранным ИНН. Не подставляйте примерный ID: возьмите точный `candidate_id` из собственного результата.
`select` принимает массив кандидатов, объект extraction или JSON envelope команды `extract`.

## Установка и OCR

Текст, CSV, HTML, JSON, EML и DOCX используют существующий stdlib-парсер.
Для PDF предусмотрен опциональный extra `inn-check-ru[pdf]`: `pypdf>=6.21,<7`.
Версия 6.21.0 проверена в изолированном окружении Python 3.12; её метаданные требуют Python >=3.9, совместимый с ядром >=3.10.
Основания: [страница выпуска PyPI](https://pypi.org/project/pypdf/6.21.0/), [официальный API извлечения](https://pypdf.readthedocs.io/en/stable/user/extract-text.html).
Сам pypdf не распознаёт изображения.

OCR включается только `--ocr`. Нужны локальные Poppler `pdftoppm`, Tesseract и языковые данные `rus+eng`; проект не устанавливает их и не загружает документы в облако.
Укажите полные пути к исполняемым файлам в настройках хоста:

```sh
export INN_CHECK_PDF_RENDERER=/absolute/path/to/pdftoppm
export INN_CHECK_TESSERACT=/absolute/path/to/tesseract
inn-check-workflow document extract scan.pdf --ocr --json
```

Используется argv-вызов без shell: `pdftoppm -f N -l N -singlefile -r 150 -scale-to 2500 -png input.bin page`, затем `tesseract page.png recognized -l rus+eng --psm 3`.
[Официальная документация Tesseract](https://tesseract-ocr.github.io/tessdoc/Command-Line-Usage.html) описывает порядок аргументов и выбор языков.
Исходное имя файла никогда не становится аргументом команды или временным путём.
OCR применяется только к страницам без извлекаемого текста. Страницы со слоем текста используют его; качество этого слоя и скана автоматически не подтверждается.

## Контракт

```python
extract(data: bytes, *, filename: str, ocr=False) -> dict
select(candidates: list[dict], *, role: str, candidate_id: str | None = None) -> dict
```

Extraction: `schema_version=1,status,candidates,pages,method,limitations`.
Кандидат: `candidate_id,inn,kpp,ogrn,address,context,page,offsets,role_hints`.
`page` начинается с 1. `offsets.start/end` — индексы Unicode-символов приведённого текста страницы; конец исключён. Это не байты исходного PDF/DOCX.
КПП, ОГРН и адрес привязываются только по строке с единственным ИНН; иначе `null`. Они извлекаются как данные, юридическая достоверность не утверждается.
ИНН проверяется существующим алгоритмом и правилом региона. Ошибочные цифры не исправляются и не угадываются.
Смещения берутся из принятого основным парсером вхождения, без повторного поиска цифр: отброшенный артикул с тем же ИНН не передаёт кандидату свои реквизиты. Повторные вхождения на разных строках или страницах сохраняются отдельными кандидатами с разными ID; контекст и роль — недоверенный текст документа.

Selection: `status,selected,candidates,reason_code`. Роли: `supplier|buyer|own_company|bank|signatory|unknown`; `other` сохранён как совместимый алиас `unknown`. В legacy CLI роль по умолчанию — `unknown`. Для каждой роли, включая `unknown`, без явного ID — `needs_input`, `selected=null`; подсказки роли не выбирают сторону и не запускают сеть или run.
Неизвестный ID — `candidate_not_found`; повторные ID или невалидные ИНН — `error/invalid_candidates`.
`selected` означает только явный выбор, а не проверку контрагента.

Лимиты: 20 МиБ входа; 100 PDF-страниц; 60 секунд на весь процесс извлечения, включая импорт PDF, разбор и все OCR-страницы. На отмену дерева предусмотрено отдельное ограниченное cleanup-ожидание существующего process helper.
Дополнительные ограничения: 20 МиБ приведённого текста, 10 000 кандидатов, 24 МиБ результата. Временные файлы имеют фиксированные имена в приватном временном каталоге.
Для DOCX лимит 20 МиБ также действует на распакованный XML каждой читаемой части и сумму XML тела, колонтитулов, сносок и комментариев. Размеры проверяются до открытия частей, затем фактическое чтение ограничено порциями до 64 КиБ и оставшимся общим бюджетом. При превышении отвергается весь документ с `output_too_large`; частично прочитанные кандидаты не возвращаются. Старый CLI сообщает о лимите чтения и возвращает код 1, если ни один документ не разобран.
До создания списка ZIP-частей проверяются и заявленные, и фактические метаданные каталога: не более 2048 частей, имя части до 1024 байт, суммарно до 256 КиБ имён, каталог до 2 МиБ. Это относится и к непрочитанным служебным частям и изображениям. Метаданные ZIP64 выходят за поддерживаемый ограниченный формат DOCX и также дают `output_too_large`.
На POSIX используется принадлежащая вызову process group, на Windows существующий Job Object helper. Потомки renderer/Tesseract наследуют containment; дерево очищается и после нормального выхода.
Это лимиты обработки, не полноценная песочница для произвольного исполняемого файла: пути renderer/Tesseract задаёт доверенный оператор.

Коды ограничений: `missing_pdf_extra`, `encrypted_pdf`, `broken_pdf`, `image_only`, `missing_renderer`, `missing_tesseract`, `ocr_failed`, `timeout`, `too_large`, `too_many_pages`, `output_too_large`, `unsupported_format`, `cleanup_unconfirmed`.
OCR-результат помечен `partial/ocr_unverified`. Отсутствие текста на странице — `partial/image_only`, а не утверждение об отсутствии реквизитов.

## Старый extract_inn CLI

Поведение `--проверить` намеренно изменено: автоматическая проверка всех найденных в документе ИНН запрещена, даже с `--подтверждаю`. Согласие на размер батча не выбирает сторону договора.
Блок `проверка` сохраняет прежние ключи и дополнен `status=needs_input` / `reason_code=explicit_selection_required`.
После получения ID через `document extract` доступен явный совместимый запуск:

```sh
python scripts/extract_inn.py invoice.txt --проверить --candidate candidate-EXACT-ID --role supplier --json
```

В батч передаётся только выбранный ИНН, если он новый/устаревший по прежним правилам. Уже проверенный кандидат не отправляется автоматически повторно.
Функция `проверить(инны, ...)` для явно переданных ИНН, порог батча и запись снимков сохранены.

## MCP

`counterparty_document_extract(text=None, run_id=None, artifact_id=None, ocr=False)` принимает ровно один источник: текст либо пару UUID `run_id` + `artifact_id`. Артефакт должен уже находиться в управляемом хранилище; registry membership и SHA-256 проверяются через `run_store.read_artifact`. Поддерживаемые хранилищем форматы — markdown, JSON, snapshot JSON и DOCX; отдельного публичного загрузчика PDF нет. Локальный PDF доступен через `document extract` CLI.

`counterparty_document_select(candidates, role, candidate_id=None)` принимает список из `extract.data.candidates` и возвращает обычный workflow envelope. Без ID, даже при единственном кандидате, результат `needs_input`. Выбор не создаёт run и не доказывает проверку реквизитов. Контекст кандидата остаётся недоверенными данными.

Оба инструмента offline/readOnly, создают только временные файлы обработки. В публичных аргументах нет `path`, `root`, `filename`, `executable` или base64-загрузки. Хранилище задаётся оператором через `INN_CHECK_RUNS_DIR`. Известные секреты окружения и credentials проходят каноническую редакцию перед ответом.

Для соблюдения отмены MCP **напрямую** запускает фиксированный `document_input.py --stdin-worker` в собственной process group/Job Object и контролирует весь обмен 60-секундным дедлайном. Не вызывайте `extract()` внутри другого supervisor worker: это создаст вложенную независимую группу.
Stdin — один JSON с точным набором полей:

```json
{"schema_version":1,"data_base64":"SU5OIDc3MDcwODM4OTM=","filename":"input.txt","ocr":false}
```

Максимум stdin: `MAX_REQUEST_BYTES = ceil(20 MiB / 3) * 4 + 8192` байт. Декодированные байты повторно ограничены 20 МиБ. `filename` — только подсказка формата, не путь. Другие поля, функции, команды и версии отвергаются. Stdout — один extraction JSON (до 24 МиБ), без полного текста, путей и traceback; сетевые операции отсутствуют.
Внутри этого worker OCR-потомки остаются в группе владельца. Сам `--stdin-worker` не создаёт второй supervisor; его нельзя использовать без внешнего deadline/cleanup.

## Что проверено и что ещё не доказано

`eval/fixtures/document-input/manifest.json` описывает пять собственных синтетических файлов, их SHA-256 и CC0-1.0. Это не реальные счета клиентов.
На этих фикстурах отдельно измеряются найденные ИНН, отказы, ручной выбор и нулевой запуск проверки без явного выбора. Реальный локальный OCR проверен на синтетическом растрированном PDF.
20 реальных документов с согласием владельцев ещё не получены; качество OCR и правильность выбора на реальном корпусе не заявляются.

## Doctor для PDF/OCR

`inn-check-workflow doctor --json` и `counterparty_doctor` показывают совместимость metadata `pypdf>=6.21,<7` без импорта PDF-парсера. Для OCR отдельно отражены `configuration_variable`, `configured_path`, `path_discovered`, `present`, `ready` и результат команды версии. Наличие Tesseract/Poppler в PATH не делает OCR настроенным: извлечение требует явных `INN_CHECK_TESSERACT` и `INN_CHECK_PDF_RENDERER` с абсолютными исполняемыми путями.

Проверка версии ограничена двумя секундами и 16 КиБ принятого вывода, очищает потомков после успеха, отказа, timeout и отмены. В MCP бинарники запускает владелец запроса, вне вложенной группы workflow worker. `readiness_scope=configured_executable_and_version` не подтверждает наличие `rus+eng`, качество распознавания или успешное чтение конкретного PDF. Отсутствие PDF extra, OCR, SDK и API-ключей не блокирует `core_ready`.
