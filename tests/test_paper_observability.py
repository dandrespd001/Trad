import json
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

from trading_ai.cli import build_parser, main
from trading_ai.data.io import write_records
from trading_ai.data.sample import generate_sample_ohlcv
from trading_ai.execution.paper_observability import build_paper_observability_report
from trading_ai.models.baseline import LogisticBaselineModel, save_model


class FakeApprovedExecutionClient:
    def __init__(self) -> None:
        self.submitted_orders: list[dict[str, object]] = []

    def get_account(self) -> object:
        class Account:
            id = "paper-account"
            status = "ACTIVE"
            cash = "10000.00"
            equity = "10000.00"
            buying_power = "9999.00"

        return Account()

    def list_positions(self) -> list[object]:
        return []

    def get_orders(self, filter: object | None = None) -> list[object]:
        return []

    def submit_order(self, **kwargs: object) -> dict[str, object]:
        self.submitted_orders.append(kwargs)
        return {"id": "broker-order-1", "status": "accepted", **kwargs}

    def get_order_by_client_id(self, client_order_id: str) -> dict[str, object]:
        submitted = self.submitted_orders[-1]
        return {
            "id": "broker-order-1",
            "client_order_id": client_order_id,
            "symbol": submitted["symbol"],
            "side": submitted["side"],
            "type": submitted["type"],
            "time_in_force": submitted["time_in_force"],
            "status": "accepted",
            "notional": submitted.get("notional"),
            "qty": submitted.get("qty"),
            "filled_qty": "0",
            "filled_avg_price": None,
            "submitted_at": "2026-06-16T22:07:42Z",
            "created_at": "2026-06-16T22:07:42Z",
            "updated_at": "2026-06-16T22:07:43Z",
            "expires_at": "2026-06-17T20:00:00Z",
        }


class FakeReconcileClient:
    def get_account(self) -> object:
        class Account:
            id = "paper-account"
            status = "ACTIVE"
            cash = "10000.00"
            equity = "10000.00"
            buying_power = "9999.00"

        return Account()

    def list_positions(self) -> list[object]:
        return []

    def get_order_by_client_id(self, client_id: str) -> dict[str, object]:
        return {
            "id": "broker-order-1",
            "client_order_id": client_id,
            "symbol": "SPY",
            "side": "buy",
            "type": "market",
            "time_in_force": "day",
            "status": "accepted",
            "notional": "1",
            "qty": None,
            "filled_qty": "0",
            "filled_avg_price": None,
            "submitted_at": "2026-06-16T22:07:42Z",
            "created_at": "2026-06-16T22:07:42Z",
            "updated_at": "2026-06-16T22:07:43Z",
            "expires_at": "2026-06-17T20:00:00Z",
        }


class PaperObservabilityTests(unittest.TestCase):
    def test_ready_session_with_submitted_execution_summarizes_totals(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            session_dir = write_observable_session(root / "sessions" / "latest", ready=True, with_execution=True)

            report = build_paper_observability_report(
                sessions_root=root / "sessions",
                session_dirs=[session_dir],
                generated_at="2026-06-16T00:00:00+00:00",
            ).to_dict()

        self.assertEqual(report["summary"]["sessions_ready"], 1)
        self.assertEqual(report["summary"]["sessions_blocked"], 0)
        self.assertEqual(report["summary"]["executions_submitted"], 1)
        self.assertEqual(report["summary"]["executions_blocked"], 0)
        self.assertEqual(report["summary"]["blockers"], {})
        session_events = [event for event in report["events"] if event["event_type"] == "paper_session"]
        self.assertEqual(session_events[0]["as_of_date"], "2026-06-16")

    def test_blocked_session_aggregates_blocker_codes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            write_observable_session(root / "sessions" / "blocked", ready=False, finding_code="freshness_blocked")

            report = build_paper_observability_report(
                sessions_root=root / "sessions",
                generated_at="2026-06-16T00:00:00+00:00",
            ).to_dict()

        self.assertEqual(report["summary"]["sessions_blocked"], 1)
        self.assertEqual(report["summary"]["blockers"]["freshness_blocked"], 1)

    def test_ledger_reconciliation_and_cancel_events_are_aggregated(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            ledger = root / "ledger.jsonl"
            ledger.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "schema_version": "1.0",
                                "event_type": "paper_reconciliation",
                                "generated_at": "2026-06-16T01:00:00+00:00",
                                "status": "UNMATCHED",
                                "exit_code": 0,
                                "client_order_id": "signal-spy-20260616",
                                "symbol": "SPY",
                                "side": "buy",
                                "notional": 1.0,
                                "reconciliation_matched": False,
                                "reasons": ["not_filled_yet"],
                            }
                        ),
                        json.dumps(
                            {
                                "schema_version": "1.0",
                                "event_type": "paper_cancel_order",
                                "generated_at": "2026-06-16T02:00:00+00:00",
                                "status": "CANCELLED",
                                "exit_code": 0,
                                "client_order_id": "signal-spy-20260616",
                            }
                        ),
                    ]
                ),
                encoding="utf-8",
            )

            report = build_paper_observability_report(
                sessions_root=root / "empty",
                ledger_inputs=[ledger],
                generated_at="2026-06-16T00:00:00+00:00",
            ).to_dict()

        self.assertEqual(report["summary"]["reconciliations_unmatched"], 1)
        self.assertEqual(report["summary"]["reconciliations_matched"], 0)
        self.assertEqual(report["summary"]["cancellations"], 1)
        self.assertEqual(report["summary"]["blockers"]["not_filled_yet"], 1)

    def test_invalid_or_missing_session_artifacts_create_diagnostics(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            session_dir = write_observable_session(root / "sessions" / "bad", ready=True)
            (session_dir / "paper" / "paper_signal_order.json").write_text("{bad json", encoding="utf-8")
            (session_dir / "audit" / "paper_audit.json").unlink()

            report = build_paper_observability_report(
                sessions_root=root / "sessions",
                generated_at="2026-06-16T00:00:00+00:00",
            ).to_dict()

        reasons = {diagnostic["reason"] for diagnostic in report["diagnostics"]}
        self.assertIn("invalid_json", reasons)
        self.assertIn("missing_artifact", reasons)
        self.assertEqual(report["summary"]["diagnostics"], 2)


class PaperObservabilityCliTests(unittest.TestCase):
    def test_parser_defaults_for_observability_and_ledger_outputs(self) -> None:
        observability = build_parser().parse_args(["paper-observability"])
        paper_session = build_parser().parse_args(
            [
                "paper-session",
                "--source-csv",
                "source.csv",
                "--from",
                "2026-03-01",
                "--to",
                "2026-06-16",
            ]
        )
        paper_execute = build_parser().parse_args(["paper-execute-session", "--session-dir", "session"])

        self.assertEqual(observability.sessions_root, "reports/tmp/paper_session")
        self.assertEqual(observability.session_dir, [])
        self.assertEqual(observability.ledger_input, [])
        self.assertEqual(observability.output, "reports/tmp/paper_observability/latest.json")
        self.assertEqual(observability.markdown_output, "reports/tmp/paper_observability/latest.md")
        self.assertIsNone(paper_session.ledger_output)
        self.assertIsNone(paper_execute.ledger_output)

    def test_paper_session_only_writes_ledger_when_flag_is_provided(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = write_sample_source(root / "source.csv")
            output_dir = root / "paper_session"
            ledger = root / "paper_ledger.jsonl"

            exit_without_ledger = main(paper_session_args(root, source=source, output_dir=output_dir))
            exists_without_flag = ledger.exists()
            exit_with_ledger = main(
                paper_session_args(
                    root,
                    source=source,
                    output_dir=root / "paper_session_with_ledger",
                    extra=["--ledger-output", str(ledger)],
                )
            )
            event = read_jsonl(ledger)[0]

        self.assertEqual(exit_without_ledger, 0)
        self.assertFalse(exists_without_flag)
        self.assertEqual(exit_with_ledger, 0)
        self.assertEqual(event["event_type"], "paper_session")
        self.assertEqual(event["status"], "READY")
        self.assertTrue(event["ready_for_paper_review"])

    def test_paper_execute_session_ledgers_success_and_local_block_without_client_on_block(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            success_session = write_execution_session(root / "success_session")
            blocked_session = write_execution_session(root / "blocked_session", ready=False, fail_count=1)
            ledger = root / "paper_ledger.jsonl"
            client = FakeApprovedExecutionClient()

            with mock.patch(
                "trading_ai.execution.paper_execute_session.build_alpaca_paper_client",
                return_value=client,
            ):
                success_exit = main(
                    [
                        "paper-execute-session",
                        "--session-dir",
                        str(success_session),
                        "--confirm-paper",
                        "--confirm-submit",
                        "--as-of-date",
                        "2026-06-16",
                        "--ledger-output",
                        str(ledger),
                    ]
                )
            with mock.patch(
                "trading_ai.execution.paper_execute_session.build_alpaca_paper_client",
                side_effect=AssertionError("client should not be built for local block"),
            ):
                blocked_exit = main(
                    [
                        "paper-execute-session",
                        "--session-dir",
                        str(blocked_session),
                        "--confirm-paper",
                        "--confirm-submit",
                        "--ledger-output",
                        str(ledger),
                    ]
                )
            events = read_jsonl(ledger)

        self.assertEqual(success_exit, 0)
        self.assertEqual(blocked_exit, 1)
        self.assertEqual(events[0]["event_type"], "paper_execution")
        self.assertEqual(events[0]["status"], "SUBMITTED")
        self.assertEqual(events[0]["client_order_id"], "signal-spy-20260616")
        self.assertEqual(events[1]["event_type"], "paper_execution")
        self.assertEqual(events[1]["status"], "BLOCKED")
        self.assertIn("session_not_ready_for_paper_review", events[1]["reasons"])

    def test_paper_reconcile_order_appends_reconciliation_event(self) -> None:
        source_report = {
            "order_intent": {
                "client_order_id": "signal-spy-20260616",
                "symbol": "SPY",
                "side": "buy",
                "notional": 1.0,
            }
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "signal_order.json"
            output = root / "reconcile.json"
            ledger = root / "paper_ledger.jsonl"
            source.write_text(json.dumps(source_report), encoding="utf-8")

            with mock.patch("trading_ai.cli.build_alpaca_paper_client", return_value=FakeReconcileClient()):
                exit_code = main(
                    [
                        "paper",
                        "--broker",
                        "alpaca",
                        "--real-paper",
                        "--confirm-paper",
                        "--reconcile-order",
                        "--source-report",
                        str(source),
                        "--output",
                        str(output),
                        "--ledger-output",
                        str(ledger),
                    ]
                )
            event = read_jsonl(ledger)[0]

        self.assertEqual(exit_code, 0)
        self.assertEqual(event["event_type"], "paper_reconciliation")
        self.assertFalse(event["reconciliation_matched"])
        self.assertEqual(event["client_order_id"], "signal-spy-20260616")
        self.assertIn("not_filled_yet", event["reasons"])


def write_observable_session(
    session_dir: Path,
    *,
    ready: bool,
    with_execution: bool = False,
    finding_code: str | None = None,
) -> Path:
    (session_dir / "audit").mkdir(parents=True)
    (session_dir / "paper").mkdir()
    (session_dir / "fresh_data").mkdir()
    findings = []
    if finding_code is not None:
        findings.append({"severity": "fail", "code": finding_code, "message": finding_code, "source": "test"})
    session = {
        "schema_version": "1.0",
        "output_dir": str(session_dir),
        "as_of_date": "2026-06-16",
        "ready_for_paper_review": ready,
        "exit_code": 0 if ready else 1,
        "paths": {
            "freshness_report": str(session_dir / "fresh_data" / "freshness.json"),
            "signal_report": str(session_dir / "paper" / "paper_signal_order.json"),
            "audit_report": str(session_dir / "audit" / "paper_audit.json"),
        },
    }
    audit = {
        "schema_version": "1.0",
        "generated_at": "2026-06-16T00:01:00+00:00",
        "ready_for_paper_review": ready,
        "findings": findings,
        "summary": {"fail_count": 0 if ready else 1},
    }
    signal = execution_signal_report()
    freshness = {"allowed": ready, "reasons": [] if ready else ["stale_symbol"]}
    write_json(session_dir / "session.json", session)
    write_json(session_dir / "audit" / "paper_audit.json", audit)
    write_json(session_dir / "paper" / "paper_signal_order.json", signal)
    write_json(session_dir / "fresh_data" / "freshness.json", freshness)
    if with_execution:
        execution_dir = session_dir / "execution"
        execution_dir.mkdir()
        write_json(
            execution_dir / "paper_execution.json",
            {
                "schema_version": "1.0",
                "generated_at": "2026-06-16T00:02:00+00:00",
                "status": "SUBMITTED",
                "session": {"session_dir": str(session_dir), "ready_for_paper_review": True},
                "preflight": {"allowed": True, "reasons": []},
                "order_sent": signal["order_intent"],
                "broker_result": {"accepted": True, "status": "submitted", "reasons": []},
            },
        )
    return session_dir


def write_execution_session(session_dir: Path, *, ready: bool = True, fail_count: int = 0) -> Path:
    session_dir.mkdir(parents=True)
    (session_dir / "audit").mkdir()
    (session_dir / "paper").mkdir()
    (session_dir / "fresh_data").mkdir()
    config = write_universe(session_dir / "universe.yml")
    risk = write_risk(session_dir / "risk.yml")
    session = {
        "schema_version": "1.0",
        "ready_for_paper_review": ready,
        "exit_code": 0 if ready else 1,
        "inputs": {"config": str(config), "risk": str(risk)},
        "paths": {
            "freshness_report": str(session_dir / "fresh_data" / "freshness.json"),
            "signal_report": str(session_dir / "paper" / "paper_signal_order.json"),
            "audit_report": str(session_dir / "audit" / "paper_audit.json"),
        },
        "summary": {"fail_count": fail_count, "freshness_allowed": ready},
    }
    audit = {
        "schema_version": "1.0",
        "ready_for_paper_review": ready,
        "findings": [],
        "summary": {"fail_count": fail_count},
    }
    write_json(session_dir / "session.json", session)
    write_json(session_dir / "audit" / "paper_audit.json", audit)
    write_json(session_dir / "paper" / "paper_signal_order.json", execution_signal_report())
    write_json(session_dir / "fresh_data" / "freshness.json", {"allowed": ready, "reasons": []})
    return session_dir


def execution_signal_report() -> dict[str, object]:
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
            "notional": 1.0,
        },
        "order_result": {
            "accepted": True,
            "status": "dry_run_accepted",
            "reasons": [],
            "dry_run": True,
            "broker_response": None,
        },
    }


def paper_session_args(
    root: Path,
    *,
    source: Path,
    output_dir: Path,
    extra: list[str] | None = None,
) -> list[str]:
    args = [
        "paper-session",
        "--source-csv",
        str(source),
        "--from",
        "2026-03-01",
        "--to",
        "2026-06-16",
        "--config",
        str(write_universe(root / "universe.yml")),
        "--risk",
        str(write_risk(root / "risk.yml")),
        "--signal-model",
        str(write_buy_model(root / "model.json")),
        "--as-of-date",
        "2026-06-16",
        "--output-dir",
        str(output_dir),
    ]
    if extra:
        args.extend(extra)
    return args


def write_sample_source(path: Path) -> Path:
    write_records(generate_sample_ohlcv(symbols=("SPY",), start="2026-03-01", end="2026-06-16"), path)
    return path


def write_buy_model(path: Path) -> Path:
    save_model(
        LogisticBaselineModel(feature_names=("momentum_20",), intercept=1.0, coefficients=(5.0,)),
        str(path),
    )
    return path


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


def write_risk(path: Path) -> Path:
    path.write_text(
        textwrap.dedent(
            """
            risk_limits:
              max_daily_loss_pct: 0.02
              max_drawdown_pct: 0.10
              max_gross_exposure: 1.0
              max_single_position: 0.30
              live_trading_allowed: false
            """
        ),
        encoding="utf-8",
    )
    return path


def write_json(path: Path, payload: dict[str, object]) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def read_jsonl(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


if __name__ == "__main__":
    unittest.main()
