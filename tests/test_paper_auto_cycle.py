import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from trading_ai.cli import build_parser, main
from trading_ai.evaluation.paper_daily_prepare import PaperDailyPrepareResult
from trading_ai.execution.llm_signal_proposals import LLMSignalProposalsResult
from trading_ai.execution.paper_auto_cycle import PaperAutoCycleResult
from trading_ai.execution.paper_bot_cycle import PaperBotCycleResult
from trading_ai.execution.paper_review_decision import PaperReviewDecisionResult
from trading_ai.execution.paper_signal_arbitration import PaperSignalArbitrationResult


class PaperAutoCycleTests(unittest.TestCase):
    def test_parser_defaults_for_paper_auto_cycle(self) -> None:
        args = build_parser().parse_args(
            [
                "paper-auto-cycle",
                "--as-of-date",
                "2026-06-16",
                "--source",
                "fresh.csv",
                "--dataset-id",
                "core_etfs",
                "--frequency",
                "1d",
                "--from",
                "2026-03-01",
                "--to",
                "2026-06-16",
            ]
        )

        self.assertEqual(args.as_of_date, "2026-06-16")
        self.assertEqual(args.source, "fresh.csv")
        self.assertIsNone(args.approved_dir)
        self.assertEqual(args.dataset_id, "core_etfs")
        self.assertEqual(args.frequency, "1d")
        self.assertFalse(args.confirm_paper_auto)
        self.assertEqual(args.output_dir, "reports/tmp/paper_auto_cycle")
        self.assertFalse(args.require_clean_state)
        self.assertIsNone(args.operator_status)
        self.assertIsNone(args.campaign_report)
        self.assertIsNone(args.session_ledger)

    def test_auto_cycle_without_confirmation_stops_after_arbitration_and_never_calls_broker(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            readiness = root / "readiness.json"
            proposals = root / "llm_signal_proposals.json"
            signal_plan = root / "signal_plan.json"
            write_json(readiness, readiness_payload(status="READY", ready=True))
            write_json(proposals, {"status": "OK", "proposals": []})
            write_json(signal_plan, {"decision": "ELIGIBLE_FOR_PAPER", "eligible_for_paper": True})

            with patched_cycle_steps(root, readiness=readiness, proposals=proposals, signal_plan=signal_plan) as calls:
                exit_code = main(auto_args(root))
            payload = read_json(root / "cycle" / "2026-06-16" / "cycle.json")
            daily_status = read_json(root / "cycle" / "2026-06-16" / "daily_status.json")

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["state"], "EVIDENCE_ONLY")
        self.assertFalse(payload["confirmations"]["confirm_paper_auto"])
        self.assertIn("daily_status", payload["artifacts"])
        self.assertEqual(daily_status["state"], "EVIDENCE_ONLY")
        self.assertEqual(daily_status["next_safe_action"], "review_artifacts")
        self.assertIn("confirm_paper_auto_missing", daily_status["reason_codes"])
        self.assertEqual(calls["review"].call_count, 0)
        self.assertEqual(calls["bot"].call_count, 0)
        self.assertFalse(payload["safety"]["broker_client_built"])
        self.assertFalse(payload["safety"]["credentials_read"])

    def test_auto_cycle_writes_local_llm_context_digest_before_proposals(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            readiness = root / "readiness.json"
            proposals = root / "llm_signal_proposals.json"
            signal_plan = root / "signal_plan.json"
            write_json(readiness, readiness_payload(status="READY", ready=True))
            write_json(proposals, {"status": "OK", "proposals": []})
            write_json(signal_plan, {"decision": "ELIGIBLE_FOR_PAPER", "eligible_for_paper": True})

            with patched_cycle_steps(root, readiness=readiness, proposals=proposals, signal_plan=signal_plan) as calls:
                exit_code = main(auto_args(root))
            payload = read_json(root / "cycle" / "2026-06-16" / "cycle.json")
            digest = read_json(root / "cycle" / "2026-06-16" / "llm_context" / "context_digest.json")
            proposal_kwargs = calls["proposals"].call_args.kwargs

        self.assertEqual(exit_code, 0)
        self.assertIn("llm_context_digest", payload["artifacts"])
        self.assertEqual(digest["status"], "OK")
        self.assertEqual(digest["authority"]["llm_authority"], "none")
        self.assertFalse(digest["safety"]["broker_client_built"])
        self.assertEqual(Path(str(proposal_kwargs["context_digest"])), Path(payload["artifacts"]["llm_context_digest"]))

    def test_auto_cycle_blocks_on_external_monitor_or_performance_critical_before_review(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            readiness = root / "readiness.json"
            proposals = root / "llm_signal_proposals.json"
            signal_plan = root / "signal_plan.json"
            monitor = write_json(
                root / "monitor.json",
                {
                    "status": "CRITICAL",
                    "alerts": [{"severity": "CRITICAL", "code": "paper_execution_without_closeout"}],
                    "broker_snapshot": {"status": "OK", "counts": {"orders": 1, "positions": 0}},
                    "safety": {"credentials_read": False, "live_trading_allowed": False},
                },
            )
            performance = write_json(
                root / "performance.json",
                {
                    "status": "WARN",
                    "blockers": ["closeout_pending"],
                    "paper_metrics": {"pending_closeouts": 1, "unmatched_closeouts": 0},
                    "statement_reconciliation": {"status": "MATCHED", "missing_fills": 0},
                    "safety": {"credentials_read": False, "live_trading_allowed": False},
                },
            )
            write_json(readiness, readiness_payload(status="READY", ready=True))
            write_json(proposals, {"status": "OK", "proposals": []})
            write_json(signal_plan, {"decision": "ELIGIBLE_FOR_PAPER", "eligible_for_paper": True})

            operator_status = write_json(root / "operator_status.json", clean_operator_status())

            with patched_cycle_steps(root, readiness=readiness, proposals=proposals, signal_plan=signal_plan) as calls:
                exit_code = main(
                    auto_args(root)
                    + [
                        "--monitor",
                        str(monitor),
                        "--performance",
                        str(performance),
                        "--confirm-paper-auto",
                        "--require-clean-state",
                        "--operator-status",
                        str(operator_status),
                    ]
                )
            payload = read_json(root / "cycle" / "2026-06-16" / "cycle.json")

        self.assertEqual(exit_code, 1)
        self.assertEqual(payload["state"], "BLOCKED")
        self.assertIn("monitor_critical", payload["reasons"])
        self.assertIn("open_broker_orders", payload["reasons"])
        self.assertIn("closeout_pending", payload["reasons"])
        self.assertEqual(calls["review"].call_count, 0)
        self.assertEqual(calls["bot"].call_count, 0)

    def test_auto_cycle_blocks_stale_prepare_before_llm_or_broker(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            readiness = root / "readiness.json"
            write_json(readiness, readiness_payload(status="BLOCKED", ready=False, reasons=["dataset_stale"]))

            prepare_result = PaperDailyPrepareResult(
                exit_code=1,
                status="BLOCKED",
                ready_for_paper_daily=False,
                output_dir=root / "prepare",
                readiness_path=readiness,
                readiness_markdown_path=root / "readiness.md",
                paper_daily_config_path=None,
                payload=read_json(readiness),
            )
            with mock.patch("trading_ai.execution.paper_auto_cycle.prepare_paper_daily", return_value=prepare_result), \
                mock.patch("trading_ai.execution.paper_auto_cycle.run_llm_signal_proposals") as proposals_mock, \
                mock.patch("trading_ai.execution.paper_auto_cycle.run_paper_bot_cycle") as bot_mock:
                exit_code = main(
                    auto_args(root)
                    + [
                        "--confirm-paper-auto",
                        "--require-clean-state",
                        "--operator-status",
                        str(write_json(root / "operator_status.json", clean_operator_status())),
                    ]
                )
            payload = read_json(root / "cycle" / "2026-06-16" / "cycle.json")

        self.assertEqual(exit_code, 1)
        self.assertEqual(payload["state"], "BLOCKED")
        self.assertIn("dataset_stale", payload["reasons"])
        self.assertEqual(proposals_mock.call_count, 0)
        self.assertEqual(bot_mock.call_count, 0)

    def test_auto_cycle_blocks_missing_prepare_signal_artifacts_before_llm(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            readiness = root / "readiness.json"
            write_json(readiness, readiness_payload(status="READY", ready=True))

            prepare_result = PaperDailyPrepareResult(
                exit_code=0,
                status="READY",
                ready_for_paper_daily=True,
                output_dir=root / "prepare",
                readiness_path=readiness,
                readiness_markdown_path=root / "readiness.md",
                paper_daily_config_path=None,
                payload=read_json(readiness),
            )
            with mock.patch("trading_ai.execution.paper_auto_cycle.prepare_paper_daily", return_value=prepare_result), \
                mock.patch("trading_ai.execution.paper_auto_cycle.run_llm_signal_proposals") as proposals_mock, \
                mock.patch("trading_ai.execution.paper_auto_cycle.run_paper_signal_arbitration") as arbitration_mock, \
                mock.patch("trading_ai.execution.paper_auto_cycle.run_paper_bot_cycle") as bot_mock:
                exit_code = main(auto_args(root))
            payload = read_json(root / "cycle" / "2026-06-16" / "cycle.json")

        self.assertEqual(exit_code, 1)
        self.assertEqual(payload["state"], "BLOCKED")
        self.assertIn("missing_session_json", payload["reasons"])
        self.assertEqual(proposals_mock.call_count, 0)
        self.assertEqual(arbitration_mock.call_count, 0)
        self.assertEqual(bot_mock.call_count, 0)

    def test_auto_cycle_blocks_when_cron_lock_is_active_before_prepare(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            lock_dir = root / "locks"
            lock_dir.mkdir()
            (lock_dir / "paper_auto_cycle_2026-06-16.lock").write_text("active", encoding="utf-8")

            with mock.patch("trading_ai.execution.paper_auto_cycle.prepare_paper_daily") as prepare_mock, \
                mock.patch("trading_ai.execution.paper_auto_cycle.run_paper_bot_cycle") as bot_mock:
                exit_code = main(auto_args(root) + ["--lock-dir", str(lock_dir), "--confirm-paper-auto"])
            payload = read_json(root / "cycle" / "2026-06-16" / "cycle.json")
            daily_status = read_json(root / "cycle" / "2026-06-16" / "daily_status.json")

        self.assertEqual(exit_code, 1)
        self.assertEqual(payload["state"], "BLOCKED")
        self.assertEqual(daily_status["state"], "BLOCKED")
        self.assertIn("cycle_lock_active", payload["reasons"])
        self.assertEqual(prepare_mock.call_count, 0)
        self.assertEqual(bot_mock.call_count, 0)

    def test_auto_cycle_removes_stale_cron_lock_and_runs_prepare(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            readiness = root / "readiness.json"
            proposals = root / "llm_signal_proposals.json"
            signal_plan = root / "signal_plan.json"
            write_json(readiness, readiness_payload(status="READY", ready=True))
            write_json(proposals, {"status": "OK", "proposals": []})
            write_json(signal_plan, {"decision": "ELIGIBLE_FOR_PAPER", "eligible_for_paper": True})
            lock_dir = root / "locks"
            lock_dir.mkdir()
            lock_path = lock_dir / "paper_auto_cycle_2026-06-16.lock"
            lock_path.write_text("generated_at=2026-06-16T10:00:00+00:00\n", encoding="utf-8")
            timestamp = time.time() - 7200
            os.utime(lock_path, (timestamp, timestamp))

            with patched_cycle_steps(root, readiness=readiness, proposals=proposals, signal_plan=signal_plan) as calls:
                exit_code = main(auto_args(root) + ["--lock-dir", str(lock_dir)])
            payload = read_json(root / "cycle" / "2026-06-16" / "cycle.json")

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["state"], "EVIDENCE_ONLY")
        self.assertFalse(lock_path.exists())
        self.assertEqual(calls["prepare"].call_count, 1)

    def test_auto_cycle_rejects_unsafe_as_of_date_before_prepare(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            args = auto_args(root)
            args[args.index("--as-of-date") + 1] = "../2026-06-16"

            with mock.patch("trading_ai.execution.paper_auto_cycle.prepare_paper_daily") as prepare_mock:
                exit_code = main(args)

        self.assertEqual(exit_code, 2)
        self.assertEqual(prepare_mock.call_count, 0)
        self.assertFalse((root / "2026-06-16").exists())

    def test_confirmed_auto_cycle_rejects_relative_today_dates_before_prepare(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            args = auto_args(root)
            args[args.index("--as-of-date") + 1] = "today"

            with mock.patch("trading_ai.execution.paper_auto_cycle.prepare_paper_daily") as prepare_mock, \
                mock.patch("trading_ai.execution.paper_auto_cycle.run_paper_bot_cycle") as bot_mock:
                exit_code = main(
                    args
                    + [
                        "--confirm-paper-auto",
                        "--require-clean-state",
                        "--operator-status",
                        str(write_json(root / "operator_status.json", clean_operator_status())),
                    ]
                )
            payload = read_json(root / "cycle" / "today" / "cycle.json")

        self.assertEqual(exit_code, 2)
        self.assertEqual(payload["state"], "ERROR")
        self.assertIn("confirmed_auto_as_of_date_must_be_explicit", payload["reasons"])
        self.assertEqual(prepare_mock.call_count, 0)
        self.assertEqual(bot_mock.call_count, 0)

    def test_auto_cycle_writes_append_only_session_ledger_for_blocked_cycle(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            readiness = root / "readiness.json"
            ledger = root / "session_ledger.jsonl"
            write_json(readiness, readiness_payload(status="BLOCKED", ready=False, reasons=["dataset_stale"]))

            prepare_result = PaperDailyPrepareResult(
                exit_code=1,
                status="BLOCKED",
                ready_for_paper_daily=False,
                output_dir=root / "prepare",
                readiness_path=readiness,
                readiness_markdown_path=root / "readiness.md",
                paper_daily_config_path=None,
                payload=read_json(readiness),
            )
            with mock.patch("trading_ai.execution.paper_auto_cycle.prepare_paper_daily", return_value=prepare_result):
                exit_code = main(
                    auto_args(root)
                    + [
                        "--session-ledger",
                        str(ledger),
                        "--confirm-paper-auto",
                        "--require-clean-state",
                        "--operator-status",
                        str(write_json(root / "operator_status.json", clean_operator_status())),
                    ]
                )
            records = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines()]

        self.assertEqual(exit_code, 1)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["record_type"], "paper_auto_cycle_session")
        self.assertEqual(records[0]["state"], "BLOCKED")
        self.assertEqual(records[0]["as_of_date"], "2026-06-16")
        self.assertIn("dataset_stale", records[0]["blockers"])
        self.assertTrue(records[0]["safety"]["paper_only"])
        self.assertFalse(records[0]["safety"]["live_trading_authorized"])

    def test_confirmed_auto_cycle_requires_require_clean_state_before_prepare(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            ledger = root / "session_ledger.jsonl"

            with mock.patch("trading_ai.execution.paper_auto_cycle.prepare_paper_daily") as prepare_mock, \
                mock.patch("trading_ai.execution.paper_auto_cycle.run_paper_bot_cycle") as bot_mock:
                exit_code = main(auto_args(root) + ["--confirm-paper-auto", "--session-ledger", str(ledger)])
            payload = read_json(root / "cycle" / "2026-06-16" / "cycle.json")
            records = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines()]

        self.assertEqual(exit_code, 1)
        self.assertEqual(payload["state"], "BLOCKED")
        self.assertIn("require_clean_state_required", payload["reasons"])
        self.assertEqual(prepare_mock.call_count, 0)
        self.assertEqual(bot_mock.call_count, 0)
        self.assertEqual(records[0]["state"], "BLOCKED")
        self.assertIn("require_clean_state_required", records[0]["blockers"])

    def test_confirmed_auto_cycle_requires_clean_operator_status_when_flag_is_set(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            readiness = root / "readiness.json"
            proposals = root / "llm_signal_proposals.json"
            signal_plan = root / "signal_plan.json"
            write_json(readiness, readiness_payload(status="READY", ready=True))
            write_json(proposals, {"status": "OK", "proposals": []})
            write_json(signal_plan, {"decision": "ELIGIBLE_FOR_PAPER", "eligible_for_paper": True})

            with patched_cycle_steps(root, readiness=readiness, proposals=proposals, signal_plan=signal_plan) as calls:
                exit_code = main(auto_args(root) + ["--confirm-paper-auto", "--require-clean-state"])
            payload = read_json(root / "cycle" / "2026-06-16" / "cycle.json")

        self.assertEqual(exit_code, 1)
        self.assertEqual(payload["state"], "BLOCKED")
        self.assertIn("operator_status_required", payload["reasons"])
        self.assertEqual(calls["review"].call_count, 0)
        self.assertEqual(calls["bot"].call_count, 0)

    def test_confirmed_auto_cycle_blocks_when_operator_status_is_not_clean(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            readiness = root / "readiness.json"
            proposals = root / "llm_signal_proposals.json"
            signal_plan = root / "signal_plan.json"
            operator_status = write_json(
                root / "operator_status.json",
                {
                    "status": "CRITICAL",
                    "as_of_date": "2026-06-16",
                    "clean_for_paper_auto": False,
                    "blockers": [{"severity": "CRITICAL", "code": "open_broker_orders", "message": "open order"}],
                    "safety": {"paper_only": True, "live_trading_authorized": False},
                },
            )
            write_json(readiness, readiness_payload(status="READY", ready=True))
            write_json(proposals, {"status": "OK", "proposals": []})
            write_json(signal_plan, {"decision": "ELIGIBLE_FOR_PAPER", "eligible_for_paper": True})

            with patched_cycle_steps(root, readiness=readiness, proposals=proposals, signal_plan=signal_plan) as calls:
                exit_code = main(
                    auto_args(root)
                    + [
                        "--confirm-paper-auto",
                        "--require-clean-state",
                        "--operator-status",
                        str(operator_status),
                    ]
                )
            payload = read_json(root / "cycle" / "2026-06-16" / "cycle.json")

        self.assertEqual(exit_code, 1)
        self.assertEqual(payload["state"], "BLOCKED")
        self.assertIn("open_broker_orders", payload["reasons"])
        self.assertEqual(payload["artifacts"]["operator_status"], str(operator_status))
        self.assertEqual(calls["review"].call_count, 0)
        self.assertEqual(calls["bot"].call_count, 0)

    def test_confirmed_auto_cycle_blocks_when_campaign_report_has_blockers(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            readiness = root / "readiness.json"
            proposals = root / "llm_signal_proposals.json"
            signal_plan = root / "signal_plan.json"
            operator_status = write_json(
                root / "operator_status.json",
                {
                    "status": "OK",
                    "as_of_date": "2026-06-16",
                    "clean_for_paper_auto": True,
                    "blockers": [],
                    "safety": {"paper_only": True, "live_trading_authorized": False},
                },
            )
            campaign_report = write_json(
                root / "campaign.json",
                {
                    "status": "OK",
                    "as_of_date": "2026-06-16",
                    "paper_auto_campaign": {
                        "state": "BLOCKED",
                        "blocker_histogram": {"statement_pending": 1},
                    },
                    "safety": {"paper_only": True, "live_trading_authorized": False},
                },
            )
            write_json(readiness, readiness_payload(status="READY", ready=True))
            write_json(proposals, {"status": "OK", "proposals": []})
            write_json(signal_plan, {"decision": "ELIGIBLE_FOR_PAPER", "eligible_for_paper": True})

            with patched_cycle_steps(root, readiness=readiness, proposals=proposals, signal_plan=signal_plan) as calls:
                exit_code = main(
                    auto_args(root)
                    + [
                        "--confirm-paper-auto",
                        "--require-clean-state",
                        "--operator-status",
                        str(operator_status),
                        "--campaign-report",
                        str(campaign_report),
                    ]
                )
            payload = read_json(root / "cycle" / "2026-06-16" / "cycle.json")

        self.assertEqual(exit_code, 1)
        self.assertEqual(payload["state"], "BLOCKED")
        self.assertIn("statement_pending", payload["reasons"])
        self.assertEqual(payload["artifacts"]["campaign_report"], str(campaign_report))
        self.assertEqual(calls["review"].call_count, 0)
        self.assertEqual(calls["bot"].call_count, 0)

    def test_confirmed_auto_cycle_blocks_duplicate_same_date_cycle_before_prepare(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            ledger = root / "session_ledger.jsonl"
            append_confirmed_record(ledger, as_of_date="2026-06-16", state="PAPER_CLOSED")
            operator_status = write_json(
                root / "operator_status.json",
                {
                    "status": "OK",
                    "as_of_date": "2026-06-16",
                    "clean_for_paper_auto": True,
                    "blockers": [],
                    "safety": {"paper_only": True, "live_trading_authorized": False},
                },
            )

            with mock.patch("trading_ai.execution.paper_auto_cycle.prepare_paper_daily") as prepare_mock, \
                mock.patch("trading_ai.execution.paper_auto_cycle.run_paper_bot_cycle") as bot_mock:
                exit_code = main(
                    auto_args(root)
                    + [
                        "--confirm-paper-auto",
                        "--require-clean-state",
                        "--operator-status",
                        str(operator_status),
                        "--session-ledger",
                        str(ledger),
                    ]
                )
            payload = read_json(root / "cycle" / "2026-06-16" / "cycle.json")

        self.assertEqual(exit_code, 1)
        self.assertEqual(payload["state"], "BLOCKED")
        self.assertIn("duplicate_confirmed_cycle", payload["reasons"])
        self.assertEqual(prepare_mock.call_count, 0)
        self.assertEqual(bot_mock.call_count, 0)

    def test_auto_cycle_with_confirmation_records_auto_review_and_calls_paper_bot_cycle(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            readiness = root / "readiness.json"
            proposals = root / "llm_signal_proposals.json"
            signal_plan = root / "signal_plan.json"
            review = root / "auto_review.json"
            bot = root / "bot_cycle.json"
            operator_status = write_json(
                root / "operator_status.json",
                {
                    "status": "OK",
                    "as_of_date": "2026-06-16",
                    "clean_for_paper_auto": True,
                    "blockers": [],
                    "safety": {"paper_only": True, "live_trading_authorized": False},
                },
            )
            write_json(readiness, readiness_payload(status="READY", ready=True))
            write_json(proposals, {"status": "OK", "proposals": []})
            write_json(signal_plan, {"decision": "ELIGIBLE_FOR_PAPER", "eligible_for_paper": True})
            write_json(review, {"status": "RECORDED", "decision": "APPROVE_PAPER_CONFIRMATION"})
            write_json(bot, {"state": "PAPER_SUBMITTED", "exit_code": 0})

            with patched_cycle_steps(
                root,
                readiness=readiness,
                proposals=proposals,
                signal_plan=signal_plan,
                review=review,
                bot=bot,
            ) as calls:
                exit_code = main(
                    auto_args(root)
                    + [
                        "--confirm-paper-auto",
                        "--require-clean-state",
                        "--operator-status",
                        str(operator_status),
                    ]
                )
            payload = read_json(root / "cycle" / "2026-06-16" / "cycle.json")
            bot_kwargs = calls["bot"].call_args.kwargs

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["state"], "PAPER_SUBMITTED")
        self.assertEqual(calls["review"].call_count, 1)
        self.assertEqual(calls["bot"].call_count, 1)
        self.assertEqual(Path(str(bot_kwargs["readiness"])), readiness)
        self.assertEqual(Path(str(bot_kwargs["signal_plan"])), signal_plan)
        self.assertTrue(bot_kwargs["confirm_readiness"])
        self.assertTrue(bot_kwargs["confirm_paper"])
        self.assertTrue(bot_kwargs["confirm_auto_submit"])


def patched_cycle_steps(
    root: Path,
    *,
    readiness: Path,
    proposals: Path,
    signal_plan: Path,
    review: Path | None = None,
    bot: Path | None = None,
):
    session_dir = root / "session"
    features = root / "features.csv"
    signal_report = root / "model_signals.json"
    freshness = root / "freshness.json"
    session_json = session_dir / "session.json"
    features.write_text("timestamp,symbol\n2026-06-16,SPY\n", encoding="utf-8")
    write_json(signal_report, {"signals": [], "selected_signal": None})
    write_json(freshness, {"features_path": str(features)})
    write_json(session_json, {"paths": {"signal_report": str(signal_report), "freshness_report": str(freshness)}})
    prepare_payload = read_json(readiness)
    prepare_payload["offline_smoke"] = {"artifacts": {"session_json": str(session_json)}}
    prepare_result = PaperDailyPrepareResult(
        exit_code=0,
        status="READY",
        ready_for_paper_daily=True,
        output_dir=root / "prepare",
        readiness_path=readiness,
        readiness_markdown_path=root / "readiness.md",
        paper_daily_config_path=root / "paper_daily.generated.yml",
        payload=prepare_payload,
    )
    proposal_result = LLMSignalProposalsResult(
        exit_code=0,
        status="OK",
        output_path=proposals,
        markdown_path=root / "llm_signal_proposals.md",
        payload=read_json(proposals),
    )
    arbitration_result = PaperSignalArbitrationResult(
        exit_code=0,
        decision="ELIGIBLE_FOR_PAPER",
        eligible_for_paper=True,
        output_path=signal_plan,
        markdown_path=root / "signal_plan.md",
        payload=read_json(signal_plan),
    )
    review_result = PaperReviewDecisionResult(
        exit_code=0,
        status="RECORDED",
        output_path=review or (root / "auto_review.json"),
        markdown_path=root / "auto_review.md",
        payload=read_json(review) if review is not None else {"status": "RECORDED"},
    )
    bot_result = PaperBotCycleResult(
        exit_code=0,
        state="PAPER_SUBMITTED",
        output_path=bot or (root / "bot_cycle.json"),
        markdown_path=root / "bot_cycle.md",
        payload=read_json(bot) if bot is not None else {"state": "PAPER_SUBMITTED", "exit_code": 0},
    )
    return _patched_steps(
        prepare_result,
        proposal_result,
        arbitration_result,
        review_result,
        bot_result,
    )


class _patched_steps:
    def __init__(
        self,
        prepare_result: PaperDailyPrepareResult,
        proposal_result: LLMSignalProposalsResult,
        arbitration_result: PaperSignalArbitrationResult,
        review_result: PaperReviewDecisionResult,
        bot_result: PaperBotCycleResult,
    ) -> None:
        self._patch = mock.patch.multiple(
            "trading_ai.execution.paper_auto_cycle",
            prepare_paper_daily=mock.DEFAULT,
            run_llm_signal_proposals=mock.DEFAULT,
            run_paper_signal_arbitration=mock.DEFAULT,
            run_paper_review_decision=mock.DEFAULT,
            run_paper_bot_cycle=mock.DEFAULT,
        )
        self._results = {
            "prepare": prepare_result,
            "proposals": proposal_result,
            "arbitration": arbitration_result,
            "review": review_result,
            "bot": bot_result,
        }

    def __enter__(self) -> dict[str, mock.Mock]:
        calls = self._patch.__enter__()
        calls["prepare_paper_daily"].return_value = self._results["prepare"]
        calls["run_llm_signal_proposals"].return_value = self._results["proposals"]
        calls["run_paper_signal_arbitration"].return_value = self._results["arbitration"]
        calls["run_paper_review_decision"].return_value = self._results["review"]
        calls["run_paper_bot_cycle"].return_value = self._results["bot"]
        return {
            "prepare": calls["prepare_paper_daily"],
            "proposals": calls["run_llm_signal_proposals"],
            "arbitration": calls["run_paper_signal_arbitration"],
            "review": calls["run_paper_review_decision"],
            "bot": calls["run_paper_bot_cycle"],
        }

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self._patch.__exit__(exc_type, exc, tb)


def auto_args(root: Path) -> list[str]:
    return [
        "paper-auto-cycle",
        "--as-of-date",
        "2026-06-16",
        "--source",
        str(root / "fresh.csv"),
        "--dataset-id",
        "core_etfs",
        "--frequency",
        "1d",
        "--from",
        "2026-03-01",
        "--to",
        "2026-06-16",
        "--license-note",
        "manual approval",
        "--output-dir",
        str(root / "cycle"),
    ]


def readiness_payload(*, status: str, ready: bool, reasons: list[str] | None = None) -> dict[str, object]:
    return {
        "status": status,
        "ready_for_paper_daily": ready,
        "as_of_date": "2026-06-16",
        "approved_dataset": {"symbols": ["SPY"], "end": "2026-06-16"},
        "artifacts": {"paper_daily_config": "paper_daily.generated.yml"},
        "reasons": reasons or [],
        "safety": {"credentials_read": False, "live_trading_allowed": False},
    }


def clean_operator_status() -> dict[str, object]:
    return {
        "status": "OK",
        "as_of_date": "2026-06-16",
        "clean_for_paper_auto": True,
        "blockers": [],
        "safety": {"paper_only": True, "live_trading_authorized": False},
    }


def append_confirmed_record(path: Path, *, as_of_date: str, state: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "record_type": "paper_auto_cycle_session",
        "session_id": f"paper-auto-{as_of_date}-confirmed",
        "generated_at": f"{as_of_date}T12:00:00+00:00",
        "as_of_date": as_of_date,
        "state": state,
        "exit_code": 0,
        "confirm_paper_auto": True,
        "order_state": "paper_order_sent",
        "closeout_status": "CLOSED",
        "statement_status": "MATCHED",
        "unreconciled_fills": 0,
        "blockers": [],
        "safety": {"paper_only": True, "live_trading_authorized": False},
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\n")


def write_json(path: Path, payload: dict[str, object]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return path


def read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
