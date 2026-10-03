"""Trading V2 pure rules (bz/trading/policy.py): margins, page credit, price ceilings, bid tiers, ask rules, ranking.

Expected values are derived by hand from the rules in the module docstring (not from running the code), so they are
specifications of the intended behaviour. The constants themselves are hypotheses (policy.CFG); changing one means updating
the matching expectation here on purpose.
"""
import random
from datetime import datetime, timedelta, timezone

import pytest

from bz.trading import policy
from bz.trading.policy import CFG

UTC = timezone.utc


class Rng:
    """random() returns the scripted values in order (default 0.99 = 'no')."""
    def __init__(self, *vals):
        self.vals = list(vals)

    def random(self):
        return self.vals.pop(0) if self.vals else 0.99


# ------------------------------------------------------------------ pressure / margins

def clock_closing_in(minutes, now):
    return {"closes": (now + timedelta(minutes=minutes)).isoformat()}


@pytest.mark.parametrize("minutes, expected", [(300, 0.0), (90, 0.0), (45, 0.5), (0, 1.0), (-10, 1.0)])
def test_pressure_ramps_over_the_last_90_minutes_of_the_day(minutes, expected):
    now = datetime(2026, 10, 3, 20, 0, tzinfo=UTC)
    assert policy.pressure(clock_closing_in(minutes, now), now) == pytest.approx(expected)


def test_pressure_is_zero_without_a_usable_clock():
    for clock in (None, {}, {"closes": "not a date"}):
        assert policy.pressure(clock) == 0.0


@pytest.mark.parametrize("liq, press, free, expected", [
    (0.0, 0.0, 1.0, 3),      # base margin 3
    (1.0, 0.0, 1.0, 2),      # a fully liquid card: 3 * 0.5 = 1.5 -> 2
    (0.0, 1.0, 1.0, 1),      # closing: 3 * 0.4 = 1.2 -> 1
    (1.0, 1.0, 1.0, 1),      # never below the floor
    (0.0, 0.0, 0.1, 4),      # scarce cash multiplies by 1.5: 4.5 -> 4 (banker's rounding)
])
def test_min_margin_is_dynamic_and_never_below_one(liq, press, free, expected):
    assert policy.min_margin(liq, press, free) == expected


def test_roi_floor_shrinks_toward_the_close():
    assert policy.roi_floor(0.0) == pytest.approx(0.04) and policy.roi_floor(1.0) == 0.0


# ------------------------------------------------------------------ page credit and price ceilings

def test_page_credit_by_cards_missing():
    assert policy.page_credit(1, 100) == pytest.approx(65.0)         # last card: 65% of the bonus
    assert policy.page_credit(2, 100) == pytest.approx(32.5)
    assert policy.page_credit(3, 100) == pytest.approx(16.25)
    assert policy.page_credit(4, 100) == 0.0                         # far from done: no credit


def test_fee_matches_the_venue_formula():
    rastro = {"fee_bps": 500, "fee_per_card": 1}
    assert policy.fee(rastro, 20) == 2 and policy.fee(rastro, 47) == 4 and policy.fee({"fee_bps": 0, "fee_per_card": 0}, 99) == 0


@pytest.mark.parametrize("value, margin, expected", [(100, 1, 93), (100, 3, 91), (10, 3, 5), (3, 3, 0)])
def test_max_price_leaves_the_margin_after_fees(value, margin, expected):
    rastro = {"fee_bps": 500, "fee_per_card": 1}
    p = policy.max_price(value, rastro, margin)
    assert p == expected
    if p:
        assert value - p - policy.fee(rastro, p) >= margin and value - (p + 1) - policy.fee(rastro, p + 1) < margin


# ------------------------------------------------------------------ bids

def test_bid_with_no_competition_is_sixty_percent_of_our_maximum():
    assert policy.bid_price(100, [], False) == (60, "no competition")


def test_bid_just_over_a_weak_rival():
    assert policy.bid_price(100, [30], False) == (31, "weak competition")


def test_bid_against_strong_competition_climbs_but_stops_at_ninety_percent():
    assert policy.bid_price(100, [70, 50], False) == (75, "strong competition")      # best 70 + increment 5
    assert policy.bid_price(100, [88, 70], False) == (90, "strong competition")      # capped at 90% of 100
    assert policy.bid_price(100, [90, 70], False)[0] is None                         # cannot beat 90 within the cap


def test_a_rival_bid_above_sixty_percent_counts_as_strong_even_alone():
    assert policy.bid_price(100, [65], False) == (70, "strong competition")


def test_last_card_of_a_page_bids_high_but_keeps_a_reserve_under_the_maximum():
    price, tier = policy.bid_price(100, [], True)
    assert (price, tier) == (80, "last card of the page")                            # 80% opening
    price, _ = policy.bid_price(100, [95], True)
    assert price == 99 and price <= 100 - CFG["bid_last_reserve"]                    # never the full maximum


def test_bid_never_exceeds_the_maximum_and_never_ties_a_rival():
    rng = random.Random(1)
    for _ in range(300):
        p_max = rng.randint(1, 300)
        others = [rng.randint(1, 320) for _ in range(rng.randint(0, 3))]
        price, _ = policy.bid_price(p_max, others, rng.random() < 0.3)
        if price is not None:
            assert 1 <= price <= p_max
            assert not others or price > max(others)


def test_no_bid_when_no_price_leaves_a_margin():
    assert policy.bid_price(0, [], False)[0] is None


# ------------------------------------------------------------------ asks

def test_ask_is_abandoned_instead_of_starting_a_price_war():
    ask, why = policy.sell_ask(floor=15, book=20, rival_asks=[12, 18], buyer_est=None, rng=Rng())
    assert ask is None and "abandon" in why


def test_ask_matches_the_cheapest_rival_ask():
    ask, why = policy.sell_ask(floor=10, book=20, rival_asks=[19, 25], buyer_est=None, rng=Rng())
    assert ask == 19 and "match" in why


def test_ask_may_undercut_by_one_to_vary_prices_but_never_below_the_floor():
    assert policy.sell_ask(10, 20, [19], None, Rng(0.0))[0] == 18                   # jitter fires
    assert policy.sell_ask(19, 20, [19], None, Rng(0.0))[0] == 19                   # would cross the floor: no jitter


def test_ask_with_no_rival_is_near_book():
    ask, why = policy.sell_ask(floor=10, book=20, rival_asks=[], buyer_est=None, rng=Rng())
    assert ask == 19 and "near book" in why                                          # 95% of 20


def test_ask_uses_eighty_percent_of_a_seen_bid_when_nobody_else_sells_it():
    ask, why = policy.sell_ask(floor=10, book=20, rival_asks=[], buyer_est={"price": 40.0}, rng=Rng())
    assert ask == 32 and "bidder" in why                                             # 0.8 * 40
    ask, _ = policy.sell_ask(10, 20, [], {"price": 400.0}, Rng())
    assert ask == 50                                                                 # capped at 2.5 x book


def test_a_seen_bid_does_not_raise_the_ask_above_a_cheaper_rival():
    ask, why = policy.sell_ask(10, 20, [22], {"price": 40.0}, Rng())
    assert ask == 22 and "match" in why


def test_ask_is_never_below_the_floor():
    rng = random.Random(3)
    for _ in range(300):
        floor, book = rng.randint(1, 60), rng.randint(5, 80)
        rivals = [rng.randint(1, 90) for _ in range(rng.randint(0, 3))]
        est = {"price": rng.randint(1, 120)} if rng.random() < 0.5 else None
        ask, _ = policy.sell_ask(floor, book, rivals, est, rng)
        assert ask is None or ask >= floor


# ------------------------------------------------------------------ ranking

def test_score_prefers_urgent_cheap_probable_gains():
    big = {"kind": "buy", "gain": 10, "cost": 100}
    small = {"kind": "buy", "gain": 8, "cost": 10}
    assert policy.rank([big, small], spendable=100, press=0.0)[0] is small           # capital locked matters
    swap = {"kind": "swap", "gain": 10, "cost": 2}
    buy = {"kind": "buy", "gain": 10, "cost": 2}
    assert policy.rank([swap, buy], 100, 0.0)[0] is buy                              # buys execute more surely (0.9 vs 0.8)


def test_rank_is_deterministic_on_ties():
    a, b = {"kind": "buy", "gain": 5, "cost": 5, "offer": 2}, {"kind": "buy", "gain": 5, "cost": 5, "offer": 1}
    assert policy.rank([a, b], 100, 0.0) == policy.rank([b, a], 100, 0.0)
