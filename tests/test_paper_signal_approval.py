import json
import tempfile
import unittest
from pathlib import Path

from trading_ai.cli import build_parser, main
from trading_ai.execution.paper_signal_approval import (
    DEFAULT_VETO_WINDOW_MINUTES,
    MIN_HASH_PREFIX,
    compute_plan_hash,
    evaluate_signal_approval_gate,
    load_signal_approval_registry,
    record_signal_plan_review,
)


def make_plan(as_of_date: str = "2026-07-06", decision: str = "ELIGIBLE_FOR_PAPER", generated_at: str = "2026-07-06T10:00:00+00:00") -> dict:
    return {
        "schema_version": "1.0",
        "generated_at": generated_at,
        "as_of_date": as_of_date,
        "decision": decision,
        "selected_symbol": "SPY",
        "eligible_for_paper": decision == "ELIGIBLE_FOR_PAPER",
    }


class ComputePlanHashTests(unittest.TestCase):
    def test_hash_is_stable_across_generated_at_changes(self) -> None:
        plan_a = make_plan(generated_at="2026-07-06T10:00:00+00:00")
        plan_b = make_plan(generated_at="2026-07-06T11:30:00+00:00")

        self.assertEqual(compute_plan_hash(plan_a), compute_plan_hash(plan_b))

    def test_hash_changes_with_decision(self) -> None:
        plan_a = make_plan(decision="ELIGIBLE_FOR_PAPER")
        plan_b = make_plan(decision="NO_TRADE_REVIEW")

        self.assertNotEqual(compute_plan_hash(plan_a), compute_plan_hash(plan_b))


class RecordSignalPlanReviewTests(unittest.TestCase):
    def test_happy_approve_records_and_writes_checksum(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            registry_dir = Path(temp_dir) / "registry"
            plan = make_plan()
            plan_hash = compute_plan_hash(plan)

            decision = record_signal_plan_review(
                as_of_date="2026-07-06",
                plan_hash_prefix=plan_hash[:MIN_HASH_PREFIX],
                verdict="approved",
                actor_user_id="67890",
                actor_chat_id="12345",
                source_update_id=10,
                reason="",
                plan=plan,
                registry_dir=registry_dir,
            )

            self.assertEqual(decision.status, "OK")
            self.assertEqual(decision.exit_code, 0)
            registry = load_signal_approval_registry("2026-07-06", registry_dir=registry_dir)
            self.assertFalse(registry["fail_closed"])
            self.assertEqual(len(registry["records"]), 1)
            self.assertEqual(registry["records"][0]["verdict"], "approved")
            self.assertEqual(registry["records"][0]["plan_hash"], plan_hash)

            registry_path = registry_dir / "2026-07-06" / "registry.json"
            raw = json.loads(registry_path.read_text(encoding="utf-8"))
            self.assertIn("integrity_sha256", raw)

    def test_happy_veto_with_reason_records(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            registry_dir = Path(temp_dir) / "registry"
            plan = make_plan()
            plan_hash = compute_plan_hash(plan)

            decision = record_signal_plan_review(
                as_of_date="2026-07-06",
                plan_hash_prefix=plan_hash[:MIN_HASH_PREFIX],
                verdict="vetoed",
                actor_user_id="67890",
                actor_chat_id="12345",
                source_update_id=11,
                reason="news risk",
                plan=plan,
                registry_dir=registry_dir,
            )

            self.assertEqual(decision.status, "OK")
            registry = load_signal_approval_registry("2026-07-06", registry_dir=registry_dir)
            self.assertEqual(registry["records"][0]["verdict"], "vetoed")
            self.assertEqual(registry["records"][0]["reason"], "news risk")

    def test_veto_without_reason_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            registry_dir = Path(temp_dir) / "registry"
            plan = make_plan()
            plan_hash = compute_plan_hash(plan)

            decision = record_signal_plan_review(
                as_of_date="2026-07-06",
                plan_hash_prefix=plan_hash[:MIN_HASH_PREFIX],
                verdict="vetoed",
                actor_user_id="67890",
                actor_chat_id="12345",
                source_update_id=12,
                reason="   ",
                plan=plan,
                registry_dir=registry_dir,
            )

            self.assertEqual(decision.status, "BLOCKED")
            self.assertIn("veto_reason_required", decision.payload["blockers"])
            registry = load_signal_approval_registry("2026-07-06", registry_dir=registry_dir)
            self.assertEqual(len(registry["records"]), 0)

    def test_short_prefix_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            registry_dir = Path(temp_dir) / "registry"
            plan = make_plan()
            plan_hash = compute_plan_hash(plan)

            decision = record_signal_plan_review(
                as_of_date="2026-07-06",
                plan_hash_prefix=plan_hash[:4],
                verdict="approved",
                actor_user_id="67890",
                actor_chat_id="12345",
                source_update_id=13,
                reason="",
                plan=plan,
                registry_dir=registry_dir,
            )

            self.assertEqual(decision.status, "BLOCKED")
            self.assertIn("invalid_plan_hash_prefix", decision.payload["blockers"])

    def test_non_hex_prefix_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            registry_dir = Path(temp_dir) / "registry"
            plan = make_plan()

            decision = record_signal_plan_review(
                as_of_date="2026-07-06",
                plan_hash_prefix="zzzzzzzz",
                verdict="approved",
                actor_user_id="67890",
                actor_chat_id="12345",
                source_update_id=14,
                reason="",
                plan=plan,
                registry_dir=registry_dir,
            )

            self.assertEqual(decision.status, "BLOCKED")
            self.assertIn("invalid_plan_hash_prefix", decision.payload["blockers"])

    def test_prefix_mismatch_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            registry_dir = Path(temp_dir) / "registry"
            plan = make_plan()

            decision = record_signal_plan_review(
                as_of_date="2026-07-06",
                plan_hash_prefix="deadbeef",
                verdict="approved",
                actor_user_id="67890",
                actor_chat_id="12345",
                source_update_id=15,
                reason="",
                plan=plan,
                registry_dir=registry_dir,
            )

            self.assertEqual(decision.status, "BLOCKED")
            self.assertIn("plan_hash_mismatch", decision.payload["blockers"])

    def test_as_of_date_mismatch_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            registry_dir = Path(temp_dir) / "registry"
            plan = make_plan(as_of_date="2026-07-05")
            plan_hash = compute_plan_hash(plan)

            decision = record_signal_plan_review(
                as_of_date="2026-07-06",
                plan_hash_prefix=plan_hash[:MIN_HASH_PREFIX],
                verdict="approved",
                actor_user_id="67890",
                actor_chat_id="12345",
                source_update_id=16,
                reason="",
                plan=plan,
                registry_dir=registry_dir,
            )

            self.assertEqual(decision.status, "BLOCKED")
            self.assertIn("plan_as_of_date_mismatch", decision.payload["blockers"])

    def test_duplicate_review_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            registry_dir = Path(temp_dir) / "registry"
            plan = make_plan()
            plan_hash = compute_plan_hash(plan)

            kwargs = dict(
                as_of_date="2026-07-06",
                plan_hash_prefix=plan_hash[:MIN_HASH_PREFIX],
                verdict="approved",
                actor_user_id="67890",
                actor_chat_id="12345",
                source_update_id=17,
                reason="",
                plan=plan,
                registry_dir=registry_dir,
            )
            first = record_signal_plan_review(**kwargs)
            second = record_signal_plan_review(**kwargs)

            self.assertEqual(first.status, "OK")
            self.assertEqual(second.status, "BLOCKED")
            self.assertIn("duplicate_review", second.payload["blockers"])

    def test_approve_after_veto_is_blocked_as_plan_already_vetoed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            registry_dir = Path(temp_dir) / "registry"
            plan = make_plan()
            plan_hash = compute_plan_hash(plan)

            veto = record_signal_plan_review(
                as_of_date="2026-07-06",
                plan_hash_prefix=plan_hash[:MIN_HASH_PREFIX],
                verdict="vetoed",
                actor_user_id="67890",
                actor_chat_id="12345",
                source_update_id=18,
                reason="news risk",
                plan=plan,
                registry_dir=registry_dir,
            )
            approve = record_signal_plan_review(
                as_of_date="2026-07-06",
                plan_hash_prefix=plan_hash[:MIN_HASH_PREFIX],
                verdict="approved",
                actor_user_id="11111",
                actor_chat_id="12345",
                source_update_id=19,
                reason="",
                plan=plan,
                registry_dir=registry_dir,
            )

            self.assertEqual(veto.status, "OK")
            self.assertEqual(approve.status, "BLOCKED")
            self.assertIn("plan_already_vetoed", approve.payload["blockers"])
            registry = load_signal_approval_registry("2026-07-06", registry_dir=registry_dir)
            self.assertEqual(len(registry["records"]), 1)

    def test_veto_after_approve_is_recorded_and_gate_reflects_veto(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            registry_dir = Path(temp_dir) / "registry"
            plan = make_plan()
            plan_hash = compute_plan_hash(plan)

            approve = record_signal_plan_review(
                as_of_date="2026-07-06",
                plan_hash_prefix=plan_hash[:MIN_HASH_PREFIX],
                verdict="approved",
                actor_user_id="67890",
                actor_chat_id="12345",
                source_update_id=20,
                reason="",
                plan=plan,
                registry_dir=registry_dir,
            )
            veto = record_signal_plan_review(
                as_of_date="2026-07-06",
                plan_hash_prefix=plan_hash[:MIN_HASH_PREFIX],
                verdict="vetoed",
                actor_user_id="11111",
                actor_chat_id="12345",
                source_update_id=21,
                reason="late risk event",
                plan=plan,
                registry_dir=registry_dir,
            )

            self.assertEqual(approve.status, "OK")
            self.assertEqual(veto.status, "OK")
            registry = load_signal_approval_registry("2026-07-06", registry_dir=registry_dir)
            self.assertEqual(len(registry["records"]), 2)
            blockers = evaluate_signal_approval_gate(
                plan_hash=plan_hash,
                registry_payload=registry,
                requested_action="real_submit_auto",
                plan_generated_at=plan["generated_at"],
                now="2026-07-06T10:05:00+00:00",
            )
            self.assertEqual(blockers, ["signal_plan_vetoed"])

    def test_corrupt_registry_is_fail_closed_and_blocks_new_reviews(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            registry_dir = Path(temp_dir) / "registry"
            registry_path = registry_dir / "2026-07-06" / "registry.json"
            registry_path.parent.mkdir(parents=True, exist_ok=True)
            registry_path.write_text("{not valid json", encoding="utf-8")

            registry = load_signal_approval_registry("2026-07-06", registry_dir=registry_dir)
            self.assertTrue(registry["fail_closed"])
            self.assertEqual(registry["records"], [])

            plan = make_plan()
            plan_hash = compute_plan_hash(plan)
            decision = record_signal_plan_review(
                as_of_date="2026-07-06",
                plan_hash_prefix=plan_hash[:MIN_HASH_PREFIX],
                verdict="approved",
                actor_user_id="67890",
                actor_chat_id="12345",
                source_update_id=22,
                reason="",
                plan=plan,
                registry_dir=registry_dir,
            )
            self.assertEqual(decision.status, "BLOCKED")
            self.assertIn("approval_registry_fail_closed", decision.payload["blockers"])

    def test_tampered_checksum_is_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            registry_dir = Path(temp_dir) / "registry"
            registry_path = registry_dir / "2026-07-06" / "registry.json"
            registry_path.parent.mkdir(parents=True, exist_ok=True)
            registry_path.write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "as_of_date": "2026-07-06",
                        "records": [],
                        "integrity_sha256": "0" * 64,
                    }
                ),
                encoding="utf-8",
            )

            registry = load_signal_approval_registry("2026-07-06", registry_dir=registry_dir)
            self.assertTrue(registry["fail_closed"])

    def test_registry_checksum_remains_valid_after_two_writes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            registry_dir = Path(temp_dir) / "registry"
            plan = make_plan()
            plan_hash = compute_plan_hash(plan)

            record_signal_plan_review(
                as_of_date="2026-07-06",
                plan_hash_prefix=plan_hash[:MIN_HASH_PREFIX],
                verdict="approved",
                actor_user_id="67890",
                actor_chat_id="12345",
                source_update_id=23,
                reason="",
                plan=plan,
                registry_dir=registry_dir,
            )
            record_signal_plan_review(
                as_of_date="2026-07-06",
                plan_hash_prefix=plan_hash[:MIN_HASH_PREFIX],
                verdict="vetoed",
                actor_user_id="11111",
                actor_chat_id="99999",
                source_update_id=24,
                reason="late risk",
                plan=plan,
                registry_dir=registry_dir,
            )

            registry = load_signal_approval_registry("2026-07-06", registry_dir=registry_dir)
            self.assertFalse(registry["fail_closed"])
            self.assertEqual(len(registry["records"]), 2)


class EvaluateSignalApprovalGateTests(unittest.TestCase):
    def _registry(self, records: list[dict], *, fail_closed: bool = False) -> dict:
        return {
            "schema_version": "1.0",
            "as_of_date": "2026-07-06",
            "records": records,
            "fail_closed": fail_closed,
        }

    def test_paper_auto_never_blocked(self) -> None:
        blockers = evaluate_signal_approval_gate(
            plan_hash="abc123",
            registry_payload=self._registry([]),
            requested_action="paper_auto",
            plan_generated_at="2026-07-06T10:00:00+00:00",
            now="2026-07-06T10:00:00+00:00",
        )
        self.assertEqual(blockers, [])

    def test_unknown_requested_action(self) -> None:
        blockers = evaluate_signal_approval_gate(
            plan_hash="abc123",
            registry_payload=self._registry([]),
            requested_action="real_submit",
            plan_generated_at="2026-07-06T10:00:00+00:00",
            now="2026-07-06T10:00:00+00:00",
        )
        self.assertEqual(blockers, ["unknown_requested_action"])

    def test_fail_closed_blocks_real_actions(self) -> None:
        for action in ("real_submit_approved", "real_submit_veto_window", "real_submit_auto"):
            blockers = evaluate_signal_approval_gate(
                plan_hash="abc123",
                registry_payload=self._registry([], fail_closed=True),
                requested_action=action,
                plan_generated_at="2026-07-06T10:00:00+00:00",
                now="2026-07-06T10:00:00+00:00",
            )
            self.assertEqual(blockers, ["approval_registry_fail_closed"])

    def test_no_records_blocks_approved_action_and_open_veto_window(self) -> None:
        blockers_approved = evaluate_signal_approval_gate(
            plan_hash="abc123",
            registry_payload=self._registry([]),
            requested_action="real_submit_approved",
            plan_generated_at="2026-07-06T10:00:00+00:00",
            now="2026-07-06T10:00:00+00:00",
        )
        self.assertEqual(blockers_approved, ["signal_approval_missing"])

        blockers_window_open = evaluate_signal_approval_gate(
            plan_hash="abc123",
            registry_payload=self._registry([]),
            requested_action="real_submit_veto_window",
            plan_generated_at="2026-07-06T10:00:00+00:00",
            now="2026-07-06T10:05:00+00:00",
        )
        self.assertEqual(blockers_window_open, ["veto_window_open"])

        blockers_auto = evaluate_signal_approval_gate(
            plan_hash="abc123",
            registry_payload=self._registry([]),
            requested_action="real_submit_auto",
            plan_generated_at="2026-07-06T10:00:00+00:00",
            now="2026-07-06T10:05:00+00:00",
        )
        self.assertEqual(blockers_auto, [])

    def test_veto_window_expires_without_veto(self) -> None:
        blockers = evaluate_signal_approval_gate(
            plan_hash="abc123",
            registry_payload=self._registry([]),
            requested_action="real_submit_veto_window",
            plan_generated_at="2026-07-06T10:00:00+00:00",
            now="2026-07-06T10:16:00+00:00",
            veto_window_minutes=DEFAULT_VETO_WINDOW_MINUTES,
        )
        self.assertEqual(blockers, [])

    def test_approve_record_clears_veto_window_and_approved_action(self) -> None:
        records = [{"plan_hash": "abc123", "verdict": "approved", "actor_user_id": "67890"}]
        blockers_approved = evaluate_signal_approval_gate(
            plan_hash="abc123",
            registry_payload=self._registry(records),
            requested_action="real_submit_approved",
            plan_generated_at="2026-07-06T10:00:00+00:00",
            now="2026-07-06T10:00:00+00:00",
        )
        self.assertEqual(blockers_approved, [])

        blockers_window = evaluate_signal_approval_gate(
            plan_hash="abc123",
            registry_payload=self._registry(records),
            requested_action="real_submit_veto_window",
            plan_generated_at="2026-07-06T10:00:00+00:00",
            now="2026-07-06T10:00:01+00:00",
        )
        self.assertEqual(blockers_window, [])

    def test_veto_record_blocks_all_real_actions_regardless_of_approval(self) -> None:
        records = [
            {"plan_hash": "abc123", "verdict": "approved", "actor_user_id": "67890"},
            {"plan_hash": "abc123", "verdict": "vetoed", "actor_user_id": "11111"},
        ]
        for action in ("real_submit_approved", "real_submit_veto_window", "real_submit_auto"):
            blockers = evaluate_signal_approval_gate(
                plan_hash="abc123",
                registry_payload=self._registry(records),
                requested_action=action,
                plan_generated_at="2026-07-06T10:00:00+00:00",
                now="2026-07-06T10:20:00+00:00",
            )
            self.assertEqual(blockers, ["signal_plan_vetoed"])

    def test_records_for_a_different_plan_hash_do_not_affect_gate(self) -> None:
        records = [{"plan_hash": "other-hash", "verdict": "vetoed", "actor_user_id": "11111"}]
        blockers = evaluate_signal_approval_gate(
            plan_hash="abc123",
            registry_payload=self._registry(records),
            requested_action="real_submit_auto",
            plan_generated_at="2026-07-06T10:00:00+00:00",
            now="2026-07-06T10:00:00+00:00",
        )
        self.assertEqual(blockers, [])


class PaperSignalApprovalStatusCliTests(unittest.TestCase):
    def test_parser_registers_status_command_without_submit_or_confirm_flags(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["paper-signal-approval-status", "--as-of-date", "2026-07-06"])

        self.assertEqual(args.as_of_date, "2026-07-06")
        self.assertIsNone(args.plan)

        subparser = next(
            action.choices["paper-signal-approval-status"]
            for action in parser._subparsers._group_actions  # noqa: SLF001
            if hasattr(action, "choices") and "paper-signal-approval-status" in action.choices
        )
        option_strings = {option for action in subparser._actions for option in action.option_strings}  # noqa: SLF001
        for forbidden in ("--confirm", "--submit", "--execute-live", "--confirm-real"):
            self.assertNotIn(forbidden, option_strings)

    def test_status_reports_gate_for_all_four_actions_when_plan_given(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            registry_dir = root / "registry"
            plan = make_plan()
            plan_path = root / "signal_plan.json"
            plan_path.write_text(json.dumps(plan), encoding="utf-8")
            plan_hash = compute_plan_hash(plan)

            record_signal_plan_review(
                as_of_date="2026-07-06",
                plan_hash_prefix=plan_hash[:MIN_HASH_PREFIX],
                verdict="approved",
                actor_user_id="67890",
                actor_chat_id="12345",
                source_update_id=1,
                reason="",
                plan=plan,
                registry_dir=registry_dir,
            )

            exit_code = main(
                [
                    "paper-signal-approval-status",
                    "--as-of-date",
                    "2026-07-06",
                    "--registry-dir",
                    str(registry_dir),
                    "--plan",
                    str(plan_path),
                    "--output",
                    str(root / "status.json"),
                ]
            )
            payload = json.loads((root / "status.json").read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 0)
        self.assertFalse(payload["fail_closed"])
        self.assertEqual(payload["record_count"], 1)
        self.assertEqual(payload["plan"]["plan_hash"], plan_hash)
        self.assertEqual(
            set(payload["plan"]["gate"].keys()),
            {"paper_auto", "real_submit_approved", "real_submit_veto_window", "real_submit_auto"},
        )
        self.assertEqual(payload["plan"]["gate"]["real_submit_approved"], [])
        self.assertEqual(payload["safety"]["orders_submitted"], False)


if __name__ == "__main__":
    unittest.main()
