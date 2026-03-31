from datetime import datetime, timezone

from oanda.client import OandaClient
from oanda.market_data import MarketData
from oanda.orders import OrderExecutor
from econ_calendar.fetcher import fetch_weekly_events, get_todays_events, print_events
from econ_calendar.filter import is_in_blackout, calculate_weekly_bias, print_weekly_bias, minutes_to_next_event
from risk.manager import RiskManager
from strategies.london_breakout import LondonBreakout
from config import PAIRS, PRIMARY_PAIR

# ── DRY RUN FLAG ───────────────────────────────────────────────
# Set to False only when you're ready to place real practice orders
DRY_RUN = True


def main():
    print("\n[Forex Bot] Starting up...")
    if DRY_RUN:
        print("[Forex Bot] ⚠️  DRY RUN MODE — no orders will be placed.\n")

    # ── 1. OANDA Connection ────────────────────────────────────
    client = OandaClient()
    ok = client.test_connection()
    if not ok:
        print("[Forex Bot] Aborting — fix connection before proceeding.")
        return

    # ── 2. Economic Calendar ───────────────────────────────────
    print("[Forex Bot] Fetching economic calendar...")
    all_events    = fetch_weekly_events()
    todays_events = get_todays_events(all_events)

    print_events(all_events,    label="This Week's High/Medium Events")
    print_events(todays_events, label="Today's Events")

    # ── 3. Weekly Bias ─────────────────────────────────────────
    bias = calculate_weekly_bias(all_events)
    print_weekly_bias(bias)

    # ── 4. Blackout Check ─────────────────────────────────────
    now = datetime.now(timezone.utc)
    blocked, reason = is_in_blackout(todays_events, now=now)

    print(f"[Blackout Check]  UTC now: {now.strftime('%H:%M')}")
    if blocked:
        print(f"  🔴 TRADING BLOCKED — {reason}")
    else:
        mins = minutes_to_next_event(todays_events, now=now)
        next_str = f"  Next event in {mins} min." if mins else "  No more events today."
        print(f"  🟢 CLEAR TO TRADE.{next_str}")

    # ── 5. Live Prices ─────────────────────────────────────────
    print(f"\n[Live Prices]\n")
    prices = client.get_prices(PAIRS)
    print(f"  {'PAIR':<12} {'BID':<12} {'ASK':<12} {'SPREAD'}")
    print("  " + "-"*46)
    for p in prices:
        print(f"  {p['pair']:<12} {p['bid']:<12} {p['ask']:<12} {p['spread']:.5f}")

    # ── 6. Market Snapshot ────────────────────────────────────
    md = MarketData(client)
    md.print_snapshot(PRIMARY_PAIR)

    # ── 7. Risk Status ────────────────────────────────────────
    risk = RiskManager(client)
    risk.print_risk_status()

    # ── 8. London Breakout Scan ───────────────────────────────
    print("[London Breakout] Running scan...\n")
    breakout = LondonBreakout(
        client=client,
        market_data=md,
        risk_manager=risk,
        pairs=["EUR_USD", "GBP_USD"],
    )
    signals = breakout.scan(events=todays_events, bias=bias)

    # ── 9. Execution ──────────────────────────────────────────
    executor = OrderExecutor(client=client, market_data=md, risk=risk)

    if signals:
        print(f"\n[Executor] {len(signals)} signal(s) ready.")
        for signal in signals:
            if DRY_RUN:
                # Show what would be placed without touching the market
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
            else:
                scalar = 0.5 if bias.get("is_fomc_week") else 1.0
                executor.execute_signal(signal, scalar=scalar, label="london_breakout")
                breakout.mark_fired(signal.pair)
    else:
        print(f"\n[Executor] No signals to execute.")

    # ── 10. Open Trades ───────────────────────────────────────
    executor.print_open_trades()

    print("\n[Forex Bot] All systems running. ✓")
    print("[Forex Bot] Next: backtest/engine.py\n")


if __name__ == "__main__":
    main()