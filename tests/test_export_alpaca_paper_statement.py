from __future__ import annotations

import csv
import importlib.util
import io
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest import mock

from trading_ai.execution.alpaca_paper import PaperOrderSnapshot

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "export-alpaca-paper-statement.py"


def load_exporter() -> Any:
    spec = importlib.util.spec_from_file_location("export_alpaca_paper_statement", SCRIPT_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load export-alpaca-paper-statement.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ExportAlpacaPaperStatementTests(unittest.TestCase):
    def test_order_id_lookup_exports_matching_broker_statement(self) -> None:
        module = load_exporter()
        order = broker_order(realized_pnl="0.13")
        broker = FakeBroker(order)

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            output = root / "statement.csv"
            exit_code = run_main(
                module,
                [
                    "--client-order-id",
                    "signal-xlk-20260618",
                    "--order-id",
                    "broker-order-1",
                    "--as-of-date",
                    "2026-06-23",
                    "--output",
                    str(output),
                ],
                broker=broker,
            )
            with output.open("r", encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))

        self.assertEqual(exit_code, 0)
        self.assertEqual(broker.calls, [("get_order", "broker-order-1")])
        self.assertEqual(rows[0]["client_order_id"], "signal-xlk-20260618")
        self.assertEqual(rows[0]["realized_pnl"], "0.13")

    def test_missing_realized_pnl_exports_zero_with_explicit_source(self) -> None:
        module = load_exporter()
        order = broker_order(realized_pnl=None)

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            output = root / "statement.csv"
            exit_code = run_main(
                module,
                [
                    "--client-order-id",
                    "signal-xlk-20260618",
                    "--order-id",
                    "broker-order-1",
                    "--as-of-date",
                    "2026-06-23",
                    "--output",
                    str(output),
                ],
                broker=FakeBroker(order),
            )
            with output.open("r", encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))

        self.assertEqual(exit_code, 0)
        self.assertEqual(rows[0]["realized_pnl"], "0.0")
        self.assertEqual(rows[0]["source"], "alpaca_paper_executor_realized_pnl_unavailable")

    def test_missing_realized_pnl_for_sell_is_not_defaulted(self) -> None:
        module = load_exporter()
        order = replace(broker_order(realized_pnl=None), side="sell")

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            output = root / "statement.csv"
            exit_code = run_main(
                module,
                [
                    "--client-order-id",
                    "signal-xlk-20260618",
                    "--order-id",
                    "broker-order-1",
                    "--as-of-date",
                    "2026-06-23",
                    "--output",
                    str(output),
                ],
                broker=FakeBroker(order),
            )

        self.assertEqual(exit_code, 1)
        self.assertFalse(output.exists())

    def test_source_contains_no_broker_credentials_http_or_env_fallback(self) -> None:
        source = SCRIPT_PATH.read_text(encoding="utf-8")
        for forbidden in (
            "ALPACA_PAPER_API_KEY",
            "ALPACA_PAPER_SECRET_KEY",
            "--env-file",
            "urlopen",
            "paper-api.alpaca.markets",
            "APCA-API-KEY-ID",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, source)
        self.assertIn("PaperExecutorBrokerClient", source)


def run_main(module: Any, argv: list[str], *, broker: Any) -> int:
    with (
        mock.patch.object(module, "PaperExecutorBrokerClient", return_value=broker),
        mock.patch.object(sys, "argv", ["export-alpaca-paper-statement.py", *argv]),
        redirect_stdout(io.StringIO()),
        redirect_stderr(io.StringIO()),
    ):
        return int(module.main())


def broker_order(*, realized_pnl: str | None) -> PaperOrderSnapshot:
    return PaperOrderSnapshot(
        order_id="broker-order-1",
        client_order_id="signal-xlk-20260618",
        symbol="XLK",
        side="buy",
        order_type="market",
        time_in_force="day",
        status="filled",
        notional=None,
        quantity=0.005327048,
        filled_quantity=0.005327048,
        filled_avg_price=185.844,
        submitted_at="2026-06-23T17:30:45Z",
        created_at="2026-06-23T17:30:45Z",
        updated_at="2026-06-23T17:30:46Z",
        expires_at="",
        filled_at="2026-06-23T17:30:46.290044+00:00",
        realized_pnl=None if realized_pnl is None else float(realized_pnl),
    )


class FakeBroker:
    def __init__(self, order: PaperOrderSnapshot) -> None:
        self.order = order
        self.calls: list[tuple[str, str]] = []

    def get_order(self, *, order_id: str) -> PaperOrderSnapshot:
        self.calls.append(("get_order", order_id))
        return self.order

    def list_orders(self, *, status: str = "open") -> tuple[PaperOrderSnapshot, ...]:
        self.calls.append(("list_orders", status))
        return (self.order,)

    @staticmethod
    def list_fill_activities(
        *,
        after: datetime,
        until: datetime,
    ) -> tuple[object, ...]:
        del after, until
        return ()


if __name__ == "__main__":
    unittest.main()
