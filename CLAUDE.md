# Polymarket 5-Min Hedge Bot — Context

## What This Bot Does
Trades BTC and ETH 5-minute Up/Down binary markets on Polymarket. Uses Binance WebSocket for real-time price momentum signals. Buys both sides (conviction + hedge) and holds to expiry for binary $1.00 resolution.

## Architecture
- `main.py` — Main loop, market discovery, resolution, CSV logging
- `hedge_engine.py` — Position management, phase transitions (WAIT→ARMED→OPEN→SCALING→HOLD→CLOSED)
- `executor.py` — Order placement via py_clob_client, GTC orders with cancel-on-resolve
- `market_scanner.py` — Finds 5-min markets via Gamma API, slug prediction
- `price_feed.py` — Binance aggTrade WebSocket for BTC+ETH real-time prices
- `config.py` — All tunable parameters
- `backtest.py` — Replays on-chain history through strategy variants

## Infrastructure
- **EC2 Region:** eu-west-1 (Dublin, Ireland) — chosen because Polymarket's matching engine is in eu-west-2 (London). Dublin is the closest non-geoblocked region (~5-10ms). UK and US IPs are geoblocked by Polymarket.
- **Bot runs in tmux** session named `bot`
- **.env file** on EC2 has all API keys (never committed to git)

## Changes Made This Session (March 24, 2026)

### Fixes Applied
1. **Disabled early exit** (`PROFIT_EXIT_ENABLED = False`) — On-chain data proved early exit cost $6.76 by selling winners at ~$0.90 instead of letting them resolve at $1.00
2. **GTC orders with cancel-on-resolve** — Tried FAK (Fill-And-Kill) but Polymarket's API has strict 2-decimal maker amount requirement that breaks most price/size combos. Reverted to GTC. Added `cancel_open_orders()` that cancels all tracked orders when markets resolve or bot shuts down, preventing stale resting orders.
3. **Fill verification** — `executor.buy()` returns `{"order_id", "filled_usdc", "filled_tokens", "price"}` dict instead of just order_id string. Extracts actual fill amounts from API response when available.
4. **Daily PnL reset at midnight UTC** — Prevents the $30 daily loss limit from permanently killing the bot across days. Also resets consecutive_losses and cooldown.
5. **MAX_DAILY_LOSS_USDC = $30** (was $20)
6. **Hedge price cap raised** from $0.55 to $0.60 — stops skipping hedges on balanced markets
7. **FLAT outcome eliminated** — `move >= 0` → UP, `move < 0` → DOWN. Polymarket always resolves binary.
8. **Market scanner cache eviction** — removes entries older than 2 windows (10 min)

### Known Issues Still Open
1. ~~**Price feed staleness**~~ — **FIXED**: Added `last_update` timestamp + `is_stale` property to AssetFeed. Main loop skips new entries and scale-ins when price >5s stale. Existing positions still resolve normally.
2. ~~**Daily loss limit kills bot permanently**~~ — **FIXED**: Now pauses until midnight UTC instead of killing the process. Logs once, then keeps the main loop alive so daily reset fires at midnight.
3. **Kelly sizing is dead code** — `_kelly_size()` in hedge_engine.py is implemented but never called. Bot uses fixed CONVICTION_BUDGET=$4.85 and HEDGE_BUDGET=$1.29.
4. **Scale-ins may overspend** — On-chain data shows some markets spending $10+ total (over the $8.50 cap), possibly from scale-in prices being much higher than initial entry.

## On-Chain Data Analysis
- **Wallet:** 0x3fab06a7278f62281a41e73a3edd4fbb061c90d0
- **Profile:** https://polymarket.com/@Joker777
- **Data API:** `https://data-api.polymarket.com/activity?user=0x3fab06a7278f62281a41e73a3edd4fbb061c90d0&limit=100`
- **Old bot (pre-changes):** Made +$39.21 with 78% ROI over ~52 markets. Early exit bug accidentally helped by forcing hold-to-expiry.
- **New bot (post-changes):** First 8 markets showed -$18.08, 25% win rate. BUT it's unclear if the EC2 was running the latest code — sells were happening despite early exit being disabled.

## Key Config Values
```
CONVICTION_BUDGET = $4.85
HEDGE_BUDGET = $1.29
FAST_LIMIT_PRICE = $0.60
DIRECTION_THRESHOLD = 0.01%
SCALE_IN_COUNT = 3
MAX_TOTAL_USDC = $8.50
ENTRY_DEADLINE_SEC = 60
MAX_DAILY_LOSS_USDC = $30
```

## Git Remote
```
origin https://github.com/Reshu-Bhatt3429/WorkingBot.git
```
**WARNING:** The remote URL had a GitHub PAT embedded. It needs to be revoked and replaced.

## Next Steps
1. Pull latest code on EC2 and verify with `git log --oneline -1`
2. Monitor live performance with the corrected code
3. Investigate why scale-ins overspend the $8.50 cap
