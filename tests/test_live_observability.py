import json
import tempfile
import unittest
from pathlib import Path

import yaml

from trading_ai.execution.live_observability import (
    LiveJsonlEventWriter,
    build_live_observability_event,
)


class LiveObservabilityTests(unittest.TestCase):
    def test_jsonl_writer_redacts_and_records_required_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "live_events.jsonl"
            writer = LiveJsonlEventWriter(path)
            event = build_live_observability_event(
                event_type="live_execute_session",
                gate_status="BLOCKED",
                readiness_state="READY_FOR_LIVE_CANARY",
                breaker_state="TRIPPED",
                order_intent_hash="a" * 64,
                slippage_bps=1.25,
                latency_ms=42.5,
                message="token=SHOULD_NOT_LEAK api_key=KEY secret_key=SECRET",
            )

            writer.write(event)
            rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["event_type"], "live_execute_session")
        self.assertEqual(row["gate_status"], "BLOCKED")
        self.assertEqual(row["readiness_state"], "READY_FOR_LIVE_CANARY")
        self.assertEqual(row["breaker_state"], "TRIPPED")
        self.assertEqual(row["order_intent_hash"], "a" * 64)
        self.assertEqual(row["slippage_bps"], 1.25)
        self.assertEqual(row["latency_ms"], 42.5)
        self.assertEqual(row["alert_tier"], "CRITICAL")
        self.assertEqual(row["sink"], "local_jsonl")
        self.assertFalse(row["safety"]["orders_submitted"])
        self.assertNotIn("SHOULD_NOT_LEAK", json.dumps(row))
        self.assertNotIn("SECRET", json.dumps(row))

    def test_alert_tiers_are_deterministic(self) -> None:
        cases = [
            ("OK", "READY_FOR_LIVE_CANARY", "CLEAN", "INFO"),
            ("WARN", "READY_FOR_LIVE_CANARY", "CLEAN", "WARN"),
            ("BLOCKED", "BLOCKED", "CLEAN", "CRITICAL"),
            ("OK", "READY_FOR_LIVE_CANARY", "TRIPPED", "CRITICAL"),
        ]
        for gate_status, readiness_state, breaker_state, expected in cases:
            with self.subTest(gate_status=gate_status, readiness_state=readiness_state, breaker_state=breaker_state):
                event = build_live_observability_event(
                    event_type="live_gate",
                    gate_status=gate_status,
                    readiness_state=readiness_state,
                    breaker_state=breaker_state,
                    order_intent_hash="b" * 64,
                    slippage_bps=0.0,
                    latency_ms=1.0,
                )
                self.assertEqual(event["alert_tier"], expected)

    def test_docker_and_compose_define_non_root_runtime_and_persistent_evidence_volume(self) -> None:
        dockerfile = Path("Dockerfile").read_text(encoding="utf-8")
        compose = yaml.safe_load(Path("compose.yml").read_text(encoding="utf-8"))
        service = compose["services"]["trading-ai-live"]

        self.assertIn("USER 10001", dockerfile)
        self.assertIn("HEALTHCHECK", dockerfile)
        self.assertEqual(service["user"], "10001:10001")
        self.assertIn("./reports/tmp:/app/reports/tmp", service["volumes"])
        self.assertIn("ALPACA_LIVE_API_KEY", service["environment"])
        self.assertIn("ALPACA_LIVE_SECRET_KEY", service["environment"])
        serialized = json.dumps(compose, sort_keys=True)
        self.assertNotIn("sk-", serialized)
        self.assertNotIn("live-secret", serialized)

    def test_runbook_documents_alert_tiers_and_live_incidents(self) -> None:
        runbook = Path("docs/paper-real-runbook.md").read_text(encoding="utf-8")

        for expected in (
            "Live observability",
            "INFO",
            "WARN",
            "CRITICAL",
            "readiness blocked",
            "breaker tripped",
            "fill timeout",
            "rollback",
        ):
            self.assertIn(expected, runbook)


if __name__ == "__main__":
    unittest.main()
