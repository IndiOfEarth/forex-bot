from datetime import datetime, timezone, timedelta
from config import (
    BLACKOUT_BEFORE_TIER1, BLACKOUT_AFTER_TIER1,
    BLACKOUT_BEFORE_TIER2, BLACKOUT_AFTER_TIER2,
    ALLOWED_TRADE_HOURS_UTC,
)


# ── Blackout Check ─────────────────────────────────────────────

def is_in_blackout(events: list[dict], now: datetime = None) -> tuple[bool, str]:
    """
    Returns (True, reason) if trading should be blocked right now.
    Returns (False, "") if safe to trade.

    Checks:
      - Session time gate (only trade allowed hours)
      - News blackout windows around Tier 1 and Tier 2 events
    """
    if now is None:
        now = datetime.now(timezone.utc)

    # ── Session gate ───────────────────────────────────────────
    if now.hour not in ALLOWED_TRADE_HOURS_UTC:
        return True, f"Outside allowed session hours (UTC {now.hour}:00)"

    # ── News blackout ──────────────────────────────────────────
    for event in events:
        event_time = event["datetime_utc"]
        if event_time is None:
            continue

        tier = event["tier"]
        before_mins = BLACKOUT_BEFORE_TIER1 if tier == 1 else BLACKOUT_BEFORE_TIER2
        after_mins  = BLACKOUT_AFTER_TIER1  if tier == 1 else BLACKOUT_AFTER_TIER2

        window_start = event_time - timedelta(minutes=before_mins)
        window_end   = event_time + timedelta(minutes=after_mins)

        if window_start <= now <= window_end:
            label = event["title"]
            country = event["country"]
            return True, f"News blackout — {country} {label} (Tier {tier}) at {event_time.strftime('%H:%M UTC')}"

    return False, ""


def minutes_to_next_event(events: list[dict], now: datetime = None) -> int | None:
    """Returns minutes until the next upcoming event, or None if no events upcoming today."""
    if now is None:
        now = datetime.now(timezone.utc)

    upcoming = [e for e in events if e["datetime_utc"] and e["datetime_utc"] > now]
    if not upcoming:
        return None

    next_event = min(upcoming, key=lambda e: e["datetime_utc"])
    delta = next_event["datetime_utc"] - now
    return int(delta.total_seconds() / 60)


# ── Weekly Bias Engine ─────────────────────────────────────────

# How each event title maps to a USD directional bias
# Positive = USD bullish signal, Negative = USD bearish signal
EVENT_BIAS_RULES = {
    # USD Bullish signals
    "Non-Farm Employment Change":  +2,
    "Non-Farm Payrolls":           +2,
    "CPI":                         +1,   # high CPI = hawkish Fed = USD up
    "Core CPI":                    +1,
    "Unemployment Claims":         -1,   # high claims = USD bearish
    "Unemployment Rate":           -1,
    "FOMC":                        0,    # neutral — too unpredictable
    "GDP":                         +1,
    "Retail Sales":                +1,
    "ISM Manufacturing PMI":       +1,
    "ISM Services PMI":            +1,
    "PCE":                         +1,
    "Core PCE":                    +1,
    "PPI":                         +1,
    "ADP Non-Farm":                +1,
    "Fed Chair":                   0,    # speech — direction unknown
    "FOMC Minutes":                0,
}

FOMC_KEYWORDS = ["FOMC", "Federal Reserve", "Fed Chair", "Powell"]


def calculate_weekly_bias(events: list[dict]) -> dict:
    """
    Scans the week's events and returns a bias dict:
    {
        usd_score:      int,     # positive = bullish USD, negative = bearish
        bias:           str,     # "bullish_usd" | "bearish_usd" | "neutral"
        is_fomc_week:   bool,
        tier1_count:    int,
        summary:        list[str],  # human-readable reasoning
    }
    """
    usd_score   = 0
    is_fomc     = False
    summary     = []
    tier1_count = len([e for e in events if e["tier"] == 1])

    for event in events:
        if event["country"] != "USD":
            continue

        title = event["title"]

        # Check FOMC week
        if any(kw in title for kw in FOMC_KEYWORDS):
            is_fomc = True

        # Score the event
        for keyword, score in EVENT_BIAS_RULES.items():
            if keyword.lower() in title.lower():
                usd_score += score
                if score != 0:
                    direction = "bullish" if score > 0 else "bearish"
                    summary.append(f"{title} → USD {direction} ({'+' if score > 0 else ''}{score})")
                break

    # Determine overall bias
    if usd_score >= 3:
        bias = "bullish_usd"
    elif usd_score <= -2:
        bias = "bearish_usd"
    else:
        bias = "neutral"

    return {
        "usd_score":    usd_score,
        "bias":         bias,
        "is_fomc_week": is_fomc,
        "tier1_count":  tier1_count,
        "summary":      summary,
    }


def get_position_size_scalar(bias: dict) -> float:
    """
    Returns a multiplier applied to base position size.
    FOMC week: reduce to 50%.
    Strong bias alignment: allow up to 1.5x.
    Neutral: normal sizing.
    """
    if bias["is_fomc_week"]:
        return 0.5

    if bias["usd_score"] >= 4:
        return 1.5   # strong macro tailwind — allow slightly larger size

    return 1.0       # default


# ── News Deviation Flag ────────────────────────────────────────

def flag_news_deviation(event: dict, actual: float) -> dict | None:
    """
    After a news release, compare actual vs forecast.
    Returns a directional flag if deviation is significant.

    Usage: call this after you manually input the actual figure
    (or once a news API with actuals is integrated).

    Returns:
        {
            direction:   "bullish_usd" | "bearish_usd" | None
            magnitude:   float   # absolute deviation
            significant: bool
        }
    """
    forecast_str = event.get("forecast", "")
    if not forecast_str:
        return None

    try:
        # Strip % and K/M suffixes for comparison
        forecast = float(forecast_str.replace("%", "").replace("K", "").replace("M", ""))
        deviation = actual - forecast
        magnitude = abs(deviation)

        # Thresholds — these are rough; tune per event type
        significant = magnitude >= 0.2

        if not significant:
            return {"direction": None, "magnitude": magnitude, "significant": False}

        # For most USD events: beat forecast = bullish USD
        direction = "bullish_usd" if deviation > 0 else "bearish_usd"

        # Special case: unemployment — higher than forecast = bearish USD
        title = event.get("title", "").lower()
        if "unemployment" in title or "claims" in title:
            direction = "bearish_usd" if deviation > 0 else "bullish_usd"

        return {
            "direction":   direction,
            "magnitude":   magnitude,
            "significant": True,
        }

    except (ValueError, TypeError):
        return None


def print_weekly_bias(bias: dict):
    print(f"\n{'='*60}")
    print(f"  WEEKLY BIAS ENGINE")
    print(f"{'='*60}")
    print(f"  USD Score    : {bias['usd_score']:+d}")
    print(f"  Bias         : {bias['bias'].upper()}")
    print(f"  FOMC Week    : {'YES — reduce position sizes 50%' if bias['is_fomc_week'] else 'No'}")
    print(f"  Tier 1 Events: {bias['tier1_count']}")
    print(f"\n  Scoring breakdown:")
    for line in bias["summary"]:
        print(f"    • {line}")
    print()