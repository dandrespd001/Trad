"""Tests for the governed crypto-sleeve rebalance cycle (Sprint M3)."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, date, datetime, timedelta
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

import trading_ai.cli as cli_module
from trading_ai.backtest.engine import (
    BacktestConfig,
    compute_target_weights_snapshot,
    run_momentum_vol_target_backtest,
)
from trading_ai.cli import main
from trading_ai.data.io import write_records
from trading_ai.execution.paper_executor_ipc import (
    ExecutorTarget,
    PaperExecutorOutcomeUnknownError,
)
from trading_ai.execution.sleeve_rebalance import (
    EXECUTOR_MODE_BLOCKED,
    EXECUTOR_MODE_REDUCE_ONLY,
    map_broker_symbol_to_pair,
    run_sleeve_rebalance,
)

CRYPTO_UNIVERSE_SYMBOLS = (
    "BTC/USD",
    "ETH/USD",
    "LTC/USD",
    "BCH/USD",
    "DOGE/USD",
    "XRP/USD",
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
        open_orders: list[Any] | None = None,
        cancel_accepted: bool = True,
        cancel_status: str = "cancelled",
        cancel_error: BaseException | None = None,
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
        self._open_orders: list[Any] = list(open_orders or [])
        self._cancel_accepted = cancel_accepted
        self._cancel_status = cancel_status
        self._cancel_error = cancel_error
        self.list_orders_calls = 0

    def read_account(self) -> SimpleNamespace:
        return SimpleNamespace(equity=self._equity, last_equity=self._last_equity)

    def read_positions(self) -> tuple[SimpleNamespace, ...]:
        return tuple(self._positions)

    def list_orders(self, *, status: str = "open") -> tuple[Any, ...]:
        """Return an explicit, complete empty-or-populated snapshot by default."""
        self.list_orders_calls += 1
        if status != "open":
            raise AssertionError(f"unexpected order status: {status}")
        return tuple(self._open_orders)

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
        if self._cancel_error is not None:
            raise self._cancel_error
        return SimpleNamespace(
            accepted=self._cancel_accepted,
            status=self._cancel_status,
            reasons=() if self._cancel_accepted else ("cancel_not_accepted",),
            dry_run=False,
            broker_response={"id": f"cancel-{len(self.cancelled_client_ids)}"},
        )


_EXECUTOR_TARGET = ExecutorTarget(
    account_scope_sha256="a" * 64,
    policy_sha256="b" * 64,
    authz_policy_sha256="c" * 64,
    run_id="1" * 32,
    fence_epoch=7,
)


class _ReceiptExecutorBroker(_FakeBroker):
    """Executor-shaped fake whose mutations return fenced receipts."""

    def submit_order_with_receipt(self, order: Any) -> Any:
        result = super().submit_order(order)
        return SimpleNamespace(
            request_id="d" * 32,
            operation="submit_order",
            outcome="completed" if result.accepted else "rejected",
            target=_EXECUTOR_TARGET,
            result=result,
        )


class _OutcomeUnknownExecutorBroker(_FakeBroker):
    def __init__(self, positions: list[SimpleNamespace]) -> None:
        super().__init__(positions=positions)
        self.submit_attempts = 0

    def submit_order_with_receipt(self, _order: Any) -> Any:
        self.submit_attempts += 1
        raise PaperExecutorOutcomeUnknownError(
            "response unavailable after dispatch",
            request_id="e" * 32,
            operation="submit_order",
            phase="receive",
            target=_EXECUTOR_TARGET,
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


def _prepare_governed_crypto_rows(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    """Make ordinary rebalance fixtures complete and non-future.

    The production gate now requires every configured crypto symbol on the
    decision date.  Most historical tests only care about BTC/ETH behavior,
    so the other governed symbols receive flat bars.  Fixtures that exercise
    missing/stale symbols write their rows directly and bypass this helper.
    """

    prepared = [dict(row) for row in rows]
    observed_days = sorted(
        {
            date.fromisoformat(str(row["timestamp"])[:10])
            for row in prepared
            if row.get("timestamp")
        }
    )
    if not observed_days:
        return prepared

    # Old fixtures were authored with dates beyond the test run's as-of date.
    # Shift the whole path, preserving intervals, so the latest bar is today.
    shift_days = max((observed_days[-1] - date.today()).days, 0)
    if shift_days:
        for row in prepared:
            original = date.fromisoformat(str(row["timestamp"])[:10])
            row["timestamp"] = (original - timedelta(days=shift_days)).isoformat()

    by_day_symbol = {
        (str(row["timestamp"])[:10], str(row["symbol"]).upper())
        for row in prepared
    }
    days = sorted({str(row["timestamp"])[:10] for row in prepared})
    for day in days:
        for symbol in CRYPTO_UNIVERSE_SYMBOLS:
            if (day, symbol) not in by_day_symbol:
                prepared.append(_flat_ohlcv_row(timestamp=day, symbol=symbol, close=100.0))
    prepared.sort(key=lambda row: (str(row["timestamp"]), str(row["symbol"])))
    return prepared


def _write_valid_fetch_attestation(path: Path, rows: list[dict[str, object]]) -> None:
    counts = {symbol: 0 for symbol in CRYPTO_UNIVERSE_SYMBOLS}
    latest = {symbol: "" for symbol in CRYPTO_UNIVERSE_SYMBOLS}
    dates: list[str] = []
    for row in rows:
        symbol = str(row["symbol"]).upper()
        day = str(row["timestamp"])[:10]
        dates.append(day)
        if symbol in counts:
            counts[symbol] += 1
            latest[symbol] = max(latest[symbol], day)
    source_sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
    payload = {
        "schema_version": "1.1",
        "generated_at": "2026-07-14T00:00:00Z",
        "start": min(dates),
        "end": max(dates),
        "observed_start": min(dates),
        "observed_end": max(dates),
        "expected_latest_bar_date": max(dates),
        "symbols": list(CRYPTO_UNIVERSE_SYMBOLS),
        "row_count": len(rows),
        "per_symbol_row_counts": counts,
        "per_symbol_latest_dates": latest,
        "provider": "alpaca_crypto_data",
        "feed": "us",
        "status": "OK",
        "blockers": [],
        "published": True,
        "source_sha256": source_sha256,
    }
    sidecar = path.with_name(path.name + ".fetch.json")
    sidecar.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def _write_csv_dataset(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    governed_rows = _prepare_governed_crypto_rows(rows)
    write_records(governed_rows, path)
    _write_valid_fetch_attestation(path, governed_rows)


def _write_healthy_breaker_state(tmp_path: Path) -> Path:
    """Create the valid, unpaused latch required by confirmed execution."""

    path = tmp_path / "breaker_state.json"
    path.write_text(
        json.dumps(
            {
                "stage": "none",
                "paused": False,
                "first_breach_at": None,
                "updated_at": datetime.now(UTC).isoformat(),
            }
        ),
        encoding="utf-8",
    )
    return path


def _write_high_water(tmp_path: Path, value: float = 100_000.0) -> Path:
    """Create the positive durable high-water prerequisite for mutations."""

    path = tmp_path / "equity_highwater.json"
    path.write_text(
        json.dumps(
            {
                "high_water_equity": float(value),
                "updated_at": datetime.now(UTC).isoformat(),
            }
        ),
        encoding="utf-8",
    )
    return path


def _confirmed_execution_paths(
    tmp_path: Path,
    *,
    high_water: float = 100_000.0,
) -> dict[str, Path]:
    """Return explicit fail-closed prerequisites for a confirmed test."""

    return {
        "breaker_state_path": _write_healthy_breaker_state(tmp_path),
        "equity_highwater_path": _write_high_water(tmp_path, high_water),
    }


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
                **_confirmed_execution_paths(tmp_path),
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
                submit_reasons=("symbol_not_allowlisted", "broker token=sk-live-secret"),
            )
            result = run_sleeve_rebalance(
                universe_config="configs/crypto_alpaca.yml",
                risk_config="configs/risk.yml",
                dataset=dataset,
                output=output,
                notional_usd=1000.0,
                broker=broker,
                confirm_submit=True,
                **_confirmed_execution_paths(tmp_path),
            )
            self.assertEqual(result.status, "WARN")
            artifact_text = output.read_text(encoding="utf-8")
            payload = json.loads(artifact_text)
            self.assertFalse(payload["safety"]["orders_submitted"])
            self.assertTrue(payload["safety"]["orders_attempted"])
            rejection_reasons = [
                reason
                for submission in payload["submissions"]
                if submission.get("status") == "rejected"
                for reason in submission.get("reasons", [])
            ]
            self.assertIn("broker token=[redacted]", rejection_reasons)
            self.assertNotIn("sk-live-secret", artifact_text)
            self.assertNotIn("sk-live-secret", json.dumps(result.payload))

    def test_pending_open_buy_counts_as_current_exposure_and_holds(self) -> None:
        # M15 (§4 of docs/revision-operaciones-2026-07-14.md, supersedes the
        # pre-M15 ``pending_open_buy_counts_as_current_exposure`` test).
        #
        # Pre-M15 (commit 72895aa): a submitted-but-unfilled buy (queued for
        # the next equity open / weekend) counted as current exposure so the
        # next cycle would emit a smaller (or zero) buy delta — preventing a
        # double entry once both orders filled. Sells were left alone under
        # the (wrong) assumption that Alpaca's quantity reservation rejects
        # duplicate exits.
        #
        # The 2026-07-14 review proved that assumption wrong for PARTIAL
        # sells (the Friday→Monday weekend re-issued the same partial sells
        # and both executed on Monday open). M15 collapses both cases into
        # one symmetric rule: ANY open system order (sleeve-/breaker-) for
        # a pair makes the next plan entry ``pending_order_hold`` — the
        # in-flight order settles first and the following cycle re-plans
        # from the true positions.
        #
        # The buy_notional netting is preserved (so ``current_notional`` and
        # ``pending_buy_notional`` still report truthfully) but no submission
        # happens regardless of how far the plan delta would have been.
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            dataset = self._build_dataset(tmp_path)
            output = tmp_path / "report.json"

            class _BrokerWithOpenOrders(_FakeBroker):
                def list_orders(self, *, status: str = "open") -> tuple[SimpleNamespace, ...]:
                    assert status == "open"
                    return (
                        # system BUY for BTC/USD — must trigger the M15 gate.
                        SimpleNamespace(
                            symbol="BTC/USD",
                            side="buy",
                            notional=100.0,
                            client_order_id="sleeve-2026-07-12-BTCUSD-buy",
                        ),
                        # Open partial SELL on BTC — pre-M15 this was silently
                        # ignored and the next cycle re-emitted it. M15 flips
                        # ``has_open_order=True`` for any side.
                        SimpleNamespace(
                            symbol="BTC/USD",
                            side="sell",
                            notional=50.0,
                            client_order_id="sleeve-2026-07-12-BTCUSD-sell",
                        ),
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
                **_confirmed_execution_paths(tmp_path),
            )
            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(payload["pending_buy_notional"], {"BTC/USD": 100.0})
            # BTC is also flagged as having an open system order (the SELL).
            self.assertEqual(payload["pending_order_pairs"], ["BTC/USD"])
            btc_entries = [e for e in payload["plan"] if e["pair"] == "BTC/USD"]
            self.assertEqual(len(btc_entries), 1)
            entry = btc_entries[0]
            # M15 action — not "buy" with a reduced delta, but a total hold.
            self.assertEqual(entry["action"], "pending_order_hold")
            self.assertEqual(entry["note"], "open_order_in_flight")
            # The buy netting still feeds ``current_notional`` so the report
            # remains truthful about in-flight exposure.
            self.assertEqual(entry["current_notional"], 100.0)
            # No BTC order reaches the broker — the hold path suppresses
            # submission regardless of how the plan delta would have looked.
            btc_orders = [o for o in broker.submitted if o.symbol == "BTC/USD"]
            self.assertEqual(btc_orders, [])
            # The submissions array still contains a no-op record so the
            # report shows what the cycle tried (and didn't) to do.
            btc_submissions = [s for s in payload["submissions"] if s.get("pair") == "BTC/USD"]
            self.assertEqual(len(btc_submissions), 1)
            sub = btc_submissions[0]
            self.assertEqual(sub["action"], "pending_order_hold")
            self.assertTrue(sub["skipped"])
            self.assertEqual(sub["reasons"], ["action_does_not_submit"])
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
                    raise RuntimeError("client_order_id must be unique token=sk-live-secret")

            broker = _RaisingBroker(positions=[])
            result = run_sleeve_rebalance(
                universe_config="configs/crypto_alpaca.yml",
                risk_config="configs/risk.yml",
                dataset=dataset,
                output=output,
                notional_usd=1000.0,
                broker=broker,
                confirm_submit=True,
                **_confirmed_execution_paths(tmp_path),
            )
            self.assertEqual(result.status, "BLOCKED")
            artifact_text = output.read_text(encoding="utf-8")
            payload = json.loads(artifact_text)
            errored = [s for s in payload["submissions"] if s.get("status") == "error"]
            self.assertGreater(len(errored), 0)
            self.assertIn("client_order_id must be unique", errored[0]["reasons"][0])
            self.assertIn("token=[redacted]", errored[0]["reasons"][0])
            self.assertNotIn("sk-live-secret", artifact_text)

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
                **_confirmed_execution_paths(tmp_path),
            )
            self.assertEqual(result.status, "OK")
            self.assertEqual(broker.submitted, [])
            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertFalse(payload["safety"]["orders_submitted"])
            self.assertTrue(payload["safety"]["confirm_submit"])


class RunSleeveRebalanceFailClosedContractTests(unittest.TestCase):
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

    @staticmethod
    def _read_sidecar(dataset: Path) -> dict[str, object]:
        path = dataset.with_name(dataset.name + ".fetch.json")
        return json.loads(path.read_text(encoding="utf-8"))

    @staticmethod
    def _write_sidecar(dataset: Path, payload: dict[str, object]) -> None:
        path = dataset.with_name(dataset.name + ".fetch.json")
        path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")

    def test_missing_expected_universe_symbol_blocks_without_sell(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            rows = _build_momentum_records(
                {
                    "BTC/USD": [100.0 + i for i in range(200)],
                    "ETH/USD": [200.0 for _ in range(200)],
                },
                start=(date.today() - timedelta(days=199)).isoformat(),
            )
            dataset = tmp_path / "partial.csv"
            # Intentionally bypass the governed fixture helper: this is the
            # partial-universe input the production gate must reject.
            write_records(rows, dataset)
            broker = _FakeBroker(
                positions=[SimpleNamespace(symbol="LTCUSD", qty=1.0, market_value=100.0)]
            )

            result = run_sleeve_rebalance(
                universe_config="configs/crypto_alpaca.yml",
                risk_config="configs/risk.yml",
                dataset=dataset,
                output=tmp_path / "report.json",
                notional_usd=1000.0,
                broker=broker,
            )

            self.assertEqual(result.status, "BLOCKED")
            self.assertIn("dataset_universe_incomplete:LTC/USD", result.payload["blockers"])
            self.assertEqual(broker.submitted, [])
            self.assertEqual(result.payload["plan"], [])

    def test_symbol_missing_global_latest_bar_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            base = _build_momentum_records(
                {
                    "BTC/USD": [100.0 + i for i in range(200)],
                    "ETH/USD": [200.0 for _ in range(200)],
                },
                start=(date.today() - timedelta(days=199)).isoformat(),
            )
            rows = _prepare_governed_crypto_rows(base)
            latest = max(str(row["timestamp"])[:10] for row in rows)
            rows = [
                row
                for row in rows
                if not (row["symbol"] == "XRP/USD" and str(row["timestamp"])[:10] == latest)
            ]
            dataset = tmp_path / "latest-missing.csv"
            write_records(rows, dataset)

            result = run_sleeve_rebalance(
                universe_config="configs/crypto_alpaca.yml",
                risk_config="configs/risk.yml",
                dataset=dataset,
                output=tmp_path / "report.json",
                notional_usd=1000.0,
            )

            self.assertEqual(result.status, "BLOCKED")
            self.assertIn("dataset_latest_bar_missing:XRP/USD", result.payload["blockers"])

    def test_missing_fetch_attestation_blocks_confirmed_cycle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            dataset = self._build_dataset(tmp_path)
            dataset.with_name(dataset.name + ".fetch.json").unlink()
            broker = _FakeBroker()

            result = run_sleeve_rebalance(
                universe_config="configs/crypto_alpaca.yml",
                risk_config="configs/risk.yml",
                dataset=dataset,
                output=tmp_path / "report.json",
                notional_usd=1000.0,
                broker=broker,
                confirm_submit=True,
                **_confirmed_execution_paths(tmp_path),
            )

            self.assertEqual(result.status, "BLOCKED")
            self.assertIn("fetch_attestation_missing", result.payload["blockers"])
            self.assertEqual(broker.list_orders_calls, 0)
            self.assertEqual(broker.submitted, [])

    def test_non_ok_fetch_attestation_blocks_confirmed_cycle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            dataset = self._build_dataset(tmp_path)
            sidecar = self._read_sidecar(dataset)
            sidecar["status"] = "BLOCKED"
            sidecar["published"] = False
            self._write_sidecar(dataset, sidecar)
            broker = _FakeBroker()

            result = run_sleeve_rebalance(
                universe_config="configs/crypto_alpaca.yml",
                risk_config="configs/risk.yml",
                dataset=dataset,
                output=tmp_path / "report.json",
                notional_usd=1000.0,
                broker=broker,
                confirm_submit=True,
                **_confirmed_execution_paths(tmp_path),
            )

            self.assertEqual(result.status, "BLOCKED")
            self.assertIn("fetch_attestation_status_not_ok", result.payload["blockers"])
            self.assertEqual(broker.submitted, [])

    def test_fetch_attestation_hash_mismatch_blocks_confirmed_cycle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            dataset = self._build_dataset(tmp_path)
            with dataset.open("a", encoding="utf-8") as handle:
                handle.write("\n")
            broker = _FakeBroker()

            result = run_sleeve_rebalance(
                universe_config="configs/crypto_alpaca.yml",
                risk_config="configs/risk.yml",
                dataset=dataset,
                output=tmp_path / "report.json",
                notional_usd=1000.0,
                broker=broker,
                confirm_submit=True,
                **_confirmed_execution_paths(tmp_path),
            )

            self.assertEqual(result.status, "BLOCKED")
            self.assertIn("fetch_attestation_hash_mismatch", result.payload["blockers"])
            self.assertEqual(broker.submitted, [])

    def test_valid_fetch_attestation_and_empty_order_snapshot_allow_submit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            dataset = self._build_dataset(tmp_path)
            broker = _FakeBroker()

            result = run_sleeve_rebalance(
                universe_config="configs/crypto_alpaca.yml",
                risk_config="configs/risk.yml",
                dataset=dataset,
                output=tmp_path / "report.json",
                notional_usd=1000.0,
                broker=broker,
                confirm_submit=True,
                **_confirmed_execution_paths(tmp_path),
            )

            self.assertEqual(result.status, "OK")
            self.assertGreaterEqual(broker.list_orders_calls, 2)
            self.assertTrue(broker.submitted)
            self.assertEqual(result.payload["dataset_attestation"]["status"], "OK")
            self.assertEqual(result.payload["open_orders_check"]["status"], "OK")

    def test_open_order_read_exception_blocks_without_submission(self) -> None:
        class _RaisingOrdersBroker(_FakeBroker):
            def list_orders(self, *, status: str = "open") -> tuple[Any, ...]:
                raise RuntimeError("temporary broker read failure")

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            dataset = self._build_dataset(tmp_path)
            broker = _RaisingOrdersBroker()

            result = run_sleeve_rebalance(
                universe_config="configs/crypto_alpaca.yml",
                risk_config="configs/risk.yml",
                dataset=dataset,
                output=tmp_path / "report.json",
                notional_usd=1000.0,
                broker=broker,
                confirm_submit=True,
                **_confirmed_execution_paths(tmp_path),
            )

            self.assertEqual(result.status, "BLOCKED")
            self.assertIn("open_orders_snapshot_unavailable", result.payload["blockers"])
            self.assertEqual(broker.submitted, [])

    def test_position_read_exception_writes_blocked_artifact(self) -> None:
        class _RaisingPositionsBroker(_FakeBroker):
            def read_positions(self) -> tuple[SimpleNamespace, ...]:
                raise TimeoutError("executor position snapshot timed out")

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output = tmp_path / "report.json"
            broker = _RaisingPositionsBroker()

            result = run_sleeve_rebalance(
                universe_config="configs/crypto_alpaca.yml",
                risk_config="configs/risk.yml",
                dataset=self._build_dataset(tmp_path),
                output=output,
                notional_usd=1000.0,
                broker=broker,
                confirm_submit=True,
                **_confirmed_execution_paths(tmp_path),
            )

            artifact = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(result.status, "BLOCKED")
        self.assertEqual(artifact["status"], "BLOCKED")
        self.assertIn(
            "positions_read_error:TimeoutError",
            artifact["position_snapshot_blockers"],
        )
        self.assertEqual(broker.submitted, [])

    def test_missing_list_orders_capability_blocks_without_submission(self) -> None:
        class _NoOrdersCapabilityBroker:
            def __init__(self) -> None:
                self.submitted: list[Any] = []

            def submit_order(self, order: Any) -> None:
                self.submitted.append(order)
                raise AssertionError("submit_order must not be called")

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            dataset = self._build_dataset(tmp_path)
            broker = _NoOrdersCapabilityBroker()

            result = run_sleeve_rebalance(
                universe_config="configs/crypto_alpaca.yml",
                risk_config="configs/risk.yml",
                dataset=dataset,
                output=tmp_path / "report.json",
                notional_usd=1000.0,
                broker=broker,
                confirm_submit=True,
                **_confirmed_execution_paths(tmp_path),
            )

            self.assertEqual(result.status, "BLOCKED")
            self.assertIn("open_orders_snapshot_unavailable", result.payload["blockers"])
            self.assertEqual(broker.submitted, [])

    def test_external_open_order_blocks_new_opening_orders(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            dataset = self._build_dataset(tmp_path)
            broker = _FakeBroker(
                open_orders=[
                    SimpleNamespace(
                        symbol="BTC/USD",
                        side="buy",
                        notional=50.0,
                        client_order_id="manual-btc-buy",
                    )
                ]
            )

            result = run_sleeve_rebalance(
                universe_config="configs/crypto_alpaca.yml",
                risk_config="configs/risk.yml",
                dataset=dataset,
                output=tmp_path / "report.json",
                notional_usd=1000.0,
                broker=broker,
                confirm_submit=True,
                **_confirmed_execution_paths(tmp_path),
            )

            self.assertEqual(result.status, "BLOCKED")
            self.assertIn("external_open_orders_present", result.payload["blockers"])
            self.assertEqual(broker.submitted, [])

    def test_corrupt_breaker_state_blocks_before_broker_reads(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            dataset = self._build_dataset(tmp_path)
            breaker_state = tmp_path / "breaker_state.json"
            breaker_state.write_text("{not-json", encoding="utf-8")
            broker = _FakeBroker()

            result = run_sleeve_rebalance(
                universe_config="configs/crypto_alpaca.yml",
                risk_config="configs/risk.yml",
                dataset=dataset,
                output=tmp_path / "report.json",
                notional_usd=1000.0,
                broker=broker,
                confirm_submit=True,
                breaker_state_path=breaker_state,
                equity_highwater_path=_write_high_water(tmp_path),
            )

            self.assertEqual(result.status, "BLOCKED")
            self.assertEqual(
                result.payload["blockers"],
                ["circuit_breaker_state_corrupt"],
            )
            self.assertEqual(broker.list_orders_calls, 0)
            self.assertEqual(broker.submitted, [])

    def test_missing_or_corrupt_high_water_blocks_confirmed_cycle(self) -> None:
        for scenario in ("missing", "corrupt"):
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as tmp:
                tmp_path = Path(tmp)
                dataset = self._build_dataset(tmp_path)
                high_water = tmp_path / "equity_highwater.json"
                if scenario == "corrupt":
                    high_water.write_text(
                        json.dumps({"high_water_equity": 0.0}),
                        encoding="utf-8",
                    )
                broker = _FakeBroker()

                result = run_sleeve_rebalance(
                    universe_config="configs/crypto_alpaca.yml",
                    risk_config="configs/risk.yml",
                    dataset=dataset,
                    output=tmp_path / "report.json",
                    notional_usd=1000.0,
                    broker=broker,
                    confirm_submit=True,
                    breaker_state_path=_write_healthy_breaker_state(tmp_path),
                    equity_highwater_path=high_water,
                )

                self.assertEqual(result.status, "BLOCKED")
                self.assertIn(
                    "account_risk_context_unavailable",
                    result.payload["blockers"],
                )
                self.assertEqual(broker.submitted, [])

    def test_unsafe_position_shapes_block_confirmed_cycle(self) -> None:
        scenarios = {
            "unknown": (
                [SimpleNamespace(symbol="AAPL", qty=1.0, market_value=100.0)],
                "position_unmapped:AAPL",
            ),
            "malformed": (
                [SimpleNamespace(symbol="BTCUSD", qty="invalid", market_value=100.0)],
                "position_numeric_invalid:BTCUSD",
            ),
            "short": (
                [SimpleNamespace(symbol="BTCUSD", qty=-1.0, market_value=-100.0)],
                "short_position_unsupported:BTC/USD",
            ),
            "duplicate": (
                [
                    SimpleNamespace(symbol="BTCUSD", qty=1.0, market_value=100.0),
                    SimpleNamespace(symbol="BTC/USD", qty=0.5, market_value=50.0),
                ],
                "position_duplicate:BTC/USD",
            ),
        }
        for scenario, (positions, blocker) in scenarios.items():
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as tmp:
                tmp_path = Path(tmp)
                dataset = self._build_dataset(tmp_path)
                broker = _FakeBroker(positions=positions)

                result = run_sleeve_rebalance(
                    universe_config="configs/crypto_alpaca.yml",
                    risk_config="configs/risk.yml",
                    dataset=dataset,
                    output=tmp_path / "report.json",
                    notional_usd=1000.0,
                    broker=broker,
                    confirm_submit=True,
                    **_confirmed_execution_paths(tmp_path),
                )

                self.assertEqual(result.status, "BLOCKED")
                self.assertIn(blocker, result.payload["position_snapshot_blockers"])
                self.assertIn(blocker, result.payload["blockers"])
                self.assertEqual(broker.submitted, [])


class RunSleeveRebalancePendingOrderHoldTests(unittest.TestCase):
    """M15: in-flight orders from this system gate the next cycle's plan.

    Fix for the §4 incident in docs/revision-operaciones-2026-07-14.md: when
    the weekend re-plans re-issued Friday's Monday-queued partial sells, both
    executed on Monday open and XLF/XLI were halved. The new rule is
    symmetric — any open sleeve-/breaker- order for a pair makes the next
    plan emit ``pending_order_hold`` so the in-flight order settles first.
    """

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

    def test_open_sell_holds_pair_others_operate_normally(self) -> None:
        # M15 case 1 (spec §tests/1): an OPEN system SELL on BTC gates the
        # BTC plan entry to ``pending_order_hold`` and the broker receives no
        # BTC submission for that pair. ETH (no open order) operates normally
        # via the existing plan path.
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            dataset = self._build_dataset(tmp_path)
            output = tmp_path / "report.json"

            class _BrokerWithOpenSell(_FakeBroker):
                def list_orders(self, *, status: str = "open") -> tuple[SimpleNamespace, ...]:
                    assert status == "open"
                    return (
                        SimpleNamespace(
                            symbol="BTC/USD",
                            side="sell",
                            notional=50.0,
                            client_order_id="sleeve-2026-07-10-BTCUSD-sell",
                        ),
                    )

            broker = _BrokerWithOpenSell(
                positions=[
                    # Pre-existing BTC position the partial sell was trimming.
                    SimpleNamespace(symbol="BTCUSD", qty=0.02, market_value=120.0),
                ],
            )
            result = run_sleeve_rebalance(
                universe_config="configs/crypto_alpaca.yml",
                risk_config="configs/risk.yml",
                dataset=dataset,
                output=output,
                notional_usd=1000.0,
                broker=broker,
                confirm_submit=True,
                **_confirmed_execution_paths(tmp_path),
            )
            self.assertEqual(result.exit_code, 0)
            payload = json.loads(output.read_text(encoding="utf-8"))
            plan_by_pair = {entry["pair"]: entry for entry in payload["plan"]}
            # BTC gated to ``pending_order_hold`` by the open SELL.
            btc_entry = plan_by_pair["BTC/USD"]
            self.assertEqual(btc_entry["action"], "pending_order_hold")
            self.assertEqual(btc_entry["note"], "open_order_in_flight")
            # BTC did NOT reach the broker (the §4 incident signature).
            btc_orders = [o for o in broker.submitted if o.symbol == "BTC/USD"]
            self.assertEqual(btc_orders, [])
            # The submissions list still records the no-op for transparency.
            btc_submissions = [s for s in payload["submissions"] if s.get("pair") == "BTC/USD"]
            self.assertEqual(len(btc_submissions), 1)
            self.assertEqual(btc_submissions[0]["action"], "pending_order_hold")
            self.assertTrue(btc_submissions[0]["skipped"])
            # ETH operates normally — no open order, so ETH enters its
            # standard plan branch (hold here, since ETH has no momentum).
            self.assertIn("ETH/USD", plan_by_pair)
            eth_entry = plan_by_pair["ETH/USD"]
            self.assertNotEqual(eth_entry["action"], "pending_order_hold")
            # ``note`` is the M15-only annotation; ETH (not held) must NOT
            # carry it. This guards against accidentally promoting every
            # entry to the M15 shape on the held-pair path's neighbours.
            self.assertNotIn("note", eth_entry)
            self.assertFalse(
                any(
                    getattr(order, "client_order_id", "").startswith("breaker-")
                    for order in broker.submitted
                )
            )
            self.assertEqual(payload["pending_order_pairs"], ["BTC/USD"])

    def test_open_buy_holds_pair_instead_of_reducing_delta(self) -> None:
        # M15 case 2 (spec §tests/2): an OPEN system BUY now triggers a
        # FULL hold — pre-M15 the cycle still emitted a (smaller) buy with
        # the pending notional netted from ``current_value``. The new
        # ``pending_order_hold`` action is the symmetric counterpart of the
        # sell-in-flight case above; it also covers the open-BUY edge.
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            dataset = self._build_dataset(tmp_path)
            output = tmp_path / "report.json"

            class _BrokerWithOpenBuy(_FakeBroker):
                def list_orders(self, *, status: str = "open") -> tuple[SimpleNamespace, ...]:
                    assert status == "open"
                    return (
                        SimpleNamespace(
                            symbol="BTC/USD",
                            side="buy",
                            notional=200.0,
                            client_order_id="sleeve-2026-07-12-BTCUSD-buy",
                        ),
                    )

            broker = _BrokerWithOpenBuy(positions=[])
            result = run_sleeve_rebalance(
                universe_config="configs/crypto_alpaca.yml",
                risk_config="configs/risk.yml",
                dataset=dataset,
                output=output,
                notional_usd=1000.0,
                broker=broker,
                confirm_submit=True,
                **_confirmed_execution_paths(tmp_path),
            )
            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(payload["pending_buy_notional"], {"BTC/USD": 200.0})
            self.assertEqual(payload["pending_order_pairs"], ["BTC/USD"])
            btc_entry = next(e for e in payload["plan"] if e["pair"] == "BTC/USD")
            self.assertEqual(btc_entry["action"], "pending_order_hold")
            self.assertEqual(btc_entry["note"], "open_order_in_flight")
            # Buy netting still feeds ``current_notional`` for visibility
            # but the plan never submits.
            self.assertEqual(btc_entry["current_notional"], 200.0)
            btc_orders = [o for o in broker.submitted if o.symbol == "BTC/USD"]
            self.assertEqual(btc_orders, [])
            self.assertEqual(result.exit_code, 0)

    def test_open_order_with_non_system_prefix_blocks_confirmed_cycle(self) -> None:
        # P0-01: an outside-system order is a broker-state divergence. It is
        # reported and the confirmed batch is blocked instead of opening a
        # potentially colliding position.
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            dataset = self._build_dataset(tmp_path)
            output = tmp_path / "report.json"

            class _BrokerWithManualOrder(_FakeBroker):
                def list_orders(self, *, status: str = "open") -> tuple[SimpleNamespace, ...]:
                    assert status == "open"
                    return (
                        SimpleNamespace(
                            symbol="BTC/USD",
                            side="buy",
                            notional=200.0,
                            client_order_id="manual-external-trader",
                        ),
                    )

            broker = _BrokerWithManualOrder(positions=[])
            result = run_sleeve_rebalance(
                universe_config="configs/crypto_alpaca.yml",
                risk_config="configs/risk.yml",
                dataset=dataset,
                output=output,
                notional_usd=1000.0,
                broker=broker,
                confirm_submit=True,
                **_confirmed_execution_paths(tmp_path),
            )
            self.assertEqual(result.status, "BLOCKED")
            self.assertEqual(result.exit_code, 1)
            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertIn("external_open_orders_present", payload["blockers"])
            self.assertEqual(broker.submitted, [])

    def test_open_breaker_order_holds_pair(self) -> None:
        # M15 case 4 (spec §tests/4): the M11 circuit breaker also places
        # orders directly (outside the plan), but its ``client_order_id``
        # is ``breaker-<date>-<pair>-{half,all}``. Any open breaker- order
        # must also gate the next cycle so a re-plan can't fight the
        # breaker's unwind.
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            dataset = self._build_dataset(tmp_path)
            output = tmp_path / "report.json"

            class _BrokerWithBreakerOrder(_FakeBroker):
                def list_orders(self, *, status: str = "open") -> tuple[SimpleNamespace, ...]:
                    assert status == "open"
                    return (
                        SimpleNamespace(
                            symbol="BTC/USD",
                            side="sell",
                            notional=80.0,
                            client_order_id="breaker-2026-07-11-BTCUSD-half",
                        ),
                    )

            broker = _BrokerWithBreakerOrder(
                positions=[
                    SimpleNamespace(symbol="BTCUSD", qty=0.02, market_value=120.0),
                ],
            )
            result = run_sleeve_rebalance(
                universe_config="configs/crypto_alpaca.yml",
                risk_config="configs/risk.yml",
                dataset=dataset,
                output=output,
                notional_usd=1000.0,
                broker=broker,
                confirm_submit=True,
                **_confirmed_execution_paths(tmp_path),
            )
            self.assertEqual(result.exit_code, 0)
            payload = json.loads(output.read_text(encoding="utf-8"))
            # The breaker order is a SELL (not a buy), so pending_buy_notional
            # is empty — but ``pending_order_pairs`` must still flag BTC.
            self.assertEqual(payload["pending_buy_notional"], {})
            self.assertEqual(payload["pending_order_pairs"], ["BTC/USD"])
            btc_entry = next(e for e in payload["plan"] if e["pair"] == "BTC/USD")
            self.assertEqual(btc_entry["action"], "pending_order_hold")
            self.assertEqual(btc_entry["note"], "open_order_in_flight")
            # Nothing reaches the broker for BTC.
            btc_orders = [o for o in broker.submitted if o.symbol == "BTC/USD"]
            self.assertEqual(btc_orders, [])

    def test_no_open_orders_matches_pre_m15_plan(self) -> None:
        # P0-01 regression: an explicit, successfully-read empty tuple keeps
        # the pre-M15 plan. A missing list_orders capability is tested above
        # and must block rather than being treated as an empty snapshot.
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
                **_confirmed_execution_paths(tmp_path),
            )
            self.assertEqual(result.exit_code, 0)
            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(payload["pending_buy_notional"], {})
            self.assertEqual(payload["pending_order_pairs"], [])
            btc_entry = next(e for e in payload["plan"] if e["pair"] == "BTC/USD")
            self.assertEqual(btc_entry["action"], "buy")
            self.assertNotIn("note", btc_entry)
            # And a buy did reach the broker, same as pre-M15.
            btc_buys = [
                o for o in broker.submitted
                if o.symbol == "BTC/USD" and o.side == "buy"
            ]
            self.assertEqual(len(btc_buys), 1)


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
        execution_paths = _confirmed_execution_paths(
            tmp_path,
            high_water=100_000.0 if highwater is None else highwater,
        )
        hw_path = execution_paths["equity_highwater_path"]
        result = run_sleeve_rebalance(
            universe_config="configs/crypto_alpaca.yml",
            risk_config="configs/risk.yml",
            dataset=dataset,
            output=output,
            notional_usd=1000.0,
            broker=broker,
            confirm_submit=True,
            **execution_paths,
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
                any(b.startswith("dataset_symbol_stale:") for b in blockers),
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


class RunSleeveRebalanceExecutorPhaseOneTests(unittest.TestCase):
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

    @staticmethod
    def _health(*, mutations_allowed: bool) -> dict[str, object]:
        return {
            "status": "ready" if mutations_allowed else "blocked",
            "mutations_allowed": mutations_allowed,
            "opening_orders_allowed": False,
            "capability_mode": "reduce_only" if mutations_allowed else "blocked",
            "account_scope_sha256": "a" * 64,
            "policy_sha256": "b" * 64,
            "authz_policy_sha256": "c" * 64,
            "run_id": "1" * 32,
            "fence_epoch": 7,
            "pending_recovery": 0 if mutations_allowed else 1,
            "kill_switch_active": False,
        }

    def _run(
        self,
        tmp_path: Path,
        broker: _FakeBroker,
        *,
        mode: str,
        order_style: str = "market",
    ) -> tuple[Any, dict[str, Any]]:
        output = tmp_path / "report.json"
        result = run_sleeve_rebalance(
            universe_config="configs/crypto_alpaca.yml",
            risk_config="configs/risk.yml",
            dataset=self._build_dataset(tmp_path),
            output=output,
            notional_usd=1000.0,
            broker=broker,
            confirm_submit=True,
            order_style=order_style,
            executor_capability_mode=mode,
            executor_health=self._health(
                mutations_allowed=mode == EXECUTOR_MODE_REDUCE_ONLY
            ),
            **_confirmed_execution_paths(tmp_path),
        )
        return result, json.loads(output.read_text(encoding="utf-8"))

    def test_reduce_only_submits_sell_before_deferring_buy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            broker = _ReceiptExecutorBroker(
                positions=[
                    SimpleNamespace(symbol="ETHUSD", qty=1.0, market_value=200.0),
                ]
            )
            result, payload = self._run(
                Path(tmp),
                broker,
                mode=EXECUTOR_MODE_REDUCE_ONLY,
            )

        self.assertEqual(result.status, "BLOCKED")
        self.assertEqual([order.side for order in broker.submitted], ["sell"])
        actionable = [
            submission
            for submission in payload["submissions"]
            if submission["action"] in {"buy", "sell", "sell_all"}
        ]
        self.assertIn(actionable[0]["action"], {"sell", "sell_all"})
        buy = next(item for item in actionable if item["action"] == "buy")
        sell = next(
            item for item in actionable if item["action"] in {"sell", "sell_all"}
        )
        self.assertEqual(buy["status"], "submit_deferred")
        self.assertEqual(buy["reasons"], ["executor_opening_orders_deferred"])
        self.assertTrue(sell["submitted"])
        self.assertEqual(sell["executor_receipt"]["target"]["fence_epoch"], 7)
        self.assertIn("executor_cycle_deferred", payload["blockers"])
        self.assertIn(
            "executor_reduction_reconciliation_pending",
            payload["blockers"],
        )
        self.assertFalse(payload["executor"]["opening_orders_attempted"])

    def test_blocked_executor_defers_every_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            broker = _ReceiptExecutorBroker(
                positions=[
                    SimpleNamespace(symbol="ETHUSD", qty=1.0, market_value=200.0),
                ]
            )
            result, payload = self._run(
                Path(tmp),
                broker,
                mode=EXECUTOR_MODE_BLOCKED,
            )

        self.assertEqual(result.status, "BLOCKED")
        self.assertEqual(broker.submitted, [])
        self.assertEqual(broker.cancelled_client_ids, [])
        actionable = [
            item
            for item in payload["submissions"]
            if item["action"] in {"buy", "sell", "sell_all"}
        ]
        self.assertGreaterEqual(len(actionable), 2)
        self.assertTrue(all(item["status"] == "submit_deferred" for item in actionable))
        self.assertTrue(
            all(
                item["reasons"] == ["executor_mutations_unavailable"]
                for item in actionable
            )
        )
        self.assertEqual(payload["executor"]["capability_mode"], "blocked")

    def test_outcome_unknown_is_structured_and_stops_later_reductions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            broker = _OutcomeUnknownExecutorBroker(
                positions=[
                    SimpleNamespace(symbol="ETHUSD", qty=1.0, market_value=200.0),
                    SimpleNamespace(symbol="LTCUSD", qty=1.0, market_value=100.0),
                ]
            )
            result, payload = self._run(
                Path(tmp),
                broker,
                mode=EXECUTOR_MODE_REDUCE_ONLY,
            )

        self.assertEqual(result.status, "BLOCKED")
        self.assertEqual(broker.submit_attempts, 1)
        self.assertEqual(broker.submitted, [])
        reductions = [
            item
            for item in payload["submissions"]
            if item["action"] in {"sell", "sell_all"}
        ]
        self.assertEqual(reductions[0]["status"], "submit_unresolved")
        self.assertEqual(reductions[1]["status"], "halted_after_unresolved")
        unknown = reductions[0]["executor_outcome_unknown"]
        self.assertEqual(unknown["request_id"], "e" * 32)
        self.assertEqual(unknown["operation"], "submit_order")
        self.assertEqual(unknown["phase"], "receive")
        self.assertFalse(unknown["retry_allowed"])
        self.assertEqual(unknown["target"]["fence_epoch"], 7)
        self.assertEqual(unknown["target"]["account_scope_sha256"], "a" * 64)
        self.assertEqual(unknown["target"]["policy_sha256"], "b" * 64)
        self.assertEqual(unknown["target"]["authz_policy_sha256"], "c" * 64)
        self.assertEqual(unknown["target"]["run_id"], "1" * 32)
        self.assertTrue(payload["safety"]["orders_submission_unknown"])
        self.assertIn("order_state_unresolved", payload["blockers"])

    def test_reduce_only_limit_maker_is_deferred_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            broker = _ReceiptExecutorBroker(
                positions=[
                    SimpleNamespace(symbol="ETHUSD", qty=1.0, market_value=200.0),
                ]
            )
            result, payload = self._run(
                Path(tmp),
                broker,
                mode=EXECUTOR_MODE_REDUCE_ONLY,
                order_style="limit-maker",
            )

        self.assertEqual(result.status, "BLOCKED")
        self.assertEqual(broker.submitted, [])
        self.assertEqual(broker.latest_trade_calls, [])
        self.assertEqual(broker.cancelled_client_ids, [])
        actionable = [
            item
            for item in payload["submissions"]
            if item["action"] in {"buy", "sell", "sell_all"}
        ]
        self.assertTrue(
            all(
                item["reasons"] == ["executor_limit_maker_workflow_unavailable"]
                for item in actionable
            )
        )

    def test_breaker_change_during_preflight_blocks_before_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            execution_paths = _confirmed_execution_paths(tmp_path)
            breaker_path = execution_paths["breaker_state_path"]

            class _BreakerFlipBroker(_ReceiptExecutorBroker):
                def __init__(self) -> None:
                    super().__init__(
                        positions=[
                            SimpleNamespace(
                                symbol="ETHUSD",
                                qty=1.0,
                                market_value=200.0,
                            )
                        ]
                    )
                    self.position_reads = 0

                def read_positions(self) -> tuple[SimpleNamespace, ...]:
                    self.position_reads += 1
                    if self.position_reads == 2:
                        breaker_path.write_text(
                            json.dumps(
                                {
                                    "stage": "partial_done",
                                    "paused": True,
                                    "first_breach_at": datetime.now(UTC).isoformat(),
                                    "updated_at": datetime.now(UTC).isoformat(),
                                }
                            ),
                            encoding="utf-8",
                        )
                    return super().read_positions()

            broker = _BreakerFlipBroker()
            output = tmp_path / "report.json"
            result = run_sleeve_rebalance(
                universe_config="configs/crypto_alpaca.yml",
                risk_config="configs/risk.yml",
                dataset=self._build_dataset(tmp_path),
                output=output,
                notional_usd=1000.0,
                broker=broker,
                confirm_submit=True,
                executor_capability_mode=EXECUTOR_MODE_REDUCE_ONLY,
                executor_health=self._health(mutations_allowed=True),
                **execution_paths,
            )
            payload = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(result.status, "BLOCKED")
        self.assertEqual(broker.position_reads, 2)
        self.assertEqual(broker.submitted, [])
        self.assertIn(
            "pre_dispatch_circuit_breaker_paused",
            payload["blockers"],
        )
        self.assertEqual(payload["executor"]["capability_mode"], "reduce_only")


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

    def test_cli_real_paper_uses_only_executor_reduce_only_capability(self) -> None:
        for builder_name in (
            "build_alpaca_paper_client",
            "build_alpaca_market_data_client",
            "build_alpaca_crypto_market_data_client",
        ):
            self.assertFalse(hasattr(cli_module, builder_name), builder_name)
        executor = mock.Mock()
        health = RunSleeveRebalanceExecutorPhaseOneTests._health(
            mutations_allowed=True
        )
        executor.health.return_value = health
        result = SimpleNamespace(
            status="BLOCKED",
            exit_code=2,
            output_path=Path("report.json"),
            payload={"submissions": []},
        )
        with (
            mock.patch(
                "trading_ai.cli.PaperExecutorBrokerClient",
                return_value=executor,
            ) as executor_constructor,
            mock.patch(
                "trading_ai.cli.run_sleeve_rebalance",
                return_value=result,
            ) as runner,
            mock.patch(
                "trading_ai.execution.alpaca_connection.build_alpaca_paper_client",
                side_effect=AssertionError("direct broker client must not be built"),
            ) as direct_client,
            mock.patch(
                "trading_ai.execution.alpaca_connection.build_alpaca_market_data_client",
                side_effect=AssertionError("direct market client must not be built"),
            ) as direct_market,
            mock.patch(
                "trading_ai.execution.alpaca_connection.build_alpaca_crypto_market_data_client",
                side_effect=AssertionError("direct crypto client must not be built"),
            ) as direct_crypto,
        ):
            exit_code = main(
                [
                    "sleeve-rebalance",
                    "--dataset",
                    "unused.csv",
                    "--notional-usd",
                    "1000",
                    "--real-paper",
                    "--confirm-paper",
                    "--confirm-auto-submit",
                    "--as-of-date",
                    "2026-07-15",
                ]
            )

        self.assertEqual(exit_code, 2)
        runner.assert_called_once()
        executor_constructor.assert_called_once_with()
        executor.health.assert_called_once_with()
        executor.pin_target.assert_called_once_with(health)
        direct_client.assert_not_called()
        direct_market.assert_not_called()
        direct_crypto.assert_not_called()
        kwargs = runner.call_args.kwargs
        self.assertIs(kwargs["broker"], executor)
        self.assertTrue(kwargs["confirm_submit"])
        self.assertEqual(
            kwargs["executor_capability_mode"],
            EXECUTOR_MODE_REDUCE_ONLY,
        )
        self.assertIs(kwargs["executor_health"], health)

    def test_invalid_as_of_date_does_not_construct_executor(self) -> None:
        with mock.patch(
            "trading_ai.cli.PaperExecutorBrokerClient",
            side_effect=AssertionError("executor must not be constructed"),
        ) as executor_constructor:
            exit_code = main(
                [
                    "sleeve-rebalance",
                    "--dataset",
                    "unused.csv",
                    "--notional-usd",
                    "1000",
                    "--real-paper",
                    "--confirm-paper",
                    "--as-of-date",
                    "invalid",
                ]
            )

        self.assertEqual(exit_code, 2)
        executor_constructor.assert_not_called()

    def test_executor_health_failure_writes_fresh_blocked_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "report.json"
            output.write_text('{"status":"STALE"}', encoding="utf-8")
            executor = mock.Mock()
            executor.health.side_effect = TimeoutError(
                "socket timeout token=sk-live-secret"
            )
            stderr = StringIO()
            with (
                mock.patch(
                    "trading_ai.cli.PaperExecutorBrokerClient",
                    return_value=executor,
                ),
                redirect_stderr(stderr),
            ):
                exit_code = main(
                    [
                        "sleeve-rebalance",
                        "--dataset",
                        "unused.csv",
                        "--notional-usd",
                        "1000",
                        "--real-paper",
                        "--confirm-paper",
                        "--confirm-auto-submit",
                        "--output",
                        str(output),
                    ]
                )
            payload = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 2)
        self.assertEqual(payload["status"], "BLOCKED")
        self.assertEqual(
            payload["blockers"],
            ["executor_preflight_failed:TimeoutError"],
        )
        self.assertEqual(payload["submissions"], [])
        self.assertFalse(payload["safety"]["orders_submission_unknown"])
        self.assertNotIn("sk-live-secret", json.dumps(payload))
        self.assertNotIn("sk-live-secret", stderr.getvalue())
        executor.pin_target.assert_not_called()

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
                **_confirmed_execution_paths(tmp_path),
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

    def test_limit_maker_rejection_reasons_are_redacted_in_result_and_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            dataset = self._build_dataset(tmp_path)
            output = tmp_path / "report.json"
            broker = _FakeBroker(
                positions=[],
                latest_prices={"BTC/USD": 100.0},
                submit_accepted=False,
                submit_status="rejected",
                submit_reasons=("broker token=sk-live-secret",),
            )
            result = run_sleeve_rebalance(
                universe_config="configs/crypto_alpaca.yml",
                risk_config="configs/risk.yml",
                dataset=dataset,
                output=output,
                notional_usd=1000.0,
                broker=broker,
                confirm_submit=True,
                **_confirmed_execution_paths(tmp_path),
                order_style="limit-maker",
            )
            artifact_text = output.read_text(encoding="utf-8")
            payload = json.loads(artifact_text)

        limit_submissions = [
            item for item in payload["submissions"] if item.get("style") == "limit-maker"
        ]
        self.assertEqual(result.status, "WARN")
        self.assertGreater(len(limit_submissions), 0)
        self.assertIn("broker token=[redacted]", limit_submissions[0]["reasons"])
        self.assertNotIn("sk-live-secret", artifact_text)
        self.assertNotIn("sk-live-secret", json.dumps(result.payload))

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
                **_confirmed_execution_paths(tmp_path),
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

    def test_limit_maker_cancel_exception_blocks_without_market_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            dataset = self._build_dataset(tmp_path)
            output = tmp_path / "report.json"
            broker = _FakeBroker(
                positions=[],
                latest_prices={"BTC/USD": 100.0},
                order_states=[{"status": "accepted"}, {"status": "accepted"}],
                cancel_error=TimeoutError("cancel timed out"),
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
                **_confirmed_execution_paths(tmp_path),
                order_style="limit-maker",
                limit_wait_seconds=20,
                sleep=clock.sleep,
                now=clock.now,
            )

            payload = json.loads(output.read_text(encoding="utf-8"))
            market_fallbacks = [
                order
                for order in broker.submitted
                if getattr(order, "client_order_id", "").endswith("-mkt")
            ]

        self.assertEqual(result.status, "BLOCKED")
        self.assertEqual(market_fallbacks, [])
        self.assertIn("order_state_unresolved", payload["blockers"])
        self.assertTrue(any(item.get("status") == "error" for item in payload["submissions"]))

    def test_limit_maker_pending_cancel_blocks_without_market_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            dataset = self._build_dataset(tmp_path)
            output = tmp_path / "report.json"
            broker = _FakeBroker(
                positions=[],
                latest_prices={"BTC/USD": 100.0},
                order_states=[
                    {"status": "accepted"},
                    {"status": "accepted"},
                    {"status": "pending_cancel"},
                ],
                cancel_status="cancel_requested",
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
                **_confirmed_execution_paths(tmp_path),
                order_style="limit-maker",
                limit_wait_seconds=20,
                sleep=clock.sleep,
                now=clock.now,
            )

            payload = json.loads(output.read_text(encoding="utf-8"))
            market_fallbacks = [
                order
                for order in broker.submitted
                if getattr(order, "client_order_id", "").endswith("-mkt")
            ]

        self.assertEqual(result.status, "BLOCKED")
        self.assertEqual(market_fallbacks, [])
        self.assertTrue(
            any(item.get("status") == "cancel_unresolved" for item in payload["submissions"])
        )

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
                    # Final post-cancel snapshot: terminal canceled with a
                    # preserved partial fill. Fallback is forbidden while the
                    # order remains merely partially_filled/pending_cancel.
                    {"status": "canceled", "filled_quantity": 0.5, "filled_avg_price": limit_price},
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
                **_confirmed_execution_paths(tmp_path),
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
                    # Terminal canceled with a partial fill that leaves a
                    # sub-$10 remainder.
                    {"status": "canceled", "filled_quantity": 0.05, "filled_avg_price": limit_price},
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
                **_confirmed_execution_paths(tmp_path),
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
                **_confirmed_execution_paths(tmp_path),
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
                **_confirmed_execution_paths(tmp_path),
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
                **_confirmed_execution_paths(tmp_path),
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

    def test_unresolved_submission_halts_remaining_plan(self) -> None:
        from trading_ai.execution.sleeve_rebalance import _execute_submissions

        broker = _FakeBroker(
            positions=[],
            submit_accepted=False,
            submit_status="submit_unresolved",
            submit_reasons=("broker_outcome_unknown",),
        )
        plan = [
            self._buy_plan(pair="BTC/USD", notional=100.0),
            self._buy_plan(pair="ETH/USD", notional=100.0),
        ]
        submissions = _execute_submissions(
            plan=plan,
            broker=broker,
            as_of_date=date(2026, 7, 10),
            universe_name="crypto",
            risk_context={
                "equity": 10_000.0,
                "last_equity": 10_000.0,
                "daily_pnl_pct": 0.0,
                "high_water_equity": 10_000.0,
                "current_drawdown_pct": 0.0,
            },
        )

        self.assertEqual(len(broker.submitted), 1)
        self.assertEqual(submissions[0]["status"], "submit_unresolved")
        self.assertEqual(submissions[1]["status"], "halted_after_unresolved")
        self.assertEqual(
            submissions[1]["reasons"],
            ("prior_order_state_unresolved",),
        )

    def test_projected_gross_accumulates_across_opening_orders(self) -> None:
        from trading_ai.execution.sleeve_rebalance import _execute_submissions

        broker = _FakeBroker(positions=[])
        submissions = _execute_submissions(
            plan=[
                self._buy_plan(pair="BTC/USD", notional=200.0),
                self._buy_plan(pair="ETH/USD", notional=300.0),
            ],
            broker=broker,
            as_of_date=date(2026, 7, 10),
            universe_name="crypto",
            risk_context={
                "equity": 10_000.0,
                "last_equity": 10_000.0,
                "daily_pnl_pct": 0.0,
                "high_water_equity": 10_000.0,
                "current_drawdown_pct": 0.0,
            },
            gross_current=1_000.0,
        )

        self.assertEqual(len(submissions), 2)
        self.assertEqual(len(broker.submitted), 2)
        self.assertAlmostEqual(broker.submitted[0].projected_gross_exposure, 0.12)
        self.assertAlmostEqual(broker.submitted[1].projected_gross_exposure, 0.15)

    def test_risk_cap_is_preserved_across_limit_market_fallback(self) -> None:
        from trading_ai.execution.sleeve_rebalance import _execute_submissions

        broker = _FakeBroker(
            positions=[],
            latest_prices={"BTC/USD": 100.0},
            order_states=[
                {"status": "accepted"},
                {"status": "accepted"},
                {"status": "canceled"},
            ],
        )
        clock = _FakeClock(start=1000.0)
        submissions = _execute_submissions(
            plan=[self._buy_plan(pair="BTC/USD", notional=950.0)],
            broker=broker,
            as_of_date=date(2026, 7, 10),
            universe_name="crypto",
            risk_context={
                "equity": 10_000.0,
                "last_equity": 10_000.0,
                "daily_pnl_pct": 0.0,
                "high_water_equity": 10_000.0,
                "current_drawdown_pct": 0.0,
            },
            order_style="limit-maker",
            limit_wait_seconds=20,
            sleep=clock.sleep,
            now=clock.now,
            risk_to_stop_enabled=True,
            risk_budget_pct=0.005,
            stop_loss_pct=0.10,
        )

        self.assertEqual(len(broker.submitted), 2)
        limit_order, market_order = broker.submitted
        self.assertEqual(limit_order.notional, 500.0)
        self.assertEqual(market_order.notional, 500.0)
        self.assertTrue(market_order.client_order_id.endswith("-mkt"))
        self.assertEqual(submissions[0]["filled_via"], "market_fallback")
        self.assertEqual(submissions[0]["risk_to_stop_cap"], 500.0)
        self.assertEqual(submissions[0]["original_notional"], 950.0)

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
