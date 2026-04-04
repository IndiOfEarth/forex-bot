import pandas as pd
from datetime import datetime, timezone, timedelta
from oanda.client import OandaClient
from config import (
    DEFAULT_GRANULARITY, BREAKOUT_GRANULARITY,
    BREAKOUT_ASIAN_START, BREAKOUT_ASIAN_END,
    EMA_SHORT, EMA_MID, EMA_LONG, RSI_PERIOD,
    ATR_VOLATILITY_MULTIPLIER,
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

    # ── Macro Regime ───────────────────────────────────────────

    def get_atr_regime(self, pair: str) -> dict:
        """
        Compares the current H1 ATR to its 20-bar SMA to detect crisis volatility.
        Returns:
            {
                "is_high_vol": bool,
                "current_atr": float,
                "atr_sma":     float,
                "ratio":       float,   # current_atr / atr_sma
            }
        A ratio > ATR_VOLATILITY_MULTIPLIER (default 2.0) flags a high-vol regime.
        """
        df = self.get_dataframe(pair, granularity="H1", count=60)
        if df.empty or len(df) < 34:   # ATR(14) needs 14 bars + 20 for SMA
            return {"is_high_vol": False, "current_atr": 0.0, "atr_sma": 0.0, "ratio": 1.0}
        df = self.add_atr(df)
        df["atr_sma"] = df["atr"].rolling(20).mean()
        last = df.dropna(subset=["atr_sma"]).iloc[-1]
        ratio = last["atr"] / last["atr_sma"] if last["atr_sma"] > 0 else 1.0
        return {
            "is_high_vol": ratio > ATR_VOLATILITY_MULTIPLIER,
            "current_atr": round(float(last["atr"]), 6),
            "atr_sma":     round(float(last["atr_sma"]), 6),
            "ratio":       round(ratio, 2),
        }

    def get_daily_trend_state(self, pair: str) -> str:
        """
        EMA 50/200 stack on D1 candles — multi-month regime filter.
        Returns "bullish" | "bearish" | "ranging".

        Uses EMA 50 vs 200 only (not 21): the 21 on D1 covers ~3 weeks and
        adds noise; 50/200 reflects the slow macro regime (golden/death cross).
        Requires 250 daily candles (~1 year) for EMA 200 to be valid.
        """
        df = self.get_dataframe(pair, granularity="D", count=250)
        if df.empty or len(df) < EMA_LONG:
            print(f"  [MarketData] Insufficient D1 data for {pair} — defaulting to ranging")
            return "ranging"
        df   = self.add_emas(df)
        last = df.iloc[-1]
        e50  = last[f"ema_{EMA_MID}"]
        e200 = last[f"ema_{EMA_LONG}"]
        if e50 > e200:
            return "bullish"
        elif e50 < e200:
            return "bearish"
        else:
            return "ranging"

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

    def get_overnight_range(self, pair: str, range_start_hour: int, range_end_hour: int) -> dict | None:
        """
        Calculates the high/low range for a window that spans midnight.
        e.g. range_start_hour=20, range_end_hour=2 captures prev-day 20:00–23:59
        plus current-day 00:00–01:59 UTC.

        Used by Tokyo Breakout: call with range_start_hour=20, range_end_hour=2.
        Returns None if the range period has not yet closed.
        """
        now = datetime.now(timezone.utc)

        # Range closes at range_end_hour on the current day
        range_close = now.replace(hour=range_end_hour, minute=0, second=0, microsecond=0)
        if now < range_close:
            print(f"[MarketData] Overnight range {range_start_hour:02d}:00–{range_end_hour:02d}:00 UTC not yet closed.")
            return None

        # Fetch enough M15 bars to cover the full overnight window
        # Window = (24 - range_start_hour) + range_end_hour hours, +buffer
        window_hours = (24 - range_start_hour) + range_end_hour
        bars_needed  = window_hours * 4 + 10
        df = self.get_dataframe(pair, granularity="M15", count=bars_needed)
        if df.empty:
            return None

        session_start = (range_close - timedelta(hours=window_hours))
        session_end   = range_close

        range_df = df[(df.index >= session_start) & (df.index < session_end)]
        if range_df.empty or len(range_df) < 4:
            print(f"[MarketData] Insufficient overnight range bars for {pair}")
            return None

        high = range_df["high"].max()
        low  = range_df["low"].min()
        pip_divisor = 0.01 if "JPY" in pair else 0.0001
        range_pips  = round((high - low) / pip_divisor, 1)

        return {
            "high":          high,
            "low":           low,
            "range_pips":    range_pips,
            "session_start": session_start,
            "session_end":   session_end,
        }

    def get_session_range(self, pair: str, start_hour: int, end_hour: int) -> dict | None:
        """
        Calculates the high/low range for a same-day UTC hour window.
        All bars must fall within [start_hour, end_hour) on the current day.

        Used by NY Breakout: call with start_hour=9, end_hour=13 to get the
        European morning consolidation range (post-London-open).

        Returns the same dict shape as get_asian_range().
        """
        now = datetime.now(timezone.utc)
        session_end   = now.replace(hour=end_hour,   minute=0, second=0, microsecond=0)
        session_start = now.replace(hour=start_hour, minute=0, second=0, microsecond=0)

        if now < session_end:
            print(f"[MarketData] Session {start_hour:02d}:00–{end_hour:02d}:00 UTC not yet closed.")
            return None

        bars_needed = int((end_hour - start_hour) * 4) + 5   # 4 M15 bars/hr + buffer
        df = self.get_dataframe(pair, granularity="M15", count=bars_needed)
        if df.empty:
            return None

        session_df = df[(df.index >= session_start) & (df.index < session_end)]
        if session_df.empty:
            print(f"[MarketData] No candles found for {pair} in {start_hour:02d}:00–{end_hour:02d}:00 UTC")
            return None

        high = session_df["high"].max()
        low  = session_df["low"].min()
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