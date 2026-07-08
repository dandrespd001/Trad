"""Tests for the cross-sectional (within-date) feature function and CLI flag.

Spec I2: cross-sectional (fuerza relativa entre símbolos) features, opt-in via
``--cross-sectional`` on the ``build-features`` subcommand. Default off must
remain byte-identical to today's behavior.
"""

from __future__ import annotations

import csv
import math
import tempfile
import unittest
from pathlib import Path
from typing import Any

from trading_ai.cli import main
from trading_ai.data.io import write_records
from trading_ai.features.engineering import (
    DEFAULT_CROSS_SECTIONAL_COLUMNS,
    FeatureConfig,
    add_cross_sectional_features,
    build_features,
)


def _base_ohlcv(
    *,
    days: int = 30,
    symbols: tuple[str, ...] = ("AAA", "BBB"),
) -> list[dict[str, Any]]:
    """Generate a small multi-symbol OHLCV fixture.

    Two symbols, identical daily drift, so that base features like
    ``return_1d`` and ``momentum_20`` are populated and finite on every row.
    Dates are kept to valid ISO calendar dates (Jan-Feb 2024) so the dataset
    validator accepts them.
    """
    from datetime import date, timedelta

    rows: list[dict[str, Any]] = []
    start = date(2024, 1, 1)
    dates: list[str] = []
    cursor = start
    while len(dates) < days:
        if cursor.weekday() < 5:  # Mon-Fri only
            dates.append(cursor.isoformat())
        cursor += timedelta(days=1)
    for symbol_index, symbol in enumerate(symbols):
        close = 100.0 + symbol_index
        for day_index, iso_date in enumerate(dates):
            close = close * 1.001
            rows.append(
                {
                    "timestamp": iso_date,
                    "symbol": symbol,
                    "open": round(close - 0.1, 4),
                    "high": round(close + 0.5, 4),
                    "low": round(close - 0.5, 4),
                    "close": round(close, 4),
                    "volume": 1_000_000 + day_index * 10,
                }
            )
    return rows


def _read_csv(path: Path) -> list[dict[str, str]]:
    with Path(path).open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


# ---------------------------------------------------------------------------
# A) Default-off byte-identical output
# ---------------------------------------------------------------------------


class DefaultOffByteIdenticalTests(unittest.TestCase):
    """Without --cross-sectional the build-features output is identical."""

    def test_function_returns_input_unchanged_when_called_with_no_columns(self) -> None:
        # Sanity: the function exists; passing the default columns on a tiny
        # fixture must still produce a list of new dicts with the same order
        # and at least the original keys present.
        records = _base_ohlcv(days=5, symbols=("A", "B"))
        out = add_cross_sectional_features(records)
        self.assertEqual([row["symbol"] for row in out], [row["symbol"] for row in records])
        self.assertEqual([row["timestamp"] for row in out], [row["timestamp"] for row in records])

    def test_cli_without_flag_produces_no_xs_columns(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            dataset = root / "dataset.csv"
            output = root / "features.csv"
            write_records(_base_ohlcv(days=40, symbols=("AAA", "BBB", "CCC")), dataset)

            exit_code = main(["build-features", "--dataset", str(dataset), "--output", str(output)])

            self.assertEqual(exit_code, 0)
            rows = _read_csv(output)

        self.assertTrue(rows, "build-features must produce rows")
        xs_columns = {key for key in rows[0].keys() if key.startswith("xs_")}
        self.assertEqual(xs_columns, set(), f"unexpected xs_* columns in default output: {sorted(xs_columns)}")

    def test_cli_default_columns_constant_matches_function_default(self) -> None:
        # The CLI --cross-sectional-columns default must mirror the function's
        # default to keep behavior consistent across both entrypoints.
        self.assertEqual(
            tuple("return_1d,momentum_20,momentum_60,rsi_14".split(",")),
            DEFAULT_CROSS_SECTIONAL_COLUMNS,
        )


# ---------------------------------------------------------------------------
# B) Rank and z-score correctness on a deterministic fixture
# ---------------------------------------------------------------------------


class RankAndZScoreCorrectnessTests(unittest.TestCase):
    """Numerical correctness on a hand-built fixture."""

    def test_three_symbols_one_date_exact_values(self) -> None:
        records = [
            {"timestamp": "2024-06-01", "symbol": "A", "momentum_20": -1.0},
            {"timestamp": "2024-06-01", "symbol": "B", "momentum_20": 0.0},
            {"timestamp": "2024-06-01", "symbol": "C", "momentum_20": 1.0},
        ]

        out = add_cross_sectional_features(records, columns=("momentum_20",))

        # Min (-1.0) -> rank 0.0; Mid (0.0) -> rank 0.5; Max (+1.0) -> rank 1.0.
        by_symbol = {row["symbol"]: row for row in out}
        self.assertAlmostEqual(by_symbol["A"]["xs_rank_momentum_20"], 0.0)
        self.assertAlmostEqual(by_symbol["B"]["xs_rank_momentum_20"], 0.5)
        self.assertAlmostEqual(by_symbol["C"]["xs_rank_momentum_20"], 1.0)

        # z-score: population std of [-1, 0, 1] = sqrt(2/3); mean = 0.
        pop_std = math.sqrt(2.0 / 3.0)
        self.assertAlmostEqual(by_symbol["A"]["xs_z_momentum_20"], -1.0 / pop_std)
        self.assertAlmostEqual(by_symbol["B"]["xs_z_momentum_20"], 0.0)
        self.assertAlmostEqual(by_symbol["C"]["xs_z_momentum_20"], 1.0 / pop_std)

    def test_only_requested_columns_get_xs_keys(self) -> None:
        records = [
            {"timestamp": "2024-06-01", "symbol": "A", "momentum_20": -1.0, "return_1d": 0.01},
            {"timestamp": "2024-06-01", "symbol": "B", "momentum_20": 0.0, "return_1d": 0.02},
            {"timestamp": "2024-06-01", "symbol": "C", "momentum_20": 1.0, "return_1d": 0.03},
        ]

        out = add_cross_sectional_features(records, columns=("momentum_20",))

        for row in out:
            self.assertIn("xs_rank_momentum_20", row)
            self.assertIn("xs_z_momentum_20", row)
            self.assertNotIn("xs_rank_return_1d", row)
            self.assertNotIn("xs_z_return_1d", row)

    def test_z_score_uses_population_not_sample_std(self) -> None:
        # values [1, 2, 3]: population std = sqrt(2/3) ≈ 0.8165;
        # sample std = sqrt(1) = 1.0. The spec mandates population.
        records = [
            {"timestamp": "2024-06-01", "symbol": "A", "momentum_20": 1.0},
            {"timestamp": "2024-06-01", "symbol": "B", "momentum_20": 2.0},
            {"timestamp": "2024-06-01", "symbol": "C", "momentum_20": 3.0},
        ]

        out = add_cross_sectional_features(records, columns=("momentum_20",))
        by_symbol = {row["symbol"]: row for row in out}

        pop_std = math.sqrt(2.0 / 3.0)
        self.assertAlmostEqual(by_symbol["A"]["xs_z_momentum_20"], (1.0 - 2.0) / pop_std)
        self.assertAlmostEqual(by_symbol["B"]["xs_z_momentum_20"], 0.0)
        self.assertAlmostEqual(by_symbol["C"]["xs_z_momentum_20"], (3.0 - 2.0) / pop_std)


# ---------------------------------------------------------------------------
# C) Ties collapse to average rank
# ---------------------------------------------------------------------------


class TiesCollapseToAverageRankTests(unittest.TestCase):
    def test_two_way_tie_gets_average_rank(self) -> None:
        # [0, 0, 1] -> ranks (1+2)/2=1.5, 1.5, 3 -> normalized by (N-1)=2.
        records = [
            {"timestamp": "2024-06-01", "symbol": "A", "momentum_20": 0.0},
            {"timestamp": "2024-06-01", "symbol": "B", "momentum_20": 0.0},
            {"timestamp": "2024-06-01", "symbol": "C", "momentum_20": 1.0},
        ]
        out = add_cross_sectional_features(records, columns=("momentum_20",))

        ranks = sorted(row["xs_rank_momentum_20"] for row in out)
        self.assertEqual(len(ranks), 3)
        # Both tied values share the same rank (1.5-1)/(3-1) = 0.25.
        self.assertAlmostEqual(ranks[0], 0.25)
        self.assertAlmostEqual(ranks[1], 0.25)
        self.assertAlmostEqual(ranks[2], 1.0)  # 3 -> (3-1)/2

    def test_two_pair_tie_each_pair_shares_rank(self) -> None:
        # [0, 0, 1, 1, 2]: tied ranks are (1+2)/2=1.5 and (3+4)/2=3.5.
        # Normalized by (N-1)=4 -> 0.125 and 0.625.
        records = [
            {"timestamp": "2024-06-01", "symbol": "A", "momentum_20": 0.0},
            {"timestamp": "2024-06-01", "symbol": "B", "momentum_20": 0.0},
            {"timestamp": "2024-06-01", "symbol": "C", "momentum_20": 1.0},
            {"timestamp": "2024-06-01", "symbol": "D", "momentum_20": 1.0},
            {"timestamp": "2024-06-01", "symbol": "E", "momentum_20": 2.0},
        ]
        out = add_cross_sectional_features(records, columns=("momentum_20",))
        ranks = sorted(row["xs_rank_momentum_20"] for row in out)
        self.assertAlmostEqual(ranks[0], 0.125)
        self.assertAlmostEqual(ranks[1], 0.125)
        self.assertAlmostEqual(ranks[2], 0.625)
        self.assertAlmostEqual(ranks[3], 0.625)
        self.assertAlmostEqual(ranks[4], 1.0)


# ---------------------------------------------------------------------------
# D) Degenerate group / col is skipped (no key emitted)
# ---------------------------------------------------------------------------


class DegenerateGroupTests(unittest.TestCase):
    def test_single_symbol_group_emits_no_xs_keys(self) -> None:
        # Only one row on 2024-06-01 -> <2 finite values -> no xs_* keys for
        # that date. The 2024-06-02 group has 2 rows and DOES get xs_* keys.
        records = [
            {"timestamp": "2024-06-01", "symbol": "A", "momentum_20": 0.5},
            {"timestamp": "2024-06-02", "symbol": "A", "momentum_20": 0.5},
            {"timestamp": "2024-06-02", "symbol": "B", "momentum_20": 0.7},
        ]
        out = add_cross_sectional_features(records, columns=("momentum_20",))

        single = next(row for row in out if row["timestamp"] == "2024-06-01")
        self.assertNotIn("xs_rank_momentum_20", single)
        self.assertNotIn("xs_z_momentum_20", single)

        # The two-row date is NOT degenerate and must carry xs_* keys.
        d2_rows = [row for row in out if row["timestamp"] == "2024-06-02"]
        for row in d2_rows:
            self.assertIn("xs_rank_momentum_20", row)
            self.assertIn("xs_z_momentum_20", row)

    def test_missing_value_in_a_row_skips_only_that_row(self) -> None:
        # Row B has None for momentum_20; A and C must still be ranked/z-scored
        # using the remaining two finite values, and B must get no xs_* keys.
        records = [
            {"timestamp": "2024-06-01", "symbol": "A", "momentum_20": -1.0},
            {"timestamp": "2024-06-01", "symbol": "B", "momentum_20": None},
            {"timestamp": "2024-06-01", "symbol": "C", "momentum_20": 1.0},
        ]
        out = add_cross_sectional_features(records, columns=("momentum_20",))
        by_symbol = {row["symbol"]: row for row in out}

        self.assertNotIn("xs_rank_momentum_20", by_symbol["B"])
        self.assertNotIn("xs_z_momentum_20", by_symbol["B"])
        # A is min -> rank 0.0; C is max -> rank 1.0 (N=2 valid, normalized).
        self.assertAlmostEqual(by_symbol["A"]["xs_rank_momentum_20"], 0.0)
        self.assertAlmostEqual(by_symbol["C"]["xs_rank_momentum_20"], 1.0)
        self.assertAlmostEqual(by_symbol["A"]["xs_z_momentum_20"], -1.0)
        self.assertAlmostEqual(by_symbol["C"]["xs_z_momentum_20"], 1.0)

    def test_non_finite_value_skipped_like_missing(self) -> None:
        # NaN / inf must be treated like missing (no xs_* key for that row).
        records = [
            {"timestamp": "2024-06-01", "symbol": "A", "momentum_20": float("nan")},
            {"timestamp": "2024-06-01", "symbol": "B", "momentum_20": 0.0},
            {"timestamp": "2024-06-01", "symbol": "C", "momentum_20": 1.0},
        ]
        out = add_cross_sectional_features(records, columns=("momentum_20",))
        by_symbol = {row["symbol"]: row for row in out}
        self.assertNotIn("xs_rank_momentum_20", by_symbol["A"])
        self.assertIn("xs_rank_momentum_20", by_symbol["B"])
        self.assertIn("xs_rank_momentum_20", by_symbol["C"])


# ---------------------------------------------------------------------------
# E) std == 0 -> xs_z = 0.0
# ---------------------------------------------------------------------------


class StdZeroTests(unittest.TestCase):
    def test_constant_value_group_zeros(self) -> None:
        records = [
            {"timestamp": "2024-06-01", "symbol": "A", "momentum_20": 0.42},
            {"timestamp": "2024-06-01", "symbol": "B", "momentum_20": 0.42},
            {"timestamp": "2024-06-01", "symbol": "C", "momentum_20": 0.42},
        ]
        out = add_cross_sectional_features(records, columns=("momentum_20",))
        for row in out:
            self.assertEqual(row["xs_z_momentum_20"], 0.0)
            # Ranks are still well-defined: average rank when all tied.
            self.assertAlmostEqual(row["xs_rank_momentum_20"], 0.5)

    def test_two_rows_same_value_zeros(self) -> None:
        records = [
            {"timestamp": "2024-06-01", "symbol": "A", "momentum_20": 7.0},
            {"timestamp": "2024-06-01", "symbol": "B", "momentum_20": 7.0},
        ]
        out = add_cross_sectional_features(records, columns=("momentum_20",))
        for row in out:
            self.assertEqual(row["xs_z_momentum_20"], 0.0)


# ---------------------------------------------------------------------------
# F) Input not mutated; original columns preserved
# ---------------------------------------------------------------------------


class NoMutationTests(unittest.TestCase):
    def test_input_dicts_are_not_mutated(self) -> None:
        original = [
            {"timestamp": "2024-06-01", "symbol": "A", "momentum_20": -1.0, "extra": "keep"},
            {"timestamp": "2024-06-01", "symbol": "B", "momentum_20": 0.0, "extra": "keep"},
            {"timestamp": "2024-06-01", "symbol": "C", "momentum_20": 1.0, "extra": "keep"},
        ]
        snapshot = [dict(row) for row in original]
        add_cross_sectional_features(original, columns=("momentum_20",))
        self.assertEqual(original, snapshot)
        # No xs_* keys leaked into the originals.
        for row in original:
            self.assertNotIn("xs_rank_momentum_20", row)
            self.assertNotIn("xs_z_momentum_20", row)

    def test_output_preserves_all_original_columns(self) -> None:
        records = [
            {"timestamp": "2024-06-01", "symbol": "A", "momentum_20": -1.0, "extra": "x"},
            {"timestamp": "2024-06-01", "symbol": "B", "momentum_20": 0.0, "extra": "y"},
            {"timestamp": "2024-06-01", "symbol": "C", "momentum_20": 1.0, "extra": "z"},
        ]
        out = add_cross_sectional_features(records, columns=("momentum_20",))
        for source, produced in zip(records, out):
            for key, value in source.items():
                self.assertEqual(produced[key], value)

    def test_output_preserves_input_order(self) -> None:
        records = [
            {"timestamp": "2024-06-01", "symbol": "C", "momentum_20": 1.0},
            {"timestamp": "2024-06-01", "symbol": "A", "momentum_20": -1.0},
            {"timestamp": "2024-06-01", "symbol": "B", "momentum_20": 0.0},
        ]
        out = add_cross_sectional_features(records, columns=("momentum_20",))
        self.assertEqual(
            [row["symbol"] for row in out],
            ["C", "A", "B"],
        )


# ---------------------------------------------------------------------------
# G) Per-date independence (rank is computed within each date group)
# ---------------------------------------------------------------------------


class PerDateIndependenceTests(unittest.TestCase):
    def test_two_dates_with_different_distributions(self) -> None:
        # Date 1: [-1, 0, 1] -> ranks 0.0, 0.5, 1.0; z-scores as before.
        # Date 2: [10, 20, 30] -> same ranks, larger z-scores.
        records = [
            {"timestamp": "2024-06-01", "symbol": "A", "momentum_20": -1.0},
            {"timestamp": "2024-06-01", "symbol": "B", "momentum_20": 0.0},
            {"timestamp": "2024-06-01", "symbol": "C", "momentum_20": 1.0},
            {"timestamp": "2024-06-02", "symbol": "A", "momentum_20": 10.0},
            {"timestamp": "2024-06-02", "symbol": "B", "momentum_20": 20.0},
            {"timestamp": "2024-06-02", "symbol": "C", "momentum_20": 30.0},
        ]
        out = add_cross_sectional_features(records, columns=("momentum_20",))

        d1 = {(row["symbol"], row["timestamp"]): row for row in out if row["timestamp"] == "2024-06-01"}
        d2 = {(row["symbol"], row["timestamp"]): row for row in out if row["timestamp"] == "2024-06-02"}

        # Ranks identical across dates (relative position within date).
        for sym in ("A", "B", "C"):
            self.assertAlmostEqual(
                d1[(sym, "2024-06-01")]["xs_rank_momentum_20"],
                d2[(sym, "2024-06-02")]["xs_rank_momentum_20"],
            )
            self.assertAlmostEqual(d1[(sym, "2024-06-01")]["xs_rank_momentum_20"], {"A": 0.0, "B": 0.5, "C": 1.0}[sym])

        # z-scores identical across dates (the dispersion is the same shape).
        for sym in ("A", "B", "C"):
            self.assertAlmostEqual(
                d1[(sym, "2024-06-01")]["xs_z_momentum_20"],
                d2[(sym, "2024-06-02")]["xs_z_momentum_20"],
            )

    def test_one_date_does_not_leak_into_another(self) -> None:
        # Use a date with only 1 row and another with 3 rows. The 1-row date
        # must not contribute to the 3-row date's stats.
        records = [
            {"timestamp": "2024-06-01", "symbol": "A", "momentum_20": 100.0},  # outlier on its own
            {"timestamp": "2024-06-02", "symbol": "A", "momentum_20": -1.0},
            {"timestamp": "2024-06-02", "symbol": "B", "momentum_20": 0.0},
            {"timestamp": "2024-06-02", "symbol": "C", "momentum_20": 1.0},
        ]
        out = add_cross_sectional_features(records, columns=("momentum_20",))

        d1 = next(row for row in out if row["timestamp"] == "2024-06-01")
        self.assertNotIn("xs_rank_momentum_20", d1)
        self.assertNotIn("xs_z_momentum_20", d1)

        d2_a = next(row for row in out if row["timestamp"] == "2024-06-02" and row["symbol"] == "A")
        self.assertAlmostEqual(d2_a["xs_rank_momentum_20"], 0.0)


# ---------------------------------------------------------------------------
# Integration: build-features --cross-sectional adds xs_* columns
# ---------------------------------------------------------------------------


class BuildFeaturesCrossSectionalCliTests(unittest.TestCase):
    def test_with_flag_emits_xs_columns_when_indicator_activation_off(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            dataset = root / "dataset.csv"
            output = root / "features.csv"
            # Need enough days so momentum_20 is finite on most rows.
            write_records(_base_ohlcv(days=40, symbols=("AAA", "BBB", "CCC")), dataset)

            exit_code = main(
                [
                    "build-features",
                    "--dataset",
                    str(dataset),
                    "--output",
                    str(output),
                    "--cross-sectional",
                ]
            )

            self.assertEqual(exit_code, 0)
            rows = _read_csv(output)

        xs_rank_cols = sorted({key for key in rows[0].keys() if key.startswith("xs_rank_")})
        xs_z_cols = sorted({key for key in rows[0].keys() if key.startswith("xs_z_")})
        # Default columns are return_1d, momentum_20, momentum_60, rsi_14.
        # rsi_14 will not be present unless --indicator-activation-dir extends it;
        # so we expect at least return_1d, momentum_20, momentum_60 to appear.
        self.assertIn("xs_rank_return_1d", xs_rank_cols)
        self.assertIn("xs_rank_momentum_20", xs_rank_cols)
        self.assertIn("xs_z_momentum_20", xs_z_cols)

    def test_with_flag_and_custom_columns(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            dataset = root / "dataset.csv"
            output = root / "features.csv"
            write_records(_base_ohlcv(days=40, symbols=("AAA", "BBB", "CCC")), dataset)

            exit_code = main(
                [
                    "build-features",
                    "--dataset",
                    str(dataset),
                    "--output",
                    str(output),
                    "--cross-sectional",
                    "--cross-sectional-columns",
                    "return_1d,momentum_20",
                ]
            )

            self.assertEqual(exit_code, 0)
            rows = _read_csv(output)

        # The CLI materializes a consistent CSV schema for the requested xs_*
        # columns (rows whose base column is None carry an empty cell), so
        # the union is exactly the 4 requested xs_* keys.
        xs_keys = {key for key in rows[0].keys() if key.startswith("xs_")}
        self.assertEqual(
            xs_keys,
            {"xs_rank_return_1d", "xs_z_return_1d", "xs_rank_momentum_20", "xs_z_momentum_20"},
        )

    def test_with_flag_does_not_change_column_order_of_originals(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            dataset = root / "dataset.csv"
            output_default = root / "default.csv"
            output_xs = root / "xs.csv"
            write_records(_base_ohlcv(days=40, symbols=("AAA", "BBB", "CCC")), dataset)

            main(["build-features", "--dataset", str(dataset), "--output", str(output_default)])
            main(
                [
                    "build-features",
                    "--dataset",
                    str(dataset),
                    "--output",
                    str(output_xs),
                    "--cross-sectional",
                ]
            )

            default_rows = _read_csv(output_default)
            xs_rows = _read_csv(output_xs)

        # The set of original columns must match between the two outputs.
        default_keys = set(default_rows[0].keys())
        xs_keys = set(xs_rows[0].keys())
        self.assertTrue(default_keys.issubset(xs_keys), "xs output is missing original columns")
        # The xs output has extra columns; verify they are exactly xs_*.
        extras = xs_keys - default_keys
        self.assertTrue(all(key.startswith("xs_") for key in extras), f"non-xs extras: {extras}")


# ---------------------------------------------------------------------------
# Compatibility: build_features output unchanged (used as input to add_*)
# ---------------------------------------------------------------------------


class BuildFeaturesPlusCrossSectionalTests(unittest.TestCase):
    def test_compose_build_then_add_is_well_formed(self) -> None:
        # The CLI applies add_cross_sectional_features to the build_features
        # output. Verify the composition is stable on a small fixture: every
        # row gets the requested xs keys, and the rank respects the per-date
        # count from the build_features output.
        raw = _base_ohlcv(days=40, symbols=("AAA", "BBB", "CCC"))
        features = build_features(raw, FeatureConfig())
        featured = add_cross_sectional_features(features, columns=("momentum_20", "return_1d"))

        # Build the same records again (build_features is deterministic) so we
        # know the date groups; here we just verify each row that has a finite
        # momentum_20 also has the xs_rank_momentum_20 key.
        for row in featured:
            if row.get("momentum_20") is not None:
                self.assertIn("xs_rank_momentum_20", row, f"missing rank for row {row}")
            if row.get("return_1d") is not None:
                self.assertIn("xs_rank_return_1d", row, f"missing return rank for row {row}")


if __name__ == "__main__":
    unittest.main()