#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
# shellcheck source=scripts/lib/python-bin.sh
source "$ROOT/scripts/lib/python-bin.sh"
PYTHON_BIN="$(resolve_python_bin "$ROOT")"
export PYTHON_BIN
REQUIRE_RESEARCH=1
REQUIRE_BROKER=0

while [ "$#" -gt 0 ]; do
  case "$1" in
    --skip-research)
      REQUIRE_RESEARCH=0
      shift
      ;;
    --require-broker)
      REQUIRE_BROKER=1
      shift
      ;;
    -h|--help)
      cat <<'USAGE'
usage: scripts/verify-paper-environment.sh [--skip-research] [--require-broker]

Checks the local operator environment before paper readiness or broker-confirmed
paper runs. By default it requires Python 3.12, PyYAML, pandas, and pyarrow.
Use --require-broker before Alpaca paper-confirmed execution.
USAGE
      exit 0
      ;;
    *)
      echo "unknown argument: $1" >&2
      exit 2
      ;;
  esac
done

cd "$ROOT"

"$PYTHON_BIN" - "$REQUIRE_RESEARCH" "$REQUIRE_BROKER" <<'PY'
from __future__ import annotations

import importlib.util
import json
import sys


require_research = sys.argv[1] == "1"
require_broker = sys.argv[2] == "1"
checks: list[dict[str, object]] = []


def add_check(name: str, ok: bool, detail: str) -> None:
    checks.append({"name": name, "ok": ok, "detail": detail})


version = sys.version_info
add_check(
    "python_version",
    version.major == 3 and version.minor == 12,
    f"{version.major}.{version.minor}.{version.micro}",
)

required_modules = [("yaml", "PyYAML")]
if require_research:
    required_modules.extend([("pandas", "pandas"), ("pyarrow", "pyarrow")])
if require_broker:
    required_modules.append(("alpaca", "alpaca-py"))

for module, package in required_modules:
    add_check(module, importlib.util.find_spec(module) is not None, package)

payload = {
    "status": "OK" if all(item["ok"] for item in checks) else "ERROR",
    "checks": checks,
    "requires": {
        "research": require_research,
        "broker": require_broker,
    },
    "install": {
        "research": 'python -m pip install -e ".[research]"',
        "broker": 'python -m pip install -e ".[broker]"',
    },
}

print(json.dumps(payload, indent=2, sort_keys=True))
if payload["status"] == "OK":
    print("paper environment check passed")
    raise SystemExit(0)

print("paper environment check failed", file=sys.stderr)
for item in checks:
    if not item["ok"]:
        print(f"- {item['name']}: {item['detail']}", file=sys.stderr)
raise SystemExit(1)
PY
