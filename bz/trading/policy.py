"""Pure pricing and acceptance rules of trading V2 (no I/O, no clock reads except `pressure(..., now=)`).

Every constant is an explicit, named HYPOTHESIS in CFG: none of them has been measured on this game. Change them in one place
and the shadow log shows what each would have done.

The rules, in plain words:
  value        what one more copy is worth to us + a share of the expected page bonus when the card helps finish a page.
  margins      dynamic: smaller for liquid cards and near the close of the day, larger when cash is scarce. A page closer only
               needs a gain of 1.
  bids         adaptive: 60% of our maximum when nobody competes, +1 over a weak rival, up to 90% against strong competition,
               up to the maximum (minus a reserve) for the LAST card of a page. Never above our maximum.
  asks         match the cheapest rival ask (never start a price war: if it is below our floor we do not list);
               when nobody else sells it and somebody was seen bidding for it, ask 80% of that estimate.
  swaps        by marginal value of what we give (a spare copy is worth little to us); gain > 0 is enough.
"""
import math
from datetime import datetime, timezone

CFG = {
    "margin_base": 3.0,        # primas of gain we ask for a plain trade before the dynamic adjustments
    "margin_floor": 1,         # never ask for less than this
    "roi_min": 0.04,           # minimum gain as a share of cost (was 10% in the legacy rules)
    "liq_discount": 0.5,       # a fully liquid card cuts the margin by this share
    "pressure_discount": 0.6,  # the last minutes of a day cut the margin by this share
    "scarce_cash_ratio": 0.25, # free cash below this share of cash multiplies the margin
    "scarce_cash_mult": 1.5,
    "closer_min": 1,           # gain needed to buy the LAST missing card of a page
    "swap_min": 1,             # gain needed for a swap; 0 when the day is about to close
    "swap_min_late": 0,
    "late_pressure": 0.7,
    "page_credit": 0.65,       # share of the expected page bonus credited to a page card
    "page_p": {1: 1.0, 2: 0.5, 3: 0.25},      # probability of completing the page, by number of cards still missing
    "bid_none": 0.60, "bid_strong_cap": 0.90, "bid_last_start": 0.80, "bid_last_reserve": 1, "bid_inc_share": 0.05,
    "strong_share": 0.60,      # a rival bid above this share of our maximum is "strong competition"
    "list_markup": 0.95,       # no rival ask and nobody bidding: ask this share of book value
    "list_cap_book": 1.3, "list_cap_book_buyer": 2.5, "buyer_share": 0.80,
    "skip_list_p": 0.2, "jitter_p": 0.3,       # information hiding: delay some listings, vary prices by 1
    "list_per_tick": 4, "max_open": 28, "max_bids": 6, "value_calls": 8, "reserve": 0,   # value_calls: b.value() requests per tick
    "bid_budget_share": 0.5,   # all open bids together may commit at most this share of cash
    "max_deals_per_maker": 3, "deal_window": 600.0,        # fair play: do not keep feeding one counterparty
    "p_exec": {"buy": 0.9, "sell": 0.9, "swap": 0.8, "arb": 0.6},
    "arb": False,              # arbitrage needs cash to count as score: UNVERIFIED, off by default
    "arb_min": 2, "arb_max_inventory": 2, "arb_cash_share": 0.25,
    "pressure_minutes": 90,    # the margin discount ramps over the last N minutes before the day closes
}


def cfg_with(**over):
    c = dict(CFG)
    c.update(over)
    return c


def clamp(x, lo, hi):
    return max(lo, min(hi, x))


def pressure(clock, now=None):
    """0..1: how close the day's close is (1 = closing). 0 if the clock says nothing. `now` for tests."""
    try:
        closes = datetime.fromisoformat((clock or {})["closes"])
    except (KeyError, TypeError, ValueError):
        return 0.0
    now = now or datetime.now(timezone.utc)
    if closes.tzinfo is None:
        closes = closes.replace(tzinfo=timezone.utc)
    mins = (closes - now).total_seconds() / 60.0
    return clamp(1.0 - mins / CFG["pressure_minutes"], 0.0, 1.0)


def min_margin(liquidity, press, free_ratio, cfg=CFG):
    m = cfg["margin_base"] * (1 - cfg["liq_discount"] * liquidity) * (1 - cfg["pressure_discount"] * press)
    if free_ratio < cfg["scarce_cash_ratio"]:
        m *= cfg["scarce_cash_mult"]
    return max(cfg["margin_floor"], int(round(m)))


def roi_floor(press, cfg=CFG):
    return cfg["roi_min"] * (1 - press)


def page_credit(n_missing, bonus, cfg=CFG):
    """Share of the page bonus credited to a page card of a page that has `n_missing` cards left (0 when far from done)."""
    return cfg["page_credit"] * bonus * cfg["page_p"].get(n_missing, 0.0)


def fee(venue, price, n=1):
    return math.ceil((venue.get("fee_bps", 500)) * price / 10000) + venue.get("fee_per_card", 1) * n


def max_price(value, venue, margin):
    """Highest price p with value - p - fee(p) >= margin (0 if none)."""
    p = int(value - margin)
    while p > 0 and value - p - fee(venue, p) < margin:
        p -= 1
    return max(0, p)


# ------------------------------------------------------------------ bids

def bid_price(p_max, others, is_last, cfg=CFG):
    """(price, tier) for a standing bid, or (None, reason). `others`: prices of rival bids for the same card."""
    if p_max < 1:
        return None, "no price leaves a margin"
    best = max(others) if others else 0
    inc = max(1, int(round(cfg["bid_inc_share"] * p_max)))
    if is_last:
        target, tier = min(p_max - cfg["bid_last_reserve"], max(best + inc, int(math.ceil(cfg["bid_last_start"] * p_max)))), "last card of the page"
    elif not others:
        target, tier = int(round(cfg["bid_none"] * p_max)), "no competition"
    elif len(others) == 1 and best < cfg["strong_share"] * p_max:
        target, tier = best + 1, "weak competition"
    else:
        target, tier = min(best + inc, int(cfg["bid_strong_cap"] * p_max)), "strong competition"
    target = min(target, p_max)
    if target < 1:
        return None, "target below 1"
    if others and target <= best:
        return None, f"cannot beat the best rival bid {best} within our maximum {p_max}"
    return int(target), tier


# ------------------------------------------------------------------ asks

def sell_ask(floor, book, rival_asks, buyer_est, rng, cfg=CFG):
    """(ask, why) for one spare copy, or (None, reason).
    No price war: if the cheapest rival ask is below our floor we do not list. We MATCH the cheapest rival ask (and sometimes
    undercut by 1 to vary prices). With no rival ask: 95% of book, or 80% of what a bidder was seen paying if that is higher."""
    cheapest = min(rival_asks) if rival_asks else None
    if cheapest is not None and cheapest < floor:
        return None, f"abandon: cheapest rival ask {cheapest} is below our floor {floor}"
    if cheapest is not None:
        ask, why, cap = max(floor, cheapest), f"match the cheapest rival ask {cheapest}", max(floor, cheapest)
    else:
        ask, why = max(floor, int(math.ceil(cfg["list_markup"] * book))), "no rival ask: near book"
        cap = int(math.ceil(cfg["list_cap_book"] * book))
        if buyer_est:
            boosted = int(math.ceil(cfg["buyer_share"] * buyer_est["price"]))
            if boosted > ask:
                ask, why, cap = boosted, f"a bidder was seen paying {buyer_est['price']:.0f}: ask {int(cfg['buyer_share'] * 100)}%", \
                                int(math.ceil(cfg["list_cap_book_buyer"] * book))
    ask = max(floor, min(ask, max(cap, floor)))
    if ask - 1 >= floor and rng.random() < cfg["jitter_p"]:
        ask -= 1
        why += " (-1 to vary prices)"
    return ask, why


# ------------------------------------------------------------------ ranking

def score(opp, spendable, press, cfg=CFG):
    """expected gain x probability of execution x urgency, discounted by the share of our cash it locks up."""
    p = cfg["p_exec"].get(opp.get("kind"), 0.5)
    capital = 1.0 + opp.get("cost", 0) / max(spendable, 1.0)
    return opp["gain"] * p * (1.0 + press) / capital


def rank(opps, spendable, press, cfg=CFG):
    return sorted(opps, key=lambda o: (-score(o, spendable, press, cfg), -o["gain"], str(o.get("offer"))))
