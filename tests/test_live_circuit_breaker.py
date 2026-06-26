import tempfile
import unittest
from pathlib import Path

from trading_ai.execution.live_circuit_breaker import (
    LiveCircuitBreakerError,
    LiveCircuitBreakerState,
    load_live_circuit_breaker,
    reset_live_circuit_breaker,
    save_live_circuit_breaker,
)


class LiveCircuitBreakerTests(unittest.TestCase):
    def test_missing_state_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = load_live_circuit_breaker(Path(tmp) / "missing.json")

        self.assertTrue(state.tripped)
        self.assertEqual(state.reason, "breaker_missing_fail_closed")

    def test_corrupt_state_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "breaker.json"
            path.write_text("{bad json", encoding="utf-8")

            state = load_live_circuit_breaker(path)

        self.assertTrue(state.tripped)
        self.assertEqual(state.reason, "breaker_corrupt_fail_closed")

    def test_checksum_mismatch_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "breaker.json"
            save_live_circuit_breaker(LiveCircuitBreakerState(tripped=False, reason=None), path)
            payload = path.read_text(encoding="utf-8").replace('"tripped": false', '"tripped": true')
            path.write_text(payload, encoding="utf-8")

            state = load_live_circuit_breaker(path)

        self.assertTrue(state.tripped)
        self.assertEqual(state.reason, "breaker_checksum_mismatch_fail_closed")

    def test_reset_requires_reviewer_reason_and_valid_checksum(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "breaker.json"
            save_live_circuit_breaker(LiveCircuitBreakerState(tripped=True, reason="manual_trip"), path)

            with self.assertRaises(LiveCircuitBreakerError):
                reset_live_circuit_breaker(path, reviewer="", reason="ops reviewed")
            with self.assertRaises(LiveCircuitBreakerError):
                reset_live_circuit_breaker(path, reviewer="ops", reason="")

            state = reset_live_circuit_breaker(path, reviewer="ops", reason="rollback prevalidated")
            reloaded = load_live_circuit_breaker(path)

        self.assertFalse(state.tripped)
        self.assertFalse(reloaded.tripped)
        self.assertEqual(reloaded.reviewer, "ops")
        self.assertEqual(reloaded.reset_reason, "rollback prevalidated")


if __name__ == "__main__":
    unittest.main()
