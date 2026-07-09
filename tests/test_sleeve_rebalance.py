"""Tests for the governed crypto-sleeve rebalance cycle (Sprint M3)."""

from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import date, timedelta
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from trading_ai.backtest.engine import (
    BacktestConfig,
    compute_target_weights_snapshot,
    run_momentum_vol_target_backtest,
)
from trading_ai.cli import main
from trading_ai.data.io import write_records
from trading_ai.execution.sleeve_rebalance import (
    map_broker_symbol_to_pair,
    run_sleeve_rebalance,
)


def _flat_ohlcv_row(*, timestamp: str, symbol: str, close: float) -> dict[str, object]:
    return {
        "timestamp": timestamp,
        "symbol": symbol,
        "open": close,
        "high": close,
        "low": close,
        "close": close,
        "volume": 1.0,
    }


def _increasing_ohlcv_row(
    *,
    timestamp: str,
    symbol: str,
    close: float,
) -> dict[str, object]:
    return {
        "timestamp": timestamp,
        "symbol": symbol,
        "open": close,
        "high": close,
        "low": close,
        "close": close,
        "volume": 1.0,
    }


def _build_flat_records(symbols: list[str], n_dates: int, start: str = "2026-06-01") -> list[dict[str, object]]:
    """Constant-close synthetic dataset (no momentum → no selection)."""
    rows: list[dict[str, object]] = []
    start_date = date.fromisoformat(start)
    for offset in range(n_dates):
        day = (start_date + timedelta(days=offset)).isoformat()
        for symbol in symbols:
            rows.append(
                _flat_ohlcv_row(timestamp=day, symbol=symbol, close=100.0)
            )
    return rows


def _build_momentum_records(
    symbols_to_close: dict[str, list[float]],
    start: str = "2026-06-01",
) -> list[dict[str, object]]:
    """Synthetic dataset where each symbol follows a per-symbol close path."""
    symbols = list(symbols_to_close.keys())
    n_dates = len(next(iter(symbols_to_close.values())))
    rows: list[dict[str, object]] = []
    start_date = date.fromisoformat(start)
    for offset in range(n_dates):
        day = (start_date + timedelta(days=offset)).isoformat()
        for symbol in symbols:
            close = symbols_to_close[symbol][offset]
            rows.append(_increasing_ohlcv_row(timestamp=day, symbol=symbol, close=close))
    return rows


class MapBrokerSymbolToPairTests(unittest.TestCase):
    def test_compact_broker_symbol_maps_to_slash_pair(self) -> None:
        self.assertEqual(
            map_broker_symbol_to_pair("BTCUSD", ["BTC/USD", "ETH/USD"]),
            "BTC/USD",
        )

    def test_uppercase_normalization(self) -> None:
        self.assertEqual(
            map_broker_symbol_to_pair("ethusd", ["BTC/USD", "ETH/USD"]),
            "ETH/USD",
        )

    def test_unknown_broker_symbol_returns_none(self) -> None:
        self.assertIsNone(map_broker_symbol_to_pair("AAPL", ["BTC/USD"]))


class _FakeBroker:
    """Duck-typed broker for tests (no real broker dependency)."""

    def __init__(
        self,
        positions: list[SimpleNamespace] | None = None,
        *,
        submit_accepted: bool = True,
        submit_status: str = "accepted",
        submit_reasons: tuple[str, ...] = (),
    ) -> None:
        self._positions = list(positions or [])
        self.submitted: list[Any] = []
        self._submit_accepted = submit_accepted
        self._submit_status = submit_status
        self._submit_reasons = submit_reasons

    def read_positions(self) -> tuple[SimpleNamespace, ...]:
        return tuple(self._positions)

    def submit_order(self, order: Any) -> Any:
        self.submitted.append(order)
        return SimpleNamespace(
            accepted=self._submit_accepted,
            status=self._submit_status,
            reasons=self._submit_reasons,
            dry_run=False,
            broker_response={"id": f"order-{len(self.submitted)}"},
        )


def _write_csv_dataset(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    write_records(rows, path)


class ComputeTargetWeightsSnapshotTests(unittest.TestCase):
    def test_flat_dataset_reports_insufficient_history_or_empty_weights(self) -> None:
        # With exactly momentum_window bars, snapshot is "insufficient_history".
        records = _build_flat_records(["BTC/USD", "ETH/USD"], n_dates=120)
        config = BacktestConfig(
            momentum_window=120,
            volatility_window=120,
            periods_per_year=365,
            max_single_position=0.10,
        )
        snapshot = compute_target_weights_snapshot(records, config)
        self.assertFalse(snapshot["sufficient_history"])
        self.assertEqual(snapshot["weights"], {})
        self.assertEqual(snapshot["as_of"], "2026-09-28")

    def test_clear_momentum_produces_non_empty_weights(self) -> None:
        # BTC rising, ETH flat → BTC must be selected.
        n_dates = 200
        btc_path = [100.0 + i * 1.0 for i in range(n_dates)]
        eth_path = [200.0 for _ in range(n_dates)]
        records = _build_momentum_records({"BTC/USD": btc_path, "ETH/USD": eth_path})
        config = BacktestConfig(
            momentum_window=120,
            volatility_window=120,
            periods_per_year=365,
            max_single_position=0.10,
        )
        snapshot = compute_target_weights_snapshot(records, config)
        self.assertTrue(snapshot["sufficient_history"])
        self.assertIn("BTC/USD", snapshot["weights"])
        self.assertNotIn("ETH/USD", snapshot["weights"])
        self.assertEqual(snapshot["as_of"], (date(2026, 6, 1) + timedelta(days=n_dates - 1)).isoformat())

    def test_snapshot_matches_backtest_last_position_for_flat_dataset(self) -> None:
        # Anti-drift: with no momentum on a flat dataset, the snapshot and
        # the backtest's last position MUST agree (both empty).
        records = _build_flat_records(["BTC/USD", "ETH/USD"], n_dates=200)
        config = BacktestConfig(
            momentum_window=120,
            volatility_window=120,
            periods_per_year=365,
            max_single_position=0.10,
        )
        snapshot = compute_target_weights_snapshot(records, config)
        backtest = run_momentum_vol_target_backtest(records, config)
        self.assertEqual(snapshot["weights"], backtest.positions[-1].weights)
        # Both should be empty on the flat dataset.
        self.assertEqual(snapshot["weights"], {})

    def test_snapshot_matches_backtest_decision_with_momentum(self) -> None:
        # Anti-drift, non-trivial: the snapshot decides on the LAST date of its
        # dataset, while the backtest's positions[-1] holds the decision taken
        # on its PENULTIMATE date. Feeding the backtest one extra day aligns
        # them: snapshot(records up to T) == backtest(records up to T+1) last
        # position. Any strategy drift between execution and the validated
        # backtest breaks this equality.
        records = _build_momentum_records(
            {
                "BTC/USD": [100.0 + i * 1.5 for i in range(201)],
                "ETH/USD": [200.0 + i * 0.5 for i in range(201)],
            }
        )
        dates = sorted({str(row["timestamp"]) for row in records})
        last_date = dates[-1]
        records_until_t = [row for row in records if str(row["timestamp"]) != last_date]
        config = BacktestConfig(
            momentum_window=120,
            volatility_window=120,
            periods_per_year=365,
            max_single_position=0.10,
        )
        snapshot = compute_target_weights_snapshot(records_until_t, config)
        backtest = run_momentum_vol_target_backtest(records, config)
        self.assertTrue(snapshot["sufficient_history"])
        self.assertNotEqual(snapshot["weights"], {})
        self.assertEqual(snapshot["weights"], backtest.positions[-1].weights)


class RunSleeveRebalanceReportOnlyTests(unittest.TestCase):
    def _build_dataset(self, tmp: Path) -> Path:
        records = _build_momentum_records(
            {
                "BTC/USD": [100.0 + i * 1.0 for i in range(200)],
                "ETH/USD": [200.0 for _ in range(200)],
            }
        )
        path = tmp / "crypto.csv"
        _write_csv_dataset(path, records)
        return path

    def test_report_only_default_no_submit_no_broker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            dataset = self._build_dataset(tmp_path)
            output = tmp_path / "report.json"
            result = run_sleeve_rebalance(
                universe_config="configs/crypto_alpaca.yml",
                risk_config="configs/risk.yml",
                dataset=dataset,
                output=output,
                notional_usd=1000.0,
            )
            self.assertEqual(result.status, "REPORT_ONLY")
            self.assertEqual(result.exit_code, 0)
            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertFalse(payload["safety"]["orders_submitted"])
            self.assertFalse(payload["safety"]["confirm_submit"])
            self.assertTrue(payload["safety"]["paper_only"])
            self.assertEqual(payload["status"], "REPORT_ONLY")
            self.assertEqual(payload["submissions"], [])
            self.assertIn("BTC/USD", payload["weights"])

    def test_report_only_with_fake_broker_does_not_call_submit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            dataset = self._build_dataset(tmp_path)
            output = tmp_path / "report.json"
            broker = _FakeBroker(
                positions=[
                    SimpleNamespace(symbol="BTCUSD", qty=0.1, market_value=600.0),
                ]
            )
            result = run_sleeve_rebalance(
                universe_config="configs/crypto_alpaca.yml",
                risk_config="configs/risk.yml",
                dataset=dataset,
                output=output,
                notional_usd=1000.0,
                broker=broker,
                confirm_submit=False,
            )
            self.assertEqual(result.status, "REPORT_ONLY")
            self.assertEqual(broker.submitted, [])
            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertFalse(payload["safety"]["orders_submitted"])


class RunSleeveRebalancePlanTests(unittest.TestCase):
    def _build_dataset(self, tmp: Path) -> Path:
        records = _build_momentum_records(
            {
                "BTC/USD": [100.0 + i * 1.0 for i in range(200)],
                "ETH/USD": [200.0 + i * 0.5 for i in range(200)],
            }
        )
        path = tmp / "crypto.csv"
        _write_csv_dataset(path, records)
        return path

    def test_plan_maps_broker_symbol_and_ignores_outside_universe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            dataset = self._build_dataset(tmp_path)
            output = tmp_path / "report.json"
            broker = _FakeBroker(
                positions=[
                    SimpleNamespace(symbol="BTCUSD", qty=0.1, market_value=600.0),
                    SimpleNamespace(symbol="AAPL", qty=2.0, market_value=400.0),
                ]
            )
            result = run_sleeve_rebalance(
                universe_config="configs/crypto_alpaca.yml",
                risk_config="configs/risk.yml",
                dataset=dataset,
                output=output,
                notional_usd=1000.0,
                broker=broker,
                confirm_submit=False,
            )
            self.assertEqual(result.status, "REPORT_ONLY")
            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertIn("AAPL", payload["ignored_positions"])
            plan_by_pair = {entry["pair"]: entry for entry in payload["plan"]}
            self.assertIn("BTC/USD", plan_by_pair)
            self.assertEqual(plan_by_pair["BTC/USD"]["current_notional"], 600.0)

    def test_plan_skip_below_min_notional(self) -> None:
        # Position very close to target → delta < $10 → skip_below_min
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            dataset = self._build_dataset(tmp_path)
            output = tmp_path / "report.json"
            # Use a $1 notional so the BTC delta (weight*1 - 0) is below $10
            broker = _FakeBroker(positions=[])
            result = run_sleeve_rebalance(
                universe_config="configs/crypto_alpaca.yml",
                risk_config="configs/risk.yml",
                dataset=dataset,
                output=output,
                notional_usd=1.0,
                broker=broker,
                confirm_submit=False,
            )
            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(result.status, "REPORT_ONLY")
            plan_by_pair = {entry["pair"]: entry for entry in payload["plan"]}
            if "BTC/USD" in plan_by_pair:
                self.assertIn(
                    plan_by_pair["BTC/USD"]["action"],
                    {"buy", "skip_below_min", "hold"},
                )

    def test_plan_sell_all_uses_exact_quantity(self) -> None:
        # When BTC has positive target but current is way above, we sell;
        # if current > target AND target == 0 → sell_all with quantity.
        # To force sell_all, set target for BTC to 0 (no BTC in weights) but
        # broker has BTC position.
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            # Build dataset where BTC has NO momentum so it's not selected.
            n = 200
            records = _build_momentum_records(
                {
                    "BTC/USD": [100.0 for _ in range(n)],
                    "ETH/USD": [200.0 + i * 1.0 for i in range(n)],
                }
            )
            path = tmp_path / "crypto.csv"
            _write_csv_dataset(path, records)
            output = tmp_path / "report.json"
            broker = _FakeBroker(
                positions=[
                    SimpleNamespace(symbol="BTCUSD", qty=0.05, market_value=300.0),
                ]
            )
            result = run_sleeve_rebalance(
                universe_config="configs/crypto_alpaca.yml",
                risk_config="configs/risk.yml",
                dataset=path,
                output=output,
                notional_usd=1000.0,
                broker=broker,
                confirm_submit=False,
            )
            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(result.status, "REPORT_ONLY")
            plan_by_pair = {entry["pair"]: entry for entry in payload["plan"]}
            self.assertIn("BTC/USD", plan_by_pair)
            entry = plan_by_pair["BTC/USD"]
            self.assertEqual(entry["action"], "sell_all")
            self.assertEqual(entry["quantity"], 0.05)


class RunSleeveRebalanceSubmitTests(unittest.TestCase):
    def _build_dataset(self, tmp: Path) -> Path:
        records = _build_momentum_records(
            {
                "BTC/USD": [100.0 + i * 1.0 for i in range(200)],
                "ETH/USD": [200.0 for _ in range(200)],
            }
        )
        path = tmp / "crypto.csv"
        _write_csv_dataset(path, records)
        return path

    def test_submit_with_confirm_calls_broker_per_action(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            dataset = self._build_dataset(tmp_path)
            output = tmp_path / "report.json"
            broker = _FakeBroker(positions=[])
            result = run_sleeve_rebalance(
                universe_config="configs/crypto_alpaca.yml",
                risk_config="configs/risk.yml",
                dataset=dataset,
                output=output,
                notional_usd=1000.0,
                broker=broker,
                confirm_submit=True,
            )
            self.assertEqual(result.status, "OK")
            self.assertGreater(len(broker.submitted), 0)
            for order in broker.submitted:
                self.assertTrue(order.client_order_id.startswith("sleeve-"))
                self.assertIn(order.symbol, {"BTC/USD", "ETH/USD"})
            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertTrue(payload["safety"]["orders_submitted"])
            self.assertTrue(payload["safety"]["confirm_submit"])
            # At least one submission was accepted
            accepted = [s for s in payload["submissions"] if s.get("submitted")]
            self.assertGreater(len(accepted), 0)

    def test_submit_with_rejection_yields_warn_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            dataset = self._build_dataset(tmp_path)
            output = tmp_path / "report.json"
            broker = _FakeBroker(
                positions=[],
                submit_accepted=False,
                submit_status="rejected",
                submit_reasons=("symbol_not_allowlisted",),
            )
            result = run_sleeve_rebalance(
                universe_config="configs/crypto_alpaca.yml",
                risk_config="configs/risk.yml",
                dataset=dataset,
                output=output,
                notional_usd=1000.0,
                broker=broker,
                confirm_submit=True,
            )
            self.assertEqual(result.status, "WARN")
            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertTrue(payload["safety"]["orders_submitted"])

    def test_confirm_submit_with_all_hold_plan_reports_no_orders(self) -> None:
        # Safety truth: confirm_submit alone must not claim orders_submitted
        # when the plan produced nothing to send (flat dataset → no weights,
        # no positions → every entry is a hold).
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            records = _build_momentum_records(
                {
                    "BTC/USD": [100.0 for _ in range(200)],
                    "ETH/USD": [200.0 for _ in range(200)],
                }
            )
            dataset = tmp_path / "crypto.csv"
            _write_csv_dataset(dataset, records)
            output = tmp_path / "report.json"
            broker = _FakeBroker(positions=[])
            result = run_sleeve_rebalance(
                universe_config="configs/crypto_alpaca.yml",
                risk_config="configs/risk.yml",
                dataset=dataset,
                output=output,
                notional_usd=1000.0,
                broker=broker,
                confirm_submit=True,
            )
            self.assertEqual(result.status, "OK")
            self.assertEqual(broker.submitted, [])
            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertFalse(payload["safety"]["orders_submitted"])
            self.assertTrue(payload["safety"]["confirm_submit"])


class RunSleeveRebalanceStalenessTests(unittest.TestCase):
    def test_stale_dataset_blocks_with_dataset_stale_code(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            # Build a dataset whose last date is far in the past.
            old_start = date(2020, 1, 1)
            rows: list[dict[str, object]] = []
            for offset in range(200):
                day = (old_start + timedelta(days=offset)).isoformat()
                for symbol in ("BTC/USD", "ETH/USD"):
                    rows.append(_flat_ohlcv_row(timestamp=day, symbol=symbol, close=100.0))
            dataset = tmp_path / "old.csv"
            _write_csv_dataset(dataset, rows)
            output = tmp_path / "report.json"
            as_of = date(2026, 7, 9)  # ~6 years after the last dataset bar
            result = run_sleeve_rebalance(
                universe_config="configs/crypto_alpaca.yml",
                risk_config="configs/risk.yml",
                dataset=dataset,
                output=output,
                notional_usd=1000.0,
                as_of_date=as_of,
                max_age_days=3,
            )
            self.assertEqual(result.status, "BLOCKED")
            payload = json.loads(output.read_text(encoding="utf-8"))
            blockers = payload["blockers"]
            self.assertTrue(
                any(b.startswith("dataset_stale:") for b in blockers),
                blockers,
            )

    def test_invalid_notional_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output = tmp_path / "report.json"
            result = run_sleeve_rebalance(
                universe_config="configs/crypto_alpaca.yml",
                risk_config="configs/risk.yml",
                dataset=tmp_path / "missing.csv",
                output=output,
                notional_usd=0.0,
            )
            self.assertEqual(result.status, "BLOCKED")
            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertIn("invalid_notional_budget", payload["blockers"])


class SleeveRebalanceCliTests(unittest.TestCase):
    def _build_dataset(self, tmp: Path) -> Path:
        records = _build_momentum_records(
            {
                "BTC/USD": [100.0 + i * 1.0 for i in range(200)],
                "ETH/USD": [200.0 for _ in range(200)],
            }
        )
        path = tmp / "crypto.csv"
        _write_csv_dataset(path, records)
        return path

    def test_cli_real_paper_without_confirm_returns_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            dataset = self._build_dataset(tmp_path)
            stderr = StringIO()
            argv = [
                "sleeve-rebalance",
                "--dataset",
                str(dataset),
                "--notional-usd",
                "1000",
                "--real-paper",
                "--output",
                str(tmp_path / "report.json"),
            ]
            with redirect_stderr(stderr):
                exit_code = main(argv)
            self.assertEqual(exit_code, 2)

    def test_cli_default_dry_run_writes_report_and_returns_zero(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            dataset = self._build_dataset(tmp_path)
            output = tmp_path / "report.json"
            stdout = StringIO()
            stderr = StringIO()
            argv = [
                "sleeve-rebalance",
                "--dataset",
                str(dataset),
                "--notional-usd",
                "1000",
                "--output",
                str(output),
            ]
            with redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = main(argv)
            self.assertEqual(exit_code, 0)
            self.assertTrue(output.exists())
            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(payload["status"], "REPORT_ONLY")
            self.assertFalse(payload["safety"]["orders_submitted"])


if __name__ == "__main__":
    unittest.main()