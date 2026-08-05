from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from trading_ai.execution.paper_executor_client import (
    PaperSafeFlattenOutcomeUnknown,
    PaperSafeFlattenStatus,
)
from trading_ai.execution.paper_executor_ipc import (
    PaperExecutorOutcomeUnknownError,
    PaperExecutorUnavailableError,
)
from trading_ai.execution.paper_safe_flatten import (
    PaperSafeFlattenOperationalError,
    run_paper_safe_flatten,
)


def _status(
    operation_id: str,
    state: str,
    *,
    outcome_unknown: PaperSafeFlattenOutcomeUnknown | None = None,
) -> PaperSafeFlattenStatus:
    return PaperSafeFlattenStatus(
        schema_version=1,
        operation_id=operation_id,
        account_scope_sha256="a" * 64,
        state=state,
        state_version=1,
        terminal=state in {"failed_latched", "flat_latched"},
        reconciled=state == "flat_latched",
        kill_switch_active=True,
        retry_allowed=False,
        failure_code="workflow_failed" if state == "failed_latched" else None,
        outcome_unknown=outcome_unknown,
        started_at="2026-07-15T13:00:00Z",
        updated_at="2026-07-15T13:01:00Z",
    )


class FakeExecutorClient:
    def __init__(
        self,
        *,
        active: PaperSafeFlattenStatus | None = None,
        start_status: PaperSafeFlattenStatus | None = None,
        status_sequence: list[PaperSafeFlattenStatus | Exception] | None = None,
        start_error: Exception | None = None,
        intent_path: Path | None = None,
    ) -> None:
        self.active = active
        self.start_status = start_status
        self.status_sequence = list(status_sequence or [])
        self.start_error = start_error
        self.intent_path = intent_path
        self.start_calls: list[str] = []
        self.status_calls: list[str] = []
        self.active_calls = 0

    def get_active_safe_flatten(self) -> PaperSafeFlattenStatus | None:
        self.active_calls += 1
        return self.active

    def start_safe_flatten(self, operation_id: str) -> object:
        self.start_calls.append(operation_id)
        if self.intent_path is not None:
            persisted = json.loads(self.intent_path.read_text(encoding="utf-8"))
            if persisted["operation_id"] != operation_id or persisted["phase"] != "prepared":
                raise AssertionError("operation identity was not persisted before start")
        if self.start_error is not None:
            raise self.start_error
        if self.start_status is None:
            raise AssertionError("missing fake start status")
        return SimpleNamespace(status=self.start_status)

    def get_safe_flatten_status(self, operation_id: str) -> PaperSafeFlattenStatus:
        self.status_calls.append(operation_id)
        if not self.status_sequence:
            raise AssertionError("unexpected status poll")
        value = self.status_sequence.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


class PaperSafeFlattenTests(unittest.TestCase):
    def _run(self, fake: FakeExecutorClient, root: Path, **kwargs: object):
        with mock.patch(
            "trading_ai.execution.paper_safe_flatten.PaperExecutorBrokerClient",
            return_value=fake,
        ):
            return run_paper_safe_flatten(
                confirm_paper=True,
                confirm_flatten=True,
                output=root / "flatten.json",
                markdown_output=root / "flatten.md",
                poll_attempts=3,
                poll_interval_seconds=0,
                sleep=lambda _seconds: None,
                **kwargs,
            )

    def test_requires_both_confirmations(self) -> None:
        with self.assertRaises(PaperSafeFlattenOperationalError):
            run_paper_safe_flatten(confirm_paper=True, confirm_flatten=False)

    def test_reset_is_rejected_before_client_construction(self) -> None:
        constructor = mock.Mock()
        with (
            mock.patch(
                "trading_ai.execution.paper_safe_flatten.PaperExecutorBrokerClient",
                constructor,
            ),
            self.assertRaisesRegex(PaperSafeFlattenOperationalError, "forbidden"),
        ):
            run_paper_safe_flatten(
                confirm_paper=True,
                confirm_flatten=True,
                reset_kill_switch_after=True,
            )
        constructor.assert_not_called()

    def test_persists_identity_before_one_start_then_only_observes(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            operation_id = "5" * 32
            fake = FakeExecutorClient(
                start_status=_status(operation_id, "latched"),
                status_sequence=[
                    _status(operation_id, "closing"),
                    _status(operation_id, "flat_latched"),
                ],
                intent_path=root / "operation.json",
            )
            with mock.patch("uuid.uuid4", return_value=SimpleNamespace(hex=operation_id)):
                result = self._run(fake, root)

            self.assertEqual(result.status, "OK")
            self.assertEqual(result.exit_code, 0)
            self.assertEqual(fake.start_calls, [operation_id])
            self.assertEqual(fake.status_calls, [operation_id, operation_id])
            self.assertTrue(result.payload["reconciled"])
            self.assertTrue(result.payload["kill_switch_active_after"])
            self.assertFalse(result.payload["retry_allowed"])
            self.assertTrue((root / "flatten.json").is_file())
            self.assertTrue((root / "flatten.md").is_file())

    def test_active_operation_is_adopted_without_second_start(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            operation_id = "5" * 32
            fake = FakeExecutorClient(
                active=_status(operation_id, "canceling"),
                status_sequence=[_status(operation_id, "flat_latched")],
            )

            result = self._run(fake, root)

            self.assertEqual(result.status, "OK")
            self.assertEqual(fake.start_calls, [])
            self.assertEqual(fake.status_calls, [operation_id])

    def test_start_outcome_unknown_is_never_retried_and_status_can_resolve(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            operation_id = "5" * 32
            fake = FakeExecutorClient(
                start_error=PaperExecutorOutcomeUnknownError(
                    "response unavailable",
                    request_id=operation_id,
                    operation="start_safe_flatten",
                    phase="response_wait",
                ),
                status_sequence=[_status(operation_id, "flat_latched")],
                intent_path=root / "operation.json",
            )
            with mock.patch("uuid.uuid4", return_value=SimpleNamespace(hex=operation_id)):
                result = self._run(fake, root)

            self.assertEqual(result.status, "OK")
            self.assertEqual(fake.start_calls, [operation_id])
            self.assertEqual(fake.status_calls, [operation_id])
            self.assertFalse(result.payload["outcome_unknown"]["retry_allowed"])

    def test_unresolved_status_is_error_and_never_reports_flat(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            operation_id = "5" * 32
            fake = FakeExecutorClient(
                start_status=_status(operation_id, "latched"),
                status_sequence=[
                    _status(operation_id, "canceling"),
                    PaperExecutorUnavailableError("executor unavailable"),
                ],
            )
            with mock.patch("uuid.uuid4", return_value=SimpleNamespace(hex=operation_id)):
                result = self._run(fake, root)

            self.assertEqual(result.status, "ERROR")
            self.assertEqual(result.exit_code, 2)
            self.assertFalse(result.payload["terminal"])
            self.assertFalse(result.payload["reconciled"])
            self.assertEqual(fake.start_calls, [operation_id])

    def test_artifact_failure_does_not_trigger_another_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            operation_id = "5" * 32
            fake = FakeExecutorClient(
                start_status=_status(operation_id, "flat_latched"),
            )
            with (
                mock.patch("uuid.uuid4", return_value=SimpleNamespace(hex=operation_id)),
                mock.patch(
                    "trading_ai.execution.paper_safe_flatten._write_result",
                    side_effect=OSError("disk full"),
                ),
            ):
                result = self._run(fake, root)

            self.assertEqual(result.status, "ERROR")
            self.assertEqual(result.payload["failure_stage"], "artifact_persistence")
            self.assertEqual(fake.start_calls, [operation_id])
            self.assertEqual(fake.status_calls, [])

    def test_malformed_persisted_identity_fails_closed_before_start(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "operation.json").write_text(
                '{"schema_version":1,"operation_id":"bad","phase":"prepared","updated_at":"x"}',
                encoding="utf-8",
            )
            fake = FakeExecutorClient()

            result = self._run(fake, root)

            self.assertEqual(result.status, "ERROR")
            self.assertEqual(result.payload["failure_stage"], "operation_intent")
            self.assertEqual(fake.start_calls, [])


if __name__ == "__main__":
    unittest.main()
