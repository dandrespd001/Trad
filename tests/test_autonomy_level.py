import json
import tempfile
import unittest
from pathlib import Path

from trading_ai.cli import build_parser
from trading_ai.execution.autonomy_level import (
    AUTONOMY_LEVELS,
    AUTONOMY_MARKETS,
    AutonomyState,
    certify_autonomy_promotion,
    evaluate_autonomy_gate,
    load_autonomy_state,
    record_autonomy_incident,
    resolve_autonomy_incident,
    save_autonomy_state,
)
from trading_ai.execution.paper_n0_certification import run_paper_n0_certification

UNVERIFIED_SCALAR_EVIDENCE = {
    "clean_days": 20,
    "evidence_kind": "paper_certification",
    "artifact_hash": "a" * 64,
}


def _read_ledger(state_dir: Path, market: str) -> list[dict[str, object]]:
    ledger_path = state_dir / market / "ledger.jsonl"
    if not ledger_path.exists():
        return []
    return [json.loads(line) for line in ledger_path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _clean_record(as_of_date: str, session_id: str) -> dict[str, object]:
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
        "safety": {"paper_only": True, "live_trading_authorized": False},
    }


def _write_n1_artifact(root: Path, *, market: str = "equities", clean_days: int = 20) -> Path:
    dates = [f"2026-01-{day:02d}" for day in range(1, clean_days + 1)]
    ledger = root / f"{market}-ledger.jsonl"
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text(
        "".join(json.dumps(_clean_record(day, f"{market}-{day}")) + "\n" for day in dates),
        encoding="utf-8",
    )
    as_of_date = dates[-1]
    performance = root / f"{market}-performance.json"
    performance.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "generated_at": f"{as_of_date}T01:00:00+00:00",
                "status": "OK",
                "paper_metrics": {
                    "dates": {"start": dates[0], "end": as_of_date},
                    "pnl": {
                        "source": "broker_statement",
                        "broker_statement": True,
                        "certification_eligible": True,
                        "realized_pnl": 100.0,
                    },
                },
                "performance": {"max_drawdown_pct": 2.0},
                "risk": {"current_drawdown_pct": 2.0},
                "statement_reconciliation": {
                    "status": "MATCHED",
                    "missing_fills": 0,
                    "extra_fills": 0,
                },
                "statement_status": {"status": "MATCHED", "unreconciled_fills": 0},
                "warnings": [],
                "blockers": [],
                "diagnostics": [],
                "safety": {
                    "paper_only": True,
                    "broker_client_built": False,
                    "credentials_read": False,
                    "live_trading_authorized": False,
                    "live_trading_allowed": False,
                },
            }
        ),
        encoding="utf-8",
    )
    result = run_paper_n0_certification(
        as_of_date=as_of_date,
        market=market,
        session_ledgers=[ledger],
        performance_report=performance,
        output_dir=root / "certificates",
    )
    return result.output_path


def _set_level(state_dir: Path, level: str, *, market: str = "equities", incident: bool = False) -> None:
    save_autonomy_state(
        AutonomyState(
            market=market,
            level=level,
            open_incident=incident,
            fail_closed=False,
        ),
        state_dir=state_dir,
    )


class AutonomyFailClosedTests(unittest.TestCase):
    def test_missing_state_file_is_n0_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state = load_autonomy_state("equities", state_dir=Path(temp_dir))
            self.assertEqual(state.level, "N0_PAPER_AUTO")
            self.assertTrue(state.fail_closed)
            self.assertFalse((Path(temp_dir) / "equities" / "ledger.jsonl").exists())

    def test_corrupt_json_is_fail_closed_and_logs_ledger_event(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_dir = Path(temp_dir)
            state_path = state_dir / "equities" / "state.json"
            state_path.parent.mkdir(parents=True)
            state_path.write_text("{not-json", encoding="utf-8")

            state = load_autonomy_state("equities", state_dir=state_dir)

            self.assertEqual(state.level, "N0_PAPER_AUTO")
            self.assertTrue(state.fail_closed)
            events = _read_ledger(state_dir, "equities")
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["event"], "fail_closed_read")

    def test_tampered_checksum_is_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_dir = Path(temp_dir)
            save_autonomy_state(
                AutonomyState(market="equities", level="N2_REAL_SEMI_AUTO"), state_dir=state_dir
            )
            state_path = state_dir / "equities" / "state.json"
            payload = json.loads(state_path.read_text(encoding="utf-8"))
            payload["integrity_sha256"] = "0" * 64
            state_path.write_text(json.dumps(payload), encoding="utf-8")

            state = load_autonomy_state("equities", state_dir=state_dir)

            self.assertEqual(state.level, "N0_PAPER_AUTO")
            self.assertTrue(state.fail_closed)
            events = _read_ledger(state_dir, "equities")
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["event"], "fail_closed_read")

    def test_unknown_level_on_disk_is_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_dir = Path(temp_dir)
            state_path = state_dir / "equities" / "state.json"
            state_path.parent.mkdir(parents=True)
            body = {
                "schema_version": "1.0",
                "market": "equities",
                "level": "N9_NOT_A_LEVEL",
                "certified_at": None,
                "certified_by": None,
                "evidence_hash": None,
                "open_incident": False,
                "fail_closed": False,
                "updated_at": None,
            }
            import hashlib

            checksum = hashlib.sha256(json.dumps(body, sort_keys=True).encode("utf-8")).hexdigest()
            payload = {**body, "integrity_sha256": checksum}
            state_path.write_text(json.dumps(payload), encoding="utf-8")

            state = load_autonomy_state("equities", state_dir=state_dir)

            self.assertEqual(state.level, "N0_PAPER_AUTO")
            self.assertTrue(state.fail_closed)

    def test_invalid_market_raises_value_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, self.assertRaises(ValueError):
            load_autonomy_state("crypto", state_dir=Path(temp_dir))


class AutonomyPromotionHappyPathTests(unittest.TestCase):
    def test_n0_to_n1_promotion_succeeds_and_writes_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            state_dir = root / "state"
            artifact = _write_n1_artifact(root)
            artifact_payload = json.loads(artifact.read_text(encoding="utf-8"))
            decision = certify_autonomy_promotion(
                market="equities",
                target_level="N1_REAL_CANARY",
                evidence_artifact=artifact,
                reviewer="ops-lead",
                reason="20 clean paper sessions, positive pnl",
                state_dir=state_dir,
            )

            self.assertEqual(decision.status, "OK")
            self.assertEqual(decision.exit_code, 0)
            self.assertEqual(decision.payload["blockers"], [])
            self.assertEqual(decision.payload["safety"]["paper_only"], True)
            self.assertEqual(decision.payload["safety"]["broker_client_built"], False)
            self.assertEqual(decision.payload["safety"]["credentials_read"], False)
            self.assertEqual(decision.payload["safety"]["orders_submitted"], False)
            self.assertEqual(decision.payload["safety"]["live_trading_allowed"], False)
            self.assertEqual(decision.payload["safety"]["live_trading_authorized"], False)
            self.assertTrue(decision.output_path.exists())

            state = load_autonomy_state("equities", state_dir=state_dir)
            self.assertEqual(state.level, "N1_REAL_CANARY")
            self.assertFalse(state.fail_closed)
            self.assertFalse(state.open_incident)
            self.assertEqual(state.certified_by, "ops-lead")
            self.assertEqual(state.evidence_hash, artifact_payload["artifact_hash"])
            self.assertTrue(decision.payload["evidence"]["source_chain_verified"])

            # State file has a verifiable checksum (round trips through load).
            state_path = state_dir / "equities" / "state.json"
            self.assertTrue(state_path.exists())
            reloaded = load_autonomy_state("equities", state_dir=state_dir)
            self.assertFalse(reloaded.fail_closed)

            events = _read_ledger(state_dir, "equities")
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["event"], "promotion")
            self.assertEqual(events[0]["from_level"], "N0_PAPER_AUTO")
            self.assertEqual(events[0]["to_level"], "N1_REAL_CANARY")


class AutonomyPromotionBlockersTests(unittest.TestCase):
    def test_unverified_scalar_claims_cannot_promote(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_dir = Path(temp_dir)
            decision = certify_autonomy_promotion(
                market="equities",
                target_level="N1_REAL_CANARY",
                evidence=UNVERIFIED_SCALAR_EVIDENCE,
                reviewer="ops",
                reason="caller supplied claims",
                state_dir=state_dir,
            )

            self.assertEqual(decision.status, "BLOCKED")
            self.assertIn("unverified_scalar_evidence_forbidden", decision.payload["blockers"])
            self.assertIn("evidence_artifact_required", decision.payload["blockers"])
            self.assertFalse((state_dir / "equities" / "state.json").exists())

    def test_tampered_certificate_summary_cannot_promote(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            artifact = _write_n1_artifact(root)
            payload = json.loads(artifact.read_text(encoding="utf-8"))
            payload["clean_days"] = 999
            artifact.write_text(json.dumps(payload), encoding="utf-8")

            decision = certify_autonomy_promotion(
                market="equities",
                target_level="N1_REAL_CANARY",
                evidence_artifact=artifact,
                reviewer="ops",
                reason="tampered certificate",
                state_dir=root / "state",
            )

            self.assertEqual(decision.status, "BLOCKED")
            self.assertIn("evidence_artifact_hash_mismatch", decision.payload["blockers"])
            self.assertIn("evidence_session_summary_mismatch", decision.payload["blockers"])

    def test_source_changed_after_certification_cannot_promote(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            artifact = _write_n1_artifact(root)
            payload = json.loads(artifact.read_text(encoding="utf-8"))
            ledger_path = Path(payload["source_manifest"]["session_ledgers"][0]["path"])
            ledger_path.write_text(
                ledger_path.read_text(encoding="utf-8")
                + json.dumps(_clean_record("2026-01-20", "late-replay"))
                + "\n",
                encoding="utf-8",
            )

            decision = certify_autonomy_promotion(
                market="equities",
                target_level="N1_REAL_CANARY",
                evidence_artifact=artifact,
                reviewer="ops",
                reason="mutated source",
                state_dir=root / "state",
            )

            self.assertEqual(decision.status, "BLOCKED")
            self.assertIn("evidence_source_integrity_mismatch", decision.payload["blockers"])

    def test_n2_is_blocked_until_a_deterministic_validator_exists(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_dir = Path(temp_dir)
            _set_level(state_dir, "N1_REAL_CANARY")
            decision = certify_autonomy_promotion(
                market="equities",
                target_level="N2_REAL_SEMI_AUTO",
                evidence_artifact=state_dir / "claimed-canary-certificate.json",
                reviewer="ops",
                reason="no N2 producer exists",
                state_dir=state_dir,
            )

            self.assertEqual(decision.status, "BLOCKED")
            self.assertIn("evidence_validator_unavailable", decision.payload["blockers"])
            self.assertEqual(load_autonomy_state("equities", state_dir=state_dir).level, "N1_REAL_CANARY")

    def test_level_skip_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_dir = Path(temp_dir)
            decision = certify_autonomy_promotion(
                market="equities",
                target_level="N2_REAL_SEMI_AUTO",
                evidence=UNVERIFIED_SCALAR_EVIDENCE,
                reviewer="ops",
                reason="skip ahead",
                state_dir=state_dir,
            )
            self.assertEqual(decision.status, "BLOCKED")
            self.assertIn("invalid_level_jump", decision.payload["blockers"])
            state = load_autonomy_state("equities", state_dir=state_dir)
            self.assertEqual(state.level, "N0_PAPER_AUTO")

    def test_insufficient_clean_days_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            state_dir = root / "state"
            artifact = _write_n1_artifact(root, clean_days=5)
            decision = certify_autonomy_promotion(
                market="equities",
                target_level="N1_REAL_CANARY",
                evidence_artifact=artifact,
                reviewer="ops",
                reason="not enough days",
                state_dir=state_dir,
            )
            self.assertEqual(decision.status, "BLOCKED")
            self.assertIn("insufficient_clean_days", decision.payload["blockers"])

    def test_wrong_evidence_kind_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            state_dir = root / "state"
            artifact = _write_n1_artifact(root)
            payload = json.loads(artifact.read_text(encoding="utf-8"))
            payload["evidence_kind"] = "real_canary_certification"
            artifact.write_text(json.dumps(payload), encoding="utf-8")
            decision = certify_autonomy_promotion(
                market="equities",
                target_level="N1_REAL_CANARY",
                evidence_artifact=artifact,
                reviewer="ops",
                reason="wrong evidence kind",
                state_dir=state_dir,
            )
            self.assertEqual(decision.status, "BLOCKED")
            self.assertIn("evidence_kind_mismatch", decision.payload["blockers"])

    def test_empty_reviewer_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            state_dir = root / "state"
            decision = certify_autonomy_promotion(
                market="equities",
                target_level="N1_REAL_CANARY",
                evidence_artifact=_write_n1_artifact(root),
                reviewer="   ",
                reason="no reviewer",
                state_dir=state_dir,
            )
            self.assertEqual(decision.status, "BLOCKED")
            self.assertIn("reviewer_required", decision.payload["blockers"])

    def test_open_incident_blocks_promotion(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_dir = Path(temp_dir)
            _set_level(state_dir, "N1_REAL_CANARY", incident=True)

            decision = certify_autonomy_promotion(
                market="equities",
                target_level="N2_REAL_SEMI_AUTO",
                evidence_artifact=state_dir / "not-a-producer.json",
                reviewer="ops",
                reason="recert too soon",
                state_dir=state_dir,
            )
            self.assertEqual(decision.status, "BLOCKED")
            self.assertIn("open_incident_blocks_promotion", decision.payload["blockers"])

    def test_promotion_blocked_when_state_is_fail_closed_from_corruption(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            state_dir = root / "state"
            _set_level(state_dir, "N1_REAL_CANARY")
            state_path = state_dir / "equities" / "state.json"
            payload = json.loads(state_path.read_text(encoding="utf-8"))
            payload["integrity_sha256"] = "deadbeef"
            state_path.write_text(json.dumps(payload), encoding="utf-8")

            decision = certify_autonomy_promotion(
                market="equities",
                target_level="N1_REAL_CANARY",
                evidence_artifact=_write_n1_artifact(root),
                reviewer="ops",
                reason="recert after corruption",
                state_dir=state_dir,
            )
            self.assertEqual(decision.status, "BLOCKED")
            self.assertIn(
                "fail_closed_state_requires_recertification_from_n0", decision.payload["blockers"]
            )


class AutonomyMarketSequenceTests(unittest.TestCase):
    def test_futures_n1_blocked_while_equities_is_n0(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            state_dir = root / "state"
            decision = certify_autonomy_promotion(
                market="futures",
                target_level="N1_REAL_CANARY",
                evidence_artifact=_write_n1_artifact(root, market="futures"),
                reviewer="ops",
                reason="start futures too early",
                state_dir=state_dir,
            )
            self.assertEqual(decision.status, "BLOCKED")
            self.assertIn("market_precondition_not_met:equities", decision.payload["blockers"])

    def test_futures_n1_allowed_when_equities_at_n2_without_incident(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            state_dir = root / "state"
            _set_level(state_dir, "N2_REAL_SEMI_AUTO", market="equities")

            decision = certify_autonomy_promotion(
                market="futures",
                target_level="N1_REAL_CANARY",
                evidence_artifact=_write_n1_artifact(root, market="futures"),
                reviewer="ops",
                reason="start futures",
                state_dir=state_dir,
            )
            self.assertEqual(decision.status, "OK")

    def test_forex_n1_blocked_while_futures_is_n1(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            state_dir = root / "state"
            _set_level(state_dir, "N2_REAL_SEMI_AUTO", market="equities")
            _set_level(state_dir, "N1_REAL_CANARY", market="futures")

            decision = certify_autonomy_promotion(
                market="forex",
                target_level="N1_REAL_CANARY",
                evidence_artifact=_write_n1_artifact(root, market="forex"),
                reviewer="ops",
                reason="start forex too early",
                state_dir=state_dir,
            )
            self.assertEqual(decision.status, "BLOCKED")
            self.assertIn("market_precondition_not_met:futures", decision.payload["blockers"])


class AutonomyIncidentTests(unittest.TestCase):
    def test_grave_incident_at_n2_degrades_to_n1_and_opens_incident(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_dir = Path(temp_dir)
            _set_level(state_dir, "N2_REAL_SEMI_AUTO")

            decision = record_autonomy_incident(
                market="equities",
                severity="grave",
                source="live_circuit_breaker",
                reason="breaker tripped",
                state_dir=state_dir,
            )
            self.assertEqual(decision.status, "CRITICAL")

            state = load_autonomy_state("equities", state_dir=state_dir)
            self.assertEqual(state.level, "N1_REAL_CANARY")
            self.assertTrue(state.open_incident)

            events = _read_ledger(state_dir, "equities")
            self.assertEqual([event["event"] for event in events[-2:]], ["demotion", "incident"])

    def test_recertification_blocked_until_incident_resolved(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_dir = Path(temp_dir)
            _set_level(state_dir, "N2_REAL_SEMI_AUTO")
            record_autonomy_incident(
                market="equities",
                severity="grave",
                source="live_circuit_breaker",
                reason="breaker tripped",
                state_dir=state_dir,
            )

            blocked = certify_autonomy_promotion(
                market="equities",
                target_level="N2_REAL_SEMI_AUTO",
                evidence_artifact=state_dir / "not-a-producer.json",
                reviewer="ops",
                reason="recert without resolving incident",
                state_dir=state_dir,
            )
            self.assertEqual(blocked.status, "BLOCKED")
            self.assertIn("open_incident_blocks_promotion", blocked.payload["blockers"])

            resolved = resolve_autonomy_incident(
                market="equities", reviewer="ops-lead", reason="root cause fixed", state_dir=state_dir
            )
            self.assertEqual(resolved.status, "OK")
            state = load_autonomy_state("equities", state_dir=state_dir)
            self.assertFalse(state.open_incident)

            still_blocked = certify_autonomy_promotion(
                market="equities",
                target_level="N2_REAL_SEMI_AUTO",
                evidence_artifact=state_dir / "not-a-producer.json",
                reviewer="ops",
                reason="recert after resolving incident",
                state_dir=state_dir,
            )
            self.assertEqual(still_blocked.status, "BLOCKED")
            self.assertNotIn("open_incident_blocks_promotion", still_blocked.payload["blockers"])
            self.assertIn("evidence_validator_unavailable", still_blocked.payload["blockers"])

    def test_warning_incident_does_not_degrade(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_dir = Path(temp_dir)
            _set_level(state_dir, "N2_REAL_SEMI_AUTO")

            decision = record_autonomy_incident(
                market="equities",
                severity="warning",
                source="reconciliation",
                reason="minor drift",
                state_dir=state_dir,
            )
            self.assertEqual(decision.status, "WARN")
            state = load_autonomy_state("equities", state_dir=state_dir)
            self.assertEqual(state.level, "N2_REAL_SEMI_AUTO")
            self.assertFalse(state.open_incident)

    def test_resolve_incident_requires_reviewer_and_reason(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_dir = Path(temp_dir)
            decision = resolve_autonomy_incident(market="equities", reviewer="", reason="", state_dir=state_dir)
            self.assertEqual(decision.status, "BLOCKED")
            self.assertIn("reviewer_required", decision.payload["blockers"])
            self.assertIn("reason_required", decision.payload["blockers"])


class AutonomyGateTests(unittest.TestCase):
    def test_paper_auto_allowed_from_n0(self) -> None:
        state = AutonomyState(market="equities", level="N0_PAPER_AUTO")
        self.assertEqual(evaluate_autonomy_gate(market="equities", requested_action="paper_auto", state=state), [])

    def test_real_submit_approved_requires_n1(self) -> None:
        state = AutonomyState(market="equities", level="N0_PAPER_AUTO")
        blockers = evaluate_autonomy_gate(market="equities", requested_action="real_submit_approved", state=state)
        self.assertIn("autonomy_level_insufficient", blockers)

        state_n1 = AutonomyState(market="equities", level="N1_REAL_CANARY")
        self.assertEqual(
            evaluate_autonomy_gate(market="equities", requested_action="real_submit_approved", state=state_n1), []
        )

    def test_real_submit_veto_window_requires_n2(self) -> None:
        state_n1 = AutonomyState(market="equities", level="N1_REAL_CANARY")
        blockers = evaluate_autonomy_gate(
            market="equities", requested_action="real_submit_veto_window", state=state_n1
        )
        self.assertIn("autonomy_level_insufficient", blockers)

        state_n2 = AutonomyState(market="equities", level="N2_REAL_SEMI_AUTO")
        self.assertEqual(
            evaluate_autonomy_gate(market="equities", requested_action="real_submit_veto_window", state=state_n2),
            [],
        )

    def test_real_submit_auto_requires_n3(self) -> None:
        state_n2 = AutonomyState(market="equities", level="N2_REAL_SEMI_AUTO")
        blockers = evaluate_autonomy_gate(market="equities", requested_action="real_submit_auto", state=state_n2)
        self.assertIn("autonomy_level_insufficient", blockers)

        state_n3 = AutonomyState(market="equities", level="N3_REAL_AUTO")
        self.assertEqual(
            evaluate_autonomy_gate(market="equities", requested_action="real_submit_auto", state=state_n3), []
        )

    def test_open_incident_blocks_only_real_actions(self) -> None:
        state = AutonomyState(market="equities", level="N3_REAL_AUTO", open_incident=True)
        self.assertEqual(evaluate_autonomy_gate(market="equities", requested_action="paper_auto", state=state), [])
        blockers = evaluate_autonomy_gate(market="equities", requested_action="real_submit_auto", state=state)
        self.assertIn("autonomy_open_incident", blockers)

    def test_fail_closed_blocks_only_real_actions(self) -> None:
        state = AutonomyState(market="equities", level="N3_REAL_AUTO", fail_closed=True)
        self.assertEqual(evaluate_autonomy_gate(market="equities", requested_action="paper_auto", state=state), [])
        blockers = evaluate_autonomy_gate(market="equities", requested_action="real_submit_auto", state=state)
        self.assertIn("autonomy_state_fail_closed", blockers)

    def test_unknown_action_returns_single_blocker(self) -> None:
        state = AutonomyState(market="equities", level="N3_REAL_AUTO")
        blockers = evaluate_autonomy_gate(market="equities", requested_action="do_something_else", state=state)
        self.assertEqual(blockers, ["unknown_requested_action"])


class AutonomyLedgerAppendOnlyTests(unittest.TestCase):
    def test_ledger_accumulates_lines_across_operations(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            state_dir = root / "state"
            certify_autonomy_promotion(
                market="equities",
                target_level="N1_REAL_CANARY",
                evidence_artifact=_write_n1_artifact(root),
                reviewer="ops",
                reason="first cert",
                state_dir=state_dir,
            )
            record_autonomy_incident(
                market="equities", severity="info", source="ops", reason="fyi", state_dir=state_dir
            )

            events = _read_ledger(state_dir, "equities")
            self.assertGreaterEqual(len(events), 2)
            for event in events:
                self.assertIn("schema_version", event)
                self.assertIn("timestamp", event)
                self.assertIn("event", event)


class AutonomyCliParserTests(unittest.TestCase):
    def test_parser_registers_autonomy_subcommands(self) -> None:
        parser = build_parser()

        status_args = parser.parse_args(["autonomy-status", "--market", "equities"])
        self.assertEqual(status_args.market, "equities")

        certify_args = parser.parse_args(
            [
                "autonomy-certify",
                "--market",
                "equities",
                "--target-level",
                "N1_REAL_CANARY",
                "--reviewer",
                "ops",
                "--reason",
                "clean cycle",
                "--evidence-artifact",
                "certification.json",
            ]
        )
        self.assertEqual(certify_args.target_level, "N1_REAL_CANARY")
        self.assertEqual(certify_args.evidence_artifact, "certification.json")

        incident_args = parser.parse_args(
            [
                "autonomy-incident",
                "--market",
                "equities",
                "--severity",
                "grave",
                "--source",
                "breaker",
                "--reason",
                "tripped",
            ]
        )
        self.assertEqual(incident_args.severity, "grave")

        resolve_args = parser.parse_args(
            [
                "autonomy-resolve-incident",
                "--market",
                "equities",
                "--reviewer",
                "ops",
                "--reason",
                "fixed",
            ]
        )
        self.assertEqual(resolve_args.reviewer, "ops")

    def test_parser_has_no_real_submit_confirmation_flags(self) -> None:
        parser = build_parser()
        for command in ("autonomy-status", "autonomy-certify", "autonomy-incident", "autonomy-resolve-incident"):
            subparser = next(
                action.choices[command]
                for action in parser._subparsers._group_actions  # noqa: SLF001
                if hasattr(action, "choices") and command in action.choices
            )
            option_strings = {
                option for action in subparser._actions for option in action.option_strings  # noqa: SLF001
            }
            for forbidden in ("--confirm-real", "--confirm-live", "--submit", "--execute-live"):
                self.assertNotIn(forbidden, option_strings)

    def test_autonomy_markets_and_levels_are_stable(self) -> None:
        self.assertEqual(AUTONOMY_MARKETS, ("equities", "futures", "forex"))
        self.assertEqual(
            AUTONOMY_LEVELS,
            ("N0_PAPER_AUTO", "N1_REAL_CANARY", "N2_REAL_SEMI_AUTO", "N3_REAL_AUTO"),
        )


if __name__ == "__main__":
    unittest.main()
