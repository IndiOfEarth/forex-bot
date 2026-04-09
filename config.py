import os
from datetime import datetime
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

load_dotenv()

# ── DST-Aware Session UTC Anchors ──────────────────────────────
# Computed at startup so the bot always uses the correct UTC hours
# regardless of whether UK/US is in summer or winter time.
def _utc_offset_hours(tz_name: str) -> int:
    tz = ZoneInfo(tz_name)
    return int(datetime.now(tz).utcoffset().total_seconds() / 3600)

_london_offset = _utc_offset_hours("Europe/London")    # +1 BST, 0 GMT
_ny_offset      = _utc_offset_hours("America/New_York") # -4 EDT, -5 EST

# London physically opens at 08:00 local time = (8 - london_offset) UTC
LONDON_OPEN_UTC  = 8  - _london_offset   # 07:00 UTC in BST, 08:00 UTC in GMT
LONDON_CLOSE_UTC = 16 - _london_offset   # 15:00 UTC in BST, 16:00 UTC in GMT

# NY physically opens at 09:00 local time; EDT = UTC-4, EST = UTC-5
NY_OPEN_UTC  = 13 + (-4 - _ny_offset)   # 13:00 UTC in EDT, 14:00 UTC in EST
NY_CLOSE_UTC = 22 + (-4 - _ny_offset)   # 22:00 UTC in EDT, 23:00 UTC in EST

# ── OANDA Credentials ──────────────────────────────────────────
OANDA_API_KEY     = os.getenv("OANDA_API_KEY")
OANDA_ACCOUNT_ID  = os.getenv("OANDA_ACCOUNT_ID")
OANDA_ENVIRONMENT = os.getenv("OANDA_ENVIRONMENT", "practice")  # "practice" or "live"

if not OANDA_API_KEY or not OANDA_ACCOUNT_ID:
    raise EnvironmentError("Missing OANDA_API_KEY or OANDA_ACCOUNT_ID in .env file")

# ── Trading Pairs ──────────────────────────────────────────────
PAIRS = [
    "EUR_USD",
    "GBP_USD",
    "USD_JPY",
]

PRIMARY_PAIR = "EUR_USD"

# ── Risk Parameters ────────────────────────────────────────────
RISK_PER_TRADE      = 0.03   # 3% of account per trade
MAX_DAILY_LOSS      = 0.06   # 6% daily loss kill-switch (~2 full losses at 3%)
MAX_OPEN_POSITIONS  = 3
MIN_REWARD_RISK     = 2.0    # minimum 1:2 RR

# ── Exit Management ────────────────────────────────────────────
TRAIL_TRIGGER_R   = 1.0   # Move SL to break-even after 1R profit
TRAIL_LOCK_R      = 0.5   # Lock in 0.5R on remainder after partial close
PARTIAL_CLOSE_R   = 1.5   # Close PARTIAL_CLOSE_PCT of position at 1.5R
PARTIAL_CLOSE_PCT = 0.5   # Fraction of position to close at partial close level
FULL_TP_R         = 3.5   # TP for remaining position after partial close

# ── Entry Quality ──────────────────────────────────────────────
BREAKOUT_ASIAN_MIN_PIPS = 20    # Minimum Asian range (raised from 10)
MOMENTUM_BODY_RATIO     = 0.6   # Breakout bar body must be >= 60% of bar range

# ── Session Windows (UTC hours) ────────────────────────────────
SESSIONS = {
    "london_open": {"start": LONDON_OPEN_UTC,      "end": LONDON_OPEN_UTC + 2},
    "london":      {"start": LONDON_OPEN_UTC,      "end": LONDON_CLOSE_UTC},
    "new_york":    {"start": NY_OPEN_UTC,           "end": NY_CLOSE_UTC},
    "overlap":     {"start": NY_OPEN_UTC,           "end": NY_OPEN_UTC + 4},
}

# Bot only trades inside these UTC hour ranges
ALLOWED_TRADE_HOURS_UTC = (
    list(range(LONDON_OPEN_UTC, LONDON_OPEN_UTC + 3)) +
    list(range(NY_OPEN_UTC, NY_CLOSE_UTC))
)

# ── News Blackout Windows (minutes) ───────────────────────────
BLACKOUT_BEFORE_TIER1 = 30   # minutes before Tier 1 event
BLACKOUT_AFTER_TIER1  = 30   # minutes after  Tier 1 event
BLACKOUT_BEFORE_TIER2 = 15
BLACKOUT_AFTER_TIER2  = 15

# ── Candle Granularity ─────────────────────────────────────────
DEFAULT_GRANULARITY = "H1"   # H1 = 1-hour candles
BREAKOUT_GRANULARITY = "M15" # 15-min for London breakout

# ── Strategy Parameters ────────────────────────────────────────
EMA_SHORT  = 21
EMA_MID    = 50
EMA_LONG   = 200

# ── Macro Regime Detection ─────────────────────────────────────
REQUIRE_DAILY_TREND_DEFAULT = False   # opt-in per pair via PAIR_CONFIG

# ATR Volatility Gate
ATR_VOLATILITY_MULTIPLIER = 2.0    # current ATR > 2× its 20-bar SMA = high-vol regime
ATR_HIGH_VOL_SIZE_SCALAR  = 0.5    # multiply position size by this in high-vol regime
ATR_BLOCK_ON_HIGH_VOL     = False  # True = skip trade entirely; False = halve size
ATR_STOP_MULTIPLIER       = 1.5    # stop = entry ± ATR_STOP_MULTIPLIER × ATR(14) on H1

# Consecutive Loss Kill-Switch
CONSECUTIVE_LOSS_LIMIT = 3         # pause rest-of-day after N consecutive closed losses

# Equity Peak Drawdown Guard
MAX_PEAK_DRAWDOWN = 0.08           # 8% below session peak NAV triggers kill-switch
RSI_PERIOD = 14
RSI_LOW    = 40
RSI_HIGH   = 60

# London Breakout
BREAKOUT_BUFFER_PIPS   = 5    # pips above/below Asian range
BREAKOUT_ASIAN_START   = 22   # UTC hour Asian session starts (prev day) — Tokyo/no DST
BREAKOUT_ASIAN_END     = LONDON_OPEN_UTC   # Asian range ends when London opens

# News fade
FADE_MIN_SPIKE_PIPS    = 40    # minimum spike size to consider fade
FADE_FIBO_LEVEL        = 0.618
FADE_STOP_BUFFER_PIPS  = 5     # stop buffer beyond spike extreme
FADE_TP_RETRACEMENT    = 0.0   # 0.0 = full reversion to pre-spike price; 0.382 = partial

# ── Logging ────────────────────────────────────────────────────
LOG_DIR        = "logs"
TRADE_LOG_FILE = "logs/trades.csv"