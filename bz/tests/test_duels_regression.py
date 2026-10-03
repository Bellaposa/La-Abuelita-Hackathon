"""Duels: golden behaviour of smart_duels.py (+ invariants that must hold forever).

Golden values were produced by the CURRENT code on 2026-10-03 (commit 098ff55): they freeze behaviour, they do not
approve it. Invariants (limits, no retraction, never accept outside the limit) are rules of the game, not of the strategy.
`DAYS_SIGN` (+1) is an UNVERIFIED assumption; the days traces freeze what that assumption does today.
"""
import contextlib
import io
import random

import pytest

from helpers import FakeDuelAPI, new_duel_mem, prod, run_duel

sd = prod("smart_duels")


@pytest.fixture(autouse=True)
def quiet_and_memory_free(monkeypatch):
    """act() logs a lot; step()/main() are not used here, and save_mem must never touch disk even by accident."""
    monkeypatch.setattr(sd, "save_mem", lambda m: None)


def play(*a, **k):
    with contextlib.redirect_stdout(io.StringIO()):
        return run_duel(*a, **k)


# ------------------------------------------------------------------ pure helpers

@pytest.mark.golden
def test_side_reservation_and_utility():
    assert (sd.side_of("seller"), sd.side_of("buyer")) == (1, -1)
    assert (sd.reservation(1, 100), sd.reservation(-1, 100)) == (101, 99)          # MIN_MARGIN = 1 on the right side of the limit
    assert sd.utility(1, 100, 0, 120, None, False) == 20                           # seller: price above cost
    assert sd.utility(-1, 100, 0, 80, None, False) == 20                           # buyer: price below value
    assert sd.utility(1, 100, 2.0, 120, 8, True) == 26.0                           # + DAYS_SIGN * w * (days - 5)
    assert sd.utility(-1, 100, -1.5, 90, 2, True) == 14.5
    assert sd.utility(1, 100, 2.0, 120, None, True) == 20                          # no days offered: price part only


@pytest.mark.golden
def test_constants_the_strategy_depends_on():
    assert (sd.OPEN_ANCHOR, sd.MIN_MARGIN, sd.BETA, sd.DEFAULT_TICKS, sd.DEFAULT_DECAY, sd.DAYS_SIGN, sd.DAYS_CARE) == \
           (0.5, 1, 2.0, 16, 0.06, 1, 0.15)


ST = lambda anchor, last: {"anchor": anchor, "our_last": last}
FIRM, GIVING = {"n": 3, "rate": 0.0, "firm": True}, {"n": 4, "rate": 5.0, "firm": False}

# name -> (args of plan_price, expected (price, info))
PLAN_PRICE = {
    "seller first":      ((1, 100, ST(150, None), None, None, 1, 16), (150, {"beta": 2.0, "frac": 0.0})),
    "seller mid":        ((1, 100, ST(150, 140), None, 90, 8, 16), (138, {"beta": 2.0, "frac": 0.25})),
    "seller at the end": ((1, 100, ST(150, 120), None, 90, 16, 16), (101, {"beta": 2.0, "frac": 1.0})),
    "seller vs firm":    ((1, 100, ST(150, 140), FIRM, 95, 8, 16), (129, {"beta": 1.2, "frac": 0.44})),
    "seller vs giving":  ((1, 100, ST(150, 140), GIVING, 95, 8, 16), (140, {"beta": 2.6, "frac": 0.16})),
    "seller no retract": ((1, 100, ST(150, 105), None, 90, 2, 16), (105, {"beta": 2.0, "frac": 0.02})),
    "buyer first":       ((-1, 100, ST(50, None), None, None, 1, 16), (50, {"beta": 2.0, "frac": 0.0})),
    "buyer mid":         ((-1, 100, ST(50, 60), None, 110, 8, 16), (62, {"beta": 2.0, "frac": 0.25})),
    "buyer at the end":  ((-1, 100, ST(50, 80), None, 110, 16, 16), (99, {"beta": 2.0, "frac": 1.0})),
    "buyer vs firm":     ((-1, 100, ST(50, 60), FIRM, 105, 8, 16), (71, {"beta": 1.2, "frac": 0.44})),
    "buyer vs giving":   ((-1, 100, ST(50, 60), GIVING, 105, 8, 16), (60, {"beta": 2.6, "frac": 0.16})),
    "buyer no retract":  ((-1, 100, ST(50, 95), None, 110, 2, 16), (95, {"beta": 2.0, "frac": 0.02})),
}


@pytest.mark.golden
@pytest.mark.parametrize("name", list(PLAN_PRICE))
def test_plan_price_is_frozen(name):
    args, expected = PLAN_PRICE[name]
    assert sd.plan_price(*args) == expected


@pytest.mark.golden
def test_firm_rival_makes_us_concede_sooner_and_a_giving_rival_makes_us_hold_longer():
    """Adaptation direction (same for both roles): firm -> lower exponent -> price closer to the reservation."""
    for side, limit, anchor, last, rp in ((1, 100, 150, 140, 95), (-1, 100, 50, 60, 105)):
        base = sd.plan_price(side, limit, ST(anchor, last), None, rp, 8, 16)[0]
        firm = sd.plan_price(side, limit, ST(anchor, last), FIRM, rp, 8, 16)[0]
        giving = sd.plan_price(side, limit, ST(anchor, last), GIVING, rp, 8, 16)[0]
        assert side * (firm - base) < 0 and side * (giving - base) >= 0, (side, base, firm, giving)


# (u_now, u_next, remaining, stats-name, decay) -> (accept, reason)
STATS = {None: None, "firm": FIRM, "giving": GIVING, "mid": {"n": 3, "rate": 0.5, "firm": False}}
SHOULD_ACCEPT = [
    ((0.5, 10, 5, None, 0.06), (False, "rival offer below our margin")),
    ((5, 5, 5, None, 0.06), (True, "rival offer already as good as our next planned offer")),
    ((8, 5, 5, None, 0.06), (True, "rival offer already as good as our next planned offer")),
    ((8, 10, 1, None, 0.06), (True, "last round: a positive deal beats zero")),
    ((8, 10, 5, None, 0.06), (False, "holding: waiting is expected to pay more")),
    ((8, 10, 5, "firm", 0.06), (True, "waiting worth 5.6 <= 8.0 now (rate +0.0, firm=True)")),
    ((8, 10, 5, "giving", 0.06), (False, "holding: waiting is expected to pay more")),
    ((8, 10, 5, "mid", 0.06), (True, "waiting worth 6.7 <= 8.0 now (rate +0.5, firm=False)")),
    ((3, 10, 2, "mid", 0.06), (True, "waiting worth 2.2 <= 3.0 now (rate +0.5, firm=False)")),
    ((3, 10, 2, None, 0.06), (False, "holding: waiting is expected to pay more")),
    ((1, 1, 9, None, 0.06), (True, "rival offer already as good as our next planned offer")),
    ((20, 30, 4, "firm", 0.08), (True, "waiting worth 12.9 <= 20.0 now (rate +0.0, firm=True)")),
    ((9, 10, 0, None, 0.06), (True, "last round: a positive deal beats zero")),
    ((1, 2, 16, "giving", 0.06), (False, "holding: waiting is expected to pay more")),
]


@pytest.mark.golden
@pytest.mark.parametrize("args, expected", SHOULD_ACCEPT, ids=[f"{a[0]}-{a[1]}-{a[2]}-{a[3]}" for a, _ in SHOULD_ACCEPT])
def test_should_accept_is_frozen(args, expected):
    u_now, u_next, remaining, stats, decay = args
    assert sd.should_accept(u_now, u_next, remaining, STATS[stats], decay) == expected


@pytest.mark.golden
def test_should_accept_never_accepts_below_the_margin():
    """Rule, not strategy: a utility under MIN_MARGIN is never accepted, whatever the clock or the rival says."""
    for remaining in (0, 1, 5, 16):
        for stats in STATS.values():
            for u_next in (0, 1, 10):
                assert sd.should_accept(0.99, u_next, remaining, stats, 0.06)[0] is False


@pytest.mark.golden
def test_rival_stats_plan_days_time_left_and_parse_offer():
    assert sd.rival_stats([], 100) is None and sd.rival_stats([{"rival_move": 1}], 100) is None
    assert sd.rival_stats([{"rival_move": 2}, {"rival_move": 0}, {"rival_move": 0}], 100) == \
        {"n": 3, "rate": 0.6666666666666666, "firm": True}
    assert sd.rival_stats([{"rival_move": 3}, {"rival_move": 4}], 100) == {"n": 2, "rate": 3.5, "firm": False}
    assert sd.rival_stats([{"rival_move": None}, {"rival_move": 1}, {"rival_move": 1}], 100) == {"n": 2, "rate": 1.0, "firm": False}
    two = {"issues": ["price", "days"], "your_days_weight": 2.0}
    assert sd.plan_days({"issues": ["price"]}, 100, 0.5, [], None) == (None, 1.0)
    assert sd.plan_days(two, 100, 0.5, [], None) == (10, 1.0)
    assert sd.plan_days({"issues": ["price", "days"], "your_days_weight": -2.0}, 100, 0.5, [], None) == (0, 1.0)
    assert sd.plan_days(two, 100, 0.5, [10, 10], 10) == (10, 1.0)
    assert sd.plan_days(two, 100, 0.5, [3, 4], 4) == (7, 1.0)
    assert sd.time_left({"deadline_tick": 132}, 120, {"rounds": 0, "total": None}) == (12, 12)
    assert sd.time_left({}, 120, {"rounds": 3, "total": None}) == (13, 16)
    assert sd.time_left({"duel_ticks": 10}, 0, {"rounds": 2, "total": None}) == (8, 10)
    assert sd.parse_offer(None) == (None, None) and sd.parse_offer(30) == (30.0, None)
    assert sd.parse_offer({"price": 30, "days": 4}) == (30.0, 4) and sd.parse_offer({"offer": {"price": 31.0, "days": None}}) == (31.0, None)
    assert sd.duel_id({"duel": 7, "id": 9}) == 7 and sd.duel_id({"id": 9}) == 9


# ------------------------------------------------------------------ whole-duel traces through act() (frozen)

def asc(start, step, n=16):
    return [start + step * k for k in range(n)]


TRACES = {
    # name: (role, limit, rival prices, expected prices we SENT, rival price we ACCEPTED or None)
    "seller vs rising rival": ("seller", 100, asc(50, 4),
                               [150, 150, 150, 149, 148, 147, 145, 142, 140, 136, 132, 127, 122, 116, 109], 110),
    "seller vs silent rival": ("seller", 100, [None] * 16,
                               [150, 150, 149, 147, 146, 144, 141, 138, 135, 131, 127, 123, 118, 113, 107, 101], None),
    "seller vs stubborn low rival": ("seller", 100, [60] * 16,
                                     [150, 150, 144, 141, 138, 135, 132, 129, 126, 123, 119, 116, 112, 109, 105, 101], None),
    "seller vs fast rival": ("seller", 100, asc(60, 10), [150, 150, 150, 149, 148, 147, 145, 142], 140),
    "buyer vs falling rival": ("buyer", 100, asc(160, -4),
                               [50, 50, 50, 51, 52, 53, 55, 58, 60, 64, 68, 73, 78, 84, 91, 99], None),
    "buyer vs silent rival": ("buyer", 100, [None] * 16,
                              [50, 50, 51, 53, 54, 56, 59, 62, 65, 69, 73, 77, 82, 87, 93, 99], None),
    "buyer vs stubborn high rival": ("buyer", 100, [150] * 16,
                                     [50, 50, 56, 59, 62, 65, 68, 71, 74, 77, 81, 84, 88, 91, 95, 99], None),
    "buyer vs fast rival": ("buyer", 100, asc(160, -10), [50, 50, 50, 51, 52, 53, 55, 58, 60, 64], 60),
}


@pytest.mark.golden
@pytest.mark.parametrize("name", list(TRACES))
def test_full_duel_trace_is_frozen(name):
    role, limit, rival, sent, accepted = TRACES[name]
    said, got, _ = play(role, limit, rival)
    assert [p for p, _ in said] == sent
    assert got == accepted


@pytest.mark.golden
def test_two_issue_duel_traces_are_frozen_under_the_unverified_days_sign():
    """DAYS_SIGN = +1 is an assumption. These traces only freeze what it does today (price, days) per tick."""
    said, got, _ = play("seller", 100, asc(60, 3), issues=("price", "days"), w=2.0, rival_days=3)
    assert said == [(150, 10), (150, 10), (150, 10), (149, 10), (148, 10), (147, 9), (145, 9), (142, 9), (140, 8),
                    (136, 8), (132, 7), (127, 7), (122, 6), (116, 5), (109, 4)] and got == 105
    said, got, _ = play("buyer", 100, asc(150, -3), issues=("price", "days"), w=-1.0, rival_days=7)
    assert said == [(50, 0), (50, 0), (50, 0), (51, 0), (52, 0), (53, 1), (55, 1), (58, 1), (60, 2), (64, 2), (68, 3),
                    (73, 3), (78, 4), (84, 5), (91, 6), (99, 7)] and got is None


# ------------------------------------------------------------------ invariants (rules of the game)

def scripted_rival(rng, role, limit):
    """A random rival: starts far from us, moves at a random pace, sometimes silent, sometimes jumps past our limit."""
    toward = 1 if role == "seller" else -1
    start = limit * (0.3 if role == "seller" else 1.7) * rng.uniform(0.8, 1.2)
    rate = rng.uniform(-0.01, 0.1) * limit
    out = []
    for k in range(16):
        p = start + toward * rate * k
        out.append(None if rng.random() < 0.15 else max(1, int(round(p))))
    return out


@pytest.mark.parametrize("role", ["seller", "buyer"])
def test_never_send_or_accept_outside_the_limit_and_never_retract(role):
    """Seller: sent price >= limit + margin and never goes up. Buyer: sent price <= limit - margin and never goes down.
    An accepted rival price must give a utility of at least the margin."""
    rng = random.Random(20261003)
    for _ in range(150):
        limit = rng.randint(40, 200)
        rival = scripted_rival(rng, role, limit)
        said, got, _ = play(role, limit, rival)
        prices = [p for p, _ in said]
        side = sd.side_of(role)
        assert all(side * (p - limit) >= sd.MIN_MARGIN for p in prices), (role, limit, prices)
        assert all(side * (b - a) <= 0 for a, b in zip(prices, prices[1:])), f"retracted: {role} {limit} {prices}"
        if got is not None:
            assert side * (got - limit) >= sd.MIN_MARGIN, f"accepted {got} with limit {limit} as {role}"


@pytest.mark.parametrize("role, rival_price", [("seller", 99), ("seller", 100), ("buyer", 101), ("buyer", 100)])
def test_a_rival_offer_at_or_beyond_our_limit_is_never_accepted(role, rival_price):
    said, got, _ = play(role, 100, [rival_price] * 16)
    assert got is None


def test_one_message_per_duel_and_tick():
    b, mem = FakeDuelAPI(), new_duel_mem()
    duel = {"duel": 1, "status": "live", "role": "seller", "your_limit": 100, "issues": ["price"], "deadline_tick": 116,
            "rival_offer": {"price": 60}}
    with contextlib.redirect_stdout(io.StringIO()):
        for _ in range(3):
            sd.act(b, duel, 100, mem)                     # same tick three times
    assert len(b.said) + len(b.accepted) == 1


@pytest.mark.golden
def test_price_only_duels_never_send_days():
    said, _, _ = play("seller", 100, asc(60, 3))
    assert all(d is None for _, d in said)


# ------------------------------------------------------------------ record_finished

@pytest.mark.known_bug
def test_KNOWN_BUG_hotfix_1_5_record_finished_uses_the_real_duel_key():
    """KNOWN BUG — hotfix 1.5. The live API names the id `duel` (see duels_raw_samples fixture); record_finished reads
    `id`, so every finished duel is stored under "None" and they overwrite each other. Desired: one entry per duel id."""
    class B:
        def duels(self, done=False):
            return {"duels": [{"duel": 77, "status": "done", "result": "deal", "price": 120},
                              {"duel": 78, "status": "done", "result": "no_deal", "price": None}]}
    mem = new_duel_mem()
    sd.record_finished(B(), mem)
    assert set(mem["finished"]) == {"77", "78"}, f"stored under {sorted(mem['finished'])}"
