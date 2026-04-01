"""
Backtest Engine — Event-Driven, Walk-Forward

Fixes vs v1:
  1. Asian range now correctly uses 22:00 (prev day) → 07:00 (current day)
     by passing the full df to _simulate_day instead of just the day slice.
  2. Trend alignment filter added to StrategyParams (require_trend_alignment).
     When True, only takes breakout signals that align with EMA 21/50/200 stack.
  3. Regime classification improved — uses EMA 50/200 separation, not 21/200.
"""

import pandas as pd
import numpy as np
from datetime import datetime, timezone, timedelta, date
from dataclasses import dataclass, field

from config import (
    EMA_SHORT, EMA_MID, EMA_LONG, RSI_PERIOD,
    BREAKOUT_BUFFER_PIPS, MIN_REWARD_RISK,
    RISK_PER_TRADE,
    TRAIL_TRIGGER_R, TRAIL_LOCK_R, PARTIAL_CLOSE_R, PARTIAL_CLOSE_PCT, FULL_TP_R,
    BREAKOUT_ASIAN_MIN_PIPS, MOMENTUM_BODY_RATIO,
)


# ── Data Structures ────────────────────────────────────────────

@dataclass
class BacktestTrade:
    pair:             str
    direction:        str
    entry_time:       datetime
    entry_price:      float
    stop_loss:        float
    take_profit:      float
    stop_pips:        float
    target_pips:      float
    rr_ratio:         float
    exit_time:        datetime = None
    exit_price:       float    = None
    outcome:          str      = None   # "win" | "loss" | "breakeven"
    pnl_pips:         float    = None
    pnl_pct:          float    = None
    regime:           str      = None
    trend_state:      str      = None   # "bullish" | "bearish" | "ranging"
    window_label:     str      = None
    asian_range_pips: float    = None


@dataclass
class WindowResult:
    label:       str
    window_type: str
    start:       datetime
    end:         datetime
    trades:      list = field(default_factory=list)

    @property
    def total(self):      return len(self.trades)
    @property
    def wins(self):       return sum(1 for t in self.trades if t.outcome in ("win", "partial_win"))
    @property
    def losses(self):     return sum(1 for t in self.trades if t.outcome == "loss")
    @property
    def win_rate(self):   return self.wins / self.total if self.total else 0
    @property
    def total_pips(self): return sum(t.pnl_pips for t in self.trades if t.pnl_pips)
    @property
    def profit_factor(self):
        gw = sum(t.pnl_pips for t in self.trades if t.pnl_pips and t.pnl_pips > 0)
        gl = abs(sum(t.pnl_pips for t in self.trades if t.pnl_pips and t.pnl_pips < 0))
        return round(gw / gl, 2) if gl else float("inf")
    @property
    def expectancy_pips(self):
        return round(self.total_pips / self.total, 2) if self.total else 0.0
    @property
    def max_drawdown(self):
        if not self.trades:
            return 0.0
        equity = np.cumsum([t.pnl_pips or 0 for t in self.trades])
        peak   = np.maximum.accumulate(equity)
        return round(float((peak - equity).max()), 1)


# ── Parameter Set ──────────────────────────────────────────────

@dataclass
class StrategyParams:
    breakout_buffer_pips:    float = BREAKOUT_BUFFER_PIPS
    min_range_pips:          float = 10.0
    max_range_pips:          float = 80.0
    reward_risk:             float = 2.5
    min_rr:                  float = MIN_REWARD_RISK
    ema_short:               int   = EMA_SHORT
    ema_mid:                 int   = EMA_MID
    ema_long:                int   = EMA_LONG
    rsi_period:              int   = RSI_PERIOD
    slippage_pips:           float = 1.5
    spread_pips:             float = 0.8

    # ── Trend filter ──────────────────────────────────────────
    # When True: only take breakouts aligned with EMA 21/50/200 stack.
    # Long breakout requires 21 > 50 > 200 (bullish).
    # Short breakout requires 21 < 50 < 200 (bearish).
    # Filters out counter-trend and ranging-market false breakouts.
    require_trend_alignment: bool  = False

    # ── Direction filter ──────────────────────────────────────
    # Controls which trade directions the strategy is allowed to take.
    # Default: both directions.
    # Set to ["buy"] to only take long breakouts — use for pairs with
    # a structural upside bias (e.g. GBP/USD in 2023-2026 dataset).
    # Set to ["sell"] to only take short breakouts.
    # This is pair-specific — pass a different StrategyParams per pair.
    allowed_directions: tuple = ("buy", "sell")

    # ── Exit management ───────────────────────────────────────
    # trail_trigger_r: when price moves this many R in profit, SL moves to BE+1pip.
    # partial_close_r: close partial_close_pct of position at this R level.
    # trail_lock_r:    after partial close, lock remaining SL at this R (e.g. 0.5R).
    # full_tp_r:       TP for the remaining position after partial close fires.
    # Set trail_trigger_r=0 and partial_close_r=0 to disable (original behaviour).
    trail_trigger_r:     float = 0.0
    trail_lock_r:        float = TRAIL_LOCK_R
    partial_close_r:     float = 0.0
    partial_close_pct:   float = PARTIAL_CLOSE_PCT
    full_tp_r:           float = FULL_TP_R

    # ── Entry quality ─────────────────────────────────────────
    # require_body_ratio: skip breakout bars where body < momentum_body_ratio × range.
    # Filters "wick breakouts" that close near entry — weak follow-through.
    require_body_ratio:   bool  = False
    momentum_body_ratio:  float = MOMENTUM_BODY_RATIO

    # ── Seasonality filters ───────────────────────────────────
    # allowed_weekdays: empty tuple = all days; (1,2,3,4) = Tue–Fri (Mon=0, Fri=4).
    # excluded_months:  empty tuple = no exclusions; (2,) = skip February.
    allowed_weekdays: tuple = ()
    excluded_months:  tuple = ()


# ── Main Engine ────────────────────────────────────────────────

class BacktestEngine:

    ASIAN_START_HOUR  = 22
    ASIAN_END_HOUR    = 7
    LONDON_OPEN_HOUR  = 7
    LONDON_CLOSE_HOUR = 9

    def __init__(self, df: pd.DataFrame, pair: str, params: StrategyParams = None):
        self.df     = df.copy()
        self.pair   = pair
        self.params = params or StrategyParams()
        self._prepare_data()

    # ── Data Prep ──────────────────────────────────────────────

    def _prepare_data(self):
        df = self.df
        p  = self.params

        df[f"ema_{p.ema_short}"] = df["close"].ewm(span=p.ema_short, adjust=False).mean()
        df[f"ema_{p.ema_mid}"]   = df["close"].ewm(span=p.ema_mid,   adjust=False).mean()
        df[f"ema_{p.ema_long}"]  = df["close"].ewm(span=p.ema_long,  adjust=False).mean()

        delta    = df["close"].diff()
        gain     = delta.clip(lower=0)
        loss     = (-delta).clip(lower=0)
        avg_gain = gain.ewm(alpha=1/p.rsi_period, min_periods=p.rsi_period, adjust=False).mean()
        avg_loss = loss.ewm(alpha=1/p.rsi_period, min_periods=p.rsi_period, adjust=False).mean()
        rs       = avg_gain / avg_loss.replace(0, float("nan"))
        df["rsi"] = 100 - (100 / (1 + rs))

        prev_close = df["close"].shift(1)
        tr = pd.concat([
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"]  - prev_close).abs(),
        ], axis=1).max(axis=1)
        df["atr"] = tr.ewm(alpha=1/14, min_periods=14, adjust=False).mean()

        # Trend state: clean EMA stack = directional, anything else = ranging
        e_s = df[f"ema_{p.ema_short}"]
        e_m = df[f"ema_{p.ema_mid}"]
        e_l = df[f"ema_{p.ema_long}"]
        df["trend_state"] = np.where(
            (e_s > e_m) & (e_m > e_l), "bullish",
            np.where((e_s < e_m) & (e_m < e_l), "bearish", "ranging")
        )

        # Regime: EMA 50/200 separation (more stable) + ATR for volatility
        pip                 = 0.01 if "JPY" in self.pair else 0.0001
        ema_sep_pips        = (e_m - e_l).abs() / pip
        atr_pips            = df["atr"] / pip
        df["regime"] = np.where(
            atr_pips > 25,          "volatile",
            np.where(ema_sep_pips > 15, "trending", "ranging")
        )

        self.df = df

    # ── Walk-Forward ───────────────────────────────────────────

    def run_walk_forward(
        self,
        train_months:    int = 6,
        validate_months: int = 2,
    ) -> list[WindowResult]:

        results      = []
        df           = self.df
        start        = df.index[0]
        end          = df.index[-1]
        window_start = start
        iteration    = 1

        while True:
            train_end    = window_start + pd.DateOffset(months=train_months)
            validate_end = train_end    + pd.DateOffset(months=validate_months)

            if train_end > end:
                break

            is_label  = f"IS_{window_start.strftime('%Y-%m')}_{train_end.strftime('%Y-%m')}"
            is_result = self._run_window(window_start, train_end, is_label, "in_sample")
            results.append(is_result)

            oos_end = min(validate_end, end)
            if train_end < oos_end:
                oos_label  = f"OOS_{train_end.strftime('%Y-%m')}_{oos_end.strftime('%Y-%m')}"
                oos_result = self._run_window(train_end, oos_end, oos_label, "out_of_sample")
                results.append(oos_result)
            else:
                oos_result = WindowResult(label="", window_type="out_of_sample",
                                          start=train_end, end=oos_end)

            print(f"  Walk-forward {iteration}: "
                  f"IS {is_result.total} trades (WR {is_result.win_rate:.0%}) | "
                  f"OOS {oos_result.total} trades "
                  f"(WR {oos_result.win_rate:.0%} | PF {oos_result.profit_factor})")

            window_start = window_start + pd.DateOffset(months=validate_months)
            iteration   += 1
            if validate_end >= end:
                break

        return results

    # ── Window Runner ──────────────────────────────────────────

    def _run_window(
        self,
        start:       datetime,
        end:         datetime,
        label:       str,
        window_type: str,
    ) -> WindowResult:
        """
        Runs the strategy day-by-day within the window.
        Passes each day to _simulate_day which reaches into self.df
        for the previous day's Asian session bars.
        """
        result       = WindowResult(label=label, window_type=window_type, start=start, end=end)
        window_df    = self.df[(self.df.index >= start) & (self.df.index < end)]
        trading_days = sorted(set(window_df.index.date))

        for day in trading_days:
            trade = self._simulate_day(day, label)
            if trade:
                result.trades.append(trade)

        return result

    # ── Single Day Simulation ──────────────────────────────────

    def _simulate_day(self, day: date, window_label: str) -> BacktestTrade | None:
        """
        Bar-by-bar simulation for one trading day.

        ASIAN RANGE FIX:
        The original code only used bars where hour < 7 on the current day.
        That captured at most 7 hours (midnight to 7am) and missed the 22:00-midnight
        portion from the previous day — the most important part of the Asian session
        where price consolidates after the NY close.

        The fix: pull previous day's bars from 22:00 onwards, combine with
        current day's bars before 07:00. This gives the full 9-hour window.
        A correctly sized Asian range means breakout levels are set at meaningful
        structural levels rather than noise.

        TREND FILTER:
        When require_trend_alignment=True, we check the EMA stack (21/50/200)
        at the exact bar where price crosses the breakout level. If a long signal
        fires but the EMAs aren't stacked bullish, we skip it. This is checked
        per-bar so there's no look-ahead — we only know the EMA values up to
        that bar, same as live trading.
        """
        p = self.params

        # ── Seasonality filters ────────────────────────────────
        if p.allowed_weekdays and day.weekday() not in p.allowed_weekdays:
            return None
        if p.excluded_months and day.month in p.excluded_months:
            return None

        # ── Asian range: prev day 22:00 + curr day 00:00–07:00 ─
        prev_day     = day - timedelta(days=1)
        prev_bars    = self.df[self.df.index.date == prev_day]
        prev_session = prev_bars[prev_bars.index.hour >= self.ASIAN_START_HOUR]

        curr_bars    = self.df[self.df.index.date == day]
        curr_session = curr_bars[curr_bars.index.hour < self.ASIAN_END_HOUR]

        asian_bars = pd.concat([prev_session, curr_session])

        if len(asian_bars) < 4:
            return None

        asian_high = asian_bars["high"].max()
        asian_low  = asian_bars["low"].min()
        range_pips = self._price_to_pips(asian_high - asian_low)

        if range_pips < p.min_range_pips or range_pips > p.max_range_pips:
            return None

        pip_size    = self._pips_to_price(p.breakout_buffer_pips)
        long_entry  = asian_high + pip_size
        short_entry = asian_low  - pip_size

        # ── London open bars 07:00–09:00 ──────────────────────
        london_bars = curr_bars[
            (curr_bars.index.hour >= self.LONDON_OPEN_HOUR) &
            (curr_bars.index.hour <  self.LONDON_CLOSE_HOUR)
        ]

        if london_bars.empty:
            return None

        direction    = None
        entry_price  = None
        entry_time   = None
        entry_regime = None
        entry_trend  = None

        for ts, bar in london_bars.iterrows():
            bar_trend = bar.get("trend_state", "ranging")

            if bar["high"] >= long_entry and direction is None:
                # Direction filter — skip if longs not allowed
                if "buy" not in p.allowed_directions:
                    continue
                if p.require_trend_alignment and bar_trend != "bullish":
                    continue
                # Momentum body ratio — require strong close on breakout bar
                if p.require_body_ratio:
                    bar_range = bar["high"] - bar["low"]
                    bar_body  = abs(bar["close"] - bar["open"])
                    if bar_range > 0 and bar_body / bar_range < p.momentum_body_ratio:
                        continue
                direction    = "buy"
                entry_price  = long_entry + self._pips_to_price(
                    p.slippage_pips + p.spread_pips / 2)
                entry_time   = ts
                entry_regime = bar.get("regime", "unknown")
                entry_trend  = bar_trend
                break

            elif bar["low"] <= short_entry and direction is None:
                # Direction filter — skip if shorts not allowed
                if "sell" not in p.allowed_directions:
                    continue
                if p.require_trend_alignment and bar_trend != "bearish":
                    continue
                # Momentum body ratio — require strong close on breakout bar
                if p.require_body_ratio:
                    bar_range = bar["high"] - bar["low"]
                    bar_body  = abs(bar["close"] - bar["open"])
                    if bar_range > 0 and bar_body / bar_range < p.momentum_body_ratio:
                        continue
                direction    = "sell"
                entry_price  = short_entry - self._pips_to_price(
                    p.slippage_pips + p.spread_pips / 2)
                entry_time   = ts
                entry_regime = bar.get("regime", "unknown")
                entry_trend  = bar_trend
                break

        if not direction:
            return None

        if direction == "buy":
            stop_loss   = asian_low  - pip_size
            take_profit = entry_price + (entry_price - stop_loss) * p.reward_risk
        else:
            stop_loss   = asian_high + pip_size
            take_profit = entry_price - (stop_loss - entry_price) * p.reward_risk

        stop_pips   = self._price_to_pips(abs(entry_price - stop_loss))
        target_pips = self._price_to_pips(abs(take_profit - entry_price))
        rr          = target_pips / stop_pips if stop_pips > 0 else 0

        if rr < p.min_rr:
            return None

        # ── Outcome simulation — full day after entry ──────────
        post_entry_bars = curr_bars[curr_bars.index > entry_time]

        # Pre-compute exit management levels
        r1_dist = abs(entry_price - stop_loss)  # 1R in price units

        trail_sl = stop_loss
        partial_closed   = False
        partial_pnl_pips = 0.0   # pips locked in at partial close

        trail_trigger_px = None
        partial_close_px = None
        full_tp_px       = None

        if p.trail_trigger_r > 0:
            trail_trigger_px = (
                entry_price + r1_dist * p.trail_trigger_r if direction == "buy"
                else entry_price - r1_dist * p.trail_trigger_r
            )

        if p.partial_close_r > 0:
            partial_close_px = (
                entry_price + r1_dist * p.partial_close_r if direction == "buy"
                else entry_price - r1_dist * p.partial_close_r
            )
            full_tp_px = (
                entry_price + r1_dist * p.full_tp_r if direction == "buy"
                else entry_price - r1_dist * p.full_tp_r
            )

        exit_price       = None
        exit_time        = None
        outcome          = None
        pnl_pips_blended = None   # set when partial close is involved

        for ts, bar in post_entry_bars.iterrows():
            if direction == "buy":
                # 1. Update trailing stop to break-even
                if trail_trigger_px and bar["high"] >= trail_trigger_px:
                    be_px = entry_price + self._pips_to_price(1)
                    if be_px > trail_sl:
                        trail_sl = be_px

                # 2. Partial close trigger
                if partial_close_px and not partial_closed and bar["high"] >= partial_close_px:
                    partial_pnl_pips = self._price_to_pips(partial_close_px - entry_price)
                    partial_closed   = True
                    if p.trail_lock_r > 0:
                        lock_px = entry_price + r1_dist * p.trail_lock_r
                        if lock_px > trail_sl:
                            trail_sl = lock_px

                # 3. Check SL hit (may be trailing)
                if bar["low"] <= trail_sl:
                    sl_exit = trail_sl - self._pips_to_price(p.slippage_pips)
                    if partial_closed:
                        remaining = self._price_to_pips(trail_sl - entry_price)
                        pnl_pips_blended = (p.partial_close_pct * partial_pnl_pips
                                            + (1 - p.partial_close_pct) * remaining)
                        outcome = "partial_win" if pnl_pips_blended > 0 else "loss"
                    else:
                        original_sl = abs(trail_sl - stop_loss) < 1e-8
                        outcome = "loss" if original_sl else "breakeven"
                    exit_price = sl_exit
                    exit_time  = ts
                    break

                # 4. Check TP hit
                effective_tp = full_tp_px if partial_closed else take_profit
                if bar["high"] >= effective_tp:
                    exit_price = effective_tp
                    exit_time  = ts
                    if partial_closed:
                        remaining = self._price_to_pips(full_tp_px - entry_price)
                        pnl_pips_blended = (p.partial_close_pct * partial_pnl_pips
                                            + (1 - p.partial_close_pct) * remaining)
                    outcome = "win"
                    break

            else:  # sell
                # 1. Update trailing stop to break-even
                if trail_trigger_px and bar["low"] <= trail_trigger_px:
                    be_px = entry_price - self._pips_to_price(1)
                    if be_px < trail_sl:
                        trail_sl = be_px

                # 2. Partial close trigger
                if partial_close_px and not partial_closed and bar["low"] <= partial_close_px:
                    partial_pnl_pips = self._price_to_pips(entry_price - partial_close_px)
                    partial_closed   = True
                    if p.trail_lock_r > 0:
                        lock_px = entry_price - r1_dist * p.trail_lock_r
                        if lock_px < trail_sl:
                            trail_sl = lock_px

                # 3. Check SL hit (may be trailing)
                if bar["high"] >= trail_sl:
                    sl_exit = trail_sl + self._pips_to_price(p.slippage_pips)
                    if partial_closed:
                        remaining = self._price_to_pips(entry_price - trail_sl)
                        pnl_pips_blended = (p.partial_close_pct * partial_pnl_pips
                                            + (1 - p.partial_close_pct) * remaining)
                        outcome = "partial_win" if pnl_pips_blended > 0 else "loss"
                    else:
                        original_sl = abs(trail_sl - stop_loss) < 1e-8
                        outcome = "loss" if original_sl else "breakeven"
                    exit_price = sl_exit
                    exit_time  = ts
                    break

                # 4. Check TP hit
                effective_tp = full_tp_px if partial_closed else take_profit
                if bar["low"] <= effective_tp:
                    exit_price = effective_tp
                    exit_time  = ts
                    if partial_closed:
                        remaining = self._price_to_pips(entry_price - full_tp_px)
                        pnl_pips_blended = (p.partial_close_pct * partial_pnl_pips
                                            + (1 - p.partial_close_pct) * remaining)
                    outcome = "win"
                    break

        # Day-close exit
        if outcome is None and not post_entry_bars.empty:
            exit_price = post_entry_bars.iloc[-1]["close"]
            exit_time  = post_entry_bars.index[-1]
            if partial_closed:
                if direction == "buy":
                    remaining = self._price_to_pips(exit_price - entry_price)
                else:
                    remaining = self._price_to_pips(entry_price - exit_price)
                pnl_pips_blended = (p.partial_close_pct * partial_pnl_pips
                                    + (1 - p.partial_close_pct) * remaining)
                outcome = "partial_win" if pnl_pips_blended > 0 else "breakeven"
            else:
                outcome = "breakeven"

        if exit_price is None:
            return None

        if pnl_pips_blended is not None:
            pnl_pips = pnl_pips_blended
        elif direction == "buy":
            pnl_pips = self._price_to_pips(exit_price - entry_price)
        else:
            pnl_pips = self._price_to_pips(entry_price - exit_price)

        pnl_pct = (pnl_pips / stop_pips) * RISK_PER_TRADE if stop_pips > 0 else 0

        return BacktestTrade(
            pair=self.pair,
            direction=direction,
            entry_time=entry_time,
            entry_price=entry_price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            stop_pips=stop_pips,
            target_pips=target_pips,
            rr_ratio=round(rr, 2),
            exit_time=exit_time,
            exit_price=exit_price,
            outcome=outcome,
            pnl_pips=round(pnl_pips, 1),
            pnl_pct=round(pnl_pct * 100, 3),
            regime=entry_regime,
            trend_state=entry_trend,
            window_label=window_label,
            asian_range_pips=round(range_pips, 1),
        )

    # ── Pip Utilities ──────────────────────────────────────────

    def _pips_to_price(self, pips: float) -> float:
        return pips * (0.01 if "JPY" in self.pair else 0.0001)

    def _price_to_pips(self, price: float) -> float:
        return price / (0.01 if "JPY" in self.pair else 0.0001)

    # ── Summary Output ─────────────────────────────────────────

    def print_summary(self, results: list[WindowResult]):
        oos_results    = [r for r in results if r.window_type == "out_of_sample"]
        all_oos_trades = [t for r in oos_results for t in r.trades]

        trend_label = "  [trend filter ON]" if self.params.require_trend_alignment else ""
        print(f"\n{'='*65}")
        print(f"  WALK-FORWARD SUMMARY — {self.pair}{trend_label}")
        print(f"{'='*65}")
        print(f"  {'Window':<32} {'T':<4} {'N':>5} {'WR':>6} {'PF':>6} {'Pips':>8} {'MaxDD':>7}")
        print(f"  {'-'*63}")

        for r in results:
            t        = "IS" if r.window_type == "in_sample" else "OS"
            pips_str = f"{r.total_pips:+.1f}"
            print(f"  {r.label:<32} {t:<4} {r.total:>5} {r.win_rate:>5.0%} "
                  f"{r.profit_factor:>6.2f} {pips_str:>8} {r.max_drawdown:>7.1f}")

        if all_oos_trades:
            oos_wins  = sum(1 for t in all_oos_trades if t.outcome in ("win", "partial_win"))
            oos_wr    = oos_wins / len(all_oos_trades)
            oos_pips  = sum(t.pnl_pips for t in all_oos_trades if t.pnl_pips)
            gw        = sum(t.pnl_pips for t in all_oos_trades if t.pnl_pips and t.pnl_pips > 0)
            gl        = abs(sum(t.pnl_pips for t in all_oos_trades if t.pnl_pips and t.pnl_pips < 0))
            oos_pf    = round(gw / gl, 2) if gl else float("inf")
            equity    = np.cumsum([t.pnl_pips or 0 for t in all_oos_trades])
            peak      = np.maximum.accumulate(equity)
            oos_maxdd = round(float((peak - equity).max()), 1)

            print(f"\n  {'─'*63}")
            print(f"  {'OOS AGGREGATE':<32} {'':4} {len(all_oos_trades):>5} {oos_wr:>5.0%} "
                  f"{oos_pf:>6.2f} {oos_pips:>+8.1f} {oos_maxdd:>7.1f}")

        self._print_regime_breakdown(all_oos_trades)
        self._print_trend_breakdown(all_oos_trades)
        self._print_stability_check(oos_results)
        print()

    def _print_regime_breakdown(self, trades: list):
        if not trades:
            return
        print(f"\n  REGIME BREAKDOWN (OOS)")
        print(f"  {'Regime':<12} {'N':>5} {'WR':>7} {'Pips':>10} {'Exp/trade':>10}")
        print(f"  {'-'*47}")
        for regime in ["trending", "ranging", "volatile"]:
            rt = [t for t in trades if t.regime == regime]
            if not rt:
                continue
            wins = sum(1 for t in rt if t.outcome in ("win", "partial_win"))
            pips = sum(t.pnl_pips for t in rt if t.pnl_pips)
            print(f"  {regime:<12} {len(rt):>5} {wins/len(rt):>6.0%} "
                  f"{pips:>+10.1f} {pips/len(rt):>+10.2f}")

    def _print_trend_breakdown(self, trades: list):
        if not trades:
            return
        print(f"\n  TREND STATE AT ENTRY (OOS)")
        print(f"  {'Trend':<10} {'N':>5} {'WR':>7} {'Pips':>10} {'Exp/trade':>10}")
        print(f"  {'-'*45}")
        for ts in ["bullish", "bearish", "ranging"]:
            tt = [t for t in trades if t.trend_state == ts]
            if not tt:
                continue
            wins = sum(1 for t in tt if t.outcome in ("win", "partial_win"))
            pips = sum(t.pnl_pips for t in tt if t.pnl_pips)
            print(f"  {ts:<10} {len(tt):>5} {wins/len(tt):>6.0%} "
                  f"{pips:>+10.1f} {pips/len(tt):>+10.2f}")
        print(f"\n  → If bullish/bearish outperform ranging: set require_trend_alignment=True")

    def _print_stability_check(self, oos_results: list):
        print(f"\n  STABILITY CHECK")
        print(f"  {'Window':<35} Result")
        print(f"  {'-'*52}")
        for r in oos_results:
            if r.total == 0:
                status = "⚪ No trades"
            elif r.profit_factor >= 1.5 and r.win_rate >= 0.40:
                status = "🟢 Robust"
            elif r.profit_factor >= 1.0:
                status = "🟡 Marginal"
            else:
                status = "🔴 Failing"
            print(f"  {r.label:<35} {status}")

    def export_trades(self, results: list, path: str = "logs/backtest_trades.csv"):
        import os
        all_trades = [t for r in results for t in r.trades]
        if not all_trades:
            print("[Backtest] No trades to export.")
            return
        os.makedirs("logs", exist_ok=True)
        rows = [{
            "pair":             t.pair,
            "direction":        t.direction,
            "entry_time":       t.entry_time,
            "entry_price":      t.entry_price,
            "stop_loss":        t.stop_loss,
            "take_profit":      t.take_profit,
            "exit_time":        t.exit_time,
            "exit_price":       t.exit_price,
            "outcome":          t.outcome,
            "pnl_pips":         t.pnl_pips,
            "pnl_pct":          t.pnl_pct,
            "regime":           t.regime,
            "trend_state":      t.trend_state,
            "asian_range_pips": t.asian_range_pips,
            "window":           t.window_label,
            "rr_ratio":         t.rr_ratio,
        } for t in all_trades]
        pd.DataFrame(rows).to_csv(path, index=False)
        print(f"[Backtest] {len(rows)} trades exported to {path}")