#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEXT_SUFFIXES = {".md", ".py", ".sh", ".txt", ".yml", ".yaml"}
SKIP_DIRS = {".git", ".venv", ".venv312", "__pycache__", "reports/tmp", "data/raw/approved"}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("live", "futures"), required=True)
    args = parser.parse_args()

    if args.mode == "live":
        pattern = re.compile(r"(live_trading_authorized|live_trading_allowed)([ \t]*[:=][ \t]*true)", re.IGNORECASE)
        paths = [ROOT / "src", ROOT / "configs", ROOT / "scripts", ROOT / "docs", ROOT / "README.md", ROOT / ".github"]
        label = "live trading authorization string found"
    else:
        pattern = re.compile(r'subparsers\.add_parser\("futures-(execute|submit)"')
        paths = [ROOT / "src", ROOT / "tests"]
        label = "futures execution parser found"

    matches = list(_scan(paths, pattern))
    if matches:
        print(label, file=sys.stderr)
        for path, line_number, line in matches:
            print(f"{path.relative_to(ROOT)}:{line_number}: {line}", file=sys.stderr)
        return 1
    return 0


def _scan(paths: list[Path], pattern: re.Pattern[str]) -> list[tuple[Path, int, str]]:
    matches: list[tuple[Path, int, str]] = []
    for path in paths:
        if path.is_file():
            matches.extend(_scan_file(path, pattern))
        elif path.is_dir():
            for candidate in path.rglob("*"):
                if _skip(candidate) or not candidate.is_file() or candidate.suffix not in TEXT_SUFFIXES:
                    continue
                matches.extend(_scan_file(candidate, pattern))
    return matches


def _scan_file(path: Path, pattern: re.Pattern[str]) -> list[tuple[Path, int, str]]:
    found: list[tuple[Path, int, str]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except UnicodeDecodeError:
        return found
    for index, line in enumerate(lines, start=1):
        if pattern.search(line):
            found.append((path, index, line.strip()))
    return found


def _skip(path: Path) -> bool:
    relative = path.relative_to(ROOT).as_posix()
    return any(relative == item or relative.startswith(item + "/") for item in SKIP_DIRS)


if __name__ == "__main__":
    raise SystemExit(main())
