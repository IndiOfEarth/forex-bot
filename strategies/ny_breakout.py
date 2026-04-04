from datetime import datetime, timezone

from oanda.client import OandaClient
from oanda.market_data import MarketData
from risk.manager import RiskManager
from econ_calendar.filter import is_in_blackout
from strategies.london_breakout import BreakoutSignal
from config import (
    BREAKOUT_BUFFER_PIPS,
    MIN_REWARD_RISK,
    EMA_SHORT, EMA_MID, EMA_LONG,
    MOMENTUM_BODY_RATIO,
    ATR_VOLATILITY_MULTIPLIER, ATR_HIGH_VOL_SIZE_SCALAR, ATR_BLOCK_ON_HIGH_VOL,
)


# ── Per-pair configuration ─────────────────────────────────────
# Derived from walk-forward backtest (config 21: ny_no_friday).
# DO NOT change these without re-running the walk-forward backtest.
#
# European range window: 09:00–13:00 UTC
# Entry window:          13:00–15:00 UTC (NY open / London–NY overlap)
#
# Validated OOS results (3-year walk-forward, ny_no_friday config):
#   EUR_USD : PF 1.29,  126 OOS trades,  +347 pips
#   GBP_USD : PF 1.72,  130 OOS trades,  +975 pips
#   USD_JPY : PF 1.82,  143 OOS trades, +1462 pips
#
# Key findings vs London strategy:
#   - first_bar_minutes=15 HURTS NY (GBP/USD PF drops from 1.72 → 0.65).
#     Unlike London, the NY breakout extends throughout 13:00–15:00 UTC,
#     not just the opening bar. first_bar_minutes=0 for all pairs.
#   - H4 trend confirmation adds no value for any NY pair (tested in
#     ny_quality config). H1 trend alignment alone is sufficient.
#   - Friday exclusion is the single biggest improvement across all pairs.
#     Friday NY sessions have low follow-through on breakouts.
#
NY_PAIR_CONFIG = {
    "EUR_USD": {
        "allowed_directions":      ("buy", "sell"),
        "require_trend_alignment": True,
        "require_4h_trend":        False,
        "require_daily_trend":     True,
        "first_bar_minutes":       0,
        "allowed_weekdays":        (0, 1, 2, 3),
    },
    "GBP_USD": {
        "allowed_directions":      ("buy", "sell"),
        "require_trend_alignment": True,
        "require_4h_trend":        False,
        "require_daily_trend":     True,
        "first_bar_minutes":       0,
        "allowed_weekdays":        (0, 1, 2, 3),
    },
    "USD_JPY": {
        "allowed_directions":      ("buy", "sell"),
        "require_trend_alignment": True,
        "require_4h_trend":        False,
        "require_daily_trend":     True,
        "first_bar_minutes":       0,
        "allowed_weekdays":        (0, 1, 2, 3),
    },
}

# ── European range window (UTC hours) ──────────────────────────
EUROPEAN_RANGE_START = 9    # 09:00 UTC — London open momentum begins
EUROPEAN_RANGE_END   = 13   # 13:00 UTC — NY open starts

# ── NY entry window (UTC hours) ────────────────────────────────
NY_ENTRY_START = 13   # 13:00 UTC — NY session opens
NY_ENTRY_END   = 15   # 15:00 UTC — early-NY momentum window closes


class NYBreakout:
    """
    New York Open Breakout Strategy.

    Uses the European morning session (09:00–13:00 UTC) as the consolidation
    range, then trades breakouts of that range when New York opens (13:00–15:00 UTC).
    This is the highest-liquidity window of the day (London–NY overlap).

    Filter pipeline mirrors the London Breakout strategy exactly — same
    trend, timing, body ratio, and risk checks. Per-pair config (NY_PAIR_CONFIG)
    is to be tuned from walk-forward backtest results.

    Logic:
      1. Build European morning range (09:00–13:00 UTC)
      2. At NY open (13:00–15:00 UTC):
             long entry  = european_high + buffer_pips
             short entry = european_low  - buffer_pips
      3. Signal fires when live price crosses either level
      4. Same trend, body ratio, weekday, news, and risk filters as London strategy
    """

    MIN_RANGE_PIPS = 20
    MAX_RANGE_PIPS = 80
    REWARD_RISK    = 2.5

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
        self.pairs  = pairs or ["EUR_USD", "GBP_USD", "USD_JPY"]
        self._fired_today: set[str] = set()

    # ── Main Entry Point ───────────────────────────────────────

    def scan(self, events: list[dict], bias: dict = None, open_trades: list[dict] = None) -> list[BreakoutSignal]:
        """
        Scans all configured pairs for NY open breakout signals.
        Call this at or after 13:00 UTC, before 15:00 UTC.
        """
        now     = datetime.now(timezone.utc)
        signals = []

        if not self._in_entry_window(now):
            print(f"[NYBreakout] Outside window (13:00–15:00 UTC). "
                  f"Current: {now.strftime('%H:%M')} UTC")
            return signals

        for pair in self.pairs:
            if pair in self._fired_today:
                print(f"[NYBreakout] {pair} — already fired today.")
                continue

            print(f"\n[NYBreakout] Scanning {pair}...")
            signal = self._evaluate_pair(pair, events, bias, now, open_trades)

            if signal:
                signals.append(signal)
                self._print_signal(signal)

        return signals

    # ── Pair Evaluation ────────────────────────────────────────

    def _evaluate_pair(
        self,
        pair:        str,
        events:      list[dict],
        bias:        dict | None,
        now:         datetime,
        open_trades: list[dict] = None,
    ) -> BreakoutSignal | None:

        cfg = NY_PAIR_CONFIG.get(pair, {
            "allowed_directions":      ("buy", "sell"),
            "require_trend_alignment": True,
            "require_4h_trend":        False,
            "first_bar_minutes":       15,
            "allowed_weekdays":        (0, 1, 2, 3),
        })
        allowed_directions      = cfg["allowed_directions"]
        require_trend_alignment = cfg["require_trend_alignment"]
        require_4h_trend        = cfg.get("require_4h_trend", False)
        require_daily_trend     = cfg.get("require_daily_trend", False)
        first_bar_minutes       = cfg.get("first_bar_minutes", 15)
        allowed_weekdays        = cfg.get("allowed_weekdays", ())

        # 0. Weekday filter — before any API calls (Mon=0, Fri=4)
        if allowed_weekdays and now.weekday() not in allowed_weekdays:
            day_name = now.strftime("%A")
            print(f"  ❌ Skipped — {day_name} not in allowed trading days")
            return None

        # 1. First-bar filter — before any API calls
        if first_bar_minutes > 0:
            minutes_past_open = now.hour * 60 + now.minute - NY_ENTRY_START * 60
            if minutes_past_open >= first_bar_minutes:
                print(f"  ❌ Outside first-bar window "
                      f"({minutes_past_open}min past NY open, limit {first_bar_minutes}min)")
                return None

        # 2. News blackout
        blocked, reason = is_in_blackout(events, now=now)
        if blocked:
            print(f"  ❌ Blocked — {reason}")
            return None

        # 3. European range
        european = self.md.get_session_range(pair, EUROPEAN_RANGE_START, EUROPEAN_RANGE_END)
        if european is None:
            print(f"  ❌ No European range data")
            return None

        # 4. Range size: minimum
        range_pips = european["range_pips"]
        if range_pips < self.MIN_RANGE_PIPS:
            print(f"  ❌ Range too small: {range_pips:.1f} pips (min {self.MIN_RANGE_PIPS})")
            return None

        # 5. Range size: maximum
        if range_pips > self.MAX_RANGE_PIPS:
            print(f"  ❌ Range too large: {range_pips:.1f} pips (max {self.MAX_RANGE_PIPS})")
            return None

        # 6. Current price
        price_data = self.client.get_price(pair)
        if not price_data or not price_data.get("tradeable"):
            print(f"  ❌ Price not available for {pair}")
            return None

        ask = price_data["ask"]
        bid = price_data["bid"]

        # 7. Breakout levels
        pip           = self.md.pips_to_price(BREAKOUT_BUFFER_PIPS, pair)
        long_entry    = european["high"] + pip
        short_entry   = european["low"]  - pip

        # 8. H1 Trend state
        trend_state = self._get_trend_state(pair)

        # 9. H4 trend (optional, per-pair)
        h4_trend = "ranging"
        if require_4h_trend:
            h4_trend = self._get_h4_trend_state(pair)
            print(f"  H4 trend    : {h4_trend.upper()}")

        # 9b. D1 macro regime filter — EMA 50/200 death/golden cross
        if require_daily_trend:
            d1_trend = self.md.get_daily_trend_state(pair)
            print(f"  D1 trend    : {d1_trend.upper()}")
        else:
            d1_trend = None

        # 10. Breakout detection + filters
        direction   = None
        entry_price = None

        if ask >= long_entry and "buy" in allowed_directions:
            if require_trend_alignment and trend_state != "bullish":
                print(f"  ❌ Long signal blocked — H1 trend is {trend_state}")
                return None
            if require_4h_trend and h4_trend != "bullish":
                print(f"  ❌ Long signal blocked — H4 trend is {h4_trend}")
                return None
            if require_daily_trend and d1_trend != "bullish":
                print(f"  ❌ Long signal blocked — D1 trend not bullish ({d1_trend})")
                return None
            direction   = "buy"
            entry_price = ask

        elif bid <= short_entry and "sell" in allowed_directions:
            if require_trend_alignment and trend_state != "bearish":
                print(f"  ❌ Short signal blocked — H1 trend is {trend_state}")
                return None
            if require_4h_trend and h4_trend != "bearish":
                print(f"  ❌ Short signal blocked — H4 trend is {h4_trend}")
                return None
            if require_daily_trend and d1_trend != "bearish":
                print(f"  ❌ Short signal blocked — D1 trend not bearish ({d1_trend})")
                return None
            direction   = "sell"
            entry_price = bid

        if direction is None:
            print(f"  — No breakout: ask={ask:.5f}, bid={bid:.5f}, "
                  f"long={long_entry:.5f}, short={short_entry:.5f}")
            return None

        # 10a. ATR volatility regime gate
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

        # 11. Momentum body ratio — fetch M15 candles, check last closed bar
        m15 = self.md.get_dataframe(pair, granularity="M15", count=5)
        if not m15.empty:
            last_bar = m15.iloc[-2]   # second-to-last = last closed bar
            bar_range = last_bar["high"] - last_bar["low"]
            bar_body  = abs(last_bar["close"] - last_bar["open"])
            body_ratio = bar_body / bar_range if bar_range > 0 else 0
            if body_ratio < MOMENTUM_BODY_RATIO:
                print(f"  ❌ Weak breakout bar: body ratio {body_ratio:.2f} "
                      f"(min {MOMENTUM_BODY_RATIO})")
                return None

        # 12. Weekly macro bias suppression
        if bias and self._bias_suppresses(direction, pair, bias):
            print(f"  ❌ Suppressed by weekly bias ({bias['bias']})")
            return None

        # 13. Calculate levels
        stop_loss, take_profit, stop_pips, target_pips = self._calculate_levels(
            direction, entry_price, european, pair
        )

        # 14. RR check
        rr = target_pips / stop_pips if stop_pips > 0 else 0
        if rr < MIN_REWARD_RISK:
            print(f"  ❌ RR too low: {rr:.2f}")
            return None

        # 15. Pre-trade risk check
        ok, _ = self.risk.pre_trade_check(
            pair=pair,
            direction=direction,
            stop_pips=stop_pips,
            target_pips=target_pips,
            open_trades=open_trades,
        )
        if not ok:
            return None

        return BreakoutSignal(
            pair=pair,
            direction=direction,
            entry_price=entry_price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            stop_pips=stop_pips,
            target_pips=target_pips,
            rr_ratio=round(rr, 2),
            asian_high=european["high"],
            asian_low=european["low"],
            asian_range_pips=range_pips,
            trend_state=trend_state,
            timestamp=now,
            vol_scalar=vol_scalar,
        )

    # ── Trend State ────────────────────────────────────────────

    def _get_trend_state(self, pair: str) -> str:
        df = self.md.get_dataframe(pair, granularity="H1", count=250)
        if df.empty or len(df) < EMA_LONG:
            print(f"  ⚠️  Insufficient data for trend state — defaulting to ranging")
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

    def _get_h4_trend_state(self, pair: str) -> str:
        df = self.md.get_dataframe(pair, granularity="H4", count=250)
        if df.empty or len(df) < EMA_LONG:
            print(f"  ⚠️  Insufficient H4 data — defaulting to ranging")
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
        european:  dict,
        pair:      str,
    ) -> tuple[float, float, float, float]:
        pip = self.md.pips_to_price(BREAKOUT_BUFFER_PIPS, pair)

        if direction == "buy":
            stop_loss   = european["low"] - pip
            take_profit = entry + (entry - stop_loss) * self.REWARD_RISK
        else:
            stop_loss   = european["high"] + pip
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

        usd_weakening     = {("EUR_USD","buy"),("GBP_USD","buy"),("AUD_USD","buy"),
                             ("USD_JPY","sell"),("USD_CAD","sell")}
        usd_strengthening = {("EUR_USD","sell"),("GBP_USD","sell"),("AUD_USD","sell"),
                             ("USD_JPY","buy"),("USD_CAD","buy")}

        if b == "bullish_usd" and (pair, direction) in usd_weakening:
            return True
        if b == "bearish_usd" and (pair, direction) in usd_strengthening:
            return True

        return False

    # ── Helpers ────────────────────────────────────────────────

    def _in_entry_window(self, now: datetime) -> bool:
        return NY_ENTRY_START <= now.hour < NY_ENTRY_END

    def reset_daily(self):
        self._fired_today.clear()
        print("[NYBreakout] Daily state reset.")

    def mark_fired(self, pair: str):
        self._fired_today.add(pair)

    def _print_signal(self, s: BreakoutSignal):
        arrow = "▲ BUY" if s.direction == "buy" else "▼ SELL"
        print(f"\n  {'='*50}")
        print(f"  🚨 NY SIGNAL — {s.pair}  {arrow}")
        print(f"  {'='*50}")
        print(f"  Entry          : {s.entry_price:.5f}")
        print(f"  Stop Loss      : {s.stop_loss:.5f}  ({s.stop_pips:.1f} pips)")
        print(f"  Take Profit    : {s.take_profit:.5f}  ({s.target_pips:.1f} pips)")
        print(f"  RR Ratio       : 1:{s.rr_ratio}")
        print(f"  Trend          : {s.trend_state.upper()}")
        print(f"  European Range : {s.asian_low:.5f} – {s.asian_high:.5f}  ({s.asian_range_pips} pips)")
        print(f"  Time           : {s.timestamp.strftime('%H:%M UTC')}")
        print(f"  {'='*50}\n")
