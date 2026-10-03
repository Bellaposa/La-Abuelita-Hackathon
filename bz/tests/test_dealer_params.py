"""Dealer negotiation parameters: one bounded table (bz/dealers/params.py), learned values derived from history.

What these tests protect:
  * the table's defaults ARE the numbers the strategy used before (introducing it changed no behaviour: the golden tests pass);
  * every value, whatever its source (default, DEALER_PARAMS override, learning), stays inside its bounds;
  * the three learned values (tol_share, open_scale, block_after_fail) move only with enough evidence, in the right direction,
    and never leave their bounds; DEALER_LEARN=0 switches learning off;
  * the safety caps (pack_edge, card_edge) are bounded but NOT learned;
  * dealer list prices come from the dealer's published menu, not from numbers typed in the code;
  * no new magic number appears inside the negotiation functions (AST check).
"""
import ast
import json
import os

import pytest

from bz.dealers import params as dp
from helpers import FIXTURES, ROOT, prod

sa = prod("smart_agent")
H = lambda *xs: [(a, f, "") for a, f in xs]


def rnd(ask, our, final=False):
    return {"her_ask": ask, "final": final, "our": our, "our_move": None, "her_move": None, "gap_before": None}


def neg(key, outcome, amount, rounds, sale=False, reason=None):
    n = {"dealer": key, "outcome": outcome, "closed_reason": reason, "rounds": rounds}
    n["received" if sale else "paid"] = amount
    return n


def mem_with(*negs):
    m = sa.load_memory()
    m["observed"]["negotiations"] = list(negs)
    return m


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    monkeypatch.delenv("DEALER_PARAMS", raising=False)
    monkeypatch.delenv("DEALER_LEARN", raising=False)


# ------------------------------------------------------------------ the table

PREVIOUS_CONSTANTS = {   # the numbers that were typed inside smart_agent.py before the table
    "tol_share": 0.04, "default_k": 0.25, "sell_k0": 0.35, "firm_k": 0.5, "slow_mult": 0.5, "token_k": 0.0,
    "k_small": 0.1, "k_mid": 0.25, "k_large": 0.5, "open_frac_list": 0.5, "open_frac_median": 0.8, "open_frac_settle": 0.7,
    "open_ask_mult": 1.2, "soft_cap_mult": 1.05, "pack_edge": 0.9, "card_edge": 0.95, "floor_mult": 1.4, "floor_add": 2,
    "cand_floor_share": 0.85, "cand_score_share": 0.8, "buy_realistic": 0.8, "pilar_list_mult": 1.0, "pilar_focus_mult": 1.1,
    "block_after_fail": 30, "retry_same_card": 90, "buy_block": 20, "buy_retry": 60, "fallback_list": 26,
    "firm_rounds": 3, "bucket_small": 0.15, "bucket_mid": 0.35, "soft_add": 1, "card_margin": 1, "reach_cap": 20,
    "traits_every": 200, "min_samples": 3, "min_deals": 2, "min_settle": 2, "min_settle_cap": 3, "min_evidence": 3,
    "trait_shrew": 0.8, "trait_gen": 0.6, "trait_pat": 0.3, "prior_lo": 0.5, "prior_hi": 1.6,
    "ref_shrew": 0.4, "ref_gen": 0.5, "ref_pat": 0.6, "open_scale": 1.0,
}


def test_defaults_equal_the_numbers_the_code_used_before():
    assert {k: dp.static(k) for k in PREVIOUS_CONSTANTS} == PREVIOUS_CONSTANTS
    assert set(dp.SPEC) == set(PREVIOUS_CONSTANTS), "a parameter was added or removed: update this list on purpose"


def test_every_default_is_inside_its_own_bounds_and_documented():
    for name, (default, lo, hi, doc) in dp.SPEC.items():
        assert lo <= default <= hi, name
        assert doc and len(doc) > 10, name


def test_clamp_keeps_values_inside_and_ints_stay_ints():
    assert dp.clamp("pack_edge", 5) == 0.99 and dp.clamp("pack_edge", -5) == 0.5
    assert dp.clamp("firm_rounds", 99) == 6 and isinstance(dp.clamp("firm_rounds", 4.4), int)
    assert dp.clamp("tol_share", "garbage") == dp.SPEC["tol_share"][0] and dp.clamp("tol_share", None) == 0.04


def test_environment_override_is_clamped_and_unknown_or_broken_input_is_ignored(monkeypatch):
    monkeypatch.setenv("DEALER_PARAMS", json.dumps({"pack_edge": 5, "default_k": 0.4, "nope": 1}))
    assert dp.static("pack_edge") == 0.99 and dp.static("default_k") == 0.4 and dp.static("sell_k0") == 0.35
    for broken in ("{not json", "[1,2]", "", '"x"'):
        monkeypatch.setenv("DEALER_PARAMS", broken)
        assert dp.static("default_k") == 0.25


def test_an_override_really_changes_the_strategy(monkeypatch):
    base = H((30, False))
    assert sa.next_offer(24, 26, [13], base, {})[:2] == ("offer", 17)                       # k 0.25 -> step 4
    monkeypatch.setenv("DEALER_PARAMS", json.dumps({"default_k": 0.5}))
    assert sa.next_offer(24, 26, [13], base, {})[:2] == ("offer", 21)                       # k 0.5 -> step 8
    monkeypatch.setenv("DEALER_PARAMS", json.dumps({"firm_rounds": 2}))
    assert sa.tone([13], H((30, False), (30, False))) == "firm"                              # two equal asks are enough now
    monkeypatch.delenv("DEALER_PARAMS")
    assert sa.tone([13], H((30, False), (30, False))) != "firm"                              # the default needs three


def test_safety_caps_are_bounded_and_not_learned():
    assert "pack_edge" not in dp.LEARNED and "card_edge" not in dp.LEARNED
    huge = mem_with(*[neg("abuela", "deal", 20, [rnd(20, 20)]) for _ in range(30)])
    assert "pack_edge" not in dp.derive(huge, "abuela")["values"] and dp.static("pack_edge") == 0.9
    assert sa.pack_cap({"cash": 1000, "affinity": {}, "assets": []}, {"sets": [], "packs": [{"id": "sobre_barrio", "slots": []}],
                                                                       "values": {"copy_marginals": [1]}}) == 0


# ------------------------------------------------------------------ learned: tol_share

def deal_gap(gap, key="abuela", lp=26):
    """A deal closed by accepting her ask `gap` primas above our last bid."""
    return neg(key, "deal", 20 + gap, [rnd(20 + gap, 20)])


def test_tol_share_needs_three_deals_then_covers_the_usual_gap():
    two = mem_with(deal_gap(1), deal_gap(1))
    assert "tol_share" not in dp.derive(two, "abuela", 26)["values"]
    three = mem_with(deal_gap(1), deal_gap(2), deal_gap(1))            # gaps 1,2,1 of 26 -> median 1/26 -> x1.5 = 0.0577
    assert dp.derive(three, "abuela", 26)["values"]["tol_share"] == pytest.approx(1.5 / 26)


def test_tol_share_is_bounded_and_finals_are_excluded():
    wide = mem_with(*[deal_gap(9) for _ in range(5)])                  # gap 9/26 = 0.35 -> would be 0.52: capped at 0.10
    assert dp.derive(wide, "abuela", 26)["values"]["tol_share"] == 0.10
    zero = mem_with(*[deal_gap(0) for _ in range(5)])
    assert dp.derive(zero, "abuela", 26)["values"]["tol_share"] == 0.0
    finals = mem_with(*[neg("abuela", "deal", 29, [rnd(29, 20, final=True)]) for _ in range(5)])
    assert "tol_share" not in dp.derive(finals, "abuela", 26)["values"]


def test_a_learned_tolerance_changes_when_we_accept():
    ours, hers = [20], H((30, False), (22, False))                      # her ask 22 is 2 above our last bid 20
    assert sa.next_offer(30, 26, ours, hers, {})[0] == "offer"          # default tolerance: int(4% of 26) = 1 -> 22 > 21
    assert sa.next_offer(30, 26, ours, hers, {"params": {"tol_share": 0.10}})[:2] == ("accept", 22)    # int(10% of 26) = 2 -> 22 <= 22
    assert sa.next_offer(30, 26, ours, hers, {"params": {"tol_share": 0.0}})[0] == "offer"


def test_sell_side_tolerance_is_mirrored():
    m = mem_with(*[neg("chato", "deal", 19, [rnd(19, 20)], sale=True) for _ in range(3)])      # we asked 20, got 19: gap 1 of 26
    assert dp.derive(m, "chato", 26)["values"]["tol_share"] == pytest.approx(1.5 / 26)


# ------------------------------------------------------------------ learned: open_scale

def quick_deal(key="abuela", sale=False):
    return neg(key, "deal", 15, [rnd(15, 15)], sale=sale)                  # closed at our opening in 1 round


def slow_deal(key="abuela", sale=False):
    return neg(key, "deal", 25, [rnd(30, 10)] * 7, sale=sale)             # 7 rounds, ended far from our opening of 10


def test_quick_deals_lower_the_opening_and_slow_ones_raise_it_buy_side():
    assert dp.derive(mem_with(quick_deal(), quick_deal()), "abuela", 26)["values"]["open_scale"] == pytest.approx(0.92)
    assert dp.derive(mem_with(slow_deal(), slow_deal()), "abuela", 26)["values"]["open_scale"] == pytest.approx(1.06)    # 1 + 0.04*0.75*2
    both = dp.derive(mem_with(quick_deal(), slow_deal()), "abuela", 26)["values"]["open_scale"]
    assert both == pytest.approx(1 + 0.04 * (0.75 - 1))
    assert "open_scale" not in dp.derive(mem_with(neg("abuela", "closed", None, [rnd(30, 10)])), "abuela", 26)["values"]


def test_sell_side_signs_are_mirrored():
    assert dp.derive(mem_with(quick_deal("chato", True)), "chato", 26)["values"]["open_scale"] == pytest.approx(1.04)    # he took our ask: ask more
    assert dp.derive(mem_with(slow_deal("chato", True)), "chato", 26)["values"]["open_scale"] == pytest.approx(0.97)


def test_open_scale_stays_inside_its_bounds():
    lo = dp.derive(mem_with(*[quick_deal() for _ in range(10)]), "abuela", 26)["values"]["open_scale"]
    hi = dp.derive(mem_with(*[slow_deal() for _ in range(10)]), "abuela", 26)["values"]["open_scale"]
    assert dp.SPEC["open_scale"][1] <= lo < 1.0 < hi <= dp.SPEC["open_scale"][2]


def test_open_scale_scales_the_opening_bid_on_every_rule_and_the_sell_ask_never_passes_the_list_price():
    assert sa.opening_bid(26, {"params": {"open_scale": 0.92}}) == 12                      # round(13 * 0.92)
    assert sa.opening_bid(26, {"paid_median": 21, "params": {"open_scale": 1.1}}) == 19    # round(17 * 1.1)
    assert sa.opening_bid(26, {"open_bid": 15, "params": {"open_scale": 0.6}}) == 9
    assert sa.opening_bid(26, {}) == 13                                                    # no params: unchanged
    assert sa.next_offer_sell(20, 26, [], [], {"open_ask": 24, "params": {"open_scale": 1.4}})[:2] == ("offer", 26)   # capped at list
    assert sa.next_offer_sell(20, 26, [], [], {"open_ask": 24, "params": {"open_scale": 0.9}})[:2] == ("offer", 22)


# ------------------------------------------------------------------ learned: block_after_fail

def test_each_recent_cooloff_stretches_our_patience_within_bounds():
    cool = lambda: neg("chato", "cooloff", None, [], sale=True, reason="cooloff")
    assert dp.derive(mem_with(), "chato")["values"] == {}
    assert dp.derive(mem_with(cool()), "chato")["values"]["block_after_fail"] == 45
    assert dp.derive(mem_with(cool(), cool()), "chato")["values"]["block_after_fail"] == 60
    assert dp.derive(mem_with(*[cool() for _ in range(10)]), "chato")["values"]["block_after_fail"] == 120      # capped


def test_phase_chato_uses_the_learned_block_after_a_failed_conversation():
    class Ended:
        def thread(self, tid):
            return {"id": tid, "with": "chato", "status": "closed", "closed_reason": "no_progress", "messages": []}
    def run(extra):
        mem = sa.load_memory()
        mem["observed"]["negotiations"] = list(extra)
        mem["active_chato"] = {"thread": 7, "topic": {"sell": {"assets": [9]}}, "cash_start": 100, "ref": "MAL-07", "floor": 16, "lp": 26, "lost": 8}
        me = {"id": "t06", "tick": 40, "cash": 100, "unlocked": ["chato"], "open_threads": [], "assets": [], "affinity": {}}
        sa.phase_chato(Ended(), me, {}, mem, True)
        return mem["chato_block_until"] - 40
    assert run([]) == 30
    two_cool = [neg("chato", "cooloff", None, [], sale=True, reason="cooloff")] * 2
    assert run(two_cool) >= 60


def test_learning_can_be_switched_off(monkeypatch):
    m = mem_with(*[quick_deal() for _ in range(5)], *[deal_gap(2) for _ in range(5)])
    assert dp.derive(m, "abuela", 26)["values"]
    monkeypatch.setenv("DEALER_LEARN", "0")
    assert dp.derive(m, "abuela", 26)["values"] == {} and dp.learned_params(m, "abuela", 26) == {
        "tol_share": 0.04, "open_scale": 1.0, "block_after_fail": 30}


def test_learned_params_are_always_inside_the_bounds_whatever_the_history():
    import random
    rng = random.Random(11)
    for _ in range(200):
        negs = []
        for _ in range(rng.randint(0, 10)):
            sale = rng.random() < 0.5
            amount = rng.choice([None, rng.randint(1, 80)])
            rounds = [rnd(rng.randint(1, 80), rng.choice([None, rng.randint(1, 80)]), rng.random() < 0.2) for _ in range(rng.randint(0, 8))]
            negs.append(neg("x", rng.choice(["deal", "closed", "cooloff"]), amount, rounds, sale=sale, reason=rng.choice([None, "cooloff", "no_progress"])))
        p = dp.learned_params(mem_with(*negs), "x", rng.choice([None, 10, 26, 150]))
        for name, v in p.items():
            assert dp.SPEC[name][1] <= v <= dp.SPEC[name][2], (name, v)


def test_learned_flows_into_the_dict_the_strategy_reads():
    m = mem_with(deal_gap(1), deal_gap(2), deal_gap(1), quick_deal())
    inf = sa.learned(m, "abuela", 26)
    assert inf["params"]["tol_share"] == pytest.approx(1.5 / 26) and inf["params"]["open_scale"] < 1.0
    assert sa.learned(mem_with(), "abuela")["params"] == {"tol_share": 0.04, "open_scale": 1.0, "block_after_fail": 30}


# ------------------------------------------------------------------ list prices come from the dealer's menu

class FakeDealers:
    def __init__(self):
        self.payload = json.load(open(os.path.join(FIXTURES, "dealers.json"), encoding="utf-8"))

    def dealers(self):
        return self.payload


def test_refresh_stores_the_published_menu_of_the_real_dealers():
    mem = sa.load_memory()
    assert sa.refresh_traits(FakeDealers(), mem, 1000)
    assert sa.menu_list_price(mem, "abuela", "pack:sobre_barrio") == 26
    assert sa.menu_list_price(mem, "chato", "rarity:rare") == 77 and sa.menu_list_price(mem, "chato", "pack:sobre_plata") == 150
    assert sa.menu_list_price(mem, "pilar", "pack:sobre_oro") == 420
    assert sa.menu_list_price(mem, "abuela", "pack:nope", default=7) == 7
    assert sa.menu_rarity_prices(mem, "chato") == {"uncommon": 26, "rare": 77}


def test_abuela_uses_the_published_list_price_not_a_typed_26(monkeypatch):
    seen = {}
    real = sa.next_offer
    monkeypatch.setattr(sa, "pack_cap", lambda me, catalog: 40)
    monkeypatch.setattr(sa, "next_offer", lambda cap, lp, ours, hers, inferred: seen.setdefault("lp", lp) and real(cap, lp, ours, hers, inferred))
    from helpers import FakeDealerAPI, buy_thread, me_state

    def run(menu):
        mem = sa.load_memory()
        if menu:
            mem["dealer_menu"] = {"abuela": {"pack:sobre_barrio": menu}}
        mem["active"] = {"thread": 7, "topic": {"buy": {"pack": "sobre_barrio"}}, "cash_start": 100}
        seen.clear()
        sa.phase_abuela(FakeDealerAPI(buy_thread(7, "t06", [], [], [])), me_state(7), {}, mem)
        return seen["lp"]
    assert run(40) == 40                                       # the dealer's own number
    assert run(None) == dp.static("fallback_list") == 26       # unknown menu: the documented fallback


def test_card_buying_merges_the_published_menu_over_the_old_table():
    mem = sa.load_memory()
    mem["dealer_menu"] = {"chato": {"rarity:rare": 99}}
    merged = {**sa.DEALER_SELLS["chato"], **sa.menu_rarity_prices(mem, "chato")}
    assert merged == {"uncommon": 26, "rare": 99}


def test_chato_candidate_prefers_the_menu_price():
    me = {"affinity": {"MAL": 0.5}, "assets": [{"id": 9, "kind": "card", "ref": "MAL-07", "serial": 1, "rarity": "uncommon"},
                                              {"id": 10, "kind": "card", "ref": "MAL-07", "serial": 2, "rarity": "uncommon"}]}
    cat = {"sets": [{"id": "MAL", "cards": [{"id": "MAL-07", "book": 25}]}], "values": {"copy_marginals": [1.0, 0.25, 0.1]}}
    assert sa.chato_candidate(me, cat)[4] == 26
    assert sa.chato_candidate(me, cat, menu={"uncommon": 40})[4] == 40


# ------------------------------------------------------------------ no new magic numbers in the negotiation code

NEGOTIATION = ["next_offer", "next_offer_sell", "opening_bid", "tone", "profile_hints", "pack_cap", "trait_multiplier", "chato_candidate",
               "pilar_candidate", "infer", "bucket", "phase_abuela", "phase_chato", "phase_card_buy", "refresh_traits", "learned",
               "menu_list_price", "learned_block", "menu_rarity_prices"]
ALLOWED = {0, 1, 2, -1,        # structure: first/last element, "at least two", signs
           3,                  # round(x, 3) and the thread-id/format widths
           4,                  # len("_buy")
           9, 10,              # 10 ** 9: "never" sentinel for tick arithmetic
           999, -999}          # sentinel: "never happened"


def test_no_numeric_literal_is_hidden_inside_the_negotiation_functions():
    tree = ast.parse(open(os.path.join(ROOT, "smart_agent.py"), encoding="utf-8").read())
    offenders = []
    for fn in (n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name in NEGOTIATION):
        for n in ast.walk(fn):
            if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)) and not isinstance(n.value, bool) and n.value not in ALLOWED:
                offenders.append((fn.name, n.lineno, n.value))
    assert offenders == [], f"magic numbers: move them to bz/dealers/params.py -> {offenders}"
