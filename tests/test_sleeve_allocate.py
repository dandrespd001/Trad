"""Tests for Sprint M4 ``sleeve-allocate`` (risk-parity execution budgets)."""

from __future__ import annotations

import json
import statistics
import tempfile
import unittest
from pathlib import Path

from trading_ai.backtest.portfolio import (
    _causal_vol_normalized,
    compute_current_sleeve_allocation,
    trailing_scale,
)
from trading_ai.cli import build_parser


def _series(start_day: int, values: list[float]) -> dict[str, float]:
    return {f"2020-01-{start_day + i:02d}": v for i, v in enumerate(values)}


class TrailingScaleTests(unittest.TestCase):
    """ANTI-DRIFT: the per-day math in ``_causal_vol_normalized`` MUST equal
    the new module-level ``trailing_scale`` helper so the live allocation
    shares the same formula as the historical edge (§28).
    """

    def test_structural_equality_with_causal_vol_normalized(self) -> None:
        # Synthetic return series with variable volatility, > warmup dates.
        n = 40
        warmup = 10
        vol_window = 8
        target = 0.01
        cap = 1.0
        raw = [0.01 if i % 3 == 0 else -0.005 if i % 3 == 1 else 0.003 for i in range(n)]
        dates = [f"2020-01-{i + 1:02d}" for i in range(n)]
        series = dict(zip(dates, raw, strict=True))

        scaled = _causal_vol_normalized(
            series,
            dates,
            target_daily_vol=target,
            vol_window=vol_window,
            leverage_cap=cap,
            warmup=warmup,
        )
        # For every date d beyond the warmup, the scaled value must equal
        # ``raw[d] * trailing_scale(history_up_to_but_not_including_d, ...)``.
        history: list[float] = []
        for _i, date in enumerate(dates):
            value = series[date]
            expected = value * trailing_scale(
                history,
                target_daily_vol=target,
                vol_window=vol_window,
                leverage_cap=cap,
                warmup=warmup,
            )
            self.assertAlmostEqual(scaled[date], expected, places=12, msg=f"date={date}")
            history.append(value)

    def test_warmup_returns_unit_scale(self) -> None:
        self.assertEqual(
            trailing_scale([], target_daily_vol=0.01, vol_window=5, leverage_cap=1.0, warmup=5),
            1.0,
        )
        self.assertEqual(
            trailing_scale([0.01] * 4, target_daily_vol=0.01, vol_window=5, leverage_cap=1.0, warmup=5),
            1.0,
        )

    def test_zero_vol_returns_cap(self) -> None:
        # All-equal history -> pstdev is 0 -> scale must equal the cap.
        s = trailing_scale(
            [0.01] * 10,
            target_daily_vol=0.02,
            vol_window=5,
            leverage_cap=0.5,
            warmup=5,
        )
        self.assertEqual(s, 0.5)


class ComputeCurrentSleeveAllocationTests(unittest.TestCase):
    def test_low_vol_sleeve_gets_higher_budget(self) -> None:
        # Both sleeves have ~30 days of history (> warmup=10). Low-vol sleeve
        # has a small trailing pstdev -> scale near the cap -> bigger budget.
        n = 30
        high_vol_returns = [0.05 if i % 2 == 0 else -0.05 for i in range(n)]
        low_vol_returns = [0.002 if i % 2 == 0 else -0.002 for i in range(n)]
        dates = [f"2020-01-{i + 1:02d}" for i in range(n)]
        sleeves = {
            "hi": dict(zip(dates, high_vol_returns, strict=True)),
            "lo": dict(zip(dates, low_vol_returns, strict=True)),
        }
        out = compute_current_sleeve_allocation(
            sleeves,
            total_notional_usd=10_000.0,
            target_daily_vol=0.01,
            vol_window=10,
            leverage_cap=1.0,
            warmup=10,
        )
        self.assertEqual(out["total_notional_usd"], 10_000.0)
        per = out["sleeves"]
        # Sorted by name -> "hi" then "lo".
        self.assertEqual(list(per.keys()), ["hi", "lo"])
        # Sum of budgets must not exceed the total (cap=1.0 guarantees this
        # when n_sleeves>=1 since each scale<=cap; with cap=1.0 and two
        # equal-weighted sleeves the sum equals the total exactly when both
        # scales == 1.0, but high-vol scale is < 1.0 so sum < total).
        self.assertLessEqual(sum(s["budget_usd"] for s in per.values()), 10_000.0 + 1e-6)
        # No scale exceeds the cap.
        for s in per.values():
            self.assertLessEqual(s["scale"], 1.0 + 1e-9)
        # Low-vol sleeve has the bigger budget.
        self.assertGreater(per["lo"]["budget_usd"], per["hi"]["budget_usd"])
        # Trailing vol sanity: hi > lo.
        self.assertGreater(per["hi"]["trailing_vol"], per["lo"]["trailing_vol"])

    def test_short_series_uses_unit_scale(self) -> None:
        # Shorter than warmup -> scale=1.0 -> budget = total/n_sleeves.
        sleeves = {
            "a": _series(1, [0.01, -0.02, 0.03]),  # 3 < warmup=20
            "b": _series(1, [0.02, -0.01, 0.04]),
        }
        out = compute_current_sleeve_allocation(
            sleeves,
            total_notional_usd=1_000.0,
            target_daily_vol=0.01,
            vol_window=5,
            leverage_cap=1.0,
            warmup=20,
        )
        for _name, attrs in out["sleeves"].items():
            self.assertEqual(attrs["scale"], 1.0)
            self.assertEqual(attrs["budget_usd"], 500.0)
            self.assertEqual(attrs["n_returns"], 3)

    def test_single_return_series_uses_zero_vol(self) -> None:
        # <2 observations -> trailing_vol=0.0; warmup > 1 -> scale=1.0.
        sleeves = {"a": _series(1, [0.01]), "b": _series(1, [0.02])}
        out = compute_current_sleeve_allocation(
            sleeves,
            total_notional_usd=1_000.0,
            target_daily_vol=0.01,
            vol_window=5,
            leverage_cap=1.0,
            warmup=5,
        )
        for attrs in out["sleeves"].values():
            self.assertEqual(attrs["scale"], 1.0)
            self.assertEqual(attrs["trailing_vol"], 0.0)
            self.assertEqual(attrs["budget_usd"], 500.0)

    def test_validation_errors(self) -> None:
        with self.assertRaises(ValueError):
            compute_current_sleeve_allocation({}, total_notional_usd=1.0)
        with self.assertRaises(ValueError):
            compute_current_sleeve_allocation(
                {"a": _series(1, [0.01])},
                total_notional_usd=0.0,
            )
        with self.assertRaises(ValueError):
            compute_current_sleeve_allocation(
                {"a": _series(1, [0.01])},
                total_notional_usd=-1.0,
            )
        with self.assertRaises(ValueError):
            compute_current_sleeve_allocation(
                {"a": _series(1, [0.01])},
                total_notional_usd=1.0,
                target_daily_vol=0.0,
            )
        with self.assertRaises(ValueError):
            compute_current_sleeve_allocation(
                {"a": _series(1, [0.01])},
                total_notional_usd=1.0,
                vol_window=1,
            )
        with self.assertRaises(ValueError):
            compute_current_sleeve_allocation(
                {"a": _series(1, [0.01])},
                total_notional_usd=1.0,
                leverage_cap=0.0,
            )

    def test_golden_budget_values_lock_in_formula(self) -> None:
        # ANTI-DRIFT companion: pin the actual numerical output of
        # ``compute_current_sleeve_allocation`` on a deterministic input so
        # future refactors of ``trailing_scale`` can't silently change the
        # execution budgets.
        n = 30
        warmup = 10
        vol_window = 5
        dates = [f"2020-01-{i + 1:02d}" for i in range(n)]
        # Sleeve "a": returns alternate +/- 0.04 -> pstdev over last 5
        # depends on which 5 are selected but is deterministic.
        a_returns = [0.04 if i % 2 == 0 else -0.04 for i in range(n)]
        b_returns = [0.005 for i in range(n)]  # constant -> pstdev 0 -> scale=cap
        sleeves = {
            "a": dict(zip(dates, a_returns, strict=True)),
            "b": dict(zip(dates, b_returns, strict=True)),
        }
        out = compute_current_sleeve_allocation(
            sleeves,
            total_notional_usd=1_000.0,
            target_daily_vol=0.01,
            vol_window=vol_window,
            leverage_cap=1.0,
            warmup=warmup,
        )
        # Sleeve b: all-equal returns -> trailing pstdev is 0 -> scale=cap=1.0
        # and budget = (1000/2) * 1.0 = 500.0.
        self.assertEqual(out["sleeves"]["b"]["scale"], 1.0)
        self.assertEqual(out["sleeves"]["b"]["trailing_vol"], 0.0)
        self.assertEqual(out["sleeves"]["b"]["budget_usd"], 500.0)
        # Sleeve a: trailing vol is pstdev of the last 5 returns
        # (i=25..29 -> a_returns[25..29] = [-0.04, 0.04, -0.04, 0.04, -0.04]).
        expected_a_vol = statistics.pstdev(a_returns[-vol_window:])
        self.assertAlmostEqual(out["sleeves"]["a"]["trailing_vol"], expected_a_vol, places=8)
        expected_a_scale = min(0.01 / expected_a_vol, 1.0)
        self.assertAlmostEqual(out["sleeves"]["a"]["scale"], expected_a_scale, places=6)
        expected_a_budget = (1_000.0 / 2) * expected_a_scale
        self.assertAlmostEqual(out["sleeves"]["a"]["budget_usd"], expected_a_budget, places=2)


class SleeveAllocateCliTests(unittest.TestCase):
    def _write_ohlcv(self, path: Path, symbols: list[str], days: int) -> None:
        import csv

        with open(path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["timestamp", "symbol", "open", "high", "low", "close", "volume"])
            for s_i, sym in enumerate(symbols):
                for i in range(days):
                    m, d = 1 + i // 28, 1 + i % 28
                    c = 100.0 + i * (0.5 + 0.2 * s_i)
                    w.writerow([f"2021-{m:02d}-{d:02d}", sym, c - 0.2, c + 1, c - 1, c, 1_000_000])

    def test_cli_sleeve_allocate_runs_and_writes_payload(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            a, b = root / "etf.csv", root / "crypto.csv"
            self._write_ohlcv(a, ["SPY", "QQQ"], 80)
            self._write_ohlcv(b, ["BTCUSD", "ETHUSD"], 80)
            out = root / "allocation.json"
            args = build_parser().parse_args([
                "sleeve-allocate",
                "--sleeve", f"etf={a},1,20,252",
                "--sleeve", f"crypto={b},25,20,365",
                "--total-notional-usd", "10000",
                "--output", str(out),
            ])
            rc = args.func(args)
            self.assertEqual(rc, 0)
            payload = json.loads(out.read_text())
            self.assertEqual(payload["schema_version"], "2.0")
            self.assertEqual(payload["total_notional_usd"], 10_000.0)
            self.assertIn("sleeve_sources", payload)
            self.assertEqual(len(payload["sleeve_sources"]), 2)
            sources_by_name = {s["name"]: s for s in payload["sleeve_sources"]}
            self.assertIn("etf", sources_by_name)
            self.assertIn("crypto", sources_by_name)
            self.assertEqual(sources_by_name["crypto"]["cost_bps"], 25.0)
            self.assertEqual(sources_by_name["crypto"]["total_one_way_cost_bps"], 25.0)
            self.assertEqual(
                sources_by_name["crypto"]["cost_input_semantics"],
                "all_in_charged_once_on_execution_turnover",
            )
            crypto_cost_model = sources_by_name["crypto"]["execution_model"]["cost_model"]
            self.assertEqual(crypto_cost_model["cost_bps"], 25.0)
            self.assertEqual(crypto_cost_model["slippage_bps"], 0.0)
            self.assertEqual(crypto_cost_model["total_one_way_bps"], 25.0)
            allocation = payload["allocation"]
            self.assertIn("sleeves", allocation)
            # Budgets sum to at most the total (cap=1.0).
            total_budget = sum(s["budget_usd"] for s in allocation["sleeves"].values())
            self.assertLessEqual(total_budget, 10_000.0 + 1e-6)
            for _name, attrs in allocation["sleeves"].items():
                self.assertIn("scale", attrs)
                self.assertIn("trailing_vol", attrs)
                self.assertIn("n_returns", attrs)
                self.assertIn("budget_usd", attrs)
                self.assertLessEqual(attrs["scale"], 1.0 + 1e-9)

    def test_cli_rejects_bad_sleeve_spec(self) -> None:
        args = build_parser().parse_args([
            "sleeve-allocate",
            "--sleeve", "bogus_spec",
            "--total-notional-usd", "1000",
        ])
        self.assertEqual(args.func(args), 2)

    def test_cli_rejects_non_finite_negative_or_non_positive_spec_values(self) -> None:
        bad_specs = (
            "etf=dataset.csv,-1,20,252",
            "etf=dataset.csv,nan,20,252",
            "etf=dataset.csv,inf,20,252",
            "etf=dataset.csv,1,0,252",
            "etf=dataset.csv,1,20,0",
        )
        for spec in bad_specs:
            with self.subTest(spec=spec):
                args = build_parser().parse_args(
                    [
                        "sleeve-allocate",
                        "--sleeve",
                        spec,
                        "--total-notional-usd",
                        "1000",
                    ]
                )
                self.assertEqual(args.func(args), 2)

    def test_cli_rejects_invalid_dataset(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            bad = root / "bad.csv"
            # Missing required columns -> validation fails -> rc=1.
            with open(bad, "w", newline="") as f:
                f.write("timestamp,symbol\n2021-01-01,SPY\n")
            out = root / "allocation.json"
            args = build_parser().parse_args([
                "sleeve-allocate",
                "--sleeve", f"etf={bad},1,20,252",
                "--total-notional-usd", "1000",
                "--output", str(out),
            ])
            self.assertEqual(args.func(args), 1)


if __name__ == "__main__":
    unittest.main()
