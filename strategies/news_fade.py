"""
News Fade Strategy — backtest-only.

Fades large M15 spike bars during the London/NY session (07:00–15:00 UTC)
on EUR/USD and GBP/USD. A spike bar is defined as one whose range exceeds
FADE_MIN_SPIKE_PIPS. Entry is a limit order at the 61.8% retracement of the
spike; stop is beyond the spike extreme; TP is the pre-spike open price.

Not wired into main.py. Use backtest/run_news_fade_backtest.py to validate
before live deployment.
"""

from datetime import datetime
from dataclasses import dataclass

from oanda.market_data import MarketData
from config import (
    FADE_MIN_SPIKE_PIPS,
    FADE_FIBO_LEVEL,
    FADE_STOP_BUFFER_PIPS,
    FADE_TP_RETRACEMENT,
)

FADE_PAIRS       = ["EUR_USD", "GBP_USD"]
ENTRY_START_HOUR = 7   # UTC — London open
ENTRY_END_HOUR   = 15  # UTC — after NY open overlap


@dataclass
class FadeSignal:
    pair:        str
    direction:   str    # "buy" | "sell"
    entry_price: float  # limit entry at Fibonacci retracement level
    stop_loss:   float
    take_profit: float
    stop_pips:   float
    target_pips: float
    rr_ratio:    float
    spike_high:  float
    spike_low:   float
    spike_open:  float
    timestamp:   datetime


class NewsFade:
    """
    Detects spike bars on the last closed M15 candle and returns a fade signal.

    Usage (backtest engine calls this per bar):
        fade = NewsFade()
        signal = fade.scan(pair, last_closed_bar, current_time)
    """

    def scan(
        self,
        pair:         str,
        bar:          dict,   # {"open", "high", "low", "close", "time"}
        current_time: datetime,
    ) -> FadeSignal | None:
        if pair not in FADE_PAIRS:
            return None
        if not (ENTRY_START_HOUR <= current_time.hour < ENTRY_END_HOUR):
            return None

        pip_size = 0.01 if "JPY" in pair else 0.0001
        bar_range_pips = (bar["high"] - bar["low"]) / pip_size

        if bar_range_pips < FADE_MIN_SPIKE_PIPS:
            return None

        is_bullish = bar["close"] > bar["open"]

        if is_bullish:
            # Fade a bullish spike — sell at 61.8% retracement from the top
            direction   = "sell"
            entry_price = bar["high"] - FADE_FIBO_LEVEL * (bar["high"] - bar["low"])
            stop_loss   = bar["high"] + FADE_STOP_BUFFER_PIPS * pip_size
            take_profit = bar["open"] + FADE_TP_RETRACEMENT * (bar["high"] - bar["open"])
        else:
            # Fade a bearish spike — buy at 61.8% retracement from the bottom
            direction   = "buy"
            entry_price = bar["low"] + FADE_FIBO_LEVEL * (bar["high"] - bar["low"])
            stop_loss   = bar["low"] - FADE_STOP_BUFFER_PIPS * pip_size
            take_profit = bar["open"] - FADE_TP_RETRACEMENT * (bar["open"] - bar["low"])

        stop_pips   = abs(entry_price - stop_loss) / pip_size
        target_pips = abs(take_profit - entry_price) / pip_size

        if stop_pips <= 0 or target_pips <= 0:
            return None

        rr_ratio = round(target_pips / stop_pips, 2)

        return FadeSignal(
            pair        = pair,
            direction   = direction,
            entry_price = round(entry_price, 5),
            stop_loss   = round(stop_loss, 5),
            take_profit = round(take_profit, 5),
            stop_pips   = round(stop_pips, 1),
            target_pips = round(target_pips, 1),
            rr_ratio    = rr_ratio,
            spike_high  = bar["high"],
            spike_low   = bar["low"],
            spike_open  = bar["open"],
            timestamp   = current_time,
        )
