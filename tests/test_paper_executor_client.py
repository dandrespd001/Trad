from __future__ import annotations

import math
import unittest
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from trading_ai.execution.alpaca_paper import PaperOrder, PaperOrderResult, PaperPosition
from trading_ai.execution.paper_executor_client import PaperExecutorBrokerClient
from trading_ai.execution.paper_executor_ipc import ExecutorTarget, PaperExecutorProtocolError
from trading_ai.execution.paper_executor_service import deterministic_command_id


class RecordingTransport:
    """Credential-free transport double that records the high-level RPC contract."""

    def __init__(
        self,
        *responses: dict[str, Any],
        automatic_health: bool = False,
    ) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []
        self.automatic_health = automatic_health

    def request(
        self,
        operation: str,
        payload: Mapping[str, Any] | None = None,
        *,
        request_id: str | None = None,
        timeout_seconds: float | None = None,
        target: ExecutorTarget | Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.calls.append(
            {
                "operation": operation,
                "payload": dict(payload or {}),
                "request_id": request_id,
                "timeout_seconds": timeout_seconds,
                "target": target,
            }
        )
        if operation == "health" and self.automatic_health:
            return _health_payload()
        if not self.responses:
            raise AssertionError("unexpected executor RPC")
        return self.responses.pop(0)


class PaperExecutorBrokerClientTests(unittest.TestCase):
    def test_health_is_a_read_only_empty_payload_request(self) -> None:
        response = _health_payload()
        transport = RecordingTransport(response)
        client = PaperExecutorBrokerClient(transport)  # type: ignore[arg-type]

        self.assertEqual(client.health(), response)
        self.assertEqual(
            transport.calls,
            [
                {
                    "operation": "health",
                    "payload": {},
                    "request_id": None,
                    "timeout_seconds": None,
                    "target": None,
                }
            ],
        )

    def test_health_rejects_noncanonical_identity_types_and_field_sets(self) -> None:
        valid = _health_payload()
        variants: dict[str, dict[str, Any]] = {
            "missing": {key: value for key, value in valid.items() if key != "status"},
            "extra": {**valid, "executor_version": "1"},
            "empty_status": {**valid, "status": ""},
            "control_status": {**valid, "status": "ready\n"},
            "mutations_integer": {**valid, "mutations_allowed": 1},
            "opening_without_mutations": {
                **valid,
                "mutations_allowed": False,
                "opening_orders_allowed": True,
            },
            "wrong_capability": {**valid, "capability_mode": "reduce_only"},
            "scope_short": {**valid, "account_scope_sha256": "a" * 62},
            "scope_nonhex": {**valid, "account_scope_sha256": "g" * 64},
            "scope_uppercase": {**valid, "account_scope_sha256": "A" * 64},
            "zero_fence": {**valid, "fence_epoch": 0},
            "boolean_fence": {**valid, "fence_epoch": True},
            "policy_uppercase": {**valid, "policy_sha256": "B" * 64},
            "authz_policy_short": {**valid, "authz_policy_sha256": "c" * 63},
            "zero_run": {**valid, "run_id": "0" * 32},
            "short_run": {**valid, "run_id": "1" * 30},
            "uppercase_run": {**valid, "run_id": "A" * 32},
            "negative_recovery": {**valid, "pending_recovery": -1},
            "boolean_recovery": {**valid, "pending_recovery": False},
            "kill_switch_integer": {**valid, "kill_switch_active": 1},
        }
        for label, response in variants.items():
            with self.subTest(label=label):
                client = PaperExecutorBrokerClient(  # type: ignore[arg-type]
                    RecordingTransport(response)
                )
                with self.assertRaises(PaperExecutorProtocolError):
                    client.health()

    def test_health_rejects_opening_orders_with_active_kill_switch(self) -> None:
        response = {
            **_health_payload(),
            "opening_orders_allowed": True,
            "capability_mode": "full",
            "kill_switch_active": True,
        }
        client = PaperExecutorBrokerClient(RecordingTransport(response))  # type: ignore[arg-type]

        with self.assertRaisesRegex(PaperExecutorProtocolError, "kill switch"):
            client.health()

    def test_health_accepts_flattening_only_as_blocked_and_latched(self) -> None:
        response = {
            **_health_payload(),
            "status": "flattening",
            "mutations_allowed": False,
            "opening_orders_allowed": False,
            "capability_mode": "blocked",
            "kill_switch_active": True,
        }
        client = PaperExecutorBrokerClient(RecordingTransport(response))  # type: ignore[arg-type]

        self.assertEqual(client.health()["status"], "flattening")

    def test_pinned_target_is_reused_for_mutation_dispatch(self) -> None:
        transport = RecordingTransport(_result_payload(), automatic_health=True)
        client = PaperExecutorBrokerClient(transport)  # type: ignore[arg-type]
        health = client.health()

        pinned = client.pin_target(health)
        receipt = client.submit_order_with_receipt(_minimal_order())

        self.assertEqual(pinned, _executor_target())
        self.assertEqual(receipt.target, pinned)
        submit_call = next(
            call for call in transport.calls if call["operation"] == "submit_order"
        )
        self.assertEqual(submit_call["target"], pinned)

    def test_target_change_blocks_before_mutation_request(self) -> None:
        initial = _health_payload()
        changed = {**initial, "run_id": "2" * 32, "fence_epoch": 8}
        transport = RecordingTransport(initial, changed, _result_payload())
        client = PaperExecutorBrokerClient(transport)  # type: ignore[arg-type]
        client.pin_target(client.health())

        with self.assertRaisesRegex(PaperExecutorProtocolError, "target changed"):
            client.submit_order_with_receipt(_minimal_order())

        self.assertEqual(
            [call["operation"] for call in transport.calls],
            ["health", "health"],
        )

    def test_safe_flatten_start_and_status_use_closed_recoverable_identity(self) -> None:
        operation_id = "5" * 32
        transport = RecordingTransport(
            {"operation": _safe_flatten_fields(operation_id=operation_id)},
            {"operation": _safe_flatten_fields(operation_id=operation_id)},
            automatic_health=True,
        )
        client = PaperExecutorBrokerClient(transport)  # type: ignore[arg-type]

        receipt = client.start_safe_flatten(operation_id)
        status = client.get_safe_flatten_status(operation_id)

        self.assertEqual(receipt.request_id, operation_id)
        self.assertEqual(receipt.operation_id, operation_id)
        self.assertEqual(receipt.target, _executor_target())
        self.assertEqual(receipt.status, status)
        self.assertTrue(status.kill_switch_active)
        self.assertFalse(status.retry_allowed)
        self.assertEqual(
            transport.calls[1],
            {
                "operation": "start_safe_flatten",
                "payload": {"operation_id": operation_id},
                "request_id": operation_id,
                "timeout_seconds": None,
                "target": _executor_target(),
            },
        )
        self.assertEqual(transport.calls[2]["operation"], "get_safe_flatten_status")
        self.assertIsNone(transport.calls[2]["target"])

    def test_safe_flatten_status_invariants_fail_closed(self) -> None:
        operation_id = "5" * 32
        valid = _safe_flatten_fields(operation_id=operation_id)
        variants = {
            "extra": {**valid, "positions": []},
            "wrong_operation": {**valid, "operation_id": "6" * 32},
            "unlatched": {**valid, "kill_switch_active": False},
            "retry": {**valid, "retry_allowed": True},
            "false_terminal": {**valid, "terminal": True},
            "false_reconciled": {**valid, "reconciled": True},
            "unexpected_failure": {**valid, "failure_code": "failed"},
            "unexpected_unknown": {
                **valid,
                "outcome_unknown": {
                    "request_id": "7" * 32,
                    "operation": "cancel_order",
                    "phase": "canceling",
                    "retry_allowed": False,
                },
            },
        }
        for label, response in variants.items():
            with self.subTest(label=label):
                client = PaperExecutorBrokerClient(  # type: ignore[arg-type]
                    RecordingTransport({"operation": response})
                )
                with self.assertRaises(PaperExecutorProtocolError):
                    client.get_safe_flatten_status(operation_id)

        blocked = _safe_flatten_fields(
            operation_id=operation_id,
            state="blocked_outcome_unknown",
            outcome_unknown={
                "request_id": "7" * 32,
                "operation": "cancel_order",
                "phase": "canceling",
                "retry_allowed": False,
            },
        )
        parsed = PaperExecutorBrokerClient(  # type: ignore[arg-type]
            RecordingTransport({"operation": blocked})
        ).get_safe_flatten_status(operation_id)
        self.assertEqual(parsed.outcome_unknown.request_id, "7" * 32)

    def test_active_safe_flatten_discovery_accepts_only_null_or_valid_status(self) -> None:
        empty = PaperExecutorBrokerClient(  # type: ignore[arg-type]
            RecordingTransport({"operation": None})
        )
        self.assertIsNone(empty.get_active_safe_flatten())

        with self.assertRaises(PaperExecutorProtocolError):
            PaperExecutorBrokerClient(  # type: ignore[arg-type]
                RecordingTransport({"operation": []})
            ).get_active_safe_flatten()

    def test_submit_sends_only_the_closed_order_dto_and_a_stable_request_id(self) -> None:
        transport = RecordingTransport(
            _result_payload(),
            _result_payload(),
            automatic_health=True,
        )
        client = PaperExecutorBrokerClient(transport)  # type: ignore[arg-type]
        first = PaperOrder(
            symbol="SPY",
            side="buy",
            client_order_id="paper-spy-42",
            quantity=2.0,
            notional=None,
            estimated_position_weight=999.0,
            projected_gross_exposure=888.0,
            daily_pnl_pct=-777.0,
            current_drawdown_pct=666.0,
            reference_price=610.25,
            order_type="limit",
            limit_price=609.5,
            position_intent="open",
        )
        second = PaperOrder(
            symbol="SPY",
            side="buy",
            client_order_id="paper-spy-42",
            quantity=2.0,
            notional=None,
            estimated_position_weight=-1.0,
            projected_gross_exposure=-2.0,
            daily_pnl_pct=3.0,
            current_drawdown_pct=4.0,
            reference_price=610.25,
            order_type="limit",
            limit_price=609.5,
            position_intent="open",
        )

        result = client.submit_order(first)
        replay = client.submit_order(second)

        expected_id = deterministic_command_id(
            "submit_order",
            "client_order_id:paper-spy-42",
        )
        expected_order = {
            "symbol": "SPY",
            "side": "buy",
            "client_order_id": "paper-spy-42",
            "quantity": 2.0,
            "notional": None,
            "reference_price": 610.25,
            "order_type": "limit",
            "limit_price": 609.5,
            "position_intent": "open",
        }
        self.assertTrue(result.accepted)
        self.assertEqual(result.reasons, ())
        self.assertIsNone(result.broker_response)
        self.assertEqual(replay, result)
        mutation_calls = [call for call in transport.calls if call["operation"] == "submit_order"]
        self.assertEqual(len(mutation_calls), 2)
        for call in mutation_calls:
            self.assertEqual(call["operation"], "submit_order")
            self.assertEqual(call["payload"], {"order": expected_order})
            self.assertEqual(call["request_id"], expected_id)
            self.assertIsNone(call["timeout_seconds"])
            self.assertEqual(call["target"], _executor_target())
            transmitted = call["payload"]["order"]
            self.assertTrue(
                {
                    "estimated_position_weight",
                    "projected_gross_exposure",
                    "daily_pnl_pct",
                    "current_drawdown_pct",
                }.isdisjoint(transmitted)
            )

    def test_submit_receipt_correlates_one_successful_mutation_with_its_exact_target(
        self,
    ) -> None:
        transport = RecordingTransport(_result_payload(), automatic_health=True)
        client = PaperExecutorBrokerClient(transport)  # type: ignore[arg-type]

        receipt = client.submit_order_with_receipt(_minimal_order())

        expected_request_id = deterministic_command_id(
            "submit_order",
            "client_order_id:paper-spy-42",
        )
        self.assertEqual(receipt.request_id, expected_request_id)
        self.assertEqual(receipt.operation, "submit_order")
        self.assertEqual(receipt.target, _executor_target())
        self.assertEqual(receipt.outcome, "completed")
        self.assertIsInstance(receipt.result, PaperOrderResult)
        self.assertTrue(receipt.result.accepted)
        self.assertEqual(receipt.result.status, "submitted")
        self.assertEqual(
            [call["operation"] for call in transport.calls],
            ["health", "submit_order"],
        )
        mutation_call = transport.calls[-1]
        self.assertEqual(mutation_call["request_id"], receipt.request_id)
        self.assertEqual(mutation_call["target"], receipt.target)

    def test_cancel_receipt_correlates_one_rejection_with_its_exact_target(self) -> None:
        transport = RecordingTransport(
            {
                "accepted": False,
                "status": "rejected",
                "reasons": ["order_not_cancelable"],
                "dry_run": False,
            },
            automatic_health=True,
        )
        client = PaperExecutorBrokerClient(transport)  # type: ignore[arg-type]

        receipt = client.cancel_order_with_receipt(order_id="broker-order-9")

        expected_request_id = deterministic_command_id(
            "cancel_order",
            "order_id:broker-order-9",
        )
        self.assertEqual(receipt.request_id, expected_request_id)
        self.assertEqual(receipt.operation, "cancel_order")
        self.assertEqual(receipt.target, _executor_target())
        self.assertEqual(receipt.outcome, "rejected")
        self.assertIsInstance(receipt.result, PaperOrderResult)
        self.assertFalse(receipt.result.accepted)
        self.assertEqual(receipt.result.status, "rejected")
        self.assertEqual(receipt.result.reasons, ("order_not_cancelable",))
        self.assertEqual(
            [call["operation"] for call in transport.calls],
            ["health", "cancel_order"],
        )
        mutation_call = transport.calls[-1]
        self.assertEqual(mutation_call["request_id"], receipt.request_id)
        self.assertEqual(mutation_call["target"], receipt.target)

    def test_legacy_mutation_methods_return_results_without_duplicate_requests(self) -> None:
        transport = RecordingTransport(
            _result_payload(),
            _result_payload(status="cancel_requested"),
            automatic_health=True,
        )
        client = PaperExecutorBrokerClient(transport)  # type: ignore[arg-type]

        submitted = client.submit_order(_minimal_order())
        canceled = client.cancel_order("paper-spy-42")

        self.assertIsInstance(submitted, PaperOrderResult)
        self.assertIsInstance(canceled, PaperOrderResult)
        self.assertEqual(submitted.status, "submitted")
        self.assertEqual(canceled.status, "cancel_requested")
        self.assertEqual(
            [call["operation"] for call in transport.calls],
            ["health", "submit_order", "health", "cancel_order"],
        )
        self.assertEqual(
            len([call for call in transport.calls if call["operation"] == "submit_order"]),
            1,
        )
        self.assertEqual(
            len([call for call in transport.calls if call["operation"] == "cancel_order"]),
            1,
        )

    def test_cancel_requires_one_identifier_and_uses_deterministic_ids(self) -> None:
        transport = RecordingTransport(
            _result_payload(status="cancel_requested"),
            _result_payload(status="cancel_requested"),
            automatic_health=True,
        )
        client = PaperExecutorBrokerClient(transport)  # type: ignore[arg-type]

        client.cancel_order("paper-spy-42")
        client.cancel_order(order_id="paper-spy-42")

        self.assertEqual(
            [call for call in transport.calls if call["operation"] == "cancel_order"][0],
            {
                "operation": "cancel_order",
                "payload": {"order_id": None, "client_order_id": "paper-spy-42"},
                "request_id": deterministic_command_id(
                    "cancel_order",
                    "client_order_id:paper-spy-42",
                ),
                "timeout_seconds": None,
                "target": _executor_target(),
            },
        )
        self.assertEqual(
            [call for call in transport.calls if call["operation"] == "cancel_order"][1],
            {
                "operation": "cancel_order",
                "payload": {"order_id": "paper-spy-42", "client_order_id": None},
                "request_id": deterministic_command_id(
                    "cancel_order",
                    "order_id:paper-spy-42",
                ),
                "timeout_seconds": None,
                "target": _executor_target(),
            },
        )
        self.assertNotEqual(
            [call for call in transport.calls if call["operation"] == "cancel_order"][0]["request_id"],
            [call for call in transport.calls if call["operation"] == "cancel_order"][1]["request_id"],
        )
        with self.assertRaisesRegex(ValueError, "exactly one"):
            client.cancel_order()
        with self.assertRaisesRegex(ValueError, "exactly one"):
            client.cancel_order("paper-spy-42", order_id="broker-order-9")
        self.assertEqual(
            len([call for call in transport.calls if call["operation"] == "cancel_order"]),
            2,
        )

    def test_kill_switch_uses_closed_dto_and_cannot_be_reset_online(self) -> None:
        transport = RecordingTransport(_kill_switch_payload(), automatic_health=True)
        client = PaperExecutorBrokerClient(transport)  # type: ignore[arg-type]

        self.assertIsNone(client.activate_kill_switch("daily_loss_limit"))
        self.assertEqual(
            [call for call in transport.calls if call["operation"] == "latch_kill_switch"],
            [
                {
                    "operation": "latch_kill_switch",
                    "payload": {"reason_code": "daily_loss_limit"},
                    "request_id": deterministic_command_id(
                        "latch_kill_switch",
                        "reason_code:daily_loss_limit",
                    ),
                    "timeout_seconds": None,
                    "target": _executor_target(),
                }
            ],
        )
        with self.assertRaisesRegex(RuntimeError, "cannot be reset"):
            client.reset_kill_switch()
        with self.assertRaisesRegex(RuntimeError, "evidence is verified"):
            client.mark_order_reconciled("paper-spy-42")
        self.assertEqual(
            len([call for call in transport.calls if call["operation"] == "latch_kill_switch"]),
            1,
        )

    def test_kill_switch_rejects_incomplete_false_and_malformed_acknowledgements(self) -> None:
        valid = _kill_switch_payload()
        variants: dict[str, dict[str, Any]] = {
            "missing": {"kill_switch_active": True, "generation": 1},
            "extra": {**valid, "reset_allowed": False},
            "not_latched": {**valid, "kill_switch_active": False},
            "active_integer": {**valid, "kill_switch_active": 1},
            "zero_generation": {**valid, "generation": 0},
            "negative_generation": {**valid, "generation": -1},
            "boolean_generation": {**valid, "generation": True},
            "string_generation": {**valid, "generation": "1"},
            "empty_reason": {**valid, "reason_code": ""},
            "control_reason": {**valid, "reason_code": "daily_loss_limit\r"},
            "different_reason": {**valid, "reason_code": "manual_operator_stop"},
        }
        for label, response in variants.items():
            with self.subTest(label=label):
                client = PaperExecutorBrokerClient(  # type: ignore[arg-type]
                    RecordingTransport(response, automatic_health=True)
                )
                with self.assertRaises(PaperExecutorProtocolError):
                    client.activate_kill_switch("daily_loss_limit")

    def test_read_account_parses_exact_finite_fields(self) -> None:
        transport = RecordingTransport(_account_payload())
        account = PaperExecutorBrokerClient(transport).read_account()  # type: ignore[arg-type]

        self.assertEqual(account.account_id, "paper-account-1")
        self.assertEqual(account.status, "ACTIVE")
        self.assertEqual(account.cash, 1_000.0)
        self.assertEqual(account.equity, 1_100.0)
        self.assertEqual(account.buying_power, 2_000.0)
        self.assertEqual(account.last_equity, 1_050.0)
        self.assertEqual(transport.calls[0]["operation"], "read_account")
        self.assertEqual(transport.calls[0]["payload"], {})

    def test_read_account_rejects_missing_extra_nonfinite_and_bool_fields(self) -> None:
        valid = _account_fields()
        variants: dict[str, dict[str, Any]] = {
            "missing": {key: value for key, value in valid.items() if key != "cash"},
            "extra": {**valid, "currency": "USD"},
            "nan": {**valid, "equity": math.nan},
            "infinity": {**valid, "buying_power": math.inf},
            "boolean": {**valid, "last_equity": True},
            "empty_text": {**valid, "account_id": ""},
            "control_text": {**valid, "status": "ACTIVE\n"},
        }
        for label, fields in variants.items():
            with self.subTest(label=label):
                client = PaperExecutorBrokerClient(  # type: ignore[arg-type]
                    RecordingTransport({"account": fields})
                )
                with self.assertRaises(PaperExecutorProtocolError):
                    client.read_account()

    def test_positions_parse_strictly_and_local_reconciliation_never_sends_a_mutation(self) -> None:
        transport = RecordingTransport(
            {"positions": [_position_fields()]},
            {"positions": [_position_fields(quantity=3.0), _position_fields(symbol="QQQ")]},
        )
        client = PaperExecutorBrokerClient(transport)  # type: ignore[arg-type]

        positions = client.read_positions()
        report = client.reconcile_positions(
            (
                PaperPosition(
                    symbol="spy",
                    quantity=2.0,
                    market_value=1_200.0,
                    avg_entry_price=590.0,
                    current_price=600.0,
                ),
            )
        )

        self.assertEqual(len(positions), 1)
        self.assertEqual(positions[0].symbol, "SPY")
        self.assertEqual(positions[0].quantity, 2.0)
        self.assertIsNone(positions[0].unrealized_pl)
        self.assertFalse(report.matched)
        self.assertEqual(
            report.differences,
            (
                "quantity_mismatch: SPY expected=2.0 broker=3.0",
                "unexpected_broker_position: QQQ",
            ),
        )
        self.assertEqual(
            [call["operation"] for call in transport.calls],
            ["read_positions", "read_positions"],
        )
        self.assertTrue(all(call["request_id"] is None for call in transport.calls))

    def test_positions_reject_non_object_exact_field_and_numeric_violations(self) -> None:
        valid = _position_fields()
        variants: dict[str, Any] = {
            "non_object": "SPY",
            "missing": {key: value for key, value in valid.items() if key != "quantity"},
            "extra": {**valid, "asset_class": "us_equity"},
            "nonfinite": {**valid, "market_value": -math.inf},
            "bool": {**valid, "quantity": False},
            "empty_symbol": {**valid, "symbol": ""},
        }
        for label, value in variants.items():
            with self.subTest(label=label):
                client = PaperExecutorBrokerClient(  # type: ignore[arg-type]
                    RecordingTransport({"positions": [value]})
                )
                with self.assertRaises(PaperExecutorProtocolError):
                    client.read_positions()

    def test_order_reads_use_closed_identifiers_and_parse_all_fields(self) -> None:
        transport = RecordingTransport(
            {"orders": [_order_fields()]},
            {"order": _order_fields()},
            {"order": _order_fields()},
        )
        client = PaperExecutorBrokerClient(transport)  # type: ignore[arg-type]

        listed = client.list_orders(status="closed")
        by_order_id = client.get_order(order_id="broker-order-9")
        by_client_id = client.get_order_by_client_id("paper-spy-42")

        self.assertEqual(listed, (by_order_id,))
        self.assertEqual(by_order_id, by_client_id)
        self.assertEqual(by_order_id.order_id, "broker-order-9")
        self.assertEqual(by_order_id.limit_price, 609.5)
        self.assertEqual(by_order_id.filled_quantity, 1.0)
        self.assertIsNone(by_order_id.realized_pnl)
        self.assertEqual(
            [(call["operation"], call["payload"]) for call in transport.calls],
            [
                ("list_orders", {"status": "closed"}),
                (
                    "get_order",
                    {"order_id": "broker-order-9", "client_order_id": None},
                ),
                (
                    "get_order",
                    {"order_id": None, "client_order_id": "paper-spy-42"},
                ),
            ],
        )

    def test_get_order_rejects_responses_bound_to_other_identifiers(self) -> None:
        by_order_id = PaperExecutorBrokerClient(  # type: ignore[arg-type]
            RecordingTransport({"order": _order_fields(order_id="different-broker-order")})
        )
        with self.assertRaisesRegex(PaperExecutorProtocolError, "different order_id"):
            by_order_id.get_order(order_id="broker-order-9")

        by_client_id = PaperExecutorBrokerClient(  # type: ignore[arg-type]
            RecordingTransport({"order": _order_fields(client_order_id="different-client-order")})
        )
        with self.assertRaisesRegex(
            PaperExecutorProtocolError,
            "different client_order_id",
        ):
            by_client_id.get_order_by_client_id("paper-spy-42")

    def test_orders_reject_non_object_missing_extra_invalid_text_and_nonfinite_numbers(self) -> None:
        valid = _order_fields()
        variants: dict[str, Any] = {
            "non_object": [],
            "missing": {key: value for key, value in valid.items() if key != "status"},
            "extra": {**valid, "raw": {}},
            "empty_id": {**valid, "order_id": ""},
            "control_status": {**valid, "status": "filled\r"},
            "nonfinite": {**valid, "filled_quantity": math.nan},
            "nonfinite_realized_pnl": {**valid, "realized_pnl": math.inf},
            "bool_numeric": {**valid, "limit_price": True},
        }
        for label, value in variants.items():
            with self.subTest(label=label):
                client = PaperExecutorBrokerClient(  # type: ignore[arg-type]
                    RecordingTransport({"orders": [value]})
                )
                with self.assertRaises(PaperExecutorProtocolError):
                    client.list_orders()

    def test_fill_activity_request_uses_iso8601_bounds_and_strictly_parses(self) -> None:
        transport = RecordingTransport({"fills": [_fill_fields()]})
        client = PaperExecutorBrokerClient(transport)  # type: ignore[arg-type]
        after = datetime(2026, 7, 15, 13, 0, tzinfo=UTC)
        until = datetime(2026, 7, 15, 14, 0, tzinfo=UTC)

        fills = client.list_fill_activities(after=after, until=until)

        self.assertEqual(len(fills), 1)
        self.assertEqual(fills[0].activity_id, "fill-1")
        self.assertEqual(fills[0].quantity, 1.0)
        self.assertEqual(
            transport.calls[0]["payload"],
            {"after": "2026-07-15T13:00:00+00:00", "until": "2026-07-15T14:00:00+00:00"},
        )

    def test_fill_activities_reject_wrong_shape_and_invalid_values(self) -> None:
        valid = _fill_fields()
        variants: dict[str, Any] = {
            "non_object": None,
            "missing": {key: value for key, value in valid.items() if key != "price"},
            "extra": {**valid, "net_amount": 610.0},
            "empty_activity": {**valid, "activity_id": ""},
            "nonfinite": {**valid, "price": math.inf},
            "bool_quantity": {**valid, "quantity": True},
        }
        for label, value in variants.items():
            with self.subTest(label=label):
                client = PaperExecutorBrokerClient(  # type: ignore[arg-type]
                    RecordingTransport({"fills": [value]})
                )
                with self.assertRaises(PaperExecutorProtocolError):
                    client.list_fill_activities(
                        after=datetime(2026, 7, 15, tzinfo=UTC),
                        until=datetime(2026, 7, 16, tzinfo=UTC),
                    )

    def test_latest_trade_price_accepts_none_or_finite_number_and_rejects_other_shapes(self) -> None:
        transport = RecordingTransport(
            {"symbol": "SPY", "price": None},
            {"symbol": "SPY", "price": 610},
        )
        client = PaperExecutorBrokerClient(transport)  # type: ignore[arg-type]

        self.assertIsNone(client.latest_trade_price("SPY"))
        self.assertEqual(client.latest_trade_price("SPY"), 610.0)
        self.assertEqual(
            [call["payload"] for call in transport.calls],
            [{"symbol": "SPY"}, {"symbol": "SPY"}],
        )

        invalid = (
            {"price": 610.0},
            {"symbol": "SPY", "price": 610.0, "feed": "sip"},
            {"symbol": "SPY", "price": math.nan},
            {"symbol": "SPY", "price": False},
        )
        for response in invalid:
            with self.subTest(response=response):
                invalid_client = PaperExecutorBrokerClient(  # type: ignore[arg-type]
                    RecordingTransport(response)
                )
                with self.assertRaises(PaperExecutorProtocolError):
                    invalid_client.latest_trade_price("SPY")

    def test_latest_trade_price_binds_the_response_to_the_requested_symbol(self) -> None:
        transport = RecordingTransport({"symbol": "SPY", "price": 610.0})
        client = PaperExecutorBrokerClient(transport)  # type: ignore[arg-type]

        self.assertEqual(client.latest_trade_price("spy"), 610.0)
        self.assertEqual(transport.calls[0]["payload"], {"symbol": "spy"})

        invalid_responses = (
            {"symbol": "QQQ", "price": 610.0},
            {"symbol": "spy", "price": 610.0},
            {"symbol": "", "price": 610.0},
            {"symbol": "SPY\n", "price": 610.0},
        )
        for response in invalid_responses:
            with self.subTest(response=response):
                mismatched = PaperExecutorBrokerClient(  # type: ignore[arg-type]
                    RecordingTransport(response)
                )
                with self.assertRaises(PaperExecutorProtocolError):
                    mismatched.latest_trade_price("SPY")

    def test_order_results_require_exact_boolean_text_and_reason_types(self) -> None:
        variants: dict[str, dict[str, Any]] = {
            "missing": {"accepted": True, "status": "submitted", "reasons": []},
            "extra": {**_result_payload(), "broker_response": {}},
            "accepted_int": {**_result_payload(), "accepted": 1},
            "dry_run_int": {**_result_payload(), "dry_run": 0},
            "reasons_tuple": {**_result_payload(), "reasons": ("reason",)},
            "empty_status": {**_result_payload(), "status": ""},
            "invalid_reason": {**_result_payload(), "reasons": ["bad\nreason"]},
        }
        for label, response in variants.items():
            with self.subTest(label=label):
                client = PaperExecutorBrokerClient(  # type: ignore[arg-type]
                    RecordingTransport(response)
                )
                with self.assertRaises(PaperExecutorProtocolError):
                    client.submit_order(_minimal_order())


def _minimal_order() -> PaperOrder:
    return PaperOrder(
        symbol="SPY",
        side="buy",
        client_order_id="paper-spy-42",
        quantity=1.0,
        notional=None,
        reference_price=610.0,
    )


def _result_payload(*, status: str = "submitted") -> dict[str, Any]:
    return {"accepted": True, "status": status, "reasons": [], "dry_run": False}


def _health_payload() -> dict[str, Any]:
    return {
        "status": "ready",
        "mutations_allowed": True,
        "opening_orders_allowed": True,
        "capability_mode": "full",
        "account_scope_sha256": "a" * 64,
        "fence_epoch": 7,
        "policy_sha256": "b" * 64,
        "authz_policy_sha256": "c" * 64,
        "run_id": "1" * 32,
        "pending_recovery": 0,
        "kill_switch_active": False,
    }


def _safe_flatten_fields(
    *,
    operation_id: str,
    state: str = "latched",
    outcome_unknown: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "operation_id": operation_id,
        "account_scope_sha256": "a" * 64,
        "state": state,
        "state_version": 1,
        "terminal": state in {"failed_latched", "flat_latched"},
        "reconciled": state == "flat_latched",
        "kill_switch_active": True,
        "retry_allowed": False,
        "failure_code": "workflow_failed" if state == "failed_latched" else None,
        "outcome_unknown": outcome_unknown,
        "started_at": "2026-07-15T13:00:00Z",
        "updated_at": "2026-07-15T13:01:00Z",
    }


def _executor_target() -> ExecutorTarget:
    health = _health_payload()
    return ExecutorTarget(
        account_scope_sha256=str(health["account_scope_sha256"]),
        policy_sha256=str(health["policy_sha256"]),
        authz_policy_sha256=str(health["authz_policy_sha256"]),
        run_id=str(health["run_id"]),
        fence_epoch=int(health["fence_epoch"]),
    )


def _kill_switch_payload() -> dict[str, Any]:
    return {
        "kill_switch_active": True,
        "generation": 1,
        "reason_code": "daily_loss_limit",
    }


def _account_fields() -> dict[str, Any]:
    return {
        "account_id": "paper-account-1",
        "status": "ACTIVE",
        "cash": 1_000,
        "equity": 1_100.0,
        "buying_power": 2_000,
        "last_equity": 1_050.0,
    }


def _account_payload() -> dict[str, Any]:
    return {"account": _account_fields()}


def _position_fields(**overrides: Any) -> dict[str, Any]:
    value = {
        "symbol": "SPY",
        "quantity": 2.0,
        "market_value": 1_200.0,
        "avg_entry_price": 590.0,
        "current_price": 600.0,
        "unrealized_pl": None,
        "unrealized_plpc": None,
    }
    value.update(overrides)
    return value


def _order_fields(**overrides: Any) -> dict[str, Any]:
    value = {
        "order_id": "broker-order-9",
        "client_order_id": "paper-spy-42",
        "symbol": "SPY",
        "side": "buy",
        "order_type": "limit",
        "time_in_force": "day",
        "status": "partially_filled",
        "notional": None,
        "quantity": 2.0,
        "filled_quantity": 1.0,
        "filled_avg_price": 609.25,
        "submitted_at": "2026-07-15T13:00:00Z",
        "created_at": "2026-07-15T13:00:00Z",
        "updated_at": "2026-07-15T13:01:00Z",
        "expires_at": "2026-07-15T20:00:00Z",
        "stop_price": None,
        "limit_price": 609.5,
        "filled_at": "",
        "realized_pnl": None,
    }
    value.update(overrides)
    return value


def _fill_fields(**overrides: Any) -> dict[str, Any]:
    value = {
        "activity_id": "fill-1",
        "order_id": "broker-order-9",
        "symbol": "SPY",
        "side": "buy",
        "quantity": 1.0,
        "price": 609.25,
        "transaction_time": "2026-07-15T13:01:00Z",
        "cumulative_quantity": 1.0,
        "leaves_quantity": 1.0,
        "activity_type": "FILL",
        "order_status": "partially_filled",
    }
    value.update(overrides)
    return value


if __name__ == "__main__":
    unittest.main()
