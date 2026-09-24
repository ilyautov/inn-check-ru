# syntax=docker/dockerfile:1
# inn-check-ru как контейнер: MCP-сервер проверки контрагента по ИНН для
# Docker MCP Toolkit (реестр docker/mcp-registry собирает образ из этого файла).
#
#   docker run -i --rm [-e CHECKO_API_KEY=...] mcp/inn-check-ru
#
# Движок — чистый stdlib, MCP SDK ставится через extra [mcp]. Исходники
# удаляются после установки: в образе остаётся только установленный пакет.
# Python закреплён намеренно — тот же, на котором гоняется CI.
FROM python:3.12-slim

LABEL io.modelcontextprotocol.server.name="io.github.ilyautov/inn-check-ru" \
      org.opencontainers.image.title="inn-check-ru" \
      org.opencontainers.image.description="Russian counterparty due diligence by INN over open registries: risk traffic light where an unreachable source is 'not checked', never clean" \
      org.opencontainers.image.source="https://github.com/ilyautov/inn-check-ru" \
      org.opencontainers.image.licenses="Apache-2.0"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

COPY pyproject.toml README.md LICENSE /src/
COPY scripts/ /src/scripts/
COPY mcp/ /src/mcp/
COPY data/ /src/data/

RUN pip install "/src[mcp]" \
    && rm -rf /src /root/.cache \
    && useradd --create-home --uid 1000 mcp

# Кэши источников (реестры санкций, доступность) — в ~/.cache пользователя mcp.
USER mcp

# Сервер общается по stdio, порт не открывается.
ENTRYPOINT ["inn-check-ru-mcp"]
