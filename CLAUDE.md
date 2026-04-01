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

Walk-forward: 3 years of M15 data split into rolling 6-month training / 2-month validation windows. `data_loader.py` fetches from OANDA in 5,000-bar chunks and caches to CSV in `backtest/data_cache/`. `engine.py` simulates day-by-day: reconstruct Asian range → scan 07:00–09:00 → simulate fill and exit. `run_backtest.py` runs 8 configurations per pair and writes results to `logs/`.

### Important implementation notes

- No `__init__.py` files — modules are imported directly, not as packages.
- `PAIR_CONFIG` in `london_breakout.py` stores per-pair overrides derived from backtest results. Do not change these without re-running the walk-forward.
- The Asian range calculation must include previous-day bars from 22:00 onwards — using only current-day bars < 07:00 is a known bug that produces a narrower range.
- Trailing stop logic in `orders.py` implements two-stage exit: move SL to break-even at 1R profit, close 50% of position at 1.5R and lock 0.5R on the remainder. **Not yet wired into `main.py`** — see Next Steps.

---

## Current State (as of 2026-04-01)

A profitability improvement pass has been completed. Validated OOS results from `backtest/run_backtest.py` (best configs):

| Pair | Config | OOS Trades | PF | OOS Pips |
|------|--------|-----------|-----|----------|
| EUR/USD | trend_all | 91 | 1.72 | +743.5 |
| GBP/USD | trend_all | 144 | 1.27 | +587.5 |

Baseline was EUR/USD PF 1.22 (+592 pips), GBP/USD PF 1.10 (+396 pips).

**What `trend_all` means**: EMA trend alignment (21/50/200) + min Asian range 20 pips + momentum body ratio filter (≥0.60) + trailing stop/partial close exit management.

### Changes made this session

- **`config.py`** — Added `TRAIL_TRIGGER_R`, `TRAIL_LOCK_R`, `PARTIAL_CLOSE_R`, `PARTIAL_CLOSE_PCT`, `FULL_TP_R`, `BREAKOUT_ASIAN_MIN_PIPS=20`, `MOMENTUM_BODY_RATIO=0.6`
- **`backtest/engine.py`** — Full trailing stop + partial close simulation; momentum body ratio filter; seasonality filters (weekday/month); 8-config test suite
- **`oanda/orders.py`** — Two-stage trailing stop + partial close implemented in `apply_trailing_stop()`
- **`strategies/london_breakout.py`** — `MIN_RANGE_PIPS` reads from config (now 20); momentum body ratio check added (step 8 of 12 in `_evaluate_pair`)

---

## Next Steps (pick up here)

**1. Update `PAIR_CONFIG` in `strategies/london_breakout.py`** ← START HERE
- GBP/USD is currently hardcoded `"allowed_directions": ("buy",)` (long-only)
- Backtest now validates both directions profitable with the full filter set:
  - bearish entries: +258 pips OOS; bullish: +329 pips OOS
- Change to `"allowed_directions": ("buy", "sell")` and update the comment

**2. Hook trailing stop into `main.py`**
- `OrderExecutor.apply_trailing_stop()` is fully implemented but never called in the live loop
- In `main.py`, after the scan/execute block, iterate open trades and call `apply_trailing_stop()` for each, passing `entry`, `initial_sl`, `direction` from the trade record
- Need to persist `initial_sl` per trade — consider storing in trade `clientExtensions` comment or a local dict keyed by trade ID

**3. Phase 3 — expand pairs**
- Add USD/JPY and AUD/USD to `backtest/run_backtest.py`
- Accept pair only if OOS PF > 1.2 and > 30 OOS trades
- If validated, add to `PAIR_CONFIG` and update `MAX_OPEN_POSITIONS`

**4. Paper trade (30 days)**
- Run with `DRY_RUN = True`, review signals daily
- Only then: set `DRY_RUN = False` with `RISK_PER_TRADE = 0.005` for first 60 live days
