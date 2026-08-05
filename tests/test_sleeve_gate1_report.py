"""Fail-closed tests for the governed Gate 1 paper scorecard."""

from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from trading_ai.cli import main
from trading_ai.execution.execution_evidence_ledger import DurableExecutionEvidenceLedger
from trading_ai.execution.sleeve_gate1_report import (
    SCHEMA_VERSION,
    compute_gate1_report_hash,
    load_gate1_policy,
    register_gate1_policy,
    run_gate1_report,
)

_AS_OF = "2026-07-09"
_CID = "sleeve-2026-07-09-XLV-buy"


def _policy(
    *,
    start: str = _AS_OF,
    end: str = _AS_OF,
    sleeves: tuple[str, ...] = ("etf",),
    min_days: int = 1,
    min_fills: int = 1,
    median_bps: float = 200.0,
    p90_bps: float = 200.0,
    max_daily_loss_pct: float = 0.05,
    max_drawdown_pct: float = 0.10,
) -> dict[str, object]:
    return {
        "schema_version": "2.0",
        "policy_id": "gate1-paper-2026-07",
        "created_at": "2026-07-01T00:00:00+00:00",
        "window": {"start": start, "end": end},
        "required_sleeves": list(sleeves),
        "min_complete_days": min_days,
        "price_shortfall_limits_bps": {
            sleeve: {
                "min_reconciled_fills": min_fills,
                "max_median": median_bps,
                "max_p90": p90_bps,
            }
            for sleeve in sleeves
        },
        "max_daily_loss_pct": max_daily_loss_pct,
        "max_drawdown_pct": max_drawdown_pct,
    }


def _valid_risk(
    *,
    equity: float = 100_000.0,
    last_equity: float = 100_000.0,
    high_water_equity: float = 100_000.0,
) -> dict[str, float]:
    return {
        "equity": equity,
        "last_equity": last_equity,
        "daily_pnl_pct": (equity - last_equity) / last_equity,
        "high_water_equity": high_water_equity,
        "current_drawdown_pct": max(
            0.0,
            (high_water_equity - equity) / high_water_equity,
        ),
    }


def _valid_attestation(*, eligible: bool = True) -> dict[str, object]:
    return {
        "schema_version": "1.1",
        "status": "OK",
        "sidecar_status": "OK",
        "published": True,
        "valid": True,
        "eligible_for_submit": eligible,
        "source_sha256": "a" * 64,
        "blockers": [],
    }


def _plan(*, symbol: str = "XLV", action: str = "buy", reference_price: float = 100.0) -> list[dict[str, object]]:
    return [
        {
            "pair": symbol,
            "action": action,
            "target_notional": 100.0,
            "current_notional": 0.0,
            "delta": 100.0,
            "notional": 100.0,
            "quantity": None,
            "reference_price": reference_price,
            "weight": 0.1,
        }
    ]


def _submission(
    *,
    client_order_id: str = _CID,
    symbol: str = "XLV",
    action: str = "buy",
) -> dict[str, object]:
    return {
        "pair": symbol,
        "action": action,
        "client_order_id": client_order_id,
        "submitted": True,
        "skipped": False,
        "status": "accepted",
    }


def _write_cycle(
    root: Path,
    *,
    sleeve: str = "etf",
    as_of: str = _AS_OF,
    status: str = "OK",
    submissions: list[dict[str, object]] | None = None,
    plan: list[dict[str, object]] | None = None,
    account_risk: dict[str, float] | None = None,
    attestation: dict[str, object] | None = None,
    blockers: list[str] | None = None,
    safety_overrides: dict[str, object] | None = None,
) -> Path:
    submissions = list(submissions if submissions is not None else [_submission()])
    submitted = any(item.get("submitted") is True for item in submissions)
    safety: dict[str, object] = {
        "paper_only": True,
        "orders_submitted": submitted,
        "orders_attempted": submitted,
        "orders_submission_unknown": False,
        "confirm_submit": submitted,
        "live_trading_authorized": False,
    }
    safety.update(safety_overrides or {})
    payload = {
        "schema_version": "1.0",
        "generated_at": f"{as_of}T22:00:00+00:00",
        "as_of": as_of,
        "universe": "test",
        "dataset": str(root / f"dataset-{sleeve}-{as_of}.csv"),
        "dataset_attestation": attestation or _valid_attestation(eligible=submitted),
        "weights": {},
        "plan": plan if plan is not None else _plan(),
        "submissions": submissions,
        "account_risk": account_risk or _valid_risk(),
        "blockers": list(blockers or []),
        "status": status,
        "safety": safety,
    }
    path = root / f"cycle_{sleeve}_{as_of}.json"
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return path


def _write_allocation(
    root: Path,
    *,
    as_of: str = _AS_OF,
    sleeves: tuple[str, ...] = ("etf",),
) -> Path:
    total = float(100 * len(sleeves))
    sleeve_payload = {
        sleeve: {
            "budget_usd": 100.0,
            "n_returns": 200,
            "scale": 1.0,
            "trailing_vol": 0.01,
        }
        for sleeve in sleeves
    }
    payload = {
        "allocation": {
            "sleeves": sleeve_payload,
            "total_notional_usd": total,
        },
        "total_notional_usd": total,
    }
    path = root / f"allocation_{as_of}.json"
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return path


def _order(
    *,
    client_order_id: str = _CID,
    order_id: str = "ord-1",
    symbol: str = "XLV",
    side: str = "buy",
    status: str = "filled",
    quantity: float = 1.0,
    filled_quantity: float = 1.0,
    filled_avg_price: float = 101.0,
) -> SimpleNamespace:
    return SimpleNamespace(
        order_id=order_id,
        client_order_id=client_order_id,
        symbol=symbol,
        side=side,
        order_type="market",
        time_in_force="day",
        status=status,
        notional=None,
        quantity=quantity,
        filled_quantity=filled_quantity,
        filled_avg_price=filled_avg_price,
        submitted_at="2026-07-09T13:30:00+00:00",
        created_at="2026-07-09T13:30:00+00:00",
        updated_at="2026-07-09T13:30:05+00:00",
        expires_at="2026-07-09T21:00:00+00:00",
        filled_at="2026-07-09T13:30:05+00:00",
    )


def _activity(
    *,
    activity_id: str = "fill-1",
    order_id: str = "ord-1",
    symbol: str = "XLV",
    side: str = "buy",
    quantity: float = 1.0,
    price: float = 101.0,
    cumulative_quantity: float = 1.0,
    leaves_quantity: float = 0.0,
    transaction_time: str = "2026-07-09T13:30:05+00:00",
) -> SimpleNamespace:
    return SimpleNamespace(
        activity_id=activity_id,
        order_id=order_id,
        symbol=symbol,
        side=side,
        quantity=quantity,
        price=price,
        cumulative_quantity=cumulative_quantity,
        leaves_quantity=leaves_quantity,
        transaction_time=transaction_time,
        activity_type="fill",
        order_status="filled",
    )


class _FakeBroker:
    def __init__(
        self,
        *,
        orders: dict[str, SimpleNamespace] | None = None,
        activities: list[SimpleNamespace] | None = None,
        positions: list[SimpleNamespace] | None = None,
        account: Any = None,
        raise_for: set[str] | None = None,
    ) -> None:
        self.orders = orders or {}
        self.activities = activities or []
        self.positions = positions or []
        self.account = account or SimpleNamespace(equity=100_000.0)
        self.raise_for = raise_for or set()
        self.activity_window: tuple[datetime, datetime] | None = None

    def get_order_by_client_id(self, client_order_id: str) -> SimpleNamespace:
        if client_order_id in self.raise_for:
            raise RuntimeError("simulated secret=must-not-leak")
        return self.orders[client_order_id]

    def list_fill_activities(
        self,
        *,
        after: datetime,
        until: datetime,
    ) -> tuple[SimpleNamespace, ...]:
        self.activity_window = (after, until)
        return tuple(self.activities)

    def read_positions(self) -> tuple[SimpleNamespace, ...]:
        return tuple(self.positions)

    def read_account(self) -> Any:
        return self.account


class _Clock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


def _registered_ledger(
    root: Path,
    policy: dict[str, object],
) -> DurableExecutionEvidenceLedger:
    clock = _Clock(datetime(2026, 7, 1, tzinfo=UTC))
    ledger = DurableExecutionEvidenceLedger(root / "execution-evidence.sqlite3", clock=clock)
    register_gate1_policy(policy, evidence_ledger=ledger)
    clock.value = datetime(2026, 7, 14, 23, 0, tzinfo=UTC)
    return ledger


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name)

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def prepare_valid(
        self,
        *,
        action: str = "buy",
        side: str = "buy",
        reference_price: float = 100.0,
        fill_price: float = 101.0,
    ) -> tuple[dict[str, object], _FakeBroker]:
        symbol = "XLV"
        _write_cycle(
            self.root,
            submissions=[_submission(action=action)],
            plan=_plan(action=action, reference_price=reference_price),
        )
        _write_allocation(self.root)
        broker = _FakeBroker(
            orders={_CID: _order(side=side, filled_avg_price=fill_price)},
            activities=[_activity(side=side, price=fill_price)],
            positions=[
                SimpleNamespace(
                    symbol=symbol,
                    qty=1.0,
                    market_value=fill_price,
                    unrealized_pl=0.0,
                )
            ],
        )
        self.evidence_ledger = _registered_ledger(self.root, _policy())
        return _policy(), broker


class StrictHappyPathTests(_Base):
    def test_valid_individual_fill_evidence_passes_only_operational_paper_gate(self) -> None:
        policy, broker = self.prepare_valid()
        output = self.root / "report.json"

        result = run_gate1_report(
            cycles_dir=self.root,
            output=output,
            policy=policy,
            broker=broker,
            evidence_ledger=self.evidence_ledger,
            generated_at="2026-07-10T00:00:00+00:00",
        )

        self.assertEqual(result.status, "OK")
        self.assertEqual(result.exit_code, 0)
        payload = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(payload["schema_version"], SCHEMA_VERSION)
        self.assertEqual(payload["signed_price_shortfall_bps"]["median"], 100.0)
        self.assertEqual(
            payload["signed_price_shortfall_bps_by_sleeve"]["etf"]["median"],
            100.0,
        )
        self.assertEqual(payload["fills"][0]["broker_latency_ms"], 5000)
        self.assertTrue(payload["gate"]["operational_pass"])
        self.assertFalse(payload["gate"]["economic_reconciliation_complete"])
        self.assertFalse(payload["gate"]["promotion_eligible"])
        self.assertFalse(payload["implementation_shortfall_bps"]["complete"])
        self.assertTrue(payload["execution_evidence_ledger"]["valid"])
        self.assertEqual(payload["execution_evidence_ledger"]["fill_count"], 1)
        self.assertEqual(payload["artifact_hash"], compute_gate1_report_hash(payload))
        self.assertEqual(
            broker.activity_window,
            (
                datetime.fromisoformat("2026-07-09T00:00:00+00:00"),
                datetime.fromisoformat("2026-07-10T00:00:00+00:00"),
            ),
        )

        rerun = run_gate1_report(
            cycles_dir=self.root,
            output=self.root / "report-rerun.json",
            policy=policy,
            broker=broker,
            evidence_ledger=self.evidence_ledger,
            generated_at="2026-07-10T00:00:01+00:00",
        )
        self.assertEqual(rerun.status, "OK")
        self.assertEqual(rerun.payload["execution_evidence_ledger"]["fill_count"], 1)
        self.assertEqual(rerun.payload["execution_evidence_ledger"]["manifest_count"], 2)

    def test_favorable_sell_keeps_negative_sign(self) -> None:
        policy, broker = self.prepare_valid(
            action="sell_all",
            side="sell",
            reference_price=100.0,
            fill_price=101.0,
        )

        result = run_gate1_report(
            cycles_dir=self.root,
            output=self.root / "sell.json",
            policy=policy,
            broker=broker,
            evidence_ledger=self.evidence_ledger,
            generated_at="2026-07-10T00:00:00+00:00",
        )

        self.assertEqual(result.status, "OK")
        self.assertEqual(result.payload["fills"][0]["price_shortfall_bps"], -100.0)
        self.assertEqual(result.payload["signed_price_shortfall_bps"]["favorable_n"], 1)


class MissingOrCorruptEvidenceTests(_Base):
    def test_policy_loader_rejects_duplicate_keys_and_nonfinite_numbers(self) -> None:
        policy_path = self.root / "policy.json"
        invalid_documents = (
            '{"schema_version":"2.0","schema_version":"2.0"}',
            '{"max_drawdown_pct":NaN}',
            '{"max_drawdown_pct":Infinity}',
        )
        for document in invalid_documents:
            with self.subTest(document=document):
                policy_path.write_text(document, encoding="utf-8")
                with self.assertRaises(ValueError):
                    load_gate1_policy(policy_path)

    def test_invalid_policy_values_block_and_still_emit_strict_json(self) -> None:
        policy, broker = self.prepare_valid()
        policy["max_drawdown_pct"] = float("nan")
        policy["required_sleeves"] = "etf"
        output = self.root / "report.json"

        result = run_gate1_report(
            cycles_dir=self.root,
            output=output,
            policy=policy,
            broker=broker,
        )

        self.assertEqual(result.status, "BLOCKED")
        self.assertIn("gate_policy_max_drawdown_pct_invalid", result.payload["incidents"])
        self.assertIn("gate_policy_required_sleeves_invalid", result.payload["incidents"])
        decoded = json.loads(
            output.read_text(encoding="utf-8"),
            parse_constant=lambda token: self.fail(f"non-finite JSON constant: {token}"),
        )
        self.assertIsNone(decoded["policy"]["max_drawdown_pct"])
        self.assertEqual(decoded["policy"]["required_sleeves"], [])

    def test_missing_policy_and_allocation_only_are_blocked(self) -> None:
        _write_allocation(self.root)
        result = run_gate1_report(cycles_dir=self.root, output=self.root / "report.json")
        self.assertEqual(result.status, "BLOCKED")
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("gate_policy_missing", result.payload["incidents"])
        self.assertIn("no_cycles_found", result.payload["incidents"])

    def test_unregistered_execution_policy_is_blocking(self) -> None:
        policy, broker = self.prepare_valid()
        empty_ledger = DurableExecutionEvidenceLedger(self.root / "empty-ledger.sqlite3")

        result = run_gate1_report(
            cycles_dir=self.root,
            output=self.root / "unregistered.json",
            policy=policy,
            broker=broker,
            evidence_ledger=empty_ledger,
        )

        self.assertEqual(result.status, "BLOCKED")
        self.assertIn("execution_policy_not_preregistered", result.payload["incidents"])

    def test_corrupt_cycle_is_blocking_and_nonzero(self) -> None:
        _write_cycle(self.root)
        _write_allocation(self.root)
        (self.root / "cycle_crypto_2026-07-09.json").write_text(
            "{not-json",
            encoding="utf-8",
        )
        broker = _FakeBroker(
            orders={_CID: _order()},
            activities=[_activity()],
        )

        result = run_gate1_report(
            cycles_dir=self.root,
            output=self.root / "report.json",
            policy=_policy(),
            broker=broker,
        )

        self.assertEqual(result.status, "BLOCKED")
        self.assertNotEqual(result.exit_code, 0)
        self.assertTrue(
            any(
                item.startswith("cycle_unreadable:cycle_crypto_2026-07-09.json")
                for item in result.payload["incidents"]
            )
        )

    def test_non_ok_cycle_and_submission_error_are_blocking(self) -> None:
        _write_cycle(
            self.root,
            status="REPORT_ONLY",
            submissions=[
                {
                    "pair": "XLV",
                    "action": "buy",
                    "submitted": False,
                    "skipped": False,
                    "status": "error",
                }
            ],
        )
        _write_allocation(self.root)

        result = run_gate1_report(
            cycles_dir=self.root,
            output=self.root / "report.json",
            policy=_policy(),
        )

        self.assertEqual(result.status, "BLOCKED")
        incidents = result.payload["incidents"]
        self.assertTrue(any("cycle_status_not_ok" in item for item in incidents))
        self.assertTrue(any("submission_unresolved" in item for item in incidents))


class BrokerAndFillReconciliationTests(_Base):
    def test_nonterminal_order_and_missing_fill_activity_are_blocking(self) -> None:
        policy, _broker = self.prepare_valid()
        broker = _FakeBroker(
            orders={_CID: _order(status="accepted", filled_quantity=0.0)},
            activities=[],
        )

        result = run_gate1_report(
            cycles_dir=self.root,
            output=self.root / "report.json",
            policy=policy,
            broker=broker,
        )

        self.assertEqual(result.status, "BLOCKED")
        self.assertTrue(
            any(item.startswith("broker_order_not_terminal") for item in result.payload["incidents"])
        )
        self.assertIn("insufficient_reconciled_fills:etf", result.payload["incidents"])

    def test_quantity_vwap_cumulative_and_external_fill_mismatches_block(self) -> None:
        policy, _broker = self.prepare_valid()
        broker = _FakeBroker(
            orders={_CID: _order(filled_quantity=1.0, filled_avg_price=101.0)},
            activities=[
                _activity(
                    quantity=0.5,
                    price=102.0,
                    cumulative_quantity=0.75,
                    leaves_quantity=0.5,
                ),
                _activity(
                    activity_id="orphan-fill",
                    order_id="external-order",
                ),
            ],
        )

        result = run_gate1_report(
            cycles_dir=self.root,
            output=self.root / "report.json",
            policy=policy,
            broker=broker,
        )

        self.assertEqual(result.status, "BLOCKED")
        incidents = result.payload["incidents"]
        self.assertTrue(any(item.startswith("fill_activity_sequence_invalid") for item in incidents))
        self.assertTrue(any(item.startswith("fill_quantity_sum_mismatch") for item in incidents))
        self.assertTrue(any(item.startswith("fill_vwap_mismatch") for item in incidents))
        self.assertIn("orphan_or_external_fill:external-order", incidents)

    def test_lookup_exception_is_redacted_and_blocking(self) -> None:
        policy, _broker = self.prepare_valid()
        broker = _FakeBroker(raise_for={_CID}, activities=[])

        result = run_gate1_report(
            cycles_dir=self.root,
            output=self.root / "report.json",
            policy=policy,
            broker=broker,
        )

        serialized = json.dumps(result.payload)
        self.assertEqual(result.status, "BLOCKED")
        self.assertIn("order_lookup_failed", serialized)
        self.assertNotIn("must-not-leak", serialized)

    def test_duplicate_client_and_broker_order_ids_block(self) -> None:
        second_cid = "sleeve-2026-07-09-XLV-buy-second"
        _write_cycle(
            self.root,
            sleeve="etf",
            submissions=[_submission(client_order_id=_CID)],
        )
        _write_cycle(
            self.root,
            sleeve="crypto",
            submissions=[
                _submission(
                    client_order_id=second_cid,
                    symbol="BTC/USD",
                )
            ],
            plan=_plan(symbol="BTC/USD"),
        )
        _write_allocation(self.root, sleeves=("etf", "crypto"))
        broker = _FakeBroker(
            orders={
                _CID: _order(order_id="same-order"),
                second_cid: _order(
                    client_order_id=second_cid,
                    order_id="same-order",
                    symbol="BTCUSD",
                ),
            },
            activities=[_activity(order_id="same-order")],
        )

        result = run_gate1_report(
            cycles_dir=self.root,
            output=self.root / "report.json",
            policy=_policy(sleeves=("etf", "crypto"), min_fills=1),
            broker=broker,
        )

        self.assertEqual(result.status, "BLOCKED")
        self.assertIn("duplicate_broker_order_id:same-order", result.payload["incidents"])


class PolicySafetyAndRiskTests(_Base):
    def test_bad_attestation_and_unknown_submission_state_block(self) -> None:
        bad_attestation = _valid_attestation()
        bad_attestation["source_sha256"] = "not-a-hash"
        _write_cycle(
            self.root,
            attestation=bad_attestation,
            safety_overrides={"orders_submission_unknown": True},
        )
        _write_allocation(self.root)
        broker = _FakeBroker(orders={_CID: _order()}, activities=[_activity()])

        result = run_gate1_report(
            cycles_dir=self.root,
            output=self.root / "report.json",
            policy=_policy(),
            broker=broker,
        )

        self.assertEqual(result.status, "BLOCKED")
        self.assertTrue(any("cycle_dataset_attestation_invalid" in item for item in result.payload["incidents"]))
        self.assertTrue(any("cycle_safety_invalid" in item for item in result.payload["incidents"]))

    def test_incoherent_or_nonfinite_risk_blocks(self) -> None:
        for invalid_risk in (
            {
                "equity": 90_000.0,
                "last_equity": 100_000.0,
                "daily_pnl_pct": 0.0,
                "high_water_equity": 100_000.0,
                "current_drawdown_pct": 0.0,
            },
            {
                "equity": float("nan"),
                "last_equity": 100_000.0,
                "daily_pnl_pct": 0.0,
                "high_water_equity": 100_000.0,
                "current_drawdown_pct": 0.0,
            },
        ):
            with self.subTest(invalid_risk=invalid_risk):
                case_root = self.root / str(len(list(self.root.iterdir())))
                case_root.mkdir()
                _write_cycle(case_root, account_risk=invalid_risk)
                _write_allocation(case_root)
                broker = _FakeBroker(orders={_CID: _order()}, activities=[_activity()])
                result = run_gate1_report(
                    cycles_dir=case_root,
                    output=case_root / "report.json",
                    policy=_policy(),
                    broker=broker,
                )
                self.assertEqual(result.status, "BLOCKED")
                self.assertTrue(
                    any("cycle_account_risk_invalid" in item for item in result.payload["incidents"])
                )

    def test_loss_drawdown_and_price_cost_policy_limits_block(self) -> None:
        loss_risk = _valid_risk(
            equity=90_000.0,
            last_equity=100_000.0,
            high_water_equity=100_000.0,
        )
        _write_cycle(self.root, account_risk=loss_risk)
        _write_allocation(self.root)
        broker = _FakeBroker(orders={_CID: _order()}, activities=[_activity()])

        result = run_gate1_report(
            cycles_dir=self.root,
            output=self.root / "report.json",
            policy=_policy(median_bps=50.0, p90_bps=50.0),
            broker=broker,
        )

        self.assertEqual(result.status, "BLOCKED")
        incidents = result.payload["incidents"]
        self.assertTrue(any("cycle_daily_loss_limit_exceeded" in item for item in incidents))
        self.assertIn("price_shortfall_median_exceeded:etf", incidents)

    def test_one_sleeve_cannot_hide_bad_cost_behind_global_aggregate(self) -> None:
        crypto_cid = "sleeve-2026-07-09-BTC-buy"
        _write_cycle(
            self.root,
            sleeve="etf",
            plan=_plan(reference_price=100.0),
        )
        _write_cycle(
            self.root,
            sleeve="crypto",
            submissions=[
                _submission(client_order_id=crypto_cid, symbol="BTC/USD")
            ],
            plan=_plan(symbol="BTC/USD", reference_price=100.0),
        )
        _write_allocation(self.root, sleeves=("etf", "crypto"))
        policy = _policy(
            sleeves=("etf", "crypto"),
            median_bps=200.0,
            p90_bps=400.0,
        )
        broker = _FakeBroker(
            orders={
                _CID: _order(filled_avg_price=100.0),
                crypto_cid: _order(
                    client_order_id=crypto_cid,
                    order_id="ord-crypto",
                    symbol="BTCUSD",
                    filled_avg_price=103.0,
                ),
            },
            activities=[
                _activity(price=100.0),
                _activity(
                    activity_id="fill-crypto",
                    order_id="ord-crypto",
                    symbol="BTCUSD",
                    price=103.0,
                ),
            ],
        )
        policy["price_shortfall_limits_bps"]["crypto"]["max_median"] = 100.0

        result = run_gate1_report(
            cycles_dir=self.root,
            output=self.root / "report.json",
            policy=policy,
            broker=broker,
            evidence_ledger=_registered_ledger(self.root, policy),
        )

        self.assertEqual(result.status, "BLOCKED")
        self.assertEqual(result.payload["signed_price_shortfall_bps"]["median"], 150.0)
        self.assertEqual(
            result.payload["signed_price_shortfall_bps_by_sleeve"]["crypto"]["median"],
            300.0,
        )
        self.assertIn("price_shortfall_median_exceeded:crypto", result.payload["incidents"])
        self.assertNotIn("price_shortfall_median_exceeded:etf", result.payload["incidents"])

    def test_policy_requires_exact_per_sleeve_limit_set(self) -> None:
        policy, broker = self.prepare_valid()
        policy["required_sleeves"] = ["etf", "crypto"]

        result = run_gate1_report(
            cycles_dir=self.root,
            output=self.root / "report.json",
            policy=policy,
            broker=broker,
        )

        self.assertEqual(result.status, "BLOCKED")
        self.assertIn(
            "gate_policy_price_shortfall_limits_bps_invalid",
            result.payload["incidents"],
        )

    def test_policy_created_inside_window_and_bad_hash_are_blocking(self) -> None:
        policy, broker = self.prepare_valid()
        policy["created_at"] = "2026-07-09T00:00:00+00:00"

        result = run_gate1_report(
            cycles_dir=self.root,
            output=self.root / "report.json",
            policy=policy,
            policy_sha256="fake",
            broker=broker,
        )

        self.assertEqual(result.status, "BLOCKED")
        self.assertIn("gate_policy_not_preregistered", result.payload["incidents"])
        self.assertIn("policy_sha256_invalid", result.payload["incidents"])

    def test_missing_required_sleeve_and_allocation_mismatch_block(self) -> None:
        _write_cycle(self.root, sleeve="etf")
        _write_allocation(self.root, sleeves=("etf",))
        broker = _FakeBroker(orders={_CID: _order()}, activities=[_activity()])

        result = run_gate1_report(
            cycles_dir=self.root,
            output=self.root / "report.json",
            policy=_policy(sleeves=("etf", "crypto")),
            broker=broker,
        )

        self.assertEqual(result.status, "BLOCKED")
        self.assertTrue(any("required_sleeves_incomplete" in item for item in result.payload["incidents"]))
        self.assertTrue(any("allocation_invalid" in item for item in result.payload["incidents"]))


class RiskTrackTests(_Base):
    def test_risk_track_uses_highest_daily_drawdown(self) -> None:
        second_cid = "sleeve-2026-07-09-BTC-buy"
        risk_etf = _valid_risk(equity=99_000.0, last_equity=100_000.0, high_water_equity=100_000.0)
        risk_crypto = _valid_risk(equity=97_000.0, last_equity=100_000.0, high_water_equity=100_000.0)
        _write_cycle(self.root, sleeve="etf", account_risk=risk_etf)
        _write_cycle(
            self.root,
            sleeve="crypto",
            submissions=[_submission(client_order_id=second_cid, symbol="BTC/USD")],
            plan=_plan(symbol="BTC/USD"),
            account_risk=risk_crypto,
        )
        _write_allocation(self.root, sleeves=("etf", "crypto"))
        broker = _FakeBroker(
            orders={
                _CID: _order(),
                second_cid: _order(
                    client_order_id=second_cid,
                    order_id="ord-2",
                    symbol="BTCUSD",
                ),
            },
            activities=[
                _activity(),
                _activity(
                    activity_id="fill-2",
                    order_id="ord-2",
                    symbol="BTCUSD",
                ),
            ],
        )

        result = run_gate1_report(
            cycles_dir=self.root,
            output=self.root / "report.json",
            policy=_policy(sleeves=("etf", "crypto"), min_fills=1),
            broker=broker,
            evidence_ledger=_registered_ledger(
                self.root,
                _policy(sleeves=("etf", "crypto"), min_fills=1),
            ),
            generated_at="2026-07-10T00:00:00+00:00",
        )

        self.assertEqual(result.status, "OK")
        self.assertEqual(result.payload["per_day"][0]["account_risk"], risk_crypto)
        self.assertEqual(result.payload["risk_track"]["max_drawdown_pct_observed"], 0.03)


class CliTests(_Base):
    def test_cli_preregisters_future_policy_idempotently(self) -> None:
        future_policy = _policy(start="2099-07-09", end="2099-07-09")
        policy_path = self.root / "future-policy.json"
        policy_path.write_text(json.dumps(future_policy), encoding="utf-8")
        ledger_path = self.root / "future-ledger.sqlite3"
        argv = [
            "sleeve-gate1-policy-register",
            "--policy",
            str(policy_path),
            "--evidence-ledger",
            str(ledger_path),
        ]

        self.assertEqual(main(argv), 0)
        self.assertEqual(main(argv), 0)
        registration = DurableExecutionEvidenceLedger(ledger_path).read_policy(
            str(future_policy["policy_id"])
        )
        self.assertIsNotNone(registration)
        assert registration is not None
        self.assertLess(registration.registered_at, registration.effective_from)

    def test_cli_without_broker_never_passes_submitted_evidence(self) -> None:
        _write_cycle(self.root)
        _write_allocation(self.root)
        policy_path = self.root / "policy.json"
        policy_path.write_text(json.dumps(_policy()), encoding="utf-8")
        output = self.root / "report.json"
        markdown = self.root / "report.md"
        stdout = StringIO()
        stderr = StringIO()

        with redirect_stdout(stdout), redirect_stderr(stderr):
            exit_code = main(
                [
                    "sleeve-gate1-report",
                    "--cycles-dir",
                    str(self.root),
                    "--policy",
                    str(policy_path),
                    "--output",
                    str(output),
                    "--markdown-output",
                    str(markdown),
                ]
            )

        self.assertNotEqual(exit_code, 0)
        self.assertEqual(json.loads(output.read_text(encoding="utf-8"))["status"], "BLOCKED")
        self.assertIn("Gate 1 Scorecard", markdown.read_text(encoding="utf-8"))

    def test_cli_rejects_invalid_policy_and_unconfirmed_real_paper(self) -> None:
        invalid_policy = self.root / "invalid.json"
        invalid_policy.write_text("[]", encoding="utf-8")
        stderr = StringIO()
        with redirect_stderr(stderr):
            invalid_exit = main(
                [
                    "sleeve-gate1-report",
                    "--policy",
                    str(invalid_policy),
                ]
            )
        self.assertEqual(invalid_exit, 2)
        self.assertIn("invalid Gate 1 policy", stderr.getvalue())

        stderr = StringIO()
        with redirect_stderr(stderr):
            confirm_exit = main(["sleeve-gate1-report", "--real-paper"])
        self.assertEqual(confirm_exit, 2)
        self.assertIn("--real-paper requires --confirm-paper", stderr.getvalue())

    def test_cli_real_paper_uses_executor_without_direct_credentials(self) -> None:
        broker = object()
        result = SimpleNamespace(
            status="BLOCKED",
            exit_code=2,
            output_path=self.root / "report.json",
            payload={"days": [], "fills": []},
        )
        with (
            patch("trading_ai.cli.PaperExecutorBrokerClient", return_value=broker) as executor,
            patch(
                "trading_ai.execution.alpaca_connection.build_alpaca_paper_client",
                side_effect=AssertionError("direct broker credentials must not be used"),
            ) as direct_broker,
            patch(
                "trading_ai.execution.sleeve_gate1_report.run_gate1_report",
                return_value=result,
            ) as run,
        ):
            exit_code = main(
                [
                    "sleeve-gate1-report",
                    "--real-paper",
                    "--confirm-paper",
                ]
            )

        self.assertEqual(exit_code, 2)
        executor.assert_called_once_with()
        direct_broker.assert_not_called()
        self.assertIs(run.call_args.kwargs["broker"], broker)


if __name__ == "__main__":
    unittest.main()
