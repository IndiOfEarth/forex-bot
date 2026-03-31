"""
Run walk-forward backtest with and without trend filter.
Cached data is reused — only fetches from OANDA once.

Usage:
    python backtest/run_backtest.py
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from oanda.client import OandaClient
from backtest.data_loader import fetch_historical
from backtest.engine import BacktestEngine, StrategyParams


def run(pair: str, params: StrategyParams, df, label: str):
    print(f"\n{'#'*65}")
    print(f"  {label} — {pair}")
    print(f"{'#'*65}")
    engine  = BacktestEngine(df=df, pair=pair, params=params)
    results = engine.run_walk_forward(train_months=6, validate_months=2)
    engine.print_summary(results)
    fname = f"logs/backtest_{pair}_{'trend' if params.require_trend_alignment else 'base'}.csv"
    engine.export_trades(results, path=fname)


if __name__ == "__main__":
    client = OandaClient()

    base_params  = StrategyParams(require_trend_alignment=False)
    trend_params = StrategyParams(require_trend_alignment=True)

    for pair in ["EUR_USD", "GBP_USD"]:
        df = fetch_historical(client, pair=pair, granularity="M15", years=3)
        if df.empty:
            continue
        run(pair, base_params,  df, "NO TREND FILTER")
        run(pair, trend_params, df, "TREND FILTER ON")

    print("\n[Backtest] Done. Compare OOS aggregates between filter variants.\n")