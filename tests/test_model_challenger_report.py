import json
import tempfile
import unittest
from pathlib import Path

from trading_ai.cli import build_parser, main


class ModelChallengerReportTests(unittest.TestCase):
    def test_parser_defaults_for_model_challenger_report(self) -> None:
        args = build_parser().parse_args(["model-challenger-report", "--evaluation-dir", "run"])

        self.assertEqual(args.evaluation_dir, "run")
        self.assertIsNone(args.paper_performance)
        self.assertIsNone(args.mlflow_review)
        self.assertIsNone(args.phase_review)
        self.assertIsNone(args.training_cycle)
        self.assertEqual(args.output_dir, "reports/tmp/model_challenger")

    def test_robust_candidate_with_compatible_paper_is_reviewable_without_model_mutation(self) -> None:
        latest_model_before = Path("models/latest_model.json").read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            evaluation_dir = write_evaluation(root / "evaluation")
            performance = write_paper_performance(root / "paper_performance.json")
            output_dir = root / "model_challenger"

            exit_code = main(
                [
                    "model-challenger-report",
                    "--evaluation-dir",
                    str(evaluation_dir),
                    "--paper-performance",
                    str(performance),
                    "--phase-review",
                    str(write_phase(root / "phase.json", ready=True)),
                    "--training-cycle",
                    str(write_training_cycle(root / "cycle.json", state="CANDIDATE_REVIEWABLE")),
                    "--output-dir",
                    str(output_dir),
                ]
            )
            payload = read_json(output_dir / "challenger_report.json")
            markdown = (output_dir / "challenger_report.md").read_text(encoding="utf-8")

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["status"], "REVIEWABLE")
        self.assertTrue(payload["evidence"]["paper_performance"]["compatible"])
        self.assertEqual(payload["candidate_quality"]["paper_compatibility"], "PASS")
        self.assertEqual(payload["candidate_quality"]["drift"], "PASS")
        self.assertFalse(payload["authority"]["mutates_latest_model"])
        self.assertFalse(payload["authority"]["automatic_champion_replacement"])
        self.assertIn("Status: **REVIEWABLE**", markdown)
        self.assertEqual(Path("models/latest_model.json").read_text(encoding="utf-8"), latest_model_before)

    def test_leakage_cost_or_drawdown_rejects_candidate(self) -> None:
        cases = (
            {"promotion_reasons": ["temporal_leakage_detected"]},
            {"cost_net_cagr": -0.01},
            {"max_drawdown": -0.75},
        )
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                evaluation_dir = write_evaluation(root / "evaluation", **case)
                performance = write_paper_performance(root / "paper_performance.json")
                output_dir = root / "model_challenger"

                exit_code = main(
                    [
                        "model-challenger-report",
                        "--evaluation-dir",
                        str(evaluation_dir),
                        "--paper-performance",
                        str(performance),
                        "--output-dir",
                        str(output_dir),
                    ]
                )
                payload = read_json(output_dir / "challenger_report.json")

            self.assertEqual(exit_code, 1)
            self.assertEqual(payload["status"], "REJECTED")

    def test_missing_required_artifacts_returns_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            evaluation_dir = root / "evaluation"
            evaluation_dir.mkdir()
            output_dir = root / "model_challenger"

            exit_code = main(
                [
                    "model-challenger-report",
                    "--evaluation-dir",
                    str(evaluation_dir),
                    "--output-dir",
                    str(output_dir),
                ]
            )
            payload = read_json(output_dir / "challenger_report.json")

        self.assertEqual(exit_code, 2)
        self.assertEqual(payload["status"], "ERROR")
        self.assertIn("missing_evaluation_summary", blocker_codes(payload))

    def test_missing_mlflow_review_is_optional(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            evaluation_dir = write_evaluation(root / "evaluation")
            performance = write_paper_performance(root / "paper_performance.json")
            output_dir = root / "model_challenger"

            exit_code = main(
                [
                    "model-challenger-report",
                    "--evaluation-dir",
                    str(evaluation_dir),
                    "--paper-performance",
                    str(performance),
                    "--mlflow-review",
                    str(root / "missing_mlflow_review.json"),
                    "--output-dir",
                    str(output_dir),
                ]
            )
            payload = read_json(output_dir / "challenger_report.json")

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["status"], "REVIEWABLE")
        self.assertEqual(payload["evidence"]["mlflow_review"]["status"], "MISSING_OPTIONAL")

    def test_phase_training_cycle_drift_and_paper_critical_block_challenger_v2(self) -> None:
        cases = (
            ("phase_not_ready", {"phase_ready": False}, "phase_review_not_ready"),
            ("training_not_reviewable", {"training_state": "BLOCKED"}, "training_cycle_not_reviewable"),
            ("drift_critical", {"drift_status": "CRITICAL"}, "drift_critical"),
            ("paper_critical", {"paper_status": "CRITICAL"}, "paper_performance_critical"),
        )
        for _name, options, expected_code in cases:
            with self.subTest(expected_code=expected_code), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                evaluation_dir = write_evaluation(root / "evaluation")
                if options.get("drift_status"):
                    write_json(evaluation_dir / "drift_report.json", {"status": options["drift_status"], "blockers": []})
                performance = write_paper_performance(root / "paper_performance.json", status=str(options.get("paper_status") or "OK"))
                output_dir = root / "model_challenger"

                exit_code = main(
                    [
                        "model-challenger-report",
                        "--evaluation-dir",
                        str(evaluation_dir),
                        "--paper-performance",
                        str(performance),
                        "--phase-review",
                        str(write_phase(root / "phase.json", ready=bool(options.get("phase_ready", True)))),
                        "--training-cycle",
                        str(write_training_cycle(root / "cycle.json", state=str(options.get("training_state") or "CANDIDATE_REVIEWABLE"))),
                        "--output-dir",
                        str(output_dir),
                    ]
                )
                payload = read_json(output_dir / "challenger_report.json")

            self.assertEqual(exit_code, 1)
            self.assertEqual(payload["status"], "BLOCKED")
            self.assertIn(expected_code, blocker_codes(payload))


def write_evaluation(
    path: Path,
    *,
    promotion_reasons: list[str] | None = None,
    cost_net_cagr: float = 0.11,
    max_drawdown: float = -0.08,
) -> Path:
    path.mkdir(parents=True)
    reasons = promotion_reasons or []
    metrics = {
        "trade_count": 12,
        "max_drawdown": max_drawdown,
        "estimated_costs": 0.01,
        "cagr": 0.12,
        "sample_count": 64,
        "accuracy_lift": 0.05,
    }
    artifacts = {
        "promotion_decision": {"path": str(path / "promotion_decision.json")},
        "walk_forward": {"path": str(path / "walk_forward.json")},
        "regime_slices": {"path": str(path / "regime_slices.json")},
    }
    write_json(
        path / "evaluation_summary.json",
        {
            "schema_version": 1,
            "status": "APPROVED" if not reasons and cost_net_cagr >= 0 and abs(max_drawdown) <= 0.5 else "REJECTED",
            "eligible_for_paper_challenger": not reasons and cost_net_cagr >= 0 and abs(max_drawdown) <= 0.5,
            "reasons": reasons,
            "metrics": metrics,
            "artifacts": artifacts,
        },
    )
    write_json(
        path / "promotion_decision.json",
        {
            "schema_version": 1,
            "eligible_for_paper_challenger": not reasons and cost_net_cagr >= 0 and abs(max_drawdown) <= 0.5,
            "approved": not reasons and cost_net_cagr >= 0 and abs(max_drawdown) <= 0.5,
            "reasons": reasons,
            "costs": {"net_cagr_after_estimated_costs": cost_net_cagr, "estimated_costs": 0.01},
            "robustness": {"backtest": {"trade_count": 12, "max_drawdown": max_drawdown}},
            "authority": {"mutates_latest_model": False, "automatic_champion_replacement": False},
        },
    )
    write_json(
        path / "walk_forward.json",
        {"schema_version": 1, "summary": {"window_count": 3, "robust_lift": True, "accuracy_lift": 0.05}},
    )
    write_json(path / "regime_slices.json", {"schema_version": 1, "summary": {"slice_count": 4}, "slices": []})
    return path


def write_paper_performance(path: Path, *, status: str = "OK") -> Path:
    write_json(
        path,
        {
            "schema_version": "1.0",
            "status": status,
            "paper_metrics": {
                "fills": 5,
                "pending_closeouts": 0,
                "unmatched_closeouts": 0,
                "rejections": 0,
                "pnl": {"source": "broker_statement", "broker_statement": True, "realized_pnl": 0.5},
            },
            "warnings": [],
            "blockers": [],
        },
    )
    return path


def write_phase(path: Path, *, ready: bool) -> Path:
    return write_json(
        path,
        {
            "status": "OK" if ready else "WARN",
            "phase_status": "READY_FOR_REVIEW" if ready else "ACCUMULATING",
            "review_only": True,
            "safety": {"paper_only": True, "live_trading_authorized": False},
        },
    )


def write_training_cycle(path: Path, *, state: str) -> Path:
    return write_json(
        path,
        {
            "status": "OK" if state == "CANDIDATE_REVIEWABLE" else "BLOCKED",
            "training_state": state,
            "review_only": True,
            "model_mutated": False,
            "live_trading_authorized": False,
            "candidate_quality": {"net_lift": "PASS", "walk_forward": "PASS", "regime_robustness": "PASS"},
            "safety": {"paper_only": True, "broker_client_built": False},
        },
    )


def write_json(path: Path, payload: dict[str, object]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return path


def read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def blocker_codes(payload: dict[str, object]) -> set[str]:
    return {str(blocker["code"]) for blocker in payload["blockers"]}


if __name__ == "__main__":
    unittest.main()
