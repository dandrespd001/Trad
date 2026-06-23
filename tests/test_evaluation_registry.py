import contextlib
import hashlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from trading_ai.cli import build_parser, main

DATASET_HASH = "d" * 64
SOURCE_SHA256 = "e" * 64


class EvaluationRegistryTests(unittest.TestCase):
    def test_approved_package_registers_run_index_and_markdown(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            evaluation_dir = write_evaluation_package(root, status="APPROVED", eligible=True)
            registry_dir = root / "registry"

            exit_code = main(
                [
                    "register-evaluation",
                    "--evaluation-dir",
                    str(evaluation_dir),
                    "--registry-dir",
                    str(registry_dir),
                ]
            )

            summary_hash = file_sha256(evaluation_dir / "evaluation_summary.json")
            run_id = f"approved-core_etfs-1d-2026-06-16-{DATASET_HASH[:12]}-{summary_hash[:12]}"
            run_path = registry_dir / "runs" / f"{run_id}.json"
            run_payload = json.loads(run_path.read_text(encoding="utf-8"))
            index = json.loads((registry_dir / "index.json").read_text(encoding="utf-8"))
            markdown = (registry_dir / "index.md").read_text(encoding="utf-8")

        self.assertEqual(exit_code, 0)
        self.assertEqual(run_payload["run_id"], run_id)
        self.assertEqual(run_payload["status"], "APPROVED")
        self.assertTrue(run_payload["eligible_for_paper_challenger"])
        self.assertEqual(run_payload["dataset_id"], "core_etfs")
        self.assertEqual(run_payload["frequency"], "1d")
        self.assertEqual(run_payload["as_of_date"], "2026-06-16")
        self.assertEqual(run_payload["dataset_hash"], DATASET_HASH)
        self.assertEqual(run_payload["source_sha256"], SOURCE_SHA256)
        self.assertEqual(run_payload["temporal_range"], {"start": "2025-01-01", "end": "2026-06-16"})
        self.assertEqual(run_payload["symbols"], ["SPY"])
        self.assertEqual(run_payload["row_count"], 123)
        self.assertEqual(run_payload["metrics"]["accuracy"], 0.61)
        self.assertIn("evaluation_summary", run_payload["artifacts"])
        self.assertIn("data_quality", run_payload["artifacts"])
        self.assertEqual(index["counts"], {"APPROVED": 1, "REJECTED": 0, "BLOCKED": 0})
        self.assertEqual(len(index["runs"]), 1)
        self.assertIn("| Run ID | Dataset | Frequency | As Of Date | Status | Eligible |", markdown)
        self.assertIn(run_id, markdown)
        self.assertIn(str(evaluation_dir / "evaluation_summary.json"), markdown)

    def test_rejected_package_registers_as_not_eligible(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            evaluation_dir = write_evaluation_package(
                root,
                status="REJECTED",
                eligible=False,
                reasons=["insufficient_test_samples"],
            )
            registry_dir = root / "registry"

            exit_code = main(
                [
                    "register-evaluation",
                    "--evaluation-dir",
                    str(evaluation_dir),
                    "--registry-dir",
                    str(registry_dir),
                ]
            )
            index = json.loads((registry_dir / "index.json").read_text(encoding="utf-8"))
            run_path = Path(index["runs"][0]["run_path"])
            run_payload = json.loads(run_path.read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 0)
        self.assertEqual(index["counts"], {"APPROVED": 0, "REJECTED": 1, "BLOCKED": 0})
        self.assertEqual(run_payload["status"], "REJECTED")
        self.assertFalse(run_payload["eligible_for_paper_challenger"])
        self.assertEqual(run_payload["reasons"], ["insufficient_test_samples"])

    def test_blocked_package_registers_with_only_summary_and_data_quality(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            evaluation_dir = write_evaluation_package(
                root,
                status="BLOCKED",
                eligible=False,
                reasons=["row 0 timestamp incompatible with 1d"],
                extra_artifacts=False,
            )
            registry_dir = root / "registry"

            exit_code = main(
                [
                    "register-evaluation",
                    "--evaluation-dir",
                    str(evaluation_dir),
                    "--registry-dir",
                    str(registry_dir),
                ]
            )
            index = json.loads((registry_dir / "index.json").read_text(encoding="utf-8"))
            run_payload = json.loads(Path(index["runs"][0]["run_path"]).read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 0)
        self.assertEqual(index["counts"], {"APPROVED": 0, "REJECTED": 0, "BLOCKED": 1})
        self.assertEqual(run_payload["status"], "BLOCKED")
        self.assertFalse(run_payload["eligible_for_paper_challenger"])
        self.assertEqual(set(run_payload["artifacts"]), {"evaluation_summary", "data_quality"})

    def test_double_registration_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            evaluation_dir = write_evaluation_package(root, status="APPROVED", eligible=True)
            registry_dir = root / "registry"
            args = [
                "register-evaluation",
                "--evaluation-dir",
                str(evaluation_dir),
                "--registry-dir",
                str(registry_dir),
            ]

            first_exit = main(args)
            first_index = json.loads((registry_dir / "index.json").read_text(encoding="utf-8"))
            first_run = json.loads(Path(first_index["runs"][0]["run_path"]).read_text(encoding="utf-8"))
            second_exit = main(args)
            second_index = json.loads((registry_dir / "index.json").read_text(encoding="utf-8"))
            second_run = json.loads(Path(second_index["runs"][0]["run_path"]).read_text(encoding="utf-8"))

        self.assertEqual(first_exit, 0)
        self.assertEqual(second_exit, 0)
        self.assertEqual(len(second_index["runs"]), 1)
        self.assertEqual(first_index["runs"][0]["run_id"], second_index["runs"][0]["run_id"])
        self.assertEqual(first_run["registered_at"], second_run["registered_at"])

    def test_artifact_hash_mismatch_returns_two_and_does_not_update_index(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            good_evaluation_dir = write_evaluation_package(root, status="APPROVED", eligible=True)
            registry_dir = root / "registry"
            good_args = [
                "register-evaluation",
                "--evaluation-dir",
                str(good_evaluation_dir),
                "--registry-dir",
                str(registry_dir),
            ]
            self.assertEqual(main(good_args), 0)
            original_index = (registry_dir / "index.json").read_text(encoding="utf-8")

            (good_evaluation_dir / "data_quality.json").write_text('{"tampered": true}', encoding="utf-8")
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                exit_code = main(good_args)
            current_index = (registry_dir / "index.json").read_text(encoding="utf-8")

        self.assertEqual(exit_code, 2)
        self.assertIn("artifact hash mismatch for data_quality", stderr.getvalue())
        self.assertEqual(current_index, original_index)

    def test_missing_evaluation_summary_returns_two(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            evaluation_dir = root / "reports" / "tmp" / "approved_eval" / "core_etfs" / "1d" / "2026-06-16"
            evaluation_dir.mkdir(parents=True)
            (evaluation_dir / "data_quality.json").write_text("{}", encoding="utf-8")
            registry_dir = root / "registry"
            stderr = io.StringIO()

            with contextlib.redirect_stderr(stderr):
                exit_code = main(
                    [
                        "register-evaluation",
                        "--evaluation-dir",
                        str(evaluation_dir),
                        "--registry-dir",
                        str(registry_dir),
                    ]
                )

        self.assertEqual(exit_code, 2)
        self.assertIn("evaluation_summary.json", stderr.getvalue())
        self.assertFalse((registry_dir / "index.json").exists())

    def test_invalid_json_or_missing_metadata_returns_two_without_index(self) -> None:
        cases = ("invalid_json", "missing_dataset_hash")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                evaluation_dir = write_evaluation_package(root, status="APPROVED", eligible=True)
                registry_dir = root / "registry"
                if case == "invalid_json":
                    (evaluation_dir / "evaluation_summary.json").write_text("{bad json", encoding="utf-8")
                    expected_message = "invalid evaluation summary JSON"
                else:
                    summary_path = evaluation_dir / "evaluation_summary.json"
                    summary = json.loads(summary_path.read_text(encoding="utf-8"))
                    del summary["approved_dataset"]["dataset_hash"]
                    summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
                    expected_message = "missing required field(s): dataset_hash"
                stderr = io.StringIO()

                with contextlib.redirect_stderr(stderr):
                    exit_code = main(
                        [
                            "register-evaluation",
                            "--evaluation-dir",
                            str(evaluation_dir),
                            "--registry-dir",
                            str(registry_dir),
                        ]
                    )

                self.assertEqual(exit_code, 2)
                self.assertIn(expected_message, stderr.getvalue())
                self.assertFalse((registry_dir / "index.json").exists())

    def test_parser_default_registry_dir(self) -> None:
        args = build_parser().parse_args(
            [
                "register-evaluation",
                "--evaluation-dir",
                "reports/tmp/approved_eval/core_etfs/1d/2026-06-16",
            ]
        )

        self.assertEqual(args.registry_dir, "reports/registry")

    def test_register_evaluation_does_not_use_broker_credentials_or_mlflow(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            evaluation_dir = write_evaluation_package(root, status="APPROVED", eligible=True)
            registry_dir = root / "registry"

            with (
                mock.patch(
                    "trading_ai.cli.build_alpaca_paper_client",
                    side_effect=AssertionError("alpaca client should not be built"),
                ),
                mock.patch(
                    "trading_ai.execution.alpaca_connection.load_alpaca_paper_credentials",
                    side_effect=AssertionError("credentials should not be read"),
                ),
                mock.patch.dict(sys.modules, {"mlflow": None}),
            ):
                exit_code = main(
                    [
                        "register-evaluation",
                        "--evaluation-dir",
                        str(evaluation_dir),
                        "--registry-dir",
                        str(registry_dir),
                    ]
                )

        self.assertEqual(exit_code, 0)


def write_evaluation_package(
    root: Path,
    *,
    status: str,
    eligible: bool,
    reasons: list[str] | None = None,
    extra_artifacts: bool = True,
) -> Path:
    evaluation_dir = root / "reports" / "tmp" / "approved_eval" / "core_etfs" / "1d" / "2026-06-16"
    evaluation_dir.mkdir(parents=True)
    data_quality_path = evaluation_dir / "data_quality.json"
    data_quality_path.write_text(
        json.dumps(
            {
                "status": "APPROVED" if status != "BLOCKED" else "BLOCKED",
                "passed": status != "BLOCKED",
                "reasons": reasons or [],
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    artifacts = {
        "data_quality": {
            "path": str(data_quality_path),
            "sha256": file_sha256(data_quality_path),
        },
    }
    if extra_artifacts:
        for name in ("backtest", "model_eval", "promotion_decision"):
            path = evaluation_dir / f"{name}.json"
            path.write_text(json.dumps({"artifact": name}, indent=2, sort_keys=True), encoding="utf-8")
            artifacts[name] = {
                "path": str(path),
                "sha256": file_sha256(path),
            }

    summary = {
        "schema_version": 1,
        "status": status,
        "eligible_for_paper_challenger": eligible,
        "reasons": reasons or [],
        "approved_dataset": {
            "dataset_id": "core_etfs",
            "frequency": "1d",
            "dataset_hash": DATASET_HASH,
            "source_sha256": SOURCE_SHA256,
            "start": "2025-01-01",
            "end": "2026-06-16",
            "symbols": ["SPY"],
            "row_count": 123,
        },
        "metrics": {
            "accuracy": 0.61,
            "sharpe": 1.25,
            "cagr": 0.14,
            "max_drawdown": -0.08,
        }
        if status != "BLOCKED"
        else {},
        "artifacts": artifacts,
    }
    (evaluation_dir / "evaluation_summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return evaluation_dir


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


if __name__ == "__main__":
    unittest.main()
