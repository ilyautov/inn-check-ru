#!/usr/bin/env python3
"""
server.py — MCP-сервер inn-check-ru (stdio, FastMCP). Тонкая обёртка:
логика в tools_impl.py, движок — в scripts/ корня репозитория.

Запуск:
    pip install mcp
    python3 mcp/server.py          # НЕ python3 -m mcp.server (см. README)

Все инструменты read-only: сервер ничего не пишет во внешние сервисы.
Ключи — только из env (CHECKO_API_KEY), секретов в коде нет.
"""

import os
import sys

# tools_impl лежит рядом; корень репо в sys.path не добавляем — каталог mcp/
# при запуске через `python -m` теневал бы пакет SDK (поэтому только скриптом).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from mcp.server.fastmcp import FastMCP
except ImportError:
    sys.stderr.write(
        "Не найден пакет mcp (официальный MCP SDK). Установка: "
        "pip install mcp — подробности в mcp/README.md\n")
    sys.exit(1)

import tools_impl

mcp_server = FastMCP("inn-check-ru")


@mcp_server.tool()
def counterparty_fetch(inn: str, save: bool = False) -> dict:
    """Полная проверка контрагента по ИНН: ЕГРЮЛ/ЕГРИП, риск-флаги ФНС,
    финансы (ГИР БО), Реестр МСП (✅ верифицирован), статус самозанятого (НПД),
    ЕРКНМ/РНП по локальному кэшу. Базовый контур — БЕЗ браузерных слоёв
    (суды/банкротства/ФССП). save=True сохраняет снимок для мониторинга.

    Full counterparty lookup by Russian tax ID (INN): registries, risk flags,
    finances, SME registry, self-employed status. Base tier only — courts,
    bankruptcy and bailiff data are a separate browser/manual layer.
    """
    return tools_impl.counterparty_fetch(inn, save=save)


@mcp_server.tool()
def counterparty_fin_scoring(inn: str) -> dict:
    """Финансовый профиль кодом: чистые активы vs ст. 30 ФЗ-14 (🔴),
    автономия, ликвидность, динамика выручки, транзитный профиль. Каждый флаг
    с сырыми строками баланса; нет строк — «не проверено».

    Code-computed financial red flags from balance-sheet lines (negative net
    assets, autonomy, liquidity, revenue dynamics), each traceable to source.
    """
    return tools_impl.counterparty_fin_scoring(inn)


@mcp_server.tool()
def sanctions_check(inn: str = "", name: str = "") -> dict:
    """Офлайн-сверка с перечнями РФМ (террористы/экстремисты), OFAC SDN (США)
    и EU Consolidated (ЕС) по локальному кэшу. По ИНН — точно; по имени —
    точное нормализованное совпадение + пометка «ручная сверка по дате
    рождения». Совпадение в списках ЕС/США — риск вторичных санкций.

    Offline sanctions screening against Rosfinmonitoring, OFAC SDN and EU
    lists (local cache). Exact INN match; name matches require manual
    date-of-birth verification.
    """
    return tools_impl.sanctions_check(inn=inn or None, name=name or None)


@mcp_server.tool()
def affiliates_graph(inn: str) -> dict:
    """Граф связей глубины 2: общие директора/учредители, соседи по адресу,
    правопреемство (checko API, env CHECKO_API_KEY; без ключа — честное
    «не проверено»). Рёбра помечены tier ⚠️ «один источник, требует
    пересечения».

    Affiliation graph (depth 2): shared directors/founders, same-address
    neighbours, succession. Edges are tier-marked as single-source.
    """
    return tools_impl.affiliates_graph(inn)


@mcp_server.tool()
def droblenie_check(inn: str) -> dict:
    """Признаки дробления бизнеса кодом по графу связей: общий директор/
    адрес/ОКВЭД, все на УСН, каждая компания под порогом НДС — группа над
    ним. Вывод всегда «N признаков, совпадающих с типовыми доводами ФНС»
    (письмо БВ-4-7/8051@) — никогда «это дробление».

    Business-splitting red flags computed over the affiliation graph. Returns
    'N signs consistent with typical FTS arguments' — never a verdict.
    """
    return tools_impl.droblenie_check(inn)


@mcp_server.tool()
def counterparty_diff(inn: str) -> dict:
    """Мониторинг изменений: diff двух последних снимков (снимки делаются
    counterparty_fetch с save=True). Ловит смену статуса 🔴, директора 🟡,
    недостоверность 🔴, ухудшение финансов — с датами «было/стало».

    Snapshot diff monitoring: status/director/address changes, unreliability
    flags, financial deterioration — with before/after dates.
    """
    return tools_impl.counterparty_diff(inn)


if __name__ == "__main__":
    mcp_server.run()
