import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from trading_ai.cli import build_parser, main


class PaperStrategyQualityTests(unittest.TestCase):
    def test_parser_defaults_for_strategy_quality(self) -> None:
        args = build_parser().parse_args(
            [
                "paper-strategy-quality",
                "--as-of-date",
                "2026-06-16",
                "--model-signals",
                "signals.json",
                "--signal-plan",
                "plan.json",
                "--performance",
                "performance.json",
            ]
        )

        self.assertEqual(args.as_of_date, "2026-06-16")
        self.assertEqual(args.model_signals, "signals.json")
        self.assertEqual(args.signal_plan, "plan.json")
        self.assertEqual(args.performance, "performance.json")
        self.assertIsNone(args.challenger_report)
        self.assertEqual(args.output_dir, "reports/tmp/paper_strategy_quality")
        self.assertEqual(args.min_clean_sessions, 20)
        self.assertEqual(args.min_paper_fills, 20)
        self.assertIsNone(args.max_cost_drag_bps)
        self.assertIsNone(args.max_trade_count_gap_pct)
        self.assertEqual(args.ledger_input, [])
        self.assertEqual(args.lookback_sessions, 60)
        self.assertIsNone(args.max_blocker_rate_pct)
        self.assertIsNone(args.max_llm_disagreement_rate_pct)

    def test_strategy_quality_summarizes_baseline_challenger_and_cost_gap_without_promotion(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            signals = write_json(
                root / "signals.json",
                {
                    "signals": [
                        {"timestamp": "2026-06-16", "symbol": "SPY", "action": "buy", "probability": 0.82},
                        {"timestamp": "2026-06-16", "symbol": "QQQ", "action": "hold", "probability": 0.41},
                    ],
                    "selected_signal": {
                        "timestamp": "2026-06-16",
                        "symbol": "SPY",
                        "action": "buy",
                        "probability": 0.82,
                    },
                },
            )
            plan = write_json(root / "plan.json", {"decision": "ELIGIBLE_FOR_PAPER", "eligible_for_paper": True})
            performance = write_json(
                root / "performance.json",
                {
                    "status": "WARN",
                    "paper_metrics": {"fills": 1, "pending_closeouts": 0, "pnl": {"source": "proxy"}},
                    "paper_vs_backtest": {
                        "backtest_available": True,
                        "backtest_metrics": {"trade_count": 5, "estimated_costs": 0.04, "sharpe": 1.2},
                        "trade_count_gap": -4,
                    },
                    "safety": {"paper_only": True, "live_trading_authorized": False},
                },
            )
            challenger = write_json(
                root / "challenger.json",
                {"status": "DEFER", "decision": "DEFER", "safety": {"live_trading_authorized": False}},
            )

            exit_code = main(
                [
                    "paper-strategy-quality",
                    "--as-of-date",
                    "2026-06-16",
                    "--model-signals",
                    str(signals),
                    "--signal-plan",
                    str(plan),
                    "--performance",
                    str(performance),
                    "--challenger-report",
                    str(challenger),
                    "--output-dir",
                    str(root / "quality"),
                ]
            )
            payload = read_json(root / "quality" / "2026-06-16" / "strategy_quality.json")
            markdown = (root / "quality" / "2026-06-16" / "strategy_quality.md").read_text(encoding="utf-8")

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["status"], "WARN")
        self.assertEqual(payload["quality_status"], "DEFER")
        self.assertEqual(payload["baseline"]["selected_symbol"], "SPY")
        self.assertEqual(payload["arbitration"]["decision"], "ELIGIBLE_FOR_PAPER")
        self.assertEqual(payload["challenger"]["decision"], "DEFER")
        self.assertEqual(payload["cost_adjusted"]["estimated_costs"], 0.04)
        self.assertFalse(payload["authority"]["model_promoted"])
        self.assertFalse(payload["authority"]["risk_changed"])
        self.assertFalse(payload["safety"]["live_trading_authorized"])
        self.assertIn("Model promoted: `False`", markdown)

    def test_strategy_quality_passes_when_campaign_and_fill_thresholds_are_met(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            signals = write_signals(root / "signals.json")
            plan = write_json(root / "plan.json", {"decision": "ELIGIBLE_FOR_PAPER", "eligible_for_paper": True})
            performance = write_performance(
                root / "performance.json",
                clean_sessions=20,
                fills=20,
                trade_count=20,
                estimated_costs_bps=4.0,
            )

            exit_code = main(
                strategy_args(root, signals=signals, plan=plan, performance=performance)
                + ["--max-cost-drag-bps", "5", "--max-trade-count-gap-pct", "10"]
            )
            payload = read_json(root / "quality" / "2026-06-16" / "strategy_quality.json")

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["status"], "OK")
        self.assertEqual(payload["quality_status"], "PASS")
        self.assertEqual(payload["thresholds"]["min_clean_sessions"], 20)
        self.assertFalse(payload["authority"]["model_promoted"])

    def test_strategy_quality_warns_on_trade_count_gap_and_blocks_performance_blockers(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            signals = write_signals(root / "signals.json")
            plan = write_json(root / "plan.json", {"decision": "ELIGIBLE_FOR_PAPER", "eligible_for_paper": True})
            gap_performance = write_performance(
                root / "gap_performance.json",
                clean_sessions=20,
                fills=20,
                trade_count=40,
                estimated_costs_bps=1.0,
            )

            gap_exit = main(
                strategy_args(root, signals=signals, plan=plan, performance=gap_performance)
                + ["--max-trade-count-gap-pct", "10"]
            )
            gap_payload = read_json(root / "quality" / "2026-06-16" / "strategy_quality.json")

            blocked_performance = write_performance(
                root / "blocked_performance.json",
                clean_sessions=20,
                fills=20,
                trade_count=20,
                estimated_costs_bps=1.0,
                blockers=["fills_unreconciled"],
            )
            blocked_exit = main(strategy_args(root, signals=signals, plan=plan, performance=blocked_performance))
            blocked_payload = read_json(root / "quality" / "2026-06-16" / "strategy_quality.json")

        self.assertEqual(gap_exit, 0)
        self.assertEqual(gap_payload["quality_status"], "WARN")
        self.assertIn("trade_count_gap_exceeds_threshold", gap_payload["warnings"])
        self.assertEqual(blocked_exit, 1)
        self.assertEqual(blocked_payload["quality_status"], "BLOCKED")
        self.assertIn("fills_unreconciled", blocked_payload["blockers"])

    def test_strategy_quality_blocks_llm_baseline_disagreement(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            signals = write_signals(root / "signals.json", action="buy")
            plan = write_json(
                root / "plan.json",
                {
                    "decision": "NO_TRADE_REVIEW",
                    "eligible_for_paper": False,
                    "discrepancies": [{"code": "llm_baseline_disagreement", "symbol": "SPY"}],
                },
            )
            performance = write_performance(
                root / "performance.json",
                clean_sessions=20,
                fills=20,
                trade_count=20,
                estimated_costs_bps=1.0,
            )

            exit_code = main(strategy_args(root, signals=signals, plan=plan, performance=performance))
            payload = read_json(root / "quality" / "2026-06-16" / "strategy_quality.json")

        self.assertEqual(exit_code, 1)
        self.assertEqual(payload["quality_status"], "BLOCKED")
        self.assertIn("llm_baseline_disagreement", payload["blockers"])

    def test_strategy_quality_trend_blocks_high_blocker_and_llm_disagreement_rates(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            signals = write_signals(root / "signals.json")
            plan = write_json(root / "plan.json", {"decision": "ELIGIBLE_FOR_PAPER", "eligible_for_paper": True})
            performance = write_performance(
                root / "performance.json",
                clean_sessions=60,
                fills=60,
                trade_count=60,
                estimated_costs_bps=1.0,
            )
            ledger = root / "session_ledger.jsonl"
            append_quality_record(ledger, session_id="clean-1", state="PAPER_CLOSED", blockers=[])
            append_quality_record(ledger, session_id="clean-2", state="PAPER_CLOSED", blockers=[])
            append_quality_record(ledger, session_id="clean-3", state="PAPER_CLOSED", blockers=[])
            append_quality_record(ledger, session_id="blocked-1", state="BLOCKED", blockers=["dataset_stale"])
            append_quality_record(
                ledger, session_id="blocked-2", state="BLOCKED", blockers=["llm_baseline_disagreement"]
            )
            append_quality_record(ledger, session_id="clean-4", state="PAPER_CLOSED", blockers=[])

            exit_code = main(
                strategy_args(root, signals=signals, plan=plan, performance=performance)
                + [
                    "--ledger-input",
                    str(ledger),
                    "--lookback-sessions",
                    "6",
                    "--max-blocker-rate-pct",
                    "20",
                    "--max-llm-disagreement-rate-pct",
                    "10",
                ]
            )
            payload = read_json(root / "quality" / "2026-06-16" / "strategy_quality.json")

        self.assertEqual(exit_code, 1)
        self.assertEqual(payload["quality_status"], "BLOCKED")
        self.assertIn("blocker_rate_exceeds_threshold", payload["blockers"])
        self.assertIn("llm_disagreement_rate_exceeds_threshold", payload["blockers"])
        self.assertEqual(payload["quality_trend"]["lookback_sessions"], 6)
        self.assertEqual(payload["quality_trend"]["total_sessions"], 6)
        self.assertAlmostEqual(payload["quality_trend"]["blocker_rate_pct"], 33.3333, places=3)
        self.assertAlmostEqual(payload["quality_trend"]["llm_disagreement_rate_pct"], 16.6666, places=3)
        self.assertEqual(payload["quality_trend"]["clean_session_trend"], "DECLINING")
        self.assertTrue(payload["quality_trend"]["fill_sufficiency"]["sufficient"])
        self.assertFalse(payload["authority"]["model_promoted"])


def write_json(path: Path, payload: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return path


def write_signals(path: Path, *, action: str = "buy") -> Path:
    return write_json(
        path,
        {
            "signals": [{"timestamp": "2026-06-16", "symbol": "SPY", "action": action, "probability": 0.82}],
            "selected_signal": {"timestamp": "2026-06-16", "symbol": "SPY", "action": action, "probability": 0.82},
        },
    )


def write_performance(
    path: Path,
    *,
    clean_sessions: int,
    fills: int,
    trade_count: int,
    estimated_costs_bps: float,
    blockers: list[str] | None = None,
) -> Path:
    return write_json(
        path,
        {
            "status": "OK" if not blockers else "WARN",
            "paper_metrics": {"fills": fills, "pending_closeouts": 0, "pnl": {"source": "proxy"}},
            "paper_auto_sessions": {
                "state": "READY_FOR_REVIEW" if clean_sessions >= 20 and not blockers else "ACCUMULATING",
                "clean_sessions": clean_sessions,
                "blocker_histogram": {},
            },
            "paper_vs_backtest": {
                "backtest_available": True,
                "backtest_metrics": {
                    "trade_count": trade_count,
                    "estimated_costs": estimated_costs_bps / 10000.0,
                    "estimated_costs_bps": estimated_costs_bps,
                    "sharpe": 1.2,
                },
                "trade_count_gap": fills - trade_count,
            },
            "blockers": blockers or [],
            "safety": {"paper_only": True, "live_trading_authorized": False},
        },
    )


def strategy_args(root: Path, *, signals: Path, plan: Path, performance: Path) -> list[str]:
    return [
        "paper-strategy-quality",
        "--as-of-date",
        "2026-06-16",
        "--model-signals",
        str(signals),
        "--signal-plan",
        str(plan),
        "--performance",
        str(performance),
        "--output-dir",
        str(root / "quality"),
    ]


def append_quality_record(path: Path, *, session_id: str, state: str, blockers: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "record_type": "paper_auto_cycle_session",
        "session_id": session_id,
        "generated_at": (
            f"2026-06-16T10:00:"
            f"{len(path.read_text(encoding='utf-8').splitlines()) if path.exists() else 0:02d}+00:00"
        ),
        "as_of_date": "2026-06-16",
        "state": state,
        "exit_code": 0 if state == "PAPER_CLOSED" else 1,
        "confirm_paper_auto": True,
        "order_state": "paper_order_sent" if state == "PAPER_CLOSED" else "not_sent",
        "closeout_status": "CLOSED" if state == "PAPER_CLOSED" else "NOT_APPLICABLE",
        "statement_status": "MATCHED" if state == "PAPER_CLOSED" else "NOT_REQUESTED",
        "unreconciled_fills": 0,
        "blockers": blockers,
        "safety": {"paper_only": True, "live_trading_authorized": False},
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\n")


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
