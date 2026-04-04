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
from strategies.tokyo_breakout import TokyoBreakout
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

def run_cycle(client, risk, executor, breakout, dry_run: bool, ny_breakout=None, tokyo_breakout=None):
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
            base_scalar = 0.5 if bias.get("is_fomc_week") else 1.0
            scalar      = base_scalar * signal.vol_scalar
            if dry_run:
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
                base_scalar = 0.5 if bias.get("is_fomc_week") else 1.0
                scalar      = base_scalar * signal.vol_scalar
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

    # ── Tokyo Breakout Scan ───────────────────────────────────
    if tokyo_breakout:
        tokyo_signals = tokyo_breakout.scan(events=todays_events, bias=bias, open_trades=open_trades)
        if tokyo_signals:
            print(f"\n[Tokyo Executor] {len(tokyo_signals)} signal(s) ready.")
            for signal in tokyo_signals:
                base_scalar = 0.5 if bias.get("is_fomc_week") else 1.0
                scalar      = base_scalar * signal.vol_scalar
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
                    executor_obj.execute_signal(signal, scalar=scalar, label="tokyo_breakout")
                    tokyo_breakout.mark_fired(signal.pair)
        else:
            print(f"\n[Tokyo Executor] No signals to execute.")

        # EUR/JPY force-close at 07:00 UTC before London open
        if now.hour >= 7:
            to_close = tokyo_breakout.get_positions_to_close(open_trades)
            if to_close:
                print(f"\n[Tokyo] Force-closing {len(to_close)} EUR/JPY position(s) before London open.")
                if not dry_run:
                    for trade in to_close:
                        executor_obj.close_trade(trade["id"])
                else:
                    for trade in to_close:
                        print(f"  [DRY RUN] Would close trade {trade['id']} ({trade.get('instrument')})")

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

def _in_london_window(now: datetime) -> bool:
    """True during 06:45–09:00 UTC."""
    return (now.hour == 6 and now.minute >= 45) or (now.hour == 7) or (now.hour == 8)


def _in_tokyo_window(now: datetime) -> bool:
    """True during 01:45–06:00 UTC (pre-scan buffer + full Tokyo entry window)."""
    return (now.hour == 1 and now.minute >= 45) or (2 <= now.hour < 6)


def _in_scan_window(now: datetime, enable_ny: bool = False, enable_tokyo: bool = False) -> bool:
    """True during any active scan window."""
    if enable_ny:
        ny = (now.hour == 12 and now.minute >= 45) or (now.hour == 13) or (now.hour == 14)
    else:
        ny = False
    tokyo = _in_tokyo_window(now) if enable_tokyo else False
    return _in_london_window(now) or ny or tokyo


def _next_window_start(now: datetime, enable_ny: bool = False, enable_tokyo: bool = False) -> datetime:
    """Returns the next scan window start strictly after now."""
    candidates = []
    today_london = now.replace(hour=6,  minute=45, second=0, microsecond=0)
    candidates.append(today_london)
    if enable_ny:
        candidates.append(now.replace(hour=12, minute=45, second=0, microsecond=0))
    if enable_tokyo:
        candidates.append(now.replace(hour=1,  minute=45, second=0, microsecond=0))
    future = [t for t in candidates if t > now]
    if future:
        return min(future)
    return today_london + timedelta(days=1)


def _seconds_until(target: datetime, now: datetime) -> float:
    return max(0.0, (target - now).total_seconds())


# ── Entry points ────────────────────────────────────────────────

def main_once(dry_run: bool, enable_ny: bool = False, enable_tokyo: bool = False):
    """Single-shot run (original behaviour)."""
    print("\n[Forex Bot] Starting up...")
    if dry_run:
        print("[Forex Bot] ⚠️  DRY RUN MODE — no orders will be placed.\n")

    client = OandaClient()
    if not client.test_connection():
        print("[Forex Bot] Aborting — fix connection before proceeding.")
        return

    md       = MarketData(client)
    risk     = RiskManager(client)
    executor = OrderExecutor(client=client, market_data=md, risk=risk)
    breakout = LondonBreakout(
        client=client, market_data=md, risk_manager=risk,
        pairs=["EUR_USD", "GBP_USD", "USD_JPY"],
    )
    ny_breakout = NYBreakout(
        client=client, market_data=md, risk_manager=risk,
        pairs=["EUR_USD", "GBP_USD", "USD_JPY"],
    ) if enable_ny else None
    tokyo_breakout = TokyoBreakout(
        client=client, market_data=md, risk_manager=risk,
        pairs=["EUR_JPY", "USD_JPY"],
    ) if enable_tokyo else None

    if enable_ny:
        print("[Forex Bot] NY open breakout strategy ENABLED.\n")
    if enable_tokyo:
        print("[Forex Bot] Tokyo breakout strategy ENABLED.\n")

    md.print_snapshot(PRIMARY_PAIR)
    run_cycle(client, risk, executor, breakout, dry_run,
              ny_breakout=ny_breakout, tokyo_breakout=tokyo_breakout)
    print("\n[Forex Bot] Done. ✓")


def main_loop(dry_run: bool, enable_ny: bool = False, enable_tokyo: bool = False):
    """
    Continuous loop mode.

    Sleeps until 06:45 UTC, then polls every 15 minutes through 09:00 UTC.
    After the window closes, sleeps until the next active window.
    Resets fired-today state at midnight UTC.
    With --ny:    also scans 12:45–15:00 UTC for the NY open breakout.
    With --tokyo: also scans 01:45–06:00 UTC for the Tokyo breakout.
    """
    print("\n[Forex Bot] Starting in LOOP mode...")
    if dry_run:
        print("[Forex Bot] ⚠️  DRY RUN MODE — no orders will be placed.\n")

    client = OandaClient()
    if not client.test_connection():
        print("[Forex Bot] Aborting — fix connection before proceeding.")
        return

    md       = MarketData(client)
    risk     = RiskManager(client)
    executor = OrderExecutor(client=client, market_data=md, risk=risk)
    breakout = LondonBreakout(
        client=client, market_data=md, risk_manager=risk,
        pairs=["EUR_USD", "GBP_USD", "USD_JPY"],
    )
    ny_breakout = NYBreakout(
        client=client, market_data=md, risk_manager=risk,
        pairs=["EUR_USD", "GBP_USD", "USD_JPY"],
    ) if enable_ny else None
    tokyo_breakout = TokyoBreakout(
        client=client, market_data=md, risk_manager=risk,
        pairs=["EUR_JPY", "USD_JPY"],
    ) if enable_tokyo else None

    if enable_ny:
        print("[Forex Bot] NY open breakout strategy ENABLED.\n")
    if enable_tokyo:
        print("[Forex Bot] Tokyo breakout strategy ENABLED.\n")

    last_reset_date = None

    while True:
        now = datetime.now(timezone.utc)

        # ── Daily reset at UTC midnight ────────────────────────
        today = now.date()
        if last_reset_date != today:
            breakout.reset_daily()
            if ny_breakout:
                ny_breakout.reset_daily()
            if tokyo_breakout:
                tokyo_breakout.reset_daily()
            setup_logging()
            last_reset_date = today
            print(f"[Forex Bot] Daily reset — {today}")

        # ── Outside scan window: sleep until next window start ──
        in_window = _in_scan_window(now, enable_ny=enable_ny, enable_tokyo=enable_tokyo)
        if not in_window:
            wake = _next_window_start(now, enable_ny=enable_ny, enable_tokyo=enable_tokyo)
            secs = _seconds_until(wake, now)
            print(f"[Scheduler] Outside window. Sleeping {secs/3600:.1f}h "
                  f"until {wake.strftime('%H:%M')} UTC...")
            time.sleep(secs)
            continue

        # ── Inside scan window: run cycle ──────────────────────
        run_cycle(client, risk, executor, breakout, dry_run,
                  ny_breakout=ny_breakout, tokyo_breakout=tokyo_breakout)

        # Sleep until next scan, or exit window
        now = datetime.now(timezone.utc)
        if _in_scan_window(now, enable_ny=enable_ny, enable_tokyo=enable_tokyo):
            print(f"[Scheduler] Next scan in {SCAN_INTERVAL_SECS//60} min.")
            time.sleep(SCAN_INTERVAL_SECS)
        # else: loop will immediately go to sleep-until-next-window branch


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
    parser.add_argument(
        "--ny", action="store_true",
        help="Enable NY open breakout strategy (13:00–15:00 UTC); London strategy always runs"
    )
    parser.add_argument(
        "--tokyo", action="store_true",
        help="Enable Tokyo breakout strategy (01:45–06:00 UTC); EUR/JPY + USD/JPY"
    )
    args = parser.parse_args()

    dry_run      = not args.live
    enable_ny    = args.ny
    enable_tokyo = args.tokyo
    tee          = setup_logging()

    try:
        if args.loop:
            main_loop(dry_run, enable_ny=enable_ny, enable_tokyo=enable_tokyo)
        else:
            main_once(dry_run, enable_ny=enable_ny, enable_tokyo=enable_tokyo)
    finally:
        sys.stdout = sys.__stdout__
        tee.close()
