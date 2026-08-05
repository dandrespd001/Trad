#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"

PYTHON_BIN="$PROJECT_ROOT/.venv312/bin/python"
if [ ! -x "$PYTHON_BIN" ]; then
  printf 'Required project Python is unavailable: %s\n' "$PYTHON_BIN" >&2
  exit 2
fi
PYTHON_REAL="$(readlink -f "$PYTHON_BIN")"
PYTHON_RUNTIME_ROOT="$(cd "$(dirname "$PYTHON_REAL")/.." && pwd -P)"
VENV_ROOT="$(readlink -f "$PROJECT_ROOT/.venv312")"
shopt -s nullglob
SITE_PACKAGE_CANDIDATES=("$VENV_ROOT"/lib/python*/site-packages)
shopt -u nullglob
if [ "${#SITE_PACKAGE_CANDIDATES[@]}" -ne 1 ]; then
  printf 'Expected exactly one project site-packages directory, found %s\n' \
    "${#SITE_PACKAGE_CANDIDATES[@]}" >&2
  exit 2
fi
PYTHON_SITE_PACKAGES="$(readlink -f "${SITE_PACKAGE_CANDIDATES[0]}")"
case "$PYTHON_SITE_PACKAGES/" in
  "$VENV_ROOT/"*) ;;
  *)
    printf 'Resolved site-packages escapes the project venv: %s\n' \
      "$PYTHON_SITE_PACKAGES" >&2
    exit 2
    ;;
esac
BWRAP_BIN="/usr/bin/bwrap"

ISOLATED_TEST_ROOT="$(mktemp -d /tmp/trading-ai-tests.XXXXXX)"
ISOLATED_CHECKOUT="$ISOLATED_TEST_ROOT/checkout"

cleanup() {
  case "$ISOLATED_TEST_ROOT" in
    /tmp/trading-ai-tests.*)
      rm -rf -- "$ISOLATED_TEST_ROOT"
      ;;
    *)
      printf 'Refusing to remove unexpected isolation path: %s\n' "$ISOLATED_TEST_ROOT" >&2
      ;;
  esac
}
trap cleanup EXIT

if [ ! -x "$BWRAP_BIN" ]; then
  printf 'Required sandbox unavailable: %s\n' "$BWRAP_BIN" >&2
  exit 2
fi
if [ ! -d "$PYTHON_SITE_PACKAGES" ]; then
  printf 'Resolved Python site-packages is unavailable: %s\n' "$PYTHON_SITE_PACKAGES" >&2
  exit 2
fi

mkdir -p "$ISOLATED_CHECKOUT"

tar \
  --directory="$PROJECT_ROOT" \
  --one-file-system \
  --exclude='./.agents' \
  --exclude='./.codex' \
  --exclude='./.env*' \
  --exclude='*/.env*' \
  --exclude='./.git' \
  --exclude='*/.git' \
  --exclude='*/.git/*' \
  --exclude='./.mypy_cache' \
  --exclude='./.pytest_cache' \
  --exclude='./.recovery' \
  --exclude='*/.recovery' \
  --exclude='*/.recovery/*' \
  --exclude='./.ruff_cache' \
  --exclude='./.venv' \
  --exclude='./.venv*' \
  --exclude='./data' \
  --exclude='./nvidia-nim-env' \
  --exclude='./reports' \
  --exclude='__pycache__' \
  --create \
  --file=- \
  . | tar --directory="$ISOLATED_CHECKOUT" --extract --file=-

mkdir -p "$ISOLATED_CHECKOUT/reports/tmp"
mkdir -p "$ISOLATED_TEST_ROOT/git-home"

# Some release-gate tests require git-diff semantics. Build a sanitized,
# disposable index from the already-filtered copy instead of exposing the real
# repository metadata, remotes, hooks, or credentials.
env -i \
  PATH=/usr/bin:/bin \
  HOME="$ISOLATED_TEST_ROOT/git-home" \
  GIT_CONFIG_NOSYSTEM=1 \
  git -C "$ISOLATED_CHECKOUT" init --quiet
env -i \
  PATH=/usr/bin:/bin \
  HOME="$ISOLATED_TEST_ROOT/git-home" \
  GIT_CONFIG_NOSYSTEM=1 \
  git -C "$ISOLATED_CHECKOUT" add --all

if [ "$#" -eq 0 ]; then
  set -- discover -s tests -v
fi

printf 'Isolated checkout: %s\n' "$ISOLATED_CHECKOUT"
printf 'Python: %s\n' "$PYTHON_REAL"
printf 'Sandbox: %s\n' "$BWRAP_BIN"

SANDBOX_PYTHON="/runtime/python/bin/$(basename "$PYTHON_REAL")"
SANDBOX_PYTHONPATH="/work:/work/src:/runtime/site-packages"
TEST_COMMAND=("$SANDBOX_PYTHON" -m unittest)
if [ "${ISOLATED_TEST_COVERAGE:-0}" = "1" ]; then
  TEST_COMMAND=("$SANDBOX_PYTHON" -m coverage run --parallel-mode -m unittest)
fi

printf 'Command:'
printf ' %q' "${TEST_COMMAND[@]}"
printf ' %q' "$@"
printf '\n'

BWRAP_ARGS=(
  --die-with-parent
  --new-session
  --unshare-all
  --clearenv
  --cap-drop ALL
  --proc /proc
  --dev /dev
  --tmpfs /tmp
  --dir /tmp/home
  --ro-bind /usr /usr
  --ro-bind "$PYTHON_RUNTIME_ROOT" /runtime/python
  --ro-bind "$PYTHON_SITE_PACKAGES" /runtime/site-packages
  --ro-bind "$ISOLATED_CHECKOUT" /work
  # Two safety-scan tests create short-lived malicious fixtures. Permit writes
  # only in these disposable copies; the real checkout remains unreachable.
  --bind "$ISOLATED_CHECKOUT/configs" /work/configs
  --bind "$ISOLATED_CHECKOUT/src" /work/src
  --tmpfs /work/reports/tmp
  --chdir /work
  --setenv PATH "/runtime/python/bin:/usr/bin:/bin"
  --setenv HOME /tmp/home
  --setenv XDG_CACHE_HOME /tmp/home/.cache
  --setenv XDG_CONFIG_HOME /tmp/home/.config
  --setenv XDG_DATA_HOME /tmp/home/.local/share
  --setenv LANG C.UTF-8
  --setenv LC_ALL C.UTF-8
  --setenv TZ UTC
  --setenv PYTHON_BIN "$SANDBOX_PYTHON"
  --setenv PYTHONDONTWRITEBYTECODE 1
  --setenv PYTHONNOUSERSITE 1
  --setenv PYTHONSAFEPATH 1
  --setenv PYTEST_DISABLE_PLUGIN_AUTOLOAD 1
  --setenv PYTHONPATH "$SANDBOX_PYTHONPATH"
  --setenv COVERAGE_FILE /tmp/.coverage
)

for system_path in /bin /lib /lib64; do
  if [ -e "$system_path" ]; then
    BWRAP_ARGS+=(--ro-bind "$system_path" "$system_path")
  fi
done
if [ -f /etc/ld.so.cache ]; then
  BWRAP_ARGS+=(--ro-bind /etc/ld.so.cache /etc/ld.so.cache)
fi
if [ -d /etc/ssl/certs ]; then
  BWRAP_ARGS+=(--ro-bind /etc/ssl/certs /etc/ssl/certs)
fi
if [ -d /etc/ca-certificates ]; then
  BWRAP_ARGS+=(--ro-bind /etc/ca-certificates /etc/ca-certificates)
fi

PREFLIGHT_CODE='
import os
import socket
from pathlib import Path

for name in os.environ:
    upper = name.upper()
    assert "ALPACA" not in upper
    assert "BROKER" not in upper
    assert "SECRET" not in upper
    assert "TOKEN" not in upper
    assert "PASSWORD" not in upper
    assert not upper.endswith("_KEY")
    assert "PROXY" not in upper

work = Path("/work")
assert not list(work.rglob(".env*"))
assert not (work / ".recovery").exists()
assert not (work / "data").exists()
assert sorted(path.name for path in (work / "reports").iterdir()) == ["tmp"]

git_dir = work / ".git"
assert git_dir.is_dir()
assert not [path for path in work.rglob(".git") if path != git_dir]
git_config = (git_dir / "config").read_text(encoding="utf-8")
assert "[remote " not in git_config
assert "url =" not in git_config

try:
    (work / ".sandbox-write-probe").write_text("forbidden", encoding="utf-8")
except OSError:
    pass
else:
    raise AssertionError("/work must be read-only")

unix_path = "/tmp/trading-ai-preflight.sock"
unix_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
unix_socket.bind(unix_path)
unix_socket.close()

network_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
network_socket.settimeout(0.2)
assert network_socket.connect_ex(("198.51.100.1", 9)) != 0
network_socket.close()
assert {name for _, name in socket.if_nameindex()} <= {"lo"}

print("sandbox preflight: PASS")
'

"$BWRAP_BIN" "${BWRAP_ARGS[@]}" "$SANDBOX_PYTHON" -I -S -c "$PREFLIGHT_CODE"
"$BWRAP_BIN" "${BWRAP_ARGS[@]}" "${TEST_COMMAND[@]}" "$@"
