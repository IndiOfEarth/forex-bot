# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Activate virtual environment first
source venv/bin/activate

# Run the live bot — single scan (dry run by default)
python main.py

# Run the live bot — continuous loop, scans every 15 min during 06:45–09:00 UTC
python main.py --loop

# Place real orders (disables dry-run)
python main.py --loop --live

# Run walk-forward backtests (all pairs, 8 configs each)
python backtest/run_backtest.py

# Quick connection test
python -c "from oanda.client import OandaClient; OandaClient().test_connection()"
```

Bot logs to `logs/bot_YYYY-MM-DD.log` (rotates at UTC midnight). Both stdout and file receive all output.

To place real trades, pass `--live` on the CLI. The bot targets the OANDA practice account (set in `.env`).

There is no test suite or linter configured.

## Architecture

This is a live forex trading bot that implements a **London Breakout strategy** on EUR/USD, GBP/USD, and USD/JPY. It trades via the OANDA v20 REST API and gates every signal through an economic calendar filter and a risk manager.

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
                                        ↓
                          OrderExecutor.apply_trailing_stop()  ← called each cycle
```

### Key components

**`main.py`** — Two entry points: `main_once()` (single scan) and `main_loop()` (continuous scheduler). The loop sleeps until 06:45 UTC, polls every 15 minutes through 09:00 UTC, then sleeps until the next day. `LondonBreakout` instance is shared across cycles so `_fired_today` state persists correctly. Resets at UTC midnight.

**`config.py`** — Single source of truth for all constants: pairs (`PAIRS`), risk parameters (`RISK_PER_TRADE=0.01`, `MAX_DAILY_LOSS=0.04`, `MAX_OPEN_POSITIONS=3`), session windows (UTC), exit management (`TRAIL_TRIGGER_R`, `TRAIL_LOCK_R`, `PARTIAL_CLOSE_R`, `PARTIAL_CLOSE_PCT`, `FULL_TP_R`), and indicator periods.

**`strategies/london_breakout.py`** — The core strategy. Asian range is 22:00 (prev day) → 07:00 UTC. Breakout entries fire at Asian high + 5 pips (long) or Asian low − 5 pips (short) during the 07:00–09:00 UTC window. Applies H1 EMA trend filter (21/50/200 stack), minimum range filter (20 pips), and momentum body ratio filter (≥0.60). Per-pair entry quality filters live in `PAIR_CONFIG` — do not change without re-running walk-forward backtest. EUR/USD uses H4 trend confirmation; GBP/USD and USD/JPY use first-bar-only filter (first 15 min of London open).

**`risk/manager.py`** — Pre-trade gatekeeper: checks daily drawdown kill-switch, open position cap, minimum R:R, and USD correlation. Position sizing uses fixed fractional (1% risk), scaled by weekly macro bias (0.5× FOMC weeks, 1.5× strong-bias weeks).

**`econ_calendar/`** — Fetches ForexFactory RSS (no API key needed). `filter.py` calculates a directional USD bias score from event types (NFP, CPI, FOMC, etc.) and blocks trades during 30-min windows around Tier 1 events.

**`oanda/market_data.py`** — Wraps candle fetching into a pandas DataFrame with indicators. `get_asian_range()` must span prev-day 22:00 through current-day 07:00 to capture the full consolidation zone. Pip conversions are pair-aware (JPY: 0.01, others: 0.0001).

**`oanda/orders.py`** — `apply_trailing_stop()` implements two-stage exit: move SL to break-even at 1R profit, close 50% of position at 1.5R and lock 0.5R on the remainder. `initial_sl` is persisted by encoding it into the OANDA `clientExtensions.comment` field as `|isl=<price>`, parsed back in `main.py` each cycle.

### Backtest system (`backtest/`)

Walk-forward: 3 years of M15 data split into rolling 6-month training / 2-month validation windows. `data_loader.py` fetches from OANDA in 5,000-bar chunks and caches to CSV in `backtest/data_cache/`. `engine.py` simulates day-by-day: reconstruct Asian range → scan 07:00–09:00 → simulate fill and exit with trailing/partial-close logic. `run_backtest.py` runs 14 configurations per pair and writes trade-level CSVs to `logs/`.

`StrategyParams` in `engine.py` supports: `require_trend_alignment`, `require_4h_trend`, `require_adx`/`min_adx`, `first_bar_minutes`, `time_exit_hour`, `min_range_pips`, `require_body_ratio`, `trail_trigger_r`, `trail_lock_r`, `partial_close_r`, `partial_close_pct`, `full_tp_r`.

### Important implementation notes

- No `__init__.py` files — modules are imported directly, not as packages.
- `PAIR_CONFIG` in `london_breakout.py` stores per-pair entry quality filter config derived from backtest results. Do not change without re-running the walk-forward.
- The Asian range calculation must include previous-day bars from 22:00 onwards — using only current-day bars < 07:00 is a known bug that produces a narrower range.
- The partial close fires at `PARTIAL_CLOSE_R = 1.5R`. RR of 2.0R and 3.0R are both worse than 2.5R in OOS testing because the trailing stop remainder rarely extends from 1.5R to those targets — they sit in a dead zone. Keep `REWARD_RISK = 2.5` in the backtest engine.
- ADX filter (`require_adx`) was tested at min_adx=25 but filters 97%+ of M15 trades even with period=56. Discarded — too aggressive on M15 granularity.
- Per-pair filters are not uniform: `require_4h_trend` helps EUR/USD but not GBP/USD; `first_bar_minutes=15` helps GBP/USD and USD/JPY but hurts EUR/USD.
- USD/JPY `first_bar_minutes=15` produces only 14 OOS trades over 3 years (PF 6.90). PF will regress in live trading. If 3+ consecutive losses occur in paper trading, switch to `first_bar_minutes=30` (PF 2.65, 23 trades — more robust).

---

## Current State (as of 2026-04-02)

### Validated OOS results — best per-pair config (with entry quality filters)

| Pair | Config | OOS Trades | PF | OOS Pips |
|------|--------|-----------|-----|----------|
| EUR/USD | trend_all + 4H trend | 64 | 1.99 | ~+620 |
| GBP/USD | trend_all + first_bar_15m | 67 | 1.71 | ~+490 |
| USD/JPY | trend_all + first_bar_15m | 14 | 6.90 | ~+800 |
| **Portfolio** | — | **~145** | **~2.30** | — |

USD/CAD was tested and **rejected** — all configs failed the gate (best PF 0.90). Attributed to oil price correlation disrupting Asian range structure.

**`trend_all` baseline** (configs 1–8): EMA trend alignment (21/50/200 H1 stack) + min Asian range 20 pips + momentum body ratio filter (≥0.60) + trailing stop/partial close exit management. Portfolio PF 1.53, 315 OOS trades.

**Entry quality filters** (configs 9–14) improved per-pair PF significantly. Best combos are now wired into `PAIR_CONFIG` in `strategies/london_breakout.py`.

### Outcome distribution (OOS, portfolio — trend_all baseline)

- ~60% break-even exits (SL moved to BE, then stopped out — slightly positive pips)
- ~17% partial wins (reached 1.5R partial close — the profit engine)
- ~22% losses (SL hit)

Note: "win rate" reported as ~51% includes BE exits. True TP-hit rate is 15–22%. Strategy is profitable due to asymmetric partial-win exits.

### Recent changes

- **`backtest/engine.py`** — Added `StrategyParams` fields: `first_bar_minutes`, `require_4h_trend`, `require_adx`/`min_adx`/`adx_period`, `time_exit_hour`. Added `_compute_adx()` static method. Added H4 resampling and `h4_trend` column prep. Entry-loop guards and time-exit logic implemented.
- **`backtest/run_backtest.py`** — Expanded to 14 configs per pair (added 9–14: first_bar_30m, first_bar_15m, noon_exit, trend_h4, trend_adx, combined_filters)
- **`strategies/london_breakout.py`** — `PAIR_CONFIG` updated with `require_4h_trend` and `first_bar_minutes` per pair. Added first-bar guard (step 1 in `_evaluate_pair()`), H4 trend check (step 7), and `_get_h4_trend_state()` method. `LONDON_OPEN_HOUR = 7` class constant added.
- **`oanda/orders.py`** — `initial_sl` encoded in `clientExtensions.comment` for trailing stop persistence
- **`main.py`** — `main_once()` / `main_loop()` / `run_cycle()`; `--loop` and `--live` CLI flags; file logging via `Tee`; trailing stop management wired into live cycle
- **`config.py`** — `MAX_OPEN_POSITIONS` raised to 3; `PAIRS` updated to EUR/USD, GBP/USD, USD/JPY

---

## Next Steps (pick up here)

**Phase C — Paper trade (30 days)**
Run `python main.py --loop` daily during 06:45–09:00 UTC. Review `logs/bot_YYYY-MM-DD.log` each morning.

Monitoring checklist:
- USD/JPY: 14 OOS trades is a thin sample — if 3+ consecutive losses occur, switch `first_bar_minutes` to 30 in `PAIR_CONFIG` (PF 2.65, 23 OOS trades)
- Check that H4 trend fetch for EUR/USD is not causing API latency during the scan window
- Verify partial close orders are filling correctly in OANDA practice account

After 30 clean days: switch to `--live` with `RISK_PER_TRADE = 0.005` for first 60 live days.
