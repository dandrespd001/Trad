"""Adversarial tests for paper-auto session ledger classification."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from trading_ai.execution.paper_auto_sessions import (
    classify_paper_auto_session,
    summarize_paper_auto_sessions,
)


def _record(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": "1.0",
        "record_type": "paper_auto_cycle_session",
        "session_id": "paper-auto-2026-06-16-clean",
        "generated_at": "2026-06-16T12:00:00+00:00",
        "as_of_date": "2026-06-16",
        "state": "PAPER_CLOSED",
        "exit_code": 0,
        "confirm_paper_auto": True,
        "order_state": "paper_order_sent",
        "closeout_status": "CLOSED",
        "statement_status": "MATCHED",
        "unreconciled_fills": 0,
        "blockers": [],
        "safety": {
            "paper_only": True,
            "live_trading_authorized": False,
        },
    }
    payload.update(overrides)
    return payload


class PaperAutoSessionsTests(unittest.TestCase):
    def test_only_closed_matched_confirmed_session_is_clean(self) -> None:
        self.assertEqual(classify_paper_auto_session(_record()), ("CLEAN", []))

        invalid_cases = (
            ({"closeout_status": "partially_filled"}, "BLOCKED"),
            ({"statement_status": "GARBAGE"}, "FILL_UNRECONCILED"),
            ({"confirm_paper_auto": "true"}, "BLOCKED"),
            ({"exit_code": 1}, "BLOCKED"),
            ({"order_state": "accepted"}, "BLOCKED"),
            ({"unreconciled_fills": "0"}, "BLOCKED"),
        )
        for overrides, expected in invalid_cases:
            with self.subTest(overrides=overrides):
                classification, reasons = classify_paper_auto_session(_record(**overrides))
                self.assertEqual(classification, expected)
                self.assertTrue(reasons)

    def test_exact_replay_is_not_counted_twice_and_blocks_campaign(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            ledger = Path(temporary) / "sessions.jsonl"
            line = json.dumps(_record(), sort_keys=True)
            ledger.write_text(f"{line}\n{line}\n", encoding="utf-8")

            summary = summarize_paper_auto_sessions([ledger], min_clean_sessions=1)

        self.assertEqual(summary["total_sessions"], 1)
        self.assertEqual(summary["clean_sessions"], 1)
        self.assertEqual(summary["state"], "BLOCKED")
        self.assertEqual(
            summary["blocker_histogram"]["session_ledger_duplicate_session_id"],
            1,
        )

    def test_invalid_identity_timestamp_safety_and_nonfinite_are_diagnostics(self) -> None:
        invalid = _record(
            session_id="",
            generated_at="2026-06-17T12:00:00",
            unreconciled_fills=float("nan"),
            safety={"paper_only": "true", "live_trading_authorized": "false"},
        )
        with tempfile.TemporaryDirectory() as temporary:
            ledger = Path(temporary) / "sessions.jsonl"
            ledger.write_text(json.dumps(invalid) + "\n", encoding="utf-8")

            summary = summarize_paper_auto_sessions([ledger], min_clean_sessions=1)

        self.assertEqual(summary["total_sessions"], 0)
        self.assertEqual(summary["clean_sessions"], 0)
        self.assertEqual(summary["state"], "BLOCKED")
        histogram = summary["blocker_histogram"]
        self.assertIn("session_record_identity_invalid", histogram)
        self.assertIn("session_record_timestamp_invalid", histogram)
        self.assertIn("session_record_safety_invalid", histogram)
        self.assertIn("session_record_nonfinite", histogram)

    def test_invalid_clean_session_threshold_cannot_report_ready(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            ledger = Path(temporary) / "sessions.jsonl"
            ledger.write_text(json.dumps(_record()) + "\n", encoding="utf-8")

            summary = summarize_paper_auto_sessions([ledger], min_clean_sessions=0)

        self.assertEqual(summary["state"], "BLOCKED")
        self.assertEqual(summary["target_clean_sessions"], 0)
        self.assertIn("min_clean_sessions_invalid", summary["blocker_histogram"])


if __name__ == "__main__":
    unittest.main()
