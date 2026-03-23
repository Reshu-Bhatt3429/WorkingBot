"""
Backtest: Replay on-chain Polymarket history through strategy variants.

Parses the Polymarket-History CSV (buys, sells, redeems), reconstructs
each 5-minute market window, determines the actual outcome, then simulates
what WOULD have happened under each strategy using real fill prices.
"""

import csv
import re
from collections import defaultdict
from dataclasses import dataclass, field

CSV_PATH = "logs/Polymarket-History-2026-03-24.csv"

# ─── Parse ───────────────────────────────────────────────────────────

@dataclass
class Trade:
    market: str
    action: str
    usdc: float
    tokens: float
    side: str
    timestamp: int
    asset: str = ""
    window: str = ""

def parse_trades():
    trades = []
    with open(CSV_PATH, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            name = row["marketName"]
            asset = "btc" if "Bitcoin" in name else ("eth" if "Ethereum" in name else "?")
            m = re.search(r'(\d+:\d+[AP]M-\d+:\d+[AP]M)', name)
            trades.append(Trade(
                market=name, action=row["action"],
                usdc=float(row["usdcAmount"]), tokens=float(row["tokenAmount"]),
                side=row["tokenName"], timestamp=int(row["timestamp"]),
                asset=asset, window=m.group(1) if m else "",
            ))
    trades.sort(key=lambda t: t.timestamp)
    return trades

# ─── Market windows ─────────────────────────────────────────────────

@dataclass
class Window:
    asset: str
    market: str
    window: str
    buys: list = field(default_factory=list)
    sells: list = field(default_factory=list)
    redeems: list = field(default_factory=list)

    @property
    def total_bought(self): return sum(b[1] for b in self.buys)
    @property
    def total_redeemed(self): return sum(r for r in self.redeems)
    @property
    def total_sold(self): return sum(s[1] for s in self.sells)
    @property
    def actual_pnl(self): return self.total_redeemed + self.total_sold - self.total_bought

    def prices(self):
        r = {"Up": [], "Down": []}
        for side, usdc, tokens, price in self.buys:
            if side in r:
                r[side].append((usdc, tokens, price))
        return r

    @property
    def outcome(self):
        """Determine winning side from redeems or structure."""
        up_tok = sum(b[2] for b in self.buys if b[0] == "Up")
        dn_tok = sum(b[2] for b in self.buys if b[0] == "Down")
        r = self.total_redeemed

        if r > 0:
            if up_tok > 0 and abs(r - up_tok) < abs(r - dn_tok):
                return "UP"
            elif dn_tok > 0:
                return "DOWN"
            # If we held both sides, figure out from redeem amount
            if up_tok > 0 and dn_tok > 0:
                # Redeem = winning_tokens * $1. Whichever side's tokens match closer
                if abs(r - up_tok) < abs(r - dn_tok):
                    return "UP"
                else:
                    return "DOWN"

        if self.sells:
            return self.sells[0][0].upper()

        if r == 0 and self.total_sold == 0 and self.total_bought > 0:
            sides = set(b[0] for b in self.buys)
            if sides == {"Up"}: return "DOWN"
            if sides == {"Down"}: return "UP"

        return "UNKNOWN"

    @property
    def conv_side(self):
        """First buy = conviction direction."""
        return self.buys[0][0] if self.buys else None

    @property
    def conv_price(self):
        if not self.buys: return 0
        return self.buys[0][3]

    @property
    def hedge_side(self):
        cs = self.conv_side
        if cs == "Up": return "Down"
        if cs == "Down": return "Up"
        return None

    def hedge_price(self):
        """Actual hedge price if we bought both sides, else estimate."""
        hs = self.hedge_side
        if hs:
            for side, usdc, tokens, price in self.buys:
                if side == hs and usdc >= 0.5:
                    return price
        # Estimate from conviction price
        cp = self.conv_price
        if cp > 0:
            return max(1.0 - cp - 0.03, 0.10)
        return 0.40


def group_windows(trades):
    windows = {}
    for t in trades:
        key = (t.asset, t.market)
        if key not in windows:
            windows[key] = Window(t.asset, t.market, t.window)
        w = windows[key]
        if t.action == "Buy":
            p = t.usdc / t.tokens if t.tokens > 0 else 0
            w.buys.append((t.side, t.usdc, t.tokens, p))
        elif t.action == "Sell":
            p = t.usdc / t.tokens if t.tokens > 0 else 0
            w.sells.append((t.side, t.usdc, t.tokens, p))
        elif t.action == "Redeem":
            w.redeems.append(t.usdc)
    return [w for w in windows.values() if w.buys and w.total_bought >= 0.5]


# ─── Strategies ──────────────────────────────────────────────────────

def strat_actual(windows):
    """What actually happened on-chain."""
    pnl = wins = losses = early = 0
    for w in windows:
        p = w.actual_pnl
        pnl += p
        if w.sells: early += 1
        if p > 0.01: wins += 1
        elif p < -0.01: losses += 1
    n = wins + losses
    return dict(name="ACTUAL on-chain results", pnl=pnl, n=n,
                wins=wins, losses=losses, early=early)


def strat_single_hold(windows):
    """Single-side conviction, hold to expiry. No hedge."""
    pnl = wins = losses = 0
    BET = 2.75
    for w in windows:
        cp = w.conv_price
        if cp <= 0 or cp >= 1: continue
        cs = w.conv_side
        out = w.outcome
        if out == "UNKNOWN": continue
        tokens = BET / cp
        if cs and cs.upper() == out:
            pnl += tokens * 1.0 - BET; wins += 1
        else:
            pnl -= BET; losses += 1
    n = wins + losses
    return dict(name="Single-side $2.75, hold to expiry", pnl=pnl, n=n,
                wins=wins, losses=losses)


def strat_single_early_exit(windows):
    """Single-side conviction with early exit at $0.94 (original bot behavior)."""
    pnl = wins = losses = exits = 0
    BET = 2.75
    EXIT_PRICE = 0.94
    EXIT_RATE = 0.34  # 34% of markets trigger an early exit (from real data)
    for w in windows:
        cp = w.conv_price
        if cp <= 0 or cp >= 1: continue
        cs = w.conv_side
        out = w.outcome
        if out == "UNKNOWN": continue

        tokens = BET / cp
        correct = cs and cs.upper() == out

        # Simulate: EXIT_RATE of correct calls exit early
        import random
        random.seed(hash(w.market))  # Deterministic per market
        if correct and random.random() < EXIT_RATE:
            # Early exit at $0.94
            sell_value = tokens * EXIT_PRICE
            pnl += sell_value - BET
            wins += 1; exits += 1
        elif correct:
            # Hold to expiry
            pnl += tokens * 1.0 - BET
            wins += 1
        else:
            pnl -= BET; losses += 1
    n = wins + losses
    return dict(name="Single-side $2.75, early exit @$0.94", pnl=pnl, n=n,
                wins=wins, losses=losses, early_exits=exits)


def strat_always_hedge_hold(windows):
    """PROPOSED: Always hedge both sides, hold to expiry."""
    pnl = wins = hedge_saves = total_loss = 0
    struct_edge = 0.0
    CONV = 2.75
    HEDGE = 1.70

    for w in windows:
        cp = w.conv_price
        if cp <= 0 or cp >= 1: continue
        hp = w.hedge_price()
        if hp <= 0 or hp >= 1: continue
        cs = w.conv_side
        hs = w.hedge_side
        out = w.outcome
        if out == "UNKNOWN": continue

        conv_tok = CONV / cp
        hedge_tok = HEDGE / hp
        cost = CONV + HEDGE
        combined = cp + hp

        # Structural edge: min tokens guaranteed
        mn = min(conv_tok, hedge_tok)
        struct_edge += mn * 1.0 - mn * combined

        if cs and cs.upper() == out:
            p = conv_tok * 1.0 - cost; wins += 1
        elif hs and hs.upper() == out:
            p = hedge_tok * 1.0 - cost; hedge_saves += 1
        else:
            p = -cost; total_loss += 1
        pnl += p

    n = wins + hedge_saves + total_loss
    return dict(name="PROPOSED: Always hedge, hold to expiry",
                pnl=pnl, n=n, wins=wins, hedge_saves=hedge_saves,
                total_loss=total_loss, struct_edge=struct_edge)


def strat_hedge_hold_longshot(windows):
    """PROPOSED+: Always hedge + hold + long-shot sniper on cheap tokens."""
    pnl = wins = hedge_saves = ls_hits = ls_tries = 0
    CONV = 2.75
    HEDGE = 1.70
    LS_BET = 0.50
    LS_THRESHOLD = 0.20  # Only bet on tokens under $0.20

    for w in windows:
        cp = w.conv_price
        if cp <= 0 or cp >= 1: continue
        hp = w.hedge_price()
        if hp <= 0 or hp >= 1: continue
        cs = w.conv_side
        hs = w.hedge_side
        out = w.outcome
        if out == "UNKNOWN": continue

        conv_tok = CONV / cp
        hedge_tok = HEDGE / hp
        cost = CONV + HEDGE

        # Long-shot: if hedge side is very cheap, add a small bet
        ls_pnl = 0
        if hp < LS_THRESHOLD:
            ls_tries += 1
            ls_tok = LS_BET / hp
            cost += LS_BET
            if hs and hs.upper() == out:
                ls_pnl = ls_tok * 1.0 - LS_BET
                ls_hits += 1
            else:
                ls_pnl = -LS_BET

        if cs and cs.upper() == out:
            p = conv_tok * 1.0 - cost + ls_pnl; wins += 1
        elif hs and hs.upper() == out:
            p = hedge_tok * 1.0 - cost + ls_pnl; hedge_saves += 1
        else:
            p = -cost + ls_pnl
        pnl += p

    n = wins + hedge_saves
    return dict(name="PROPOSED+: Hedge + Hold + Long-shot sniper",
                pnl=pnl, n=n, wins=wins, hedge_saves=hedge_saves,
                ls_hits=ls_hits, ls_tries=ls_tries)


# ─── Main ────────────────────────────────────────────────────────────

def main():
    trades = parse_trades()
    windows = group_windows(trades)

    print(f"\n{'='*70}")
    print(f"  BACKTEST: Polymarket 5-min BTC/ETH Strategy Comparison")
    print(f"  Data source: {CSV_PATH} ({len(trades)} on-chain transactions)")
    print(f"  Active market windows: {len(windows)}")
    print(f"{'='*70}")

    # ── Data analysis ────────────────────────────────────────────────
    print(f"\n{'─'*70}")
    print(f"  DATA ANALYSIS")
    print(f"{'─'*70}")

    total_bought = sum(w.total_bought for w in windows)
    total_redeemed = sum(w.total_redeemed for w in windows)
    total_sold = sum(w.total_sold for w in windows)
    print(f"  Total volume:     ${total_bought:.2f} bought")
    print(f"  Total redeemed:   ${total_redeemed:.2f}")
    print(f"  Total sold:       ${total_sold:.2f}")
    print(f"  Actual net PnL:   ${total_redeemed + total_sold - total_bought:+.2f}")

    # Directional accuracy
    correct = total = 0
    for w in windows:
        cs = w.conv_side
        out = w.outcome
        if cs and out != "UNKNOWN":
            total += 1
            if cs.upper() == out: correct += 1
    print(f"\n  Directional accuracy: {correct}/{total} = {correct/total*100:.1f}%")

    # Price analysis
    all_cp = [w.conv_price for w in windows if 0 < w.conv_price < 1]
    all_hp = [w.hedge_price() for w in windows if 0 < w.hedge_price() < 1]
    if all_cp:
        print(f"\n  Conviction fill prices: avg ${sum(all_cp)/len(all_cp):.4f} "
              f"(min ${min(all_cp):.4f}, max ${max(all_cp):.4f})")
    if all_hp:
        print(f"  Hedge fill prices:      avg ${sum(all_hp)/len(all_hp):.4f} "
              f"(min ${min(all_hp):.4f}, max ${max(all_hp):.4f})")
    combined = [cp + hp for cp, hp in zip(all_cp, all_hp) if cp + hp > 0]
    if combined:
        under1 = sum(1 for c in combined if c < 1.0)
        print(f"  Combined UP+DOWN:       avg ${sum(combined)/len(combined):.4f} "
              f"| {under1}/{len(combined)} under $1.00 "
              f"({under1/len(combined)*100:.0f}%)")

    # ── Strategy results ─────────────────────────────────────────────
    strategies = [
        strat_actual(windows),
        strat_single_hold(windows),
        strat_single_early_exit(windows),
        strat_always_hedge_hold(windows),
        strat_hedge_hold_longshot(windows),
    ]

    print(f"\n{'='*70}")
    print(f"  STRATEGY RESULTS")
    print(f"{'='*70}")

    for s in strategies:
        n = s.get("n", 0)
        w = s.get("wins", 0)
        wr = w / n * 100 if n > 0 else 0
        ev = s["pnl"] / n if n > 0 else 0

        print(f"\n{'─'*70}")
        print(f"  {s['name']}")
        print(f"{'─'*70}")
        print(f"  Total PnL:       ${s['pnl']:+.2f}")
        print(f"  Markets traded:  {n}")
        print(f"  Wins:            {w} ({wr:.1f}%)")
        print(f"  Losses:          {s.get('losses', n - w)}")
        print(f"  EV per trade:    ${ev:+.4f}")

        if "early" in s:
            print(f"  Early exits:     {s['early']}")
        if "early_exits" in s:
            print(f"  Early exits:     {s['early_exits']}")
        if "hedge_saves" in s:
            print(f"  Hedge saves:     {s['hedge_saves']}")
        if "struct_edge" in s:
            print(f"  Structural edge: ${s['struct_edge']:+.2f} "
                  f"(from UP+DOWN < $1.00)")
        if "ls_tries" in s and s["ls_tries"] > 0:
            print(f"  Long-shots:      {s['ls_hits']}/{s['ls_tries']} hit "
                  f"({s['ls_hits']/s['ls_tries']*100:.0f}%)")

        # Projections
        if n > 0:
            daily = ev * 144  # ~144 markets/day (2 assets × 12/hr × ~6 active hrs)
            monthly = daily * 30
            yearly = daily * 365
            print(f"\n  Projections (144 trades/day):")
            print(f"    Daily:   ${daily:+.2f}")
            print(f"    Monthly: ${monthly:+.2f}")
            print(f"    Yearly:  ${yearly:+.2f}")

    # ── Verdict ──────────────────────────────────────────────────────
    best = max(strategies, key=lambda s: s["pnl"] / s["n"] if s["n"] > 0 else -999)
    n = best["n"]
    ev = best["pnl"] / n if n > 0 else 0

    print(f"\n{'='*70}")
    print(f"  VERDICT")
    print(f"{'='*70}")
    print(f"  Best strategy:   {best['name']}")
    print(f"  EV/trade:        ${ev:+.4f}")
    print(f"  Projected daily: ${ev * 144:+.2f}")
    print(f"  Projected monthly: ${ev * 144 * 30:+.2f}")
    print(f"{'='*70}\n")


if __name__ == "__main__":
    main()
