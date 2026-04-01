# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Activate virtual environment first
source venv/bin/activate

# Run the live bot (dry run mode by default — no real orders placed)
python main.py

# Run walk-forward backtests on EUR_USD and GBP_USD
python backtest/run_backtest.py

# Quick connection test
python -c "from oanda.client import OandaClient; OandaClient().test_connection()"
```

To place real trades, set `DRY_RUN = False` in `main.py:14`. The bot targets the OANDA practice account (set in `.env`).

There is no test suite or linter configured.

## Architecture

This is a live forex trading bot that implements a **London Breakout strategy** on EUR/USD and GBP/USD. It trades via the OANDA v20 REST API and gates every signal through an economic calendar filter and a risk manager.

### Data flow (live trading)

```
OANDA API → OandaClient → MarketData (EMAs, ATR, Asian range)
                                        ↓
                          LondonBreakout.scan() → BreakoutSignal
                                        ↓
ForexFactory RSS → filter.py  → bias + blackout check
                                        ↓
                          RiskManager.pre_trade_check()
                          RiskManager.calculate_units()
                                        ↓
                          OrderExecutor.execute_signal()
```

### Key components

**`main.py`** — Orchestrates one full trading cycle in sequence: connect → calendar → blackout check → prices → risk status → scan → execute.

**`config.py`** — Single source of truth for all constants: pairs, risk parameters (`RISK_PER_TRADE=0.01`, `MAX_DAILY_LOSS=0.04`, `MAX_OPEN_POSITIONS=2`), session windows (UTC), breakout buffer pips, and indicator periods.

**`strategies/london_breakout.py`** — The core strategy. Asian range is 22:00 (prev day) → 07:00 UTC. Breakout entries fire at Asian high + 5 pips (long) or Asian low − 5 pips (short) during the 07:00–09:00 UTC window. Applies an H1 EMA trend filter (21/50/200 stack). GBP/USD is configured long-only — this is backtest-validated, not arbitrary.

**`risk/manager.py`** — Pre-trade gatekeeper: checks daily drawdown kill-switch, open position cap, minimum R:R, and USD correlation. Position sizing uses fixed fractional (1% risk), scaled by weekly macro bias (0.5× FOMC weeks, 1.5× strong-bias weeks).

**`econ_calendar/`** — Fetches ForexFactory RSS (no API key needed). `filter.py` calculates a directional USD bias score from event types (NFP, CPI, FOMC, etc.) and blocks trades during 30-min windows around Tier 1 events.

**`oanda/market_data.py`** — Wraps candle fetching into a pandas DataFrame with indicators. `get_asian_range()` must span prev-day 22:00 through current-day 07:00 to capture the full consolidation zone. Pip conversions are pair-aware (JPY: 0.01, others: 0.0001).

### Backtest system (`backtest/`)

Walk-forward: 3 years of M15 data split into rolling 6-month training / 2-month validation windows. `data_loader.py` fetches from OANDA in 5,000-bar chunks and caches to CSV in `backtest/data_cache/`. `engine.py` simulates day-by-day: reconstruct Asian range → scan 07:00–09:00 → simulate fill and exit. `run_backtest.py` runs four configurations per pair (base, trend filter, long-only, short-only) and writes results to `logs/`.

### Important implementation notes

- No `__init__.py` files — modules are imported directly, not as packages.
- `PAIR_CONFIG` in `london_breakout.py` stores per-pair overrides (e.g., GBP/USD direction restriction) derived from backtest results. Do not change these without re-running the walk-forward.
- The Asian range calculation must include previous-day bars from 22:00 onwards — using only current-day bars < 07:00 is a known bug that produces a narrower range.
- Trailing stop logic in `orders.py` moves SL to break-even + 1 pip when 1R in profit.
