import sys
import os
import time
import argparse
from datetime import datetime, timezone, timedelta

from oanda.client import OandaClient
from oanda.market_data import MarketData
from oanda.orders import OrderExecutor, _append_trade_csv
from econ_calendar.fetcher import fetch_weekly_events, get_todays_events
from econ_calendar.filter import is_in_blackout, calculate_weekly_bias, print_weekly_bias, minutes_to_next_event
from risk.manager import RiskManager
from strategies.london_breakout import LondonBreakout
from strategies.ny_breakout import NYBreakout
from config import PAIRS, PRIMARY_PAIR, LOG_DIR

# ── DRY RUN FLAG ───────────────────────────────────────────────
# Set to False only when you're ready to place real practice orders
DRY_RUN = True

# ── Scan interval inside the breakout window (seconds) ────────
SCAN_INTERVAL_SECS = 15 * 60   # 15 minutes — matches M15 granularity


# ── Logging ────────────────────────────────────────────────────

class Tee:
    """
    Writes to both stdout and a dated log file simultaneously.
    Replaces sys.stdout for the duration of the process.
    """
    def __init__(self, log_path: str):
        self.terminal = sys.__stdout__
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        self.logfile  = open(log_path, "a", buffering=1)   # line-buffered

    def write(self, message):
        self.terminal.write(message)
        self.logfile.write(message)

    def flush(self):
        self.terminal.flush()
        self.logfile.flush()

    def close(self):
        self.logfile.close()


def setup_logging():
    today    = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    log_path = os.path.join(LOG_DIR, f"bot_{today}.log")
    tee      = Tee(log_path)
    sys.stdout = tee
    print(f"[Log] Writing to {log_path}")
    return tee


# ── Single trading cycle ────────────────────────────────────────

def run_cycle(client, risk, executor, breakout, dry_run: bool, ny_breakout=None):
    """Runs one full scan-and-execute cycle."""
    now = datetime.now(timezone.utc)
    print(f"\n{'─'*60}")
    print(f"[Cycle] {now.strftime('%Y-%m-%d %H:%M')} UTC")
    print(f"{'─'*60}")

    # ── Economic Calendar ─────────────────────────────────────
    all_events    = fetch_weekly_events()
    todays_events = get_todays_events(all_events)

    # ── Weekly Bias ───────────────────────────────────────────
    bias = calculate_weekly_bias(all_events)
    print_weekly_bias(bias)

    # ── Blackout Check ────────────────────────────────────────
    blocked, reason = is_in_blackout(todays_events, now=now)
    if blocked:
        print(f"[Blackout]  🔴 TRADING BLOCKED — {reason}")
        return
    else:
        mins    = minutes_to_next_event(todays_events, now=now)
        next_str = f"  Next event in {mins} min." if mins else "  No more events today."
        print(f"[Blackout]  🟢 CLEAR TO TRADE.{next_str}")

    # ── Live Prices ───────────────────────────────────────────
    prices = client.get_prices(PAIRS)
    print(f"\n  {'PAIR':<12} {'BID':<12} {'ASK':<12} {'SPREAD'}")
    print("  " + "-"*46)
    for p in prices:
        print(f"  {p['pair']:<12} {p['bid']:<12} {p['ask']:<12} {p['spread']:.5f}")

    # ── Risk Status ───────────────────────────────────────────
    risk.print_risk_status()

    # ── London Breakout Scan ──────────────────────────────────
    executor_obj = executor
    open_trades = executor_obj.get_open_trades()
    print("[London Breakout] Running scan...\n")
    signals = breakout.scan(events=todays_events, bias=bias, open_trades=open_trades)

    # ── Execution ─────────────────────────────────────────────
    if signals:
        print(f"\n[Executor] {len(signals)} signal(s) ready.")
        for signal in signals:
            if dry_run:
                scalar = 0.5 if bias.get("is_fomc_week") else 1.0
                units  = risk.calculate_units(
                    pair=signal.pair,
                    direction=signal.direction,
                    stop_pips=signal.stop_pips,
                    scalar=scalar,
                )
                print(f"\n[DRY RUN] Would place:")
                print(f"  Pair        : {signal.pair}")
                print(f"  Direction   : {signal.direction.upper()}")
                print(f"  Units       : {units:+,}")
                print(f"  Entry       : {signal.entry_price:.5f}")
                print(f"  Stop Loss   : {signal.stop_loss:.5f}")
                print(f"  Take Profit : {signal.take_profit:.5f}")
                print(f"  RR          : 1:{signal.rr_ratio}")
                _append_trade_csv({
                    "timestamp":   datetime.now(timezone.utc).isoformat(),
                    "pair":        signal.pair,
                    "direction":   signal.direction,
                    "entry":       signal.entry_price,
                    "stop_loss":   signal.stop_loss,
                    "take_profit": signal.take_profit,
                    "units":       units,
                    "rr_ratio":    signal.rr_ratio,
                    "scalar":      scalar,
                    "mode":        "dry_run",
                })
            else:
                scalar = 0.5 if bias.get("is_fomc_week") else 1.0
                executor_obj.execute_signal(signal, scalar=scalar, label="london_breakout")
                breakout.mark_fired(signal.pair)
    else:
        print(f"\n[Executor] No signals to execute.")

    # ── NY Open Breakout Scan ─────────────────────────────────
    if ny_breakout:
        ny_signals = ny_breakout.scan(events=todays_events, bias=bias, open_trades=open_trades)
        if ny_signals:
            print(f"\n[NY Executor] {len(ny_signals)} signal(s) ready.")
            for signal in ny_signals:
                scalar = 0.5 if bias.get("is_fomc_week") else 1.0
                if dry_run:
                    units = risk.calculate_units(
                        pair=signal.pair,
                        direction=signal.direction,
                        stop_pips=signal.stop_pips,
                        scalar=scalar,
                    )
                    print(f"\n[DRY RUN] Would place:")
                    print(f"  Pair        : {signal.pair}")
                    print(f"  Direction   : {signal.direction.upper()}")
                    print(f"  Units       : {units:+,}")
                    print(f"  Entry       : {signal.entry_price:.5f}")
                    print(f"  Stop Loss   : {signal.stop_loss:.5f}")
                    print(f"  Take Profit : {signal.take_profit:.5f}")
                    print(f"  RR          : 1:{signal.rr_ratio}")
                    _append_trade_csv({
                        "timestamp":   datetime.now(timezone.utc).isoformat(),
                        "pair":        signal.pair,
                        "direction":   signal.direction,
                        "entry":       signal.entry_price,
                        "stop_loss":   signal.stop_loss,
                        "take_profit": signal.take_profit,
                        "units":       units,
                        "rr_ratio":    signal.rr_ratio,
                        "scalar":      scalar,
                        "mode":        "dry_run",
                    })
                else:
                    executor_obj.execute_signal(signal, scalar=scalar, label="ny_breakout")
                    ny_breakout.mark_fired(signal.pair)
        else:
            print(f"\n[NY Executor] No signals to execute.")

    # ── Open Trades ───────────────────────────────────────────
    executor_obj.print_open_trades()

    # ── Trailing Stop Management ──────────────────────────────
    if not dry_run:
        managed = 0
        for trade in open_trades:
            comment  = trade.get("clientExtensions", {}).get("comment", "")
            isl_part = next((p for p in comment.split("|") if p.startswith("isl=")), None)
            if isl_part is None:
                continue

            entry      = float(trade["price"])
            initial_sl = float(isl_part[4:])
            direction  = "buy" if int(trade["currentUnits"]) > 0 else "sell"
            acted = executor_obj.apply_trailing_stop(
                trade_id=trade["id"],
                pair=trade["instrument"],
                entry=entry,
                direction=direction,
                initial_sl=initial_sl,
            )
            if acted:
                managed += 1

        if open_trades:
            print(f"[Trailing Stop] Checked {len(open_trades)} trade(s), "
                  f"acted on {managed}.\n")


# ── Scheduler helpers ───────────────────────────────────────────

def _next_window_start(now: datetime) -> datetime:
    """Returns the next scan window start (06:45 or 12:45 UTC) strictly after now."""
    today_london = now.replace(hour=6,  minute=45, second=0, microsecond=0)
    today_ny     = now.replace(hour=12, minute=45, second=0, microsecond=0)
    candidates   = [t for t in [today_london, today_ny] if t > now]
    if candidates:
        return min(candidates)
    return today_london + timedelta(days=1)


def _in_scan_window(now: datetime) -> bool:
    """True during 06:45–09:00 UTC (London) or 12:45–15:00 UTC (NY)."""
    london = (now.hour == 6 and now.minute >= 45) or (now.hour == 7) or (now.hour == 8)
    ny     = (now.hour == 12 and now.minute >= 45) or (now.hour == 13) or (now.hour == 14)
    return london or ny


def _seconds_until(target: datetime, now: datetime) -> float:
    return max(0.0, (target - now).total_seconds())


# ── Entry points ────────────────────────────────────────────────

def main_once(dry_run: bool):
    """Single-shot run (original behaviour)."""
    print("\n[Forex Bot] Starting up...")
    if dry_run:
        print("[Forex Bot] ⚠️  DRY RUN MODE — no orders will be placed.\n")

    client = OandaClient()
    if not client.test_connection():
        print("[Forex Bot] Aborting — fix connection before proceeding.")
        return

    md          = MarketData(client)
    risk        = RiskManager(client)
    executor    = OrderExecutor(client=client, market_data=md, risk=risk)
    breakout    = LondonBreakout(
        client=client, market_data=md, risk_manager=risk,
        pairs=["EUR_USD", "GBP_USD", "USD_JPY"],
    )
    ny_breakout = NYBreakout(
        client=client, market_data=md, risk_manager=risk,
        pairs=["EUR_USD", "GBP_USD", "USD_JPY"],
    )

    md.print_snapshot(PRIMARY_PAIR)
    run_cycle(client, risk, executor, breakout, dry_run, ny_breakout=ny_breakout)
    print("\n[Forex Bot] Done. ✓")


def main_loop(dry_run: bool):
    """
    Continuous loop mode.

    Sleeps until 06:45 UTC, then polls every 15 minutes through 09:00 UTC.
    After the window closes, sleeps until 06:45 the next day.
    Resets fired-today state at midnight UTC.
    """
    print("\n[Forex Bot] Starting in LOOP mode...")
    if dry_run:
        print("[Forex Bot] ⚠️  DRY RUN MODE — no orders will be placed.\n")

    client = OandaClient()
    if not client.test_connection():
        print("[Forex Bot] Aborting — fix connection before proceeding.")
        return

    md          = MarketData(client)
    risk        = RiskManager(client)
    executor    = OrderExecutor(client=client, market_data=md, risk=risk)
    breakout    = LondonBreakout(
        client=client, market_data=md, risk_manager=risk,
        pairs=["EUR_USD", "GBP_USD", "USD_JPY"],
    )
    ny_breakout = NYBreakout(
        client=client, market_data=md, risk_manager=risk,
        pairs=["EUR_USD", "GBP_USD", "USD_JPY"],
    )

    last_reset_date = None

    while True:
        now = datetime.now(timezone.utc)

        # ── Daily reset at UTC midnight ────────────────────────
        today = now.date()
        if last_reset_date != today:
            breakout.reset_daily()
            ny_breakout.reset_daily()
            # Rotate log file at midnight
            setup_logging()
            last_reset_date = today
            print(f"[Forex Bot] Daily reset — {today}")

        # ── Outside scan window: sleep until 06:45 UTC ─────────
        if not _in_scan_window(now):
            wake = _next_window_start(now)
            secs = _seconds_until(wake, now)
            print(f"[Scheduler] Outside window. Sleeping {secs/3600:.1f}h "
                  f"until {wake.strftime('%H:%M')} UTC...")
            time.sleep(secs)
            continue

        # ── Inside scan window: run cycle ──────────────────────
        run_cycle(client, risk, executor, breakout, dry_run, ny_breakout=ny_breakout)

        # Sleep until next scan, or exit window
        now = datetime.now(timezone.utc)
        if _in_scan_window(now):
            print(f"[Scheduler] Next scan in {SCAN_INTERVAL_SECS//60} min.")
            time.sleep(SCAN_INTERVAL_SECS)
        # else: loop will immediately go to sleep-until-06:45 branch


# ── CLI ─────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Forex trading bot — London Breakout")
    parser.add_argument(
        "--loop", action="store_true",
        help="Run continuously, scanning every 15 min during 06:45–09:00 UTC"
    )
    parser.add_argument(
        "--live", action="store_true",
        help="Disable dry-run mode and place real orders"
    )
    args = parser.parse_args()

    dry_run = not args.live
    tee     = setup_logging()

    try:
        if args.loop:
            main_loop(dry_run)
        else:
            main_once(dry_run)
    finally:
        sys.stdout = sys.__stdout__
        tee.close()
