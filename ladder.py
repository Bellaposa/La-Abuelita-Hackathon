"""The dealer ladder as a price: how many score points a deal at price p is worth, and how far past our private value
we may go for it.

    python3 ladder.py --selftest

RULES: the ladder scores, per dealer level, the share of each dealer's price range we capture (from its opening), our
best three deals per level count, a missing one as zero, higher levels weigh more; each day is a round. So a deal is
worth something only if it lifts one of our three best shares at that dealer in the current round.

  share(p)      buy:  (open - p) / (open - limit)      sell: (p - open) / (limit - open)      clipped to [0, 1]
                limit = the best price any team got from this dealer (feed_intel deals), else a fallback from the list
  points(p)     S * w(level) * max(0, share(p) - worst of our best three) / 3
                w(level) = level / 15 (prior: "higher levels weigh more"); S = score points per ladder unit, learned live
                from our own score (a change of ladder_points with trades and duels unchanged), prior S0
  budget(pts)   free cash * (1 - exp(-pts))      no fixed cap: small gains buy small premiums, never all the cash

ladder_cap / ladder_floor scan integer prices and return the furthest one whose premium over our private-value limit is
still covered by budget(points(p)); the negotiation then tries to pay less than that, as before.
"""
import math
import statistics
import sys

LEVEL = {"abuela": 1, "chato": 2, "pilar": 3, "picaros": 4, "banco": 5}
S0 = 11.0                 # prior: score points per ladder unit (+0.043 ladder -> +0.49 negotiating on Sat 19:15-19:25)
S_BOUNDS = (3.0, 30.0)
S_KEEP = 20
DEALS_KEEP = 60


def weight(dealer):
    return LEVEL.get(dealer, 1) / 15.0


def share(open_, limit, p, buy):
    """Share of the dealer's range [open, limit] captured at price p. 0 at its opening (that deal does not count)."""
    if open_ is None or limit is None or p is None:
        return 0.0
    span = (open_ - limit) if buy else (limit - open_)
    if span <= 0:
        return 1.0 if (p < open_ if buy else p > open_) else 0.0
    got = (open_ - p) if buy else (p - open_)
    return max(0.0, min(1.0, got / span))


def limit_estimate(hints, open_, buy, list_price=None):
    """Best price reachable with this dealer: what it gave any team (feed_intel hints), else a guess from list/opening."""
    if hints:
        return hints["deal_min"] if buy else hints["deal_max"]
    base = list_price or open_
    if base is None:
        return None
    return round(base * 0.8) if buy else round(base * 1.3)


def round_deals(mem, rnd, dealer):
    return [d for d in (mem.get("ladder") or {}).get("deals", []) if d.get("round") == rnd and d.get("dealer") == dealer]


def record_deal(mem, rnd, dealer, thread, open_, limit, price, buy):
    """Store a finished deal once (by thread) with the share it captured."""
    lad = mem.setdefault("ladder", {})
    deals = lad.setdefault("deals", [])
    if any(d.get("thread") == thread for d in deals):
        return None
    s = share(open_, limit, price, buy)
    deals.append({"round": rnd, "dealer": dealer, "thread": thread, "open": open_, "limit": limit, "price": price,
                  "buy": buy, "share": round(s, 3)})
    del deals[:-DEALS_KEEP]
    return s


def worst_of_best3(mem, rnd, dealer):
    shares = sorted((d["share"] for d in round_deals(mem, rnd, dealer)), reverse=True)[:3]
    return shares[2] if len(shares) == 3 else 0.0


def slope(mem):
    obs = (mem.get("ladder") or {}).get("slope_obs") or []
    return max(S_BOUNDS[0], min(S_BOUNDS[1], statistics.median(obs))) if len(obs) >= 3 else S0


def learn_slope(mem, score):
    """Feed me['score'] every tick: when ladder_points moved while trades and duels did not, negotiating moved because of
    the ladder alone, so d(negotiating)/d(ladder) is one observation of S."""
    if not score or score.get("ladder_points") is None:
        return None
    lad = mem.setdefault("ladder", {})
    keys = ("ladder_points", "negotiating", "neg_points", "duel_points")
    now = {k: score.get(k) for k in keys}
    prev, lad["last_score"] = lad.get("last_score"), now
    if not prev or any(prev.get(k) is None for k in keys):
        return None
    dl = now["ladder_points"] - prev["ladder_points"]
    if abs(dl) < 0.005 or now["neg_points"] != prev["neg_points"] or now["duel_points"] != prev["duel_points"]:
        return None
    s = (now["negotiating"] - prev["negotiating"]) / dl
    if not (S_BOUNDS[0] <= s <= S_BOUNDS[1]):
        return None
    obs = lad.setdefault("slope_obs", [])
    obs.append(round(s, 2))
    del obs[:-S_KEEP]
    return s


def points(mem, rnd, dealer, open_, limit, p, buy):
    gain = max(0.0, share(open_, limit, p, buy) - worst_of_best3(mem, rnd, dealer))
    return slope(mem) * weight(dealer) * gain / 3.0


def budget(free_cash, pts):
    return max(0.0, free_cash) * (1.0 - math.exp(-max(0.0, pts)))


def ladder_cap(mem, rnd, dealer, open_, limit, value_cap, free_cash):
    """Highest price we may pay when buying: value_cap (our private-value limit) or more, while the premium over it is
    covered by what the ladder gain is worth. (cap, points at cap, premium)."""
    best = (value_cap, points(mem, rnd, dealer, open_, limit, value_cap, True), 0)
    if open_ is None or limit is None:
        return best
    for p in range(max(1, value_cap + 1), min(open_, value_cap + int(free_cash)) + 1):
        pts = points(mem, rnd, dealer, open_, limit, p, True)
        if pts <= 0 or p - value_cap > budget(free_cash, pts):
            break
        best = (p, pts, p - value_cap)
    return best


def ladder_floor(mem, rnd, dealer, open_, limit, value_floor, free_cash):
    """Lowest price we may accept when selling: value_floor or less, while the value we give up under it is covered by
    the ladder gain. (floor, points at floor, premium)."""
    best = (value_floor, points(mem, rnd, dealer, open_, limit, value_floor, False), 0)
    if open_ is None or limit is None:
        return best
    for p in range(value_floor - 1, max(open_, 0), -1):
        pts = points(mem, rnd, dealer, open_, limit, p, False)
        if pts <= 0 or value_floor - p > budget(free_cash, pts):
            break
        best = (p, pts, value_floor - p)
    return best


def selftest():
    assert share(30, 20, 30, True) == 0.0 and share(30, 20, 20, True) == 1.0 and share(30, 20, 25, True) == 0.5
    assert share(16, 30, 23, False) == 0.5 and share(16, 30, 40, False) == 1.0 and share(16, 30, 10, False) == 0.0
    mem = {}
    # empty level: a Chato buy (opens 33, best any team got 26) with our value cap 21 -> we may go past 21, not to 33
    cap, pts, prem = ladder_cap(mem, 2, "chato", 33, 26, 21, 47)
    assert 21 < cap < 33 and prem == cap - 21 and pts > 0, (cap, pts, prem)
    # more free cash -> the same gain may buy a larger premium (never a fixed constant)
    cap_rich, _, _ = ladder_cap(mem, 2, "chato", 33, 26, 21, 300)
    assert cap_rich >= cap, (cap_rich, cap)
    # no cash: no premium
    assert ladder_cap(mem, 2, "chato", 33, 26, 21, 0)[0] == 21
    # a level already full of perfect deals: nothing left to gain, no premium
    for t in (1, 2, 3):
        record_deal(mem, 2, "chato", t, 33, 26, 26, True)
    assert worst_of_best3(mem, 2, "chato") == 1.0 and ladder_cap(mem, 2, "chato", 33, 26, 21, 300)[0] == 21
    assert record_deal(mem, 2, "chato", 1, 33, 26, 26, True) is None, "a thread is recorded once"
    # other round: empty again
    assert worst_of_best3(mem, 3, "chato") == 0.0
    # selling to Pilar (opens 16, best 20): floor 19 from value; ladder may lower it, never to her opening
    fl, pts, prem = ladder_floor({}, 2, "pilar", 16, 20, 19, 47)
    assert 16 < fl <= 19, fl
    # higher level, same share gain -> more points
    assert points({}, 2, "banco", 100, 120, 110, False) > points({}, 2, "abuela", 100, 120, 110, False)
    # slope learning: only from clean ladder moves, bounded, median of >= 3
    m = {}
    learn_slope(m, {"ladder_points": 0.20, "negotiating": 19.0, "neg_points": 150, "duel_points": 10})
    learn_slope(m, {"ladder_points": 0.25, "negotiating": 19.5, "neg_points": 150, "duel_points": 10})       # S = 10
    learn_slope(m, {"ladder_points": 0.30, "negotiating": 21.0, "neg_points": 160, "duel_points": 10})       # trades moved: skip
    assert m["ladder"]["slope_obs"] == [10.0] and slope(m) == S0
    learn_slope(m, {"ladder_points": 0.35, "negotiating": 21.6, "neg_points": 160, "duel_points": 10})       # S = 12
    learn_slope(m, {"ladder_points": 0.40, "negotiating": 22.3, "neg_points": 160, "duel_points": 10})       # S = 14
    assert slope(m) == 12.0, slope(m)
    print("ladder selftest OK")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
