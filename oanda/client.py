import time
import requests.exceptions

import oandapyV20
import oandapyV20.endpoints.accounts as accounts
import oandapyV20.endpoints.pricing as pricing
import oandapyV20.endpoints.instruments as instruments
import oandapyV20.endpoints.trades as trades_ep

from config import OANDA_API_KEY, OANDA_ACCOUNT_ID, OANDA_ENVIRONMENT

_RETRY_EXCEPTIONS = (
    requests.exceptions.ConnectionError,
    requests.exceptions.Timeout,
)


class OandaClient:
    """
    Core OANDA API client.
    Wraps oandapyV20 with clean methods for the rest of the bot to use.
    """

    def __init__(self):
        self.account_id = OANDA_ACCOUNT_ID
        self.client = oandapyV20.API(
            access_token=OANDA_API_KEY,
            environment=OANDA_ENVIRONMENT  # "practice" or "live"
        )
        print(f"[OandaClient] Connected — environment: {OANDA_ENVIRONMENT.upper()}")

    def _request(self, endpoint, retries: int = 3, backoff: float = 5.0):
        """Execute an oandapyV20 request with retry on transient network errors."""
        for attempt in range(1, retries + 1):
            try:
                self.client.request(endpoint)
                return
            except _RETRY_EXCEPTIONS as e:
                if attempt == retries:
                    raise
                wait = backoff * attempt
                print(f"[OandaClient] Network error (attempt {attempt}/{retries}): {e}. Retrying in {wait:.0f}s...")
                time.sleep(wait)

    # ── Account ────────────────────────────────────────────────

    def get_account_summary(self) -> dict:
        """Returns account balance, NAV, margin, open trade count."""
        r = accounts.AccountSummary(self.account_id)
        self._request(r)
        return r.response["account"]

    def get_account_balance(self) -> float:
        summary = self.get_account_summary()
        return float(summary["balance"])

    def get_open_trade_count(self) -> int:
        summary = self.get_account_summary()
        return int(summary["openTradeCount"])

    def get_nav(self) -> float:
        """Net Asset Value — balance + unrealised P&L."""
        summary = self.get_account_summary()
        return float(summary["NAV"])

    # ── Pricing ────────────────────────────────────────────────

    def get_price(self, pair: str) -> dict:
        """
        Returns current bid/ask for a single pair.
        pair: e.g. "EUR_USD"
        """
        params = {"instruments": pair}
        r = pricing.PricingInfo(self.account_id, params=params)
        self._request(r)
        price_data = r.response["prices"][0]
        return {
            "pair":    pair,
            "bid":     float(price_data["bids"][0]["price"]),
            "ask":     float(price_data["asks"][0]["price"]),
            "spread":  round(float(price_data["asks"][0]["price"]) - float(price_data["bids"][0]["price"]), 5),
            "tradeable": price_data["tradeable"],
        }

    def get_prices(self, pairs: list) -> list:
        """Returns bid/ask for multiple pairs at once."""
        params = {"instruments": ",".join(pairs)}
        r = pricing.PricingInfo(self.account_id, params=params)
        self._request(r)
        results = []
        for p in r.response["prices"]:
            results.append({
                "pair":    p["instrument"],
                "bid":     float(p["bids"][0]["price"]),
                "ask":     float(p["asks"][0]["price"]),
                "spread":  round(float(p["asks"][0]["price"]) - float(p["bids"][0]["price"]), 5),
                "tradeable": p["tradeable"],
            })
        return results

    # ── Candle Data ────────────────────────────────────────────

    def get_candles(self, pair: str, granularity: str = "H1", count: int = 200) -> list:
        """
        Fetch OHLCV candles.
        granularity: M1, M5, M15, H1, H4, D
        count: number of candles to return (max 5000)
        Returns list of dicts: {time, open, high, low, close, volume}
        """
        params = {
            "granularity": granularity,
            "count": count,
            "price": "M",  # midpoint candles
        }
        r = instruments.InstrumentsCandles(pair, params=params)
        self._request(r)

        candles = []
        for c in r.response["candles"]:
            if not c["complete"]:
                continue  # skip the current incomplete candle
            candles.append({
                "time":   c["time"],
                "open":   float(c["mid"]["o"]),
                "high":   float(c["mid"]["h"]),
                "low":    float(c["mid"]["l"]),
                "close":  float(c["mid"]["c"]),
                "volume": int(c["volume"]),
            })
        return candles

    # ── Trade History ─────────────────────────────────────────

    def get_recent_closed_trade_outcomes(self, count: int = 5) -> list[str]:
        """
        Returns the last `count` closed trades as a list of "win" or "loss" strings,
        ordered chronologically (oldest first). A trade is a win if realizedPL > 0.
        """
        params = {"state": "CLOSED", "count": str(count)}
        r = trades_ep.TradesList(self.account_id, params=params)
        self._request(r)
        outcomes = []
        for t in r.response.get("trades", []):
            pl = float(t.get("realizedPL", 0))
            outcomes.append("win" if pl > 0 else "loss")
        outcomes.reverse()   # OANDA returns newest-first; reverse to chronological
        return outcomes

    # ── Connection Test ────────────────────────────────────────

    def test_connection(self) -> bool:
        """Prints account summary and live EUR/USD price. Returns True if OK."""
        try:
            summary = self.get_account_summary()
            price   = self.get_price("EUR_USD")

            print("\n" + "="*50)
            print("  CONNECTION TEST — PASSED ✓")
            print("="*50)
            print(f"  Account ID   : {self.account_id}")
            print(f"  Balance      : ${float(summary['balance']):,.2f}")
            print(f"  NAV          : ${float(summary['NAV']):,.2f}")
            print(f"  Open Trades  : {summary['openTradeCount']}")
            print(f"  EUR/USD Bid  : {price['bid']}")
            print(f"  EUR/USD Ask  : {price['ask']}")
            print(f"  EUR/USD Spread: {price['spread']:.5f}")
            print(f"  Tradeable    : {price['tradeable']}")
            print("="*50 + "\n")
            return True

        except Exception as e:
            print(f"\n[CONNECTION FAILED] {e}\n")
            return False