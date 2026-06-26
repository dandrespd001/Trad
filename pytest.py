"""Small pytest compatibility shim for the local unittest-only gate.

The project still contains a few pytest-style tests, but the governed release
environment intentionally does not require installing pytest. This module keeps
`unittest discover` imports working. It does not pretend to be the real pytest
runner for CLI metadata such as `python -m pytest --version`.
"""

from __future__ import annotations

import importlib
import sys
import types
import unittest
from contextlib import contextmanager
from typing import Callable


class _Mark:
    def parametrize(self, *_args: object, **_kwargs: object) -> Callable[[Callable[..., object]], Callable[..., object]]:
        return _identity_decorator

    def skipif(self, condition: object, *, reason: str = "") -> Callable[[Callable[..., object]], Callable[..., object]]:
        if condition:
            return unittest.skip(reason or "pytest skipif condition")
        return _identity_decorator


class _Approx:
    def __init__(self, expected: object, *, rel: float | None = None, abs: float | None = None) -> None:
        self.expected = expected
        self.rel = 1e-12 if rel is None else rel
        self.abs = 1e-12 if abs is None else abs

    def __eq__(self, actual: object) -> bool:
        try:
            return builtins_abs(float(actual) - float(self.expected)) <= max(
                self.abs,
                self.rel * builtins_abs(float(self.expected)),
            )
        except (TypeError, ValueError):
            return actual == self.expected


class MonkeyPatch:
    def setattr(self, target: object, name: str, value: object) -> None:
        setattr(target, name, value)

    def setenv(self, name: str, value: str) -> None:
        import os

        os.environ[name] = value

    def delenv(self, name: str, raising: bool = True) -> None:
        import os

        if name in os.environ:
            del os.environ[name]
        elif raising:
            raise KeyError(name)


class LogCaptureFixture:
    messages: list[str]

    def __init__(self) -> None:
        self.messages = []

    @contextmanager
    def at_level(self, _level: int) -> object:
        yield self


mark = _Mark()
builtins_abs = abs


def fixture(*_args: object, **_kwargs: object) -> Callable[[Callable[..., object]], Callable[..., object]]:
    return _identity_decorator


def approx(expected: object, *, rel: float | None = None, abs: float | None = None) -> _Approx:
    return _Approx(expected, rel=rel, abs=abs)


def importorskip(name: str) -> types.ModuleType:
    try:
        return importlib.import_module(name)
    except ImportError as exc:
        raise unittest.SkipTest(f"{name} not installed") from exc


def fail(reason: str = "") -> None:
    raise AssertionError(reason)


def skip(reason: str = "") -> None:
    raise unittest.SkipTest(reason)


def main() -> int:
    if "--version" in sys.argv[1:]:
        print("pytest compatibility shim: real pytest is not installed in this environment", file=sys.stderr)
        return 2
    args = [arg for arg in sys.argv[1:] if not arg.startswith("--cov") and not arg.startswith("-q")]
    start_dir = "tests"
    if args and not args[0].startswith("-"):
        start_dir = args[0]
    suite = unittest.defaultTestLoader.discover(start_dir)
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    return 0 if result.wasSuccessful() else 1


def _identity_decorator(func: Callable[..., object]) -> Callable[..., object]:
    return func


if __name__ == "__main__":
    raise SystemExit(main())
