# Futures Micro Platform Decision

Status: research-only.

Decision: use LEAN plus IBKR only for future offline/research scaffolding if futures work proceeds. Alpaca remains the only operational paper broker in this repository.

Alternatives considered:

- `LEAN_IBKR_RESEARCH_ONLY`: supports MES/MNQ research with calendars, roll logic, tick values, margin placeholders, sessions, and costs before any broker execution work.
- `ALPACA_ONLY`: keeps the current operational paper workflow unchanged, but does not cover CME micro futures.
- `DEFER`: keeps futures out of scope until paper equity evidence is stronger.

Minimum futures evidence before any later scaffold:

- Contract calendars and sessions for MES/MNQ.
- Roll rules for quarterly contracts.
- Tick size and tick value.
- Margin placeholders.
- Commission and slippage assumptions.
- Data source and permission notes.

Offline scaffold command:

```bash
PYTHONPATH=src python3 -m trading_ai.cli futures-research-scaffold \
  --config configs/futures_micro.yml \
  --as-of-date 2026-06-18
```

The command writes
`reports/tmp/futures_research/<as_of_date>/research_manifest.json` and `.md`.
It reuses `futures-readiness-report` logic: complete MES/MNQ placeholders
produce `OK`, missing `platform_decision` produces `WARN`, and missing
calendar, roll, costs, margin, tick size, or tick value produces `BLOCKED`.
The manifest is research-only and includes contracts, tick values, margin
placeholders, sessions, roll rules, costs, and data requirements.

Execution guardrails:

- No futures submit/cancel command is authorized.
- No IBKR credentials are read.
- No broker MCP is used.
- `live_trading_allowed` remains `false`.
- `futures-research-scaffold` does not create adapters, submit/cancel orders,
  read credentials, or authorize execution.
