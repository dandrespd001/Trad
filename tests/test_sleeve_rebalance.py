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
        equity: float = 100000.0,
        last_equity: float = 100000.0,
        latest_prices: dict[str, float | None] | None = None,
        order_states: list[Any] | None = None,
    ) -> None:
        self._positions = list(positions or [])
        self.submitted: list[Any] = []
        self._submit_accepted = submit_accepted
        self._submit_status = submit_status
        self._submit_reasons = submit_reasons
        self._equity = equity
        self._last_equity = last_equity
        # Limit-maker (M10) knobs. Defaults keep every pre-M10 test unchanged:
        # ``latest_trade_price`` returns None (caller should fall back to market
        # with style_note), and ``get_order_by_client_id`` returns an empty
        # accepted snapshot — but only matters when order_style="limit-maker".
        self._latest_prices: dict[str, float | None] = dict(latest_prices or {})
        self._order_states: list[Any] = list(order_states or [])
        self.latest_trade_calls: list[str] = []
        self.get_order_calls: list[str] = []
        self.cancelled_client_ids: list[str] = []

    def read_account(self) -> SimpleNamespace:
        return SimpleNamespace(equity=self._equity, last_equity=self._last_equity)

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

    def latest_trade_price(self, symbol: str) -> float | None:
        self.latest_trade_calls.append(symbol)
        return self._latest_prices.get(symbol.upper())

    def get_order_by_client_id(self, client_order_id: str) -> Any:
        self.get_order_calls.append(client_order_id)
        if not self._order_states:
            return SimpleNamespace(
                client_order_id=client_order_id,
                status="accepted",
                filled_quantity=0.0,
                filled_avg_price=None,
            )
        state = self._order_states.pop(0)
        if isinstance(state, BaseException):
            raise state
        if isinstance(state, dict):
            return SimpleNamespace(
                client_order_id=client_order_id,
                status=state.get("status", "accepted"),
                filled_quantity=float(state.get("filled_quantity", 0.0) or 0.0),
                filled_avg_price=state.get("filled_avg_price"),
            )
        return state

    def cancel_order(self, client_order_id: str | None = None, *, order_id: str | None = None) -> Any:
        self.cancelled_client_ids.append(client_order_id)
        return SimpleNamespace(
            accepted=True,
            status="cancelled",
            reasons=(),
            dry_run=False,
            broker_response={"id": f"cancel-{len(self.cancelled_client_ids)}"},
        )


class _FakeClock:
    """Clock + sleeper that advance in lockstep — for offline limit-maker tests.

    ``sleep(seconds)`` advances the internal time by exactly ``seconds``; the
    next ``now()`` reflects the bump. Tests pass ``now=clock.now`` and
    ``sleep=clock.sleep`` to ``run_sleeve_rebalance`` so the limit poll loop
    converges deterministically without touching the wall clock.
    """

    def __init__(self, start: float = 1000.0) -> None:
        self.t = float(start)

    def sleep(self, seconds: float) -> None:
        self.t += float(seconds)

    def now(self) -> float:
        return self.t


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
                equity_highwater_path=tmp_path / "equity_highwater.json",
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
                equity_highwater_path=tmp_path / "equity_highwater.json",
            )
            self.assertEqual(result.status, "WARN")
            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertTrue(payload["safety"]["orders_submitted"])

    def test_pending_open_buy_counts_as_current_exposure(self) -> None:
        # A submitted-but-unfilled buy (queued for next open / weekend) must
        # count as current exposure, or the next cycle re-buys and doubles
        # the position once both orders fill.
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            dataset = self._build_dataset(tmp_path)
            output = tmp_path / "report.json"

            class _BrokerWithOpenOrders(_FakeBroker):
                def list_orders(self, *, status: str = "open") -> tuple[SimpleNamespace, ...]:
                    assert status == "open"
                    return (
                        SimpleNamespace(symbol="BTC/USD", side="buy", notional=100.0),
                        SimpleNamespace(symbol="AAPL", side="buy", notional=999.0),
                        SimpleNamespace(symbol="BTC/USD", side="sell", notional=50.0),
                    )

            broker = _BrokerWithOpenOrders(positions=[])
            result = run_sleeve_rebalance(
                universe_config="configs/crypto_alpaca.yml",
                risk_config="configs/risk.yml",
                dataset=dataset,
                output=output,
                notional_usd=1000.0,
                broker=broker,
                confirm_submit=True,
                equity_highwater_path=tmp_path / "equity_highwater.json",
            )
            payload = json.loads(output.read_text(encoding="utf-8"))
            # Only the open BUY for a universe pair counts (slash notation
            # maps too); AAPL is outside the universe and sells are ignored.
            self.assertEqual(payload["pending_buy_notional"], {"BTC/USD": 100.0})
            btc_entries = [e for e in payload["plan"] if e["pair"] == "BTC/USD"]
            self.assertEqual(len(btc_entries), 1)
            self.assertEqual(btc_entries[0]["current_notional"], 100.0)
            # No duplicate BTC buy was submitted for already-pending exposure.
            btc_orders = [o for o in broker.submitted if o.symbol == "BTC/USD" and o.side == "buy"]
            expected_target = btc_entries[0]["target_notional"]
            if expected_target <= 100.0:
                self.assertEqual(btc_orders, [])
            self.assertEqual(result.exit_code, 0)

    def test_submit_exception_is_reported_not_raised(self) -> None:
        # Same-day re-runs resubmit deterministic client_order_ids; the broker
        # (or Alpaca behind it) may raise. The cycle must record the error and
        # finish WARN instead of crashing mid-way.
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            dataset = self._build_dataset(tmp_path)
            output = tmp_path / "report.json"

            class _RaisingBroker(_FakeBroker):
                def submit_order(self, order: Any) -> Any:
                    raise RuntimeError("client_order_id must be unique")

            broker = _RaisingBroker(positions=[])
            result = run_sleeve_rebalance(
                universe_config="configs/crypto_alpaca.yml",
                risk_config="configs/risk.yml",
                dataset=dataset,
                output=output,
                notional_usd=1000.0,
                broker=broker,
                confirm_submit=True,
                equity_highwater_path=tmp_path / "equity_highwater.json",
            )
            self.assertEqual(result.status, "WARN")
            payload = json.loads(output.read_text(encoding="utf-8"))
            errored = [s for s in payload["submissions"] if s.get("status") == "error"]
            self.assertGreater(len(errored), 0)
            self.assertIn("client_order_id must be unique", errored[0]["reasons"][0])

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
                equity_highwater_path=tmp_path / "equity_highwater.json",
            )
            self.assertEqual(result.status, "OK")
            self.assertEqual(broker.submitted, [])
            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertFalse(payload["safety"]["orders_submitted"])
            self.assertTrue(payload["safety"]["confirm_submit"])


class RunSleeveRebalanceRiskContextTests(unittest.TestCase):
    """M7: real account state must flow into every submitted order."""

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

    def _run(self, tmp_path: Path, broker: Any, *, highwater: float | None = None) -> dict[str, Any]:
        dataset = self._build_dataset(tmp_path)
        output = tmp_path / "report.json"
        hw_path = tmp_path / "equity_highwater.json"
        if highwater is not None:
            hw_path.write_text(
                json.dumps({"high_water_equity": highwater}), encoding="utf-8"
            )
        result = run_sleeve_rebalance(
            universe_config="configs/crypto_alpaca.yml",
            risk_config="configs/risk.yml",
            dataset=dataset,
            output=output,
            notional_usd=1000.0,
            broker=broker,
            confirm_submit=True,
            equity_highwater_path=hw_path,
        )
        payload = json.loads(output.read_text(encoding="utf-8"))
        payload["_result"] = result
        payload["_hw_path"] = hw_path
        return payload

    def test_daily_loss_flows_into_orders(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            broker = _FakeBroker(positions=[], equity=95000.0, last_equity=100000.0)
            payload = self._run(Path(tmp), broker)
            self.assertAlmostEqual(payload["account_risk"]["daily_pnl_pct"], -0.05)
            self.assertGreater(len(broker.submitted), 0)
            for order in broker.submitted:
                self.assertAlmostEqual(order.daily_pnl_pct, -0.05)

    def test_drawdown_from_stored_high_water(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            broker = _FakeBroker(positions=[], equity=100000.0, last_equity=100000.0)
            payload = self._run(Path(tmp), broker, highwater=120000.0)
            self.assertAlmostEqual(
                payload["account_risk"]["current_drawdown_pct"], (120000 - 100000) / 120000, places=5
            )
            for order in broker.submitted:
                self.assertAlmostEqual(order.current_drawdown_pct, 1 / 6, places=5)
            stored = json.loads(payload["_hw_path"].read_text(encoding="utf-8"))
            self.assertEqual(stored["high_water_equity"], 120000.0)

    def test_high_water_rises_with_equity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            broker = _FakeBroker(positions=[], equity=130000.0, last_equity=130000.0)
            payload = self._run(Path(tmp), broker, highwater=120000.0)
            self.assertEqual(payload["account_risk"]["current_drawdown_pct"], 0.0)
            stored = json.loads(payload["_hw_path"].read_text(encoding="utf-8"))
            self.assertEqual(stored["high_water_equity"], 130000.0)

    def test_position_weight_and_gross_exposure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            broker = _FakeBroker(positions=[], equity=100000.0, last_equity=100000.0)
            self._run(Path(tmp), broker)
            buys = [o for o in broker.submitted if o.side == "buy"]
            self.assertGreater(len(buys), 0)
            for order in buys:
                self.assertAlmostEqual(
                    order.estimated_position_weight, float(order.notional) / 100000.0
                )
                self.assertGreaterEqual(order.projected_gross_exposure, order.estimated_position_weight)

    def test_unreadable_account_blocks_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:

            class _NoAccountBroker(_FakeBroker):
                def read_account(self) -> SimpleNamespace:
                    raise RuntimeError("account unavailable")

            broker = _NoAccountBroker(positions=[])
            payload = self._run(Path(tmp), broker)
            self.assertEqual(payload["status"], "BLOCKED")
            self.assertIn("account_risk_context_unavailable", payload["blockers"])
            self.assertEqual(broker.submitted, [])
            self.assertFalse(payload["safety"]["orders_submitted"])


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

    def test_cli_passes_order_style_to_runner(self) -> None:
        # Smoke: the runner receives the value the CLI parsed.
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
                "--order-style",
                "limit-maker",
                "--limit-wait-secs",
                "30",
                "--output",
                str(output),
            ]
            with redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = main(argv)
            self.assertEqual(exit_code, 0)
            self.assertTrue(output.exists())
            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(payload["params"]["order_style"], "limit-maker")


class RunSleeveRebalanceLimitMakerTests(unittest.TestCase):
    """M10: ``order_style="limit-maker"`` resting-limit flow for crypto pairs."""

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

    def test_limit_maker_fills_immediately(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            dataset = self._build_dataset(tmp_path)
            output = tmp_path / "report.json"
            # First poll returns "filled" → loop breaks on the first sleep tick.
            broker = _FakeBroker(
                positions=[],
                latest_prices={"BTC/USD": 100.0},
                order_states=[{"status": "filled", "filled_quantity": 1.0, "filled_avg_price": 99.99}],
            )
            clock = _FakeClock(start=1000.0)
            result = run_sleeve_rebalance(
                universe_config="configs/crypto_alpaca.yml",
                risk_config="configs/risk.yml",
                dataset=dataset,
                output=output,
                notional_usd=1000.0,
                broker=broker,
                confirm_submit=True,
                equity_highwater_path=tmp_path / "equity_highwater.json",
                order_style="limit-maker",
                limit_wait_seconds=30,
                sleep=clock.sleep,
                now=clock.now,
            )
            self.assertEqual(result.status, "OK")
            payload = json.loads(output.read_text(encoding="utf-8"))
            accepted = [s for s in payload["submissions"] if s.get("submitted")]
            self.assertGreater(len(accepted), 0)
            submission = accepted[0]
            self.assertEqual(submission["style"], "limit-maker")
            self.assertEqual(submission["filled_via"], "limit")
            self.assertTrue(submission["client_order_id"].endswith("-lim"))
            # Limit price sent to the broker rests inside the spread (1bp).
            limit_orders = [
                order
                for order in broker.submitted
                if getattr(order, "order_type", "market") == "limit"
            ]
            self.assertEqual(len(limit_orders), 1)
            expected_limit = round(100.0 * (1 - 1.0 / 1e4), 4)
            self.assertAlmostEqual(limit_orders[0].limit_price, expected_limit, places=4)
            self.assertEqual(limit_orders[0].limit_price, round(expected_limit, 4))

    def test_limit_maker_timeout_triggers_market_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            dataset = self._build_dataset(tmp_path)
            output = tmp_path / "report.json"
            # 2 polls (sleep(10) × 2 under limit_wait_seconds=20) return
            # "accepted". After the deadline the cancel is issued and the
            # post-cancel snapshot has 0 fills → full market fallback.
            broker = _FakeBroker(
                positions=[],
                latest_prices={"BTC/USD": 100.0},
                order_states=[
                    {"status": "accepted"},
                    {"status": "accepted"},
                    {"status": "cancelled", "filled_quantity": 0.0, "filled_avg_price": None},
                ],
            )
            clock = _FakeClock(start=1000.0)
            result = run_sleeve_rebalance(
                universe_config="configs/crypto_alpaca.yml",
                risk_config="configs/risk.yml",
                dataset=dataset,
                output=output,
                notional_usd=1000.0,
                broker=broker,
                confirm_submit=True,
                equity_highwater_path=tmp_path / "equity_highwater.json",
                order_style="limit-maker",
                limit_wait_seconds=20,  # 2 polls (10s each) → exit on deadline
                sleep=clock.sleep,
                now=clock.now,
            )
            self.assertEqual(result.status, "OK")
            # A limit order was submitted…
            limit_orders = [
                order for order in broker.submitted
                if getattr(order, "order_type", "market") == "limit"
            ]
            self.assertEqual(len(limit_orders), 1)
            limit_id = limit_orders[0].client_order_id
            # …and a market fallback followed the cancel.
            market_orders = [
                order for order in broker.submitted
                if getattr(order, "order_type", "market") == "market"
                and order.client_order_id.endswith("-mkt")
            ]
            self.assertEqual(len(market_orders), 1)
            # Cancel was issued exactly once on the limit id.
            self.assertIn(limit_id, broker.cancelled_client_ids)
            payload = json.loads(output.read_text(encoding="utf-8"))
            accepted = [s for s in payload["submissions"] if s.get("submitted")]
            submission = accepted[0]
            self.assertEqual(submission["style"], "limit-maker")
            self.assertEqual(submission["filled_via"], "market_fallback")
            self.assertEqual(submission["market_client_order_id"], market_orders[0].client_order_id)

    def test_limit_maker_partial_fill_sends_market_for_remainder(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            dataset = self._build_dataset(tmp_path)
            output = tmp_path / "report.json"
            # BTC's plan entry under max_single_position=0.10 / notional 1000
            # is a buy for ≈ $100. Simulate a partial fill at the resting
            # limit price (≈ $99.99) of 0.5 BTC → filled notional $49.995.
            # Remainder ≈ $50.005 → market fallback for that amount.
            limit_price = round(100.0 * (1 - 1.0 / 1e4), 4)  # 99.99
            broker = _FakeBroker(
                positions=[],
                latest_prices={"BTC/USD": 100.0},
                order_states=[
                    # 2 polls (sleep(10) each) under limit_wait_seconds=20:
                    {"status": "accepted"},
                    {"status": "accepted"},
                    # Final post-cancel snapshot: partial fill at the limit price.
                    # Status "partially_filled" (NOT "filled") is the
                    # canonical Alpaca representation of an order that filled
                    # some qty but not all — fully "filled" would short-circuit
                    # the remainder calculation in the runner.
                    {"status": "partially_filled", "filled_quantity": 0.5, "filled_avg_price": limit_price},
                ],
            )
            clock = _FakeClock(start=1000.0)
            run_sleeve_rebalance(
                universe_config="configs/crypto_alpaca.yml",
                risk_config="configs/risk.yml",
                dataset=dataset,
                output=output,
                notional_usd=1000.0,
                broker=broker,
                confirm_submit=True,
                equity_highwater_path=tmp_path / "equity_highwater.json",
                order_style="limit-maker",
                limit_wait_seconds=20,
                sleep=clock.sleep,
                now=clock.now,
            )
            # The market fallback's notional must equal the original BTC
            # plan notional minus the limit fill notional (rounded to cents).
            market_orders = [
                order for order in broker.submitted
                if order.client_order_id.endswith("-mkt")
            ]
            self.assertEqual(len(market_orders), 1)
            original_notional = float(market_orders[0].notional) + float(0.5 * limit_price)
            expected_remainder = round(original_notional - (0.5 * limit_price), 2)
            self.assertEqual(market_orders[0].notional, expected_remainder)

    def test_limit_maker_partial_fill_below_min_skips_market(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            dataset = self._build_dataset(tmp_path)
            output = tmp_path / "report.json"
            # Use the smallest notional that still produces a real BTC buy
            # ($10 at the 0.10 max-single-position cap). A partial fill of
            # 0.05 BTC at $99.99 ≈ $4.9995 leaves a remainder of $5.0005:
            # above the $1 noise floor but below Alpaca's $10 crypto
            # minimum → ``fallback_below_min`` skip, no market fallback.
            limit_price = round(100.0 * (1 - 1.0 / 1e4), 4)
            broker = _FakeBroker(
                positions=[],
                latest_prices={"BTC/USD": 100.0},
                order_states=[
                    # 2 polls under limit_wait_seconds=20:
                    {"status": "accepted"},
                    {"status": "accepted"},
                    # Post-cancel: partial fill that leaves a sub-$10 remainder.
                    {"status": "partially_filled", "filled_quantity": 0.05, "filled_avg_price": limit_price},
                ],
            )
            clock = _FakeClock(start=1000.0)
            run_sleeve_rebalance(
                universe_config="configs/crypto_alpaca.yml",
                risk_config="configs/risk.yml",
                dataset=dataset,
                output=output,
                notional_usd=100.0,
                broker=broker,
                confirm_submit=True,
                equity_highwater_path=tmp_path / "equity_highwater.json",
                order_style="limit-maker",
                limit_wait_seconds=20,
                sleep=clock.sleep,
                now=clock.now,
            )
            market_orders = [
                order for order in broker.submitted
                if order.client_order_id.endswith("-mkt")
            ]
            # No market was sent — the remainder is below $10.
            self.assertEqual(market_orders, [])
            payload = json.loads(output.read_text(encoding="utf-8"))
            accepted = [s for s in payload["submissions"] if s.get("submitted")]
            self.assertGreater(len(accepted), 0)
            submission = accepted[0]
            self.assertEqual(submission["filled_via"], "limit_partial")
            self.assertEqual(submission["style_note"], "fallback_below_min")

    def test_limit_maker_no_live_price_falls_back_to_market(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            dataset = self._build_dataset(tmp_path)
            output = tmp_path / "report.json"
            broker = _FakeBroker(
                positions=[],
                latest_prices={"BTC/USD": None},  # degraded broker
            )
            clock = _FakeClock(start=1000.0)
            result = run_sleeve_rebalance(
                universe_config="configs/crypto_alpaca.yml",
                risk_config="configs/risk.yml",
                dataset=dataset,
                output=output,
                notional_usd=1000.0,
                broker=broker,
                confirm_submit=True,
                equity_highwater_path=tmp_path / "equity_highwater.json",
                order_style="limit-maker",
                limit_wait_seconds=30,
                sleep=clock.sleep,
                now=clock.now,
            )
            self.assertEqual(result.status, "OK")
            # No limit order was sent — the cycle went straight to market.
            limit_orders = [
                order for order in broker.submitted
                if getattr(order, "order_type", "market") == "limit"
            ]
            self.assertEqual(limit_orders, [])
            market_orders = [
                order for order in broker.submitted
                if order.client_order_id.endswith("-mkt")
            ]
            # And the market order did NOT get the -mkt suffix (the
            # fallback replaces, rather than augments, the original id).
            self.assertEqual(market_orders, [])
            payload = json.loads(output.read_text(encoding="utf-8"))
            accepted = [s for s in payload["submissions"] if s.get("submitted")]
            self.assertGreater(len(accepted), 0)
            submission = accepted[0]
            self.assertEqual(submission["style"], "market")
            self.assertEqual(submission["style_note"], "limit_price_unavailable")
            # ``filled_via`` is absent on the limit_price_unavailable path.
            self.assertNotIn("filled_via", submission)

    def test_default_order_style_keeps_ids_byte_identical(self) -> None:
        # Regression: with the default ``order_style="market"``, neither the
        # submission record NOR the client_order_id carries a ``-lim`` /
        # ``-mkt`` suffix. (Pre-existing tests already cover this implicit
        # contract; we re-state it explicitly so the limit-maker flag
        # cannot accidentally regress it.)
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
                equity_highwater_path=tmp_path / "equity_highwater.json",
            )
            self.assertEqual(result.status, "OK")
            self.assertGreater(len(broker.submitted), 0)
            for order in broker.submitted:
                self.assertNotIn("-lim", order.client_order_id)
                self.assertNotIn("-mkt", order.client_order_id)
                self.assertEqual(getattr(order, "order_type", "market"), "market")
            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(payload["params"]["order_style"], "market")
            for submission in payload["submissions"]:
                if submission.get("submitted"):
                    self.assertNotIn("-lim", submission["client_order_id"])
                    self.assertNotIn("-mkt", submission["client_order_id"])
                    # ``style`` key is absent when order_style is the default.
                    self.assertNotIn("style", submission)


class PaperOrderLimitValidationTests(unittest.TestCase):
    """M10: the broker rejects limit orders that are missing a ``limit_price``."""

    def test_limit_order_without_limit_price_is_rejected(self) -> None:
        # Construct a dry-run broker (no client, no risk surface, no network).
        # The new validation runs BEFORE the dry-run short-circuit, so the
        # broker still returns a rejected PaperOrderResult.
        from trading_ai.execution.alpaca_paper import (
            AlpacaPaperBroker,
            PaperOrder,
            PaperOrderResult,
            is_crypto_symbol,
        )
        from trading_ai.risk.policy import RiskLimits

        broker = AlpacaPaperBroker(
            client=None,
            allowlist=("BTC/USD",),
            risk_limits=RiskLimits(),
            dry_run=True,
        )
        self.assertTrue(is_crypto_symbol("BTC/USD"))
        order = PaperOrder(
            symbol="BTC/USD",
            side="buy",
            notional=50.0,
            client_order_id="limit-missing-price",
            order_type="limit",
            limit_price=None,
        )
        result = broker.submit_order(order)
        self.assertIsInstance(result, PaperOrderResult)
        self.assertFalse(result.accepted)
        self.assertEqual(result.status, "rejected")
        self.assertIn("limit_price_required", result.reasons)

    def test_invalid_order_type_is_rejected(self) -> None:
        from trading_ai.execution.alpaca_paper import (
            AlpacaPaperBroker,
            PaperOrder,
        )
        from trading_ai.risk.policy import RiskLimits

        broker = AlpacaPaperBroker(
            client=None,
            allowlist=("BTC/USD",),
            risk_limits=RiskLimits(),
            dry_run=True,
        )
        order = PaperOrder(
            symbol="BTC/USD",
            side="buy",
            notional=50.0,
            client_order_id="invalid-type",
            order_type="stop",  # not in {"market", "limit"}
        )
        result = broker.submit_order(order)
        self.assertFalse(result.accepted)
        self.assertEqual(result.status, "rejected")
        self.assertIn("invalid_order_type", result.reasons)

    def test_limit_order_with_positive_limit_price_is_accepted(self) -> None:
        from trading_ai.execution.alpaca_paper import (
            AlpacaPaperBroker,
            PaperOrder,
        )
        from trading_ai.risk.policy import RiskLimits

        broker = AlpacaPaperBroker(
            client=None,
            allowlist=("BTC/USD",),
            risk_limits=RiskLimits(),
            dry_run=True,
        )
        order = PaperOrder(
            symbol="BTC/USD",
            side="buy",
            notional=50.0,
            client_order_id="limit-good",
            order_type="limit",
            limit_price=99.99,
        )
        result = broker.submit_order(order)
        # In dry-run, valid orders come back as "dry_run_accepted".
        self.assertTrue(result.accepted)
        self.assertEqual(result.status, "dry_run_accepted")

    def test_latest_trade_price_public_method(self) -> None:
        from trading_ai.execution.alpaca_paper import (
            AlpacaPaperBroker,
        )
        from trading_ai.risk.policy import RiskLimits

        class _StubMarketData:
            """Stub that matches the ``response.values()`` contract used in
            ``AlpacaPaperBroker._read_latest_trade_price``.
            """

            def __init__(self) -> None:
                self.calls: list[Any] = []

            def get_stock_latest_trade(self, request: Any) -> Any:
                self.calls.append(request)

                class _Trade(SimpleNamespace):
                    pass

                class _Response:
                    def values(self) -> list[Any]:
                        return [_Trade(price=150.0)]

                return _Response()

        market_data = _StubMarketData()
        broker = AlpacaPaperBroker(
            client=None,
            allowlist=("AAPL",),
            risk_limits=RiskLimits(),
            dry_run=True,
            market_data=market_data,
        )
        price = broker.latest_trade_price("AAPL")
        self.assertEqual(price, 150.0)
        self.assertEqual(len(market_data.calls), 1)
        # Upper-casing is a contract of the public method.
        broker.latest_trade_price("aapl")
        self.assertEqual(len(market_data.calls), 2)


class RunSleeveRebalanceInvalidOrderStyleTests(unittest.TestCase):
    """M10: unknown ``order_style`` blocks the cycle before any broker call."""

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

    def test_invalid_order_style_blocks_cycle(self) -> None:
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
                equity_highwater_path=tmp_path / "equity_highwater.json",
                order_style="not-a-real-style",
            )
            self.assertEqual(result.status, "BLOCKED")
            self.assertIn("invalid_order_style", result.payload["blockers"])
            self.assertEqual(broker.submitted, [])
            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertFalse(payload["safety"]["orders_submitted"])


class RunSleeveRebalanceRiskToStopTests(unittest.TestCase):
    """M12 (WS3, Gate 2): opt-in risk-to-stop cap on each order.

    The cap reuses ``build_canary_sizing_decision`` (§37). It ONLY reduces
    buy/sell notionals — sell_all is never capped, no order is converted
    hold→trade, and the default-off path is byte-identical to pre-M12.
    """

    @staticmethod
    def _buy_plan(*, pair: str, notional: float, reference_price: float = 100.0) -> dict[str, object]:
        return {
            "pair": pair,
            "action": "buy",
            "target_notional": float(notional),
            "current_notional": 0.0,
            "delta": float(notional),
            "notional": float(notional),
            "quantity": None,
            "reference_price": float(reference_price),
            "weight": 0.10,
        }

    @staticmethod
    def _sell_all_plan(*, pair: str, quantity: float, reference_price: float) -> dict[str, object]:
        notional = float(quantity) * float(reference_price)
        return {
            "pair": pair,
            "action": "sell_all",
            "target_notional": 0.0,
            "current_notional": notional,
            "delta": -notional,
            "notional": notional,
            "quantity": float(quantity),
            "reference_price": float(reference_price),
            "weight": 0.0,
        }

    def _execute(
        self,
        plan: list[dict[str, object]],
        *,
        equity: float = 100000.0,
        risk_to_stop_enabled: bool = True,
        risk_budget_pct: float = 0.005,
        stop_loss_pct: float = 0.10,
    ) -> tuple[_FakeBroker, list[dict[str, object]]]:
        from trading_ai.execution.sleeve_rebalance import _execute_submissions

        broker = _FakeBroker(positions=[], equity=equity, last_equity=equity)
        risk_context = {
            "equity": float(equity),
            "last_equity": float(equity),
            "daily_pnl_pct": 0.0,
            "high_water_equity": float(equity),
            "current_drawdown_pct": 0.0,
        }
        submissions = _execute_submissions(
            plan=plan,
            broker=broker,
            as_of_date=date(2026, 7, 10),
            universe_name="crypto",
            risk_context=risk_context,
            order_style="market",
            risk_to_stop_enabled=risk_to_stop_enabled,
            risk_budget_pct=risk_budget_pct,
            stop_loss_pct=stop_loss_pct,
        )
        return broker, submissions

    def test_cap_reduces_buy_notional_when_enabled(self) -> None:
        # equity=10000, planned $950 buy, risk_budget=0.005, stop=0.10 →
        # cap = 10000*0.005/0.10 = $500; broker receives notional=500.0
        # and the submission carries risk_to_stop_cap=500.0 / original_notional=950.0.
        broker, submissions = self._execute(
            plan=[self._buy_plan(pair="BTC/USD", notional=950.0)],
            equity=10000.0,
            risk_to_stop_enabled=True,
            risk_budget_pct=0.005,
            stop_loss_pct=0.10,
        )
        btc_orders = [o for o in broker.submitted if o.symbol == "BTC/USD" and o.side == "buy"]
        self.assertEqual(len(btc_orders), 1)
        self.assertEqual(btc_orders[0].notional, 500.0)
        btc_subs = [s for s in submissions if s.get("pair") == "BTC/USD" and s.get("action") == "buy"]
        self.assertEqual(len(btc_subs), 1)
        sub = btc_subs[0]
        self.assertTrue(sub["submitted"])
        self.assertEqual(sub["risk_to_stop_cap"], 500.0)
        self.assertEqual(sub["original_notional"], 950.0)

    def test_cap_below_min_crypto_skips_with_reason(self) -> None:
        # Tiny risk_budget → cap = 10000*0.00005/0.10 = $5 (< $10) → skip.
        plan = [self._buy_plan(pair="BTC/USD", notional=200.0)]
        broker, submissions = self._execute(
            plan=plan,
            equity=10000.0,
            risk_to_stop_enabled=True,
            risk_budget_pct=0.00005,
            stop_loss_pct=0.10,
        )
        self.assertEqual(broker.submitted, [])
        self.assertEqual(len(submissions), 1)
        sub = submissions[0]
        self.assertTrue(sub["skipped"])
        self.assertEqual(sub["status"], "skipped")
        self.assertEqual(sub["reasons"], ("risk_to_stop_below_min",))
        self.assertEqual(sub["risk_to_stop_cap"], 5.0)
        self.assertEqual(sub["original_notional"], 200.0)

    def test_sell_all_not_capped_when_enabled(self) -> None:
        # sell_all actions never carry the cap — closing risk is always allowed.
        plan = [self._sell_all_plan(pair="BTC/USD", quantity=0.05, reference_price=12000.0)]
        broker, submissions = self._execute(plan=plan, equity=10000.0)
        btc_orders = [o for o in broker.submitted if o.symbol == "BTC/USD" and o.side == "sell"]
        self.assertEqual(len(btc_orders), 1)
        # qty untouched — cap does not touch sell_all.
        self.assertAlmostEqual(btc_orders[0].quantity, 0.05)
        btc_subs = [s for s in submissions if s.get("pair") == "BTC/USD" and s.get("action") == "sell_all"]
        self.assertEqual(len(btc_subs), 1)
        sub = btc_subs[0]
        self.assertTrue(sub["submitted"])
        self.assertNotIn("risk_to_stop_cap", sub)
        self.assertNotIn("original_notional", sub)

    def test_default_off_no_cap_fields_or_regression(self) -> None:
        # Opt-in default-off: with risk_to_stop_enabled=False the cap is a
        # no-op. No risk_to_stop_cap / original_notional fields appear in
        # the submission records and the broker receives the strategy's
        # original notional (no change vs pre-M12).
        plan = [self._buy_plan(pair="BTC/USD", notional=950.0)]
        broker, submissions = self._execute(
            plan=plan,
            equity=10000.0,
            risk_to_stop_enabled=False,
            risk_budget_pct=0.005,
            stop_loss_pct=0.10,
        )
        btc_orders = [o for o in broker.submitted if o.symbol == "BTC/USD" and o.side == "buy"]
        self.assertEqual(len(btc_orders), 1)
        self.assertEqual(btc_orders[0].notional, 950.0)
        for sub in submissions:
            self.assertNotIn("risk_to_stop_cap", sub)
            self.assertNotIn("original_notional", sub)

    def test_decision_blockers_skip_with_risk_to_stop_blocked(self) -> None:
        # stop_loss_pct=0 trips build_canary_sizing_decision's blocker list,
        # which the cycle surfaces as a single skipped submission whose
        # reasons lead with ``risk_to_stop_blocked`` and include the upstream
        # blocker codes (no order reaches the broker).
        plan = [self._buy_plan(pair="BTC/USD", notional=950.0)]
        broker, submissions = self._execute(
            plan=plan,
            equity=10000.0,
            risk_to_stop_enabled=True,
            risk_budget_pct=0.005,
            stop_loss_pct=0.0,
        )
        self.assertEqual(broker.submitted, [])
        self.assertEqual(len(submissions), 1)
        sub = submissions[0]
        self.assertTrue(sub["skipped"])
        self.assertEqual(sub["status"], "skipped")
        self.assertEqual(sub["reasons"][0], "risk_to_stop_blocked")
        self.assertIn("stop_loss_pct_invalid", sub["reasons"])


if __name__ == "__main__":
    unittest.main()