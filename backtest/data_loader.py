"""
Historical data loader for backtesting.
Fetches M15 candles from OANDA in chunks (max 5000 per request)
and caches to CSV so you don't re-fetch on every run.
"""

import os
import pandas as pd
from datetime import datetime, timezone, timedelta

from oanda.client import OandaClient
import oandapyV20.endpoints.instruments as instruments


CACHE_DIR = "backtest/data_cache"


def fetch_historical(
    client:      OandaClient,
    pair:        str,
    granularity: str = "M15",
    years:       int = 3,
    force:       bool = False,
) -> pd.DataFrame:
    """
    Fetches historical OHLCV data for a pair.
    Caches to CSV — subsequent calls load from cache unless force=True.

    pair:        e.g. "EUR_USD"
    granularity: "M15" recommended for London breakout backtesting
    years:       how many years of history to fetch
    force:       re-fetch even if cache exists
    """
    os.makedirs(CACHE_DIR, exist_ok=True)
    cache_path = f"{CACHE_DIR}/{pair}_{granularity}_{years}y.csv"

    if os.path.exists(cache_path) and not force:
        print(f"[DataLoader] Loading cached data: {cache_path}")
        df = pd.read_csv(cache_path, index_col="time", parse_dates=True)
        df.index = pd.to_datetime(df.index, utc=True)
        print(f"[DataLoader] Loaded {len(df):,} bars — {df.index[0].date()} to {df.index[-1].date()}")
        return df

    print(f"[DataLoader] Fetching {years} years of {pair} {granularity} from OANDA...")
    print(f"[DataLoader] This may take a minute — OANDA caps at 5,000 bars per request.")

    all_candles = []
    end_dt      = datetime.now(timezone.utc)
    start_dt    = end_dt - timedelta(days=365 * years)

    # Chunk size: 5000 bars. M15 = 4 bars/hr = 96/day
    # 5000 bars ≈ 52 days of M15 data
    chunk_bars  = 4500
    current_end = end_dt
    chunk_count = 0

    while current_end > start_dt:
        from_dt = current_end - timedelta(minutes=15 * chunk_bars)
        if from_dt < start_dt:
            from_dt = start_dt

        params = {
            "granularity": granularity,
            "from":        from_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "to":          current_end.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "price":       "M",
        }

        try:
            r = instruments.InstrumentsCandles(pair, params=params)
            client.client.request(r)

            chunk = []
            for c in r.response["candles"]:
                if not c["complete"]:
                    continue
                chunk.append({
                    "time":   c["time"],
                    "open":   float(c["mid"]["o"]),
                    "high":   float(c["mid"]["h"]),
                    "low":    float(c["mid"]["l"]),
                    "close":  float(c["mid"]["c"]),
                    "volume": int(c["volume"]),
                })

            all_candles = chunk + all_candles
            chunk_count += 1
            current_end  = from_dt

            if chunk_count % 5 == 0:
                print(f"  ...fetched {len(all_candles):,} bars so far")

        except Exception as e:
            print(f"[DataLoader] Fetch error: {e}")
            break

    if not all_candles:
        print("[DataLoader] No data returned.")
        return pd.DataFrame()

    df = pd.DataFrame(all_candles)
    df["time"] = pd.to_datetime(df["time"], utc=True)
    df.set_index("time", inplace=True)
    df = df[~df.index.duplicated(keep="first")]
    df.sort_index(inplace=True)

    # Cache it
    df.to_csv(cache_path)
    print(f"[DataLoader] Saved {len(df):,} bars to {cache_path}")
    print(f"[DataLoader] Range: {df.index[0].date()} → {df.index[-1].date()}")

    return df


def load_cached(pair: str, granularity: str = "M15", years: int = 3) -> pd.DataFrame | None:
    """Load from cache only — returns None if not found."""
    cache_path = f"{CACHE_DIR}/{pair}_{granularity}_{years}y.csv"
    if not os.path.exists(cache_path):
        return None
    df = pd.read_csv(cache_path, index_col="time", parse_dates=True)
    df.index = pd.to_datetime(df.index, utc=True)
    return df