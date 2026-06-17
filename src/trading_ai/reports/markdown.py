"""Markdown reports for deterministic backtests."""

from __future__ import annotations

from trading_ai.backtest.engine import BacktestResult


METRIC_LABELS = (
    ("cumulative_return", "Cumulative return"),
    ("cagr", "CAGR"),
    ("sharpe", "Sharpe"),
    ("sortino", "Sortino"),
    ("max_drawdown", "Max drawdown"),
    ("turnover", "Turnover"),
    ("trade_count", "Trade count"),
    ("average_exposure", "Average exposure"),
    ("estimated_costs", "Estimated costs"),
)


def render_backtest_report(result: BacktestResult, *, title: str = "Backtest Report") -> str:
    lines = [
        f"# {title}",
        "",
        "## Metrics",
        "",
        "| Metric | Value |",
        "| --- | ---: |",
    ]
    for key, label in METRIC_LABELS:
        lines.append(f"| {label} | {_format_metric(result.metrics.get(key, 0.0))} |")
    if result.metadata:
        lines.extend(["", "## Dataset", ""])
        if "dataset_id" in result.metadata:
            lines.append(f"- Dataset id: `{result.metadata['dataset_id']}`")
        if "frequency" in result.metadata:
            lines.append(f"- Frequency: `{result.metadata['frequency']}`")
        if "dataset_path" in result.metadata:
            lines.append(f"- Dataset path: `{result.metadata['dataset_path']}`")
        if "dataset_hash" in result.metadata:
            lines.append(f"- Dataset hash: `{result.metadata['dataset_hash']}`")
        if "source_sha256" in result.metadata:
            lines.append(f"- Source SHA-256: `{result.metadata['source_sha256']}`")
        if "row_count" in result.metadata:
            lines.append(f"- Row count: {result.metadata['row_count']}")
        if "start" in result.metadata and "end" in result.metadata:
            lines.append(f"- Date range: {result.metadata['start']} to {result.metadata['end']}")
        if "symbols" in result.metadata:
            lines.append(f"- Symbols: {', '.join(str(symbol) for symbol in result.metadata['symbols'])}")
    lines.extend(
        [
            "",
            "## Configuration",
            "",
            f"- Momentum window: {result.config.momentum_window}",
            f"- Volatility window: {result.config.volatility_window}",
            f"- Target annual volatility: {result.config.target_annual_volatility:.2%}",
            f"- Max gross exposure: {result.config.max_gross_exposure:.2%}",
            f"- Max single position: {result.config.max_single_position:.2%}",
            f"- Cost + slippage bps: {result.config.cost_bps + result.config.slippage_bps:.2f}",
            "",
            "## Risk Notes",
            "",
            "- This report is generated from a deterministic research backtest.",
            "- It does not authorize live trading or bypass risk gates.",
        ]
    )
    return "\n".join(lines) + "\n"


def _format_metric(value: float) -> str:
    if abs(value) < 10 and not float(value).is_integer():
        return f"{value:.6f}"
    return f"{value:.2f}"
