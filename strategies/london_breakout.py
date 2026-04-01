from datetime import datetime, timezone, timedelta
from dataclasses import dataclass

from oanda.client import OandaClient
from oanda.market_data import MarketData
from risk.manager import RiskManager
from econ_calendar.filter import is_in_blackout
from config import (
    BREAKOUT_BUFFER_PIPS,
    MIN_REWARD_RISK,
    EMA_SHORT, EMA_MID, EMA_LONG,
)


@dataclass
class BreakoutSignal:
    pair:             str
    direction:        str        # "buy" | "sell"
    entry_price:      float
    stop_loss:        float
    take_profit:      float
    stop_pips:        float
    target_pips:      float
    rr_ratio:         float
    asian_high:       float
    asian_low:        float
    asian_range_pips: float
    trend_state:      str        # "bullish" | "bearish" | "ranging"
    timestamp:        datetime


# ── Per-pair configuration ─────────────────────────────────────
# Derived directly from backtest results.
# DO NOT change these without re-running the walk-forward backtest.
#
# EUR/USD: trend filter on, both directions profitable
# GBP/USD: trend filter on, long only — shorts confirmed losing OOS
#
PAIR_CONFIG = {
    "EUR_USD": {
        "allowed_directions":    ("buy", "sell"),
        "require_trend_alignment": True,
    },
    "GBP_USD": {
        "allowed_directions":    ("buy",),
        "require_trend_alignment": True,
    },
}


class LondonBreakout:
    """
    London Breakout Strategy — backtest-validated configuration.

    Per-pair settings applied from PAIR_CONFIG above.
    Trend alignment and direction filters match the walk-forward
    backtest results exactly.

    Logic:
      1. Build Asian session range (22:00 prev day – 07:00 UTC)
      2. At London open (07:00–09:00 UTC):
             long entry  = asian_high + buffer_pips
             short entry = asian_low  - buffer_pips
      3. Signal fires when live price crosses either level
      4. Trend filter: EMA 21/50/200 must be stacked in signal direction
      5. Direction filter: pair-specific — GBP/USD long only
      6. Stop: opposite side of Asian range + buffer
      7. Take profit: entry + (stop_distance * 2.5R)
    """

    MIN_RANGE_PIPS = 10
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
        self.pairs  = pairs or ["EUR_USD", "GBP_USD"]
        self._fired_today: set[str] = set()

    # ── Main Entry Point ───────────────────────────────────────

    def scan(self, events: list[dict], bias: dict = None) -> list[BreakoutSignal]:
        """
        Scans all configured pairs for London breakout signals.
        Call this at or after 07:00 UTC, before 09:00 UTC.
        """
        now     = datetime.now(timezone.utc)
        signals = []

        if not self._in_breakout_window(now):
            print(f"[LondonBreakout] Outside window (07:00–09:00 UTC). "
                  f"Current: {now.strftime('%H:%M')} UTC")
            return signals

        for pair in self.pairs:
            if pair in self._fired_today:
                print(f"[LondonBreakout] {pair} — already fired today.")
                continue

            print(f"\n[LondonBreakout] Scanning {pair}...")
            signal = self._evaluate_pair(pair, events, bias, now)

            if signal:
                signals.append(signal)
                self._print_signal(signal)

        return signals

    # ── Pair Evaluation ────────────────────────────────────────

    def _evaluate_pair(
        self,
        pair:   str,
        events: list[dict],
        bias:   dict | None,
        now:    datetime,
    ) -> BreakoutSignal | None:

        # Load per-pair config — fall back to most conservative defaults
        cfg = PAIR_CONFIG.get(pair, {
            "allowed_directions":      ("buy", "sell"),
            "require_trend_alignment": True,
        })
        allowed_directions     = cfg["allowed_directions"]
        require_trend_alignment = cfg["require_trend_alignment"]

        # 1. News blackout
        blocked, reason = is_in_blackout(events, now=now)
        if blocked:
            print(f"  ❌ Blocked — {reason}")
            return None

        # 2. Asian session range
        asian = self.md.get_asian_range(pair)
        if not asian:
            print(f"  ❌ Asian range unavailable.")
            return None

        range_pips = asian["range_pips"]

        # 3. Range size filter
        if range_pips < self.MIN_RANGE_PIPS:
            print(f"  ❌ Range too narrow: {range_pips} pips (min {self.MIN_RANGE_PIPS})")
            return None
        if range_pips > self.MAX_RANGE_PIPS:
            print(f"  ❌ Range too wide: {range_pips} pips (max {self.MAX_RANGE_PIPS})")
            return None

        print(f"  ✓ Asian range: {asian['low']:.5f} – {asian['high']:.5f} ({range_pips} pips)")

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
        long_entry  = asian["high"] + pip_size
        short_entry = asian["low"]  - pip_size

        print(f"  Long entry  : {long_entry:.5f}  (asian high + {BREAKOUT_BUFFER_PIPS} pips)")
        print(f"  Short entry : {short_entry:.5f}  (asian low  - {BREAKOUT_BUFFER_PIPS} pips)")
        print(f"  Current mid : {mid:.5f}")

        # 6. Trend state — read current EMA stack from H1 data
        trend_state = self._get_trend_state(pair)
        print(f"  Trend state : {trend_state.upper()}")

        # 7. Detect breakout direction, apply direction + trend filters
        direction   = None
        entry_price = None

        if ask >= long_entry:
            if "buy" not in allowed_directions:
                print(f"  ❌ Long breakout blocked — {pair} long only? "
                      f"No, direction config: {allowed_directions}")
                return None
            if require_trend_alignment and trend_state != "bullish":
                print(f"  ❌ Long breakout blocked — trend not bullish "
                      f"(EMA stack: {trend_state})")
                return None
            direction   = "buy"
            entry_price = ask

        elif bid <= short_entry:
            if "sell" not in allowed_directions:
                print(f"  ❌ Short breakout blocked — {pair} is long-only "
                      f"(backtest confirmed shorts unprofitable)")
                return None
            if require_trend_alignment and trend_state != "bearish":
                print(f"  ❌ Short breakout blocked — trend not bearish "
                      f"(EMA stack: {trend_state})")
                return None
            direction   = "sell"
            entry_price = bid

        if not direction:
            print(f"  — No breakout yet.")
            return None

        # 8. Bias filter — weekly macro suppression
        if bias:
            suppressed = self._bias_suppresses(direction, pair, bias)
            if suppressed:
                print(f"  ❌ Suppressed by weekly bias ({bias['bias']})")
                return None

        # 9. Calculate levels
        stop_loss, take_profit, stop_pips, target_pips = self._calculate_levels(
            direction, entry_price, asian, pair
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
            asian_high=asian["high"],
            asian_low=asian["low"],
            asian_range_pips=range_pips,
            trend_state=trend_state,
            timestamp=now,
        )

    # ── Trend State ────────────────────────────────────────────

    def _get_trend_state(self, pair: str) -> str:
        """
        Reads current EMA 21/50/200 stack from H1 candles.
        Returns "bullish" | "bearish" | "ranging".

        bullish = 21 > 50 > 200  (all pointing up, aligned)
        bearish = 21 < 50 < 200  (all pointing down, aligned)
        ranging = anything else  (mixed — no clean trend)

        Uses H1 to match the backtest granularity for indicator
        calculation. Requires 250 candles for EMA 200 to be valid.
        """
        df = self.md.get_dataframe(pair, granularity="H1", count=250)
        if df.empty or len(df) < EMA_LONG:
            print(f"  ⚠️  Insufficient data for trend state — defaulting to ranging")
            return "ranging"

        df = self.md.add_emas(df)
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
        direction:   str,
        entry:       float,
        asian:       dict,
        pair:        str,
    ) -> tuple[float, float, float, float]:
        pip = self.md.pips_to_price(BREAKOUT_BUFFER_PIPS, pair)

        if direction == "buy":
            stop_loss   = asian["low"] - pip
            take_profit = entry + (entry - stop_loss) * self.REWARD_RISK
        else:
            stop_loss   = asian["high"] + pip
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

    def _in_breakout_window(self, now: datetime) -> bool:
        return 7 <= now.hour < 9

    def reset_daily(self):
        self._fired_today.clear()
        print("[LondonBreakout] Daily state reset.")

    def mark_fired(self, pair: str):
        self._fired_today.add(pair)

    def _print_signal(self, s: BreakoutSignal):
        arrow = "▲ BUY" if s.direction == "buy" else "▼ SELL"
        print(f"\n  {'='*50}")
        print(f"  🚨 SIGNAL — {s.pair}  {arrow}")
        print(f"  {'='*50}")
        print(f"  Entry       : {s.entry_price:.5f}")
        print(f"  Stop Loss   : {s.stop_loss:.5f}  ({s.stop_pips:.1f} pips)")
        print(f"  Take Profit : {s.take_profit:.5f}  ({s.target_pips:.1f} pips)")
        print(f"  RR Ratio    : 1:{s.rr_ratio}")
        print(f"  Trend       : {s.trend_state.upper()}")
        print(f"  Asian Range : {s.asian_low:.5f} – {s.asian_high:.5f}  ({s.asian_range_pips} pips)")
        print(f"  Time        : {s.timestamp.strftime('%H:%M UTC')}")
        print(f"  {'='*50}\n")