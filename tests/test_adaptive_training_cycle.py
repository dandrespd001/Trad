import json
import tempfile
import unittest
from pathlib import Path

from trading_ai.cli import build_parser, main


class AdaptiveTrainingCycleTests(unittest.TestCase):
    def test_parser_defaults_for_adaptive_training_cycle(self) -> None:
        args = build_parser().parse_args(
            [
                "adaptive-training-cycle",
                "--as-of-date",
                "2026-06-16",
                "--approved-dir",
                "approved",
                "--phase-review",
                "phase.json",
                "--paper-performance",
                "performance.json",
                "--registry-dir",
                "registry",
            ]
        )

        self.assertEqual(args.as_of_date, "2026-06-16")
        self.assertEqual(args.approved_dir, "approved")
        self.assertEqual(args.phase_review, "phase.json")
        self.assertEqual(args.paper_performance, "performance.json")
        self.assertEqual(args.registry_dir, "registry")
        self.assertEqual(args.cadence, "weekly")
        self.assertFalse(args.force)
        self.assertEqual(args.output_dir, "reports/tmp/adaptive_training")

    def test_ready_phase_produces_reviewable_candidate_without_mutating_champion(self) -> None:
        latest_model_before = Path("models/latest_model.json").read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            approved = write_approved_dataset(root / "approved", dataset_hash="dataset-a")
            phase = write_phase(root / "phase.json", phase_status="READY_FOR_REVIEW")
            performance = write_performance(root / "performance.json")
            registry = root / "registry"

            exit_code = main(cycle_args(root, approved=approved, phase=phase, performance=performance, registry=registry))
            payload = read_json(root / "adaptive" / "2026-06-16" / "training_cycle.json")
            ledger_records = read_jsonl(root / "adaptive" / "cycle_ledger.jsonl")

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["training_state"], "CANDIDATE_REVIEWABLE")
        self.assertTrue(payload["review_only"])
        self.assertFalse(payload["model_mutated"])
        self.assertFalse(payload["live_trading_authorized"])
        self.assertFalse(payload["safety"]["broker_client_built"])
        self.assertEqual(payload["dedupe_key"]["dataset_hash"], "dataset-a")
        self.assertEqual(len(ledger_records), 1)
        self.assertEqual(ledger_records[0]["training_state"], "CANDIDATE_REVIEWABLE")
        self.assertEqual(Path("models/latest_model.json").read_text(encoding="utf-8"), latest_model_before)

    def test_duplicate_cycle_is_not_due_and_force_records_forced_rerun(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            approved = write_approved_dataset(root / "approved", dataset_hash="dataset-a")
            phase = write_phase(root / "phase.json", phase_status="READY_FOR_REVIEW")
            performance = write_performance(root / "performance.json")
            registry = root / "registry"

            first = main(cycle_args(root, approved=approved, phase=phase, performance=performance, registry=registry))
            duplicate = main(cycle_args(root, approved=approved, phase=phase, performance=performance, registry=registry))
            forced = main(cycle_args(root, approved=approved, phase=phase, performance=performance, registry=registry, force=True))
            payload = read_json(root / "adaptive" / "2026-06-16" / "training_cycle.json")
            records = read_jsonl(root / "adaptive" / "cycle_ledger.jsonl")

        self.assertEqual(first, 0)
        self.assertEqual(duplicate, 0)
        self.assertEqual(forced, 0)
        self.assertEqual(payload["training_state"], "CANDIDATE_REVIEWABLE")
        self.assertTrue(payload["forced"])
        self.assertEqual([record["training_state"] for record in records], ["CANDIDATE_REVIEWABLE", "NOT_DUE", "CANDIDATE_REVIEWABLE"])
        self.assertEqual(records[1]["duplicate_of"], records[0]["cycle_id"])
        self.assertTrue(records[2]["forced"])

    def test_phase_not_ready_blocks_before_training(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            approved = write_approved_dataset(root / "approved", dataset_hash="dataset-a")
            phase = write_phase(root / "phase.json", phase_status="ACCUMULATING")
            performance = write_performance(root / "performance.json")
            registry = root / "registry"

            exit_code = main(cycle_args(root, approved=approved, phase=phase, performance=performance, registry=registry))
            payload = read_json(root / "adaptive" / "2026-06-16" / "training_cycle.json")

        self.assertEqual(exit_code, 1)
        self.assertEqual(payload["training_state"], "BLOCKED")
        self.assertIn("phase_review_not_ready", blocker_codes(payload))
        self.assertFalse(payload["evaluation_ran"])

    def test_invalid_dataset_hash_and_missing_performance_block_cycle(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            approved = write_approved_dataset(root / "approved", dataset_hash="")
            phase = write_phase(root / "phase.json", phase_status="READY_FOR_REVIEW")
            performance = root / "missing.json"
            registry = root / "registry"

            exit_code = main(cycle_args(root, approved=approved, phase=phase, performance=performance, registry=registry))
            payload = read_json(root / "adaptive" / "2026-06-16" / "training_cycle.json")

        self.assertEqual(exit_code, 1)
        self.assertEqual(payload["training_state"], "BLOCKED")
        self.assertIn("invalid_dataset_hash", blocker_codes(payload))
        self.assertIn("missing_paper_performance", blocker_codes(payload))


def cycle_args(
    root: Path,
    *,
    approved: Path,
    phase: Path,
    performance: Path,
    registry: Path,
    force: bool = False,
) -> list[str]:
    args = [
        "adaptive-training-cycle",
        "--as-of-date",
        "2026-06-16",
        "--approved-dir",
        str(approved),
        "--phase-review",
        str(phase),
        "--paper-performance",
        str(performance),
        "--registry-dir",
        str(registry),
        "--output-dir",
        str(root / "adaptive"),
    ]
    if force:
        args.append("--force")
    return args


def write_approved_dataset(path: Path, *, dataset_hash: str) -> Path:
    write_json(
        path / "manifest.json",
        {
            "schema_version": 1,
            "dataset_hash": dataset_hash,
            "dataset_path": str(path / "data.csv"),
            "row_count": 200,
            "symbols": ["SPY", "QQQ"],
            "start": "2026-03-01",
            "end": "2026-06-16",
        },
    )
    write_json(path / "data.csv", {"placeholder": True})
    return path


def write_phase(path: Path, *, phase_status: str) -> Path:
    return write_json(
        path,
        {
            "status": "OK" if phase_status == "READY_FOR_REVIEW" else "WARN",
            "phase_status": phase_status,
            "review_only": True,
            "safety": {"paper_only": True, "live_trading_authorized": False},
        },
    )


def write_performance(path: Path, *, status: str = "OK") -> Path:
    return write_json(
        path,
        {
            "status": status,
            "paper_metrics": {"fills": 60, "pending_closeouts": 0, "unmatched_closeouts": 0, "rejections": 0},
            "blockers": [],
            "safety": {"paper_only": True},
        },
    )


def write_json(path: Path, payload: dict[str, object]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return path


def read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def read_jsonl(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def blocker_codes(payload: dict[str, object]) -> set[str]:
    return {str(blocker["code"]) for blocker in payload["blockers"]}


if __name__ == "__main__":
    unittest.main()
