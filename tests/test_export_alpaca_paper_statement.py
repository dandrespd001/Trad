from __future__ import annotations

import csv
import io
import importlib.util
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any
from unittest import mock


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
        calls: list[str] = []

        def fake_get_json(url: str, *, env: dict[str, str], params: dict[str, str]) -> object:
            calls.append(url)
            self.assertEqual(env["ALPACA_PAPER_API_KEY"], "KEY")
            self.assertEqual(env["ALPACA_PAPER_SECRET_KEY"], "SECRET")
            self.assertEqual(params, {})
            return order

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            output = root / "statement.csv"
            exit_code = run_main(
                module,
                [
                    "--env-file",
                    str(root / "missing.env"),
                    "--client-order-id",
                    "signal-xlk-20260618",
                    "--order-id",
                    "broker-order-1",
                    "--as-of-date",
                    "2026-06-23",
                    "--output",
                    str(output),
                ],
                fake_get_json=fake_get_json,
            )
            with output.open("r", encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))

        self.assertEqual(exit_code, 0)
        self.assertEqual(calls, [f"{module.PAPER_BASE_URL}/orders/broker-order-1"])
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
                    "--env-file",
                    str(root / "missing.env"),
                    "--client-order-id",
                    "signal-xlk-20260618",
                    "--order-id",
                    "broker-order-1",
                    "--as-of-date",
                    "2026-06-23",
                    "--output",
                    str(output),
                ],
                fake_get_json=lambda *_args, **_kwargs: order,
            )
            with output.open("r", encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))

        self.assertEqual(exit_code, 0)
        self.assertEqual(rows[0]["realized_pnl"], "0.0")
        self.assertEqual(rows[0]["source"], "alpaca_paper_orders_api_realized_pnl_unavailable")

    def test_missing_realized_pnl_for_sell_is_not_defaulted(self) -> None:
        module = load_exporter()
        order = broker_order(realized_pnl=None)
        order["side"] = "sell"

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            output = root / "statement.csv"
            exit_code = run_main(
                module,
                [
                    "--env-file",
                    str(root / "missing.env"),
                    "--client-order-id",
                    "signal-xlk-20260618",
                    "--order-id",
                    "broker-order-1",
                    "--as-of-date",
                    "2026-06-23",
                    "--output",
                    str(output),
                ],
                fake_get_json=lambda *_args, **_kwargs: order,
            )

        self.assertEqual(exit_code, 1)
        self.assertFalse(output.exists())


def run_main(module: Any, argv: list[str], *, fake_get_json: Any) -> int:
    env = {
        "ALPACA_PAPER_API_KEY": "KEY",
        "ALPACA_PAPER_SECRET_KEY": "SECRET",
    }
    with (
        mock.patch.dict(os.environ, env, clear=True),
        mock.patch.object(module, "_get_json", side_effect=fake_get_json),
        mock.patch.object(sys, "argv", ["export-alpaca-paper-statement.py", *argv]),
        redirect_stdout(io.StringIO()),
        redirect_stderr(io.StringIO()),
    ):
        return int(module.main())


def broker_order(*, realized_pnl: str | None) -> dict[str, object]:
    payload: dict[str, object] = {
        "id": "broker-order-1",
        "client_order_id": "signal-xlk-20260618",
        "symbol": "XLK",
        "side": "buy",
        "filled_qty": "0.005327048",
        "filled_avg_price": "185.844",
        "filled_at": "2026-06-23T17:30:46.290044+00:00",
        "status": "filled",
    }
    if realized_pnl is not None:
        payload["realized_pnl"] = realized_pnl
    return payload


if __name__ == "__main__":
    unittest.main()
