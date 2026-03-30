from oanda.client import OandaClient
from config import PAIRS


def main():
    print("\n[Forex Bot] Starting up...")

    # ── Step 1: Test OANDA connection ──────────────────────────
    client = OandaClient()
    ok = client.test_connection()

    if not ok:
        print("[Forex Bot] Aborting — fix connection before proceeding.")
        return

    # ── Step 2: Fetch live prices for all configured pairs ─────
    print("[Forex Bot] Fetching live prices for all pairs...\n")
    prices = client.get_prices(PAIRS)

    print(f"  {'PAIR':<12} {'BID':<12} {'ASK':<12} {'SPREAD':<10} {'TRADEABLE'}")
    print("  " + "-"*55)
    for p in prices:
        print(f"  {p['pair']:<12} {p['bid']:<12} {p['ask']:<12} {p['spread']:<10.5f} {p['tradeable']}")

    # ── Step 3: Fetch sample candles for primary pair ──────────
    print(f"\n[Forex Bot] Fetching last 10 H1 candles for EUR_USD...\n")
    candles = client.get_candles("EUR_USD", granularity="H1", count=10)

    print(f"  {'TIME':<35} {'OPEN':<10} {'HIGH':<10} {'LOW':<10} {'CLOSE':<10} {'VOL'}")
    print("  " + "-"*85)
    for c in candles:
        t = c['time'][:19].replace("T", " ")
        print(f"  {t:<35} {c['open']:<10} {c['high']:<10} {c['low']:<10} {c['close']:<10} {c['volume']}")

    print("\n[Forex Bot] Phase 1 foundation ready. ✓")
    print("[Forex Bot] Next: calendar/fetcher.py — economic event integration.\n")


if __name__ == "__main__":
    main()