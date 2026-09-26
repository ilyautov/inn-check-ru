"""Точка входа расширения Claude Desktop (.mcpb, тип сервера uv).

Кода сервера здесь нет: он приезжает зависимостью inn-check-ru[mcp] с PyPI той же
версии, что и расширение (pyproject.toml рядом собирает packaging/mcpb/build.py).
"""

from inn_check_ru_mcp.server import entry

if __name__ == "__main__":
    entry()
