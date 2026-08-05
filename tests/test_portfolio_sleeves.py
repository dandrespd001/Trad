"""Tests for multi-sleeve risk-parity combination (Sprint L1)."""

from __future__ import annotations

import statistics
import unittest

from trading_ai.backtest.portfolio import combine_risk_parity_sleeves


def _series(start_day: int, values: list[float]) -> dict[str, float]:
    return {f"2020-01-{start_day + i:02d}": v for i, v in enumerate(values)}


class RiskParitySleeveTests(unittest.TestCase):
    def test_single_sleeve_passthrough_after_warmup(self) -> None:
        # One sleeve, constant tiny returns; during warmup returns are raw.
        s = _series(1, [0.001] * 25)
        result = combine_risk_parity_sleeves({"a": s}, warmup=5, vol_window=5)
        self.assertEqual(len(result.daily_returns), 25)
        # First warmup values are unscaled.
        self.assertAlmostEqual(result.daily_returns[0], 0.001, places=12)

    def test_leverage_cap_limits_scale_up(self) -> None:
        # Low-vol series -> target/vol would be huge; cap must bound the scale.
        vals = [0.0001 * ((-1) ** i) for i in range(40)]  # tiny alternating
        s = {"a": _series(1, vals)}
        capped = combine_risk_parity_sleeves(s, leverage_cap=1.0, warmup=5, vol_window=10, target_daily_vol=0.01)
        uncapped = combine_risk_parity_sleeves(s, leverage_cap=5.0, warmup=5, vol_window=10, target_daily_vol=0.01)
        # With cap 1.0 no post-warmup value exceeds the raw magnitude.
        raw_max = max(abs(v) for v in vals)
        self.assertLessEqual(max(abs(v) for v in capped.daily_returns), raw_max + 1e-12)
        # A higher cap lets the scale grow, so its max magnitude is larger.
        self.assertGreater(
            max(abs(v) for v in uncapped.daily_returns),
            max(abs(v) for v in capped.daily_returns),
        )

    def test_closed_sleeve_budget_remains_in_cash(self) -> None:
        # Sleeve b trades on an extra date a does not (like crypto weekends).
        a = {"2020-01-01": 0.01, "2020-01-02": 0.02}
        b = {"2020-01-01": -0.01, "2020-01-02": 0.02, "2020-01-03": 0.05}
        result = combine_risk_parity_sleeves({"a": a, "b": b}, warmup=100)  # warmup>len -> raw
        by_date = dict(zip(result.timestamps, result.daily_returns, strict=True))
        # Day 1: both present -> average of raw (warmup) values.
        self.assertAlmostEqual(by_date["2020-01-01"], (0.01 + -0.01) / 2, places=12)
        # Day 3: sleeve a's fixed half-budget is cash, so b contributes half.
        self.assertAlmostEqual(by_date["2020-01-03"], 0.05 / 2, places=12)

    def test_start_date_filters(self) -> None:
        a = {"2019-12-31": 0.9, "2020-01-01": 0.01}
        result = combine_risk_parity_sleeves({"a": a}, start_date="2020-01-01", warmup=100)
        self.assertEqual(result.timestamps, ("2020-01-01",))

    def test_start_date_filters_output_without_resetting_volatility_warmup(self) -> None:
        values = [0.001 if index % 2 == 0 else -0.001 for index in range(20)]
        sleeve = _series(1, values)

        result = combine_risk_parity_sleeves(
            {"a": sleeve},
            start_date="2020-01-11",
            warmup=5,
            vol_window=5,
            target_daily_vol=0.0005,
            leverage_cap=1.0,
        )

        expected_scale = min(0.0005 / statistics.pstdev(values[5:10]), 1.0)
        self.assertEqual(result.timestamps[0], "2020-01-11")
        self.assertAlmostEqual(result.daily_returns[0], values[10] * expected_scale, places=12)

    def test_diversification_lowers_combined_volatility(self) -> None:
        # Two anti-correlated sleeves -> combined vol below each sleeve's vol.
        n = 60
        a = {f"2020-{1 + i // 28:02d}-{1 + i % 28:02d}": (0.01 if i % 2 == 0 else -0.01) for i in range(n)}
        b = {f"2020-{1 + i // 28:02d}-{1 + i % 28:02d}": (-0.01 if i % 2 == 0 else 0.01) for i in range(n)}
        result = combine_risk_parity_sleeves({"a": a, "b": b}, warmup=100)
        combined_vol = statistics.pstdev(result.daily_returns)
        self.assertLess(combined_vol, statistics.pstdev(list(a.values())))

    def test_validation_errors(self) -> None:
        with self.assertRaises(ValueError):
            combine_risk_parity_sleeves({})
        with self.assertRaises(ValueError):
            combine_risk_parity_sleeves({"a": {"2020-01-01": 0.0}}, target_daily_vol=0.0)
        with self.assertRaises(ValueError):
            combine_risk_parity_sleeves({"a": {"2020-01-01": 0.0}}, leverage_cap=0.0)


if __name__ == "__main__":
    unittest.main()


class SleeveBacktestCliTests(unittest.TestCase):
    def _write(self, path, symbols, days):
        import csv
        with open(path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["timestamp", "symbol", "open", "high", "low", "close", "volume"])
            for s_i, sym in enumerate(symbols):
                for i in range(days):
                    m, d = 1 + i // 28, 1 + i % 28
                    c = 100.0 + i * (0.5 + 0.2 * s_i)
                    w.writerow([f"2021-{m:02d}-{d:02d}", sym, c - 0.2, c + 1, c - 1, c, 1_000_000])

    def test_cli_sleeve_backtest_runs_and_writes_metrics(self) -> None:
        import json
        import tempfile
        from pathlib import Path

        from trading_ai.cli import build_parser

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            a, b = root / "a.csv", root / "b.csv"
            self._write(a, ["SPY", "QQQ"], 80)
            self._write(b, ["BTCUSD", "ETHUSD"], 80)
            out = root / "sleeve.json"
            args = build_parser().parse_args([
                "sleeve-backtest",
                "--sleeve", f"etf={a},1,20,252",
                "--sleeve", f"crypto={b},25,20,365",
                "--output", str(out),
            ])
            rc = args.func(args)
            self.assertEqual(rc, 0)
            payload = json.loads(out.read_text())
            self.assertEqual(payload["schema_version"], "2.0")
            self.assertEqual(payload["strategy"], "risk-parity-sleeves")
            self.assertTrue(payload["research_only"])
            self.assertFalse(payload["promotion_eligible"])
            self.assertEqual(
                payload["promotion_blockers"],
                [
                    "deflated_sharpe_trial_registry_missing",
                    "trade_level_profit_factor_unavailable",
                ],
            )

            metrics = payload["metrics"]
            self.assertIn("return_gain_loss_ratio", metrics)
            self.assertIsNone(metrics["profit_factor"])
            self.assertEqual(metrics["profit_factor_status"], "UNAVAILABLE_NOT_TRADE_LEVEL")
            self.assertIsNone(metrics["deflated_sharpe"])
            self.assertEqual(
                metrics["deflated_sharpe_status"],
                "UNAVAILABLE_NO_TRIAL_REGISTRY",
            )
            self.assertIn("sharpe_full", metrics)
            self.assertIn("max_drawdown", metrics)

            expected_costs = {"etf": 1.0, "crypto": 25.0}
            self.assertEqual(len(payload["sleeves"]), 2)
            for source in payload["sleeves"]:
                self.assertEqual(source["cost_bps"], expected_costs[source["name"]])
                self.assertEqual(source["cost_bps"], source["total_one_way_cost_bps"])
                self.assertEqual(
                    source["cost_input_semantics"],
                    "all_in_charged_once_on_execution_turnover",
                )
                execution = source["execution_model"]
                self.assertEqual(execution["engine_version"], "next_open_v2")
                self.assertEqual(
                    execution["execution_timing"],
                    "signal_close_execute_next_open",
                )
                self.assertEqual(execution["cost_model"]["slippage_bps"], 0.0)
                self.assertEqual(
                    execution["cost_model"]["total_one_way_bps"],
                    source["total_one_way_cost_bps"],
                )

    def test_cli_rejects_bad_sleeve_spec(self) -> None:
        from trading_ai.cli import build_parser

        args = build_parser().parse_args(["sleeve-backtest", "--sleeve", "bogus_spec"])
        self.assertEqual(args.func(args), 2)

    def test_cli_rejects_non_finite_or_negative_cost(self) -> None:
        from trading_ai.cli import build_parser

        for cost in ("nan", "inf", "-0.01"):
            with self.subTest(cost=cost):
                args = build_parser().parse_args(
                    [
                        "sleeve-backtest",
                        "--sleeve",
                        f"etf=unused.csv,{cost},20,252",
                    ]
                )
                self.assertEqual(args.func(args), 2)
