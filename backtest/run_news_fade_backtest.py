"""
News Fade Walk-Forward Backtest — Phase H

Fades large M15 spike bars during London/NY session (07:00–15:00 UTC).
Tested on EUR/USD and GBP/USD only (sufficient liquidity for limit fills).

Configs per pair (31–35):
  31. fade_base        — 40-pip spike, full reversion TP, 5-pip stop buffer
  32. fade_tight_spike — 60-pip minimum (higher conviction spikes only)
  33. fade_partial_tp  — 40-pip spike, TP at 38.2% retracement (earlier exit)
  34. fade_narrow_stop — 40-pip spike, 3-pip stop buffer (tighter risk control)
  35. fade_london_only — 40-pip spike, entry window 07:00–09:00 only

Minimum bar for live deployment: PF > 1.3, >= 20 OOS trades.
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from oanda.client import OandaClient
from backtest.data_loader import fetch_historical
from backtest.engine import BacktestEngine, StrategyParams
from config import TRAIL_TRIGGER_R, TRAIL_LOCK_R, PARTIAL_CLOSE_R, PARTIAL_CLOSE_PCT, FULL_TP_R


def run(pair: str, params: StrategyParams, df, label: str):
    print(f"\n{'='*65}")
    print(f"  {label} — {pair}")
    print(f"{'='*65}")
    engine  = BacktestEngine(df=df, pair=pair, params=params)
    results = engine.run_walk_forward(train_months=6, validate_months=2)
    engine.print_summary(results)

    fname_label = label.lower().replace(" ", "_")
    fname = f"logs/backtest_{pair}_{fname_label}.csv"
    engine.export_trades(results, path=fname)


TRAILING_PARAMS = dict(
    trail_trigger_r  = TRAIL_TRIGGER_R,
    trail_lock_r     = TRAIL_LOCK_R,
    partial_close_r  = PARTIAL_CLOSE_R,
    partial_close_pct = PARTIAL_CLOSE_PCT,
    full_tp_r        = FULL_TP_R,
)

# Base fade params shared by most configs
# entry_start_hour / entry_end_hour control the spike detection window (07:00–15:00 UTC)
FADE_BASE = dict(
    is_news_fade          = True,
    fade_min_spike_pips   = 40.0,
    fade_fibo_level       = 0.618,
    fade_stop_buffer_pips = 5.0,
    fade_tp_retracement   = 0.0,
    entry_start_hour      = 7,
    entry_end_hour        = 15,
    min_rr                = 0.0,   # fade RR is structurally <1; disable floor filter
    **TRAILING_PARAMS,
)


if __name__ == "__main__":
    client = OandaClient()

    for pair in ["EUR_USD", "GBP_USD"]:
        df = fetch_historical(client, pair=pair, granularity="M15", years=3)
        if df.empty:
            continue

        # 31. Base fade — 40-pip spike, full reversion TP
        run(pair, StrategyParams(**FADE_BASE), df, "fade_base")

        # 32. Tighter spike — 60-pip minimum (higher conviction only)
        run(pair, StrategyParams(
            **{**FADE_BASE, "fade_min_spike_pips": 60.0},
        ), df, "fade_tight_spike")

        # 33. Partial TP — fade to 38.2% retracement only (earlier profit taking)
        run(pair, StrategyParams(
            **{**FADE_BASE, "fade_tp_retracement": 0.382},
        ), df, "fade_partial_tp")

        # 34. Narrow stop — 3-pip buffer beyond spike extreme (tighter risk)
        run(pair, StrategyParams(
            **{**FADE_BASE, "fade_stop_buffer_pips": 3.0},
        ), df, "fade_narrow_stop")

        # 35. London only — restrict spike detection to 07:00–09:00 UTC
        run(pair, StrategyParams(
            **{**FADE_BASE, "entry_end_hour": 9},
        ), df, "fade_london_only")
