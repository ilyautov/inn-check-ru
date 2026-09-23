# Публикация носителей: релизный ZIP, PyPI, реестр MCP

Ручных шага три (все разовые): social preview в настройках репозитория,
pending publisher на PyPI, первая публикация в реестр MCP. Дальше всё
делают workflow по тегу. Папка `dist/` в git не живёт, поэтому runbook здесь.

## Релизный цикл (тег → всё остальное)

1. Поднять версию везде (истина — frontmatter `SKILL.md`): плагин-манифесты,
   `pyproject.toml`, `server.json`, бейдж README, секция в `CHANGELOG.md`.
   Проверка: `python3 eval/run_version_gate.py`.
2. Если менялись каскад источников / чеклисты — перерисовать картинку:
   `python3 scripts/make_social_preview.py` (гейт свежести — в CI).
3. `git tag vX.Y.Z && git push origin vX.Y.Z`.
4. `release.yml` собирает `dist/inn-check-ru.zip` (SKILL.md в корне архива)
   и создаёт GitHub Release с `--generate-notes`. По ссылке
   `releases/latest/download/inn-check-ru.zip` всегда последний ZIP.
5. `publish-pypi.yml` (по тому же пушу тега) собирает пакет и публикует на PyPI
   через OIDC Trusted Publishing — токен в секретах не нужен.

   **Почему по тегу, а не по событию Release.** До 23.09.2026 здесь стояло
   `on: release: published`, и цепочка «тег → Release → публикация» выглядела
   рабочей. Она не отработала ни разу: Release создаёт `release.yml` через
   `GITHUB_TOKEN`, а события от `GITHUB_TOKEN` новых прогонов не запускают —
   это защита GitHub от петель. Три релиза, ноль прогонов `publish-pypi.yml`,
   PyPI отдаёт 404. Если когда-нибудь понадобится убедиться, что звено живо:
   `gh run list --workflow publish-pypi.yml` — пустой список после релиза
   означает, что оно молчит, а не что публиковать было нечего.

## PyPI: пакет `inn-check-ru` (Trusted Publishing, разовая настройка)

Состояние на 23.09.2026: **опубликованы оба пакета 1.11.1** — `inn-check-ru`
и `inn-check-ru-mcp` (колесо + sdist, Trusted Publishing, токена в секретах нет).
Проверено: CLI из чистого venv отвечает, канон и `proxy` на месте;
`python3 eval/mcp_handshake.py uvx inn-check-ru-mcp==1.11.1` с живого PyPI —
initialize + 14 инструментов.

До этого пакет не публиковался ни разу, и причин было ТРИ — каждая следующая
пряталась за предыдущей:
1. pending publisher на PyPI не заведён (ручной шаг ниже) — заведён 23.09.2026;
2. `publish-pypi.yml` не запускался вовсе: триггер по событию Release, а Release
   создаётся через `GITHUB_TOKEN` (см. пункт 5 выше) — переведён на пуш тега;
3. пин `pypa/gh-action-pypi-publish` указывал на несуществующий коммит — job
   падал на «Set up job». Теперь все пины проверяет job `action-pins`
   в `supply-chain.yml` на каждом пуше.

Дальше публикация идёт сама по пушу тега. Разовые шаги ниже оставлены для
истории и на случай переезда проекта.

**С 1.11.1 пакетов два**, и публикуются они одним прогоном:
- `inn-check-ru` — движок и CLI, без зависимостей;
- `inn-check-ru-mcp` (`packaging/inn-check-ru-mcp/`) — обёртка для реестра MCP:
  кода нет, только исполняемый файл с именем пакета и зависимость
  `inn-check-ru[mcp]==<та же версия>`.

У каждого проекта на PyPI свой Trusted Publisher. Для `inn-check-ru-mcp`
заводится второй pending publisher с теми же полями, кроме имени проекта:
`inn-check-ru-mcp` / `ilyautov` / `inn-check-ru` / `publish-pypi.yml` /
Environment пусто. Без него публикация обёртки падает с
`400 Non-user identities cannot create new projects`.

**Где заводить.** Pending publisher для ЕЩЁ НЕ СУЩЕСТВУЮЩЕГО проекта — только на
странице аккаунта: https://pypi.org/manage/account/publishing/ (форма
«Add a new pending publisher», первое поле — PyPI Project Name). Страница
проекта (`/manage/project/inn-check-ru/settings/publishing/`) выглядит почти
так же, но поля с именем проекта в ней нет: всё, что там добавлено, привязано
к `inn-check-ru`. На 23.09.2026 на этом потеряно три прогона.
Движок при этом может уже уйти на PyPI; перезапуск workflow доливает
недостающее (`skip-existing`).

Как устроена публикация (`.github/workflows/publish-pypi.yml`):
Пуш тега `v*` → job `pypi` с `permissions: id-token: write` (OIDC) →
проверка «тег = версия pyproject» → «версия ещё не на PyPI» → `python -m build`
→ гейт «колесо содержит canon и CLI работает из чистого venv» →
`pypa/gh-action-pypi-publish` (Trusted Publishing, без токена в секретах).
Проект `inn-check-ru` на PyPI создаётся сам при первой успешной публикации,
но только если pending publisher заведён ДО неё.

Последовательность (до первого релиза с этим workflow):

1. Войти на https://pypi.org (аккаунт с 2FA; без 2FA Publishing недоступен).
2. Your account → **Publishing** → **Add a new pending publisher** → GitHub.
   Поля должны совпасть с workflow буквально, иначе OIDC-токен отклоняется
   (`invalid-publisher`):
   - PyPI project name: `inn-check-ru` (= `name` в `pyproject.toml`, он же
     `identifier` в `server.json`)
   - Owner: `ilyautov`
   - Repository name: `inn-check-ru`
   - Workflow name: `publish-pypi.yml` (имя файла, не `name:` внутри)
   - Environment name: пусто (job без `environment:`; если когда-нибудь
     добавить `environment: pypi` в job — завести то же имя и здесь)
3. Сохранить: publisher висит как pending, проект на PyPI ещё не существует —
   это нормально.
4. Локально убедиться, что колесо полное (то же, что гейт в workflow):
   ```bash
   uv build --out-dir /tmp/inn-dist
   unzip -l /tmp/inn-dist/*.whl | grep inn_check_ru_data/canon_ru.json
   uv venv /tmp/inn-venv && uv pip install --python /tmp/inn-venv/bin/python /tmp/inn-dist/*.whl
   (cd /tmp && /tmp/inn-venv/bin/inn-check-ru 123 \
     && /tmp/inn-venv/bin/python -c "import droblenie_check as d; assert d._load_canon(); print('canon ok')")
   ```
   Ожидается: строка с `canon_ru.json` в колесе, JSON «Некорректный ИНН» от
   CLI, `canon ok`. Канон едет в колесо как package-data пакета
   `inn_check_ru_data` (`pyproject.toml`: `package-dir` → `data/`,
   `package-data = ["*.json"]`, маркер `data/__init__.py`);
   `droblenie_check._load_canon` ищет его: путь репозитория →
   `importlib.resources.files("inn_check_ru_data") / "canon_ru.json"`.
5. Выпустить релиз по циклу выше (`git tag vX.Y.Z && git push origin vX.Y.Z`
   → `release.yml` → Release → `publish-pypi.yml`). Версия тега обязана
   совпасть с `pyproject.toml` (гейт в workflow), а `pyproject.toml` — со
   всеми манифестами (`python3 eval/run_version_gate.py`).
6. Если Release уже вышел до настройки publisher (как 1.6.0/1.6.1) —
   Actions → **Publish to PyPI** → Run workflow → существующий тег, например
   `v1.6.1`. Workflow сам выйдет без ошибки, если версия уже на PyPI.
7. После первой успешной публикации на PyPI появится проект; pending
   publisher автоматически станет обычным Trusted Publisher проекта
   (Manage → Publishing). Больше ничего заводить не нужно.

Проверка после релиза:
```bash
for p in inn-check-ru inn-check-ru-mcp; do
  curl -fsS https://pypi.org/pypi/$p/json | python3 -c "import json,sys; print('$p', json.load(sys.stdin)['info']['version'])"
done
uvx inn-check-ru <ИНН>                                  # CLI
python3 eval/mcp_handshake.py uvx inn-check-ru-mcp      # сервер: initialize + tools/list
```

Типовые отказы ДО шага «Публикация»: `Unable to resolve action ...@<sha>,
unable to find version` — пин указывает на коммит, которого нет в репозитории
действия; job падает на «Set up job», не дойдя до первого шага. Проверить все
пины разом: job `action-pins` в `supply-chain.yml` (бежит на каждом пуше).

Типовые отказы на шаге «Публикация»: `invalid-publisher` — не совпало одно из
пяти полей pending publisher (чаще всего Workflow name или Environment);
`403`/`project name already exists` — имя занято чужим проектом (тогда
переименовывать пакет и `identifier` в `server.json`); `400 File already
exists` — эта версия уже загружена, поднять версию, PyPI перезаливать не даёт.

## Реестр MCP: `io.github.ilyautov/inn-check-ru` (ручной шаг)

Манифест `server.json` в корне готов (description ≤ 100 символов, схема
camelCase — оба подводных камня учтены и проверяются версионным гейтом).
Состояние на 23.09.2026: **опубликовано**, `io.github.ilyautov/inn-check-ru`
1.11.1, пакет `inn-check-ru-mcp`.

Публикация пока РУКАМИ, после того как PyPI отдаст ОБА пакета версии
(первые минуты после загрузки CDN PyPI может ещё отвечать 404 — подождать).
JWT реестра живёт недолго: `401 token is expired` лечится повторным
`mcp-publisher login github`.
Реестр проверяет две вещи, и обе сверяет `eval/run_version_gate.py`: у пакета
из `server.json` есть исполняемый файл с тем же именем, и в его README стоит
`mcp-name: io.github.ilyautov/inn-check-ru`. README этого пакета —
`packaging/inn-check-ru-mcp/README.md`, а не корневой.

```bash
brew install mcp-publisher   # или с релизов modelcontextprotocol/registry
mcp-publisher login github
mcp-publisher publish        # из корня репозитория
```

Проверка: `curl -s "https://registry.modelcontextprotocol.io/v0.1/servers?search=inn-check-ru"`.
Автоматизация по паттерну publish-registry.yml из marketplaces-mcp-ru
(OIDC + ожидание PyPI) — следующий шаг, когда пакет стабилизируется на PyPI.

## Social preview репозитория (ручной шаг)

GitHub не даёт API для загрузки Social preview (проверено 19.09.2026).
Руками один раз: Settings → Social preview → Upload →
`assets/social-preview.png` (1200×630).

## Подключение у клиентов

```bash
# MCP-сервер из репозитория
claude mcp add inn-check-ru -- python3 "$PWD/mcp/server.py"
# или с PyPI
claude mcp add inn-check-ru -- uvx inn-check-ru-mcp
```

## Homepage

Пока не ставим: лендинг — отдельный следующий этап. Когда появится —
`gh api -X PATCH repos/ilyautov/inn-check-ru -f homepage=...` и hero-ссылка
в README с репо на лендинг.
