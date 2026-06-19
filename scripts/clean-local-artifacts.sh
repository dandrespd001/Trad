#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
APPLY=0

while [ "$#" -gt 0 ]; do
  case "$1" in
    --apply)
      APPLY=1
      shift
      ;;
    --dry-run)
      APPLY=0
      shift
      ;;
    -h|--help)
      cat <<'USAGE'
usage: scripts/clean-local-artifacts.sh [--dry-run|--apply]

Dry-run is the default. Removes only local ignored caches:
__pycache__, .pytest_cache, .mypy_cache, .ruff_cache, and *.egg-info.
It never removes reports/tmp, data/raw/approved, models, or source files.
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

mapfile -t paths < <(
  find . \
    \( -path './.git' -o -path './.venv' -o -path './.venv312' -o -path './reports/tmp' -o -path './data/raw/approved' \) -prune \
    -o \( -type d \( -name '__pycache__' -o -name '.pytest_cache' -o -name '.mypy_cache' -o -name '.ruff_cache' -o -name '*.egg-info' \) \) \
    -print | sort
)

if [ "${#paths[@]}" -eq 0 ]; then
  echo "no local cache artifacts found"
  exit 0
fi

if [ "$APPLY" -eq 0 ]; then
  echo "dry-run: would remove"
  printf '%s\n' "${paths[@]}"
  exit 0
fi

printf '%s\n' "${paths[@]}" | while IFS= read -r path; do
  rm -rf "$path"
done
echo "removed ${#paths[@]} local cache artifacts"
