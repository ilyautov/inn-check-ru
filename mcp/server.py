#!/usr/bin/env python3
"""
server.py — MCP-сервер inn-check-ru (stdio, FastMCP). Тонкая обёртка:
логика в tools_impl.py, движок — в scripts/ корня репозитория.

Запуск (как его делает .mcp.json плагина):
    uvx --from <корень> --with "mcp>=1.2,<2" inn-check-ru-mcp

Голым скриптом — только если SDK уже стоит в этом интерпретаторе:
    python3 mcp/server.py          # НЕ python3 -m mcp.server (см. README)

Побочные эффекты объявлены для каждого инструмента: сеть, локальные снимки,
проверки и экспорты. Read/resume/render и doctor без probe работают офлайн.
Ключи — из env (CHECKO_API_KEY, DADATA_API_KEY) или из настроек плагина
(userConfig, защищённое хранилище системы), секретов в коде нет.
"""

import os
import re
import sys
from typing import Annotated, Literal

# tools_impl лежит рядом; корень репо в sys.path не добавляем — каталог mcp/
# при запуске через `python -m` теневал бы пакет SDK (поэтому только скриптом).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

def _без_sdk(версия, ошибка):
    """Текст ошибки, когда FastMCP не импортируется: нет пакета, mcp 2.x (FastMCP
    переименован в MCPServer) или сломанная установка 1.x — у каждого свой совет."""
    if not версия:
        return ("Не найден пакет mcp (официальный MCP SDK). Установка: "
                "pip install mcp — подробности в mcp/README.md\n")
    мажор = версия.split(".", 1)[0]
    if not (мажор.isdigit() and int(мажор) == 1):
        return ("Установлен mcp %s, сервер работает на mcp 1.x (>=1.30,<2). Установка: "
                "pip install 'mcp>=1.30,<2' — подробности в mcp/README.md\n" % версия)
    return ("Установлен mcp %s, но FastMCP не импортируется (%s). Переустановка: "
            "pip install --force-reinstall 'mcp>=1.30,<2'\n" % (версия, ошибка))


try:
    from pydantic import Field, StrictBool, StrictStr

    from mcp.server.fastmcp import FastMCP
    from mcp.types import ToolAnnotations
except ImportError as _ошибка:
    try:
        from importlib.metadata import version as _ver
        _sdk = _ver("mcp")
    except Exception:
        _sdk = None
    sys.stderr.write(_без_sdk(_sdk, _ошибка))
    sys.exit(1)

try:
    from . import tools_impl  # установка колесом (пакет inn_check_ru_mcp)
except ImportError:
    import tools_impl  # запуск скриптом из mcp/

tools_impl.ключи_из_плагина(os.environ)

def _версия():
    """Версия пакета для serverInfo. Без неё FastMCP отдаёт клиенту свою
    собственную версию SDK — клиент видит не тот номер, что в релизе."""
    # запуск из исходников: источник истины — frontmatter SKILL.md. Он идёт
    # первым намеренно: в venv разработчика может лежать старая установка
    # пакета, и её метаданные соврали бы про версию работающего кода.
    try:
        skill = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "SKILL.md")
        with open(skill, encoding="utf-8") as f:
            head = f.read(2048)
        m = re.search(r'^\s*version:\s*"([^"]+)"', head, re.MULTILINE)
        if m:
            return m.group(1)
    except OSError:
        pass
    # установка колесом: SKILL.md рядом нет, версия — из метаданных пакета
    try:
        from importlib.metadata import version
        return version("inn-check-ru")
    except Exception:
        return None


class WorkflowMCP(FastMCP):
    async def list_tools(self):
        tools = await super().list_tools()
        for tool in tools:
            if tool.name in tools_impl.WORKFLOW_TOOLS:
                tool.inputSchema['additionalProperties'] = False
            if tool.name == 'counterparty_document_extract':
                tool.inputSchema['oneOf'] = [
                    {'required': ['text'], 'properties': {'text': {'type': 'string'},
                        'run_id': {'type': 'null'}, 'artifact_id': {'type': 'null'}}},
                    {'required': ['run_id', 'artifact_id'], 'properties': {'text': {'type': 'null'},
                        'run_id': {'type': 'string'}, 'artifact_id': {'type': 'string'}}},
                ]
        return tools

    async def call_tool(self, name, arguments):
        if name in tools_impl.WORKFLOW_TOOLS:
            # Preserve a JSON envelope even for schema-invalid raw MCP input;
            # FastMCP otherwise coerces values or returns a string ToolError.
            try:
                tools_impl.validate_workflow_arguments(name, arguments)
            except Exception:
                return tools_impl.workflow_error()
            try:
                return await super().call_tool(name, arguments)
            except Exception:
                return tools_impl.workflow_error('execution_error')
        return await super().call_tool(name, arguments)


def effects(*, writes=False, network=False, idempotent=True):
    return ToolAnnotations(readOnlyHint=not writes, destructiveHint=False,
                           idempotentHint=idempotent, openWorldHint=network)


mcp_server = WorkflowMCP("inn-check-ru")

# FastMCP v1 не принимает version в конструкторе, а serverInfo его берёт с
# низкоуровневого Server — без этого клиент видит версию SDK вместо нашей.
# Приватный атрибут: если SDK его переименует, версия просто не проставится,
# сервер продолжит работать (проверяется дымовым тестом mcp/test_server.py).
_v = _версия()
if _v:
    _low = getattr(mcp_server, "_mcp_server", None)
    if _low is not None and hasattr(_low, "version"):
        _low.version = _v


@mcp_server.tool(annotations=effects(writes=True, network=True, idempotent=False))
async def counterparty_fetch(inn: str, save: bool = False) -> dict:
    """Полная проверка контрагента по ИНН: ЕГРЮЛ/ЕГРИП, риск-флаги ФНС,
    финансы (ГИР БО), Реестр МСП (✅ верифицирован), статус самозанятого (НПД),
    ЕРКНМ/РНП по локальному кэшу. Базовый контур — БЕЗ браузерных слоёв
    (суды/банкротства/ФССП). save=True сохраняет снимок для мониторинга.

    Full counterparty lookup by Russian tax ID (INN): registries, risk flags,
    finances, SME registry, self-employed status. Base tier only — courts,
    bankruptcy and bailiff data are a separate browser/manual layer.
    """
    return await tools_impl.legacy_call('counterparty_fetch', locals())


@mcp_server.tool(annotations=effects(writes=False, network=True, idempotent=True))
async def counterparty_fin_scoring(inn: str) -> dict:
    """Финансовый профиль кодом: чистые активы vs ст. 30 ФЗ-14 (🔴),
    автономия, ликвидность, динамика выручки, транзитный профиль. Каждый флаг
    с сырыми строками баланса; нет строк — «не проверено».

    Code-computed financial red flags from balance-sheet lines (negative net
    assets, autonomy, liquidity, revenue dynamics), each traceable to source.
    """
    return await tools_impl.legacy_call('counterparty_fin_scoring', locals())


@mcp_server.tool(annotations=effects(writes=False, network=False, idempotent=True))
async def sanctions_check(inn: str = "", name: str = "") -> dict:
    """Офлайн-сверка с перечнями РФМ (террористы/экстремисты), OFAC SDN (США)
    и EU Consolidated (ЕС) по локальному кэшу. По ИНН — точно; по имени —
    точное нормализованное совпадение + пометка «ручная сверка по дате
    рождения». Совпадение в списках ЕС/США — риск вторичных санкций.

    Offline sanctions screening against Rosfinmonitoring, OFAC SDN and EU
    lists (local cache). Exact INN match; name matches require manual
    date-of-birth verification.
    """
    return await tools_impl.legacy_call('sanctions_check', locals())


@mcp_server.tool(annotations=effects(writes=False, network=True, idempotent=True))
async def affiliates_graph(inn: str) -> dict:
    """Граф связей глубины 2: общие директора/учредители, соседи по адресу,
    правопреемство (checko API, env CHECKO_API_KEY; без ключа — честное
    «не проверено»). Рёбра помечены tier ⚠️ «один источник, требует
    пересечения».

    Affiliation graph (depth 2): shared directors/founders, same-address
    neighbours, succession. Edges are tier-marked as single-source.
    """
    return await tools_impl.legacy_call('affiliates_graph', locals())


@mcp_server.tool(annotations=effects(writes=False, network=True, idempotent=True))
async def droblenie_check(inn: str) -> dict:
    """Признаки дробления бизнеса кодом по графу связей: общий директор/
    адрес/ОКВЭД, все на УСН, каждая компания под порогом НДС — группа над
    ним. Вывод всегда «N признаков, совпадающих с типовыми доводами ФНС»
    (письмо БВ-4-7/8051@) — никогда «это дробление».

    Business-splitting red flags computed over the affiliation graph. Returns
    'N signs consistent with typical FTS arguments' — never a verdict.
    """
    return await tools_impl.legacy_call('droblenie_check', locals())


@mcp_server.tool(annotations=effects(writes=False, network=False, idempotent=True))
async def counterparty_diff(inn: str) -> dict:
    """Мониторинг изменений: diff двух последних снимков (снимки делаются
    counterparty_fetch с save=True). Ловит смену статуса 🔴, директора 🟡,
    недостоверность 🔴, ухудшение финансов — с датами «было/стало».

    Snapshot diff monitoring: status/director/address changes, unreliability
    flags, financial deterioration — with before/after dates.
    """
    return await tools_impl.legacy_call('counterparty_diff', locals())


@mcp_server.tool(annotations=effects(writes=False, network=False, idempotent=True))
async def retro_verdict(inn: str, date: str, profile: str = "") -> dict:
    """Вердикт на дату в ПРОШЛОМ по снимку, сохранённому тогда: чем доказывают
    должную осмотрительность — тем, что было видно ДО сделки, а не сегодняшней
    карточкой. Снимок новее запрошенной даты не берётся, недостающее из сети не
    добирается, «источник не сохранён» не превращается в «признака не было».
    Без снимков на дату — честный отказ, а не сегодняшние данные задним числом.

    Retrospective verdict as of a past date, computed from the snapshot stored
    back then: never uses data newer than the requested date, never refetches.
    """
    return await tools_impl.legacy_call('retro_verdict', locals())


@mcp_server.tool(annotations=effects(writes=False, network=True, idempotent=True))
async def counterparty_verdict(inn: str, profile: str = "нейтрально") -> dict:
    """Вердикт по контрагенту одной командой: сбор + финансы + резолвер профиля
    цели. Возвращает светофор 🟢/🟡/🔴 (или null, если проверка НЕ состоялась),
    сигналы, поднявшие его, рекомендацию и перечень «не проверено». Профили:
    нейтрально (факты без вердикта), отсрочка, предоплата, подрядчик, доля,
    клиент_115фз, самопроверка, ндс_вычет, получаю_предоплату, тендер,
    цепочка_поставки — одни и те же факты весят
    по-разному. Нейтрально, ндс_вычет и цепочка_поставки светофора не выносят: отдают факты.

    One-call verdict for a counterparty: collect + financials + target-profile
    resolver. Returns a traffic light (null when the check did not actually
    happen), the signals behind it, a recommendation and what stayed unchecked.
    """
    return await tools_impl.legacy_call('counterparty_verdict', locals())


@mcp_server.tool(annotations=effects(writes=False, network=True, idempotent=True))
async def counterparty_batch(inns: list[str], profile: str = "нейтрально") -> dict:
    """Батч-проверка списка ИНН (до 50 за вызов): по каждому быстрая проверка
    и светофор профиля. Сортировка 🔴 → «проверка не состоялась» → 🟡 → 🟢 —
    опасное и непроверенное сверху. Сбой по одному ИНН не роняет батч.

    Batch screening of up to 50 INNs per call: quick check plus a per-profile
    traffic light, sorted danger-first. A failure on one INN never kills the
    batch — that row comes back as 'not checked' with a reason.
    """
    return await tools_impl.legacy_call('counterparty_batch', locals())


@mcp_server.tool(annotations=effects(writes=True, network=True, idempotent=False))
async def access_check() -> dict:
    """Что реально доступно с текущей сети: probe по всем источникам (кэш на
    сутки) — доступен / гео (нужен РФ-IP) / tls (нужен корень УЦ Минцифры) /
    капча / dns. Диагностика перед тем, как верить «не проверено» в выводах.

    Source availability probe from the current network (cached 24h): reachable,
    geo-blocked (needs a Russian IP), TLS (needs the Russian CA root), captcha
    or DNS. Run it before trusting any 'not checked' verdicts.
    """
    return await tools_impl.legacy_call('access_check', locals())


@mcp_server.tool(annotations=effects(writes=False, network=True, idempotent=True))
async def industry_benchmarks(inn: str) -> dict:
    """Где компания относительно отраслевой нормы: перцентиль выручки,
    налоговая нагрузка, численность и доля УСН по группе (ОКВЭД2, регион,
    размер). Нормы собраны из открытых дампов ФНС. Группы меньше порога
    наблюдений не публикуются, у каждого показателя свой счётчик, а для
    среднего и крупного бизнеса ответ несёт предупреждение: ОКВЭД в открытых
    данных есть только у субъектов МСП, поэтому настоящего крупного бизнеса
    в этих группах нет.

    Where a company sits against its industry: revenue percentile, tax burden,
    headcount and simplified-regime share for its (activity, region, size)
    group, built from Russian tax service open data dumps.
    """
    return await tools_impl.legacy_call('industry_benchmarks', locals())


@mcp_server.tool(annotations=effects(writes=False, network=True, idempotent=True))
async def paper_vat_signs(inn: str, subject: str = "", amount: str = "") -> dict:
    """Признаки «технической» компании, из-за которой покупателю снимают вычет
    по НДС: возраст, ресурсы, массовость адреса и руководителя, спецрежим,
    несоразмерность выручки штату. Язык фактов, а не налоговый вывод — что
    видно в открытых данных, чего не видно и что осталось непроверенным.
    subject — предмет сделки (сверяется с ОКВЭД), amount — сумма в рублях.

    Signs of a shell counterparty that can cost the buyer its VAT deduction.
    States what open data shows, never a tax conclusion.
    """
    return await tools_impl.legacy_call('paper_vat_signs', locals())


@mcp_server.tool(annotations=effects(writes=True, network=True, idempotent=False))
async def due_diligence_dossier(inn: str, profile: str = "нейтрально",
                          subject: str = "", amount: str = "",
                          evidence_dir: str = "", timestamp: bool = False) -> dict:
    """Досье должной осмотрительности в Markdown: что было видно в открытых
    источниках на дату проверки, что осталось непроверенным и почему.
    Документ заключением не является и ценен только тогда, когда составлен ДО
    сделки — это написано в нём самом. Файл DOCX собирает dossier.py.
    evidence_dir — новый/пустой локальный каталог для пакета доказательств
    (сырые ответы источников + манифест SHA-256); timestamp — штамп времени
    RFC 3161 (на публичный TSA уходит только хеш).

    Due-diligence dossier as Markdown: what open sources showed on the date of
    the check, and what stayed unchecked. Not an opinion, and only meaningful
    when produced BEFORE the deal. evidence_dir saves raw source responses with
    a SHA-256 manifest; timestamp adds an RFC 3161 timestamp (hash only).
    """
    return await tools_impl.legacy_call('due_diligence_dossier', locals())


@mcp_server.tool(annotations=effects(writes=False, network=False, idempotent=True))
async def extract_inns(text: str, only_new: bool = False) -> dict:
    """Вынуть ИНН из текста документа: счёта, договора, письма, выгрузки.
    Мусор режется контрольным числом ФНС — номера счетов, телефоны, расчётные
    счета и КПП отсеиваются с причиной. Каждый найденный ИНН с контекстом и
    пометкой «новый» либо «уже проверялся» по кэшу снимков. Сеть не трогает:
    это разбор текста, а не проверка. Дальше новые ИНН идут в
    counterparty_batch.

    Pull Russian tax IDs out of an invoice, contract, email or export. Garbage
    is rejected by the official checksum, with a reason. No network access.
    """
    return await tools_impl.legacy_call('extract_inns', locals())


# Strict schemas are advertised to agents; raw validation above additionally
# rejects unknown keys and SDK string-to-object/coercion shortcuts.
RunId = Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")]
Revision = Annotated[int, Field(strict=True, ge=1)]


@mcp_server.tool(annotations=effects(writes=True, network=True, idempotent=False))
async def counterparty_run_start(inn: StrictStr, profile: StrictStr = 'нейтрально',
                                 mode: StrictStr = 'полный') -> dict:
    """Start collection over the network; save a resumable local run and evidence."""
    return await tools_impl.counterparty_run_start(inn, profile, mode)


@mcp_server.tool(annotations=effects())
async def counterparty_run_read(run_id: RunId, revision: Revision | None = None) -> dict:
    """Read and verify a saved revision; omit revision for HEAD. No writes or network."""
    return await tools_impl.counterparty_run_read(run_id, revision)


@mcp_server.tool(annotations=effects())
async def counterparty_run_resume(run_id: RunId) -> dict:
    """Read HEAD and pending actions; no collection, writes or network."""
    return await tools_impl.counterparty_run_resume(run_id)


@mcp_server.tool(annotations=effects(writes=True))
async def counterparty_run_manual(run_id: RunId, blocks: dict[str, dict],
                                  expected_revision: Revision, operation_id: RunId) -> dict:
    """Validate a manual blocks object and append a local revision with CAS/idempotency."""
    return await tools_impl.counterparty_run_manual(run_id, blocks, expected_revision, operation_id)


@mcp_server.tool(annotations=effects(writes=True, network=True))
async def counterparty_run_refresh(run_id: RunId, source_ids: Annotated[list[StrictStr], Field(min_length=1)],
                                   expected_revision: Revision, operation_id: RunId) -> dict:
    """Explicitly refresh selected sources over the network and append a saved revision."""
    return await tools_impl.counterparty_run_refresh(run_id, source_ids, expected_revision, operation_id)


@mcp_server.tool(annotations=effects())
async def counterparty_run_render(run_id: RunId, format: Literal['markdown', 'json'] = 'markdown',
                                  revision: Revision | None = None) -> dict:
    """Render a verified saved revision in memory. No writes or network."""
    return await tools_impl.counterparty_run_render(run_id, format, revision)


@mcp_server.tool(annotations=effects(writes=True, idempotent=False))
async def counterparty_run_export(run_id: RunId, format: Literal['markdown', 'json', 'docx'] = 'markdown',
                                  revision: Revision | None = None) -> dict:
    """Write a local managed artifact with digest and origin revision. No network."""
    return await tools_impl.counterparty_run_export(run_id, format, revision)


@mcp_server.tool(annotations=effects(writes=True, idempotent=False))
async def counterparty_run_snapshot(run_id: RunId, revision: Revision | None = None) -> dict:
    """Save a local managed monitoring fingerprint of a verified revision. No network."""
    return await tools_impl.counterparty_run_snapshot(run_id, revision)


@mcp_server.tool(annotations=effects())
async def counterparty_doctor(profile: StrictStr = 'нейтрально') -> dict:
    """Offline installation, dependency and cache diagnostics; no writes or network probes."""
    return await tools_impl.counterparty_doctor(profile)


@mcp_server.tool(annotations=effects(writes=True, network=True, idempotent=False))
async def counterparty_probe(profile: StrictStr = 'нейтрально') -> dict:
    """Explicit network diagnostic probes; writes local availability cache."""
    return await tools_impl.counterparty_probe(profile)


@mcp_server.tool(annotations=effects())
async def counterparty_cache_status(profile: StrictStr, now: StrictStr) -> dict:
    """Read cache freshness as of an explicit ISO timestamp. No writes or network."""
    return await tools_impl.counterparty_cache_status(profile, now)


@mcp_server.tool(annotations=effects())
async def counterparty_cache_plan(profile: StrictStr, now: StrictStr) -> dict:
    """Return a cache preparation plan as of an ISO timestamp; do not execute it."""
    return await tools_impl.counterparty_cache_plan(profile, now)


ConsumerId = Annotated[StrictStr, Field(pattern=r'^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$')]
EventId = Annotated[StrictStr, Field(pattern=r'^[0-9a-f]{64}$')]
EventLimit = Annotated[int, Field(strict=True, ge=1, le=50)]
LeaseSeconds = Annotated[int, Field(strict=True, ge=1, le=3600)]


@mcp_server.tool(annotations=effects())
async def counterparty_events_list(
        state: Literal['pending', 'delivered', 'acknowledged', 'dead_letter', 'all'] = 'pending',
        limit: EventLimit = 50) -> dict:
    """Read durable monitoring events without claiming or acknowledging them. Offline, no writes."""
    return await tools_impl.counterparty_events_list(state, limit)


@mcp_server.tool(annotations=effects(writes=True, idempotent=False))
async def counterparty_events_claim(consumer_id: ConsumerId, limit: EventLimit = 50,
                                    lease_seconds: LeaseSeconds = 300) -> dict:
    """Lease local monitoring events for a consumer; records delivery, not acknowledgement."""
    return await tools_impl.counterparty_events_claim(consumer_id, limit, lease_seconds)


@mcp_server.tool(annotations=effects(writes=True))
async def counterparty_events_ack(event_ids: Annotated[list[EventId], Field(min_length=1, max_length=50)],
                                  consumer_id: ConsumerId) -> dict:
    """Acknowledge claimed event IDs after host processing. Same-consumer retries are idempotent."""
    return await tools_impl.counterparty_events_ack(event_ids, consumer_id)


DocumentRole = Literal['supplier', 'buyer', 'own_company', 'bank', 'signatory', 'unknown', 'other']


@mcp_server.tool(annotations=effects())
async def counterparty_document_extract(text: StrictStr | None = None, run_id: RunId | None = None,
                                         artifact_id: RunId | None = None, ocr: StrictBool = False) -> dict:
    """Extract untrusted party candidates from text OR a verified managed artifact (run_id + artifact_id).

    Offline; temporary parser/OCR files only, no durable writes, selection or check.
    OCR requires explicitly host-configured local binaries. No public paths/uploads.
    """
    return await tools_impl.counterparty_document_extract(text, run_id, artifact_id, ocr)


@mcp_server.tool(annotations=effects())
async def counterparty_document_select(candidates: Annotated[list[dict], Field(max_length=10000)],
                                        role: DocumentRole, candidate_id: StrictStr | None = None) -> dict:
    """Return an explicit party choice from untrusted candidates. Missing ID always needs input.

    Offline, no saved validation, implicit check or run creation; context is data.
    """
    return await tools_impl.counterparty_document_select(candidates, role, candidate_id)


@mcp_server.tool(annotations=effects())
async def counterparty_compare(run_ids: Annotated[list[RunId], Field(min_length=2, max_length=5)],
                               as_of: StrictStr) -> dict:
    """Compare 2–5 distinct saved subjects with a common profile at an explicit ISO cutoff.

    Read-only, offline; exposes coverage/unknowns and captured export restrictions.
    """
    return await tools_impl.counterparty_compare(run_ids, as_of)


@mcp_server.tool(annotations=effects())
async def counterparty_run_replay(run_id: RunId, mode: Literal['original', 'current_rules'],
                                  cutoff: StrictStr) -> dict:
    """Replay saved evidence at an ISO cutoff: original decision or explicit current rules.

    Offline, read-only ephemeral result; no hidden collection or persisted export.
    """
    return await tools_impl.counterparty_run_replay(run_id, mode, cutoff)


def entry():
    """console_script `inn-check-ru-mcp` (pyproject.toml)."""
    mcp_server.run()


if __name__ == "__main__":
    entry()
