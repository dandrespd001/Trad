import json
import tempfile
import unittest
from pathlib import Path

from trading_ai.cli import build_parser, main
from trading_ai.llm.schemas import schema_for


class LlmPaperReviewTests(unittest.TestCase):
    def test_parser_defaults_for_llm_paper_review(self) -> None:
        args = build_parser().parse_args(
            [
                "llm-paper-review",
                "--as-of-date",
                "2026-06-16",
                "--readiness",
                "readiness.json",
                "--ops-check",
                "ops_check.json",
                "--evidence-index",
                "evidence_index.json",
                "--llm-model-alias",
                "llm_alias.json",
            ]
        )

        self.assertEqual(args.as_of_date, "2026-06-16")
        self.assertEqual(args.readiness, "readiness.json")
        self.assertEqual(args.ops_check, "ops_check.json")
        self.assertEqual(args.evidence_index, "evidence_index.json")
        self.assertIsNone(args.performance)
        self.assertIsNone(args.challenger_report)
        self.assertIsNone(args.cycle_report)
        self.assertEqual(args.llm_model_alias, "llm_alias.json")
        self.assertFalse(args.use_openai)
        self.assertFalse(args.confirm_llm)
        self.assertEqual(args.output_dir, "reports/tmp/llm_paper_review")

    def test_schema_for_paper_ops_review_requires_no_llm_authority(self) -> None:
        schema = schema_for("PaperOpsReview")

        self.assertIn("llm_authority", schema["required"])
        self.assertEqual(schema["properties"]["llm_authority"]["enum"], ["none"])
        self.assertIn("recommendation", schema["required"])

    def test_deterministic_warn_review_continues_offline_without_openai(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            readiness = write_readiness(root, status="READY", ready=True)
            ops = write_ops(root, status="WARN", issue_codes=["statement_absent"])
            evidence = write_evidence(root, status="WARN", issue_codes=["missing_statement"])
            performance = write_performance(root, fills=0)

            exit_code = main(
                review_args(
                    root,
                    readiness=readiness,
                    ops=ops,
                    evidence=evidence,
                    performance=performance,
                )
            )
            payload = read_json(root / "review" / "2026-06-16" / "llm_paper_review.json")
            markdown = (root / "review" / "2026-06-16" / "llm_paper_review.md").read_text(encoding="utf-8")

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["status"], "WARN")
        self.assertEqual(payload["review"]["recommendation"], "CONTINUE_OFFLINE")
        self.assertEqual(payload["review"]["llm_authority"], "none")
        self.assertTrue(payload["review"]["human_review_required"])
        self.assertFalse(payload["safety"]["broker_client_built"])
        self.assertFalse(payload["safety"]["credentials_read"])
        self.assertFalse(payload["safety"]["orders_submitted"])
        self.assertIn("Recommendation: **CONTINUE_OFFLINE**", markdown)

    def test_model_cycle_deferred_recommends_defer_model(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            readiness = write_readiness(root, status="READY", ready=True)
            ops = write_ops(root, status="WARN", issue_codes=["statement_absent"])
            evidence = write_evidence(root, status="WARN", issue_codes=["missing_model_review_decision"])
            challenger = write_json(root / "challenger.json", {"status": "BLOCKED"})
            cycle = write_json(root / "cycle.json", {"status": "OK", "recommended_next_state": "DEFERRED"})

            exit_code = main(
                review_args(
                    root,
                    readiness=readiness,
                    ops=ops,
                    evidence=evidence,
                    challenger=challenger,
                    cycle=cycle,
                )
            )
            payload = read_json(root / "review" / "2026-06-16" / "llm_paper_review.json")

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["review"]["recommendation"], "DEFER_MODEL")
        self.assertIn("model_governance_deferred", blocker_codes(payload))

    def test_critical_ops_blocks_review(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            readiness = write_readiness(root, status="READY", ready=True)
            ops = write_ops(root, status="CRITICAL", issue_codes=["monitor_critical"])
            evidence = write_evidence(root, status="ERROR", issue_codes=["missing_ops_check"])

            exit_code = main(review_args(root, readiness=readiness, ops=ops, evidence=evidence))
            payload = read_json(root / "review" / "2026-06-16" / "llm_paper_review.json")

        self.assertEqual(exit_code, 1)
        self.assertEqual(payload["status"], "BLOCKED")
        self.assertEqual(payload["review"]["recommendation"], "BLOCK")
        self.assertIn("ops_check_blocking", blocker_codes(payload))

    def test_openai_mode_requires_explicit_llm_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            readiness = write_readiness(root, status="READY", ready=True)
            ops = write_ops(root, status="OK", issue_codes=[])
            evidence = write_evidence(root, status="OK", issue_codes=[])

            exit_code = main(review_args(root, readiness=readiness, ops=ops, evidence=evidence) + ["--use-openai"])
            payload = read_json(root / "review" / "2026-06-16" / "llm_paper_review.json")

        self.assertEqual(exit_code, 2)
        self.assertEqual(payload["status"], "ERROR")
        self.assertIn("missing_confirm_llm", error_codes(payload))
        self.assertTrue(payload["external_llm_requested"])
        self.assertFalse(payload["external_llm_used"])
        self.assertFalse(payload["safety"]["broker_client_built"])
        self.assertFalse(payload["safety"]["credentials_read"])

    def test_confirmed_openai_mode_is_blocked_without_api_use(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            readiness = write_readiness(root, status="READY", ready=True)
            ops = write_ops(root, status="OK", issue_codes=[])
            evidence = write_evidence(root, status="OK", issue_codes=[])

            exit_code = main(
                review_args(root, readiness=readiness, ops=ops, evidence=evidence) + ["--use-openai", "--confirm-llm"]
            )
            payload = read_json(root / "review" / "2026-06-16" / "llm_paper_review.json")

        self.assertEqual(exit_code, 2)
        self.assertEqual(payload["status"], "ERROR")
        self.assertIn("external_llm_api_disabled", error_codes(payload))
        self.assertTrue(payload["external_llm_requested"])
        self.assertFalse(payload["external_llm_used"])
        self.assertIsNone(payload["model"])
        self.assertFalse(payload["safety"]["credentials_read"])

    def test_expired_llm_model_alias_blocks_without_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            readiness = write_readiness(root, status="READY", ready=True)
            ops = write_ops(root, status="OK", issue_codes=[])
            evidence = write_evidence(root, status="OK", issue_codes=[])
            alias = write_json(
                root / "llm_alias.json",
                {
                    "alias_state": "ACTIVE_LLM_ALIAS",
                    "role_id": "paper_ops_reviewer",
                    "active_model": "gpt-5.5",
                    "alias_hash": "a" * 64,
                    "expires_on": "2026-06-15",
                    "safety": {"paper_only": True},
                },
            )

            exit_code = main(
                review_args(root, readiness=readiness, ops=ops, evidence=evidence) + ["--llm-model-alias", str(alias)]
            )
            payload = read_json(root / "review" / "2026-06-16" / "llm_paper_review.json")

        self.assertEqual(exit_code, 1)
        self.assertEqual(payload["status"], "BLOCKED")
        self.assertEqual(payload["llm_model_route"]["route_state"], "BLOCKED")
        self.assertEqual(payload["llm_model_route"]["reason"], "alias_expired")
        self.assertIn("llm_model_alias_blocked", blocker_codes(payload))

    def test_secret_like_values_are_redacted(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            readiness = write_readiness(root, status="READY", ready=True)
            ops = write_ops(root, status="WARN", issue_codes=["statement_absent"])
            write_json(
                ops,
                {
                    "status": "WARN",
                    "as_of_date": "2026-06-16",
                    "issues": [
                        {
                            "severity": "WARNING",
                            "code": "statement_absent",
                            "message": "token=TOKEN api_key=KEY secret_key=SECRET",
                        }
                    ],
                    "safety": {"live_trading_allowed": False},
                },
            )
            evidence = write_evidence(root, status="WARN", issue_codes=["missing_statement"])

            exit_code = main(review_args(root, readiness=readiness, ops=ops, evidence=evidence))
            output = (root / "review" / "2026-06-16" / "llm_paper_review.json").read_text(encoding="utf-8")
            markdown = (root / "review" / "2026-06-16" / "llm_paper_review.md").read_text(encoding="utf-8")

        self.assertEqual(exit_code, 0)
        self.assertNotIn("TOKEN", output)
        self.assertNotIn("KEY", markdown)
        self.assertNotIn("SECRET", output)
        self.assertIn("[redacted]", output)


def review_args(
    root: Path,
    *,
    readiness: Path,
    ops: Path,
    evidence: Path,
    performance: Path | None = None,
    challenger: Path | None = None,
    cycle: Path | None = None,
) -> list[str]:
    args = [
        "llm-paper-review",
        "--as-of-date",
        "2026-06-16",
        "--readiness",
        str(readiness),
        "--ops-check",
        str(ops),
        "--evidence-index",
        str(evidence),
        "--output-dir",
        str(root / "review"),
    ]
    if performance is not None:
        args.extend(["--performance", str(performance)])
    if challenger is not None:
        args.extend(["--challenger-report", str(challenger)])
    if cycle is not None:
        args.extend(["--cycle-report", str(cycle)])
    return args


def write_readiness(root: Path, *, status: str, ready: bool) -> Path:
    return write_json(
        root / "readiness.json",
        {
            "status": status,
            "ready_for_paper_daily": ready,
            "as_of_date": "2026-06-16",
            "safety": {"credentials_read": False, "live_trading_allowed": False},
        },
    )


def write_ops(root: Path, *, status: str, issue_codes: list[str]) -> Path:
    return write_json(
        root / "ops_check.json",
        {
            "status": status,
            "as_of_date": "2026-06-16",
            "issues": [
                {"severity": "WARNING", "code": code, "message": code.replace("_", " ")} for code in issue_codes
            ],
            "safety": {
                "broker_client_built": False,
                "credentials_read": False,
                "orders_submitted": False,
                "live_trading_allowed": False,
            },
        },
    )


def write_evidence(root: Path, *, status: str, issue_codes: list[str]) -> Path:
    return write_json(
        root / "evidence_index.json",
        {
            "status": status,
            "as_of_date": "2026-06-16",
            "issues": [
                {"severity": "WARNING", "code": code, "message": code.replace("_", " ")} for code in issue_codes
            ],
            "safety": {
                "broker_client_built": False,
                "credentials_read": False,
                "orders_submitted": False,
                "live_trading_allowed": False,
            },
        },
    )


def write_performance(root: Path, *, fills: int) -> Path:
    return write_json(
        root / "performance.json",
        {
            "status": "OK",
            "paper_metrics": {"fills": fills, "pending_closeouts": 0, "unmatched_closeouts": 0, "rejections": 0},
            "statement_reconciliation": {"status": "NOT_REQUESTED"},
        },
    )


def write_json(path: Path, payload: dict[str, object]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return path


def read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def blocker_codes(payload: dict[str, object]) -> set[str]:
    return {str(blocker["code"]) for blocker in payload["review"]["blockers"]}


def error_codes(payload: dict[str, object]) -> set[str]:
    return {str(error["code"]) for error in payload["errors"]}


if __name__ == "__main__":
    unittest.main()
