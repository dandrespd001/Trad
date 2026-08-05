import json
import tempfile
import unittest
from pathlib import Path

from trading_ai import config as config_module
from trading_ai.execution import paper_graduation
from trading_ai.execution.live_stage_policy import (
    LIVE_CANARY_STAGE,
    LIVE_SCALE_UP_STAGE,
    LIVE_STAGES,
    evaluate_live_stage_policy,
    write_live_stage_scorecard,
)


class LiveStagePolicyTests(unittest.TestCase):
    def test_live_stages_are_separate_from_paper_stages(self) -> None:
        self.assertEqual(LIVE_STAGES, frozenset({LIVE_CANARY_STAGE, LIVE_SCALE_UP_STAGE}))
        for stage in LIVE_STAGES:
            self.assertNotIn(stage, config_module.PAPER_STAGES)
            self.assertNotIn(stage, paper_graduation.PAPER_STAGES)

    def test_blocks_scale_without_required_clean_live_evidence(self) -> None:
        result = evaluate_live_stage_policy(
            target_stage=LIVE_SCALE_UP_STAGE,
            requested_notional_usd=75.0,
            canary_sessions=[
                clean_session(slippage_bps=4.0),
                clean_session(slippage_bps=12.0),
                {**clean_session(), "rollback_triggered": True},
            ],
            clean_sessions_required=3,
            reviewer="",
            reason="",
            approval_reference="",
            release_gate_passed=False,
            secrets_rotated=False,
            llm_drift_ok=False,
            model_drift_ok=False,
            max_slippage_bps=5.0,
        )

        self.assertEqual(result["status"], "BLOCKED")
        for blocker in (
            "clean_live_sessions_below_threshold",
            "human_approval_required",
            "release_gate_not_green",
            "secrets_not_rotated",
            "llm_drift_review_failed",
            "model_drift_review_failed",
            "session_slippage_bps_exceeded:1",
            "session_rollback_triggered:2",
        ):
            self.assertIn(blocker, result["blockers"])
        self.assertFalse(result["safety"]["orders_submitted"])

    def test_approves_scale_review_for_clean_sessions_in_usd_50_100_range(self) -> None:
        result = evaluate_live_stage_policy(
            target_stage=LIVE_SCALE_UP_STAGE,
            requested_notional_usd=75.0,
            canary_sessions=[clean_session(order_id=f"order-{idx}") for idx in range(3)],
            clean_sessions_required=3,
            reviewer="ops",
            reason="three clean USD 1 canaries",
            approval_reference="ticket-123",
            release_gate_passed=True,
            secrets_rotated=True,
            llm_drift_ok=True,
            model_drift_ok=True,
            max_slippage_bps=5.0,
        )

        self.assertEqual(result["status"], "APPROVED_FOR_LIVE_SCALE_REVIEW")
        self.assertEqual(result["recommended_notional_range_usd"], [50.0, 100.0])
        self.assertEqual(result["scorecard"]["clean_sessions"], 3)
        self.assertEqual(result["scorecard"]["max_slippage_bps"], 4.0)
        self.assertEqual(result["scorecard"]["max_latency_ms"], 250.0)
        self.assertEqual(result["scorecard"]["breaker_trips"], 0)
        self.assertGreater(result["scorecard"]["net_edge_bps"], 0)

    def test_nonfinite_numbers_and_string_booleans_never_approve(self) -> None:
        session = clean_session(
            orders_submitted="true",
            rollback_triggered="false",
            breaker_tripped="false",
            slippage_bps=float("nan"),
            latency_ms=float("inf"),
            net_edge_bps=float("nan"),
        )
        result = evaluate_live_stage_policy(
            target_stage=LIVE_SCALE_UP_STAGE,
            requested_notional_usd=float("nan"),
            canary_sessions=[session],
            clean_sessions_required=1,
            reviewer="ops",
            reason="review",
            approval_reference="ticket-unsafe",
            release_gate_passed="true",  # type: ignore[arg-type]
            secrets_rotated="true",  # type: ignore[arg-type]
            llm_drift_ok="true",  # type: ignore[arg-type]
            model_drift_ok="true",  # type: ignore[arg-type]
            max_slippage_bps=float("nan"),
            max_latency_ms=float("inf"),
        )

        self.assertEqual(result["status"], "BLOCKED")
        self.assertIsNone(result["requested_notional_usd"])
        self.assertIn("requested_notional_usd_invalid", result["blockers"])
        self.assertIn("release_gate_not_green", result["blockers"])
        self.assertIn("session_order_missing:0", result["blockers"])
        self.assertNotIn("NaN", json.dumps(result, allow_nan=False))

    def test_partial_or_accepted_fills_and_duplicate_order_ids_do_not_count_clean(self) -> None:
        sessions = [
            clean_session(fill_status="accepted", order_id="same-order"),
            clean_session(fill_status="partially_filled", order_id="same-order"),
        ]
        result = evaluate_live_stage_policy(
            target_stage=LIVE_SCALE_UP_STAGE,
            requested_notional_usd=75.0,
            canary_sessions=sessions,
            clean_sessions_required=2,
            reviewer="ops",
            reason="review",
            approval_reference="ticket-duplicate",
            release_gate_passed=True,
            secrets_rotated=True,
            llm_drift_ok=True,
            model_drift_ok=True,
            max_slippage_bps=5.0,
        )

        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(result["scorecard"]["clean_sessions"], 0)
        self.assertIn("session_fill_not_confirmed:0", result["blockers"])
        self.assertIn("duplicate_order_id:1", result["blockers"])

    def test_drawdown_sign_or_magnitude_must_be_explicitly_valid(self) -> None:
        for drawdown in (-0.01, 1.1, float("nan")):
            with self.subTest(drawdown=drawdown):
                result = evaluate_live_stage_policy(
                    target_stage=LIVE_CANARY_STAGE,
                    requested_notional_usd=1.0,
                    canary_sessions=[clean_session(drawdown_pct=drawdown)],
                    clean_sessions_required=1,
                    reviewer="ops",
                    reason="review",
                    approval_reference="ticket-drawdown",
                    release_gate_passed=True,
                    secrets_rotated=True,
                    llm_drift_ok=True,
                    model_drift_ok=True,
                    max_slippage_bps=5.0,
                    max_drawdown_pct=0.05,
                )
                self.assertEqual(result["status"], "BLOCKED")
                self.assertIn("session_drawdown_invalid:0", result["blockers"])

        exceeded = evaluate_live_stage_policy(
            target_stage=LIVE_CANARY_STAGE,
            requested_notional_usd=1.0,
            canary_sessions=[clean_session(drawdown_pct=0.06)],
            clean_sessions_required=1,
            reviewer="ops",
            reason="review",
            approval_reference="ticket-drawdown",
            release_gate_passed=True,
            secrets_rotated=True,
            llm_drift_ok=True,
            model_drift_ok=True,
            max_slippage_bps=5.0,
            max_drawdown_pct=0.05,
        )
        self.assertEqual(exceeded["status"], "BLOCKED")
        self.assertIn("session_drawdown_exceeded:0", exceeded["blockers"])

    def test_write_live_stage_scorecard_writes_json_and_markdown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            artifacts = write_live_stage_scorecard(
                output_dir=Path(tmp),
                as_of_date="2026-06-26",
                target_stage=LIVE_SCALE_UP_STAGE,
                requested_notional_usd=50.0,
                canary_sessions=[clean_session(order_id=f"order-{idx}") for idx in range(3)],
                clean_sessions_required=3,
                reviewer="ops",
                reason="ready for limited scale review",
                approval_reference="ticket-456",
                release_gate_passed=True,
                secrets_rotated=True,
                llm_drift_ok=True,
                model_drift_ok=True,
                max_slippage_bps=5.0,
            )
            payload = json.loads(artifacts.json_path.read_text(encoding="utf-8"))
            markdown = artifacts.markdown_path.read_text(encoding="utf-8")

        self.assertEqual(payload["target_stage"], LIVE_SCALE_UP_STAGE)
        self.assertIn("Live Stage Scorecard", markdown)
        self.assertIn("APPROVED_FOR_LIVE_SCALE_REVIEW", markdown)


def clean_session(**overrides: object) -> dict[str, object]:
    session = {
        "status": "SUBMITTED",
        "order_id": "live-order-1",
        "notional_usd": 1.0,
        "orders_submitted": True,
        "fill_status": "filled",
        "rollback_triggered": False,
        "breaker_tripped": False,
        "slippage_bps": 4.0,
        "latency_ms": 250.0,
        "alert_tier": "canary",
        "drawdown_pct": 0.0,
        "net_edge_bps": 3.0,
    }
    session.update(overrides)
    return session


if __name__ == "__main__":
    unittest.main()
