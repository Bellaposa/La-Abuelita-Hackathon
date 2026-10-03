"""Learning and profiles (the five 'calarlos' changes), tests of the first four:

  1. learn from fewer observations           MIN_SAMPLES 5 -> 3, MIN_DEALS 3 -> 2
  2. estimate where a dealer settles         profile_hints -> open_bid / open_ask / soft_cap
  3. use the dealers' published traits       trait_multiplier, refresh_traits, learned()
  4. rival profile in duels                  rival_profile / rival_prior, alias stored, prior tilts the price curve
  (5. TRADING_V2 shadow by default           tests in test_trading_v2_mode.py)

Expected numbers are derived by hand from the rules in the docstrings. Old behaviour without any evidence is frozen by the golden
tests (test_dealer_strategy_golden.py, test_duels_regression.py), which pass unchanged.
"""
import random

import pytest

from helpers import FIXTURES, new_duel_mem, prod, run_duel
import json
import os

sa, sd = prod("smart_agent"), prod("smart_duels")
H = lambda *xs: [(a, f, "") for a, f in xs]


def rnd(ask, our, final=False, om=None, hm=None, gap=None):
    return {"her_ask": ask, "final": final, "our": our, "our_move": om, "her_move": hm, "gap_before": gap}


def neg(dealer, outcome, amount=None, rounds=(), sale=False):
    n = {"dealer": dealer, "outcome": outcome, "rounds": list(rounds)}
    n["received" if sale else "paid"] = amount
    return n


def mem_with(*negs):
    m = sa.load_memory()
    m["observed"]["negotiations"] = list(negs)
    return m


# ------------------------------------------------------------------ 1. fewer observations

def small_rounds(n):
    return [rnd(30, 13, om=1, hm=1, gap=15)] * n


def test_a_step_bucket_needs_three_observations_now_not_five():
    assert (sa.MIN_SAMPLES, sa.MIN_DEALS) == (3, 2)
    two = mem_with(neg("abuela", "closed", rounds=small_rounds(2)))
    three = mem_with(neg("abuela", "closed", rounds=small_rounds(3)))
    assert sa.infer(two)["response_ratio"] == {}
    assert sa.infer(three)["response_ratio"] == {"small": {"mean": 1.0, "n": 3}}


def test_two_deals_are_enough_for_a_paid_median_now_not_three():
    one = mem_with(neg("abuela", "deal", 22))
    two = mem_with(neg("abuela", "deal", 22), neg("abuela", "deal", 20))
    assert sa.infer(one)["paid_median"] is None
    assert sa.infer(two)["paid_median"] == 21 and sa.opening_bid(26, sa.infer(two)) == 17


def test_best_k_still_needs_two_measured_buckets():
    one_bucket = mem_with(neg("abuela", "closed", rounds=small_rounds(9)))
    assert sa.infer(one_bucket)["best_k"] is None                            # no comparison possible with a single bucket


# ------------------------------------------------------------------ 2. where a dealer settles

def test_no_hints_without_two_settle_prices():
    assert sa.profile_hints(mem_with(neg("abuela", "deal", 22)), "abuela") == {}
    assert sa.profile_hints(mem_with(), "abuela") == {}


def test_buy_side_hints_from_deals_and_finals():
    m = mem_with(neg("abuela", "deal", 22, [rnd(25, 15, final=True), rnd(20, 13)]), neg("abuela", "deal", 24))
    h = sa.profile_hints(m, "abuela")
    # settle prices: deals 22 and 24 + her final 25 -> min 22, max 25
    assert (h["settle_n"], h["settle_min"], h["settle_max"]) == (3, 22, 25)
    assert h["open_bid"] == 15                                               # round(0.7 * 22)
    assert h["soft_cap"] == 27                                               # ceil(1.05 * 25)
    assert h["rejected"] == 15                                               # we offered 15 and she asked 25: refused; 13 vs 20 too
    assert "open_ask" not in h


def test_two_settle_prices_give_an_opening_but_not_a_soft_cap_yet():
    h = sa.profile_hints(mem_with(neg("abuela", "deal", 22), neg("abuela", "deal", 24)), "abuela")
    assert h["open_bid"] == 15 and "soft_cap" not in h


def test_sell_side_hints_mirror_the_buy_side():
    m = mem_with(neg("chato", "deal", 18, [rnd(20, 26, final=True), rnd(13, 22)], sale=True), neg("chato", "deal", 16, sale=True))
    h = sa.profile_hints(m, "chato")
    assert (h["settle_min"], h["settle_max"]) == (16, 20) and h["open_ask"] == 24      # ceil(1.2 * 20)
    assert h["rejected"] == 22                                               # lowest ask of ours he turned down (bid 13 < ask 22)
    assert "open_bid" not in h and "soft_cap" not in h


def test_hints_are_per_dealer_key():
    m = mem_with(neg("abuela", "deal", 22), neg("abuela", "deal", 24), neg("abuela_buy", "deal", 9), neg("chato", "deal", 5, sale=True))
    assert sa.profile_hints(m, "abuela")["settle_n"] == 2
    assert sa.profile_hints(m, "abuela_buy") == {} and sa.profile_hints(m, "chato") == {}


def test_next_offer_opens_with_the_evidence_and_never_above_the_cap():
    assert sa.next_offer(24, 26, [], [], {"open_bid": 15}) == ("offer", 15, "opening bid")
    assert sa.next_offer(10, 26, [], [], {"open_bid": 15}) == ("offer", 10, "opening bid")
    assert sa.next_offer(24, 26, [], [], {}) == ("offer", 13, "opening bid")             # unchanged without evidence


def test_next_offer_creeps_one_prima_at_a_time_past_the_usual_ceiling():
    args = (30, 26, [20, 26], H((45, False), (44, False)))
    assert sa.next_offer(*args, {})[:2] == ("offer", 30)                                 # gap 18 -> step 4 -> capped at 30
    action, price, why = sa.next_offer(*args, {"soft_cap": 25})
    assert (action, price) == ("offer", 27) and "creeping" in why                        # last 26 >= 25: step 1
    assert sa.next_offer(*args, {"soft_cap": 40})[:2] == ("offer", 30)                   # we are still below the ceiling: normal step


def test_the_soft_cap_never_lets_us_pass_the_hard_cap_nor_blocks_a_final():
    assert sa.next_offer(30, 26, [26], H((29, True)), {"soft_cap": 20})[:2] == ("accept", 29)        # a final under the cap is taken
    assert sa.next_offer(30, 26, [26], H((31, True)), {"soft_cap": 20})[0] == "walk"


def test_sell_opening_uses_the_evidence_between_floor_and_list():
    assert sa.next_offer_sell(20, 26, [], [], {"open_ask": 24})[:2] == ("offer", 24)
    assert sa.next_offer_sell(20, 26, [], [], {"open_ask": 40})[:2] == ("offer", 26)       # never above his list price
    assert sa.next_offer_sell(30, 26, [], [], {"open_ask": 24})[:2] == ("offer", 30)       # never below our floor
    assert sa.next_offer_sell(20, 26, [], H((12, False)), {"open_ask": 24})[:2] == ("offer", 24)   # he spoke first
    assert sa.next_offer_sell(20, 26, [], [], {})[:2] == ("offer", 26)                   # unchanged without evidence


def test_hints_never_break_the_cap_or_the_floor():
    rng = random.Random(5)
    for _ in range(400):
        cap, lp = rng.randint(5, 40), rng.choice([10, 26, 77])
        inf = {"open_bid": rng.randint(1, 60), "soft_cap": rng.randint(1, 60), "prior_mult": rng.uniform(0.5, 1.6),
               "open_ask": rng.randint(1, 90), "best_k": rng.choice([None, 0.1, 0.25, 0.5])}
        ours = sorted(rng.sample(range(1, 45), rng.randint(0, 3)))
        hers = [(rng.randint(1, 60), rng.random() < 0.2, "") for _ in range(rng.randint(0, 3))]
        act, price, _ = sa.next_offer(cap, lp, ours, hers, inf)
        assert price is None or price <= cap, (cap, ours, hers, inf, act, price)
        floor = rng.randint(1, 40)
        act, price, _ = sa.next_offer_sell(floor, lp, ours, hers, inf)
        assert price is None or price >= floor, (floor, ours, hers, inf, act, price)


# ------------------------------------------------------------------ 3. traits as a prior

ABUELA = {"patience": 0.85, "generosity": 0.8, "shrewdness": 0.2, "memory": 0.15, "strictness": 0.1, "chattiness": 0.75}
PILAR = {"patience": 0.6, "generosity": 0.5, "shrewdness": 0.75, "memory": 0.7, "strictness": 0.6, "chattiness": 0.55}


def test_trait_multiplier_soft_for_abuela_firm_for_pilar_and_bounded():
    assert sa.trait_multiplier(ABUELA) == pytest.approx(0.585)               # 1 + 0.8(-0.2) - 0.6(0.3) - 0.3(0.25)
    assert sa.trait_multiplier(PILAR) == pytest.approx(1.28)                 # 1 + 0.8(0.35)
    assert sa.trait_multiplier({}) == 1.0 and sa.trait_multiplier(None) == 1.0
    assert sa.trait_multiplier({"shrewdness": 5, "generosity": -5}) == 1.6 and sa.trait_multiplier({"shrewdness": -5, "generosity": 5}) == 0.5


def test_prior_scales_the_default_step_only_when_nothing_was_learned():
    base = H((30, False))
    assert sa.next_offer(24, 26, [13], base, {})[:2] == ("offer", 17)                                   # k 0.25 -> step 4
    action, price, why = sa.next_offer(24, 26, [13], base, {"prior_mult": 0.585})
    assert (action, price) == ("offer", 15) and "k=0.146" in why                                         # k 0.146 -> step 2
    assert sa.next_offer(24, 26, [13], base, {"prior_mult": 1.28})[:2] == ("offer", 18)                  # k 0.32 -> step 5
    assert sa.next_offer(24, 26, [13], base, {"prior_mult": 0.5, "best_k": 0.5})[:2] == ("offer", 21)    # learned k wins
    assert sa.next_offer_sell(20, 26, [26], H((10, False)), {"prior_mult": 0.5})[1] == 23                # k0 0.35 x 0.5 = 0.175 -> step 3


class FakeDealersAPI:
    def __init__(self, payload=None, error=None):
        self.payload, self.error, self.calls = payload, error, 0

    def dealers(self):
        self.calls += 1
        if self.error:
            raise prod("bazaar_sdk").BazaarError(self.error, "x", 500)
        return self.payload


@pytest.fixture
def real_dealers():
    return json.load(open(os.path.join(FIXTURES, "dealers.json"), encoding="utf-8"))          # real shape: {"personas": [...]}


def test_refresh_traits_reads_the_real_personas_shape(real_dealers):
    mem, api = sa.load_memory(), None
    api = FakeDealersAPI(real_dealers)
    assert sa.refresh_traits(api, mem, 1000) is True
    assert set(mem["dealer_traits"]) == {"abuela", "chato", "pilar"} and mem["dealer_traits"]["abuela"]["shrewdness"] == 0.2
    assert sa.trait_multiplier(mem["dealer_traits"]["abuela"]) == pytest.approx(0.585)
    assert sa.trait_multiplier(mem["dealer_traits"]["pilar"]) == pytest.approx(1.28)


def test_refresh_traits_is_throttled_and_survives_failures(real_dealers):
    mem = sa.load_memory()
    api = FakeDealersAPI(real_dealers)
    sa.refresh_traits(api, mem, 1000)
    assert sa.refresh_traits(api, mem, 1100) is False and api.calls == 1                 # within TRAITS_EVERY: no request
    assert sa.refresh_traits(api, mem, 1000 + sa.TRAITS_EVERY) is True and api.calls == 2
    failing = FakeDealersAPI(error="network")
    before = dict(mem["dealer_traits"])
    assert sa.refresh_traits(failing, mem, 5000) is False and mem["dealer_traits"] == before
    assert sa.refresh_traits(failing, mem, 5001) is False and failing.calls == 1         # a failure also waits a full period


def test_refresh_traits_ignores_malformed_rows_and_accepts_the_old_key():
    mem = sa.load_memory()
    api = FakeDealersAPI({"dealers": [{"id": "abuela", "traits": ABUELA}, {"id": "x"}, {"traits": PILAR}, "junk", {"id": "y", "traits": "no"}]})
    assert sa.refresh_traits(api, mem, 10) is True and list(mem["dealer_traits"]) == ["abuela"]


def test_learned_combines_inference_hints_and_prior_and_strips_the_buy_suffix():
    m = mem_with(neg("abuela", "deal", 22), neg("abuela", "deal", 24), neg("abuela_buy", "deal", 9), neg("abuela_buy", "deal", 11))
    m["dealer_traits"] = {"abuela": ABUELA}
    a, b = sa.learned(m, "abuela"), sa.learned(m, "abuela_buy")
    assert a["paid_median"] == 23 and a["open_bid"] == 15 and a["prior_mult"] == pytest.approx(0.585)
    assert b["open_bid"] == 6 and b["prior_mult"] == pytest.approx(0.585)                # traits belong to the dealer, not the item
    assert "prior_mult" not in sa.learned(mem_with(), "abuela")                          # no traits stored: no prior


# ------------------------------------------------------------------ 4. duel rival profiles

def duel_rec(role, limit, moves, rival=None, silent=0):
    hist = [{"rival_price": None, "rival_move": None}] * silent + [{"rival_price": 50 + i, "rival_move": m} for i, m in enumerate(moves)]
    obs = {"role": role, "limit": limit, "issues": ["price"], "history": hist}
    if rival:
        obs["rival"] = rival
    return {"observed": obs, "inferred": {}, "state": {}}


def mem_of(*recs):
    m = new_duel_mem()
    m["duels"] = {f"old{i}": r for i, r in enumerate(recs)}      # keys must not collide with the live duel id (1)
    return m


def test_rival_profile_normalises_moves_by_our_limit_and_counts_silence():
    m = mem_of(duel_rec("seller", 100, [5, 0, 5], silent=1), duel_rec("seller", 200, [10, 0], silent=1), duel_rec("buyer", 100, [9, 9]))
    p = sd.rival_profile(m, role="seller")
    assert p["duels"] == 2 and p["moves"] == 5
    assert p["move_frac"] == pytest.approx((0.05 + 0 + 0.05 + 0.05 + 0) / 5)           # 5/100, 0, 5/100, 10/200, 0
    assert p["firm_share"] == pytest.approx(2 / 5) and p["silent_share"] == pytest.approx(2 / 7)
    assert sd.rival_profile(m, role="buyer")["moves"] == 2
    assert sd.rival_profile(mem_of(), role="seller") == {"duels": 0, "moves": 0, "move_frac": None, "firm_share": None, "silent_share": None}


def test_alias_profile_uses_only_that_rival():
    m = mem_of(duel_rec("seller", 100, [5] * 4, rival="Rival Plata"), duel_rec("seller", 100, [0] * 4, rival="Rival Oro"), duel_rec("seller", 100, [1] * 4))
    assert sd.rival_profile(m, alias="Rival Plata", role="seller")["move_frac"] == pytest.approx(0.05)
    assert sd.rival_profile(m, alias="Rival Oro", role="seller")["firm_share"] == 1.0
    assert sd.rival_profile(m, alias="Rival Luna", role="seller")["duels"] == 0


def test_prior_needs_enough_moves_and_prefers_the_alias():
    thin = mem_of(duel_rec("seller", 100, [5] * 5))
    assert sd.rival_prior(thin, None, "seller", 100) is None                              # 5 role moves < 12
    role_ok = mem_of(duel_rec("seller", 100, [5] * 6), duel_rec("seller", 100, [5] * 6))
    p = sd.rival_prior(role_ok, "Rival X", "seller", 100)
    assert p == {"n": 12, "rate": pytest.approx(5.0), "firm": False, "source": "role"}
    alias_ok = mem_of(duel_rec("seller", 100, [0] * 6, rival="Rival X"), duel_rec("seller", 100, [5] * 8))
    p = sd.rival_prior(alias_ok, "Rival X", "seller", 100)
    assert p["source"] == "alias" and p["firm"] is True and p["rate"] == 0.0
    assert sd.rival_prior(alias_ok, "Rival Y", "seller", 100)["source"] == "role"


def test_the_prior_rate_is_scaled_to_the_current_limit():
    m = mem_of(duel_rec("seller", 100, [5] * 12))
    assert sd.rival_prior(m, None, "seller", 200)["rate"] == pytest.approx(10.0)


def test_act_stores_the_rival_alias_for_the_next_duel(monkeypatch):
    monkeypatch.setattr(sd, "save_mem", lambda m: None)
    mem = new_duel_mem()
    run_duel("seller", 100, [None, None], mem=mem, alias="Rival Plata")
    assert [r["observed"]["rival"] for r in mem["duels"].values()] == ["Rival Plata"]
    run_duel("seller", 100, [None], mem=new_duel_mem(), alias=None)                      # no alias in the payload: nothing stored, no crash


FIRM_RIVAL_HISTORY = [duel_rec("seller", 100, [0] * 8, rival="Rival Roca") for _ in range(2)]


def firm_mem():
    return mem_of(*[json.loads(json.dumps(r)) for r in FIRM_RIVAL_HISTORY])


def test_a_prior_of_a_firm_rival_makes_us_concede_sooner_from_the_start(monkeypatch):
    monkeypatch.setattr(sd, "save_mem", lambda m: None)
    import contextlib, io
    with contextlib.redirect_stdout(io.StringIO()):
        cold, _, _ = run_duel("seller", 100, [None] * 12, mem=new_duel_mem(), alias="Rival Roca")
        warm, _, _ = run_duel("seller", 100, [None] * 12, mem=firm_mem(), alias="Rival Roca")
    cold, warm = [p for p, _ in cold], [p for p, _ in warm]
    assert 0 <= cold[0] - warm[0] <= 2, "the opening stays at the anchor (the lower exponent only shaves a prima or two)"
    assert all(w <= c for w, c in zip(warm, cold)) and any(w < c for w, c in zip(warm, cold)), (cold, warm)


def test_a_prior_does_not_change_a_duel_when_no_alias_and_not_enough_history(monkeypatch):
    monkeypatch.setattr(sd, "save_mem", lambda m: None)
    import contextlib, io
    thin = mem_of(duel_rec("seller", 100, [0] * 3, rival="Rival Roca"))
    with contextlib.redirect_stdout(io.StringIO()):
        a, _, _ = run_duel("seller", 100, [None] * 8, mem=new_duel_mem())
        b, _, _ = run_duel("seller", 100, [None] * 8, mem=thin, alias="Rival Roca")
    assert a == b


@pytest.mark.parametrize("role", ["seller", "buyer"])
def test_priors_never_break_the_limits(role, monkeypatch):
    """Whatever the profile says, we never send below (seller) / above (buyer) our limit plus margin and never retract."""
    monkeypatch.setattr(sd, "save_mem", lambda m: None)
    import contextlib, io
    rng = random.Random(77)
    side = sd.side_of(role)
    for _ in range(80):
        limit = rng.randint(40, 200)
        recs = [duel_rec(role, rng.randint(40, 200), [rng.choice([0, 0, 3, 8, 15]) for _ in range(8)], rival="Rival Z") for _ in range(2)]
        mem = mem_of(*recs)
        start = limit * (0.3 if role == "seller" else 1.7)
        rival = [None if rng.random() < 0.2 else max(1, int(start + (1 if role == "seller" else -1) * rng.uniform(0, 0.08) * limit * k)) for k in range(16)]
        with contextlib.redirect_stdout(io.StringIO()):
            said, got, _ = run_duel(role, limit, rival, mem=mem, alias="Rival Z")
        prices = [p for p, _ in said]
        assert all(side * (p - limit) >= sd.MIN_MARGIN for p in prices), (role, limit, prices)
        assert all(side * (b - a) <= 0 for a, b in zip(prices, prices[1:])), prices
        if got is not None:
            assert side * (got - limit) >= sd.MIN_MARGIN
