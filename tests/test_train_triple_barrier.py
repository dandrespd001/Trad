"""Tests for the ``train`` subcommand's --labeling triple_barrier opt-in (Sprint H2).

Drives the CLI end-to-end (no edits to internal modules) for the gating tests,
and exercises ``build_triple_barrier_examples`` directly for the deterministic
labeling rules. The pre-existing
``test_models_baseline.py::test_train_and_evaluate_cli_write_reproducible_run_files``
covers the default direction path and must remain untouched (also asserted by
``test_default_direction_keeps_legacy_schema``).
"""

from __future__ import annotations

import io
import json
import math
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from typing import Any

from trading_ai.cli import build_parser
from trading_ai.data.io import write_records
from trading_ai.features.engineering import FeatureConfig, build_features
from trading_ai.models.baseline import (
    build_supervised_examples,
    build_triple_barrier_examples,
    temporal_train_test_split,
)


# ---------------------------------------------------------------------------
# Reusable fixtures
# ---------------------------------------------------------------------------

# 18 features exactly as named in the H1 evidence (5-year extended set).
EXTENDED_18_FEATURES: tuple[str, ...] = (
    "return_1d",
    "momentum_20",
    "momentum_60",
    "momentum_120",
    "realized_volatility_20",
    "rolling_drawdown_20",
    "daily_range",
    "true_range",
    "atr_14",
    "relative_volume_20",
    "close_to_sma_20",
    "close_to_sma_60",
    "vol_adjusted_momentum_20",
    "vol_adjusted_momentum_60",
    "vol_adjusted_momentum_120",
    "rsi_14",
    "macd_hist",
    "bb_pct_b",
)


def _base_ohlcv(days: int) -> list[dict[str, Any]]:
    """Deterministic OHLCV fixture: monotonic uptrend with mild noise."""
    rows: list[dict[str, Any]] = []
    base_close = 100.0
    for index in range(days):
        month = 1 + (index // 28)
        day = (index % 28) + 1
        timestamp = f"2024-{month:02d}-{day:02d}"
        close = base_close + index * 0.5
        rows.append(
            {
                "timestamp": timestamp,
                "symbol": "SPY",
                "open": close - 0.2,
                "high": close + 1.0,
                "low": close - 1.0,
                "close": close,
                "volume": 1_000_000 + index * 1000,
            }
        )
    return rows


def _extended_feature_rows(days: int = 120) -> list[dict[str, Any]]:
    """Build rows that include all 18 extended-indicator feature columns."""
    return build_features(_base_ohlcv(days=days), FeatureConfig(rsi_window=14, macd_fast=12, bb_window=20))


# ---------------------------------------------------------------------------
# Direct library-level labeling tests (B/C/D/F) -- deterministic, no CLI.
# ---------------------------------------------------------------------------


def _records(closes: list[float], atr_value: float | None = 0.5) -> list[dict[str, Any]]:
    """Build a flat ``len(closes)``-long fixture with a uniform ``atr_14``.

    Pass ``atr_value=None`` for rows that must default to ``None`` (we then
    manually override the ``atr_14`` slot per row in the test). The function
    intentionally has no ``feature_names`` columns; ``build_triple_barrier_examples``
    accepts any iterable of mappings and filters via ``_extract_features``.
    """
    rows: list[dict[str, Any]] = []
    for index, close in enumerate(closes):
        month = 1 + (index // 28)
        day = (index % 28) + 1
        rows.append(
            {
                "timestamp": f"2024-{month:02d}-{day:02d}",
                "symbol": "SPY",
                "close": close,
                "atr_14": atr_value,
            }
        )
    return rows


class TripleBarrierLabelingTests(unittest.TestCase):
    """Direct unit coverage of ``build_triple_barrier_examples``.

    These tests pin the labeling semantics (López de Prado, close-only touch
    detection) without going through the CLI, so a regression in the labeling
    function itself is caught even if the CLI is wired correctly.
    """

    def test_monotonic_uptrend_with_small_vol_labels_all_labelable_one(self) -> None:
        closes = [100.0, 101.0, 102.0, 103.0, 104.0, 105.0]
        # horizon=2 skips i=4 and i=5 (last_labelable = 6-2-1=3); i=0..3 are
        # labelable. All four have close_j >= entry + 0.5 within the window.
        examples = build_triple_barrier_examples(
            _records(closes, atr_value=0.5),
            feature_names=(),
            horizon=2,
            atr_mult=1.0,
        )

        targets = [ex.target for ex in examples]
        self.assertEqual(len(examples), 4)
        self.assertEqual(targets, [1, 1, 1, 1])
        # Output must be sorted by (timestamp, symbol) like build_supervised_examples.
        self.assertEqual(
            [ex.timestamp for ex in examples],
            sorted(ex.timestamp for ex in examples),
        )

    def test_monotonic_downtrend_with_small_vol_labels_all_labelable_zero(self) -> None:
        closes = [105.0, 104.0, 103.0, 102.0, 101.0, 100.0]
        examples = build_triple_barrier_examples(
            _records(closes, atr_value=0.5),
            feature_names=(),
            horizon=2,
            atr_mult=1.0,
        )

        self.assertEqual(len(examples), 4)
        self.assertEqual([ex.target for ex in examples], [0, 0, 0, 0])

    def test_upper_touched_first_then_lower_labels_upper(self) -> None:
        # Both barriers are physically reachable inside the horizon. The
        # function must label by the FIRST j that hits a barrier (upper in
        # this case) and stop scanning from there.
        closes = [100.0, 100.0, 100.0, 110.0, 100.0, 100.0]
        examples = build_triple_barrier_examples(
            _records(closes, atr_value=1.0),
            feature_names=(),
            horizon=3,
            atr_mult=1.0,
        )

        self.assertEqual(len(examples), 3)
        self.assertEqual([ex.target for ex in examples], [1, 1, 1])

    def test_lower_touched_first_then_upper_labels_lower(self) -> None:
        # Mirror of the previous test: lower barrier is hit first regardless
        # of subsequent upper touches inside the same window.
        closes = [100.0, 100.0, 100.0, 90.0, 110.0, 110.0]
        examples = build_triple_barrier_examples(
            _records(closes, atr_value=1.0),
            feature_names=(),
            horizon=3,
            atr_mult=1.0,
        )

        self.assertEqual(len(examples), 3)
        self.assertEqual([ex.target for ex in examples], [0, 0, 0])

    def test_timeout_label_uses_close_at_end_of_horizon_vs_entry(self) -> None:
        # Barriers are absurdly wide (100) so neither upper nor lower can be
        # touched inside the horizon. The function MUST fall back to the sign
        # of close_{i+horizon} - entry.
        closes = [100.0, 100.0, 100.0, 105.0, 105.0, 105.0]
        examples = build_triple_barrier_examples(
            _records(closes, atr_value=100.0),
            feature_names=(),
            horizon=2,
            atr_mult=1.0,
        )

        self.assertEqual(len(examples), 4)
        # last_labelable = 6-2-1=3 (i=0..3 are labelable).
        # i=0: j in [1,2], all 100 -> int(close[2]=100 > entry=100) = 0
        # i=1: j in [2,3], all 100/105 -> int(close[3]=105 > entry=100) = 1
        # i=2: j in [3,4], 105/105 -> int(close[4]=105 > entry=100) = 1
        # i=3: j in [4,5], all 105 -> int(close[5]=105 > entry=105) = 0
        self.assertEqual([ex.target for ex in examples], [0, 1, 1, 0])

    def test_tail_without_full_lookahead_is_dropped(self) -> None:
        # 4 rows with horizon=3: last_labelable = 4-3-1=0. Only i=0 is
        # labelable; rows i=1,2,3 sit inside the [i+1, i+horizon] window but
        # i+horizon falls off the end. The function must drop i=1..3 and
        # NEVER raise.
        closes = [100.0, 101.0, 102.0, 103.0]
        examples = build_triple_barrier_examples(
            _records(closes, atr_value=0.5),
            feature_names=(),
            horizon=3,
            atr_mult=1.0,
        )

        self.assertEqual(len(examples), 1)
        self.assertEqual(examples[0].target, 1)

    def test_missing_or_non_positive_vol_skips_example(self) -> None:
        # Mix of valid and invalid ``atr_14`` values: None, "", 0.0, -1.0,
        # NaN, inf are all reasons to skip the example (we cannot scale the
        # barriers without a finite positive unit). Valid rows still come
        # through.
        rows = [
            {"timestamp": "2024-01-01", "symbol": "SPY", "close": 100.0, "atr_14": 0.5},
            {"timestamp": "2024-01-02", "symbol": "SPY", "close": 100.5, "atr_14": None},
            {"timestamp": "2024-01-03", "symbol": "SPY", "close": 101.0, "atr_14": ""},
            {"timestamp": "2024-01-04", "symbol": "SPY", "close": 101.5, "atr_14": 0.0},
            {"timestamp": "2024-01-05", "symbol": "SPY", "close": 102.0, "atr_14": -1.0},
            {"timestamp": "2024-01-06", "symbol": "SPY", "close": 102.5, "atr_14": float("nan")},
            {"timestamp": "2024-01-07", "symbol": "SPY", "close": 103.0, "atr_14": float("inf")},
            {"timestamp": "2024-01-08", "symbol": "SPY", "close": 103.5, "atr_14": "not_a_number"},
            {"timestamp": "2024-01-09", "symbol": "SPY", "close": 104.0, "atr_14": 0.5},
            {"timestamp": "2024-01-10", "symbol": "SPY", "close": 104.5, "atr_14": 0.5},
            {"timestamp": "2024-01-11", "symbol": "SPY", "close": 105.0, "atr_14": 0.5},
        ]
        # horizon=2 -> last_labelable = 11-2-1 = 8, so indices 0..8 are
        # labelable. Indices 1..7 carry invalid ``atr_14`` and MUST be
        # dropped. Only indices 0 and 8 (both with atr=0.5) survive.
        examples = build_triple_barrier_examples(
            rows,
            feature_names=(),
            horizon=2,
            atr_mult=1.0,
        )

        self.assertEqual(len(examples), 2)
        self.assertEqual([ex.timestamp for ex in examples], ["2024-01-01", "2024-01-09"])
        self.assertEqual([ex.target for ex in examples], [1, 1])

    def test_uses_high_low_not_at_all_in_label_detection(self) -> None:
        # The spec REQUIRES close-only touch detection. We set ``low`` and
        # ``high`` so a high/low-based detector would flip the label, but a
        # close-only detector (which uses only ``close``) must label 0.
        # i=0 has low=80 (would touch lower by low detector) but close=100.
        # With horizons sized so neither barrier is touched by close within
        # the window, the label must come from the time-out rule.
        rows = [
            {"timestamp": "2024-01-01", "symbol": "SPY",
             "close": 100.0, "high": 200.0, "low": 80.0, "atr_14": 0.5},
            {"timestamp": "2024-01-02", "symbol": "SPY",
             "close": 100.0, "high": 200.0, "low": 80.0, "atr_14": 0.5},
            {"timestamp": "2024-01-03", "symbol": "SPY",
             "close": 100.0, "high": 200.0, "low": 80.0, "atr_14": 0.5},
            {"timestamp": "2024-01-04", "symbol": "SPY",
             "close": 100.0, "high": 200.0, "low": 80.0, "atr_14": 0.5},
        ]
        # horizon=2 -> barriers entry±0.5 = [99.5, 100.5]. close=100.0 forever
        # -> neither barrier touched by close, so time-out: close[2]=100 > 100 = 0.
        examples = build_triple_barrier_examples(
            rows,
            feature_names=(),
            horizon=2,
            atr_mult=1.0,
        )
        self.assertEqual(len(examples), 2)
        self.assertEqual([ex.target for ex in examples], [0, 0])

    def test_invalid_args_raise_value_error(self) -> None:
        records = _records([100.0, 101.0, 102.0], atr_value=0.5)
        with self.assertRaises(ValueError):
            build_triple_barrier_examples(
                records, feature_names=(), horizon=0, atr_mult=1.0
            )
        with self.assertRaises(ValueError):
            build_triple_barrier_examples(
                records, feature_names=(), horizon=-1, atr_mult=1.0
            )
        with self.assertRaises(ValueError):
            build_triple_barrier_examples(
                records, feature_names=(), horizon=2, atr_mult=0.0
            )
        with self.assertRaises(ValueError):
            build_triple_barrier_examples(
                records, feature_names=(), horizon=2, atr_mult=-0.5
            )


# ---------------------------------------------------------------------------
# End-to-end CLI coverage (A/E/G) and embargo plumbing.
# ---------------------------------------------------------------------------


class TrainTripleBarrierCliTests(unittest.TestCase):
    """End-to-end CLI coverage for the ``--labeling triple_barrier`` opt-in."""

    def _run_train(
        self,
        dataset: Path,
        model_output: Path,
        run_output: Path,
        *extra: str,
    ) -> tuple[int, str]:
        argv = [
            "train",
            "--model",
            "logistic-baseline",
            "--dataset",
            str(dataset),
            "--output",
            str(model_output),
            "--run-output",
            str(run_output),
            *extra,
        ]
        parser = build_parser()
        args = parser.parse_args(argv)
        err_buffer = io.StringIO()
        with redirect_stderr(err_buffer):
            exit_code = args.func(args)
        return exit_code, err_buffer.getvalue()

    # ------------------------------------------------------------------
    # A) Default direction = byte-idéntico
    # ------------------------------------------------------------------

    def test_default_direction_keeps_legacy_schema(self) -> None:
        """Default path: ``train`` without ``--labeling`` records ``labeling``
        == ``"direction"`` and NO triple-barrier-only keys. The model artifact
        is unchanged (legacy pre-H2 schema).
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            dataset = root / "features.csv"
            model_output = root / "model.json"
            run_output = root / "run.json"
            write_records(_extended_feature_rows(), dataset)

            exit_code, stderr = self._run_train(dataset, model_output, run_output)
            self.assertEqual(exit_code, 0, msg=stderr)

            run_payload = json.loads(run_output.read_text(encoding="utf-8"))
            model_payload = json.loads(model_output.read_text(encoding="utf-8"))

        self.assertEqual(run_payload["labeling"], "direction")
        # Direction-mode run payloads must NOT carry the triple-barrier keys
        # (legacy hashes / downstream assertions stay stable).
        for extra_key in ("label_horizon", "label_atr_mult", "vol_column", "label_positive_rate"):
            self.assertNotIn(
                extra_key,
                run_payload,
                msg=f"direction-mode run leaked triple-barrier key {extra_key!r}",
            )
        # The model artifact schema is unchanged regardless of labeling.
        self.assertNotIn("feature_means", model_payload)
        self.assertNotIn("feature_stds", model_payload)
        for required_key in ("feature_names", "intercept", "coefficients"):
            self.assertIn(required_key, model_payload)

    # ------------------------------------------------------------------
    # E) Embargo aplicado
    # ------------------------------------------------------------------

    def test_triple_barrier_purges_horizon_examples_at_train_test_boundary(self) -> None:
        """When the labeling is triple_barrier, the function MUST pass
        ``embargo=horizon`` to ``temporal_train_test_split``. Without the
        embargo the trainer would memorize the labels of the bars whose
        close it is about to be asked to predict (look-ahead leakage).
        """
        # 120 bars feature-engineered so each ``record`` carries a valid
        # ``atr_14``. horizon=5, so labeling creates one example per row
        # except the last ``horizon`` rows (skipped due to the lookahead
        # window). Whatever the exact counts, the embargo must drop exactly
        # ``horizon`` examples from the end of the train set vs. embargo=0.
        records = _extended_feature_rows(days=120)
        feature_names = ("return_1d", "momentum_20", "atr_14")
        horizon = 5
        examples = build_triple_barrier_examples(
            records, feature_names=feature_names, horizon=horizon, atr_mult=1.0
        )
        self.assertGreater(
            len(examples), 20, msg="fixture too small to exercise the embargo"
        )

        no_embargo = temporal_train_test_split(examples, test_fraction=0.25, embargo=0)
        embargoed = temporal_train_test_split(
            examples, test_fraction=0.25, embargo=horizon
        )

        # Same test set (the embargo only purges train at the boundary).
        self.assertEqual(embargoed.test, no_embargo.test)
        # Exactly ``horizon`` extra rows dropped from the END of train.
        self.assertEqual(len(no_embargo.train) - len(embargoed.train), horizon)
        # And the dropped rows are exactly the last ``horizon`` of no_embargo.train.
        self.assertEqual(embargoed.train, no_embargo.train[:-horizon])

    def test_triple_barrier_cli_invokes_embargo_in_run(self) -> None:
        """End-to-end: with ``--labeling triple_barrier`` and ``--label-horizon
        5``, the manifest's train range is exactly ``horizon`` bars earlier
        than the unflagged version (using the same feature set/dataset).
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            dataset = root / "features.csv"
            write_records(_extended_feature_rows(), dataset)
            feature_names = ("return_1d", "momentum_20", "atr_14")
            extra = [
                "--feature-names",
                ",".join(feature_names),
                "--labeling",
                "triple_barrier",
                "--label-horizon",
                "5",
                "--label-atr-mult",
                "1.0",
            ]
            tb_model = root / "tb_model.json"
            tb_run = root / "tb_run.json"
            self.assertEqual(self._run_train(dataset, tb_model, tb_run, *extra)[0], 0)
            direction_model = root / "d_model.json"
            direction_run = root / "d_run.json"
            self.assertEqual(
                self._run_train(
                    dataset,
                    direction_model,
                    direction_run,
                    "--feature-names",
                    ",".join(feature_names),
                )[0],
                0,
            )

            tb_payload = json.loads(tb_run.read_text(encoding="utf-8"))
            d_payload = json.loads(direction_run.read_text(encoding="utf-8"))

        # The triple-barrier run must declare the new keys; the direction
        # run must NOT.
        self.assertEqual(tb_payload["labeling"], "triple_barrier")
        self.assertEqual(tb_payload["label_horizon"], 5)
        self.assertEqual(tb_payload["label_atr_mult"], 1.0)
        self.assertEqual(tb_payload["vol_column"], "atr_14")
        self.assertIn("label_positive_rate", tb_payload)
        self.assertGreaterEqual(tb_payload["label_positive_rate"], 0.0)
        self.assertLessEqual(tb_payload["label_positive_rate"], 1.0)
        for extra_key in ("label_horizon", "label_atr_mult", "vol_column", "label_positive_rate"):
            self.assertNotIn(extra_key, d_payload)
        self.assertEqual(d_payload["labeling"], "direction")

        # Defense against a no-op wiring: the train-end timestamps MUST differ,
        # proving the embargo actually shrank the train set.
        self.assertLess(
            tb_payload["train_range"][1],
            d_payload["train_range"][1],
            msg="triple_barrier train_range end must end strictly earlier than direction",
        )

    # ------------------------------------------------------------------
    # G) Evidencia 5y (skipUnless)
    # ------------------------------------------------------------------

    @unittest.skipUnless(
        Path("reports/tmp/train/features_5y_ext.csv").exists(),
        "5y extended CSV not present in repo (only generated during evidence runs)",
    )
    def test_5y_triple_barrier_runs_and_records_positive_rate(self) -> None:
        """End-to-end smoke: with the 18-feature extended dataset, the CLI
        with ``--labeling triple_barrier --label-horizon 5 --label-atr-mult
        1.0 --standardize-features`` must produce a run with FINITE metrics
        AND a non-missing ``label_positive_rate``. H2 is an EVIDENCE sprint:
        no accuracy gate is imposed here.
        """
        dataset = Path("reports/tmp/train/features_5y_ext.csv")
        self.assertTrue(dataset.exists(), msg=f"missing fixture: {dataset}")
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            model_output = root / "model.json"
            run_output = root / "run.json"

            exit_code, stderr = self._run_train(
                dataset,
                model_output,
                run_output,
                "--feature-names",
                ",".join(EXTENDED_18_FEATURES),
                "--standardize-features",
                "--labeling",
                "triple_barrier",
                "--label-horizon",
                "5",
                "--label-atr-mult",
                "1.0",
            )
            self.assertEqual(exit_code, 0, msg=stderr)

            run_payload = json.loads(run_output.read_text(encoding="utf-8"))

        # The run must be self-describing.
        self.assertEqual(run_payload["labeling"], "triple_barrier")
        self.assertEqual(run_payload["standardized"], True)
        self.assertEqual(run_payload["label_horizon"], 5)
        self.assertEqual(run_payload["label_atr_mult"], 1.0)
        self.assertEqual(run_payload["vol_column"], "atr_14")
        # Positive rate is a float in [0, 1] and finite.
        rate = run_payload["label_positive_rate"]
        self.assertTrue(isinstance(rate, float))
        self.assertTrue(math.isfinite(rate))
        self.assertGreaterEqual(rate, 0.0)
        self.assertLessEqual(rate, 1.0)
        # Test and walk-forward metrics must be finite (no NaNs/inf).
        for section in ("test", "walk_forward"):
            section_metrics = run_payload["metrics"][section]
            for key, value in section_metrics.items():
                if isinstance(value, float):
                    self.assertTrue(
                        math.isfinite(value),
                        msg=f"non-finite metric {section}.{key}={value}",
                    )


if __name__ == "__main__":
    unittest.main()
