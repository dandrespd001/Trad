import json
import tempfile
import unittest
from pathlib import Path

from trading_ai.cli import build_parser
from trading_ai.execution.autonomy_level import certify_autonomy_promotion
from trading_ai.execution.paper_n0_certification import (
    DEFAULT_MAX_DRAWDOWN_PCT,
    DEFAULT_MIN_CLEAN_DAYS,
    compute_certification_hash,
    run_paper_n0_certification,
)


def _clean_record(as_of_date: str, session_id: str) -> dict:
    return {
        "record_type": "paper_auto_cycle_session",
        "session_id": session_id,
        "generated_at": f"{as_of_date}T00:05:00+00:00",
        "as_of_date": as_of_date,
        "state": "PAPER_CLOSED",
        "exit_code": 0,
        "confirm_paper_auto": True,
        "order_state": "paper_order_sent",
        "closeout_status": "CLOSED",
        "statement_status": "MATCHED",
        "unreconciled_fills": 0,
        "blockers": [],
    }


def _blocked_record(as_of_date: str, session_id: str) -> dict:
    return {
        "record_type": "paper_auto_cycle_session",
        "session_id": session_id,
        "generated_at": f"{as_of_date}T00:06:00+00:00",
        "as_of_date": as_of_date,
        "state": "BLOCKED",
        "exit_code": 1,
        "confirm_paper_auto": False,
        "order_state": "blocked",
        "closeout_status": "",
        "statement_status": "",
        "unreconciled_fills": 0,
        "blockers": ["risk_limit_breach"],
    }


def _write_ledger(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record))
            handle.write("\n")


def _dates(count: int, *, prefix: str = "2026-01") -> list[str]:
    return [f"{prefix}-{day:02d}" for day in range(1, count + 1)]


def _performance_payload(
    *,
    as_of_date: str,
    net_pnl: float | None = 150.0,
    drawdown_pct: float | None = 3.0,
    dangerous_flag: bool = False,
    include_pnl: bool = True,
    include_drawdown: bool = True,
) -> dict:
    safety = {
        "paper_only": True,
        "broker_client_built": False,
        "credentials_read": False,
        "live_trading_authorized": False,
        "live_trading_allowed": False,
    }
    if dangerous_flag:
        safety["broker_client_built"] = True
    paper_metrics: dict[str, object] = {
        "dates": {"start": as_of_date, "end": as_of_date},
    }
    if include_pnl:
        paper_metrics["pnl"] = {
            "source": "proxy",
            "proxy_unrealized_pnl": net_pnl,
            "broker_statement": False,
        }
    payload: dict[str, object] = {
        "schema_version": "1.0",
        "generated_at": f"{as_of_date}T00:10:00+00:00",
        "status": "OK",
        "paper_metrics": paper_metrics,
        "safety": safety,
    }
    if include_drawdown:
        payload["performance"] = {"max_drawdown_pct": drawdown_pct}
        payload["risk"] = {"current_drawdown_pct": drawdown_pct}
    return payload


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


class ComputeCertificationHashTests(unittest.TestCase):
    def test_hash_ignores_generated_at_but_reacts_to_clean_days(self) -> None:
        payload_a = {"schema_version": "1.0", "generated_at": "t1", "clean_days": 5, "status": "ACCUMULATING"}
        payload_b = {**payload_a, "generated_at": "t2"}
        self.assertEqual(compute_certification_hash(payload_a), compute_certification_hash(payload_b))

        payload_c = {**payload_a, "clean_days": 6}
        self.assertNotEqual(compute_certification_hash(payload_a), compute_certification_hash(payload_c))

    def test_hash_ignores_artifact_hash_field(self) -> None:
        payload_a = {"schema_version": "1.0", "clean_days": 5, "artifact_hash": "aaa"}
        payload_b = {**payload_a, "artifact_hash": "bbb"}
        self.assertEqual(compute_certification_hash(payload_a), compute_certification_hash(payload_b))

    def test_hash_ignores_suggested_certify_command_field(self) -> None:
        payload_a = {"schema_version": "1.0", "clean_days": 5, "suggested_certify_command": ""}
        payload_b = {
            **payload_a,
            "suggested_certify_command": "trading-ai autonomy-certify ... --artifact-hash abc",
        }
        self.assertEqual(compute_certification_hash(payload_a), compute_certification_hash(payload_b))

    def test_stored_hash_is_reverifiable_from_written_certified_ready_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            dates = _dates(DEFAULT_MIN_CLEAN_DAYS)
            ledger = root / "ledger.jsonl"
            _write_ledger(ledger, [_clean_record(day, f"session-{day}") for day in dates])
            as_of_date = dates[-1]
            performance_report = root / "performance.json"
            _write_json(performance_report, _performance_payload(as_of_date=as_of_date))

            result = run_paper_n0_certification(
                as_of_date=as_of_date,
                market="equities",
                session_ledgers=[ledger],
                performance_report=performance_report,
                output_dir=root / "out",
            )
            self.assertEqual(result.status, "CERTIFIED_READY")

            on_disk = json.loads(result.output_path.read_text(encoding="utf-8"))

        self.assertNotEqual(on_disk["suggested_certify_command"], "")
        self.assertEqual(compute_certification_hash(on_disk), on_disk["artifact_hash"])

    def test_stored_hash_is_reverifiable_from_written_accumulating_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            dates = _dates(3)
            ledger = root / "ledger.jsonl"
            _write_ledger(ledger, [_clean_record(day, f"session-{day}") for day in dates])
            as_of_date = dates[-1]
            performance_report = root / "performance.json"
            _write_json(performance_report, _performance_payload(as_of_date=as_of_date))

            result = run_paper_n0_certification(
                as_of_date=as_of_date,
                market="equities",
                session_ledgers=[ledger],
                performance_report=performance_report,
                output_dir=root / "out",
            )
            self.assertEqual(result.status, "ACCUMULATING")

            on_disk = json.loads(result.output_path.read_text(encoding="utf-8"))

        self.assertEqual(on_disk["suggested_certify_command"], "")
        self.assertEqual(compute_certification_hash(on_disk), on_disk["artifact_hash"])


class RunPaperN0CertificationTests(unittest.TestCase):
    def test_twenty_distinct_clean_days_with_healthy_performance_is_certified_ready(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            dates = _dates(DEFAULT_MIN_CLEAN_DAYS)
            records = [_clean_record(day, f"session-{day}") for day in dates]
            ledger = root / "ledger.jsonl"
            _write_ledger(ledger, records)

            as_of_date = dates[-1]
            performance_report = root / "performance.json"
            _write_json(performance_report, _performance_payload(as_of_date=as_of_date))

            result = run_paper_n0_certification(
                as_of_date=as_of_date,
                market="equities",
                session_ledgers=[ledger],
                performance_report=performance_report,
                output_dir=root / "out",
            )

            self.assertTrue(result.output_path.exists())
            self.assertTrue((result.output_path.parent / "certification.md").exists())

        self.assertEqual(result.status, "CERTIFIED_READY")
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.payload["clean_days"], DEFAULT_MIN_CLEAN_DAYS)
        self.assertEqual(result.payload["blockers"], [])
        command = result.payload["suggested_certify_command"]
        self.assertIn(result.payload["artifact_hash"], command)
        self.assertIn(f"--clean-days {DEFAULT_MIN_CLEAN_DAYS}", command)
        self.assertIn("--evidence-kind paper_certification", command)

    def test_sessions_vs_distinct_days_distinction_yields_accumulating(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            dates = _dates(18)
            records: list[dict] = []
            # First 7 days get two clean sessions each (14 sessions), remaining 11 get one each (11) = 25 total.
            for index, day in enumerate(dates):
                records.append(_clean_record(day, f"session-{day}-a"))
                if index < 7:
                    records.append(_clean_record(day, f"session-{day}-b"))
            self.assertEqual(len(records), 25)

            ledger = root / "ledger.jsonl"
            _write_ledger(ledger, records)

            as_of_date = dates[-1]
            performance_report = root / "performance.json"
            _write_json(performance_report, _performance_payload(as_of_date=as_of_date))

            result = run_paper_n0_certification(
                as_of_date=as_of_date,
                market="equities",
                session_ledgers=[ledger],
                performance_report=performance_report,
                output_dir=root / "out",
            )

        self.assertEqual(result.status, "ACCUMULATING")
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.payload["total_sessions"], 25)
        self.assertEqual(result.payload["clean_days"], 18)
        self.assertEqual(result.payload["remaining_clean_days"], 2)
        self.assertEqual(result.payload["suggested_certify_command"], "")

    def test_mixed_day_does_not_count_clean_and_blocks_on_session_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            dates = _dates(DEFAULT_MIN_CLEAN_DAYS)
            records = [_clean_record(day, f"session-{day}") for day in dates]
            mixed_day = "2026-02-01"
            records.append(_clean_record(mixed_day, "session-mixed-clean"))
            records.append(_blocked_record(mixed_day, "session-mixed-blocked"))

            ledger = root / "ledger.jsonl"
            _write_ledger(ledger, records)

            as_of_date = mixed_day
            performance_report = root / "performance.json"
            _write_json(performance_report, _performance_payload(as_of_date=as_of_date))

            result = run_paper_n0_certification(
                as_of_date=as_of_date,
                market="equities",
                session_ledgers=[ledger],
                performance_report=performance_report,
                output_dir=root / "out",
            )

        self.assertEqual(result.status, "BLOCKED")
        self.assertEqual(result.exit_code, 1)
        self.assertIn("session_evidence_blocked", result.payload["blockers"])
        self.assertNotIn(mixed_day, result.payload["clean_day_dates"])
        self.assertEqual(result.payload["clean_days"], DEFAULT_MIN_CLEAN_DAYS)

    def test_negative_pnl_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            as_of_date = "2026-03-01"
            ledger = root / "ledger.jsonl"
            _write_ledger(ledger, [_clean_record(as_of_date, "s1")])
            performance_report = root / "performance.json"
            _write_json(
                performance_report,
                _performance_payload(as_of_date=as_of_date, net_pnl=-1.0, drawdown_pct=2.0),
            )

            result = run_paper_n0_certification(
                as_of_date=as_of_date,
                market="equities",
                session_ledgers=[ledger],
                performance_report=performance_report,
                output_dir=root / "out",
            )

        self.assertEqual(result.status, "BLOCKED")
        self.assertIn("net_pnl_not_positive", result.payload["blockers"])

    def test_drawdown_above_limit_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            as_of_date = "2026-03-02"
            ledger = root / "ledger.jsonl"
            _write_ledger(ledger, [_clean_record(as_of_date, "s1")])
            performance_report = root / "performance.json"
            _write_json(
                performance_report,
                _performance_payload(
                    as_of_date=as_of_date, net_pnl=100.0, drawdown_pct=DEFAULT_MAX_DRAWDOWN_PCT + 5.0
                ),
            )

            result = run_paper_n0_certification(
                as_of_date=as_of_date,
                market="equities",
                session_ledgers=[ledger],
                performance_report=performance_report,
                output_dir=root / "out",
            )

        self.assertEqual(result.status, "BLOCKED")
        self.assertIn("drawdown_above_limit", result.payload["blockers"])

    def test_realized_pnl_ignored_unless_source_is_broker_statement(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            as_of_date = "2026-03-08"
            ledger = root / "ledger.jsonl"
            _write_ledger(ledger, [_clean_record(as_of_date, "s1")])
            payload = _performance_payload(as_of_date=as_of_date, net_pnl=25.0)
            payload["paper_metrics"]["pnl"] = {
                "source": "proxy",
                "realized_pnl": 999.0,
                "proxy_unrealized_pnl": 25.0,
                "broker_statement": False,
            }
            performance_report = root / "performance.json"
            _write_json(performance_report, payload)

            result = run_paper_n0_certification(
                as_of_date=as_of_date,
                market="equities",
                session_ledgers=[ledger],
                performance_report=performance_report,
                output_dir=root / "out",
            )

        self.assertEqual(result.payload["net_pnl_usd"], 25.0)

    def test_realized_pnl_used_when_source_is_broker_statement(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            as_of_date = "2026-03-09"
            ledger = root / "ledger.jsonl"
            _write_ledger(ledger, [_clean_record(as_of_date, "s1")])
            payload = _performance_payload(as_of_date=as_of_date)
            payload["paper_metrics"]["pnl"] = {
                "source": "broker_statement",
                "realized_pnl": 42.0,
                "proxy_unrealized_pnl": 1.0,
                "broker_statement": True,
            }
            performance_report = root / "performance.json"
            _write_json(performance_report, payload)

            result = run_paper_n0_certification(
                as_of_date=as_of_date,
                market="equities",
                session_ledgers=[ledger],
                performance_report=performance_report,
                output_dir=root / "out",
            )

        self.assertEqual(result.payload["net_pnl_usd"], 42.0)

    def test_missing_performance_fields_blocks_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            as_of_date = "2026-03-03"
            ledger = root / "ledger.jsonl"
            _write_ledger(ledger, [_clean_record(as_of_date, "s1")])
            performance_report = root / "performance.json"
            _write_json(
                performance_report,
                _performance_payload(as_of_date=as_of_date, include_pnl=False, include_drawdown=False),
            )

            result = run_paper_n0_certification(
                as_of_date=as_of_date,
                market="equities",
                session_ledgers=[ledger],
                performance_report=performance_report,
                output_dir=root / "out",
            )

        self.assertEqual(result.status, "BLOCKED")
        self.assertIn("performance_fields_missing", result.payload["blockers"])
        self.assertIsNone(result.payload["net_pnl_usd"])
        self.assertIsNone(result.payload["max_drawdown_pct_observed"])

    def test_unreadable_performance_report_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            as_of_date = "2026-03-04"
            ledger = root / "ledger.jsonl"
            _write_ledger(ledger, [_clean_record(as_of_date, "s1")])
            performance_report = root / "performance.json"
            performance_report.parent.mkdir(parents=True, exist_ok=True)
            performance_report.write_text("{not valid json", encoding="utf-8")

            result = run_paper_n0_certification(
                as_of_date=as_of_date,
                market="equities",
                session_ledgers=[ledger],
                performance_report=performance_report,
                output_dir=root / "out",
            )

        self.assertEqual(result.status, "BLOCKED")
        self.assertIn("performance_artifact_invalid", result.payload["blockers"])

    def test_as_of_date_mismatch_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            as_of_date = "2026-03-05"
            ledger = root / "ledger.jsonl"
            _write_ledger(ledger, [_clean_record(as_of_date, "s1")])
            performance_report = root / "performance.json"
            _write_json(performance_report, _performance_payload(as_of_date="2026-03-04"))

            result = run_paper_n0_certification(
                as_of_date=as_of_date,
                market="equities",
                session_ledgers=[ledger],
                performance_report=performance_report,
                output_dir=root / "out",
            )

        self.assertEqual(result.status, "BLOCKED")
        self.assertIn("performance_as_of_date_mismatch", result.payload["blockers"])

    def test_dangerous_safety_flag_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            as_of_date = "2026-03-06"
            ledger = root / "ledger.jsonl"
            _write_ledger(ledger, [_clean_record(as_of_date, "s1")])
            performance_report = root / "performance.json"
            _write_json(
                performance_report,
                _performance_payload(as_of_date=as_of_date, dangerous_flag=True),
            )

            result = run_paper_n0_certification(
                as_of_date=as_of_date,
                market="equities",
                session_ledgers=[ledger],
                performance_report=performance_report,
                output_dir=root / "out",
            )

        self.assertEqual(result.status, "BLOCKED")
        self.assertIn("performance_safety_flag", result.payload["blockers"])

    def test_unknown_market_raises_value_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            as_of_date = "2026-03-07"
            ledger = root / "ledger.jsonl"
            _write_ledger(ledger, [_clean_record(as_of_date, "s1")])
            performance_report = root / "performance.json"
            _write_json(performance_report, _performance_payload(as_of_date=as_of_date))

            with self.assertRaises(ValueError):
                run_paper_n0_certification(
                    as_of_date=as_of_date,
                    market="not_a_market",
                    session_ledgers=[ledger],
                    performance_report=performance_report,
                    output_dir=root / "out",
                )

    def test_certified_ready_evidence_feeds_autonomy_promotion_end_to_end(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            dates = _dates(DEFAULT_MIN_CLEAN_DAYS)
            records = [_clean_record(day, f"session-{day}") for day in dates]
            ledger = root / "ledger.jsonl"
            _write_ledger(ledger, records)

            as_of_date = dates[-1]
            performance_report = root / "performance.json"
            _write_json(performance_report, _performance_payload(as_of_date=as_of_date))

            result = run_paper_n0_certification(
                as_of_date=as_of_date,
                market="equities",
                session_ledgers=[ledger],
                performance_report=performance_report,
                output_dir=root / "out",
            )
            self.assertEqual(result.status, "CERTIFIED_READY")

            decision = certify_autonomy_promotion(
                market="equities",
                target_level="N1_REAL_CANARY",
                evidence={
                    "clean_days": result.payload["clean_days"],
                    "evidence_kind": result.payload["evidence_kind"],
                    "artifact_hash": result.payload["artifact_hash"],
                },
                reviewer="architect",
                reason="N0 evidence certified via paper_n0_certification",
                state_dir=root / "autonomy",
            )

        self.assertEqual(decision.status, "OK")
        self.assertEqual(decision.exit_code, 0)
        self.assertEqual(decision.payload["target_level"], "N1_REAL_CANARY")


class PaperN0CertificationCliTests(unittest.TestCase):
    def test_parser_registers_report_only_flags(self) -> None:
        parser = build_parser()
        args = parser.parse_args(
            [
                "paper-n0-certification",
                "--as-of-date",
                "2026-01-01",
                "--session-ledger",
                "ledger.jsonl",
                "--performance-report",
                "performance.json",
            ]
        )

        self.assertEqual(args.market, "equities")
        self.assertEqual(args.session_ledger, ["ledger.jsonl"])
        self.assertEqual(args.performance_report, "performance.json")
        self.assertEqual(args.min_clean_days, DEFAULT_MIN_CLEAN_DAYS)
        self.assertEqual(args.max_drawdown_pct, DEFAULT_MAX_DRAWDOWN_PCT)
        self.assertFalse(hasattr(args, "submit"))
        self.assertFalse(hasattr(args, "confirm"))
        self.assertFalse(hasattr(args, "confirm_paper_auto"))

    def test_parser_rejects_unknown_market(self) -> None:
        parser = build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(
                [
                    "paper-n0-certification",
                    "--as-of-date",
                    "2026-01-01",
                    "--market",
                    "crypto",
                    "--session-ledger",
                    "ledger.jsonl",
                    "--performance-report",
                    "performance.json",
                ]
            )


if __name__ == "__main__":
    unittest.main()
