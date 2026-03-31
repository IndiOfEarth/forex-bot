from datetime import datetime, timezone

from oanda.client import OandaClient
from oanda.market_data import MarketData
from econ_calendar.fetcher import fetch_weekly_events, get_todays_events, print_events
from econ_calendar.filter import is_in_blackout, calculate_weekly_bias, print_weekly_bias, minutes_to_next_event
from risk.manager import RiskManager
from config import PAIRS, PRIMARY_PAIR


def main():
    print("\n[Forex Bot] Starting up...")

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

    # ── 8. Sample pre-trade check ─────────────────────────────
    print("[Pre-Trade Check Sample — EUR_USD BUY, 20 pip stop, 50 pip target]")
    risk.pre_trade_check(
        pair="EUR_USD",
        direction="buy",
        stop_pips=20,
        target_pips=50,
    )

    print("\n[Forex Bot] Core foundation complete. ✓")
    print("[Forex Bot] Next: strategies/london_breakout.py\n")


if __name__ == "__main__":
    main()