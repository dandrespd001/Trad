import json
import tempfile
import unittest
from pathlib import Path

from trading_ai.cli import main


class AiFeatureAttributionTests(unittest.TestCase):
    def test_report_marks_ready_when_ai_candidate_improves_risk_adjusted_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            baseline = write_ranking(
                root / "baseline.json",
                best_candidate_id="logreg_current_features",
                candidates=[
                    candidate(
                        "logreg_current_features",
                        features=["momentum_20", "realized_volatility_20"],
                        sharpe=0.75,
                        calmar=0.40,
                        max_drawdown=-0.08,
                        estimated_costs=0.003,
                    )
                ],
            )
            ai = write_ranking(
                root / "ai.json",
                best_candidate_id="logreg_ai_features",
                feature_sources={"ai_features": [{"path": "ai_features.csv", "dataset_hash": "a" * 64}]},
                candidates=[
                    candidate(
                        "logreg_current_features",
                        features=["momentum_20", "realized_volatility_20"],
                        sharpe=0.76,
                        calmar=0.42,
                        max_drawdown=-0.08,
                        estimated_costs=0.003,
                    ),
                    candidate(
                        "logreg_ai_features",
                        features=["momentum_20", "ai_sentiment_1d", "ai_risk_1d"],
                        sharpe=0.94,
                        calmar=0.62,
                        max_drawdown=-0.075,
                        estimated_costs=0.0035,
                    ),
                ],
            )

            exit_code = main(
                [
                    "ai-feature-attribution-report",
                    "--as-of-date",
                    "2026-06-18",
                    "--baseline-ranking",
                    str(baseline),
                    "--ai-ranking",
                    str(ai),
                    "--output-dir",
                    str(root / "out"),
                    "--min-sharpe-delta",
                    "0.05",
                ]
            )
            report = json.loads((root / "out" / "2026-06-18" / "ai_feature_attribution.json").read_text())

        self.assertEqual(exit_code, 0)
        self.assertEqual(report["status"], "AI_VALUE_READY")
        self.assertEqual(report["baseline_candidate_id"], "logreg_current_features")
        self.assertEqual(report["best_ai_candidate_id"], "logreg_ai_features")
        self.assertGreater(report["best_ai_delta"]["sharpe"], 0.05)
        self.assertEqual(report["incremental_value"]["decision"], "ACCEPT_FOR_PAPER_EVIDENCE")
        self.assertRegex(report["input_hashes"]["baseline_ranking_sha256"], r"^[0-9a-f]{64}$")
        self.assertRegex(report["input_hashes"]["ai_ranking_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(report["authority"]["llm_authority"], "none")
        self.assertFalse(report["safety"]["orders_submitted"])

    def test_report_marks_insufficient_when_ai_candidate_does_not_clear_threshold(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            baseline = write_ranking(
                root / "baseline.json",
                best_candidate_id="logreg_current_features",
                candidates=[
                    candidate(
                        "logreg_current_features",
                        features=["momentum_20"],
                        sharpe=0.80,
                        calmar=0.40,
                        max_drawdown=-0.07,
                        estimated_costs=0.003,
                    )
                ],
            )
            ai = write_ranking(
                root / "ai.json",
                best_candidate_id="logreg_ai_features",
                feature_sources={"ai_features": [{"path": "ai_features.csv", "dataset_hash": "b" * 64}]},
                candidates=[
                    candidate(
                        "logreg_ai_features",
                        features=["momentum_20", "ai_confidence_1d"],
                        sharpe=0.82,
                        calmar=0.41,
                        max_drawdown=-0.071,
                        estimated_costs=0.003,
                    )
                ],
            )

            exit_code = main(
                [
                    "ai-feature-attribution-report",
                    "--as-of-date",
                    "2026-06-18",
                    "--baseline-ranking",
                    str(baseline),
                    "--ai-ranking",
                    str(ai),
                    "--output-dir",
                    str(root / "out"),
                    "--min-sharpe-delta",
                    "0.05",
                ]
            )
            report = json.loads((root / "out" / "2026-06-18" / "ai_feature_attribution.json").read_text())

        self.assertEqual(exit_code, 1)
        self.assertEqual(report["status"], "AI_VALUE_INSUFFICIENT")
        self.assertIn("ai_candidate_did_not_clear_thresholds", report["blockers"])
        self.assertEqual(report["incremental_value"]["decision"], "REJECT_FOR_PAPER_EVIDENCE")

    def test_report_blocks_when_ranking_dates_do_not_match(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            baseline = write_ranking(
                root / "baseline.json",
                as_of_date="2026-06-17",
                best_candidate_id="logreg_current_features",
                candidates=[candidate("logreg_current_features", features=["momentum_20"], sharpe=0.5)],
            )
            ai = write_ranking(
                root / "ai.json",
                best_candidate_id="logreg_ai_features",
                candidates=[candidate("logreg_ai_features", features=["momentum_20", "ai_sentiment_1d"], sharpe=0.8)],
            )

            exit_code = main(
                [
                    "ai-feature-attribution-report",
                    "--as-of-date",
                    "2026-06-18",
                    "--baseline-ranking",
                    str(baseline),
                    "--ai-ranking",
                    str(ai),
                    "--output-dir",
                    str(root / "out"),
                ]
            )
            report = json.loads((root / "out" / "2026-06-18" / "ai_feature_attribution.json").read_text())

        self.assertEqual(exit_code, 1)
        self.assertEqual(report["status"], "BLOCKED")
        self.assertIn("baseline_as_of_date_mismatch", report["blockers"])
        self.assertFalse(report["safety"]["broker_client_built"])


def write_ranking(
    path: Path,
    *,
    as_of_date: str = "2026-06-18",
    best_candidate_id: str,
    candidates: list[dict[str, object]],
    feature_sources: dict[str, object] | None = None,
) -> Path:
    payload = {
        "schema_version": 1,
        "status": "CANDIDATE_READY",
        "approved_dataset": {
            "dataset_id": "core_etfs",
            "frequency": "1d",
            "as_of_date": as_of_date,
            "window_start": "2026-03-01",
            "window_end": as_of_date,
            "dataset_hash": "0" * 64,
        },
        "feature_sources": feature_sources or {},
        "candidates": candidates,
        "best_candidate_id": best_candidate_id,
        "safety": {"orders_submitted": False, "live_trading_authorized": False},
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return path


def candidate(
    candidate_id: str,
    *,
    features: list[str],
    sharpe: float,
    calmar: float = 0.0,
    max_drawdown: float = -0.05,
    estimated_costs: float = 0.001,
) -> dict[str, object]:
    return {
        "candidate_id": candidate_id,
        "status": "OK",
        "features": features,
        "feature_names": features,
        "metrics": {
            "sharpe": sharpe,
            "calmar": calmar,
            "max_drawdown": max_drawdown,
            "estimated_costs": estimated_costs,
            "turnover": 0.2,
            "trade_count": 4,
        },
        "score": sharpe,
    }
