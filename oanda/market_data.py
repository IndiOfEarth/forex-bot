import pandas as pd
from datetime import datetime, timezone, timedelta
from oanda.client import OandaClient
from config import (
    DEFAULT_GRANULARITY, BREAKOUT_GRANULARITY,
    BREAKOUT_ASIAN_START, BREAKOUT_ASIAN_END,
    EMA_SHORT, EMA_MID, EMA_LONG, RSI_PERIOD,
)


class MarketData:
    """
    Fetches and prepares market data for strategy consumption.
    All methods return pandas DataFrames with indicators pre-calculated.
    """

    def __init__(self, client: OandaClient):
        self.client = client

    # ── Core OHLCV ─────────────────────────────────────────────

    def get_dataframe(self, pair: str, granularity: str = None, count: int = 250) -> pd.DataFrame:
        """
        Fetches candles and returns a DataFrame with OHLCV columns.
        Index is a UTC-aware DatetimeIndex.
        """
        gran = granularity or DEFAULT_GRANULARITY
        candles = self.client.get_candles(pair, granularity=gran, count=count)

        if not candles:
            print(f"[MarketData] No candles returned for {pair} {gran}")
            return pd.DataFrame()

        df = pd.DataFrame(candles)
        df["time"] = pd.to_datetime(df["time"], utc=True)
        df.set_index("time", inplace=True)
        df = df[["open", "high", "low", "close", "volume"]].astype(float)
        df["volume"] = df["volume"].astype(int)

        return df

    # ── Indicators ─────────────────────────────────────────────

    def add_emas(self, df: pd.DataFrame) -> pd.DataFrame:
        """Adds EMA 21, 50, 200 columns."""
        df = df.copy()
        df[f"ema_{EMA_SHORT}"]  = df["close"].ewm(span=EMA_SHORT,  adjust=False).mean()
        df[f"ema_{EMA_MID}"]    = df["close"].ewm(span=EMA_MID,    adjust=False).mean()
        df[f"ema_{EMA_LONG}"]   = df["close"].ewm(span=EMA_LONG,   adjust=False).mean()
        return df

    def add_rsi(self, df: pd.DataFrame, period: int = None) -> pd.DataFrame:
        """Adds RSI column using Wilder's smoothing method."""
        df = df.copy()
        p = period or RSI_PERIOD
        delta = df["close"].diff()
        gain  = delta.clip(lower=0)
        loss  = (-delta).clip(lower=0)
        avg_gain = gain.ewm(alpha=1/p, min_periods=p, adjust=False).mean()
        avg_loss = loss.ewm(alpha=1/p, min_periods=p, adjust=False).mean()
        rs = avg_gain / avg_loss.replace(0, float("nan"))
        df["rsi"] = 100 - (100 / (1 + rs))
        return df

    def add_atr(self, df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
        """Adds Average True Range — used for dynamic stop placement."""
        df = df.copy()
        prev_close = df["close"].shift(1)
        tr = pd.concat([
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"]  - prev_close).abs(),
        ], axis=1).max(axis=1)
        df["atr"] = tr.ewm(alpha=1/period, min_periods=period, adjust=False).mean()
        return df

    def add_all_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        """Convenience: adds EMAs, RSI, ATR in one call."""
        df = self.add_emas(df)
        df = self.add_rsi(df)
        df = self.add_atr(df)
        return df

    # ── Asian Session Range ────────────────────────────────────

    def get_asian_range(self, pair: str) -> dict | None:
        """
        Calculates the Asian session high/low range for today.
        Asian session: 22:00 UTC (previous day) to 07:00 UTC (today).

        Returns:
            {
                high:       float,
                low:        float,
                range_pips: float,
                session_start: datetime,
                session_end:   datetime,
            }
        """
        now = datetime.now(timezone.utc)

        # Asian session for today's London open
        session_end   = now.replace(hour=BREAKOUT_ASIAN_END, minute=0, second=0, microsecond=0)
        session_start = (session_end - timedelta(hours=9))  # 22:00 previous day → 07:00

        if now < session_end:
            # Asian session hasn't closed yet — too early for London breakout
            print(f"[MarketData] Asian session still open. London breakout not available yet.")
            return None

        # Fetch M15 candles — enough to cover the 9hr Asian window
        df = self.get_dataframe(pair, granularity="M15", count=40)
        if df.empty:
            return None

        # Filter to Asian session window
        asian_df = df[(df.index >= session_start) & (df.index < session_end)]

        if asian_df.empty:
            print(f"[MarketData] No Asian session candles found for {pair}")
            return None

        high = asian_df["high"].max()
        low  = asian_df["low"].min()

        # Convert range to pips (EUR_USD, GBP_USD etc = 4 decimal places)
        pip_divisor = 0.01 if "JPY" in pair else 0.0001
        range_pips  = round((high - low) / pip_divisor, 1)

        return {
            "high":          high,
            "low":           low,
            "range_pips":    range_pips,
            "session_start": session_start,
            "session_end":   session_end,
        }

    # ── Pip Utilities ──────────────────────────────────────────

    @staticmethod
    def pips_to_price(pips: float, pair: str) -> float:
        """Convert pip count to price units."""
        if "JPY" in pair:
            return pips * 0.01
        return pips * 0.0001

    @staticmethod
    def price_to_pips(price_diff: float, pair: str) -> float:
        """Convert price difference to pips."""
        if "JPY" in pair:
            return round(price_diff / 0.01, 1)
        return round(price_diff / 0.0001, 1)

    # ── Trend State ────────────────────────────────────────────

    def get_trend_state(self, df: pd.DataFrame) -> str:
        """
        Returns current trend based on EMA alignment on latest candle.
        Requires EMAs to be calculated first (add_emas or add_all_indicators).

        Returns: "bullish" | "bearish" | "ranging"
        """
        if df.empty or len(df) < EMA_LONG:
            return "ranging"

        last = df.iloc[-1]
        e21  = last.get(f"ema_{EMA_SHORT}")
        e50  = last.get(f"ema_{EMA_MID}")
        e200 = last.get(f"ema_{EMA_LONG}")

        if e21 is None or e50 is None or e200 is None:
            return "ranging"

        if e21 > e50 > e200:
            return "bullish"
        elif e21 < e50 < e200:
            return "bearish"
        else:
            return "ranging"

    def print_snapshot(self, pair: str):
        """Prints a quick market snapshot: trend, RSI, ATR, Asian range."""
        df = self.get_dataframe(pair, granularity="H1", count=250)
        if df.empty:
            print(f"[MarketData] No data for {pair}")
            return

        df = self.add_all_indicators(df)
        last    = df.iloc[-1]
        trend   = self.get_trend_state(df)
        asian   = self.get_asian_range(pair)

        print(f"\n{'='*50}")
        print(f"  MARKET SNAPSHOT — {pair}")
        print(f"{'='*50}")
        print(f"  Price    : {last['close']:.5f}")
        print(f"  EMA 21   : {last[f'ema_{EMA_SHORT}']:.5f}")
        print(f"  EMA 50   : {last[f'ema_{EMA_MID}']:.5f}")
        print(f"  EMA 200  : {last[f'ema_{EMA_LONG}']:.5f}")
        print(f"  RSI(14)  : {last['rsi']:.1f}")
        print(f"  ATR(14)  : {last['atr']:.5f}  ({self.price_to_pips(last['atr'], pair):.1f} pips)")
        print(f"  Trend    : {trend.upper()}")
        if asian:
            print(f"  Asian Hi : {asian['high']:.5f}")
            print(f"  Asian Lo : {asian['low']:.5f}")
            print(f"  Range    : {asian['range_pips']} pips")
        print()