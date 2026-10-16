#!/usr/bin/env python3
"""Compatibility entry point for installed core and optional MCP diagnostics."""
import sys
from pathlib import Path

# Direct checkout invocation retains its historic entry point. Installed wheels
# resolve workflow_doctor normally and never require a repository layout.
try:
    from workflow_doctor import main
except ModuleNotFoundError:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'scripts'))
    from workflow_doctor import main


if __name__ == '__main__':
    sys.exit(main())
