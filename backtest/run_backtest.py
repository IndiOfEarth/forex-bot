"""
Walk-forward backtest — direction filter + exit improvements + entry quality.

Configs per pair:
  1.  base                    — no filters, both directions (baseline)
  2.  trend_filter            — EMA 21/50/200 alignment required
  3.  trend_long_only         — trend filter + long only
  4.  trend_short_only        — trend filter + short only (for reference)
  5.  trend_trailing          — trend filter + trailing stop + partial close
  6.  trend_trailing_lo       — config 5 + long only
  7.  trend_all               — config 5 + min range 20 pips + body ratio filter
  8.  trend_all_lo            — config 7 + long only

Configs 5–8 implement Phase 1+2 improvements. Compare OOS pips vs configs 2–4
to decide which to activate in the live strategy.
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

    fname_label = label.lower().replace(" ", "_").replace("+", "").replace("/", "")
    fname = f"logs/backtest_{pair}_{fname_label}.csv"
    engine.export_trades(results, path=fname)


# ── Reusable param blocks ──────────────────────────────────────

TRAILING_PARAMS = dict(
    trail_trigger_r=TRAIL_TRIGGER_R,
    trail_lock_r=TRAIL_LOCK_R,
    partial_close_r=PARTIAL_CLOSE_R,
    partial_close_pct=PARTIAL_CLOSE_PCT,
    full_tp_r=FULL_TP_R,
)

ALL_IMPROVEMENTS = dict(
    **TRAILING_PARAMS,
    min_range_pips=20.0,
    require_body_ratio=True,
)


if __name__ == "__main__":
    client = OandaClient()

    for pair in ["EUR_USD", "GBP_USD", "USD_JPY", "USD_CAD"]:
        df = fetch_historical(client, pair=pair, granularity="M15", years=3)
        if df.empty:
            continue

        lo_dirs = ("buy", "sell")  # all pairs: both directions tested

        # ── Baseline configs (unchanged) ───────────────────────
        run(pair, StrategyParams(), df, "base")

        run(pair, StrategyParams(
            require_trend_alignment=True,
        ), df, "trend_filter")

        run(pair, StrategyParams(
            require_trend_alignment=True,
            allowed_directions=("buy",),
        ), df, "trend_long_only")

        run(pair, StrategyParams(
            require_trend_alignment=True,
            allowed_directions=("sell",),
        ), df, "trend_short_only")

        # ── Phase 1+2 improvement configs ─────────────────────
        # 5. Trend filter + trailing stop + partial close
        run(pair, StrategyParams(
            require_trend_alignment=True,
            **TRAILING_PARAMS,
        ), df, "trend_trailing")

        # 6. Config 5 + per-pair direction restriction
        run(pair, StrategyParams(
            require_trend_alignment=True,
            allowed_directions=lo_dirs,
            **TRAILING_PARAMS,
        ), df, "trend_trailing_lo")

        # 7. Config 5 + min range 20 pips + body ratio
        run(pair, StrategyParams(
            require_trend_alignment=True,
            **ALL_IMPROVEMENTS,
        ), df, "trend_all")

        # 8. Config 7 + per-pair direction restriction (fullest filter set)
        run(pair, StrategyParams(
            require_trend_alignment=True,
            allowed_directions=lo_dirs,
            **ALL_IMPROVEMENTS,
        ), df, "trend_all_lo")

        # ── Phase B: Entry quality + exit filter configs ───────
        # 9. First 30 min of London open only (07:00–07:30 UTC)
        run(pair, StrategyParams(
            require_trend_alignment=True,
            **ALL_IMPROVEMENTS,
            first_bar_minutes=30,
        ), df, "first_bar_30m")

        # 10. First 15 min only (07:00 bar only — strongest momentum)
        run(pair, StrategyParams(
            require_trend_alignment=True,
            **ALL_IMPROVEMENTS,
            first_bar_minutes=15,
        ), df, "first_bar_15m")

        # 11. Time-based exit at noon UTC (close before London lunchtime)
        run(pair, StrategyParams(
            require_trend_alignment=True,
            **ALL_IMPROVEMENTS,
            time_exit_hour=12,
        ), df, "noon_exit")

        # 12. 4H trend confirmation (H1 + H4 EMA stacks must agree)
        run(pair, StrategyParams(
            require_trend_alignment=True,
            require_4h_trend=True,
            **ALL_IMPROVEMENTS,
        ), df, "trend_h4")

        # 13. ADX > 25 filter (trending market required at entry)
        run(pair, StrategyParams(
            require_trend_alignment=True,
            require_adx=True,
            min_adx=25.0,
            **ALL_IMPROVEMENTS,
        ), df, "trend_adx")

        # 14. Combined: first_bar + noon_exit + 4H + ADX (kitchen sink)
        run(pair, StrategyParams(
            require_trend_alignment=True,
            require_4h_trend=True,
            require_adx=True,
            min_adx=25.0,
            first_bar_minutes=30,
            time_exit_hour=12,
            **ALL_IMPROVEMENTS,
        ), df, "combined_filters")

    print("\n[Backtest] Done.\n")
    print("OOS aggregate comparison — check logs/ for per-trade CSVs.")
    print("Improvement targets vs baseline (trend_filter):")
    print("  EUR_USD: PF > 1.40, OOS pips > +700")
    print("  GBP_USD: PF > 1.25, OOS pips > +460")
    print("  USD_JPY: acceptance gate — OOS PF > 1.2 and > 30 OOS trades")
    print("  USD_CAD: acceptance gate — OOS PF > 1.2 and > 30 OOS trades")