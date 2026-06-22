import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from trading_ai.cli import build_parser, main
from trading_ai.execution.paper_daily import PaperDailyFromReadinessResult


class PaperBotCycleTests(unittest.TestCase):
    def test_parser_defaults_for_paper_bot_cycle(self) -> None:
        args = build_parser().parse_args(
            [
                "paper-bot-cycle",
                "--as-of-date",
                "2026-06-16",
                "--readiness",
                "readiness.json",
                "--human-review",
                "review.json",
            ]
        )

        self.assertEqual(args.as_of_date, "2026-06-16")
        self.assertEqual(args.readiness, "readiness.json")
        self.assertEqual(args.human_review, "review.json")
        self.assertIsNone(args.llm_review)
        self.assertIsNone(args.ops_check)
        self.assertIsNone(args.evidence_index)
        self.assertFalse(args.confirm_readiness)
        self.assertFalse(args.confirm_paper)
        self.assertFalse(args.confirm_auto_submit)
        self.assertFalse(args.confirm_auto_close)
        self.assertFalse(args.require_clean_state)
        self.assertEqual(args.output_dir, "reports/tmp/paper_bot_cycle")

    def test_cycle_without_confirmations_stops_before_broker_call_when_eligible(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            readiness = write_readiness(root, ready=True)
            ops = write_ops(root, status="OK")
            evidence = write_evidence(root, status="OK")
            review = write_review(root, decision="APPROVE_PAPER_CONFIRMATION")

            with mock.patch(
                "trading_ai.execution.paper_bot_cycle.run_paper_daily_from_readiness",
                side_effect=AssertionError("broker paper daily must not run without confirmations"),
            ):
                exit_code = main(cycle_args(root, readiness=readiness, ops=ops, evidence=evidence, review=review))
            payload = read_json(root / "cycle" / "2026-06-16" / "cycle.json")
            markdown = (root / "cycle" / "2026-06-16" / "cycle.md").read_text(encoding="utf-8")

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["state"], "ELIGIBLE_FOR_PAPER")
        self.assertEqual(payload["autopilot"]["action"], "ELIGIBLE_FOR_PAPER_CONFIRMED")
        self.assertFalse(payload["confirmations"]["confirm_paper"])
        self.assertFalse(payload["safety"]["broker_client_built"])
        self.assertIn("State: **ELIGIBLE_FOR_PAPER**", markdown)

    def test_cycle_with_broker_confirmations_requires_clean_state_before_broker_call(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            readiness = write_readiness(root, ready=True)
            ops = write_ops(root, status="OK")
            evidence = write_evidence(root, status="OK")
            review = write_review(root, decision="APPROVE_PAPER_CONFIRMATION")

            with mock.patch(
                "trading_ai.execution.paper_bot_cycle.run_paper_daily_from_readiness",
                side_effect=AssertionError("broker paper daily must not run without clean-state confirmation"),
            ):
                exit_code = main(
                    cycle_args(root, readiness=readiness, ops=ops, evidence=evidence, review=review)
                    + [
                        "--confirm-readiness",
                        "--confirm-paper",
                        "--confirm-auto-submit",
                        "--confirm-auto-close",
                    ]
                )
            payload = read_json(root / "cycle" / "2026-06-16" / "cycle.json")

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["state"], "ELIGIBLE_FOR_PAPER")
        self.assertFalse(payload["confirmations"]["require_clean_state"])
        self.assertFalse(payload["safety"]["broker_client_built"])

    def test_cycle_with_all_confirmations_calls_paper_daily_from_readiness(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            readiness = write_readiness(root, ready=True)
            ops = write_ops(root, status="OK")
            evidence = write_evidence(root, status="OK")
            review = write_review(root, decision="APPROVE_PAPER_CONFIRMATION")
            seen: dict[str, object] = {}

            def fake_daily(**kwargs: object) -> PaperDailyFromReadinessResult:
                seen.update(kwargs)
                output_path = root / "broker" / "broker_run.json"
                markdown_path = root / "broker" / "broker_run.md"
                payload = {
                    "status": "OK",
                    "exit_code": 0,
                    "paper_daily": {"status": "OK", "exit_code": 0},
                    "broker_confirmed_paths": {"daily_json": str(root / "broker" / "daily.json")},
                    "reasons": [],
                    "safety": {
                        "paper_only": True,
                        "broker_client_built": True,
                        "credentials_read": True,
                        "orders_submitted": True,
                        "live_trading_allowed": False,
                        "live_trading_authorized": False,
                    },
                }
                write_json(output_path, payload)
                markdown_path.write_text("# broker\n", encoding="utf-8")
                return PaperDailyFromReadinessResult(
                    exit_code=0,
                    status="OK",
                    output_path=output_path,
                    markdown_path=markdown_path,
                    payload=payload,
                )

            with mock.patch("trading_ai.execution.paper_bot_cycle.run_paper_daily_from_readiness", side_effect=fake_daily):
                exit_code = main(
                    cycle_args(root, readiness=readiness, ops=ops, evidence=evidence, review=review)
                    + [
                        "--confirm-readiness",
                        "--confirm-paper",
                        "--confirm-auto-submit",
                        "--confirm-auto-close",
                        "--require-clean-state",
                    ]
                )
            payload = read_json(root / "cycle" / "2026-06-16" / "cycle.json")

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["state"], "PAPER_CLOSED")
        self.assertEqual(Path(str(seen["readiness_path"])), readiness)
        self.assertTrue(seen["confirm_readiness"])
        self.assertTrue(seen["confirm_paper"])
        self.assertTrue(seen["confirm_auto_submit"])
        self.assertTrue(seen["confirm_auto_close"])
        self.assertTrue(seen["require_clean_state"])
        self.assertEqual(payload["artifacts"]["paper_daily_from_readiness"]["status"], "OK")
        self.assertTrue(payload["authority"]["orders_submitted_by_cycle"])
        self.assertTrue(payload["safety"]["broker_client_built"])
        self.assertTrue(payload["safety"]["credentials_read"])
        self.assertTrue(payload["safety"]["orders_submitted"])
        self.assertTrue(payload["safety"]["observed_child_safety"]["orders_submitted"])

    def test_cycle_reject_review_blocks_before_broker_call(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            readiness = write_readiness(root, ready=True)
            ops = write_ops(root, status="OK")
            evidence = write_evidence(root, status="OK")
            review = write_review(root, decision="REJECT")

            with mock.patch(
                "trading_ai.execution.paper_bot_cycle.run_paper_daily_from_readiness",
                side_effect=AssertionError("broker paper daily must not run after reject review"),
            ):
                exit_code = main(
                    cycle_args(root, readiness=readiness, ops=ops, evidence=evidence, review=review)
                    + [
                        "--confirm-readiness",
                        "--confirm-paper",
                        "--confirm-auto-submit",
                        "--confirm-auto-close",
                    ]
                )
            payload = read_json(root / "cycle" / "2026-06-16" / "cycle.json")

        self.assertEqual(exit_code, 1)
        self.assertEqual(payload["state"], "BLOCKED")
        self.assertEqual(payload["autopilot"]["action"], "BLOCKED")
        self.assertIn("human_review_rejected", reason_codes(payload))


def cycle_args(
    root: Path,
    *,
    readiness: Path,
    ops: Path,
    evidence: Path,
    review: Path,
) -> list[str]:
    return [
        "paper-bot-cycle",
        "--as-of-date",
        "2026-06-16",
        "--readiness",
        str(readiness),
        "--ops-check",
        str(ops),
        "--evidence-index",
        str(evidence),
        "--human-review",
        str(review),
        "--output-dir",
        str(root / "cycle"),
    ]


def write_readiness(root: Path, *, ready: bool) -> Path:
    return write_json(
        root / "readiness.json",
        {
            "status": "READY" if ready else "BLOCKED",
            "ready_for_paper_daily": ready,
            "as_of_date": "2026-06-16",
            "safety": {"credentials_read": False, "live_trading_allowed": False},
        },
    )


def write_ops(root: Path, *, status: str) -> Path:
    return write_json(
        root / "ops_check.json",
        {
            "status": status,
            "as_of_date": "2026-06-16",
            "issues": [],
            "safety": {
                "broker_client_built": False,
                "credentials_read": False,
                "orders_submitted": False,
                "live_trading_allowed": False,
            },
        },
    )


def write_evidence(root: Path, *, status: str) -> Path:
    return write_json(
        root / "evidence_index.json",
        {
            "status": status,
            "as_of_date": "2026-06-16",
            "issues": [],
            "safety": {
                "broker_client_built": False,
                "credentials_read": False,
                "orders_submitted": False,
                "live_trading_allowed": False,
            },
        },
    )


def write_review(root: Path, *, decision: str) -> Path:
    return write_json(
        root / "review.json",
        {
            "status": "RECORDED",
            "decision": decision,
            "as_of_date": "2026-06-16",
            "safety": {"live_trading_authorized": False, "live_trading_allowed": False},
        },
    )


def write_json(path: Path, payload: dict[str, object]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return path


def read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def reason_codes(payload: dict[str, object]) -> set[str]:
    return {str(reason["code"]) for reason in payload["reasons"]}


if __name__ == "__main__":
    unittest.main()
