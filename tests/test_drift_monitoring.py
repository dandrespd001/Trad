import json
import tempfile
import unittest
from pathlib import Path
from typing import Any, cast

from trading_ai.cli import main
from trading_ai.monitoring.drift import evaluate_feature_drift


def feature_row(symbol: str, timestamp: str, **values: object) -> dict[str, Any]:
    row: dict[str, Any] = {
        "timestamp": timestamp,
        "symbol": symbol,
        "open": 100.0,
        "high": 101.0,
        "low": 99.0,
        "close": 100.5,
        "volume": 1000000,
    }
    row.update(values)
    return row


class DriftMonitoringTests(unittest.TestCase):
    def test_report_is_stable_when_reference_and_current_match(self) -> None:
        rows = [
            feature_row("SPY", f"2026-01-{day:02d}", momentum_20=float(day), realized_volatility_20=0.1 + day / 1000)
            for day in range(1, 31)
        ]

        report = cast(
            dict[str, Any],
            evaluate_feature_drift(
                rows,
                rows,
                generated_at="2026-06-16T00:00:00+00:00",
            ).to_dict(),
        )

        self.assertFalse(report["drift_detected"])
        self.assertEqual(report["summary"]["feature_count"], 2)
        self.assertEqual(report["summary"]["drifted_feature_count"], 0)
        self.assertEqual(warn_codes(report), set())

    def test_detects_mean_shift(self) -> None:
        reference = [
            feature_row("SPY", f"2026-01-{day:02d}", momentum_20=float(day), realized_volatility_20=0.2)
            for day in range(1, 31)
        ]
        current = [
            feature_row("SPY", f"2026-02-{day:02d}", momentum_20=float(day + 100), realized_volatility_20=0.2)
            for day in range(1, 31)
        ]

        report = cast(dict[str, Any], evaluate_feature_drift(reference, current).to_dict())

        self.assertTrue(report["drift_detected"])
        self.assertIn("mean_shift", warn_codes(report))
        self.assertIn("momentum_20", drifted_features(report))

    def test_detects_missingness_shift(self) -> None:
        reference = [feature_row("SPY", f"2026-01-{day:02d}", momentum_20=float(day)) for day in range(1, 31)]
        current = [
            feature_row("SPY", f"2026-02-{day:02d}", momentum_20="" if day <= 10 else float(day))
            for day in range(1, 31)
        ]

        report = cast(dict[str, Any], evaluate_feature_drift(reference, current).to_dict())

        self.assertTrue(report["drift_detected"])
        self.assertIn("missingness_shift", warn_codes(report))

    def test_default_feature_selection_ignores_identity_and_ohlcv_columns(self) -> None:
        rows = [feature_row("SPY", f"2026-01-{day:02d}", momentum_20=float(day)) for day in range(1, 31)]

        report = cast(dict[str, Any], evaluate_feature_drift(rows, rows).to_dict())

        self.assertEqual([metric["feature"] for metric in report["metrics"]], ["momentum_20"])

    def test_feature_names_limits_columns_evaluated(self) -> None:
        rows = [
            feature_row("SPY", f"2026-01-{day:02d}", momentum_20=float(day), momentum_2=float(day * 2))
            for day in range(1, 31)
        ]

        report = cast(dict[str, Any], evaluate_feature_drift(rows, rows, feature_names=("momentum_2",)).to_dict())

        self.assertEqual([metric["feature"] for metric in report["metrics"]], ["momentum_2"])

    def test_cli_writes_json_and_markdown_with_exit_zero(self) -> None:
        rows = [feature_row("SPY", f"2026-01-{day:02d}", momentum_20=float(day)) for day in range(1, 31)]
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            reference_path = write_csv(root / "reference.csv", rows)
            current_path = write_csv(root / "current.csv", rows)
            output = root / "drift.json"
            markdown = root / "drift.md"

            exit_code = main(
                [
                    "drift-report",
                    "--reference-features",
                    str(reference_path),
                    "--current-features",
                    str(current_path),
                    "--output",
                    str(output),
                    "--markdown-output",
                    str(markdown),
                ]
            )
            payload = json.loads(output.read_text(encoding="utf-8"))
            markdown_text = markdown.read_text(encoding="utf-8")

        self.assertEqual(exit_code, 0)
        self.assertFalse(payload["drift_detected"])
        self.assertIn("# Feature Drift Report", markdown_text)
        self.assertIn("momentum_20", markdown_text)

    def test_cli_returns_two_when_no_numeric_features_are_monitorable(self) -> None:
        rows = [
            {
                "timestamp": f"2026-01-{day:02d}",
                "symbol": "SPY",
                "label": "x",
            }
            for day in range(1, 31)
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            reference_path = write_csv(root / "reference.csv", rows)
            current_path = write_csv(root / "current.csv", rows)
            output = root / "drift.json"

            exit_code = main(
                [
                    "drift-report",
                    "--reference-features",
                    str(reference_path),
                    "--current-features",
                    str(current_path),
                    "--output",
                    str(output),
                ]
            )

        self.assertEqual(exit_code, 2)
        self.assertFalse(output.exists())


def write_csv(path: Path, rows: list[dict[str, Any]]) -> Path:
    fieldnames = list(rows[0].keys())
    lines = [",".join(fieldnames)]
    for row in rows:
        lines.append(",".join(str(row.get(field, "")) for field in fieldnames))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def warn_codes(report: dict[str, Any]) -> set[str]:
    return {
        str(finding["code"])
        for finding in report["findings"]  # type: ignore[index]
        if finding["severity"] == "warn"
    }


def drifted_features(report: dict[str, Any]) -> set[str]:
    return {
        str(metric["feature"])
        for metric in report["metrics"]  # type: ignore[index]
        if metric["drifted"]
    }


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------------------
# feature_drift_report() tests — pure pytest
# ---------------------------------------------------------------------------

import pytest
from trading_ai.monitoring.drift import feature_drift_report  # noqa: E402

_requires_scipy = pytest.mark.skipif(
    __import__("importlib.util", fromlist=["find_spec"]).find_spec("scipy") is None,
    reason="scipy not installed",
)


def _rows(feature: str, values: list[float]) -> list[dict]:
    return [{feature: v} for v in values]


@_requires_scipy
def test_drift_same_distribution_no_drift() -> None:
    vals = [float(i) for i in range(50)]
    ref = _rows("f1", vals)
    cur = _rows("f1", vals)
    result = feature_drift_report(ref, cur, features=["f1"])
    assert result["f1"]["drift_detected"] is False
    assert result["f1"]["ks_statistic"] is not None


@_requires_scipy
def test_drift_mean_shifted_detects_drift() -> None:
    ref = _rows("f1", [float(i) for i in range(50)])
    cur = _rows("f1", [float(i) + 100 for i in range(50)])
    result = feature_drift_report(ref, cur, features=["f1"], alpha=0.05)
    assert result["f1"]["drift_detected"] is True


@_requires_scipy
def test_drift_missing_feature_in_current() -> None:
    ref = _rows("f1", [float(i) for i in range(50)])
    cur = [{"other_feat": 1.0}] * 20
    result = feature_drift_report(ref, cur, features=["f1"])
    assert result["f1"]["drift_detected"] is True
    assert result["f1"]["ks_statistic"] == 1.0
    assert result["f1"]["current_mean"] is None


@_requires_scipy
def test_drift_insufficient_samples_no_drift_flagged(caplog: pytest.LogCaptureFixture) -> None:
    import logging
    ref = _rows("f1", [1.0, 2.0, 3.0])
    cur = _rows("f1", [4.0, 5.0, 6.0])
    with caplog.at_level(logging.WARNING):
        result = feature_drift_report(ref, cur, features=["f1"])
    assert result["f1"]["drift_detected"] is False
    assert result["f1"]["ks_statistic"] is None
    assert any("insufficient" in msg.lower() for msg in caplog.messages)


@_requires_scipy
def test_drift_alpha_strict_more_sensitive() -> None:
    ref = _rows("f1", [float(i) for i in range(50)])
    cur = _rows("f1", [float(i) + 2 for i in range(50)])
    strict = feature_drift_report(ref, cur, features=["f1"], alpha=0.5)
    assert strict["f1"]["drift_detected"] is True


@_requires_scipy
def test_drift_nan_in_reference_handled() -> None:
    ref = [{"f1": float("nan")}, {"f1": 1.0}] * 15
    cur = _rows("f1", [float(i) for i in range(30)])
    result = feature_drift_report(ref, cur, features=["f1"])
    assert "f1" in result


@_requires_scipy
def test_drift_nan_in_current_handled() -> None:
    ref = _rows("f1", [float(i) for i in range(30)])
    cur = [{"f1": float("nan")}] * 5 + _rows("f1", [float(i) for i in range(25)])
    result = feature_drift_report(ref, cur, features=["f1"])
    assert "f1" in result


@_requires_scipy
def test_drift_partial_feature_subset() -> None:
    ref = [{"f1": float(i), "f2": float(i) * 2} for i in range(50)]
    cur = [{"f1": float(i) + 50, "f2": float(i) * 2} for i in range(50)]
    result = feature_drift_report(ref, cur, features=["f1", "f2"])
    assert result["f1"]["drift_detected"] is True
    assert result["f2"]["drift_detected"] is False


@_requires_scipy
def test_drift_mean_shift_pct_computed() -> None:
    ref = _rows("f1", [100.0] * 50)
    cur = _rows("f1", [110.0] * 50)
    result = feature_drift_report(ref, cur, features=["f1"])
    shift = result["f1"]["mean_shift_pct"]
    assert shift is not None
    assert abs(float(str(shift)) - 10.0) < 0.1
