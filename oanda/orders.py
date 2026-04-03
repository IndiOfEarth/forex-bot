import csv
import os
from datetime import datetime, timezone

import oandapyV20.endpoints.orders as orders
import oandapyV20.endpoints.trades as trades
import oandapyV20.endpoints.positions as positions

from oanda.client import OandaClient
from oanda.market_data import MarketData
from risk.manager import RiskManager
from strategies.london_breakout import BreakoutSignal
from config import OANDA_ACCOUNT_ID, TRAIL_TRIGGER_R, TRAIL_LOCK_R, PARTIAL_CLOSE_R, PARTIAL_CLOSE_PCT, TRADE_LOG_FILE

_TRADE_CSV_FIELDS = [
    "timestamp", "pair", "direction", "entry", "stop_loss",
    "take_profit", "units", "rr_ratio", "scalar", "mode",
]


def _append_trade_csv(row: dict) -> None:
    """Appends one entry row to TRADE_LOG_FILE. Creates the file with a header if needed."""
    os.makedirs(os.path.dirname(TRADE_LOG_FILE), exist_ok=True)
    file_exists = os.path.isfile(TRADE_LOG_FILE)
    with open(TRADE_LOG_FILE, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=_TRADE_CSV_FIELDS)
        if not file_exists:
            writer.writeheader()
        writer.writerow(row)


class OrderExecutor:
    """
    Handles all order placement, modification, and closing.
    Takes validated signals from strategies — never places orders
    without a pre-validated signal and risk check.
    """

    def __init__(self, client: OandaClient, market_data: MarketData, risk: RiskManager):
        self.client = client
        self.md     = market_data
        self.risk   = risk

    # ── Market Order ───────────────────────────────────────────

    def place_market_order(
        self,
        pair:        str,
        units:       int,       # positive = buy, negative = sell
        stop_loss:   float,
        take_profit: float,
        label:       str   = "",     # strategy tag for logs
        initial_sl:  float | None = None,  # persisted for trailing stop logic
    ) -> dict | None:
        """
        Places a market order with attached SL and TP.
        Returns the OANDA trade response dict, or None on failure.
        """
        direction = "buy" if units > 0 else "sell"

        data = {
            "order": {
                "type":        "MARKET",
                "instrument":  pair,
                "units":       str(units),
                "timeInForce": "FOK",   # Fill Or Kill — no partial fills
                "stopLossOnFill": {
                    "price": f"{stop_loss:.5f}",
                },
                "takeProfitOnFill": {
                    "price": f"{take_profit:.5f}",
                },
            }
        }

        comment = label
        if initial_sl is not None:
            comment = f"{label}|isl={initial_sl:.5f}"
        if comment:
            data["order"]["clientExtensions"] = {"comment": comment[:128]}

        try:
            r = orders.OrderCreate(self.client.account_id, data=data)
            self.client.client.request(r)
            response = r.response

            trade_id = response.get("orderFillTransaction", {}).get("tradeOpened", {}).get("tradeID")
            fill_price = response.get("orderFillTransaction", {}).get("price")

            print(f"\n[Orders] ✅ Order filled — {pair} {direction.upper()}")
            print(f"  Trade ID    : {trade_id}")
            print(f"  Units       : {units:+,}")
            print(f"  Fill Price  : {fill_price}")
            print(f"  Stop Loss   : {stop_loss:.5f}")
            print(f"  Take Profit : {take_profit:.5f}")
            print(f"  Label       : {label or 'none'}\n")

            return response

        except Exception as e:
            print(f"\n[Orders] ❌ Order failed — {pair} {direction.upper()}: {e}\n")
            return None

    # ── Execute from Signal ────────────────────────────────────

    def execute_signal(
        self,
        signal:  BreakoutSignal,
        scalar:  float = 1.0,
        label:   str   = "london_breakout",
    ) -> dict | None:
        """
        Takes a validated BreakoutSignal, calculates units, places order.
        scalar: position size multiplier from weekly bias engine.
        """
        # Final pre-trade check before touching the market
        open_trades = self.get_open_trades()
        ok, _ = self.risk.pre_trade_check(
            pair=signal.pair,
            direction=signal.direction,
            stop_pips=signal.stop_pips,
            target_pips=signal.target_pips,
            open_trades=open_trades,
        )
        if not ok:
            print(f"[Orders] Pre-trade check failed — order cancelled.")
            return None

        # Calculate position size
        units = self.risk.calculate_units(
            pair=signal.pair,
            direction=signal.direction,
            stop_pips=signal.stop_pips,
            scalar=scalar,
        )

        if units == 0:
            print(f"[Orders] Unit calculation returned 0 — order cancelled.")
            return None

        result = self.place_market_order(
            pair=signal.pair,
            units=units,
            stop_loss=signal.stop_loss,
            take_profit=signal.take_profit,
            label=label,
            initial_sl=signal.stop_loss,
        )
        if result:
            _append_trade_csv({
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "pair":      signal.pair,
                "direction": signal.direction,
                "entry":     signal.entry_price,
                "stop_loss": signal.stop_loss,
                "take_profit": signal.take_profit,
                "units":     units,
                "rr_ratio":  signal.rr_ratio,
                "scalar":    scalar,
                "mode":      "live",
            })
        return result

    # ── Trade Management ───────────────────────────────────────

    def get_open_trades(self) -> list[dict]:
        """Returns all currently open trades."""
        try:
            r = trades.OpenTrades(self.client.account_id)
            self.client.client.request(r)
            return r.response.get("trades", [])
        except Exception as e:
            print(f"[Orders] Failed to fetch open trades: {e}")
            return []

    def close_trade(self, trade_id: str) -> dict | None:
        """Closes a specific trade by ID."""
        try:
            r = trades.TradeClose(self.client.account_id, tradeID=trade_id)
            self.client.client.request(r)
            print(f"[Orders] ✅ Trade {trade_id} closed.")
            return r.response
        except Exception as e:
            print(f"[Orders] ❌ Failed to close trade {trade_id}: {e}")
            return None

    def close_all_trades(self) -> int:
        """Closes all open trades. Returns count closed."""
        open_trades = self.get_open_trades()
        closed = 0
        for trade in open_trades:
            result = self.close_trade(trade["id"])
            if result:
                closed += 1
        print(f"[Orders] Closed {closed}/{len(open_trades)} trades.")
        return closed

    def modify_stop_loss(self, trade_id: str, new_sl: float) -> dict | None:
        """Moves stop loss on an open trade — used for trailing stop logic."""
        try:
            data = {"stopLoss": {"price": f"{new_sl:.5f}", "timeInForce": "GTC"}}
            r = trades.TradeCRCDO(self.client.account_id, tradeID=trade_id, data=data)
            self.client.client.request(r)
            print(f"[Orders] Stop loss moved to {new_sl:.5f} on trade {trade_id}")
            return r.response
        except Exception as e:
            print(f"[Orders] ❌ Failed to modify SL on trade {trade_id}: {e}")
            return None

    # ── Trailing Stop + Partial Close ─────────────────────────

    def apply_trailing_stop(self, trade_id: str, pair: str, entry: float,
                            direction: str, initial_sl: float) -> bool:
        """
        Two-stage exit management — call on each bot loop for open trades.

        Stage 1 (TRAIL_TRIGGER_R = 1R profit):
            Move SL to break-even + 1 pip.

        Stage 2 (PARTIAL_CLOSE_R = 1.5R profit):
            Close PARTIAL_CLOSE_PCT (50%) of the position.
            Move SL to lock in TRAIL_LOCK_R (0.5R) on the remainder.

        Returns True if any action was taken.
        """
        open_trades = self.get_open_trades()
        trade = next((t for t in open_trades if t["id"] == trade_id), None)
        if not trade:
            return False

        price_data    = self.client.get_price(pair)
        current_price = price_data["ask"] if direction == "buy" else price_data["bid"]
        current_sl    = float(trade.get("stopLossOrder", {}).get("price", 0))
        current_units = abs(int(trade.get("currentUnits", 0)))
        r1_dist       = abs(entry - initial_sl)
        acted         = False

        if direction == "buy":
            trail_trigger_px   = entry + r1_dist * TRAIL_TRIGGER_R
            partial_close_px   = entry + r1_dist * PARTIAL_CLOSE_R
            lock_px            = entry + r1_dist * TRAIL_LOCK_R
            be_px              = entry + self.md.pips_to_price(1, pair)

            # Stage 2: partial close + lock remainder
            if current_price >= partial_close_px and current_sl < lock_px:
                close_units = round(current_units * PARTIAL_CLOSE_PCT)
                if close_units > 0:
                    self._close_partial(trade_id, pair, close_units, direction)
                if lock_px > current_sl:
                    self.modify_stop_loss(trade_id, lock_px)
                    print(f"[Orders] Trailing stop: locked 0.5R at {lock_px:.5f} on {pair}")
                acted = True

            # Stage 1: move to break-even
            elif current_price >= trail_trigger_px and current_sl < be_px:
                self.modify_stop_loss(trade_id, be_px)
                print(f"[Orders] Trailing stop: moved to break-even on {pair}")
                acted = True

        else:  # sell
            trail_trigger_px   = entry - r1_dist * TRAIL_TRIGGER_R
            partial_close_px   = entry - r1_dist * PARTIAL_CLOSE_R
            lock_px            = entry - r1_dist * TRAIL_LOCK_R
            be_px              = entry - self.md.pips_to_price(1, pair)

            # Stage 2: partial close + lock remainder
            if current_price <= partial_close_px and current_sl > lock_px:
                close_units = round(current_units * PARTIAL_CLOSE_PCT)
                if close_units > 0:
                    self._close_partial(trade_id, pair, close_units, direction)
                if lock_px < current_sl:
                    self.modify_stop_loss(trade_id, lock_px)
                    print(f"[Orders] Trailing stop: locked 0.5R at {lock_px:.5f} on {pair}")
                acted = True

            # Stage 1: move to break-even
            elif current_price <= trail_trigger_px and current_sl > be_px:
                self.modify_stop_loss(trade_id, be_px)
                print(f"[Orders] Trailing stop: moved to break-even on {pair}")
                acted = True

        return acted

    def _close_partial(self, trade_id: str, pair: str, units: int, direction: str):
        """Closes a partial number of units on an open trade."""
        close_units = -units if direction == "buy" else units
        try:
            data = {"units": str(close_units)}
            r = trades.TradeClose(self.client.account_id, tradeID=trade_id, data=data)
            self.client.client.request(r)
            print(f"[Orders] Partial close: {units:,} units on {pair} (trade {trade_id})")
        except Exception as e:
            print(f"[Orders] ❌ Partial close failed on {trade_id}: {e}")

    # ── End of Day Cleanup ─────────────────────────────────────

    def end_of_day_close(self) -> int:
        """
        Closes all positions before weekend or end of session.
        Call at 21:00 UTC Friday.
        """
        print("[Orders] End-of-day close — closing all open positions...")
        return self.close_all_trades()

    # ── Print Open Trades ──────────────────────────────────────

    def print_open_trades(self):
        open_trades = self.get_open_trades()
        if not open_trades:
            print("\n[Orders] No open trades.\n")
            return

        print(f"\n{'='*60}")
        print(f"  OPEN TRADES ({len(open_trades)})")
        print(f"{'='*60}")
        for t in open_trades:
            units     = int(t["currentUnits"])
            direction = "BUY" if units > 0 else "SELL"
            pair      = t["instrument"]
            open_px   = float(t["price"])
            unreal_pl = float(t["unrealizedPL"])
            sl        = t.get("stopLossOrder", {}).get("price", "—")
            tp        = t.get("takeProfitOrder", {}).get("price", "—")
            pl_str    = f"+${unreal_pl:.2f}" if unreal_pl >= 0 else f"-${abs(unreal_pl):.2f}"

            print(f"  [{t['id']}] {pair} {direction} {abs(units):,} units")
            print(f"    Open: {open_px:.5f}  SL: {sl}  TP: {tp}  P&L: {pl_str}")
        print()