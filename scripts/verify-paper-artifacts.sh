#!/usr/bin/env bash
set -euo pipefail

ROOT=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --root)
      if [ "$#" -lt 2 ]; then
        echo "--root requires a path" >&2
        exit 2
      fi
      ROOT="$2"
      shift 2
      ;;
    -h|--help)
      echo "usage: scripts/verify-paper-artifacts.sh [--root PATH]"
      exit 0
      ;;
    *)
      echo "unknown argument: $1" >&2
      exit 2
      ;;
  esac
done

if [ -z "$ROOT" ]; then
  ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
else
  ROOT="$(cd "$ROOT" && pwd -P)"
fi

PYTHON_BIN="${PYTHON_BIN:-python3}"

"$PYTHON_BIN" - "$ROOT" <<'PY'
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Iterable


root = Path(sys.argv[1])
artifact_suffixes = {".csv", ".html", ".json", ".jsonl", ".md", ".parquet", ".txt"}
allowed_report_prefixes = ("reports/tmp/", "reports/historical/")
ignored_dirs = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    ".venv312",
    "__pycache__",
}
errors: list[str] = []


def rel(path: Path) -> str:
    return path.relative_to(root).as_posix()


def candidate_untracked_paths() -> list[Path]:
    git_check = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--is-inside-work-tree"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        check=False,
    )
    if git_check.returncode == 0 and git_check.stdout.strip() == "true":
        listed = subprocess.run(
            ["git", "-C", str(root), "ls-files", "--others", "--exclude-standard", "-z"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if listed.returncode == 0:
            raw_paths = [item for item in listed.stdout.decode("utf-8").split("\0") if item]
            return [root / item for item in raw_paths]
        errors.append("git ls-files failed while checking generated artifacts")
        return []
    return list(walk_files(root))


def walk_files(start: Path) -> Iterable[Path]:
    for dirpath, dirnames, filenames in os.walk(start):
        dirnames[:] = [name for name in dirnames if name not in ignored_dirs]
        base = Path(dirpath)
        for filename in filenames:
            yield base / filename


def is_generated_output_outside_tmp(path: Path) -> bool:
    if not path.is_file() or path.suffix.lower() not in artifact_suffixes:
        return False
    path_rel = rel(path)
    if path_rel.startswith(allowed_report_prefixes):
        return False
    if path_rel.startswith("reports/"):
        return True
    return "/" not in path_rel


def scan_generated_outputs() -> None:
    for path in candidate_untracked_paths():
        if is_generated_output_outside_tmp(path):
            errors.append(
                f"{rel(path)}: generated artifact outside reports/tmp; "
                "write generated outputs under reports/tmp/ or move intentional snapshots under reports/historical/"
            )


def scan_live_authorization() -> None:
    for base in (root / "reports" / "tmp", root / "reports" / "historical"):
        if not base.exists():
            continue
        for path in sorted(base.rglob("*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                errors.append(f"{rel(path)}: invalid JSON in paper artifact: {exc}")
                continue
            for dotted_path, value in live_authorization_values(payload):
                if value is not False:
                    errors.append(
                        f"{rel(path)}: {dotted_path} must be false, got {value!r}"
                    )


def live_authorization_values(value: object, prefix: str = "") -> Iterable[tuple[str, object]]:
    if isinstance(value, dict):
        for key, item in value.items():
            next_prefix = f"{prefix}.{key}" if prefix else str(key)
            if key in {"live_trading_authorized", "live_trading_allowed"}:
                yield next_prefix, item
            yield from live_authorization_values(item, next_prefix)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            next_prefix = f"{prefix}[{index}]"
            yield from live_authorization_values(item, next_prefix)


scan_generated_outputs()
scan_live_authorization()

if errors:
    print("paper artifact gate failed:", file=sys.stderr)
    for error in errors:
        print(f"- {error}", file=sys.stderr)
    raise SystemExit(1)

print("paper artifact gate passed")
PY
