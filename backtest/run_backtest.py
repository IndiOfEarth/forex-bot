"""
Walk-forward backtest — direction filter added.

Runs four combinations per pair:
  1. Base (no filters)
  2. Trend filter only
  3. Trend filter + long only  (relevant for GBP/USD)
  4. Trend filter + short only (for completeness)

Compare OOS aggregates to find the best combination per pair.
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from oanda.client import OandaClient
from backtest.data_loader import fetch_historical
from backtest.engine import BacktestEngine, StrategyParams


def run(pair: str, params: StrategyParams, df, label: str):
    print(f"\n{'='*65}")
    print(f"  {label} — {pair}")
    print(f"{'='*65}")
    engine  = BacktestEngine(df=df, pair=pair, params=params)
    results = engine.run_walk_forward(train_months=6, validate_months=2)
    engine.print_summary(results)

    # Sanitise label for filename
    fname_label = label.lower().replace(" ", "_").replace("+", "").replace("/", "")
    fname = f"logs/backtest_{pair}_{fname_label}.csv"
    engine.export_trades(results, path=fname)


if __name__ == "__main__":
    client = OandaClient()

    for pair in ["EUR_USD", "GBP_USD"]:
        df = fetch_historical(client, pair=pair, granularity="M15", years=3)
        if df.empty:
            continue

        # 1. Base — no filters, both directions
        run(pair, StrategyParams(), df, "base")

        # 2. Trend filter — both directions
        run(pair, StrategyParams(
            require_trend_alignment=True,
        ), df, "trend_filter")

        # 3. Trend filter + long only
        run(pair, StrategyParams(
            require_trend_alignment=True,
            allowed_directions=("buy",),
        ), df, "trend_long_only")

        # 4. Trend filter + short only
        run(pair, StrategyParams(
            require_trend_alignment=True,
            allowed_directions=("sell",),
        ), df, "trend_short_only")

    print("\n[Backtest] Done.\n")
    print("Key comparison — OOS aggregate pips per combination:")
    print("  Check logs/ for per-trade CSVs.")