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
5. `publish-pypi.yml` (по событию Release) собирает пакет и публикует на PyPI
   через OIDC Trusted Publishing — токен в секретах не нужен.

## PyPI: пакет `inn-check-ru` (разовая настройка)

До первого релиза после подключения `publish-pypi.yml`:

1. Войти на https://pypi.org (аккаунт с 2FA).
2. Your account → **Publishing** → **Add a new pending publisher**:
   - PyPI project name: `inn-check-ru`
   - Owner: `ilyautov`
   - Repository name: `inn-check-ru`
   - Workflow name: `publish-pypi.yml`
   - Environment name: пусто
3. Сохранить. Проект появится на PyPI при первой успешной публикации.

Проверка после релиза: `pip install inn-check-ru && inn-check-ru <ИНН>`,
MCP-обёртка: `uvx --from "inn-check-ru[mcp]" inn-check-ru-mcp`.
Если релиз вышел до настройки — Actions → Publish to PyPI → Run workflow → тег.

Известное ограничение колеса: `data/canon_ru.json` в пакет не едет
(py-modules без данных), поэтому `droblenie_check` из установленного пакета
отвечает по выручке «не проверено — канон отсутствует». Из репозитория
(`python3 scripts/...`, скилл, `uvx --from git+...`) канон на месте.

## Реестр MCP: `io.github.ilyautov/inn-check-ru` (ручной шаг)

Манифест `server.json` в корне готов (description ≤ 100 символов, схема
camelCase — оба подводных камня учтены и проверяются версионным гейтом).
Публикация пока РУКАМИ, после того как PyPI отдаст версию:

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
# или из PyPI (после публикации)
claude mcp add inn-check-ru -- uvx --from "inn-check-ru[mcp]" inn-check-ru-mcp
```

## Homepage

Пока не ставим: лендинг — отдельный следующий этап. Когда появится —
`gh api -X PATCH repos/ilyautov/inn-check-ru -f homepage=...` и hero-ссылка
в README с репо на лендинг.
