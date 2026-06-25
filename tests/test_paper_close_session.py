import hashlib
import json
import tempfile
import textwrap
import unittest
from pathlib import Path
from typing import Any, cast
from unittest import mock

from trading_ai.cli import build_parser, main
from trading_ai.execution.paper_close_session import run_paper_close_session
from trading_ai.execution.paper_observability import build_paper_observability_report


class FakeCloseoutClient:
    def __init__(
        self,
        *,
        order: dict[str, Any] | None,
        positions: list[object] | None = None,
    ) -> None:
        self.order = order
        self.positions = positions or []
        self.get_orders_calls: list[object] = []

    def get_account(self) -> object:
        class Account:
            id = "paper-account"
            status = "ACTIVE"
            cash = "10000.00"
            equity = "10000.00"
            buying_power = "9999.00"

        return Account()

    def list_positions(self) -> list[object]:
        return list(self.positions)

    def get_orders(self, filter: object | None = None) -> list[object]:
        self.get_orders_calls.append(filter)
        return []

    def get_order_by_client_id(self, client_order_id: str) -> dict[str, Any]:
        if self.order is None:
            raise ValueError("not found")
        return {**self.order, "client_order_id": client_order_id}


class Position:
    symbol = "SPY"
    qty = "0.002"
    market_value = "1.01"


class PaperCloseSessionTests(unittest.TestCase):
    def test_parser_defaults_keep_closeout_explicit(self) -> None:
        args = build_parser().parse_args(["paper-close-session", "--session-dir", "session"])

        self.assertFalse(args.confirm_paper)
        self.assertIsNone(args.execution_report)
        self.assertIsNone(args.output_dir)
        self.assertIsNone(args.ledger_output)

    def test_missing_confirm_returns_two_without_client(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            session_dir = write_submitted_session(Path(temp_dir))
            with mock.patch(
                "trading_ai.execution.paper_close_session.build_alpaca_paper_client",
                side_effect=AssertionError("client should not be built"),
            ):
                exit_code = main(["paper-close-session", "--session-dir", str(session_dir)])

        self.assertEqual(exit_code, 2)

    def test_blocked_session_returns_one_without_client_or_closeout(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            session_dir = write_submitted_session(Path(temp_dir), ready=False, fail_count=1)
            with mock.patch(
                "trading_ai.execution.paper_close_session.build_alpaca_paper_client",
                side_effect=AssertionError("client should not be built"),
            ):
                exit_code = main(
                    [
                        "paper-close-session",
                        "--session-dir",
                        str(session_dir),
                        "--confirm-paper",
                    ]
                )

        self.assertEqual(exit_code, 1)
        self.assertFalse((session_dir / "closeout").exists())

    def test_execution_report_not_submitted_returns_one_without_client(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            session_dir = write_submitted_session(Path(temp_dir), execution_status="BLOCKED")
            with mock.patch(
                "trading_ai.execution.paper_close_session.build_alpaca_paper_client",
                side_effect=AssertionError("client should not be built"),
            ):
                exit_code = main(
                    [
                        "paper-close-session",
                        "--session-dir",
                        str(session_dir),
                        "--confirm-paper",
                    ]
                )

        self.assertEqual(exit_code, 1)
        self.assertFalse((session_dir / "closeout").exists())

    def test_schema_invalid_execution_report_returns_unmatched_without_client(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            session_dir = write_submitted_session(Path(temp_dir))
            execution = read_json(session_dir / "execution" / "paper_execution.json")
            execution.pop("order_sent", None)
            write_json(session_dir / "execution" / "paper_execution.json", execution)

            with mock.patch(
                "trading_ai.execution.paper_close_session.build_alpaca_paper_client",
                side_effect=AssertionError("client should not be built"),
            ):
                exit_code = main(
                    [
                        "paper-close-session",
                        "--session-dir",
                        str(session_dir),
                        "--confirm-paper",
                    ]
                )
            payload = read_json(session_dir / "closeout" / "paper_closeout.json")

        self.assertEqual(exit_code, 1)
        self.assertEqual(payload["status"], "UNMATCHED")
        self.assertIn("execution_schema_invalid", payload["reasons"])
        self.assertIn("execution_order_sent_missing", payload["reasons"])

    def test_invalid_json_returns_two_without_client(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            session_dir = write_submitted_session(Path(temp_dir))
            (session_dir / "execution" / "paper_execution.json").write_text("{bad json", encoding="utf-8")
            with mock.patch(
                "trading_ai.execution.paper_close_session.build_alpaca_paper_client",
                side_effect=AssertionError("client should not be built"),
            ):
                exit_code = main(
                    [
                        "paper-close-session",
                        "--session-dir",
                        str(session_dir),
                        "--confirm-paper",
                    ]
                )

        self.assertEqual(exit_code, 2)

    def test_missing_execution_report_returns_two_without_client(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            session_dir = write_submitted_session(Path(temp_dir))
            (session_dir / "execution" / "paper_execution.json").unlink()
            with mock.patch(
                "trading_ai.execution.paper_close_session.build_alpaca_paper_client",
                side_effect=AssertionError("client should not be built"),
            ):
                exit_code = main(
                    [
                        "paper-close-session",
                        "--session-dir",
                        str(session_dir),
                        "--confirm-paper",
                    ]
                )

        self.assertEqual(exit_code, 2)

    def test_broker_connection_error_writes_closeout_error_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            session_dir = write_submitted_session(Path(temp_dir))

            with mock.patch(
                "trading_ai.execution.paper_close_session.build_alpaca_paper_client",
                side_effect=RuntimeError("broker unavailable token=DO-NOT-KEEP"),
            ):
                exit_code = main(
                    [
                        "paper-close-session",
                        "--session-dir",
                        str(session_dir),
                        "--confirm-paper",
                    ]
                )

            payload = read_json(session_dir / "closeout" / "paper_closeout.json")

        self.assertEqual(exit_code, 2)
        self.assertEqual(payload["status"], "ERROR")
        self.assertIn("broker unavailable", " ".join(payload["reasons"]))
        self.assertNotIn("DO-NOT-KEEP", json.dumps(payload))

    def test_execution_order_mismatch_writes_unmatched_without_client(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            session_dir = write_submitted_session(Path(temp_dir))
            execution_path = session_dir / "execution" / "paper_execution.json"
            execution = read_json(execution_path)
            execution["order_sent"]["notional"] = 2.0
            write_json(execution_path, execution)
            with mock.patch(
                "trading_ai.execution.paper_close_session.build_alpaca_paper_client",
                side_effect=AssertionError("client should not be built"),
            ):
                exit_code = main(
                    [
                        "paper-close-session",
                        "--session-dir",
                        str(session_dir),
                        "--confirm-paper",
                    ]
                )
            payload = read_json(session_dir / "closeout" / "paper_closeout.json")

        self.assertEqual(exit_code, 1)
        self.assertEqual(payload["status"], "UNMATCHED")
        self.assertIn("execution_notional_mismatch", payload["reasons"])

    def test_filled_order_with_position_writes_closed_closeout(self) -> None:
        client = FakeCloseoutClient(order=broker_order(status="filled", filled_qty="0.002"), positions=[Position()])
        with tempfile.TemporaryDirectory() as temp_dir:
            session_dir = write_submitted_session(Path(temp_dir))
            with mock.patch("trading_ai.execution.paper_close_session.build_alpaca_paper_client", return_value=client):
                exit_code = main(
                    [
                        "paper-close-session",
                        "--session-dir",
                        str(session_dir),
                        "--confirm-paper",
                    ]
                )
            payload = read_json(session_dir / "closeout" / "paper_closeout.json")
            markdown = (session_dir / "closeout" / "paper_closeout.md").read_text(encoding="utf-8")

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["status"], "CLOSED")
        self.assertEqual(payload["expected_order"]["client_order_id"], "signal-spy-20260616")
        self.assertEqual(payload["broker_order"]["status"], "filled")
        self.assertEqual(payload["reasons"], [])
        self.assertIn("Status: **CLOSED**", markdown)

    def test_custom_paper_notional_from_risk_limits_closes_successfully(self) -> None:
        client = FakeCloseoutClient(
            order=broker_order(status="filled", filled_qty="0.004", notional="2.0"),
            positions=[Position()],
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            session_dir = write_submitted_session(Path(temp_dir), signal_notional=2.0, risk_notional=2.0)
            with mock.patch("trading_ai.execution.paper_close_session.build_alpaca_paper_client", return_value=client):
                exit_code = main(
                    [
                        "paper-close-session",
                        "--session-dir",
                        str(session_dir),
                        "--confirm-paper",
                    ]
                )
            payload = read_json(session_dir / "closeout" / "paper_closeout.json")

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["status"], "CLOSED")
        self.assertEqual(payload["expected_order"]["notional"], 2.0)
        self.assertEqual(payload["broker_order"]["notional"], 2.0)
        self.assertEqual(payload["reasons"], [])

    def test_close_blocks_when_campaign_evidence_hash_changes_after_review(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            session_dir = write_submitted_session(root, signal_notional=2.0, risk_notional=2.0)
            campaign = root / "campaign.json"
            campaign_payload = read_json(campaign)
            campaign_payload["real_money_consideration"]["error_days"] = 1
            write_json(campaign, campaign_payload)

            with mock.patch(
                "trading_ai.execution.paper_close_session.build_alpaca_paper_client",
                side_effect=AssertionError("client should not be built"),
            ):
                result = run_paper_close_session(session_dir=session_dir, confirm_paper=True)

        self.assertEqual(result.exit_code, 1)
        self.assertEqual(result.status, "BLOCKED")
        self.assertIn("paper_campaign_evidence_hash_mismatch", result.reasons)
        self.assertFalse((session_dir / "closeout").exists())

    def test_accepted_order_without_fill_or_position_writes_pending(self) -> None:
        client = FakeCloseoutClient(order=broker_order(status="accepted", filled_qty="0"))
        with tempfile.TemporaryDirectory() as temp_dir:
            session_dir = write_submitted_session(Path(temp_dir))
            with mock.patch("trading_ai.execution.paper_close_session.build_alpaca_paper_client", return_value=client):
                exit_code = main(
                    [
                        "paper-close-session",
                        "--session-dir",
                        str(session_dir),
                        "--confirm-paper",
                    ]
                )
            payload = read_json(session_dir / "closeout" / "paper_closeout.json")

        self.assertEqual(exit_code, 1)
        self.assertEqual(payload["status"], "PENDING")
        self.assertIn("not_filled_yet", payload["reasons"])
        self.assertIn("position_missing", payload["reasons"])

    def test_missing_mismatched_or_rejected_orders_write_unmatched(self) -> None:
        cases = [
            (None, "order_missing"),
            (broker_order(symbol="QQQ"), "broker_symbol_mismatch"),
            (broker_order(status="rejected"), "order_rejected"),
            (broker_order(status="canceled"), "order_canceled"),
            (broker_order(status="expired"), "order_expired"),
        ]
        for order, reason in cases:
            with self.subTest(reason=reason):
                client = FakeCloseoutClient(order=order)
                with tempfile.TemporaryDirectory() as temp_dir:
                    session_dir = write_submitted_session(Path(temp_dir))
                    with mock.patch(
                        "trading_ai.execution.paper_close_session.build_alpaca_paper_client",
                        return_value=client,
                    ):
                        exit_code = main(
                            [
                                "paper-close-session",
                                "--session-dir",
                                str(session_dir),
                                "--confirm-paper",
                            ]
                        )
                    payload = read_json(session_dir / "closeout" / "paper_closeout.json")

                self.assertEqual(exit_code, 1)
                self.assertEqual(payload["status"], "UNMATCHED")
                self.assertIn(reason, payload["reasons"])

    def test_ledger_opt_in_writes_closeout_event_only_when_requested(self) -> None:
        client = FakeCloseoutClient(order=broker_order(status="filled", filled_qty="0.002"), positions=[Position()])
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            session_dir = write_submitted_session(root)
            ledger = root / "ledger.jsonl"
            with mock.patch("trading_ai.execution.paper_close_session.build_alpaca_paper_client", return_value=client):
                exit_without_ledger = main(
                    [
                        "paper-close-session",
                        "--session-dir",
                        str(session_dir),
                        "--confirm-paper",
                    ]
                )
            exists_without_flag = ledger.exists()
            session_dir_with_ledger = write_submitted_session(root / "with_ledger")
            with mock.patch("trading_ai.execution.paper_close_session.build_alpaca_paper_client", return_value=client):
                exit_with_ledger = main(
                    [
                        "paper-close-session",
                        "--session-dir",
                        str(session_dir_with_ledger),
                        "--confirm-paper",
                        "--ledger-output",
                        str(ledger),
                    ]
                )
            event = read_jsonl(ledger)[0]

        self.assertEqual(exit_without_ledger, 0)
        self.assertFalse(exists_without_flag)
        self.assertEqual(exit_with_ledger, 0)
        self.assertEqual(event["event_type"], "paper_closeout")
        self.assertEqual(event["status"], "CLOSED")
        self.assertEqual(event["client_order_id"], "signal-spy-20260616")

    def test_observability_discovers_closeout_and_counts_statuses(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            closed = write_observable_closeout(root / "sessions" / "closed", status="CLOSED")
            pending = write_observable_closeout(
                root / "sessions" / "pending",
                status="PENDING",
                reasons=["not_filled_yet"],
            )
            unmatched = write_observable_closeout(
                root / "sessions" / "unmatched",
                status="UNMATCHED",
                reasons=["order_missing"],
            )

            report = cast(
                dict[str, Any],
                build_paper_observability_report(
                    sessions_root=root / "sessions",
                    session_dirs=[closed, pending, unmatched],
                    generated_at="2026-06-16T00:00:00+00:00",
                ).to_dict(),
            )

        self.assertEqual(report["summary"]["closeouts_closed"], 1)
        self.assertEqual(report["summary"]["closeouts_pending"], 1)
        self.assertEqual(report["summary"]["closeouts_unmatched"], 1)
        self.assertEqual(report["summary"]["blockers"]["not_filled_yet"], 1)
        self.assertEqual(report["summary"]["blockers"]["order_missing"], 1)


def write_submitted_session(
    root: Path,
    *,
    ready: bool = True,
    fail_count: int = 0,
    execution_status: str = "SUBMITTED",
    signal_notional: float = 1.0,
    risk_notional: float = 1.0,
) -> Path:
    session_dir = root / "paper_session"
    (session_dir / "audit").mkdir(parents=True)
    (session_dir / "paper").mkdir()
    (session_dir / "fresh_data").mkdir()
    (session_dir / "execution").mkdir()
    config = write_universe(root / "universe.yml")
    risk = write_risk(root / "risk.yml", paper_notional_usd=risk_notional)
    signal = signal_report(notional=signal_notional)
    campaign = None
    if risk_notional != 1.0:
        campaign = root / "campaign.json"
        write_json(
            campaign,
            {
                "real_money_consideration": {
                    "state": "PAPER_EVIDENCE_READY",
                    "clean_trial_days": 30,
                    "target_trial_days": 30,
                    "recovery_days": 0,
                    "error_days": 0,
                }
            },
        )
    graduation = paper_graduation_payload(paper_notional_usd=risk_notional, campaign=campaign)
    if risk_notional != 1.0:
        signal["paper_graduation"] = graduation
    session = {
        "schema_version": "1.0",
        "output_dir": str(session_dir),
        "as_of_date": "2026-06-16",
        "ready_for_paper_review": ready,
        "exit_code": 0 if ready else 1,
        "inputs": {
            "config": str(config),
            "risk": str(risk),
            "campaign_report": str(campaign) if campaign is not None else None,
            "phase_review": None,
        },
        "paths": {
            "freshness_report": str(session_dir / "fresh_data" / "freshness.json"),
            "signal_report": str(session_dir / "paper" / "paper_signal_order.json"),
            "audit_report": str(session_dir / "audit" / "paper_audit.json"),
        },
        "summary": {"fail_count": fail_count, "freshness_allowed": ready},
        "paper_graduation": graduation,
    }
    audit = {
        "schema_version": "1.0",
        "ready_for_paper_review": ready,
        "findings": [],
        "summary": {"fail_count": fail_count, "freshness_allowed": ready},
    }
    execution = {
        "schema_version": "1.0",
        "generated_at": "2026-06-16T00:02:00+00:00",
        "status": execution_status,
        "session": {"session_dir": str(session_dir), "ready_for_paper_review": ready},
        "preflight": {"allowed": True, "reasons": []},
        "order_sent": signal["order_intent"],
        "broker_result": {"accepted": execution_status == "SUBMITTED", "status": "submitted", "reasons": []},
    }
    write_json(session_dir / "session.json", session)
    write_json(session_dir / "audit" / "paper_audit.json", audit)
    write_json(session_dir / "paper" / "paper_signal_order.json", signal)
    write_json(session_dir / "fresh_data" / "freshness.json", {"allowed": ready, "reasons": []})
    write_json(session_dir / "execution" / "paper_execution.json", execution)
    return session_dir


def write_observable_closeout(session_dir: Path, *, status: str, reasons: list[str] | None = None) -> Path:
    session_dir = write_submitted_session(session_dir, execution_status="SUBMITTED")
    closeout_dir = session_dir / "closeout"
    closeout_dir.mkdir()
    write_json(
        closeout_dir / "paper_closeout.json",
        {
            "schema_version": "1.0",
            "generated_at": "2026-06-16T00:03:00+00:00",
            "status": status,
            "session": {"session_dir": str(session_dir), "ready_for_paper_review": True},
            "expected_order": signal_report()["order_intent"],
            "reasons": reasons or [],
        },
    )
    return session_dir


def signal_report(*, notional: float = 1.0) -> dict[str, Any]:
    return {
        "mode": "dry-run",
        "broker": "alpaca",
        "freshness_allowed": True,
        "preflight": {"allowed": True, "reasons": [], "checked_at": "2026-06-16", "max_feature_age_days": 5},
        "open_orders": [],
        "positions": [],
        "submitted": True,
        "selected_signal": {
            "timestamp": "2026-06-16",
            "symbol": "SPY",
            "probability": 0.93,
            "threshold": 0.5,
            "action": "buy",
        },
        "order_intent": {
            "symbol": "SPY",
            "side": "buy",
            "client_order_id": "signal-spy-20260616",
            "type": "market",
            "time_in_force": "day",
            "notional": notional,
        },
        "order_result": {
            "accepted": True,
            "status": "dry_run_accepted",
            "reasons": [],
            "dry_run": True,
            "broker_response": None,
        },
    }


def broker_order(
    *,
    status: str = "filled",
    filled_qty: str = "0.002",
    symbol: str = "SPY",
    side: str = "buy",
    notional: str = "1.0",
) -> dict[str, Any]:
    return {
        "id": "broker-order-1",
        "client_order_id": "signal-spy-20260616",
        "symbol": symbol,
        "side": side,
        "type": "market",
        "time_in_force": "day",
        "status": status,
        "notional": notional,
        "qty": None,
        "filled_qty": filled_qty,
        "filled_avg_price": "500.0" if filled_qty != "0" else None,
        "submitted_at": "2026-06-16T22:07:42Z",
        "created_at": "2026-06-16T22:07:42Z",
        "updated_at": "2026-06-16T22:07:43Z",
        "expires_at": "2026-06-17T20:00:00Z",
    }


def write_universe(path: Path) -> Path:
    path.write_text(
        textwrap.dedent(
            """
            universe:
              symbols: [SPY]
            """
        ),
        encoding="utf-8",
    )
    return path


def write_risk(path: Path, *, paper_notional_usd: float = 1.0) -> Path:
    stage_lines = ""
    if paper_notional_usd != 1.0:
        stage_lines = """
              paper_stage: SCALE_UP
              paper_stage_reviewer: reviewer@example.com
              paper_stage_reason: clean paper campaign
"""
    path.write_text(
        textwrap.dedent(
            f"""
            risk_limits:
              max_daily_loss_pct: 0.02
              max_drawdown_pct: 0.10
              max_gross_exposure: 1.0
              max_single_position: 0.30
              paper_notional_usd: {paper_notional_usd}
{stage_lines.rstrip()}
              live_trading_allowed: false
            """
        ),
        encoding="utf-8",
    )
    return path


def paper_graduation_payload(*, paper_notional_usd: float, campaign: Path | None = None) -> dict[str, Any]:
    stage = "CANARY" if paper_notional_usd == 1.0 else "SCALE_UP"
    campaign_evidence: dict[str, Any] = {"provided": stage != "CANARY"}
    if campaign is not None:
        campaign_evidence["path"] = str(campaign)
        campaign_evidence["sha256"] = sha256_file(campaign)
    return {
        "stage": stage,
        "paper_notional_usd": paper_notional_usd,
        "stage_cap_usd": 1.0 if stage == "CANARY" else 5.0,
        "reviewer": None if stage == "CANARY" else "reviewer@example.com",
        "reason": None if stage == "CANARY" else "clean paper campaign",
        "allowed": True,
        "blockers": [],
        "evidence": {"campaign_report": campaign_evidence, "phase_review": {"provided": False}},
    }


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


if __name__ == "__main__":
    unittest.main()
