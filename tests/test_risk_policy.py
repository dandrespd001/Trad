import unittest

from trading_ai.risk.policy import RiskLimits, evaluate_risk_state


class RiskPolicyTests(unittest.TestCase):
    def test_live_mode_is_blocked_by_default(self) -> None:
        decision = evaluate_risk_state(
            daily_pnl_pct=0.0,
            current_drawdown_pct=0.0,
            gross_exposure=0.25,
            largest_position_weight=0.10,
            mode="live",
            limits=RiskLimits(),
        )

        self.assertFalse(decision.allowed)
        self.assertIn("live_trading_not_authorized", decision.reasons)
        self.assertIn("disable_trading", decision.actions)

    def test_paper_mode_allows_state_inside_limits(self) -> None:
        decision = evaluate_risk_state(
            daily_pnl_pct=-0.005,
            current_drawdown_pct=0.02,
            gross_exposure=0.70,
            largest_position_weight=0.20,
            mode="paper",
            limits=RiskLimits(),
        )

        self.assertTrue(decision.allowed)
        self.assertEqual(decision.reasons, [])
        self.assertEqual(decision.actions, ["allow"])

    def test_daily_loss_and_drawdown_breach_disable_trading(self) -> None:
        decision = evaluate_risk_state(
            daily_pnl_pct=-0.03,
            current_drawdown_pct=0.12,
            gross_exposure=0.70,
            largest_position_weight=0.20,
            mode="paper",
            limits=RiskLimits(max_daily_loss_pct=0.02, max_drawdown_pct=0.10),
        )

        self.assertFalse(decision.allowed)
        self.assertIn("daily_loss_limit_breached", decision.reasons)
        self.assertIn("drawdown_limit_breached", decision.reasons)
        self.assertIn("disable_trading", decision.actions)

    def test_exposure_breaches_require_position_reduction(self) -> None:
        decision = evaluate_risk_state(
            daily_pnl_pct=0.0,
            current_drawdown_pct=0.01,
            gross_exposure=1.25,
            largest_position_weight=0.35,
            mode="paper",
            limits=RiskLimits(max_gross_exposure=1.0, max_single_position=0.30),
        )

        self.assertFalse(decision.allowed)
        self.assertIn("gross_exposure_limit_breached", decision.reasons)
        self.assertIn("single_position_limit_breached", decision.reasons)
        self.assertIn("reduce_positions", decision.actions)


if __name__ == "__main__":
    unittest.main()
