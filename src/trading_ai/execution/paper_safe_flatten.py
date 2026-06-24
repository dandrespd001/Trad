"""Emergency safe-mode flatten for Alpaca paper positions.

Closes every allowlisted open position with market sell orders. Intended as the
operator response when the kill-switch trips. Sells bypass the per-order
daily-loss/drawdown gates by design (de-risking must never be trapped), and the
in-memory broker kill-switch is intentionally left inactive so the exits can run.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from trading_ai.config import load_risk_config, load_universe_config
from trading_ai.execution.alpaca_connection import build_alpaca_paper_client
from trading_ai.execution.alpaca_paper import AlpacaPaperBroker, PaperOrder
from trading_ai.execution.paper_common import (
    PAPER_ERROR,
    PAPER_OK,
    PAPER_WARN,
    paper_exit_code,
    redact_secrets,
    write_json_artifact,
    write_text_artifact,
)
from trading_ai.execution.paper_position_plan import dynamic_client_order_id
from trading_ai.execution.paper_risk_state import (
    DEFAULT_RISK_STATE_PATH,
    load_risk_state,
    reset_kill_switch,
    save_risk_state,
)

SCHEMA_VERSION = "1.0"
DEFAULT_OUTPUT = "reports/tmp/paper_safe_flatten/latest.json"
DEFAULT_MARKDOWN_OUTPUT = "reports/tmp/paper_safe_flatten/latest.md"


class PaperSafeFlattenOperationalError(RuntimeError):
    """Raised when the safe-flatten cannot run safely."""


@dataclass(frozen=True)
class PaperSafeFlattenResult:
    exit_code: int
    status: str
    output_path: Path
    markdown_path: Path
    payload: dict[str, object]


def run_paper_safe_flatten(
    *,
    confirm_paper: bool,
    confirm_flatten: bool,
    config: str | Path = "configs/universe.yml",
    risk: str | Path = "configs/risk.yml",
    reset_kill_switch_after: bool = False,
    as_of_date: str = "today",
    risk_state_path: str | Path = DEFAULT_RISK_STATE_PATH,
    output: str | Path = DEFAULT_OUTPUT,
    markdown_output: str | Path = DEFAULT_MARKDOWN_OUTPUT,
) -> PaperSafeFlattenResult:
    if not confirm_paper or not confirm_flatten:
        missing = []
        if not confirm_paper:
            missing.append("--confirm-paper")
        if not confirm_flatten:
            missing.append("--confirm-flatten")
        raise PaperSafeFlattenOperationalError("paper safe flatten requires " + " and ".join(missing))

    output_path = Path(output)
    markdown_path = Path(markdown_output)
    compact_date = datetime.now(UTC).date().isoformat() if as_of_date == "today" else as_of_date

    try:
        universe = load_universe_config(config)
        risk_limits = load_risk_config(risk)
        client = build_alpaca_paper_client()
        broker = AlpacaPaperBroker(client=client, allowlist=universe.symbols, risk_limits=risk_limits, dry_run=False)
        positions = broker.read_positions()
        results: list[dict[str, object]] = []
        for position in positions:
            if position.quantity <= 0:
                continue
            order = PaperOrder(
                symbol=position.symbol,
                side="sell",
                quantity=position.quantity,
                client_order_id=dynamic_client_order_id(
                    prefix="flatten", symbol=position.symbol, as_of_date=compact_date
                ),
            )
            broker_result = broker.submit_order(order)
            results.append(
                {
                    "symbol": position.symbol,
                    "quantity": position.quantity,
                    "accepted": broker_result.accepted,
                    "status": broker_result.status,
                    "reasons": list(broker_result.reasons),
                }
            )
    except Exception as exc:  # broker boundary must always leave a redacted artifact
        payload = _error_payload(reason=redact_secrets(str(exc)))
        write_json_artifact(payload, output_path)
        write_text_artifact(_render_markdown(payload), markdown_path)
        return PaperSafeFlattenResult(
            exit_code=paper_exit_code(PAPER_ERROR),
            status=PAPER_ERROR,
            output_path=output_path,
            markdown_path=markdown_path,
            payload=payload,
        )

    unflattened = [item for item in results if item["accepted"] is not True]
    kill_switch_reset = False
    if reset_kill_switch_after and not unflattened:
        save_risk_state(reset_kill_switch(load_risk_state(risk_state_path)), risk_state_path)
        kill_switch_reset = True

    status = PAPER_OK if results and not unflattened else PAPER_WARN if not results else PAPER_ERROR
    payload = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(UTC).isoformat(),
        "status": status,
        "mode": "real-paper",
        "broker": "alpaca",
        "flattened": results,
        "flatten_count": len(results),
        "unflattened_count": len(unflattened),
        "kill_switch_reset": kill_switch_reset,
        "confirmations": {"confirm_paper": confirm_paper, "confirm_flatten": confirm_flatten},
        "safety": {
            "paper_only": True,
            "live_trading_authorized": False,
            "live_trading_allowed": False,
        },
    }
    write_json_artifact(payload, output_path)
    write_text_artifact(_render_markdown(payload), markdown_path)
    return PaperSafeFlattenResult(
        exit_code=paper_exit_code(status),
        status=status,
        output_path=output_path,
        markdown_path=markdown_path,
        payload=payload,
    )


def _error_payload(*, reason: str) -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(UTC).isoformat(),
        "status": PAPER_ERROR,
        "mode": "real-paper",
        "broker": "alpaca",
        "reason": reason,
        "flattened": [],
        "flatten_count": 0,
        "unflattened_count": 0,
        "kill_switch_reset": False,
        "safety": {"paper_only": True, "live_trading_authorized": False, "live_trading_allowed": False},
    }


def _render_markdown(payload: dict[str, object]) -> str:
    flattened = payload.get("flattened")
    rows = flattened if isinstance(flattened, list) else []
    lines = [
        "# Paper Safe Flatten",
        "",
        f"Status: **{payload.get('status') or PAPER_ERROR}**",
        f"Generated at: `{payload.get('generated_at') or ''}`",
        f"Kill switch reset: `{payload.get('kill_switch_reset')}`",
        "",
        "| Symbol | Quantity | Accepted | Status |",
        "| --- | --- | --- | --- |",
    ]
    if not rows:
        lines.append("| none | 0 | - | no_open_positions |")
    for row in rows:
        if isinstance(row, dict):
            lines.append(
                f"| `{row.get('symbol') or ''}` | `{row.get('quantity')}` | "
                f"`{row.get('accepted')}` | `{row.get('status') or ''}` |"
            )
    lines.extend(["", "Live trading authorized: `False`", ""])
    return "\n".join(lines)
