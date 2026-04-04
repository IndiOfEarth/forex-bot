from datetime import datetime, timezone, date
from oanda.client import OandaClient
from config import (
    RISK_PER_TRADE, MAX_DAILY_LOSS, MAX_OPEN_POSITIONS, MIN_REWARD_RISK,
    CONSECUTIVE_LOSS_LIMIT, MAX_PEAK_DRAWDOWN,
)


class RiskManager:
    """
    Enforces all risk rules before any order is placed.
    Single point of truth for position sizing and risk checks.
    """

    def __init__(self, client: OandaClient):
        self.client                = client
        self._daily_start_balance: float | None = None
        self._daily_date:          date  | None = None
        self._peak_balance:        float | None = None

    # ── Daily Loss Tracking ────────────────────────────────────

    def _refresh_daily_baseline(self):
        """Sets starting balance for today if not already set."""
        today = datetime.now(timezone.utc).date()
        if self._daily_date != today:
            self._daily_start_balance = self.client.get_account_balance()
            self._daily_date = today

    def get_daily_drawdown(self) -> float:
        """Returns today's drawdown as a fraction of starting balance. e.g. 0.02 = 2%."""
        self._refresh_daily_baseline()
        current = self.client.get_nav()
        if not self._daily_start_balance:
            return 0.0
        return (self._daily_start_balance - current) / self._daily_start_balance

    def is_daily_limit_breached(self) -> tuple[bool, str]:
        """Returns (True, reason) if daily loss kill-switch should fire."""
        drawdown = self.get_daily_drawdown()
        if drawdown >= MAX_DAILY_LOSS:
            return True, f"Daily loss limit reached: {drawdown*100:.2f}% (max {MAX_DAILY_LOSS*100:.0f}%)"
        return False, ""

    # ── Consecutive Loss Kill-Switch ───────────────────────────

    def is_consecutive_loss_limit_breached(self) -> tuple[bool, str]:
        """
        Returns (True, reason) if the last CONSECUTIVE_LOSS_LIMIT closed trades
        were all losses. Reads live OANDA trade history — no in-memory state,
        so it resets automatically once a winning trade is closed.
        """
        outcomes = self.client.get_recent_closed_trade_outcomes(count=CONSECUTIVE_LOSS_LIMIT)
        if len(outcomes) < CONSECUTIVE_LOSS_LIMIT:
            return False, ""
        if all(o == "loss" for o in outcomes):
            return True, (
                f"Consecutive loss limit: last {CONSECUTIVE_LOSS_LIMIT} closed trades "
                f"all losses — pausing for rest of day"
            )
        return False, ""

    # ── Equity Peak Drawdown Guard ─────────────────────────────

    def get_peak_drawdown(self) -> float:
        """
        Returns drawdown from the in-session equity peak as a fraction.
        Updates the peak on every call if current NAV is higher.
        e.g. 0.05 = 5% below peak.
        """
        current = self.client.get_nav()
        if self._peak_balance is None or current > self._peak_balance:
            self._peak_balance = current
        if self._peak_balance == 0:
            return 0.0
        return (self._peak_balance - current) / self._peak_balance

    def is_peak_drawdown_breached(self) -> tuple[bool, str]:
        """Returns (True, reason) if NAV has fallen MAX_PEAK_DRAWDOWN below session peak."""
        dd = self.get_peak_drawdown()
        if dd >= MAX_PEAK_DRAWDOWN:
            return True, (
                f"Peak drawdown limit reached: {dd*100:.2f}% below peak "
                f"${self._peak_balance:,.2f} (max {MAX_PEAK_DRAWDOWN*100:.0f}%)"
            )
        return False, ""

    # ── Open Position Check ────────────────────────────────────

    def is_max_positions_reached(self) -> tuple[bool, str]:
        """Returns (True, reason) if already at max concurrent trades."""
        open_count = self.client.get_open_trade_count()
        if open_count >= MAX_OPEN_POSITIONS:
            return True, f"Max open positions reached: {open_count}/{MAX_OPEN_POSITIONS}"
        return False, ""

    # ── Correlation Check ──────────────────────────────────────

    # Pairs grouped by USD direction exposure
    # If you're long USD in one, you shouldn't be long USD in another
    USD_LONG_PAIRS  = {"USD_JPY", "USD_CAD", "USD_CHF"}   # long = long USD
    USD_SHORT_PAIRS = {"EUR_USD", "GBP_USD", "AUD_USD", "NZD_USD"}  # long = short USD

    def check_correlation(self, pair: str, direction: str, open_trades: list[dict]) -> tuple[bool, str]:
        """
        Checks if a proposed trade would create correlated USD exposure.

        pair:        e.g. "EUR_USD"
        direction:   "buy" | "sell"
        open_trades: list of open trade dicts from OANDA (each has 'instrument' and 'currentUnits')

        Returns (True, reason) if correlated risk exists.
        """
        if not open_trades:
            return False, ""

        # Determine proposed USD direction
        proposed_usd_long = (
            (pair in self.USD_LONG_PAIRS  and direction == "buy") or
            (pair in self.USD_SHORT_PAIRS and direction == "sell")
        )
        proposed_usd_short = not proposed_usd_long

        for trade in open_trades:
            existing_pair      = trade.get("instrument", "")
            existing_units     = float(trade.get("currentUnits", 0))
            existing_direction = "buy" if existing_units > 0 else "sell"

            existing_usd_long = (
                (existing_pair in self.USD_LONG_PAIRS  and existing_direction == "buy") or
                (existing_pair in self.USD_SHORT_PAIRS and existing_direction == "sell")
            )

            # Both trades = same USD direction → correlated
            if proposed_usd_long and existing_usd_long:
                return True, f"Correlated: {pair} {direction} duplicates USD long exposure with open {existing_pair}"
            if proposed_usd_short and not existing_usd_long:
                return True, f"Correlated: {pair} {direction} duplicates USD short exposure with open {existing_pair}"

        return False, ""

    # ── Position Sizing ────────────────────────────────────────

    def calculate_units(
        self,
        pair:         str,
        direction:    str,
        stop_pips:    float,
        scalar:       float = 1.0,
    ) -> int:
        """
        Calculates order units based on fixed fractional risk.

        pair:       "EUR_USD"
        direction:  "buy" | "sell"
        stop_pips:  distance from entry to stop loss in pips
        scalar:     position size multiplier from weekly bias (0.5 – 1.5)

        Returns integer units (positive = buy, negative = sell).
        Units for OANDA = number of base currency units.
        """
        if stop_pips <= 0:
            print("[RiskManager] Invalid stop_pips — must be > 0")
            return 0

        balance      = self.client.get_account_balance()
        risk_amount  = balance * RISK_PER_TRADE * scalar
        pip_value    = self._get_pip_value(pair)

        # Units = risk_amount / (stop_pips * pip_value_per_unit)
        units = int(risk_amount / (stop_pips * pip_value))

        if direction == "sell":
            units = -units

        print(f"[RiskManager] Size: {abs(units):,} units | "
              f"Risk: ${risk_amount:.2f} | Stop: {stop_pips} pips | Scalar: {scalar}x")
        return units

    def _get_pip_value(self, pair: str) -> float:
        """
        Approximate pip value per unit in account currency (USD).
        For a $100k practice account this is fine.
        More precise: fetch current price and calculate dynamically.
        """
        # For USD-quoted pairs (EUR/USD, GBP/USD, AUD/USD): pip = 0.0001
        # For JPY-quoted pairs (USD/JPY): pip = 0.01
        # For USD-base pairs (USD/CAD): pip value depends on CAD/USD rate
        # Simplified approximations — good enough for practice sizing
        pip_values = {
            "EUR_USD": 0.0001,
            "GBP_USD": 0.0001,
            "AUD_USD": 0.0001,
            "NZD_USD": 0.0001,
            "USD_JPY": 0.000625,  # ~1/160 at current rates
            "USD_CAD": 0.000072,  # ~1/1.39
            "USD_CHF": 0.000109,
        }
        return pip_values.get(pair, 0.0001)

    # ── Validate RR ────────────────────────────────────────────

    def validate_reward_risk(self, stop_pips: float, target_pips: float) -> tuple[bool, float]:
        """
        Checks if the trade meets minimum reward-to-risk ratio.
        Returns (True, rr_ratio) if valid.
        """
        if stop_pips <= 0:
            return False, 0.0
        rr = target_pips / stop_pips
        return rr >= MIN_REWARD_RISK, round(rr, 2)

    # ── Master Pre-Trade Check ─────────────────────────────────

    def pre_trade_check(
        self,
        pair:         str,
        direction:    str,
        stop_pips:    float,
        target_pips:  float,
        open_trades:  list[dict] = None,
    ) -> tuple[bool, list[str]]:
        """
        Runs all risk checks before placing an order.
        Returns (True, []) if safe to trade.
        Returns (False, [reasons]) if blocked.
        """
        blocks = []

        # Daily loss limit
        breached, reason = self.is_daily_limit_breached()
        if breached:
            blocks.append(reason)

        # Consecutive loss kill-switch
        consec, reason = self.is_consecutive_loss_limit_breached()
        if consec:
            blocks.append(reason)

        # Peak drawdown guard
        peak_hit, reason = self.is_peak_drawdown_breached()
        if peak_hit:
            blocks.append(reason)

        # Max positions
        maxed, reason = self.is_max_positions_reached()
        if maxed:
            blocks.append(reason)

        # Reward-to-risk
        valid_rr, rr = self.validate_reward_risk(stop_pips, target_pips)
        if not valid_rr:
            blocks.append(f"RR too low: {rr:.2f} (min {MIN_REWARD_RISK})")

        # Correlation
        if open_trades:
            correlated, reason = self.check_correlation(pair, direction, open_trades)
            if correlated:
                blocks.append(reason)

        if blocks:
            print(f"[RiskManager] ❌ Trade blocked:")
            for b in blocks:
                print(f"   • {b}")
            return False, blocks

        print(f"[RiskManager] ✅ Pre-trade checks passed — {pair} {direction.upper()} | RR: {rr:.2f}")
        return True, []

    def print_risk_status(self):
        """Prints current risk state to terminal."""
        self._refresh_daily_baseline()
        drawdown   = self.get_daily_drawdown()
        nav        = self.client.get_nav()
        balance    = self.client.get_account_balance()
        open_count = self.client.get_open_trade_count()

        print(f"\n{'='*50}")
        print(f"  RISK STATUS")
        print(f"{'='*50}")
        print(f"  Balance       : ${balance:,.2f}")
        print(f"  NAV           : ${nav:,.2f}")
        print(f"  Daily Drawdown: {drawdown*100:.2f}% (limit {MAX_DAILY_LOSS*100:.0f}%)")
        print(f"  Open Trades   : {open_count}/{MAX_OPEN_POSITIONS}")
        print(f"  Daily Start   : ${self._daily_start_balance:,.2f}")
        limit_hit, _ = self.is_daily_limit_breached()
        print(f"  Kill-switch   : {'🔴 ACTIVE' if limit_hit else '🟢 OK'}")

        consec_hit, _ = self.is_consecutive_loss_limit_breached()
        print(f"  Consec Loss   : {'🔴 LIMIT HIT' if consec_hit else '🟢 OK'}")

        peak_dd = self.get_peak_drawdown()
        peak_hit, _ = self.is_peak_drawdown_breached()
        peak_str = f"${self._peak_balance:,.2f}" if self._peak_balance else "not set"
        print(f"  Peak DD       : {peak_dd*100:.2f}% from {peak_str} (limit {MAX_PEAK_DRAWDOWN*100:.0f}%)")
        print(f"  Peak Guard    : {'🔴 ACTIVE' if peak_hit else '🟢 OK'}")
        print()