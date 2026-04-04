from datetime import datetime, timezone
from dataclasses import dataclass

from oanda.client import OandaClient
from oanda.market_data import MarketData
from risk.manager import RiskManager
from econ_calendar.filter import is_in_blackout
from config import (
    BREAKOUT_BUFFER_PIPS,
    MIN_REWARD_RISK,
    EMA_SHORT, EMA_MID, EMA_LONG,
    BREAKOUT_ASIAN_MIN_PIPS,
    MOMENTUM_BODY_RATIO,
    ATR_VOLATILITY_MULTIPLIER, ATR_HIGH_VOL_SIZE_SCALAR, ATR_BLOCK_ON_HIGH_VOL,
)


@dataclass
class TokyoSignal:
    pair:             str
    direction:        str        # "buy" | "sell"
    entry_price:      float
    stop_loss:        float
    take_profit:      float
    stop_pips:        float
    target_pips:      float
    rr_ratio:         float
    range_high:       float
    range_low:        float
    range_pips:       float
    trend_state:      str        # "bullish" | "bearish" | "ranging"
    timestamp:        datetime
    vol_scalar:       float = 1.0


# ── Per-pair configuration ─────────────────────────────────────
# Derived from walk-forward backtest (Phase G, Tokyo session).
# DO NOT change without re-running the backtest.
#
# EUR/JPY: tokyo_london_exit config — OOS PF 1.94, 118 trades.
#          Force-close at 07:00 UTC before London open takes over the trend.
#          Win rate 56% (highest of the set) because time-exit locks in small
#          gains before adverse London opening moves can reverse positions.
#
# USD/JPY: tokyo_no_friday config — OOS PF 1.87, 127 trades.
#          No forced time exit — trailing stop + partial close manages exit
#          (same as London breakout). PF 1.87 vs 1.74 with london_exit —
#          USD/JPY trades extend well past 07:00, unlike EUR/JPY.
#          Mon-Thu only for both pairs; Friday Tokyo win rate is ~40% lower.
#
# AUD/USD: REJECTED — best OOS PF 1.08 across all Tokyo configs.
#          AUD/USD lacks clean consolidation structure in the 20:00–02:00 UTC
#          window; range boundaries are too noisy for breakout entries.
#
TOKYO_PAIR_CONFIG = {
    "EUR_JPY": {
        "allowed_directions":      ("buy", "sell"),
        "require_trend_alignment": True,
        "first_bar_minutes":       0,
        "allowed_weekdays":        (0, 1, 2, 3),  # Mon–Thu
        "time_exit_hour":          7,              # force-close before London open
    },
    "USD_JPY": {
        "allowed_directions":      ("buy", "sell"),
        "require_trend_alignment": True,
        "first_bar_minutes":       0,
        "allowed_weekdays":        (0, 1, 2, 3),  # Mon–Thu
        "time_exit_hour":          0,              # trailing stop manages exit
    },
}


class TokyoBreakout:
    """
    Tokyo Session Breakout Strategy — backtest-validated configuration.

    Consolidation range: 20:00–02:00 UTC (NY close → Sydney → early Tokyo).
    Entry window:        02:00–06:00 UTC (peak Tokyo liquidity).
    Validated pairs:     EUR/JPY (PF 1.94), USD/JPY (PF 1.87).

    Logic mirrors LondonBreakout:
      1. Build overnight range (prev-day 20:00 → curr-day 02:00)
      2. At 02:00 UTC: long entry  = range_high + buffer_pips
                       short entry = range_low  - buffer_pips
      3. Signal fires when live price crosses either level (02:00–06:00 UTC)
      4. Trend filter: EMA 21/50/200 stack must align with signal direction
      5. Stop: opposite side of range + buffer
      6. TP:   entry + (stop_distance × 2.5R)
      7. EUR/JPY: force-close positions at 07:00 UTC before London open
    """

    MIN_RANGE_PIPS   = BREAKOUT_ASIAN_MIN_PIPS   # 20 pips
    MAX_RANGE_PIPS   = 80
    REWARD_RISK      = 2.5
    TOKYO_OPEN_HOUR  = 2    # entry window starts 02:00 UTC
    TOKYO_CLOSE_HOUR = 6    # entry window ends   06:00 UTC

    # Overnight range window: prev-day >= 20:00, curr-day < 02:00
    RANGE_START_HOUR = 20
    RANGE_END_HOUR   = 2

    def __init__(
        self,
        client:       OandaClient,
        market_data:  MarketData,
        risk_manager: RiskManager,
        pairs:        list[str] = None,
    ):
        self.client = client
        self.md     = market_data
        self.risk   = risk_manager
        self.pairs  = pairs or ["EUR_JPY", "USD_JPY"]
        self._fired_today: set[str] = set()

    # ── Main Entry Point ───────────────────────────────────────

    def scan(self, events: list[dict], bias: dict = None, open_trades: list[dict] = None) -> list[TokyoSignal]:
        """
        Scans configured pairs for Tokyo session breakout signals.
        Call this at or after 02:00 UTC, before 06:00 UTC.
        """
        now     = datetime.now(timezone.utc)
        signals = []

        if not self._in_entry_window(now):
            print(f"[TokyoBreakout] Outside window (02:00–06:00 UTC). "
                  f"Current: {now.strftime('%H:%M')} UTC")
            return signals

        for pair in self.pairs:
            if pair in self._fired_today:
                print(f"[TokyoBreakout] {pair} — already fired today.")
                continue

            print(f"\n[TokyoBreakout] Scanning {pair}...")
            signal = self._evaluate_pair(pair, events, bias, now, open_trades)

            if signal:
                signals.append(signal)
                self._print_signal(signal)

        return signals

    def get_positions_to_close(self, open_trades: list[dict]) -> list[dict]:
        """
        Returns trades that should be force-closed at 07:00 UTC (EUR/JPY only).
        Call this once per cycle at or after 07:00 UTC.
        """
        now      = datetime.now(timezone.utc)
        to_close = []
        for trade in (open_trades or []):
            pair = trade.get("instrument", "").replace("/", "_")
            cfg  = TOKYO_PAIR_CONFIG.get(pair, {})
            exit_hour = cfg.get("time_exit_hour", 0)
            if exit_hour and now.hour >= exit_hour:
                # Only close if this was a Tokyo session trade (entry before 06:00)
                entry_time_str = trade.get("openTime", "")
                if entry_time_str:
                    try:
                        from datetime import datetime as dt
                        entry_dt = dt.fromisoformat(entry_time_str.replace("Z", "+00:00"))
                        if 2 <= entry_dt.hour < 6:
                            to_close.append(trade)
                    except Exception:
                        pass
        return to_close

    # ── Pair Evaluation ────────────────────────────────────────

    def _evaluate_pair(
        self,
        pair:        str,
        events:      list[dict],
        bias:        dict | None,
        now:         datetime,
        open_trades: list[dict] = None,
    ) -> TokyoSignal | None:

        cfg = TOKYO_PAIR_CONFIG.get(pair, {
            "allowed_directions":      ("buy", "sell"),
            "require_trend_alignment": True,
            "first_bar_minutes":       0,
            "allowed_weekdays":        (),
            "time_exit_hour":          0,
        })
        allowed_directions      = cfg["allowed_directions"]
        require_trend_alignment = cfg["require_trend_alignment"]
        first_bar_minutes       = cfg.get("first_bar_minutes", 0)
        allowed_weekdays        = cfg.get("allowed_weekdays", ())

        # 0. Weekday filter
        if allowed_weekdays and now.weekday() not in allowed_weekdays:
            day_name = now.strftime("%A")
            print(f"  ❌ Skipped — {day_name} not in allowed trading days")
            return None

        # 1. First-bar filter
        if first_bar_minutes > 0:
            minutes_past_open = now.hour * 60 + now.minute - self.TOKYO_OPEN_HOUR * 60
            if minutes_past_open >= first_bar_minutes:
                print(f"  ❌ Outside first-bar window "
                      f"({minutes_past_open}min past open, limit {first_bar_minutes}min)")
                return None

        # 2. News blackout
        blocked, reason = is_in_blackout(events, now=now)
        if blocked:
            print(f"  ❌ Blocked — {reason}")
            return None

        # 3. Overnight consolidation range (20:00 prev day → 02:00 today)
        session = self.md.get_overnight_range(
            pair, self.RANGE_START_HOUR, self.RANGE_END_HOUR
        )
        if not session:
            print(f"  ❌ Overnight range unavailable.")
            return None

        range_pips = session["range_pips"]

        if range_pips < self.MIN_RANGE_PIPS:
            print(f"  ❌ Range too narrow: {range_pips} pips (min {self.MIN_RANGE_PIPS})")
            return None
        if range_pips > self.MAX_RANGE_PIPS:
            print(f"  ❌ Range too wide: {range_pips} pips (max {self.MAX_RANGE_PIPS})")
            return None

        print(f"  ✓ Overnight range: {session['low']:.5f} – {session['high']:.5f} ({range_pips} pips)")

        # 4. Current price
        price_data = self.client.get_price(pair)
        if not price_data["tradeable"]:
            print(f"  ❌ Pair not tradeable.")
            return None

        bid = price_data["bid"]
        ask = price_data["ask"]
        mid = (bid + ask) / 2

        # 5. Breakout levels
        pip_size    = self.md.pips_to_price(BREAKOUT_BUFFER_PIPS, pair)
        long_entry  = session["high"] + pip_size
        short_entry = session["low"]  - pip_size

        print(f"  Long entry  : {long_entry:.5f}  (range high + {BREAKOUT_BUFFER_PIPS} pips)")
        print(f"  Short entry : {short_entry:.5f}  (range low  - {BREAKOUT_BUFFER_PIPS} pips)")
        print(f"  Current mid : {mid:.5f}")

        # 6. H1 trend state
        trend_state = self._get_trend_state(pair)
        print(f"  H1 trend    : {trend_state.upper()}")

        # 7. Detect breakout direction + apply filters
        direction   = None
        entry_price = None

        if ask >= long_entry:
            if "buy" not in allowed_directions:
                print(f"  ❌ Long blocked — direction config: {allowed_directions}")
                return None
            if require_trend_alignment and trend_state != "bullish":
                print(f"  ❌ Long blocked — H1 trend not bullish ({trend_state})")
                return None
            direction   = "buy"
            entry_price = ask

        elif bid <= short_entry:
            if "sell" not in allowed_directions:
                print(f"  ❌ Short blocked — direction config: {allowed_directions}")
                return None
            if require_trend_alignment and trend_state != "bearish":
                print(f"  ❌ Short blocked — H1 trend not bearish ({trend_state})")
                return None
            direction   = "sell"
            entry_price = bid

        if not direction:
            print(f"  — No breakout yet.")
            return None

        # 7a. ATR volatility gate
        vol_scalar = 1.0
        atr_regime = self.md.get_atr_regime(pair)
        vol_label  = "HIGH-VOL" if atr_regime["is_high_vol"] else "normal"
        print(f"  ATR regime  : {vol_label} (ratio {atr_regime['ratio']}x)")
        if atr_regime["is_high_vol"]:
            if ATR_BLOCK_ON_HIGH_VOL:
                print(f"  ❌ High-vol regime — trade blocked (ATR {atr_regime['ratio']}x baseline)")
                return None
            vol_scalar = ATR_HIGH_VOL_SIZE_SCALAR
            print(f"  ⚠️  High-vol regime — size reduced to {vol_scalar}x")

        # 7b. Momentum body ratio
        m15_df = self.md.get_dataframe(pair, granularity="M15", count=3)
        if not m15_df.empty:
            last_bar   = m15_df.iloc[-1]
            bar_range  = last_bar["high"] - last_bar["low"]
            bar_body   = abs(last_bar["close"] - last_bar["open"])
            body_ratio = bar_body / bar_range if bar_range > 0 else 0
            print(f"  Body ratio  : {body_ratio:.2f} (min {MOMENTUM_BODY_RATIO})")
            if body_ratio < MOMENTUM_BODY_RATIO:
                print(f"  ❌ Wick breakout — body ratio too low ({body_ratio:.2f})")
                return None

        # 8. Bias filter
        if bias:
            if self._bias_suppresses(direction, pair, bias):
                print(f"  ❌ Suppressed by weekly bias ({bias['bias']})")
                return None

        # 9. Calculate levels
        stop_loss, take_profit, stop_pips, target_pips = self._calculate_levels(
            direction, entry_price, session, pair
        )

        # 10. RR check
        rr = target_pips / stop_pips if stop_pips > 0 else 0
        if rr < MIN_REWARD_RISK:
            print(f"  ❌ RR too low: {rr:.2f}")
            return None

        # 11. Pre-trade risk check
        ok, _ = self.risk.pre_trade_check(
            pair=pair,
            direction=direction,
            stop_pips=stop_pips,
            target_pips=target_pips,
            open_trades=open_trades,
        )
        if not ok:
            return None

        return TokyoSignal(
            pair=pair,
            direction=direction,
            entry_price=entry_price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            stop_pips=stop_pips,
            target_pips=target_pips,
            rr_ratio=round(rr, 2),
            range_high=session["high"],
            range_low=session["low"],
            range_pips=range_pips,
            trend_state=trend_state,
            timestamp=now,
            vol_scalar=vol_scalar,
        )

    # ── Trend State ────────────────────────────────────────────

    def _get_trend_state(self, pair: str) -> str:
        df = self.md.get_dataframe(pair, granularity="H1", count=250)
        if df.empty or len(df) < EMA_LONG:
            return "ranging"

        df   = self.md.add_emas(df)
        last = df.iloc[-1]
        e21  = last[f"ema_{EMA_SHORT}"]
        e50  = last[f"ema_{EMA_MID}"]
        e200 = last[f"ema_{EMA_LONG}"]

        if e21 > e50 > e200:
            return "bullish"
        elif e21 < e50 < e200:
            return "bearish"
        else:
            return "ranging"

    # ── Level Calculation ──────────────────────────────────────

    def _calculate_levels(
        self,
        direction: str,
        entry:     float,
        session:   dict,
        pair:      str,
    ) -> tuple[float, float, float, float]:
        pip = self.md.pips_to_price(BREAKOUT_BUFFER_PIPS, pair)

        if direction == "buy":
            stop_loss   = session["low"]  - pip
            take_profit = entry + (entry - stop_loss) * self.REWARD_RISK
        else:
            stop_loss   = session["high"] + pip
            take_profit = entry - (stop_loss - entry) * self.REWARD_RISK

        stop_pips   = self.md.price_to_pips(abs(entry - stop_loss), pair)
        target_pips = self.md.price_to_pips(abs(take_profit - entry), pair)

        return stop_loss, take_profit, stop_pips, target_pips

    # ── Bias Filter ────────────────────────────────────────────

    def _bias_suppresses(self, direction: str, pair: str, bias: dict) -> bool:
        usd_score = bias.get("usd_score", 0)
        b         = bias.get("bias", "neutral")

        if abs(usd_score) < 3:
            return False

        # For JPY pairs: USD strengthening = USD/JPY buy (JPY weakens),
        # USD weakening = USD/JPY sell (JPY strengthens).
        # EUR/JPY is EUR vs JPY — no direct USD bias signal, skip suppression.
        usd_weakening     = {("USD_JPY", "sell")}
        usd_strengthening = {("USD_JPY", "buy")}

        if b == "bullish_usd" and (pair, direction) in usd_weakening:
            return True
        if b == "bearish_usd" and (pair, direction) in usd_strengthening:
            return True

        return False

    # ── Helpers ────────────────────────────────────────────────

    def _in_entry_window(self, now: datetime) -> bool:
        return self.TOKYO_OPEN_HOUR <= now.hour < self.TOKYO_CLOSE_HOUR

    def reset_daily(self):
        self._fired_today.clear()
        print("[TokyoBreakout] Daily state reset.")

    def mark_fired(self, pair: str):
        self._fired_today.add(pair)

    def _print_signal(self, s: TokyoSignal):
        arrow = "▲ BUY" if s.direction == "buy" else "▼ SELL"
        print(f"\n  {'='*50}")
        print(f"  🚨 TOKYO SIGNAL — {s.pair}  {arrow}")
        print(f"  {'='*50}")
        print(f"  Entry       : {s.entry_price:.5f}")
        print(f"  Stop Loss   : {s.stop_loss:.5f}  ({s.stop_pips:.1f} pips)")
        print(f"  Take Profit : {s.take_profit:.5f}  ({s.target_pips:.1f} pips)")
        print(f"  RR Ratio    : 1:{s.rr_ratio}")
        print(f"  Trend       : {s.trend_state.upper()}")
        print(f"  Range       : {s.range_low:.5f} – {s.range_high:.5f}  ({s.range_pips} pips)")
        print(f"  Time        : {s.timestamp.strftime('%H:%M UTC')}")
        print(f"  {'='*50}\n")
