import os
from dotenv import load_dotenv

load_dotenv()

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
    "AUD_USD",
    "USD_CAD",
]

PRIMARY_PAIR = "EUR_USD"

# ── Risk Parameters ────────────────────────────────────────────
RISK_PER_TRADE      = 0.01   # 1% of account per trade
MAX_DAILY_LOSS      = 0.04   # 4% daily loss kill-switch
MAX_OPEN_POSITIONS  = 2
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
    "london_open":  {"start": 7,  "end": 9},
    "london":       {"start": 8,  "end": 16},
    "new_york":     {"start": 13, "end": 22},
    "overlap":      {"start": 13, "end": 17},  # highest liquidity
}

# Bot only trades inside these UTC hour ranges
ALLOWED_TRADE_HOURS_UTC = list(range(7, 10)) + list(range(13, 22))

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
RSI_PERIOD = 14
RSI_LOW    = 40
RSI_HIGH   = 60

# London Breakout
BREAKOUT_BUFFER_PIPS   = 5    # pips above/below Asian range
BREAKOUT_ASIAN_START   = 22   # UTC hour Asian session starts (prev day)
BREAKOUT_ASIAN_END     = 7    # UTC hour Asian session ends

# News fade
FADE_MIN_SPIKE_PIPS    = 40   # minimum spike size to consider fade
FADE_FIBO_LEVEL        = 0.618

# ── Logging ────────────────────────────────────────────────────
LOG_DIR        = "logs"
TRADE_LOG_FILE = "logs/trades.csv"