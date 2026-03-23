"""
╔══════════════════════════════════════════════════════════════════════╗
║          HEDGE ENGINE — Hybrid Both-Sides Position Manager          ║
║                                                                      ║
║  Strategy (reverse-engineered from @Hcrystallash + early exit):     ║
║  1. Buy BOTH sides at market open: 60% conviction, 40% hedge       ║
║  2. Scale into conviction side as direction confirms (3 add-ons)    ║
║  3. Exit early if conviction side hits 70%+ unrealized profit       ║
║  4. Otherwise hold to expiry (binary resolution)                    ║
║  5. Hedge covers downside — wrong calls lose ~$0.50 not ~$2.75     ║
╚══════════════════════════════════════════════════════════════════════╝
"""

import time
import logging
from dataclasses import dataclass, field
from typing import Optional

from config import (
    HEDGE_ENABLED,
    CONVICTION_BUDGET, HEDGE_BUDGET, TOTAL_MARKET_BUDGET,
    HEDGE_RATIO, CONVICTION_RATIO,
    SCALE_IN_ENABLED, SCALE_IN_COUNT, SCALE_IN_INTERVAL_SEC, SCALE_IN_THRESHOLD,
    DIRECTION_THRESHOLD, MAIN_BET_DELAY_SEC,
    MAX_TOTAL_USDC, MAX_ONE_SIDE_USDC,
    ENTRY_DEADLINE_SEC, PROFIT_EXIT_ENABLED, PROFIT_EXIT_PCT,
    KELLY_FRACTION, MOMENTUM_SCALE, MAX_MOMENTUM_EDGE,
    MIN_BET_PCT, MAX_BET_PCT, MIN_BET_USDC,
    DEFAULT_BANKROLL_USDC,
    FAST_LIMIT_PRICE,
    LOG_DIR,
    # Legacy
    MAIN_BET_SIZE_USDC,
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
        ARMED   — market started, waiting for direction signal
        OPEN    — both sides bought, watching for scale-in
        SCALING — adding to conviction side as direction confirms
        HOLD    — fully positioned, holding to expiry or profit exit
        CLOSED  — position resolved or exited
    """
    asset: str
    market: dict
    open_time: float = field(default_factory=time.time)

    up:   Optional[Side] = None
    down: Optional[Side] = None

    phase: str = "WAIT"
    lean: Optional[str] = None       # "UP" or "DOWN" — conviction direction

    # Hedge tracking
    conviction_side_dir: Optional[str] = None  # "UP" or "DOWN"
    hedge_side_dir: Optional[str] = None       # "UP" or "DOWN"
    conviction_spent: float = 0.0
    hedge_spent: float = 0.0

    # Scale-in tracking
    scale_in_count: int = 0
    scale_in_last_time: float = 0.0

    # Hedge state
    is_hedged: bool = False

    # Exit tracking
    early_exit: bool = False
    exit_type: Optional[str] = None  # "EARLY_EXIT" or "RESOLUTION"
    exit_pnl: float = 0.0

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
    Manages hybrid both-sides positions across BTC and ETH markets.

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
        mode = "HEDGE" if HEDGE_ENABLED else "LEGACY"
        logger.info(
            f"\n{'='*60}\n"
            f"  [{asset.upper()}] NEW MARKET ({mode}): {market['question']}\n"
            f"{'='*60}"
        )

    def update(self, asset: str, price_feed, remaining: float):
        """
        Main decision loop for one asset.

        Called every tick (~50ms). Manages phase transitions and order placement.
        """
        pos = self.positions.get(asset)
        if pos is None or pos.is_closed:
            return

        feed = price_feed.get(asset)
        move_pct = feed.move_pct()
        market = pos.market
        neg_risk = market.get("neg_risk", False)

        # ── Phase: WAIT -> ARMED ─────────────────────────────────
        if pos.phase == "WAIT":
            if remaining is None or remaining > 300:
                return
            pos.phase = "ARMED"
            logger.info(f"  [{asset.upper()}] ARMED — waiting for direction signal")

        # ── Phase: ARMED -> OPEN (conditional hedge entry) ────────
        elif pos.phase == "ARMED":
            if remaining is None or remaining < ENTRY_DEADLINE_SEC:
                pos.phase = "HOLD"
                return

            if pos.elapsed < MAIN_BET_DELAY_SEC:
                return

            if abs(move_pct) < DIRECTION_THRESHOLD:
                return  # No signal yet

            main_dir = "UP" if move_pct > 0 else "DOWN"
            hedge_dir = "DOWN" if main_dir == "UP" else "UP"
            pos.lean = main_dir
            pos.conviction_side_dir = main_dir
            pos.hedge_side_dir = hedge_dir

            conviction_obj = pos.side_for(main_dir)
            hedge_obj = pos.side_for(hedge_dir)

            if conviction_obj is None or hedge_obj is None:
                return

            # ── Always place conviction bet (fast path) ───────────
            conv_budget = CONVICTION_BUDGET
            ask = FAST_LIMIT_PRICE
            conv_ok = False
            fill = self.executor.buy(
                token_id=conviction_obj.token_id,
                size_usdc=conv_budget,
                price=ask,
                neg_risk=neg_risk,
            )
            if fill:
                conviction_obj.add_fill(fill["filled_usdc"], fill["price"])
                pos.conviction_spent += fill["filled_usdc"]
                conv_ok = True

            # ── Always hedge: buy opposite side at real market price ─
            hedge_ok = False
            hedge_price_used = 0.0
            if HEDGE_ENABLED and conv_ok:
                # Fetch REAL market price for hedge side (should be cheaper)
                prices = self.scanner.get_token_prices(market)
                h_prices = prices.get(hedge_dir, {})
                hedge_ask = h_prices.get("best_ask", 0)

                # Only hedge if price is reasonable (<$0.60)
                if 0 < hedge_ask < 0.60:
                    h_budget = HEDGE_BUDGET
                    fill_h = self.executor.buy(
                        token_id=hedge_obj.token_id,
                        size_usdc=h_budget,
                        price=hedge_ask,
                        neg_risk=neg_risk,
                    )
                    if fill_h:
                        hedge_obj.add_fill(fill_h["filled_usdc"], fill_h["price"])
                        pos.hedge_spent += fill_h["filled_usdc"]
                        hedge_ok = True
                        hedge_price_used = fill_h["price"]
                elif hedge_ask <= 0:
                    # Orderbook fetch failed — use fallback price
                    fallback_price = max(1.0 - ask - 0.03, 0.35)
                    h_budget = HEDGE_BUDGET
                    fill_h = self.executor.buy(
                        token_id=hedge_obj.token_id,
                        size_usdc=h_budget,
                        price=fallback_price,
                        neg_risk=neg_risk,
                    )
                    if fill_h:
                        hedge_obj.add_fill(fill_h["filled_usdc"], fill_h["price"])
                        pos.hedge_spent += fill_h["filled_usdc"]
                        hedge_ok = True
                        hedge_price_used = fill_h["price"]
                    logger.info(
                        f"  [{asset.upper()}] Orderbook unavailable, "
                        f"hedge at fallback ${fallback_price:.2f}"
                    )

            pos.is_hedged = hedge_ok

            # Transition
            if SCALE_IN_ENABLED and conv_ok:
                pos.scale_in_last_time = time.time()
                pos.phase = "SCALING"
            else:
                pos.phase = "HOLD"

            logger.info(
                f"  [{asset.upper()}] OPEN: {main_dir} "
                f"${conv_budget:.2f}@${ask:.2f} | "
                f"hedge={hedge_dir} ${HEDGE_BUDGET:.2f}@${hedge_price_used:.2f} "
                f"{'OK' if hedge_ok else 'SKIP'} | "
                f"move={move_pct:+.4f}%"
            )

        # ── Phase: SCALING (add to conviction side) ──────────────
        elif pos.phase == "SCALING":
            if remaining is None or remaining < ENTRY_DEADLINE_SEC:
                pos.phase = "HOLD"
                return

            # Check for early profit exit during scaling
            if PROFIT_EXIT_ENABLED and self._check_profit_exit(pos, neg_risk):
                return

            # Scale-in: add to conviction side if direction continues
            if (pos.scale_in_count < SCALE_IN_COUNT
                    and pos.lean is not None
                    and time.time() - pos.scale_in_last_time >= SCALE_IN_INTERVAL_SEC):

                current_dir = "UP" if move_pct > 0 else "DOWN"
                if abs(move_pct) >= SCALE_IN_THRESHOLD and current_dir == pos.lean:
                    conviction_obj = pos.side_for(pos.lean)
                    if conviction_obj is None:
                        pos.phase = "HOLD"
                        return

                    # Fetch actual ask for scale-in (not racing anymore)
                    prices = self.scanner.get_token_prices(market)
                    p = prices.get(pos.lean, {})
                    ask = p.get("best_ask", 0)

                    if 0 < ask < 0.90:
                        remaining_budget = MAX_TOTAL_USDC - pos.total_spent
                        remaining_scales = SCALE_IN_COUNT - pos.scale_in_count
                        scale_size = min(
                            remaining_budget / max(remaining_scales, 1),
                            MAX_ONE_SIDE_USDC - conviction_obj.spent,
                        )
                        scale_size = max(scale_size, MIN_BET_USDC)

                        if scale_size >= MIN_BET_USDC and remaining_budget >= MIN_BET_USDC:
                            fill = self.executor.buy(
                                token_id=conviction_obj.token_id,
                                size_usdc=scale_size,
                                price=ask,
                                neg_risk=neg_risk,
                            )
                            if fill:
                                conviction_obj.add_fill(fill["filled_usdc"], fill["price"])
                                pos.conviction_spent += fill["filled_usdc"]
                                pos.scale_in_count += 1
                                pos.scale_in_last_time = time.time()
                                logger.info(
                                    f"  [{asset.upper()}] SCALE #{pos.scale_in_count}: "
                                    f"{pos.lean} ${fill['filled_usdc']:.2f} @ ${fill['price']:.2f} "
                                    f"(move={move_pct:+.3f}%)"
                                )

            # Transition to HOLD when done
            if pos.scale_in_count >= SCALE_IN_COUNT:
                pos.phase = "HOLD"
                logger.info(f"  [{asset.upper()}] Scale-ins complete -> HOLD")
            elif pos.elapsed > SCALE_IN_COUNT * SCALE_IN_INTERVAL_SEC + 60:
                pos.phase = "HOLD"

        # ── Phase: HOLD ──────────────────────────────────────────
        elif pos.phase == "HOLD":
            if PROFIT_EXIT_ENABLED:
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

        # If already exited early, return cached result
        if pos.early_exit and pos.exit_type == "EARLY_EXIT":
            pos.phase = "CLOSED"
            return self._build_result(pos, asset_direction)

        pos.exit_type = "RESOLUTION"

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
            "exit_type":  "RESOLUTION",
            "hedge_cost": pos.hedge_spent,
            "conviction_spent": pos.conviction_spent,
        }

        emoji = "+" if net_pnl > 0 else "-"
        logger.info(
            f"\n  {emoji} [{asset.upper()}] RESOLVED: went {asset_direction}\n"
            f"  Lean: {pos.lean or 'none'} "
            f"({'correct' if result['correct_lean'] else 'wrong'})\n"
            f"  Conviction: ${pos.conviction_spent:.2f} | "
            f"Hedge: ${pos.hedge_spent:.2f}\n"
            f"  Payout: ${payout:.2f} | Net PnL: ${net_pnl:+.2f}\n"
        )

        pos.phase = "CLOSED"
        return result

    # ─────────────────────────────────────────────────────────
    #  Helpers
    # ─────────────────────────────────────────────────────────

    def _build_result(self, pos: MarketPosition, asset_direction: str) -> dict:
        """Build result dict from an early-exited position."""
        return {
            "asset":      pos.asset,
            "direction":  asset_direction,
            "lean":       pos.lean,
            "up_spent":   pos.up.spent if pos.up else 0,
            "down_spent": pos.down.spent if pos.down else 0,
            "total_cost": pos.total_spent,
            "payout":     pos.exit_pnl + pos.total_spent,
            "net_pnl":    pos.exit_pnl,
            "correct_lean": pos.lean == asset_direction,
            "exit_type":  "EARLY_EXIT",
            "hedge_cost": pos.hedge_spent,
            "conviction_spent": pos.conviction_spent,
        }

    def _check_profit_exit(self, pos: MarketPosition, neg_risk: bool) -> bool:
        """
        Check conviction side for profit exit opportunity.

        ONLY for unhedged positions. Hedged positions hold to expiry
        for the full $1.00/token payout (the hedge caps downside).
        Returns True if an exit was triggered.
        """
        # Hedged positions hold to expiry — hedge protects the downside,
        # so we want the full $1.00 resolution, not a $0.94 early exit.
        if pos.is_hedged:
            return False

        # Target the conviction side for exit (unhedged only)
        if pos.lean is not None:
            conviction_obj = pos.side_for(pos.lean)
            if conviction_obj is not None and conviction_obj.tokens > 0:
                result = self._try_exit_side(pos, conviction_obj, neg_risk)
                if result:
                    return True

        # Also check the hedge side (it can profit if direction reversed)
        if pos.hedge_side_dir is not None:
            hedge_obj = pos.side_for(pos.hedge_side_dir)
            if hedge_obj is not None and hedge_obj.tokens > 0:
                result = self._try_exit_side(pos, hedge_obj, neg_risk)
                if result:
                    return True

        return False

    def _try_exit_side(self, pos: MarketPosition, side_obj: Side, neg_risk: bool) -> bool:
        """Attempt to sell a side if it has sufficient unrealized profit."""
        book = self.executor.get_orderbook(side_obj.token_id)
        if not book:
            return False

        current_bid = book["best_bid"]
        if current_bid <= 0:
            return False

        unreal_pct = side_obj.unrealized_pct(current_bid)

        if unreal_pct >= PROFIT_EXIT_PCT:
            sell_value = side_obj.tokens * current_bid
            profit = sell_value - pos.total_spent  # Net of ALL costs including hedge

            logger.info(
                f"\n  [{pos.asset.upper()}] PROFIT EXIT: "
                f"{side_obj.outcome} {unreal_pct:+.1%}\n"
                f"  Selling {side_obj.tokens:.4f} tokens @ ${current_bid:.2f} "
                f"-> ${sell_value:.2f} (conviction=${pos.conviction_spent:.2f}, "
                f"hedge=${pos.hedge_spent:.2f})"
            )
            fill = self.executor.sell(
                token_id=side_obj.token_id,
                qty_tokens=side_obj.tokens,
                price=current_bid,
                neg_risk=neg_risk,
            )
            if fill:
                pos.early_exit = True
                pos.exit_type = "EARLY_EXIT"
                pos.exit_pnl = profit
                pos.phase = "CLOSED"
                return True

        return False

    def _kelly_size(self, move_pct: float, ask: float, label: str = "") -> float:
        """
        Kelly-optimal bet size based on momentum signal and market price.
        """
        if ask <= 0 or ask >= 1:
            return 0.0

        momentum_edge = min(abs(move_pct) * MOMENTUM_SCALE, MAX_MOMENTUM_EDGE)
        p_est = 0.50 + momentum_edge

        edge = p_est - ask
        if edge <= 0:
            return 0.0

        kelly_pct = edge / (1.0 - ask)
        bet_pct   = kelly_pct * KELLY_FRACTION

        bet_pct = min(bet_pct, MAX_BET_PCT)
        bet_pct = max(bet_pct, MIN_BET_PCT)

        size = self.bankroll * bet_pct
        size = max(size, MIN_BET_USDC)

        logger.info(
            f"  [{label}] Kelly: move={move_pct:+.3f}% p_est={p_est:.3f} "
            f"ask={ask:.2f} edge={edge:.3f} "
            f"kelly%={kelly_pct:.3%} -> bet=${size:.2f}"
        )
        return round(size, 2)

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
            f"Conv: {pos.conviction_side_dir or '-'} ${pos.conviction_spent:.2f} | "
            f"Hedge: {pos.hedge_side_dir or '-'} ${pos.hedge_spent:.2f}"
        )
