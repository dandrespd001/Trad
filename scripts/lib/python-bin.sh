#!/usr/bin/env bash

resolve_python_bin() {
  local root="${1:-.}"

  if [ -n "${PYTHON_BIN:-}" ]; then
    printf '%s\n' "$PYTHON_BIN"
    return 0
  fi

  if [ -x "$root/.venv312/bin/python" ]; then
    printf '%s\n' "$root/.venv312/bin/python"
    return 0
  fi

  if [ -x "$root/.venv/bin/python" ]; then
    printf '%s\n' "$root/.venv/bin/python"
    return 0
  fi

  printf '%s\n' "python3"
}
