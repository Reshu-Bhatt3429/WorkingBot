"""
╔══════════════════════════════════════════════════════════════════════╗
║       POLYMARKET HEDGE BOT — MAIN LOOP                            ║
║  Hybrid both-sides strategy on BTC + ETH 5-min markets            ║
║                                                                    ║
║  Strategy (hybrid of @Hcrystallash + early exit):                ║
║    1. Every 5-min market: buy BOTH sides (60/40 conviction/hedge)║
║    2. Scale into conviction side as direction confirms            ║
║    3. Exit early if 70%+ profit, otherwise hold to resolution    ║
║    4. Hedge covers downside on wrong calls                        ║
║    5. Run BTC and ETH simultaneously                              ║
╚══════════════════════════════════════════════════════════════════════╝
"""

import time
import signal
import sys
import csv
import os
import logging
from datetime import datetime, timedelta, timezone

from config import (
    LIVE_TRADING, MAIN_LOOP_INTERVAL, DISPLAY_INTERVAL,
    ASSETS, WINDOW_SEC, ENTRY_DEADLINE_SEC,
    MAX_DAILY_LOSS_USDC, MAX_CONSECUTIVE_LOSSES, LOSS_COOLDOWN_SEC,
    BALANCE_REFRESH_SEC,
    LOG_DIR,
    HEDGE_ENABLED,
)
from price_feed import PriceFeed
from market_scanner import MarketScanner
from executor import Executor
from hedge_engine import HedgeEngine

# ═══════════════════════════════════════════════════════════════
#  LOGGING
# ═══════════════════════════════════════════════════════════════

logger = logging.getLogger("main")
logger.setLevel(logging.INFO)
if not logger.handlers:
    ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    fh = logging.FileHandler(f"{LOG_DIR}/bot_{ts}.log")
    fh.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s"))
    logger.addHandler(fh)
    sh = logging.StreamHandler()
    sh.setFormatter(logging.Formatter("%(asctime)s | %(message)s", datefmt="%H:%M:%S"))
    logger.addHandler(sh)


class HedgeBot:
    def __init__(self):
        self.feed    = PriceFeed()
        self.scanner = MarketScanner()
        self.executor = Executor()
        self.engine  = HedgeEngine(self.executor, self.scanner)

        # Session stats
        self.daily_pnl         = 0.0
        self.session_pnl       = 0.0
        self.total_markets     = 0
        self.profitable_markets = 0
        self.consecutive_losses = 0
        self._cooldown_until   = 0.0

        # Display
        self._last_display  = 0.0
        self._last_window   = 0    # Last 5-min window we processed
        self._last_balance  = 0.0  # Last time we refreshed balance
        self._last_day      = datetime.now(timezone.utc).date()
        self._running       = True

        # Trade log — start fresh with new columns
        self._csv_path = os.path.join(LOG_DIR, "trades_v2.csv")
        self._init_csv()

    def run(self):
        signal.signal(signal.SIGINT,  self._handle_stop)
        signal.signal(signal.SIGTERM, self._handle_stop)

        mode = "LIVE" if LIVE_TRADING else "DRY RUN"
        hedge = "HEDGE" if HEDGE_ENABLED else "LEGACY"
        logger.info(f"\n{'='*60}")
        logger.info(f"  POLYMARKET HEDGE BOT | {mode} | {hedge}")
        logger.info(f"  Assets: {', '.join(a.upper() for a in ASSETS)}")
        logger.info(f"{'='*60}\n")

        if not self.executor.setup():
            logger.error("Executor setup failed")
            return

        self.feed.start()
        self.feed.wait_ready(timeout=15)

        # Fetch initial balance and wire it into the engine
        self._refresh_balance()

        logger.info("Bot running\n")

        while self._running:
            try:
                self._tick()
                time.sleep(MAIN_LOOP_INTERVAL)
            except KeyboardInterrupt:
                break
            except Exception as e:
                logger.error(f"Loop error: {e}", exc_info=True)
                time.sleep(5)

        self._shutdown()

    def _tick(self):
        now = time.time()
        current_window = (int(now) // WINDOW_SEC) * WINDOW_SEC

        # ── Daily PnL reset at midnight UTC ────────────────────
        today = datetime.now(timezone.utc).date()
        if today != self._last_day:
            logger.info(
                f"New day ({today}) — resetting daily PnL from ${self.daily_pnl:+.2f}"
            )
            self.daily_pnl = 0.0
            self.consecutive_losses = 0
            self._cooldown_until = 0.0
            self._loss_limit_logged = False
            self._last_day = today

        # ── Balance refresh ───────────────────────────────────
        if now - self._last_balance >= BALANCE_REFRESH_SEC:
            self._refresh_balance()

        # ── Risk check ────────────────────────────────────────
        if now < self._cooldown_until:
            remaining_cd = int(self._cooldown_until - now)
            if int(now) % 30 == 0:
                logger.info(f"Cooldown active — {remaining_cd}s remaining")
            self._maybe_display()
            return

        if self.daily_pnl <= -MAX_DAILY_LOSS_USDC:
            midnight = datetime.now(timezone.utc).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            midnight_tomorrow = midnight + timedelta(days=1)
            pause_secs = (midnight_tomorrow - datetime.now(timezone.utc)).total_seconds()
            if not getattr(self, '_loss_limit_logged', False):
                logger.warning(
                    f"Daily loss limit hit (${self.daily_pnl:+.2f}) — "
                    f"pausing until midnight UTC ({int(pause_secs)}s)"
                )
                self._loss_limit_logged = True
            self._maybe_display()
            return

        # ── Price staleness check ────────────────────────────
        stale_assets = [a for a in ASSETS if self.feed.get(a).is_stale]
        if stale_assets:
            if int(now) % 30 == 0:
                logger.warning(
                    f"Stale price feed: {', '.join(a.upper() for a in stale_assets)} "
                    f"— skipping new entries"
                )

        # ── New window: discover markets ──────────────────────
        if current_window != self._last_window:
            self._last_window = current_window
            self._on_new_window(current_window, stale_assets)

        # ── Per-asset update ──────────────────────────────────
        for asset in ASSETS:
            if not self.engine.has_position(asset):
                continue

            pos = self.engine.positions[asset]

            # If position was closed by early exit, log it
            if pos.is_closed and pos.early_exit and not getattr(pos, '_exit_logged', False):
                self._handle_early_exit(asset)
                pos._exit_logged = True
                continue

            if pos.is_closed:
                continue

            remaining = self.scanner.seconds_remaining(pos.market)

            # Market expired — resolve
            if remaining is not None and remaining <= 0:
                self._resolve_market(asset)
                continue

            # Skip scale-ins/updates if price feed is stale
            if self.feed.get(asset).is_stale:
                continue

            # Update hedge engine (may place orders or trigger early exit)
            self.engine.update(asset, self.feed, remaining)

            # Check if update() just triggered an early exit
            if pos.is_closed and pos.early_exit and not getattr(pos, '_exit_logged', False):
                self._handle_early_exit(asset)
                pos._exit_logged = True

        # ── Display ───────────────────────────────────────────
        self._maybe_display()

    def _on_new_window(self, window_ts: int, stale_assets: list[str] = None):
        """Called at the start of each new 5-minute window."""
        logger.info(f"\nNew window: {datetime.utcfromtimestamp(window_ts).strftime('%H:%M UTC')}")
        if stale_assets is None:
            stale_assets = []

        for asset in ASSETS:
            # Resolve any lingering position from last window
            if self.engine.has_position(asset):
                old_pos = self.engine.positions[asset]
                if not old_pos.is_closed:
                    self._resolve_market(asset)

            # Skip new entry if price feed is stale
            if asset in stale_assets:
                logger.warning(f"  [{asset.upper()}] Price feed stale — skipping entry")
                continue

            # Discover new market
            market = self.scanner.get_market(asset, window_ts)
            if market is None:
                logger.info(f"  [{asset.upper()}] No market found for this window")
                continue

            remaining = self.scanner.seconds_remaining(market)
            if remaining is not None and remaining < ENTRY_DEADLINE_SEC:
                logger.info(
                    f"  [{asset.upper()}] Market too close to expiry "
                    f"({remaining:.0f}s) — skipping"
                )
                continue

            # Set open price for this asset
            feed = self.feed.get(asset)
            feed.set_open()

            # Register position
            self.engine.open_position(asset, market)

    def _resolve_market(self, asset: str):
        """Close and record the result of a completed market."""
        feed = self.feed.get(asset)

        # Determine outcome from price vs open
        # Polymarket always resolves UP or DOWN, never FLAT.
        # If move is exactly 0 (e.g. WS disconnect), default to UP
        # to avoid recording a false total loss.
        move = feed.move_pct()
        if move >= 0:
            outcome = "UP"
        else:
            outcome = "DOWN"

        # Cancel any resting GTC orders before resolving
        self.executor.cancel_open_orders()

        result = self.engine.resolve(asset, outcome)
        if result is None:
            return

        # Ensure fields exist
        result.setdefault("exit_type", "RESOLUTION")
        result.setdefault("hedge_cost", 0.0)
        result.setdefault("conviction_spent", 0.0)

        self._record_result(result)

    def _handle_early_exit(self, asset: str):
        """Record early exit result to CSV and session stats."""
        pos = self.engine.positions[asset]
        feed = self.feed.get(asset)
        move = feed.move_pct()
        outcome = "UP" if move > 0 else ("DOWN" if move < 0 else "FLAT")

        result = {
            "asset": asset,
            "direction": outcome,
            "lean": pos.lean,
            "up_spent": pos.up.spent if pos.up else 0,
            "down_spent": pos.down.spent if pos.down else 0,
            "total_cost": pos.total_spent,
            "payout": pos.exit_pnl + pos.total_spent,
            "net_pnl": pos.exit_pnl,
            "correct_lean": pos.lean == outcome,
            "exit_type": "EARLY_EXIT",
            "hedge_cost": pos.hedge_spent,
            "conviction_spent": pos.conviction_spent,
        }

        self._record_result(result)
        logger.info(
            f"  [{asset.upper()}] Early exit logged: PnL ${result['net_pnl']:+.2f} "
            f"(hedge=${result['hedge_cost']:.2f})"
        )

    def _record_result(self, result: dict):
        """Update session stats and write CSV row."""
        pnl = result["net_pnl"]
        self.daily_pnl   += pnl
        self.session_pnl += pnl
        self.total_markets += 1

        if pnl > 0:
            self.profitable_markets += 1
            self.consecutive_losses = 0
        else:
            self.consecutive_losses += 1
            if self.consecutive_losses >= MAX_CONSECUTIVE_LOSSES:
                self._cooldown_until = time.time() + LOSS_COOLDOWN_SEC
                logger.warning(
                    f"{self.consecutive_losses} consecutive losing markets — "
                    f"cooling down for {LOSS_COOLDOWN_SEC//60}m"
                )

        self._log_csv(result)

    def _maybe_display(self):
        now = time.time()
        if now - self._last_display < DISPLAY_INTERVAL:
            return
        self._last_display = now

        mkt_wr = (self.profitable_markets / self.total_markets * 100
                  if self.total_markets > 0 else 0)

        from config import KELLY_FRACTION, HEDGE_ENABLED
        mode = "HEDGE" if HEDGE_ENABLED else "LEGACY"
        lines = [
            f"\n{'-'*60}",
            f"  BTC ${self.feed.btc.price:>10,.2f}  "
            f"move {self.feed.btc.move_pct():+.3f}%  "
            f"{self.feed.btc.direction()}",
            f"  ETH ${self.feed.eth.price:>10,.2f}  "
            f"move {self.feed.eth.move_pct():+.3f}%  "
            f"{self.feed.eth.direction()}",
            f"  Mode: {mode} | Bankroll: ${self.engine.bankroll:.2f} | "
            f"Markets: {self.profitable_markets}W / "
            f"{self.total_markets - self.profitable_markets}L "
            f"({mkt_wr:.0f}%)",
            f"  Daily PnL: ${self.daily_pnl:+.2f}  "
            f"Session: ${self.session_pnl:+.2f}",
        ]
        for asset in ASSETS:
            lines.append(f"  {self.engine.summary(asset)}")
        lines.append(f"{'-'*60}")
        logger.info("\n".join(lines))

    def _refresh_balance(self):
        """Fetch live balance and update Kelly engine bankroll."""
        balance = self.executor.get_balance()
        if balance > 0:
            self.engine.bankroll = balance
            logger.info(f"Bankroll: ${balance:.2f} USDC")
        self._last_balance = time.time()

    def _shutdown(self):
        logger.info("\nShutting down...")
        self.executor.cancel_open_orders()
        self.feed.stop()
        mkt_wr = (self.profitable_markets / self.total_markets * 100
                  if self.total_markets > 0 else 0)
        logger.info(
            f"\n{'='*60}\n"
            f"  Session complete\n"
            f"  Markets: {self.profitable_markets}W / "
            f"{self.total_markets - self.profitable_markets}L "
            f"({mkt_wr:.0f}%)\n"
            f"  Session PnL: ${self.session_pnl:+.2f}\n"
            f"{'='*60}\n"
        )

    def _handle_stop(self, *_):
        logger.info("\nStop signal received")
        self._running = False

    def _init_csv(self):
        if not os.path.exists(self._csv_path):
            with open(self._csv_path, "w", newline="") as f:
                writer = csv.writer(f)
                writer.writerow([
                    "timestamp", "asset", "direction", "lean",
                    "up_spent", "down_spent", "total_cost",
                    "payout", "net_pnl", "correct_lean",
                    "daily_pnl", "session_pnl",
                    "exit_type", "hedge_cost", "conviction_spent",
                ])

    def _log_csv(self, result: dict):
        try:
            with open(self._csv_path, "a", newline="") as f:
                writer = csv.writer(f)
                writer.writerow([
                    datetime.utcnow().isoformat(),
                    result["asset"],
                    result["direction"],
                    result["lean"],
                    f"{result['up_spent']:.4f}",
                    f"{result['down_spent']:.4f}",
                    f"{result['total_cost']:.4f}",
                    f"{result['payout']:.4f}",
                    f"{result['net_pnl']:.4f}",
                    result["correct_lean"],
                    f"{self.daily_pnl:.4f}",
                    f"{self.session_pnl:.4f}",
                    result.get("exit_type", "RESOLUTION"),
                    f"{result.get('hedge_cost', 0):.4f}",
                    f"{result.get('conviction_spent', 0):.4f}",
                ])
        except Exception as e:
            logger.warning(f"CSV log failed: {e}")


if __name__ == "__main__":
    bot = HedgeBot()
    bot.run()
