"""
╔══════════════════════════════════════════════════════════════════════╗
║          POLYMARKET HEDGE BOT — CONFIGURATION                      ║
║  Both-sides strategy on BTC + ETH 5-min markets                   ║
╚══════════════════════════════════════════════════════════════════════╝
"""

import os
from dotenv import load_dotenv
load_dotenv()

# ═══════════════════════════════════════════════════════════════
#  POLYMARKET API
# ═══════════════════════════════════════════════════════════════

POLYMARKET_PRIVATE_KEY      = os.environ.get("POLYMARKET_PRIVATE_KEY", "").strip()
POLYMARKET_API_KEY          = os.environ.get("POLYMARKET_API_KEY", "").strip()
POLYMARKET_API_SECRET       = os.environ.get("POLYMARKET_API_SECRET", "").strip()
POLYMARKET_API_PASSPHRASE   = os.environ.get("POLYMARKET_API_PASSPHRASE", "").strip()
POLYMARKET_FUNDER_ADDRESS   = os.environ.get("POLYMARKET_FUNDER_ADDRESS", "").strip()
POLYMARKET_SIGNATURE_TYPE   = int(os.environ.get("POLYMARKET_SIGNATURE_TYPE", "0"))

CHAIN_ID  = int(os.environ.get("CHAIN_ID", "137"))
CLOB_HOST = "https://clob.polymarket.com"
GAMMA_HOST = "https://gamma-api.polymarket.com"

LIVE_TRADING = os.environ.get("LIVE_TRADING", "false").lower() in ("true", "1", "yes")

# ═══════════════════════════════════════════════════════════════
#  MARKETS
# ═══════════════════════════════════════════════════════════════

WINDOW_SEC = 300            # 5-minute markets
ASSETS = ["btc", "eth"]     # Trade both simultaneously

# Slug patterns: btc-updown-5m-{ts}, eth-updown-5m-{ts}
SLUG_PATTERN = "{asset}-updown-5m-{ts}"

# ═══════════════════════════════════════════════════════════════
#  PRICE FEEDS (Binance)
# ═══════════════════════════════════════════════════════════════

BINANCE_WS_BTC = "wss://stream.binance.com:9443/ws/btcusdt@aggTrade"
BINANCE_WS_ETH = "wss://stream.binance.com:9443/ws/ethusdt@aggTrade"

# ═══════════════════════════════════════════════════════════════
#  HEDGE STRATEGY PARAMETERS
# ═══════════════════════════════════════════════════════════════

# Phase 1 — Hedge (open immediately on market start)
HEDGE_SIZE_USDC     = 1.50   # Buy the cheap side at open (ensures ≥5 tokens at typical prices)
CHEAP_SIDE_MAX      = 0.42   # Buy hedge when token price is below this

# Phase 2 — Main bet (after direction is confirmed)
MAIN_BET_SIZE_USDC  = 2.00   # Primary directional position
DIRECTION_THRESHOLD = 0.01   # Any real tick triggers — we're racing market makers
MAIN_BET_DELAY_SEC  = 0      # Instant — race the market makers

# Phase 3 — Add-on (optional averaging in)
ADDON_SIZE_USDC     = 0.50   # Add-on to winning side
ADDON_DELAY_SEC     = 120    # Add-on available after 120s
ADDON_THRESHOLD     = 0.20   # Need >0.20% move for add-on

# Position limits per market
MAX_TOTAL_USDC      = 4.00   # Max total spend per asset per market ($4 BTC + $4 ETH = $8/cycle)
MAX_ONE_SIDE_USDC   = 3.00   # Never put more than $3 on one direction

# Entry deadline — don't open new positions in last N seconds
ENTRY_DEADLINE_SEC  = 60

# ═══════════════════════════════════════════════════════════════
#  EARLY EXIT
# ═══════════════════════════════════════════════════════════════

PROFIT_EXIT_PCT       = 0.70   # Sell when unrealized profit >= 70%
PROFIT_CHECK_INTERVAL = 10     # Check every 10 seconds

# ═══════════════════════════════════════════════════════════════
#  RISK CONTROLS
# ═══════════════════════════════════════════════════════════════

MAX_DAILY_LOSS_USDC     = 20.0   # Stop trading for the day after $20 loss
MAX_CONSECUTIVE_LOSSES  = 6      # Pause after 6 straight losing markets
LOSS_COOLDOWN_SEC       = 1800   # 30-minute cooldown after loss streak

# ═══════════════════════════════════════════════════════════════
#  KELLY CRITERION SIZING
# ═══════════════════════════════════════════════════════════════

# Kelly fraction: 1.0 = full Kelly, 0.5 = half Kelly (recommended)
KELLY_FRACTION      = 0.5

# How much edge (probability boost) per 1% BTC/ETH price move
# e.g. 0.10% move → +0.01 edge above 50/50 → 51% win probability
MOMENTUM_SCALE      = 0.10

# Maximum estimated edge — caps at 65% win probability (15% above 50%)
MAX_MOMENTUM_EDGE   = 0.15

# Bet size floor/ceiling as % of bankroll
MIN_BET_PCT         = 0.005  # 0.5% minimum (prevents dust trades)
MAX_BET_PCT         = 0.05   # 5% maximum per position (never over-bet)

# Hard USDC floor — never trade below this regardless of Kelly
MIN_BET_USDC        = 0.50

# How often to re-fetch balance from Polymarket
BALANCE_REFRESH_SEC = 300    # every 5 minutes

# Fallback bankroll if balance can't be fetched
DEFAULT_BANKROLL_USDC = 50.0

# ═══════════════════════════════════════════════════════════════
#  EXECUTION
# ═══════════════════════════════════════════════════════════════

# Fast-path: skip order book, use fixed limit price to shave ~300ms
FAST_LIMIT_PRICE    = 0.55   # Submit at $0.55 — fills if ask ≤ $0.55, else sits

TICK_SIZE           = "0.01"
ORDER_TIMEOUT_SEC   = 20
FILL_TIMEOUT_SEC    = 30
API_RATE_LIMIT      = 10    # requests/sec

# ═══════════════════════════════════════════════════════════════
#  LOOP TIMING
# ═══════════════════════════════════════════════════════════════

MAIN_LOOP_INTERVAL  = 0.05  # 50ms — millisecond-level responsiveness
DISPLAY_INTERVAL    = 30.0  # seconds between status prints

# ═══════════════════════════════════════════════════════════════
#  LOGGING
# ═══════════════════════════════════════════════════════════════

LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
os.makedirs(LOG_DIR, exist_ok=True)
