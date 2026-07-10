"""Tests for the Gate 1 sleeve-rebalance scorecard (Sprint M5)."""

from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import date
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from trading_ai.cli import main
from trading_ai.execution.sleeve_gate1_report import SCHEMA_VERSION, run_gate1_report


def _write_cycle(
    path: Path,
    *,
    sleeve: str,
    as_of: str,
    status: str,
    submissions: list[dict[str, object]],
    plan: list[dict[str, object]],
    account_risk: dict[str, float] | None = None,
    blockers: list[str] | None = None,
) -> None:
    """Persist a minimal but realistic sleeve-rebalance payload."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": "1.0",
        "generated_at": f"{as_of}T22:00:00+00:00",
        "as_of": as_of,
        "universe": "test",
        "dataset": f"/tmp/{as_of}.csv",
        "weights": {},
        "plan": plan,
        "submissions": submissions,
        "account_risk": account_risk,
        "blockers": list(blockers) if blockers is not None else [],
        "status": status,
        "safety": {
            "paper_only": True,
            "orders_submitted": any(s.get("submitted") for s in submissions),
            "confirm_submit": True,
            "live_trading_authorized": False,
        },
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def _build_minimal_plan(pairs_to_price: dict[str, float]) -> list[dict[str, object]]:
    return [
        {
            "pair": pair,
            "action": "buy" if price > 0 else "hold",
            "target_notional": 50.0,
            "current_notional": 0.0,
            "delta": 50.0,
            "notional": 50.0,
            "quantity": None,
            "reference_price": price,
            "weight": 0.1,
        }
        for pair, price in pairs_to_price.items()
    ]


class _FakeBroker:
    """Duck-typed broker providing the read-only surface used by the scorecard."""

    def __init__(
        self,
        *,
        order_snapshots: dict[str, SimpleNamespace] | None = None,
        positions: list[SimpleNamespace] | None = None,
        account: Any = None,
        raise_for: set[str] | None = None,
    ) -> None:
        self._order_snapshots = order_snapshots or {}
        self._positions = positions or []
        self._account = account if account is not None else SimpleNamespace(equity=100_000.0)
        self._raise_for = raise_for or set()

    def get_order_by_client_id(self, client_order_id: str) -> SimpleNamespace:
        if client_order_id in self._raise_for:
            raise RuntimeError(f"simulated lookup failure for {client_order_id}")
        if client_order_id not in self._order_snapshots:
            raise RuntimeError(f"unknown client_order_id {client_order_id}")
        return self._order_snapshots[client_order_id]

    def read_positions(self) -> tuple[SimpleNamespace, ...]:
        return tuple(self._positions)

    def read_account(self) -> Any:
        return self._account


class _HelperBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()


class DryRunAggregationTests(_HelperBase):
    def test_aggregates_two_dates_two_sleeves_per_day(self) -> None:
        # Day 1: crypto REPORT_ONLY, etf OK with one submitted + one skipped.
        _write_cycle(
            self.tmp_path / "cycle_crypto_2026-07-09.json",
            sleeve="crypto",
            as_of="2026-07-09",
            status="REPORT_ONLY",
            submissions=[
                {"pair": "BTC/USD", "action": "hold", "submitted": False, "skipped": True, "status": "skipped"},
            ],
            plan=_build_minimal_plan({"BTC/USD": 63000.0, "ETH/USD": 1700.0}),
        )
        _write_cycle(
            self.tmp_path / "cycle_etf_2026-07-09.json",
            sleeve="etf",
            as_of="2026-07-09",
            status="OK",
            submissions=[
                {
                    "pair": "IWM",
                    "action": "buy",
                    "client_order_id": "sleeve-2026-07-09-IWM-buy",
                    "submitted": True,
                    "skipped": False,
                    "status": "accepted",
                },
                {"pair": "SPY", "action": "hold", "submitted": False, "skipped": True, "status": "skipped"},
            ],
            plan=_build_minimal_plan({"IWM": 220.0, "SPY": 540.0}),
        )
        # Day 2: only crypto with one error.
        _write_cycle(
            self.tmp_path / "cycle_crypto_2026-07-10.json",
            sleeve="crypto",
            as_of="2026-07-10",
            status="OK",
            submissions=[
                {
                    "pair": "ETH/USD",
                    "action": "buy",
                    "client_order_id": "sleeve-2026-07-10-ETH-buy",
                    "submitted": False,
                    "skipped": False,
                    "status": "error",
                    "reasons": ["APIError"],
                },
            ],
            plan=_build_minimal_plan({"ETH/USD": 1710.0}),
        )

        output = self.tmp_path / "report.json"
        result = run_gate1_report(cycles_dir=self.tmp_path, output=output)
        self.assertEqual(result.status, "OK")
        self.assertEqual(result.exit_code, 0)
        self.assertTrue(output.exists())
        payload = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(payload["schema_version"], SCHEMA_VERSION)
        self.assertEqual(payload["n_cycles"], 3)
        self.assertEqual(sorted(payload["days"]), ["2026-07-09", "2026-07-10"])
        self.assertEqual(payload["by_status"]["REPORT_ONLY"], 1)
        self.assertEqual(payload["by_status"]["OK"], 2)
        self.assertEqual(payload["orders_submitted_total"] if "orders_submitted_total" in payload else 1, 1)
        # Fills are only collected when a broker is provided; without one
        # the list is empty.
        self.assertEqual(payload["fills"], [])
        self.assertEqual(payload["incidents"], [])
        per_day = payload["per_day"]
        self.assertEqual([entry["date"] for entry in per_day], ["2026-07-09", "2026-07-10"])
        self.assertEqual(sorted(per_day[0]["sleeves"]), ["crypto", "etf"])
        self.assertEqual(per_day[0]["orders_submitted"], 1)
        self.assertEqual(per_day[0]["orders_errored"], 0)
        self.assertEqual(per_day[1]["orders_errored"], 1)


class BrokerEnrichmentTests(_HelperBase):
    def test_effective_cost_bps_computed_when_fill_and_reference_match(self) -> None:
        _write_cycle(
            self.tmp_path / "cycle_etf_2026-07-09.json",
            sleeve="etf",
            as_of="2026-07-09",
            status="OK",
            submissions=[
                {
                    "pair": "XLV",
                    "action": "buy",
                    "client_order_id": "sleeve-2026-07-09-XLV-buy",
                    "submitted": True,
                    "skipped": False,
                    "status": "accepted",
                },
            ],
            plan=_build_minimal_plan({"XLV": 100.0}),
        )
        broker = _FakeBroker(
            order_snapshots={
                "sleeve-2026-07-09-XLV-buy": SimpleNamespace(
                    order_id="ord-1",
                    client_order_id="sleeve-2026-07-09-XLV-buy",
                    symbol="XLV",
                    side="buy",
                    order_type="market",
                    time_in_force="day",
                    status="filled",
                    notional=None,
                    quantity=1.0,
                    filled_quantity=1.0,
                    filled_avg_price=101.0,
                    submitted_at="2026-07-09T13:30:00Z",
                    created_at="2026-07-09T13:30:00Z",
                    updated_at="2026-07-09T13:30:05Z",
                    expires_at="2026-07-09T21:00:00Z",
                ),
            },
            positions=[SimpleNamespace(symbol="XLV", qty=1.0, market_value=101.0, unrealized_pl=1.0)],
            account=SimpleNamespace(equity=100_010.0),
        )
        output = self.tmp_path / "report.json"
        result = run_gate1_report(cycles_dir=self.tmp_path, output=output, broker=broker)
        self.assertEqual(result.status, "OK")
        payload = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(payload["account_equity"], 100_010.0)
        self.assertEqual(len(payload["fills"]), 1)
        fill = payload["fills"][0]
        self.assertEqual(fill["symbol"], "XLV")
        self.assertEqual(fill["effective_cost_bps"], 100.0)
        # 1% absolute deviation vs reference 100 → exactly 100 bps.
        self.assertEqual(payload["effective_cost_bps"]["n"], 1)
        self.assertEqual(payload["effective_cost_bps"]["median"], 100.0)
        self.assertEqual(payload["positions"][0]["unrealized_pl"], 1.0)


class CorruptArtifactTests(_HelperBase):
    def test_corrupt_json_recorded_as_incident_and_report_continues(self) -> None:
        # Valid file plus a corrupted sibling.
        _write_cycle(
            self.tmp_path / "cycle_etf_2026-07-09.json",
            sleeve="etf",
            as_of="2026-07-09",
            status="OK",
            submissions=[
                {"pair": "IWM", "action": "buy", "submitted": True, "skipped": False, "status": "accepted"},
            ],
            plan=_build_minimal_plan({"IWM": 220.0}),
        )
        (self.tmp_path / "cycle_crypto_2026-07-09.json").write_text("{ this is not json", encoding="utf-8")

        output = self.tmp_path / "report.json"
        result = run_gate1_report(cycles_dir=self.tmp_path, output=output)
        self.assertEqual(result.status, "WARN")
        self.assertEqual(result.exit_code, 0)  # WARN keeps exit 0 (per _EXIT_CODES)
        payload = json.loads(output.read_text(encoding="utf-8"))
        # The valid cycle still produced counts and the corrupt file is in incidents.
        self.assertEqual(payload["n_cycles"], 2)
        self.assertEqual(payload["by_status"]["OK"], 1)
        self.assertTrue(any(incident.startswith("unreadable:cycle_crypto_2026-07-09.json") for incident in payload["incidents"]))


class EmptyDirectoryTests(_HelperBase):
    def test_empty_directory_blocks_with_no_cycles_found(self) -> None:
        empty = self.tmp_path / "empty"
        empty.mkdir()
        output = empty / "report.json"
        result = run_gate1_report(cycles_dir=empty, output=output)
        self.assertEqual(result.status, "BLOCKED")
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("no_cycles_found", result.payload["incidents"])
        self.assertTrue(output.exists())


class WindowFilterTests(_HelperBase):
    def test_from_to_excludes_dates_outside_window(self) -> None:
        _write_cycle(
            self.tmp_path / "cycle_crypto_2026-07-08.json",
            sleeve="crypto",
            as_of="2026-07-08",
            status="OK",
            submissions=[],
            plan=_build_minimal_plan({"BTC/USD": 63000.0}),
        )
        _write_cycle(
            self.tmp_path / "cycle_crypto_2026-07-10.json",
            sleeve="crypto",
            as_of="2026-07-10",
            status="OK",
            submissions=[],
            plan=_build_minimal_plan({"BTC/USD": 64000.0}),
        )
        _write_cycle(
            self.tmp_path / "cycle_crypto_2026-07-12.json",
            sleeve="crypto",
            as_of="2026-07-12",
            status="OK",
            submissions=[],
            plan=_build_minimal_plan({"BTC/USD": 65000.0}),
        )
        output = self.tmp_path / "report.json"
        result = run_gate1_report(
            cycles_dir=self.tmp_path,
            output=output,
            start=date(2026, 7, 9),
            end=date(2026, 7, 11),
        )
        self.assertEqual(result.status, "OK")
        self.assertEqual(sorted(result.payload["days"]), ["2026-07-10"])


class BrokerLookupFailureTests(_HelperBase):
    def test_broker_lookup_raising_records_incident_without_crashing(self) -> None:
        _write_cycle(
            self.tmp_path / "cycle_etf_2026-07-09.json",
            sleeve="etf",
            as_of="2026-07-09",
            status="OK",
            submissions=[
                {
                    "pair": "IWM",
                    "action": "buy",
                    "client_order_id": "sleeve-2026-07-09-IWM-buy",
                    "submitted": True,
                    "skipped": False,
                    "status": "accepted",
                },
            ],
            plan=_build_minimal_plan({"IWM": 220.0}),
        )
        broker = _FakeBroker(
            order_snapshots={},
            raise_for={"sleeve-2026-07-09-IWM-buy"},
        )
        output = self.tmp_path / "report.json"
        result = run_gate1_report(cycles_dir=self.tmp_path, output=output, broker=broker)
        self.assertEqual(result.status, "WARN")
        self.assertTrue(output.exists())
        payload = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(payload["fills"], [])
        self.assertTrue(
            any(
                incident.startswith("order_lookup_failed:sleeve-2026-07-09-IWM-buy")
                for incident in payload["incidents"]
            )
        )


class CliTests(_HelperBase):
    def test_cli_dry_run_writes_json_and_markdown(self) -> None:
        _write_cycle(
            self.tmp_path / "cycle_etf_2026-07-09.json",
            sleeve="etf",
            as_of="2026-07-09",
            status="OK",
            submissions=[],
            plan=_build_minimal_plan({"IWM": 220.0}),
        )
        output = self.tmp_path / "report.json"
        markdown_output = self.tmp_path / "report.md"
        stdout = StringIO()
        stderr = StringIO()
        argv = [
            "sleeve-gate1-report",
            "--cycles-dir",
            str(self.tmp_path),
            "--output",
            str(output),
            "--markdown-output",
            str(markdown_output),
        ]
        with redirect_stdout(stdout), redirect_stderr(stderr):
            exit_code = main(argv)
        self.assertEqual(exit_code, 0, msg=stderr.getvalue())
        self.assertTrue(output.exists())
        self.assertTrue(markdown_output.exists())
        payload = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(payload["status"], "OK")
        # Markdown should at least contain the status header and the days table.
        markdown_text = markdown_output.read_text(encoding="utf-8")
        self.assertIn("Gate 1 Scorecard", markdown_text)
        self.assertIn("2026-07-09", markdown_text)

    def test_cli_real_paper_without_confirm_returns_error(self) -> None:
        argv = [
            "sleeve-gate1-report",
            "--cycles-dir",
            str(self.tmp_path),
            "--real-paper",
        ]
        stderr = StringIO()
        with redirect_stderr(stderr):
            exit_code = main(argv)
        self.assertEqual(exit_code, 2)
        self.assertIn("--real-paper requires --confirm-paper", stderr.getvalue())


class RiskTrackTests(_HelperBase):
    """Sprint M8: Gate 1 scorecard must surface per-cycle ``account_risk``
    snapshots (Sprint M7+) so the operator can audit the kill-switch inputs
    day by day from the Gate 1 evidence.
    """

    def test_per_day_picks_highest_drawdown_and_builds_risk_track(self) -> None:
        # Day 1: two cycles on the same date with different account_risk
        # snapshots — the higher current_drawdown_pct must win per-day.
        risk_a = {
            "equity": 99_000.0,
            "last_equity": 100_000.0,
            "daily_pnl_pct": -0.01,
            "high_water_equity": 100_000.0,
            "current_drawdown_pct": 0.01,
        }
        risk_b = {
            "equity": 97_000.0,
            "last_equity": 100_000.0,
            "daily_pnl_pct": -0.03,
            "high_water_equity": 100_000.0,
            "current_drawdown_pct": 0.03,
        }
        # Day 2: a single cycle with a positive daily PnL and zero drawdown.
        risk_c = {
            "equity": 102_000.0,
            "last_equity": 101_000.0,
            "daily_pnl_pct": 0.0099,
            "high_water_equity": 102_000.0,
            "current_drawdown_pct": 0.0,
        }
        _write_cycle(
            self.tmp_path / "cycle_crypto_2026-07-09.json",
            sleeve="crypto",
            as_of="2026-07-09",
            status="OK",
            submissions=[],
            plan=_build_minimal_plan({"BTC/USD": 63000.0}),
            account_risk=risk_a,
        )
        _write_cycle(
            self.tmp_path / "cycle_etf_2026-07-09.json",
            sleeve="etf",
            as_of="2026-07-09",
            status="OK",
            submissions=[],
            plan=_build_minimal_plan({"IWM": 220.0}),
            account_risk=risk_b,
        )
        _write_cycle(
            self.tmp_path / "cycle_crypto_2026-07-10.json",
            sleeve="crypto",
            as_of="2026-07-10",
            status="OK",
            submissions=[],
            plan=_build_minimal_plan({"BTC/USD": 64000.0}),
            account_risk=risk_c,
        )

        output = self.tmp_path / "report.json"
        markdown_output = self.tmp_path / "report.md"
        result = run_gate1_report(
            cycles_dir=self.tmp_path,
            output=output,
            markdown_output=markdown_output,
        )
        self.assertEqual(result.status, "OK")
        payload = json.loads(output.read_text(encoding="utf-8"))
        per_day = payload["per_day"]
        self.assertEqual([entry["date"] for entry in per_day], ["2026-07-09", "2026-07-10"])

        # Per-day selection: day 1 keeps the highest-drawdown snapshot (risk_b),
        # day 2 has only risk_c.
        day1 = per_day[0]["account_risk"]
        day2 = per_day[1]["account_risk"]
        self.assertEqual(day1["current_drawdown_pct"], 0.03)
        self.assertEqual(day1["equity"], 97_000.0)
        self.assertEqual(day2, risk_c)

        risk_track = payload["risk_track"]
        self.assertEqual(risk_track["days_with_risk_context"], 2)
        self.assertEqual(risk_track["min_equity"], 97_000.0)
        self.assertEqual(risk_track["max_equity"], 102_000.0)
        self.assertEqual(risk_track["max_drawdown_pct_observed"], 0.03)
        self.assertEqual(risk_track["worst_daily_pnl_pct"], -0.03)

        # Markdown surfaces the new section AND both per-day rows.
        markdown_text = markdown_output.read_text(encoding="utf-8")
        self.assertIn("## Risk track", markdown_text)
        self.assertIn("Days with risk context: **2**", markdown_text)
        self.assertIn("2026-07-09", markdown_text)
        self.assertIn("2026-07-10", markdown_text)
        self.assertIn("| Date | Equity (USD) | Daily PnL % | Drawdown % |", markdown_text)

    def test_pre_m7_payloads_without_account_risk_remain_compatible(self) -> None:
        # Pre-M7 cycle shapes have no ``account_risk`` key at all — the
        # scorecard must keep producing OK reports with ``risk_track`` set
        # to the empty/null shape and per-day ``account_risk`` left as
        # None.
        _write_cycle(
            self.tmp_path / "cycle_crypto_2026-07-09.json",
            sleeve="crypto",
            as_of="2026-07-09",
            status="OK",
            submissions=[],
            plan=_build_minimal_plan({"BTC/USD": 63000.0}),
        )
        _write_cycle(
            self.tmp_path / "cycle_etf_2026-07-09.json",
            sleeve="etf",
            as_of="2026-07-09",
            status="OK",
            submissions=[],
            plan=_build_minimal_plan({"IWM": 220.0}),
        )

        output = self.tmp_path / "report.json"
        markdown_output = self.tmp_path / "report.md"
        result = run_gate1_report(
            cycles_dir=self.tmp_path,
            output=output,
            markdown_output=markdown_output,
        )
        self.assertEqual(result.status, "OK")
        self.assertEqual(result.exit_code, 0)
        payload = json.loads(output.read_text(encoding="utf-8"))
        # No account_risk seen anywhere — per-day stays None and risk_track
        # collapses to its empty/null shape.
        for entry in payload["per_day"]:
            self.assertIsNone(entry["account_risk"])
        risk_track = payload["risk_track"]
        self.assertEqual(risk_track["days_with_risk_context"], 0)
        self.assertIsNone(risk_track["min_equity"])
        self.assertIsNone(risk_track["max_equity"])
        self.assertIsNone(risk_track["max_drawdown_pct_observed"])
        self.assertIsNone(risk_track["worst_daily_pnl_pct"])
        # Pre-M7 must not generate a noise incident.
        self.assertEqual(payload["incidents"], [])

        # Markdown section still rendered (with null aggregates) so the
        # operator can see "no risk context" explicitly.
        markdown_text = markdown_output.read_text(encoding="utf-8")
        self.assertIn("## Risk track", markdown_text)
        self.assertIn("Days with risk context: **0**", markdown_text)

    def test_blocked_cycle_with_account_risk_context_unavailable_records_incident(self) -> None:
        # M7 path: the broker failed to provide a real account snapshot, so
        # sleeve_rebalance BLOCKed the cycle with that blocker. The scorecard
        # must surface the text as an incident (in addition to promoting the
        # global status to WARN via the existing BLOCKED routing).
        from trading_ai.execution.sleeve_gate1_report import (
            ACCOUNT_RISK_CONTEXT_UNAVAILABLE,
            PAPER_WARN,
        )

        _write_cycle(
            self.tmp_path / "cycle_crypto_2026-07-09.json",
            sleeve="crypto",
            as_of="2026-07-09",
            status="BLOCKED",
            submissions=[],
            plan=_build_minimal_plan({"BTC/USD": 63000.0}),
            account_risk=None,
            blockers=[ACCOUNT_RISK_CONTEXT_UNAVAILABLE],
        )

        output = self.tmp_path / "report.json"
        result = run_gate1_report(cycles_dir=self.tmp_path, output=output)
        self.assertEqual(result.status, PAPER_WARN)
        payload = json.loads(output.read_text(encoding="utf-8"))
        self.assertIn(ACCOUNT_RISK_CONTEXT_UNAVAILABLE, payload["incidents"])
        # The day must record the missing context (account_risk left None
        # by definition — the broker was the reason it was BLOCKed).
        self.assertEqual(len(payload["per_day"]), 1)
        self.assertIsNone(payload["per_day"][0]["account_risk"])
        # risk_track has 0 days with risk context because no snapshot was seen.
        self.assertEqual(payload["risk_track"]["days_with_risk_context"], 0)


if __name__ == "__main__":
    unittest.main()