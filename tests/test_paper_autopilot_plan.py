import json
import tempfile
import unittest
from pathlib import Path

from trading_ai.cli import build_parser, main


class PaperAutopilotPlanTests(unittest.TestCase):
    def test_parser_defaults_for_paper_autopilot_plan(self) -> None:
        args = build_parser().parse_args(
            [
                "paper-autopilot-plan",
                "--as-of-date",
                "2026-06-16",
                "--readiness",
                "readiness.json",
            ]
        )

        self.assertEqual(args.as_of_date, "2026-06-16")
        self.assertEqual(args.readiness, "readiness.json")
        self.assertIsNone(args.ops_check)
        self.assertIsNone(args.evidence_index)
        self.assertIsNone(args.llm_review)
        self.assertIsNone(args.human_review)
        self.assertEqual(args.permissions, "configs/permissions.yml")
        self.assertEqual(args.output_dir, "reports/tmp/paper_autopilot_plan")

    def test_not_ready_runs_readiness(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            readiness = write_readiness(root, status="BLOCKED", ready=False)

            exit_code = main(plan_args(root, readiness=readiness))
            payload = read_json(root / "plan" / "2026-06-16" / "autopilot_plan.json")

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["status"], "OK")
        self.assertEqual(payload["action"], "RUN_READINESS")
        self.assertFalse(payload["safety"]["broker_client_built"])
        self.assertFalse(payload["safety"]["credentials_read"])
        self.assertFalse(payload["safety"]["orders_submitted"])

    def test_ready_without_ops_runs_offline_daily(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            readiness = write_readiness(root, status="READY", ready=True)

            exit_code = main(plan_args(root, readiness=readiness))
            payload = read_json(root / "plan" / "2026-06-16" / "autopilot_plan.json")

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["action"], "RUN_OFFLINE_DAILY")

    def test_warn_ops_without_human_review_requests_review(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            readiness = write_readiness(root, status="READY", ready=True)
            ops = write_ops(root, status="WARN", issue_codes=["statement_absent"])
            evidence = write_evidence(root, status="WARN", issue_codes=["missing_statement"])

            exit_code = main(plan_args(root, readiness=readiness, ops=ops, evidence=evidence))
            payload = read_json(root / "plan" / "2026-06-16" / "autopilot_plan.json")
            markdown = (root / "plan" / "2026-06-16" / "autopilot_plan.md").read_text(encoding="utf-8")

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["status"], "OK")
        self.assertEqual(payload["action"], "REQUEST_REVIEW")
        self.assertIn("statement_absent", reason_codes(payload))
        self.assertIn("Action: **REQUEST_REVIEW**", markdown)

    def test_ready_warn_with_human_review_is_eligible_for_paper_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            readiness = write_readiness(root, status="READY", ready=True)
            ops = write_ops(root, status="WARN", issue_codes=["statement_absent"])
            evidence = write_evidence(root, status="WARN", issue_codes=["missing_statement"])
            review = write_json(
                root / "human_review.json",
                {"status": "RECORDED", "decision": "APPROVE_PAPER_CONFIRMATION"},
            )

            exit_code = main(plan_args(root, readiness=readiness, ops=ops, evidence=evidence, human_review=review))
            payload = read_json(root / "plan" / "2026-06-16" / "autopilot_plan.json")

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["action"], "ELIGIBLE_FOR_PAPER_CONFIRMED")
        self.assertTrue(payload["human_review"]["present"])
        self.assertEqual(payload["authority"]["llm_authority"], "none")
        self.assertFalse(payload["authority"]["orders_submitted"])

    def test_defer_human_review_does_not_make_plan_eligible(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            readiness = write_readiness(root, status="READY", ready=True)
            ops = write_ops(root, status="OK", issue_codes=[])
            evidence = write_evidence(root, status="OK", issue_codes=[])
            review = write_json(root / "human_review.json", {"status": "RECORDED", "decision": "DEFER"})

            exit_code = main(plan_args(root, readiness=readiness, ops=ops, evidence=evidence, human_review=review))
            payload = read_json(root / "plan" / "2026-06-16" / "autopilot_plan.json")

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["status"], "OK")
        self.assertEqual(payload["action"], "REQUEST_REVIEW")
        self.assertIn("human_review_deferred", reason_codes(payload))

    def test_reject_human_review_blocks_plan(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            readiness = write_readiness(root, status="READY", ready=True)
            ops = write_ops(root, status="OK", issue_codes=[])
            evidence = write_evidence(root, status="OK", issue_codes=[])
            review = write_json(root / "human_review.json", {"status": "RECORDED", "decision": "REJECT"})

            exit_code = main(plan_args(root, readiness=readiness, ops=ops, evidence=evidence, human_review=review))
            payload = read_json(root / "plan" / "2026-06-16" / "autopilot_plan.json")

        self.assertEqual(exit_code, 1)
        self.assertEqual(payload["status"], "BLOCKED")
        self.assertEqual(payload["action"], "BLOCKED")
        self.assertIn("human_review_rejected", reason_codes(payload))

    def test_critical_ops_blocks_even_with_human_review(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            readiness = write_readiness(root, status="READY", ready=True)
            ops = write_ops(root, status="CRITICAL", issue_codes=["monitor_critical"])
            evidence = write_evidence(root, status="OK", issue_codes=[])
            review = write_json(
                root / "human_review.json",
                {"status": "RECORDED", "decision": "APPROVE_PAPER_CONFIRMATION"},
            )

            exit_code = main(plan_args(root, readiness=readiness, ops=ops, evidence=evidence, human_review=review))
            payload = read_json(root / "plan" / "2026-06-16" / "autopilot_plan.json")

        self.assertEqual(exit_code, 1)
        self.assertEqual(payload["status"], "BLOCKED")
        self.assertEqual(payload["action"], "BLOCKED")
        self.assertIn("ops_check_blocking", reason_codes(payload))

    def test_live_permission_blocks_autopilot(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            permissions = write_json_like_yaml(root / "permissions.yml", "risk_limits:\n  live_trading_allowed: true\n")
            readiness = write_readiness(root, status="READY", ready=True)
            ops = write_ops(root, status="OK", issue_codes=[])
            evidence = write_evidence(root, status="OK", issue_codes=[])
            review = write_json(
                root / "human_review.json",
                {"status": "RECORDED", "decision": "APPROVE_PAPER_CONFIRMATION"},
            )

            exit_code = main(
                plan_args(
                    root, readiness=readiness, ops=ops, evidence=evidence, human_review=review, permissions=permissions
                )
            )
            payload = read_json(root / "plan" / "2026-06-16" / "autopilot_plan.json")

        self.assertEqual(exit_code, 1)
        self.assertEqual(payload["action"], "BLOCKED")
        self.assertIn("live_permission_not_allowed", reason_codes(payload))

    def test_blocked_safety_input_is_reflected_in_output_safety(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            readiness = write_json(
                root / "readiness.json",
                {
                    "status": "READY",
                    "ready_for_paper_daily": True,
                    "as_of_date": "2026-06-16",
                    "safety": {"credentials_read": True, "live_trading_allowed": False},
                },
            )

            exit_code = main(plan_args(root, readiness=readiness))
            payload = read_json(root / "plan" / "2026-06-16" / "autopilot_plan.json")

        self.assertEqual(exit_code, 1)
        self.assertEqual(payload["action"], "BLOCKED")
        self.assertTrue(payload["safety"]["credentials_read"])
        self.assertTrue(payload["safety"]["observed_child_safety"]["credentials_read"])

    def test_llm_review_cannot_make_plan_eligible_without_human_review(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            readiness = write_readiness(root, status="READY", ready=True)
            ops = write_ops(root, status="OK", issue_codes=[])
            evidence = write_evidence(root, status="OK", issue_codes=[])
            llm_review = write_json(
                root / "llm_review.json",
                {
                    "status": "OK",
                    "review": {
                        "recommendation": "READY_FOR_PAPER_CONFIRMATION",
                        "llm_authority": "none",
                    },
                },
            )

            exit_code = main(plan_args(root, readiness=readiness, ops=ops, evidence=evidence, llm_review=llm_review))
            payload = read_json(root / "plan" / "2026-06-16" / "autopilot_plan.json")

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["action"], "REQUEST_REVIEW")
        self.assertEqual(payload["llm_review"]["recommendation"], "READY_FOR_PAPER_CONFIRMATION")


def plan_args(
    root: Path,
    *,
    readiness: Path,
    ops: Path | None = None,
    evidence: Path | None = None,
    llm_review: Path | None = None,
    human_review: Path | None = None,
    permissions: Path | None = None,
) -> list[str]:
    args = [
        "paper-autopilot-plan",
        "--as-of-date",
        "2026-06-16",
        "--readiness",
        str(readiness),
        "--output-dir",
        str(root / "plan"),
    ]
    if ops is not None:
        args.extend(["--ops-check", str(ops)])
    if evidence is not None:
        args.extend(["--evidence-index", str(evidence)])
    if llm_review is not None:
        args.extend(["--llm-review", str(llm_review)])
    if human_review is not None:
        args.extend(["--human-review", str(human_review)])
    if permissions is not None:
        args.extend(["--permissions", str(permissions)])
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


def write_json(path: Path, payload: dict[str, object]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return path


def write_json_like_yaml(path: Path, payload: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload, encoding="utf-8")
    return path


def read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def reason_codes(payload: dict[str, object]) -> set[str]:
    return {str(reason["code"]) for reason in payload["reasons"]}


if __name__ == "__main__":
    unittest.main()
