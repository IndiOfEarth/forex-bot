from datetime import datetime, timezone
from dataclasses import dataclass

from oanda.client import OandaClient
from oanda.market_data import MarketData
from risk.manager import RiskManager
from econ_calendar.filter import is_in_blackout
from config import (
    BREAKOUT_BUFFER_PIPS,
    BREAKOUT_ASIAN_END,
    MIN_REWARD_RISK,
)


@dataclass
class BreakoutSignal:
    pair:           str
    direction:      str        # "buy" | "sell"
    entry_price:    float
    stop_loss:      float
    take_profit:    float
    stop_pips:      float
    target_pips:    float
    rr_ratio:       float
    asian_high:     float
    asian_low:      float
    asian_range_pips: float
    timestamp:      datetime


class LondonBreakout:
    """
    London Breakout Strategy.

    Logic:
      1. Calculate the Asian session range (22:00 – 07:00 UTC)
      2. At London open (07:00 UTC), set breakout levels:
             long entry  = asian_high + buffer_pips
             short entry = asian_low  - buffer_pips
      3. Signal fires when live price crosses either level
      4. Stop loss: opposite side of Asian range + buffer
      5. Take profit: entry + (stop_distance * RR multiplier)
      6. Cancel if: news blackout, range too wide/narrow, after 09:00 UTC

    Filters:
      - Asian range must be between MIN_RANGE and MAX_RANGE pips
      - No signal during news blackout window
      - Only valid between 07:00 and 09:00 UTC
      - Weekly bias can suppress counter-bias signals
    """

    # Range filters — avoid false breakouts on abnormally tight/wide days
    MIN_RANGE_PIPS = 10    # below this = too quiet, likely to whipsaw
    MAX_RANGE_PIPS = 80    # above this = already volatile, risk too high

    # RR for this strategy (can override config default)
    REWARD_RISK = 2.5

    def __init__(
        self,
        client:       OandaClient,
        market_data:  MarketData,
        risk_manager: RiskManager,
        pairs:        list[str] = None,
    ):
        self.client      = client
        self.md          = market_data
        self.risk        = risk_manager
        self.pairs       = pairs or ["EUR_USD", "GBP_USD"]
        self._fired_today: set[str] = set()   # track which pairs already signalled today

    # ── Main Entry Point ───────────────────────────────────────

    def scan(self, events: list[dict], bias: dict = None) -> list[BreakoutSignal]:
        """
        Scans all configured pairs for London breakout signals.
        Call this at or after 07:00 UTC, before 09:00 UTC.

        Returns list of valid BreakoutSignal objects (usually 0 or 1).
        """
        now = datetime.now(timezone.utc)
        signals = []

        # ── Time gate: only run 07:00 – 09:00 UTC ─────────────
        if not self._in_breakout_window(now):
            print(f"[LondonBreakout] Outside breakout window (07:00–09:00 UTC). Current: {now.strftime('%H:%M')} UTC")
            return signals

        for pair in self.pairs:

            # Skip if already fired for this pair today
            if pair in self._fired_today:
                print(f"[LondonBreakout] {pair} — already signalled today, skipping.")
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

        # 1. News blackout check
        blocked, reason = is_in_blackout(events, now=now)
        if blocked:
            print(f"  ❌ Blocked — {reason}")
            return None

        # 2. Get Asian session range
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

        # 4. Get current price
        price_data = self.client.get_price(pair)
        if not price_data["tradeable"]:
            print(f"  ❌ Pair not tradeable right now.")
            return None

        bid = price_data["bid"]
        ask = price_data["ask"]
        mid = (bid + ask) / 2

        # 5. Calculate breakout levels
        pip = self.md.pips_to_price(BREAKOUT_BUFFER_PIPS, pair)
        long_entry  = asian["high"] + pip
        short_entry = asian["low"]  - pip

        print(f"  Long entry  : {long_entry:.5f}  (asian high + {BREAKOUT_BUFFER_PIPS} pips)")
        print(f"  Short entry : {short_entry:.5f}  (asian low  - {BREAKOUT_BUFFER_PIPS} pips)")
        print(f"  Current mid : {mid:.5f}")

        # 6. Determine if price has broken out
        direction = None
        entry_price = None

        if ask >= long_entry:
            direction   = "buy"
            entry_price = ask
        elif bid <= short_entry:
            direction   = "sell"
            entry_price = bid

        if not direction:
            print(f"  — No breakout yet. Waiting...")
            return None

        # 7. Bias filter — suppress counter-bias signals
        if bias:
            suppressed = self._bias_suppresses(direction, pair, bias)
            if suppressed:
                print(f"  ❌ Signal suppressed by weekly bias ({bias['bias']})")
                return None

        # 8. Calculate stop loss and take profit
        stop_loss, take_profit, stop_pips, target_pips = self._calculate_levels(
            direction, entry_price, asian, pair
        )

        # 9. Validate RR
        rr = target_pips / stop_pips if stop_pips > 0 else 0
        if rr < MIN_REWARD_RISK:
            print(f"  ❌ RR too low: {rr:.2f}")
            return None

        # 10. Pre-trade risk check
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
            timestamp=now,
        )

    # ── Level Calculation ──────────────────────────────────────

    def _calculate_levels(
        self,
        direction:   str,
        entry:       float,
        asian:       dict,
        pair:        str,
    ) -> tuple[float, float, float, float]:
        """
        Returns (stop_loss, take_profit, stop_pips, target_pips).

        Stop: placed at the opposite side of the Asian range + buffer.
        This means the entire range acts as the risk buffer — clean logic.
        Target: entry + (stop_distance * REWARD_RISK).
        """
        pip = self.md.pips_to_price(BREAKOUT_BUFFER_PIPS, pair)

        if direction == "buy":
            stop_loss   = asian["low"] - pip          # below Asian low
            take_profit = entry + (entry - stop_loss) * self.REWARD_RISK
        else:
            stop_loss   = asian["high"] + pip         # above Asian high
            take_profit = entry - (stop_loss - entry) * self.REWARD_RISK

        stop_pips   = self.md.price_to_pips(abs(entry - stop_loss), pair)
        target_pips = self.md.price_to_pips(abs(take_profit - entry), pair)

        return stop_loss, take_profit, stop_pips, target_pips

    # ── Bias Filter ────────────────────────────────────────────

    def _bias_suppresses(self, direction: str, pair: str, bias: dict) -> bool:
        """
        Returns True if the weekly bias contradicts this signal direction.
        Only suppresses when bias is strong (score >= 3 or <= -2).
        """
        usd_score = bias.get("usd_score", 0)
        b = bias.get("bias", "neutral")

        if abs(usd_score) < 3:
            return False   # bias not strong enough to suppress

        # bullish_usd = USD strengthening
        # EUR/USD buy = USD weakening → suppress if bullish_usd
        usd_weakening_signals = {
            ("EUR_USD", "buy"), ("GBP_USD", "buy"),
            ("AUD_USD", "buy"), ("USD_JPY", "sell"),
            ("USD_CAD", "sell"),
        }
        usd_strengthening_signals = {
            ("EUR_USD", "sell"), ("GBP_USD", "sell"),
            ("AUD_USD", "sell"), ("USD_JPY", "buy"),
            ("USD_CAD", "buy"),
        }

        if b == "bullish_usd" and (pair, direction) in usd_weakening_signals:
            return True
        if b == "bearish_usd" and (pair, direction) in usd_strengthening_signals:
            return True

        return False

    # ── Time Window ────────────────────────────────────────────

    def _in_breakout_window(self, now: datetime) -> bool:
        """Valid breakout window: 07:00 – 09:00 UTC."""
        return 7 <= now.hour < 9

    def reset_daily(self):
        """Call this at the start of each trading day to reset fired pairs."""
        self._fired_today.clear()
        print("[LondonBreakout] Daily state reset.")

    def mark_fired(self, pair: str):
        """Mark a pair as having fired today — prevents duplicate signals."""
        self._fired_today.add(pair)

    # ── Signal Display ─────────────────────────────────────────

    def _print_signal(self, s: BreakoutSignal):
        arrow = "▲ BUY" if s.direction == "buy" else "▼ SELL"
        print(f"\n  {'='*48}")
        print(f"  🚨 SIGNAL — {s.pair}  {arrow}")
        print(f"  {'='*48}")
        print(f"  Entry       : {s.entry_price:.5f}")
        print(f"  Stop Loss   : {s.stop_loss:.5f}  ({s.stop_pips:.1f} pips)")
        print(f"  Take Profit : {s.take_profit:.5f}  ({s.target_pips:.1f} pips)")
        print(f"  RR Ratio    : 1:{s.rr_ratio}")
        print(f"  Asian Range : {s.asian_low:.5f} – {s.asian_high:.5f}  ({s.asian_range_pips} pips)")
        print(f"  Time        : {s.timestamp.strftime('%H:%M UTC')}")
        print(f"  {'='*48}\n")