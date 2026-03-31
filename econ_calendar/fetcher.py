import requests
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

# ForexFactory RSS — publicly available, no API key needed
FF_RSS_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.xml"

IMPACT_MAP = {
    "High":   1,
    "Medium": 2,
    "Low":    3,
    "Holiday": 4,
}

# Which currencies we care about (maps to our pairs)
WATCHED_CURRENCIES = {"USD", "EUR", "GBP", "JPY", "AUD", "CAD"}

# Tier classification by impact label
TIER_MAP = {
    "High":   1,
    "Medium": 2,
    "Low":    3,
}


def fetch_weekly_events() -> list[dict]:
    """
    Fetches this week's economic calendar from ForexFactory RSS.
    Returns a list of event dicts, sorted by UTC datetime.
    Each event:
        {
            title:      str,
            country:    str,      # e.g. "USD", "EUR"
            date:       str,      # ISO date string UTC
            datetime_utc: datetime,
            impact:     str,      # "High" | "Medium" | "Low"
            tier:       int,      # 1 | 2 | 3
            forecast:   str,
            previous:   str,
        }
    """
    try:
        response = requests.get(FF_RSS_URL, timeout=10)
        response.raise_for_status()
    except requests.RequestException as e:
        print(f"[Calendar] Failed to fetch ForexFactory feed: {e}")
        return []

    root = ET.fromstring(response.content)
    events = []

    for item in root.findall(".//item"):

        def get(tag):
            el = item.find(tag)
            return el.text.strip() if el is not None and el.text else ""

        country = get("country")
        impact  = get("impact")

        # Only keep events for pairs we trade
        if country not in WATCHED_CURRENCIES:
            continue

        # Only keep High and Medium impact
        if impact not in ("High", "Medium"):
            continue

        # Parse datetime — FF RSS uses US Eastern time
        date_str = get("date")   # e.g. "01-06-2025"
        time_str = get("time")   # e.g. "8:30am" or "" for all-day

        dt_utc = _parse_ff_datetime(date_str, time_str)

        events.append({
            "title":        get("title"),
            "country":      country,
            "date":         date_str,
            "datetime_utc": dt_utc,
            "impact":       impact,
            "tier":         TIER_MAP.get(impact, 3),
            "forecast":     get("forecast"),
            "previous":     get("previous"),
        })

    events.sort(key=lambda e: e["datetime_utc"] or datetime.min.replace(tzinfo=timezone.utc))
    print(f"[Calendar] Fetched {len(events)} High/Medium impact events for watched currencies.")
    return events


def _parse_ff_datetime(date_str: str, time_str: str) -> datetime | None:
    """
    Converts ForexFactory date + time strings to UTC datetime.
    date_str: "01-06-2025"
    time_str: "8:30am" | "12:00pm" | "" (all-day)
    """
    try:
        eastern = ZoneInfo("America/New_York")

        if time_str:
            # Normalise: "8:30am" → "8:30 AM"
            time_clean = time_str.upper().replace("AM", " AM").replace("PM", " PM").strip()
            dt_eastern = datetime.strptime(f"{date_str} {time_clean}", "%m-%d-%Y %I:%M %p")
        else:
            # All-day event — treat as midnight ET
            dt_eastern = datetime.strptime(date_str, "%m-%d-%Y")

        dt_eastern = dt_eastern.replace(tzinfo=eastern)
        return dt_eastern.astimezone(timezone.utc)

    except Exception as e:
        print(f"[Calendar] Date parse error ({date_str} {time_str}): {e}")
        return datetime.min.replace(tzinfo=timezone.utc)


def get_todays_events(events: list[dict]) -> list[dict]:
    """Filter events list to today only (UTC date)."""
    today = datetime.now(timezone.utc).date()
    return [e for e in events if e["datetime_utc"].date() == today]


def get_tier1_events(events: list[dict]) -> list[dict]:
    return [e for e in events if e["tier"] == 1]


def print_events(events: list[dict], label: str = "Events"):
    print(f"\n{'='*60}")
    print(f"  {label} ({len(events)} total)")
    print(f"{'='*60}")
    for e in events:
        t = e["datetime_utc"].strftime("%a %d %b  %H:%M UTC") if e["datetime_utc"] else "All Day"
        tier_label = f"[T{e['tier']}]"
        forecast   = f"  Forecast: {e['forecast']}" if e["forecast"] else ""
        previous   = f"  Prev: {e['previous']}"    if e["previous"] else ""
        print(f"  {tier_label} {e['country']}  {t}  —  {e['title']}{forecast}{previous}")
    print()