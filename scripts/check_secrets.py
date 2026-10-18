r"""Fail when a tracked file looks like it carries a credential.

Run before every commit / push:

    .venv\Scripts\python scripts\check_secrets.py

Exit code 0 means clean, 1 means something must be removed.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9]{16,}"),
    re.compile(r"[0-9a-f]{32}\.[A-Za-z0-9]{16,}"),                 # zhipu key shape
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"(?i)(?:api[_-]?key|secret|token)\s*[:=]\s*['\"][^'\"]{16,}['\"]"),
)
SKIP_PREFIXES = ("scripts/check_secrets.py", "tests/", "docs/")
SKIP_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".pack", ".pkl", ".sqlite")


def tracked_files() -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files"], capture_output=True, text=True, check=True
    ).stdout
    return [Path(line) for line in out.splitlines() if line.strip()]


def scan(paths) -> list[tuple[str, int, str]]:
    findings = []
    for path in paths:
        name = str(path).replace("\\", "/")
        if "tests" in Path(path).parts or name.startswith(SKIP_PREFIXES):
            continue
        if name.endswith(SKIP_SUFFIXES):
            continue
        try:
            text = Path(path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for number, line in enumerate(text.splitlines(), start=1):
            for pattern in PATTERNS:
                match = pattern.search(line)
                if match and not _looks_placeholder(match.group(0)):
                    findings.append((name, number, pattern.pattern))
                    break
    return findings


def _looks_placeholder(line: str) -> bool:
    lowered = line.lower()
    return any(
        marker in lowered
        for marker in ("example", "placeholder", "your-", "redacted", "changeme", "***")
    )


def main() -> int:
    findings = scan(tracked_files())
    if not findings:
        print("check_secrets: clean")
        return 0
    for name, line, pattern in findings:
        print(f"SECRET? {name}:{line}  matches {pattern}")
    print(f"check_secrets: {len(findings)} suspicious line(s)")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
