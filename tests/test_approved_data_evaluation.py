import contextlib
import io
import json
import tempfile
import textwrap
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from trading_ai.backtest.engine import BacktestConfig, BacktestResult
from trading_ai.cli import _default_feature_names as cli_default_feature_names
from trading_ai.cli import build_parser, main
from trading_ai.data.io import PARQUET_DEPENDENCY_MESSAGE, ParquetDependencyError
from trading_ai.data.manifest import build_dataset_manifest, dataset_hash
from trading_ai.data.sample import generate_sample_ohlcv
from trading_ai.evaluation.approved_data import _default_feature_names as evaluation_default_feature_names


def write_universe(path: Path, symbols: tuple[str, ...]) -> Path:
    path.write_text(
        textwrap.dedent(
            f"""
            universe:
              symbols: [{", ".join(symbols)}]
            """
        ),
        encoding="utf-8",
    )
    return path


def write_risk(path: Path) -> Path:
    path.write_text(
        textwrap.dedent(
            """
            risk_limits:
              max_daily_loss_pct: 0.02
              max_drawdown_pct: 0.10
              max_gross_exposure: 1.0
              max_single_position: 0.30
              live_trading_allowed: false
            """
        ),
        encoding="utf-8",
    )
    return path


def write_trading_first_risk(path: Path) -> Path:
    path.write_text(
        textwrap.dedent(
            """
            risk_limits:
              max_daily_loss_pct: 0.02
              max_drawdown_pct: 0.10
              max_gross_exposure: 1.0
              max_single_position: 0.30
              live_trading_allowed: false
            model_quality:
              mode: trading_first
              min_sharpe: 1.0
              min_net_cagr: 0.05
              max_drawdown_pct: 0.12
              max_turnover: 200.0
              max_estimated_costs: 0.05
              min_trade_count: 100
            """
        ),
        encoding="utf-8",
    )
    return path


def fake_backtest_result(*, max_drawdown: float = 0.10) -> BacktestResult:
    return BacktestResult(
        config=BacktestConfig(),
        daily_returns=(0.01, 0.002),
        equity_curve=(1.01, 1.012),
        positions=(),
        trades=(),
        metrics={
            "cumulative_return": 0.20,
            "cagr": 0.13,
            "sharpe": 1.25,
            "sortino": 1.50,
            "max_drawdown": max_drawdown,
            "turnover": 150.0,
            "trade_count": 120.0,
            "average_exposure": 0.70,
            "estimated_costs": 0.03,
        },
    )


def daily_records(*, symbols: tuple[str, ...] = ("SPY",)) -> list[dict[str, object]]:
    return generate_sample_ohlcv(symbols=symbols, start="2025-01-01", end="2026-06-16")


def hourly_records() -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    timestamp = datetime(2026, 1, 1, 9)
    for index in range(90):
        close = 100.0 + index * 0.2
        rows.append(
            {
                "timestamp": timestamp.isoformat(timespec="seconds"),
                "symbol": "SPY",
                "open": close - 0.1,
                "high": close + 0.5,
                "low": close - 0.5,
                "close": close,
                "volume": 1_000_000 + index,
            }
        )
        timestamp += timedelta(hours=1)
    return rows


def write_approved_package(root: Path, *, dataset_id: str, frequency: str, records: list[dict[str, object]]) -> Path:
    approved_dir = root / "approved" / dataset_id / frequency
    approved_dir.mkdir(parents=True)
    dataset_path = approved_dir / "ohlcv.parquet"
    manifest_path = approved_dir / "manifest.json"
    catalog_path = approved_dir / "catalog_entry.json"
    dataset_path.write_bytes(b"PAR1 fake approved parquet for unit tests")
    manifest = build_dataset_manifest(records, source=str(dataset_path))
    manifest.update(
        {
            "dataset_id": dataset_id,
            "frequency": frequency,
            "source_sha256": "a" * 64,
            "provider": "manual_csv",
            "provider_kind": "manual",
            "license_note": "approved local fixture",
            "as_of_date": "2026-06-16",
        }
    )
    catalog_entry = {
        "schema_version": 1,
        "dataset_id": dataset_id,
        "frequency": frequency,
        "dataset_path": str(dataset_path),
        "manifest_path": str(manifest_path),
        "dataset_hash": manifest["dataset_hash"],
        "symbols": manifest["symbols"],
        "row_count": manifest["row_count"],
        "start": manifest["start"],
        "end": manifest["end"],
        "as_of_date": "2026-06-16",
        "provider": "manual_csv",
        "provider_kind": "manual",
        "network_allowed": False,
        "license_note": "approved local fixture",
    }
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    catalog_path.write_text(json.dumps(catalog_entry, indent=2, sort_keys=True), encoding="utf-8")
    return approved_dir


def candidate_spec_payload(
    *,
    candidate_id: str = "candidate-return-1d",
    dataset_hash_value: str,
    as_of_date: str = "2026-06-16",
    safety: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "candidate_id": candidate_id,
        "model_type": "logistic-baseline",
        "feature_names": ["return_1d"],
        "preprocessing": {"type": "none"},
        "training_config": {
            "learning_rate": 0.2,
            "epochs": 80,
            "l2": 0.001,
            "test_fraction": 0.25,
        },
        "dataset_hash": dataset_hash_value,
        "source_sha256": "a" * 64,
        "as_of_date": as_of_date,
        "authority": {
            "mutates_latest_model": False,
            "orders_submitted": False,
            "broker_client_built": False,
            "credentials_read": False,
        },
        "safety": safety
        if safety is not None
        else {
            "paper_only": True,
            "live_trading_allowed": False,
            "futures_forex_execution": False,
            "llm_order_authority": "none",
        },
    }


class ApprovedDataEvaluationTests(unittest.TestCase):
    def test_evaluate_daily_approved_dataset_writes_full_reproducible_package(self) -> None:
        records = daily_records()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            approved_dir = write_approved_package(root, dataset_id="core_etfs", frequency="1d", records=records)
            output_dir = root / "reports"
            universe = write_universe(root / "universe.yml", ("SPY",))
            risk = write_risk(root / "risk.yml")

            with mock.patch("trading_ai.evaluation.approved_data.read_records", return_value=records):
                exit_code = main(
                    [
                        "evaluate-approved-data",
                        "--approved-dir",
                        str(approved_dir),
                        "--config",
                        str(universe),
                        "--risk",
                        str(risk),
                        "--output-dir",
                        str(output_dir),
                        "--as-of-date",
                        "2026-06-16",
                        "--min-accuracy-lift",
                        "-1.0",
                        "--min-test-samples",
                        "1",
                    ]
                )

            run_dir = output_dir / "core_etfs" / "1d" / "2026-06-16"
            payloads = {
                name: json.loads((run_dir / name).read_text(encoding="utf-8"))
                for name in (
                    "data_quality.json",
                    "backtest.json",
                    "model_run.json",
                    "model_eval.json",
                    "promotion_decision.json",
                    "evaluation_summary.json",
                )
            }
            markdown_exists = (run_dir / "backtest.md").exists() and (run_dir / "evaluation_summary.md").exists()
            signal_policy_backtest_exists = (run_dir / "signal_policy_backtest.json").exists()

        self.assertEqual(exit_code, 0)
        self.assertTrue(markdown_exists)
        self.assertTrue(payloads["data_quality.json"]["passed"])
        self.assertEqual(payloads["backtest.json"]["config"]["periods_per_year"], 252)
        self.assertEqual(payloads["promotion_decision.json"]["eligible_for_paper_challenger"], True)
        # The deployed logistic signal policy is now backtested and surfaced (informational).
        signal_policy_gate = payloads["promotion_decision.json"]["signal_policy_gate"]
        self.assertEqual(signal_policy_gate["strategy"], "signal_policy_single_name")
        self.assertFalse(signal_policy_gate["blocking"])
        self.assertIn("sharpe", signal_policy_gate["metrics"])
        self.assertTrue(signal_policy_backtest_exists)
        self.assertEqual(payloads["evaluation_summary.json"]["status"], "APPROVED")
        self.assertEqual(payloads["evaluation_summary.json"]["approved_dataset"]["dataset_hash"], dataset_hash(records))
        feature_names = payloads["model_eval.json"]["feature_names"]
        self.assertIn("momentum_60", feature_names)
        self.assertIn("rolling_drawdown_20", feature_names)
        self.assertIn("daily_range", feature_names)
        self.assertIn("close_to_sma_20", feature_names)
        self.assertIn("vol_adjusted_momentum_20", feature_names)
        self.assertNotIn("sma_20", feature_names)
        for payload in payloads.values():
            self.assertEqual(payload["approved_dataset"]["dataset_id"], "core_etfs")
            self.assertEqual(payload["approved_dataset"]["frequency"], "1d")
            self.assertEqual(payload["approved_dataset"]["source_sha256"], "a" * 64)

    def test_default_feature_whitelist_is_expanded_and_shared_without_raw_sma(self) -> None:
        feature_row = {
            "return_1d": 0.01,
            "momentum_20": 0.02,
            "momentum_60": 0.03,
            "momentum_120": 0.04,
            "realized_volatility_20": 0.20,
            "rolling_drawdown_20": 0.05,
            "daily_range": 0.01,
            "relative_volume_20": 1.10,
            "sma_20": 100.0,
            "close_to_sma_20": 0.02,
            "close_to_sma_60": 0.03,
            "vol_adjusted_momentum_20": 0.10,
            "vol_adjusted_momentum_60": 0.15,
        }
        expected = (
            "return_1d",
            "momentum_20",
            "momentum_60",
            "momentum_120",
            "realized_volatility_20",
            "rolling_drawdown_20",
            "daily_range",
            "relative_volume_20",
            "close_to_sma_20",
            "close_to_sma_60",
            "vol_adjusted_momentum_20",
            "vol_adjusted_momentum_60",
        )

        self.assertEqual(evaluation_default_feature_names([feature_row]), expected)
        self.assertEqual(cli_default_feature_names([feature_row]), expected)

    def test_hourly_auto_periods_per_year_and_override(self) -> None:
        records = hourly_records()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            approved_dir = write_approved_package(root, dataset_id="core_etfs", frequency="1h", records=records)
            universe = write_universe(root / "universe.yml", ("SPY",))
            risk = write_risk(root / "risk.yml")

            with mock.patch("trading_ai.evaluation.approved_data.read_records", return_value=records):
                auto_exit = main(
                    eval_args(root, approved_dir=approved_dir, universe=universe, risk=risk, output_dir=root / "auto")
                )
                override_exit = main(
                    eval_args(
                        root,
                        approved_dir=approved_dir,
                        universe=universe,
                        risk=risk,
                        output_dir=root / "override",
                        extra=["--periods-per-year", "1000"],
                    )
                )
            auto_backtest = json.loads(
                (root / "auto" / "core_etfs" / "1h" / "2026-06-16" / "backtest.json").read_text(encoding="utf-8")
            )
            override_backtest = json.loads(
                (root / "override" / "core_etfs" / "1h" / "2026-06-16" / "backtest.json").read_text(encoding="utf-8")
            )

        self.assertEqual(auto_exit, 0)
        self.assertEqual(override_exit, 0)
        self.assertEqual(auto_backtest["config"]["periods_per_year"], 1638)
        self.assertEqual(override_backtest["config"]["periods_per_year"], 1000)

    def test_hash_mismatch_is_operational_error(self) -> None:
        records = daily_records()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            approved_dir = write_approved_package(root, dataset_id="core_etfs", frequency="1d", records=records)
            manifest_path = approved_dir / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["dataset_hash"] = "0" * 64
            manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
            catalog_path = approved_dir / "catalog_entry.json"
            catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
            catalog["dataset_hash"] = "0" * 64
            catalog_path.write_text(json.dumps(catalog, indent=2, sort_keys=True), encoding="utf-8")
            universe = write_universe(root / "universe.yml", ("SPY",))
            risk = write_risk(root / "risk.yml")

            with mock.patch("trading_ai.evaluation.approved_data.read_records", return_value=records):
                exit_code = main(eval_args(root, approved_dir=approved_dir, universe=universe, risk=risk))

        self.assertEqual(exit_code, 2)

    def test_candidate_spec_hash_mismatch_blocks_evaluation(self) -> None:
        records = daily_records()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            approved_dir = write_approved_package(root, dataset_id="core_etfs", frequency="1d", records=records)
            candidate_spec = root / "bad_candidate_spec.json"
            candidate_spec.write_text(
                json.dumps(
                    candidate_spec_payload(candidate_id="bad-hash", dataset_hash_value="0" * 64),
                    indent=2,
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
            universe = write_universe(root / "universe.yml", ("SPY",))
            risk = write_risk(root / "risk.yml")
            stderr = io.StringIO()

            with (
                mock.patch(
                    "trading_ai.evaluation.approved_data.read_records",
                    return_value=records,
                ),
                contextlib.redirect_stderr(stderr),
            ):
                exit_code = main(
                    eval_args(
                        root,
                        approved_dir=approved_dir,
                        universe=universe,
                        risk=risk,
                        extra=["--candidate-spec", str(candidate_spec)],
                    )
                )

        self.assertEqual(exit_code, 2)
        self.assertIn("candidate spec dataset_hash mismatch", stderr.getvalue())

    def test_candidate_spec_unsafe_safety_blocks_evaluation(self) -> None:
        records = daily_records()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            approved_dir = write_approved_package(root, dataset_id="core_etfs", frequency="1d", records=records)
            candidate_spec = root / "unsafe_candidate_spec.json"
            candidate_spec.write_text(
                json.dumps(
                    candidate_spec_payload(
                        candidate_id="unsafe",
                        dataset_hash_value=dataset_hash(records),
                        safety={
                            "paper_only": False,
                            "live_trading_allowed": True,
                            "orders_submitted": False,
                            "broker_client_built": False,
                            "credentials_read": False,
                        },
                    ),
                    indent=2,
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
            universe = write_universe(root / "universe.yml", ("SPY",))
            risk = write_risk(root / "risk.yml")
            stderr = io.StringIO()

            with (
                mock.patch(
                    "trading_ai.evaluation.approved_data.read_records",
                    return_value=records,
                ),
                contextlib.redirect_stderr(stderr),
            ):
                exit_code = main(
                    eval_args(
                        root,
                        approved_dir=approved_dir,
                        universe=universe,
                        risk=risk,
                        extra=["--candidate-spec", str(candidate_spec)],
                    )
                )

        self.assertEqual(exit_code, 2)
        self.assertIn("candidate spec safety.paper_only must be true", stderr.getvalue())

    def test_candidate_spec_stale_as_of_date_blocks_evaluation(self) -> None:
        records = daily_records()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            approved_dir = write_approved_package(root, dataset_id="core_etfs", frequency="1d", records=records)
            candidate_spec = root / "stale_candidate_spec.json"
            candidate_spec.write_text(
                json.dumps(
                    candidate_spec_payload(
                        candidate_id="stale",
                        dataset_hash_value=dataset_hash(records),
                        as_of_date="2026-06-15",
                    ),
                    indent=2,
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
            universe = write_universe(root / "universe.yml", ("SPY",))
            risk = write_risk(root / "risk.yml")
            stderr = io.StringIO()

            with (
                mock.patch(
                    "trading_ai.evaluation.approved_data.read_records",
                    return_value=records,
                ),
                contextlib.redirect_stderr(stderr),
            ):
                exit_code = main(
                    eval_args(
                        root,
                        approved_dir=approved_dir,
                        universe=universe,
                        risk=risk,
                        extra=["--candidate-spec", str(candidate_spec)],
                    )
                )

        self.assertEqual(exit_code, 2)
        self.assertIn("candidate spec as_of_date mismatch", stderr.getvalue())

    def test_invalid_symbols_or_timestamps_block_before_model_artifacts(self) -> None:
        cases = {
            "bad_symbol": ("1d", [{**row, "symbol": "TSLA"} for row in daily_records()]),
            "bad_timestamp": ("1h", [{**row, "timestamp": "2026-06-16T09:30:00"} for row in hourly_records()]),
        }
        for case_name, (frequency, records) in cases.items():
            with self.subTest(case=case_name), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                approved_dir = write_approved_package(
                    root,
                    dataset_id="core_etfs",
                    frequency=frequency,
                    records=records,
                )
                universe = write_universe(root / "universe.yml", ("SPY",))
                risk = write_risk(root / "risk.yml")

                with mock.patch("trading_ai.evaluation.approved_data.read_records", return_value=records):
                    exit_code = main(eval_args(root, approved_dir=approved_dir, universe=universe, risk=risk))

                run_dir = root / "reports" / "core_etfs" / frequency / "2026-06-16"
                summary = json.loads((run_dir / "evaluation_summary.json").read_text(encoding="utf-8"))
                data_quality = json.loads((run_dir / "data_quality.json").read_text(encoding="utf-8"))

                self.assertEqual(exit_code, 1)
                self.assertEqual(summary["status"], "BLOCKED")
                self.assertFalse(data_quality["passed"])
                self.assertFalse((run_dir / "model_run.json").exists())
                self.assertFalse((run_dir / "backtest.json").exists())

    def test_rejected_package_writes_evidence_and_summary(self) -> None:
        records = daily_records()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            approved_dir = write_approved_package(root, dataset_id="core_etfs", frequency="1d", records=records)
            universe = write_universe(root / "universe.yml", ("SPY",))
            risk = write_risk(root / "risk.yml")

            with mock.patch("trading_ai.evaluation.approved_data.read_records", return_value=records):
                exit_code = main(
                    eval_args(
                        root,
                        approved_dir=approved_dir,
                        universe=universe,
                        risk=risk,
                        extra=["--min-test-samples", "9999"],
                    )
                )
            run_dir = root / "reports" / "core_etfs" / "1d" / "2026-06-16"
            summary = json.loads((run_dir / "evaluation_summary.json").read_text(encoding="utf-8"))
            promotion = json.loads((run_dir / "promotion_decision.json").read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 1)
        self.assertEqual(summary["status"], "REJECTED")
        self.assertFalse(summary["eligible_for_paper_challenger"])
        self.assertIn("insufficient_test_samples", promotion["reasons"])

    def test_parser_defaults_and_missing_parquet_dependency_exit_two(self) -> None:
        args = build_parser().parse_args(
            [
                "evaluate-approved-data",
                "--approved-dir",
                "/tmp/approved/core_etfs/1d",  # noqa: S108
                "--as-of-date",
                "2026-06-16",
            ]
        )
        self.assertEqual(args.config, "configs/universe.yml")
        self.assertEqual(args.risk, "configs/risk.yml")
        self.assertEqual(args.output_dir, "reports/tmp/approved_eval")
        self.assertEqual(args.periods_per_year, "auto")
        self.assertEqual(args.min_accuracy_lift, 0.02)
        self.assertEqual(args.min_test_samples, 30)

        records = daily_records()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            approved_dir = write_approved_package(root, dataset_id="core_etfs", frequency="1d", records=records)
            universe = write_universe(root / "universe.yml", ("SPY",))
            risk = write_risk(root / "risk.yml")
            stderr = io.StringIO()

            with (
                mock.patch(
                    "trading_ai.evaluation.approved_data.read_records",
                    side_effect=ParquetDependencyError(PARQUET_DEPENDENCY_MESSAGE),
                ),
                contextlib.redirect_stderr(stderr),
            ):
                exit_code = main(eval_args(root, approved_dir=approved_dir, universe=universe, risk=risk))

        self.assertEqual(exit_code, 2)
        self.assertIn('pip install -e ".[research]"', stderr.getvalue())

    def test_evaluation_does_not_build_alpaca_client(self) -> None:
        records = daily_records()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            approved_dir = write_approved_package(root, dataset_id="core_etfs", frequency="1d", records=records)
            universe = write_universe(root / "universe.yml", ("SPY",))
            risk = write_risk(root / "risk.yml")

            with (
                mock.patch("trading_ai.evaluation.approved_data.read_records", return_value=records),
                mock.patch(
                    "trading_ai.cli.build_alpaca_paper_client",
                    side_effect=AssertionError("alpaca client should not be built"),
                ),
            ):
                exit_code = main(
                    eval_args(
                        root,
                        approved_dir=approved_dir,
                        universe=universe,
                        risk=risk,
                        extra=["--min-test-samples", "9999"],
                    )
                )

        self.assertEqual(exit_code, 1)

    def test_trading_first_accepts_candidate_when_trading_gate_passes_despite_accuracy_failure(self) -> None:
        records = daily_records()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            approved_dir = write_approved_package(root, dataset_id="core_etfs", frequency="1d", records=records)
            universe = write_universe(root / "universe.yml", ("SPY",))
            risk = write_trading_first_risk(root / "risk.yml")

            with (
                mock.patch("trading_ai.evaluation.approved_data.read_records", return_value=records),
                mock.patch(
                    "trading_ai.evaluation.approved_data.run_momentum_vol_target_backtest",
                    return_value=fake_backtest_result(),
                ),
            ):
                exit_code = main(
                    eval_args(
                        root,
                        approved_dir=approved_dir,
                        universe=universe,
                        risk=risk,
                        extra=["--min-accuracy-lift", "999.0", "--min-test-samples", "1"],
                    )
                )
            run_dir = root / "reports" / "core_etfs" / "1d" / "2026-06-16"
            summary = json.loads((run_dir / "evaluation_summary.json").read_text(encoding="utf-8"))
            promotion = json.loads((run_dir / "promotion_decision.json").read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 0)
        self.assertEqual(summary["status"], "APPROVED")
        self.assertEqual(summary["quality_policy"]["mode"], "trading_first")
        self.assertEqual(summary["trading_gate"]["status"], "PASS")
        self.assertEqual(summary["classification_gate"]["status"], "FAIL")
        self.assertFalse(summary["classification_gate"]["blocking"])
        self.assertTrue(promotion["eligible_for_paper_challenger"])
        self.assertEqual(promotion["reasons"], [])

    def test_trading_first_rejects_candidate_when_drawdown_exceeds_trading_limit(self) -> None:
        records = daily_records()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            approved_dir = write_approved_package(root, dataset_id="core_etfs", frequency="1d", records=records)
            universe = write_universe(root / "universe.yml", ("SPY",))
            risk = write_trading_first_risk(root / "risk.yml")

            with (
                mock.patch("trading_ai.evaluation.approved_data.read_records", return_value=records),
                mock.patch(
                    "trading_ai.evaluation.approved_data.run_momentum_vol_target_backtest",
                    return_value=fake_backtest_result(max_drawdown=0.13),
                ),
            ):
                exit_code = main(
                    eval_args(
                        root,
                        approved_dir=approved_dir,
                        universe=universe,
                        risk=risk,
                        extra=["--min-accuracy-lift", "999.0", "--min-test-samples", "1"],
                    )
                )
            run_dir = root / "reports" / "core_etfs" / "1d" / "2026-06-16"
            summary = json.loads((run_dir / "evaluation_summary.json").read_text(encoding="utf-8"))
            promotion = json.loads((run_dir / "promotion_decision.json").read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 1)
        self.assertEqual(summary["status"], "REJECTED")
        self.assertEqual(summary["trading_gate"]["status"], "FAIL")
        self.assertIn("max_drawdown_above_limit", summary["reasons"])
        self.assertIn("max_drawdown_above_limit", promotion["reasons"])

    def test_evaluation_writes_walk_forward_and_regime_robustness_artifacts(self) -> None:
        records = daily_records()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            approved_dir = write_approved_package(root, dataset_id="core_etfs", frequency="1d", records=records)
            universe = write_universe(root / "universe.yml", ("SPY",))
            risk = write_risk(root / "risk.yml")

            with mock.patch("trading_ai.evaluation.approved_data.read_records", return_value=records):
                exit_code = main(
                    eval_args(
                        root,
                        approved_dir=approved_dir,
                        universe=universe,
                        risk=risk,
                        extra=["--min-accuracy-lift", "-1.0", "--min-test-samples", "1"],
                    )
                )
            run_dir = root / "reports" / "core_etfs" / "1d" / "2026-06-16"
            summary = json.loads((run_dir / "evaluation_summary.json").read_text(encoding="utf-8"))
            promotion = json.loads((run_dir / "promotion_decision.json").read_text(encoding="utf-8"))
            walk_forward = json.loads((run_dir / "walk_forward.json").read_text(encoding="utf-8"))
            regimes = json.loads((run_dir / "regime_slices.json").read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 0)
        self.assertIn("walk_forward", summary["artifacts"])
        self.assertIn("regime_slices", summary["artifacts"])
        self.assertIn("costs", promotion)
        self.assertIn("robustness", promotion)
        self.assertGreaterEqual(walk_forward["summary"]["window_count"], 1)
        self.assertIn("slices", regimes)

    def test_temporal_leakage_feature_blocks_challenger_without_mutating_latest_model(self) -> None:
        records = [{**row, "future_return_1d": 0.01} for row in daily_records()]
        latest_model_before = Path("models/latest_model.json").read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            approved_dir = write_approved_package(root, dataset_id="core_etfs", frequency="1d", records=records)
            universe = write_universe(root / "universe.yml", ("SPY",))
            risk = write_risk(root / "risk.yml")

            with mock.patch("trading_ai.evaluation.approved_data.read_records", return_value=records):
                exit_code = main(
                    eval_args(
                        root,
                        approved_dir=approved_dir,
                        universe=universe,
                        risk=risk,
                        extra=["--min-accuracy-lift", "-1.0", "--min-test-samples", "1"],
                    )
                )
            run_dir = root / "reports" / "core_etfs" / "1d" / "2026-06-16"
            promotion = json.loads((run_dir / "promotion_decision.json").read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 1)
        self.assertFalse(promotion["eligible_for_paper_challenger"])
        self.assertIn("temporal_leakage_detected", promotion["reasons"])
        self.assertEqual(Path("models/latest_model.json").read_text(encoding="utf-8"), latest_model_before)


def eval_args(
    root: Path,
    *,
    approved_dir: Path,
    universe: Path,
    risk: Path,
    output_dir: Path | None = None,
    extra: list[str] | None = None,
) -> list[str]:
    args = [
        "evaluate-approved-data",
        "--approved-dir",
        str(approved_dir),
        "--config",
        str(universe),
        "--risk",
        str(risk),
        "--output-dir",
        str(output_dir or root / "reports"),
        "--as-of-date",
        "2026-06-16",
        "--min-accuracy-lift",
        "-1.0",
        "--min-test-samples",
        "1",
    ]
    if extra:
        args.extend(extra)
    return args


if __name__ == "__main__":
    unittest.main()
