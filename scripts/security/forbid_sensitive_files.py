#!/usr/bin/env python3
"""Block secret-bearing files from entering Git.

Adapted from letya999/ai-repo-safety-skill. The one local change for this repo:
`.mcp.json` is intentionally COMMITTED — it is the Claude-plugin distribution
manifest (stdio servers via ${CLAUDE_PLUGIN_ROOT}, no secrets). It is allow-listed
here and its CONTENT is still verified by scan_mcp_config.py (defense in depth).
"""
from __future__ import annotations

import argparse
import fnmatch
import subprocess  # nosec
from pathlib import Path

DENY = [
    ".env", ".env.*", "*.pem", "*.key", "*.p12", "*.pfx", "id_rsa", "id_ed25519",
    "credentials.json", "credentials.*.json", "service-account*.json", "token.json",
    "tokens.json", "secrets.json", ".mcp.json", "claude_desktop_config.json", "*.ovpn",
    "cabinets.json",  # the local credential store for this project (chmod 600)
]
# Vetted, intentionally-tracked files that match a DENY pattern but carry no secrets.
ALLOW = [
    ".env.example", "example.credentials.json", "credentials.example.json",
    ".mcp.json",  # plugin distribution manifest — secret-free, scanned by scan_mcp_config.py
]
# Public CA certificates (exact paths). rospatent.gov.ru does not send its intermediate,
# so rospatent_tz.py ships it. Content is checked: certificates only, no private key.
ALLOW_CERTS = [
    "data/ca/globalsign_gcc_r3_dv_tls_ca_2020.pem",
]


def only_certificates(path: str) -> bool:
    try:
        text = Path(path).read_text(encoding="ascii")
    except (OSError, UnicodeDecodeError):
        return False
    begins = [line for line in text.splitlines() if line.startswith("-----BEGIN ")]
    return bool(begins) and all(line == "-----BEGIN CERTIFICATE-----" for line in begins)


def git_files(all_files: bool) -> list[str]:
    cmd = ["git", "ls-files"] if all_files else ["git", "diff", "--cached", "--name-only"]
    try:
        out = subprocess.check_output(cmd, text=True, stderr=subprocess.DEVNULL)  # nosec
    except Exception:
        return []
    return [p.strip().replace("\\", "/") for p in out.splitlines() if p.strip()]


def matches(path: str, patterns: list[str]) -> bool:
    name = Path(path).name
    return any(fnmatch.fnmatch(path, pat) or fnmatch.fnmatch(name, pat) for pat in patterns)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--all", action="store_true",
                        help="scan all tracked files instead of staged files")
    args = parser.parse_args()
    blocked = []
    for path in git_files(args.all):
        if matches(path, ALLOW):
            continue
        if path in ALLOW_CERTS and only_certificates(path):
            continue
        if matches(path, DENY):
            blocked.append(path)
    if blocked:
        print("[repo-safety] blocked sensitive files:")
        for path in blocked:
            print(f"  - {path}")
        print("Move real secrets out of Git. Keep only examples like .env.example.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
