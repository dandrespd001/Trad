"""Shared pytest fixtures for the trading-ai test suite."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest


@pytest.fixture()
def tmp_reports_dir(tmp_path: Path) -> Path:
    """Temporary directory tree mirroring the reports/ structure."""
    (tmp_path / "reports" / "paper").mkdir(parents=True)
    (tmp_path / "reports" / "registry").mkdir(parents=True)
    (tmp_path / "reports" / "tmp").mkdir(parents=True)
    (tmp_path / "data" / "raw" / "approved" / "core_etfs" / "1d").mkdir(parents=True)
    return tmp_path


@pytest.fixture()
def sample_ohlcv_df() -> Any:
    """120-row synthetic OHLCV DataFrame for SPY (daily, realistic prices).

    Requires the 'research' extras: pip install -e '.[research]'
    """
    pd = pytest.importorskip("pandas")
    np = pytest.importorskip("numpy")

    rng = np.random.default_rng(42)
    n = 120
    dates = pd.date_range("2023-01-03", periods=n, freq="B")
    close = 420.0 + np.cumsum(rng.normal(0, 1.5, n))
    high = close + rng.uniform(1, 5, n)
    low = close - rng.uniform(1, 5, n)
    open_ = close + rng.normal(0, 0.5, n)
    volume = rng.integers(40_000_000, 60_000_000, n).astype(float)
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": volume},
        index=dates,
    )


@pytest.fixture()
def dry_run_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Inject dry-run paper trading environment variables."""
    monkeypatch.setenv("ALPACA_PAPER_API_KEY", "TEST_KEY")
    monkeypatch.setenv("ALPACA_PAPER_SECRET_KEY", "TEST_SECRET")
    monkeypatch.setenv("PAPER_DRY_RUN", "true")


@pytest.fixture()
def minimal_risk_config() -> dict:
    """Minimal valid RiskLimits configuration dict."""
    return {
        "max_daily_loss_pct": 2.0,
        "max_drawdown_pct": 10.0,
        "max_single_position": 0.30,
        "max_gross_exposure": 1.0,
        "max_consecutive_error_days": 0,
    }
