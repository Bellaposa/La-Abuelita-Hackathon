"""Characterization (golden) tests for the pure dealer strategy in smart_agent.py.

Every expected value below was produced by running the CURRENT code on 2026-10-03 (commit 098ff55). They do NOT claim the
strategy is good; they say "same inputs -> same output", so the functions can move without changing behaviour.
If a deliberate change alters one, update the test in the same commit and write down why.
"""
import pytest

from helpers import prod

sa = prod("smart_agent")
H = lambda *xs: [(a, f, "") for a, f in xs]          # dealer messages: (price, final, text)

# (id, cap, list_price, ours, hers, inferred) -> (action, price, reason)
NEXT_OFFER = [
    ("first offer", (24, 26, [], [], {}), ("offer", 13, "opening bid")),
    ("first offer, learned median 21", (24, 26, [], [], {"paid_median": 21}), ("offer", 17, "opening bid")),
    ("first offer never above a low cap", (10, 26, [], [], {}), ("offer", 10, "opening bid")),
    ("we spoke, no reply yet", (24, 26, [13], [], {}), ("wait", None, "no reply yet")),
    ("our last bid unanswered", (24, 26, [13, 15], H((30, False)), {}), ("wait", None, "she has not answered our last offer")),
    ("first reply: concede k=0.25 of the gap", (24, 26, [13], H((30, False)), {}),
     ("offer", 17, "mood unknown, gap 17, step 4 (k=0.25)")),
    ("normal concession, she yields", (24, 26, [13, 16], H((30, False), (28, False)), {}),
     ("offer", 19, "mood yielding, gap 12, step 3 (k=0.25)")),
    ("dealer does not concede, ask above cap: token step", (24, 26, [13, 15, 17], H((30, False), (30, False), (30, False)), {}),
     ("offer", 18, "mood firm, gap 13, step 1 (k=0.25, she is firm above cap, token step)")),
    ("dealer firm but ask under cap: close faster", (24, 26, [13, 15], H((24, False), (24, False)), {}),
     ("offer", 19, "mood firm, gap 9, step 4 (k=0.25, she is firm and ask <= cap, close)")),
    ("dealer concedes a lot: slow down", (24, 26, [13, 16], H((30, False), (18, False)), {}),
     ("offer", 17, "mood yielding, gap 2, step 1 (k=0.25, she out-conceded us, slow down)")),
    ("ask within 4% tolerance of our last bid", (24, 26, [22], H((30, False), (23, False)), {}),
     ("accept", 23, "her ask is within reach of our own bid")),
    ("final under cap", (24, 26, [13], H((22, True)), {}), ("accept", 22, "her final is under our cap")),
    ("final exactly at cap", (24, 26, [13], H((24, True)), {}), ("accept", 24, "her final is under our cap")),
    ("final over cap", (24, 26, [13], H((27, True)), {}), ("walk", None, "her final 27 is above our cap 24")),
    ("non-final ask over cap keeps bargaining", (24, 26, [20], H((30, False)), {}),
     ("offer", 22, "mood unknown, gap 10, step 2 (k=0.25)")),
    ("near cap and she is stalled", (24, 26, [23, 24], H((28, False), (28, False)), {}),
     ("walk", None, "at our cap 24, she asks 28")),
    ("at cap, let her move", (24, 26, [24], H((30, False), (27, False)), {}), ("wait", None, "at our cap, letting her move")),
    ("inherited bid above cap: start clean", (24, 26, [25], H((30, False)), {}),
     ("walk", None, "our standing offer 25 is above our cap 24 (inherited): start clean")),
    ("learned k=0.5", (24, 26, [13], H((30, False)), {"best_k": 0.5}), ("offer", 21, "mood unknown, gap 17, step 8 (k=0.5)")),
    ("her TEXT says last offer but no final flag: bargaining continues",
     (24, 26, [13], [(30, False, "This is my last offer, take it or leave it")], {}),
     ("offer", 17, "mood near_final, gap 17, step 4 (k=0.25)")),
]


@pytest.mark.golden
@pytest.mark.parametrize("name, args, expected", NEXT_OFFER, ids=[c[0] for c in NEXT_OFFER])
def test_next_offer_is_frozen(name, args, expected):
    assert sa.next_offer(*args) == expected


@pytest.mark.golden
def test_next_offer_never_exceeds_the_cap_over_many_inputs():
    """Invariant on top of the frozen cases: for a grid of situations an offer or accept is never above the cap."""
    for cap in (10, 24, 40):
        for ours in ([], [5], [13, 16], [cap]):
            for hers in ([], H((30, False)), H((30, False), (cap + 3, False)), H((cap, True)), H((cap + 1, True))):
                act, price, _ = sa.next_offer(cap, 26, ours, hers, {})
                assert price is None or price <= cap, (cap, ours, hers, act, price)


# (name, args, expected) for the selling mirror (we ask, the dealer bids)
NEXT_OFFER_SELL = [
    ("opening ask at list", (20, 26, [], [], {}), ("offer", 26, "opening ask at his list price")),
    ("floor above list price: ask the floor", (30, 26, [], [], {}), ("offer", 30, "opening ask at his list price")),
    ("we spoke, no reply", (20, 26, [26], [], {}), ("wait", None, "no reply yet")),
    ("first reply", (20, 26, [26], H((10, False)), {}), ("offer", 20, "he is yielding, gap 16, step 6 (k=0.35)")),
    ("normal concession", (20, 26, [26, 23], H((10, False), (13, False)), {}), ("offer", 20, "he is yielding, gap 10, step 4 (k=0.35)")),
    ("bid within reach of our ask", (20, 26, [22], H((10, False), (21, False)), {}), ("accept", 21, "his bid is within reach of our ask")),
    ("final over floor", (20, 26, [26], H((22, True)), {}), ("accept", 22, "his final is over our floor")),
    ("final below floor", (20, 26, [26], H((15, True)), {}), ("walk", None, "his final 15 is below our floor 20")),
    ("at floor and he is stalled", (20, 26, [26, 20], H((10, False), (10, False)), {}), ("walk", None, "at our floor 20, he bids 10")),
    ("at floor, let him move", (20, 26, [20], H((10, False), (12, False)), {}), ("wait", None, "at our floor, letting him move")),
    ("he is firm 3 rounds: concede faster", (20, 26, [26, 24, 22], H((10, False), (10, False), (10, False)), {}),
     ("offer", 20, "he is firm, gap 12, step 6 (k=0.5)")),
    ("he spoke first with a low bid: we state our ask", (20, 26, [], H((12, False)), {}), ("offer", 26, "opening ask (he spoke first)")),
    ("he spoke first with a final under floor: we state our ask", (20, 26, [], H((12, True)), {}),
     ("offer", 26, "opening ask (he spoke first)")),
]


@pytest.mark.golden
@pytest.mark.parametrize("name, args, expected", NEXT_OFFER_SELL, ids=[c[0] for c in NEXT_OFFER_SELL])
def test_next_offer_sell_is_frozen(name, args, expected):
    assert sa.next_offer_sell(*args) == expected


@pytest.mark.golden
def test_next_offer_sell_never_goes_below_the_floor():
    for floor in (10, 20, 30):
        for ours in ([26], [26, 23], [floor]):
            for hers in (H((5, False)), H((5, False), (floor - 2, False)), H((floor - 1, True)), H((floor + 3, True))):
                act, price, _ = sa.next_offer_sell(floor, 26, ours, hers, {})
                assert price is None or act == "accept" and price >= floor or act == "offer" and price >= floor, (floor, ours, hers, act, price)


# ------------------------------------------------------------------ pack_private_value

CAT3 = {"sets": [{"id": "LAV", "released": True,
                  "cards": [{"id": f"LAV-{i:02d}", "book": 10, "rarity": "common"} for i in range(1, 6)]}],
        "packs": [{"id": "sobre_barrio", "slots": [{"common": 1.0}, {"common": 1.0}]}],
        "values": {"copy_marginals": [1.0, 0.25, 0.1]}}
OWN = lambda refs, aff=1.0: {"affinity": {"LAV": aff}, "assets": [{"kind": "card", "ref": r, "id": i, "serial": i} for i, r in enumerate(refs, 1)]}


@pytest.mark.golden
@pytest.mark.parametrize("name, me, expected", [
    ("empty collection", OWN([]), 20.0),
    ("already own every card of the set", OWN([f"LAV-{i:02d}" for i in range(1, 6)]), 5.0),
    ("own 2 of 5", OWN(["LAV-01", "LAV-02"]), 14.0),
    ("three copies of the same card", OWN(["LAV-01"] * 3), 16.4),
    ("high affinity 1.6", OWN([], 1.6), 32.0),
])
def test_pack_private_value_is_frozen(name, me, expected):
    assert sa.pack_private_value(me, CAT3) == pytest.approx(expected)


@pytest.mark.golden
def test_pack_private_value_ignores_unreleased_sets():
    unreleased = dict(CAT3, sets=[dict(CAT3["sets"][0], released=False)])
    assert sa.pack_private_value(OWN([]), unreleased) == 0.0


@pytest.mark.golden
def test_pack_private_value_mixed_rarities():
    cat = {"sets": [{"id": "LAV", "released": True, "cards": [{"id": "LAV-01", "book": 10, "rarity": "common"},
                                                               {"id": "LAV-02", "book": 30, "rarity": "uncommon"}]}],
           "packs": [{"id": "sobre_barrio", "slots": [{"common": 0.7, "uncommon": 0.3}]}],
           "values": {"copy_marginals": [1.0, 0.25, 0.1]}}
    assert sa.pack_private_value(OWN([]), cat) == pytest.approx(16.0)


# ------------------------------------------------------------------ infer / opening_bid / bucket

def rnd(ask, our, om, hm, gap):
    return {"her_ask": ask, "final": False, "our": our, "our_move": om, "her_move": hm, "gap_before": gap}


def neg(paid, rounds, **kw):
    return dict({"dealer": "abuela", "outcome": "deal", "paid": paid, "rounds": rounds}, **kw)


R_SMALL, R_MID, R_LARGE = [rnd(30, 13, 1, 1, 15)] * 6, [rnd(28, 16, 4, 1, 16)] * 6, [rnd(25, 20, 8, 3, 16)] * 6


@pytest.mark.golden
def test_infer_no_data():
    assert sa.infer({"observed": {"negotiations": []}}) == {"response_ratio": {}, "best_k": None, "paid_median": None, "paid_n": 0}


@pytest.mark.golden
def test_infer_two_buckets_measured_gives_best_k():
    m = {"observed": {"negotiations": [neg(20, R_SMALL + R_MID)]}}
    assert sa.infer(m) == {"response_ratio": {"small": {"mean": 1.0, "n": 6}, "mid": {"mean": 0.25, "n": 6}},
                           "best_k": 0.1, "paid_median": None, "paid_n": 1}


@pytest.mark.golden
def test_infer_three_deals_and_three_buckets():
    m = {"observed": {"negotiations": [neg(20, R_SMALL), neg(22, R_MID), neg(24, R_LARGE)]}}
    assert sa.infer(m) == {"response_ratio": {"small": {"mean": 1.0, "n": 6}, "mid": {"mean": 0.25, "n": 6},
                                              "large": {"mean": 0.375, "n": 6}}, "best_k": 0.1, "paid_median": 22, "paid_n": 3}


@pytest.mark.golden
def test_infer_one_bucket_is_not_enough_for_best_k_and_failed_negotiations_do_not_count_as_paid():
    m = {"observed": {"negotiations": [neg(None, R_SMALL, outcome="closed"), neg(None, [], outcome="walked")]}}
    assert sa.infer(m) == {"response_ratio": {"small": {"mean": 1.0, "n": 6}}, "best_k": None, "paid_median": None, "paid_n": 0}


@pytest.mark.golden
def test_infer_is_per_dealer():
    m = {"observed": {"negotiations": [neg(21, []), neg(23, []), neg(25, []),
                                       {"dealer": "chato", "outcome": "deal", "paid": None, "received": 99, "rounds": []}]}}
    assert sa.infer(m)["paid_median"] == 23 and sa.infer(m)["paid_n"] == 3
    assert sa.infer(m, "chato") == {"response_ratio": {}, "best_k": None, "paid_median": None, "paid_n": 1}


@pytest.mark.golden
def test_opening_bid_and_bucket():
    assert (sa.opening_bid(26, {}), sa.opening_bid(26, {"paid_median": 21}), sa.opening_bid(26, {"paid_median": 1})) == (13, 17, 1)
    assert (sa.bucket(1, 15), sa.bucket(4, 16), sa.bucket(8, 16), sa.bucket(1, 0)) == ("small", "mid", "large", "small")
