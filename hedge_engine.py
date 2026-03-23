"""
╔══════════════════════════════════════════════════════════════════════╗
║          HEDGE ENGINE — Both-Sides Position Manager                ║
║                                                                    ║
║  Reverse-engineered from @Hcrystallash's trading pattern:         ║
║  1. Open a small hedge on the CHEAP side immediately              ║
║  2. After 60s, load up on the direction BTC/ETH is actually moving║
║  3. Average in with small additional buys as conviction grows     ║
║  4. Exit early if position hits 70%+ unrealized profit            ║
║  5. Otherwise hold to expiry (binary resolution)                  ║
╚══════════════════════════════════════════════════════════════════════╝
"""

import time
import logging
from dataclasses import dataclass, field
from typing import Optional

from config import (
    HEDGE_SIZE_USDC, CHEAP_SIDE_MAX,
    MAIN_BET_SIZE_USDC, DIRECTION_THRESHOLD, MAIN_BET_DELAY_SEC,
    ADDON_SIZE_USDC, ADDON_DELAY_SEC, ADDON_THRESHOLD,
    MAX_TOTAL_USDC, MAX_ONE_SIDE_USDC,
    ENTRY_DEADLINE_SEC, PROFIT_EXIT_PCT,
    KELLY_FRACTION, MOMENTUM_SCALE, MAX_MOMENTUM_EDGE,
    MIN_BET_PCT, MAX_BET_PCT, MIN_BET_USDC,
    DEFAULT_BANKROLL_USDC,
    FAST_LIMIT_PRICE,
    LOG_DIR,
)

logger = logging.getLogger("hedge")
logger.setLevel(logging.INFO)
if not logger.handlers:
    fh = logging.FileHandler(f"{LOG_DIR}/hedge.log")
    fh.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s"))
    logger.addHandler(fh)
    sh = logging.StreamHandler()
    sh.setFormatter(logging.Formatter("%(asctime)s | %(message)s"))
    logger.addHandler(sh)


@dataclass
class Side:
    """Tracks a single-direction (UP or DOWN) token position."""
    token_id: str
    outcome: str        # "UP" or "DOWN"
    tokens: float = 0.0
    spent: float = 0.0

    @property
    def avg_price(self) -> float:
        return self.spent / self.tokens if self.tokens > 0 else 0.0

    def add_fill(self, usdc: float, price: float):
        qty = usdc / price
        self.tokens += qty
        self.spent += usdc

    def unrealized_pct(self, current_bid: float) -> float:
        if self.avg_price <= 0:
            return 0.0
        return (current_bid - self.avg_price) / self.avg_price

    def current_value(self, current_bid: float) -> float:
        return self.tokens * current_bid


@dataclass
class MarketPosition:
    """
    Tracks the full both-sides position for one market (one asset).

    Phases:
        WAIT    — market found, waiting for open
        HEDGE   — small hedge placed on cheap side, watching direction
        MAIN    — main directional bet placed, may add-on
        HOLD    — fully positioned, holding to expiry or profit exit
        CLOSED  — position resolved or exited
    """
    asset: str
    market: dict
    open_time: float = field(default_factory=time.time)

    up:   Optional[Side] = None
    down: Optional[Side] = None

    phase: str = "WAIT"          # WAIT → HEDGE → MAIN → HOLD → CLOSED
    lean: Optional[str] = None   # "UP" or "DOWN" — which direction we favor
    addon_done: bool = False
    early_exit: bool = False

    def __post_init__(self):
        tokens = self.market.get("tokens", [])
        for t in tokens:
            o = t["outcome"].upper()
            if o in ("UP", "YES", "HIGHER"):
                self.up = Side(token_id=t["token_id"], outcome="UP")
            elif o in ("DOWN", "NO", "LOWER"):
                self.down = Side(token_id=t["token_id"], outcome="DOWN")

    @property
    def elapsed(self) -> float:
        return time.time() - self.open_time

    @property
    def total_spent(self) -> float:
        return (self.up.spent if self.up else 0) + (self.down.spent if self.down else 0)

    @property
    def is_closed(self) -> bool:
        return self.phase == "CLOSED"

    def side_for(self, direction: str) -> Optional[Side]:
        if direction == "UP":
            return self.up
        if direction == "DOWN":
            return self.down
        return None

    def opposite_side(self, direction: str) -> Optional[Side]:
        if direction == "UP":
            return self.down
        if direction == "DOWN":
            return self.up
        return None


class HedgeEngine:
    """
    Manages both-sides positions across BTC and ETH markets.

    Call update() on each loop iteration — it decides what to buy/sell.
    """

    def __init__(self, executor, scanner):
        self.executor = executor
        self.scanner = scanner
        self.positions: dict[str, MarketPosition] = {}  # asset -> position
        self.bankroll: float = DEFAULT_BANKROLL_USDC     # updated from live balance

    def has_position(self, asset: str) -> bool:
        p = self.positions.get(asset)
        return p is not None and not p.is_closed

    def open_position(self, asset: str, market: dict):
        """Register a new market for an asset."""
        pos = MarketPosition(asset=asset, market=market)
        self.positions[asset] = pos
        logger.info(
            f"\n{'═'*60}\n"
            f"  📊 [{asset.upper()}] NEW MARKET: {market['question']}\n"
            f"{'═'*60}"
        )

    def update(self, asset: str, price_feed, remaining: float):
        """
        Main decision loop for one asset.

        Called every second. Decides whether to:
        - Place initial hedge
        - Place main directional bet
        - Add to winning side
        - Exit early for profit
        """
        pos = self.positions.get(asset)
        if pos is None or pos.is_closed:
            return

        feed = price_feed.get(asset)
        move_pct = feed.move_pct()
        direction = feed.direction()
        market = pos.market
        neg_risk = market.get("neg_risk", False)

        # ── Phase: WAIT → HEDGE (skip straight to watching) ───
        if pos.phase == "WAIT":
            if remaining is None or remaining > 300:
                return  # Market hasn't started yet
            # Skip hedge order book fetch — go straight to watching
            # for directional signal to fire the fast bet
            pos.phase = "HEDGE"
            logger.info(f"  ⚡ [{asset.upper()}] ARMED — waiting for first tick")

        # ── Phase: HEDGE → MAIN (FAST PATH) ────────────────────
        elif pos.phase == "HEDGE":
            if remaining is None or remaining < ENTRY_DEADLINE_SEC:
                pos.phase = "HOLD"
                return

            if pos.elapsed < MAIN_BET_DELAY_SEC:
                return

            if abs(move_pct) < DIRECTION_THRESHOLD:
                return  # No tick yet

            # FAST: direction detected → submit immediately at fixed limit
            # Skip order book fetch (~300ms) — use FAST_LIMIT_PRICE instead
            main_dir = "UP" if move_pct > 0 else "DOWN"
            pos.lean = main_dir

            main_side_obj = pos.side_for(main_dir)
            if main_side_obj is None:
                return

            ask = FAST_LIMIT_PRICE
            kelly_bet = self._kelly_size(move_pct, ask, label=f"{asset.upper()} MAIN")
            budget = min(
                kelly_bet if kelly_bet > 0 else MAIN_BET_SIZE_USDC,
                MAX_ONE_SIDE_USDC - main_side_obj.spent,
                MAX_TOTAL_USDC - pos.total_spent,
            )
            if budget < MIN_BET_USDC:
                return

            order_id = self.executor.buy(
                token_id=main_side_obj.token_id,
                size_usdc=budget,
                price=ask,
                neg_risk=neg_risk,
            )
            if order_id:
                main_side_obj.add_fill(budget, ask)
                pos.phase = "MAIN"
                logger.info(
                    f"  ⚡ [{asset.upper()}] FAST BET: {main_dir} "
                    f"${budget:.2f} @ ${ask:.2f} "
                    f"(move={move_pct:+.4f}%, {pos.elapsed:.1f}s in)"
                )

        # ── Phase: MAIN → ADD-ON ──────────────────────────────
        elif pos.phase == "MAIN":
            if remaining is None or remaining < ENTRY_DEADLINE_SEC:
                pos.phase = "HOLD"
                return

            # Check for early profit exit first
            if self._check_profit_exit(pos, neg_risk):
                return

            # Add-on: double down if direction is very clear
            if (not pos.addon_done
                    and pos.elapsed >= ADDON_DELAY_SEC
                    and abs(move_pct) >= ADDON_THRESHOLD
                    and pos.lean is not None):

                main_side_obj = pos.side_for(pos.lean)
                if main_side_obj is None:
                    pos.phase = "HOLD"
                    return

                prices_addon = self.scanner.get_token_prices(market)
                p_addon = prices_addon.get(pos.lean, {})
                ask_addon = p_addon.get("best_ask", 0)
                kelly_addon = self._kelly_size(move_pct, ask_addon, label=f"{asset.upper()} ADDON") if ask_addon > 0 else ADDON_SIZE_USDC
                budget = min(
                    kelly_addon,
                    MAX_ONE_SIDE_USDC - main_side_obj.spent,
                    MAX_TOTAL_USDC - pos.total_spent,
                )
                if budget >= MIN_BET_USDC:
                    # Reuse the already-fetched prices_addon
                    ask = ask_addon

                    if 0 < ask < 0.90:
                        order_id = self.executor.buy(
                            token_id=main_side_obj.token_id,
                            size_usdc=budget,
                            price=ask,
                            neg_risk=neg_risk,
                        )
                        if order_id:
                            main_side_obj.add_fill(budget, ask)
                            pos.addon_done = True
                            logger.info(
                                f"  ➕ [{asset.upper()}] ADD-ON: {pos.lean} "
                                f"${budget:.2f} @ ${ask:.2f} "
                                f"(move={move_pct:+.3f}%)"
                            )

            # Transition to HOLD after add-on window
            if pos.elapsed >= ADDON_DELAY_SEC + 30:
                pos.phase = "HOLD"

        # ── Phase: HOLD ───────────────────────────────────────
        elif pos.phase == "HOLD":
            self._check_profit_exit(pos, neg_risk)

    def resolve(self, asset: str, asset_direction: str):
        """
        Resolve position when market ends.

        Args:
            asset:           "btc" or "eth"
            asset_direction: "UP" or "DOWN" — how asset actually moved
        """
        pos = self.positions.get(asset)
        if pos is None or pos.is_closed:
            return None

        up_s   = pos.up.spent   if pos.up   else 0
        down_s = pos.down.spent if pos.down else 0

        if asset_direction == "UP":
            winning_tokens = pos.up.tokens if pos.up else 0
            payout = winning_tokens * 1.0
        elif asset_direction == "DOWN":
            winning_tokens = pos.down.tokens if pos.down else 0
            payout = winning_tokens * 1.0
        else:
            payout = 0.0

        total_cost = pos.total_spent
        net_pnl = payout - total_cost

        result = {
            "asset":      asset,
            "direction":  asset_direction,
            "lean":       pos.lean,
            "up_spent":   up_s,
            "down_spent": down_s,
            "total_cost": total_cost,
            "payout":     payout,
            "net_pnl":    net_pnl,
            "correct_lean": pos.lean == asset_direction,
        }

        emoji = "✅" if net_pnl > 0 else "❌"
        logger.info(
            f"\n  {emoji} [{asset.upper()}] RESOLVED: BTC/ETH went {asset_direction}\n"
            f"  Lean was: {pos.lean or 'none'} "
            f"({'correct' if result['correct_lean'] else 'wrong'})\n"
            f"  Up spent: ${up_s:.2f} | Down spent: ${down_s:.2f}\n"
            f"  Payout: ${payout:.2f} | Net PnL: ${net_pnl:+.2f}\n"
        )

        pos.phase = "CLOSED"
        return result

    # ─────────────────────────────────────────────────────────
    #  Helpers
    # ─────────────────────────────────────────────────────────

    def _kelly_size(self, move_pct: float, ask: float, label: str = "") -> float:
        """
        Kelly-optimal bet size based on momentum signal and market price.

        Formula:
            p_est  = 0.50 + min(|move_pct| * MOMENTUM_SCALE, MAX_MOMENTUM_EDGE)
            edge   = p_est - ask
            kelly% = edge / (1 - ask)          [binary outcome Kelly]
            bet    = bankroll * kelly% * KELLY_FRACTION

        Clamped to [MIN_BET_USDC, bankroll * MAX_BET_PCT].
        Returns 0 if no positive edge.
        """
        if ask <= 0 or ask >= 1:
            return 0.0

        # Estimated win probability from momentum signal
        momentum_edge = min(abs(move_pct) * MOMENTUM_SCALE, MAX_MOMENTUM_EDGE)
        p_est = 0.50 + momentum_edge

        # Edge over market price
        edge = p_est - ask
        if edge <= 0:
            logger.info(
                f"  📉 [{label}] No edge: p_est={p_est:.3f} ask={ask:.2f} → skip"
            )
            return 0.0

        # Kelly fraction of bankroll
        kelly_pct = edge / (1.0 - ask)
        bet_pct   = kelly_pct * KELLY_FRACTION

        # Clamp to configured bounds
        bet_pct = min(bet_pct, MAX_BET_PCT)
        bet_pct = max(bet_pct, MIN_BET_PCT)

        size = self.bankroll * bet_pct
        size = max(size, MIN_BET_USDC)

        logger.info(
            f"  📐 [{label}] Kelly: move={move_pct:+.3f}% p_est={p_est:.3f} "
            f"ask={ask:.2f} edge={edge:.3f} "
            f"kelly%={kelly_pct:.3%} half={kelly_pct*KELLY_FRACTION:.3%} "
            f"bankroll=${self.bankroll:.2f} → bet=${size:.2f}"
        )
        return round(size, 2)

    def _find_cheap_side(self, prices: dict):
        """Return (direction, ask_price) for the cheaper side, or (None, None)."""
        best_dir, best_price = None, 1.0
        for direction, p in prices.items():
            ask = p.get("best_ask", 1.0)
            if ask < best_price:
                best_price = ask
                best_dir = direction
        return best_dir, best_price if best_dir else None

    def _check_profit_exit(self, pos: MarketPosition, neg_risk: bool) -> bool:
        """
        If either side has 70%+ unrealized profit, sell it back.
        Returns True if an exit order was placed.
        """
        for side_obj in [pos.up, pos.down]:
            if side_obj is None or side_obj.tokens <= 0:
                continue

            book = self.executor.get_orderbook(side_obj.token_id)
            if not book:
                continue

            current_bid = book["best_bid"]
            if current_bid <= 0:
                continue

            unreal_pct = side_obj.unrealized_pct(current_bid)

            if unreal_pct >= PROFIT_EXIT_PCT:
                sell_value = side_obj.tokens * current_bid
                logger.info(
                    f"\n  💰 [{pos.asset.upper()}] PROFIT EXIT: "
                    f"{side_obj.outcome} {unreal_pct:+.1%}\n"
                    f"  Selling {side_obj.tokens:.4f} tokens @ ${current_bid:.2f} "
                    f"→ ${sell_value:.2f} (entry avg ${side_obj.avg_price:.2f})"
                )
                order_id = self.executor.sell(
                    token_id=side_obj.token_id,
                    qty_tokens=side_obj.tokens,
                    price=current_bid,
                    neg_risk=neg_risk,
                )
                if order_id:
                    pos.early_exit = True
                    pos.phase = "CLOSED"
                    return True

        return False

    def summary(self, asset: str) -> str:
        pos = self.positions.get(asset)
        if pos is None:
            return f"[{asset.upper()}] No position"
        up_s   = pos.up.spent   if pos.up   else 0
        down_s = pos.down.spent if pos.down else 0
        return (
            f"[{asset.upper()}] {pos.phase} | "
            f"UP ${up_s:.2f} | DOWN ${down_s:.2f} | "
            f"Total ${pos.total_spent:.2f} | "
            f"Lean: {pos.lean or '—'}"
        )
