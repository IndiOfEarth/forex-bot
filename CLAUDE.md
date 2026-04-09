# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Activate virtual environment first
source venv/bin/activate

# Run the live bot — single scan (dry run by default)
python main.py

# Run the live bot — London only, continuous loop (DST-aware start, 2-hour window)
python main.py --loop

# Run the live bot — London + Tokyo sessions
python main.py --loop --tokyo

# Place real orders (disables dry-run)
python main.py --loop --live
python main.py --loop --tokyo --ny --live

# Run walk-forward backtests (all pairs, 30 configs each)
python backtest/run_backtest.py

# Quick connection test
python -c "from oanda.client import OandaClient; OandaClient().test_connection()"
```

Bot logs to `logs/bot_YYYY-MM-DD.log` (rotates at UTC midnight). Both stdout and file receive all output.

To place real trades, pass `--live` on the CLI. The bot targets the OANDA practice account (set in `.env`).

There is no test suite or linter configured.

## Architecture

This is a live forex trading bot implementing a **London Breakout strategy** (EUR/USD, GBP/USD, USD/JPY) and a **Tokyo Session Breakout strategy** (EUR/JPY, USD/JPY). It trades via the OANDA v20 REST API and gates every signal through an economic calendar filter and a risk manager.

### Data flow (live trading)

```
OANDA API → OandaClient → MarketData (EMAs, ATR, session ranges)
                                        ↓
                  LondonBreakout.scan() / TokyoBreakout.scan() → Signal
                                        ↓
ForexFactory RSS → filter.py  → bias + blackout check
                                        ↓
                          RiskManager.pre_trade_check()
                          RiskManager.calculate_units()
                                        ↓
                          OrderExecutor.execute_signal()
                                        ↓
                          OrderExecutor.apply_trailing_stop()  ← each cycle
                                        ↓
                  TokyoBreakout.get_positions_to_close()       ← EUR/JPY at 07:00
```

### Key components

**`main.py`** — Two entry points: `main_once()` (single scan) and `main_loop()` (continuous scheduler). The loop wakes at 01:45 UTC when `--tokyo` is active (Tokyo window), and at `LONDON_OPEN_UTC - 1`:45 for London (DST-aware). Both `LondonBreakout` and `TokyoBreakout` instances are shared across cycles so `_fired_today` state persists correctly. Both reset at UTC midnight. `run_cycle()` handles Tokyo scan, EUR/JPY force-close at `LONDON_OPEN_UTC`, and London scan in sequence. Startup prints `[Session] London open: HH:00 UTC | NY open: HH:00 UTC` to confirm live UTC anchors.

**`config.py`** — Single source of truth for all constants: pairs (`PAIRS`), risk parameters (`RISK_PER_TRADE=0.03`, `MAX_DAILY_LOSS=0.06`, `MAX_OPEN_POSITIONS=3`), session windows (UTC), exit management (`TRAIL_TRIGGER_R`, `TRAIL_LOCK_R`, `PARTIAL_CLOSE_R`, `PARTIAL_CLOSE_PCT`, `FULL_TP_R`), and indicator periods. Exports DST-aware `LONDON_OPEN_UTC`, `LONDON_CLOSE_UTC`, `NY_OPEN_UTC`, `NY_CLOSE_UTC` computed at startup via `zoneinfo` — 07:00/15:00/13:00/22:00 UTC in BST+EDT, 08:00/16:00/14:00/23:00 UTC in GMT+EST.

**`strategies/london_breakout.py`** — London Breakout. Asian range is 22:00 (prev day) → `LONDON_OPEN_UTC`. Breakout entries fire at Asian high + 5 pips (long) or Asian low − 5 pips (short) during `LONDON_OPEN_UTC`–`LONDON_OPEN_UTC+2` UTC. Applies H1 EMA trend filter (21/50/200 stack), min range 20 pips, momentum body ratio ≥0.60. Per-pair `PAIR_CONFIG` — do not change without re-running walk-forward backtest. EUR/USD uses H4 trend confirmation; GBP/USD and USD/JPY use first-bar-only filter (first 15 min of London open).

**`strategies/tokyo_breakout.py`** — Tokyo Breakout. Consolidation range: 20:00 (prev day) → 02:00 UTC. Entry window: 02:00–06:00 UTC (Tokyo JST has no DST — these are permanent UTC hours). Validated pairs: EUR/JPY (PF 1.94) and USD/JPY (PF 1.87). EUR/JPY force-closes at `LONDON_OPEN_UTC` via `get_positions_to_close()`; USD/JPY uses trailing stop exit. Both pairs Mon–Thu only. Per-pair config in `TOKYO_PAIR_CONFIG` — do not change without re-running Phase G backtest.

**`risk/manager.py`** — Pre-trade gatekeeper: checks daily drawdown kill-switch, open position cap, minimum R:R, and USD correlation. Position sizing uses fixed fractional (1% risk), scaled by weekly macro bias (0.5× FOMC weeks, 1.5× strong-bias weeks).

**`econ_calendar/`** — Fetches ForexFactory RSS (no API key needed). `filter.py` calculates a directional USD bias score from event types (NFP, CPI, FOMC, etc.) and blocks trades during 30-min windows around Tier 1 events.

**`oanda/market_data.py`** — Wraps candle fetching into a pandas DataFrame with indicators. `get_asian_range()` must span prev-day 22:00 through current-day 07:00 to capture the full consolidation zone. `get_overnight_range(start_hour, end_hour)` handles midnight-spanning windows (e.g., 20:00–02:00 for Tokyo); when `start_hour > end_hour` the method automatically splits across the day boundary. `get_session_range()` handles same-day windows (e.g., 09:00–13:00 for NY). Pip conversions are pair-aware (JPY: 0.01, others: 0.0001).

**`oanda/orders.py`** — `apply_trailing_stop()` implements two-stage exit: move SL to break-even at 1R profit, close 50% of position at 1.5R and lock 0.5R on the remainder. `initial_sl` is persisted by encoding it into the OANDA `clientExtensions.comment` field as `|isl=<price>`, parsed back in `main.py` each cycle.

### Backtest system (`backtest/`)

Walk-forward: 3 years of M15 data split into rolling 6-month training / 2-month validation windows. `data_loader.py` fetches from OANDA in 5,000-bar chunks and caches to CSV in `backtest/data_cache/`. `engine.py` simulates day-by-day: reconstruct session range → scan entry window → simulate fill and exit with trailing/partial-close logic. `run_backtest.py` runs 30 configurations per pair and writes trade-level CSVs to `logs/`.

`StrategyParams` in `engine.py` supports: `require_trend_alignment`, `require_4h_trend`, `require_adx`/`min_adx`, `first_bar_minutes`, `time_exit_hour`, `min_range_pips`, `require_body_ratio`, `trail_trigger_r`, `trail_lock_r`, `partial_close_r`, `partial_close_pct`, `full_tp_r`, `range_start_hour`, `range_end_hour`, `entry_start_hour`, `entry_end_hour`, `pullback_entry`, `pullback_pips`, `pullback_timeout_bars`, `allowed_weekdays`.

### Important implementation notes

- No `__init__.py` files — modules are imported directly, not as packages.
- `PAIR_CONFIG` in `london_breakout.py` and `TOKYO_PAIR_CONFIG` in `tokyo_breakout.py` store per-pair filter configs derived from backtest results. Do not change without re-running the walk-forward.
- The Asian range calculation must include previous-day bars from 22:00 onwards — using only current-day bars < 07:00 is a known bug that produces a narrower range.
- The partial close fires at `PARTIAL_CLOSE_R = 1.5R`. RR of 2.0R and 3.0R are both worse than 2.5R in OOS testing because the trailing stop remainder rarely extends from 1.5R to those targets — they sit in a dead zone. Keep `REWARD_RISK = 2.5` in the backtest engine.
- ADX filter (`require_adx`) was tested at min_adx=25 but filters 97%+ of M15 trades even with period=56. Discarded — too aggressive on M15 granularity.
- Per-pair London filters are not uniform: `require_4h_trend` helps EUR/USD but not GBP/USD; `first_bar_minutes=15` helps GBP/USD and USD/JPY but hurts EUR/USD.
- London USD/JPY `first_bar_minutes=15` produces only 14 OOS trades over 3 years (PF 6.90). PF will regress in live trading. If 3+ consecutive losses occur, switch to `first_bar_minutes=30` (PF 2.65, 23 trades — more robust).
- Pullback limit entries (Phase F) were tested and **rejected**: EUR/USD trades dropped from 91 → 21 with no PF gain. The breakout is a momentum play — retracements indicate failing breakouts, not better entries.
- Tokyo AUD/USD rejected across all 6 configs (best OOS PF 1.08). AUD/USD lacks clean overnight consolidation structure in the 20:00–02:00 window.
- Tokyo EUR/JPY force-close at `LONDON_OPEN_UTC` outperforms letting trades run (PF 1.94 vs 1.53) because London open regularly reverses Tokyo moves on EUR/JPY. USD/JPY shows the opposite — trailing stop is better (PF 1.87 vs 1.74).
- USD/JPY participates in both Tokyo (02:00–06:00) and London (07:00–09:00) windows independently. Watch for correlated drawdowns if both fire on the same day.

---

## Current State (as of 2026-04-09)

### Validated OOS results — London Breakout (best per-pair config)

| Pair | Config | OOS Trades | PF | OOS Pips |
|------|--------|-----------|-----|----------|
| EUR/USD | trend_all + 4H trend | 64 | 1.99 | ~+620 |
| GBP/USD | trend_all + first_bar_15m | 67 | 1.71 | ~+490 |
| USD/JPY | trend_all + first_bar_15m | 14 | 6.90 | ~+800 |
| **London Portfolio** | — | **~145** | **~2.30** | — |

USD/CAD was tested and **rejected** — all London configs failed (best PF 0.90). Oil correlation disrupts Asian range structure.

### Validated OOS results — Tokyo Breakout (Phase G)

| Pair | Config | OOS Trades | PF | Notes |
|------|--------|-----------|-----|-------|
| EUR/JPY | tokyo_london_exit | 118 | 1.94 | Force-close at 07:00 UTC |
| USD/JPY | tokyo_no_friday | 127 | 1.87 | Trailing stop exit, Mon–Thu |
| AUD/USD | — | — | 1.08 | **Rejected** — noisy overnight range |

### Outcome distribution (OOS, London portfolio — trend_all baseline)

- ~60% break-even exits (SL moved to BE, then stopped out — slightly positive pips)
- ~17% partial wins (reached 1.5R partial close — the profit engine)
- ~22% losses (SL hit)

Note: "win rate" reported as ~51% includes BE exits. True TP-hit rate is 15–22%. Strategy is profitable due to asymmetric partial-win exits.

### Recent changes

- **`strategies/tokyo_breakout.py`** — New file. `TokyoBreakout` class: 20:00–02:00 UTC overnight range, 02:00–06:00 UTC entry window, EUR/JPY force-close at 07:00 via `get_positions_to_close()`, USD/JPY trailing stop exit, Mon–Thu filter, EMA trend + body ratio + ATR regime filters. `TOKYO_PAIR_CONFIG` stores validated per-pair settings.
- **`oanda/market_data.py`** — Added `get_overnight_range()` for midnight-spanning windows; `get_session_range()` for same-day windows (NY breakout).
- **`backtest/run_backtest.py`** — Phase F pullback configs (22–24, rejected) + Phase G Tokyo configs (25–30). Now 30 configs per pair.
- **`backtest/engine.py`** — Added `pullback_entry`, `pullback_pips`, `pullback_timeout_bars`, `allowed_weekdays`, session window params (`range_start_hour`, `range_end_hour`, `entry_start_hour`, `entry_end_hour`) to `StrategyParams`. Midnight-wrap range logic for Tokyo. Pullback limit entry simulation.
- **`main.py`** — `--tokyo` CLI flag; Tokyo scan and EUR/JPY force-close logic in `run_cycle()`; `_in_tokyo_window()` helper; Tokyo wake at 01:45 UTC when enabled; `TokyoBreakout.reset_daily()` in midnight reset block. Startup session log line added.
- **`strategies/london_breakout.py`** — `PAIR_CONFIG` with `require_4h_trend` and `first_bar_minutes` per pair. H4 trend check and first-bar guard in `_evaluate_pair()`.
- **`oanda/orders.py`** — `initial_sl` encoded in `clientExtensions.comment` for trailing stop persistence.
- **`config.py`** — `MAX_OPEN_POSITIONS` raised to 3; `PAIRS` set to EUR/USD, GBP/USD, USD/JPY.
- **`config.py` (DST)** — Session UTC anchors (`LONDON_OPEN_UTC`, `LONDON_CLOSE_UTC`, `NY_OPEN_UTC`, `NY_CLOSE_UTC`) now computed dynamically at startup via `zoneinfo` so the bot automatically adjusts when the UK/US clocks change. All hardcoded session hours in `london_breakout.py`, `ny_breakout.py`, `tokyo_breakout.py`, and `main.py` replaced with these constants.
- **`oanda/client.py`** (retry) — Added `_request()` wrapper with up to 3 retries and 5s×attempt backoff on `ConnectionError`/`Timeout`. Fixes crashes caused by OANDA server dropping the TCP connection mid-cycle. All call sites in `client.py`, `orders.py`, and `backtest/data_loader.py` updated.

---

## Next Steps (pick up here)

**Phase C — Paper trade (30 days)**
Run `python main.py --loop --tokyo` daily. Check the startup `[Session]` line to confirm UTC hours are correct for the current DST state.
Review `logs/bot_YYYY-MM-DD.log` each morning.

Monitoring checklist:
- **London USD/JPY**: 14 OOS trades is a thin sample — if 3+ consecutive losses occur, switch `first_bar_minutes` to 30 in `PAIR_CONFIG` (PF 2.65, 23 OOS trades)
- **Tokyo USD/JPY**: watch for correlated drawdowns with London USD/JPY if both fire the same day
- **EUR/JPY force-close**: verify `get_positions_to_close()` correctly identifies Tokyo-entry trades by checking `openTime` hour 2–5 UTC
- Check that H4 trend fetch for EUR/USD is not causing API latency during the London scan window
- Verify partial close orders are filling correctly in OANDA practice account
- **Network retries**: if `[OandaClient] Network error (attempt N/3)` appears in logs, note frequency — occasional is fine; repeated failures on every cycle suggest a connectivity issue to investigate

After 30 clean days: switch to `--live` with `RISK_PER_TRADE = 0.005` for first 60 live days.
