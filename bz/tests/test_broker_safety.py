"""Broker: pairs that cannot trade are never crossed, prices stay between ask and bid, every offer is used once.

Covers broker.bench_plan (the Market Test), starter_broker.public_plan (the venue's real offers) and the decision rules in
broker.decide. Pure functions, random books with a fixed seed, plus the real fixture book from phase 0.
"""
import json
import math
import os
import random

import pytest

from helpers import FIXTURES, prod

broker, starter = prod("broker"), prod("starter_broker")


def bench_book(rng, n=10, run="b1"):
    offers = []
    for i in range(n):
        offers.append({"id": f"{run}-{i}", "want": {"cash": rng.randint(20, 70)}, "give": {"cash": 0}, "expires_tick": 16})
    for i in range(n, 2 * n):
        offers.append({"id": f"{run}-{i}", "want": {"cash": 0}, "give": {"cash": rng.randint(30, 90)}, "expires_tick": 16})
    return {"bench_offers": offers}


def quotes(book):
    return {o["id"]: (o["want"]["cash"] or o["give"]["cash"]) for o in book["bench_offers"]}


def sells(book):
    return {o["id"] for o in book["bench_offers"] if o["want"]["cash"]}


@pytest.mark.golden
def test_bench_plan_never_crosses_impossible_pairs_and_prices_inside_the_spread():
    rng = random.Random(7)
    for _ in range(100):
        book = bench_book(rng)
        q, s = quotes(book), sells(book)
        for plan in (broker.bench_plan(book, 3, broker.Track(), quiet=True), broker.bench_plan_naive(book), starter.bench_plan(book)):
            for sell, buy, price in plan:
                assert sell in s and buy not in s, "a sell id must be an ask and a buy id a bid"
                assert q[sell] <= price <= q[buy], (sell, buy, price, q[sell], q[buy])
                assert q[buy] >= q[sell], "pair with bid < ask was crossed"


@pytest.mark.golden
def test_bench_plan_uses_each_offer_at_most_once():
    rng = random.Random(11)
    for _ in range(100):
        plan = broker.bench_plan(bench_book(rng), 3, broker.Track(), quiet=True)
        ids = [i for sell, buy, _ in plan for i in (sell, buy)]
        assert len(ids) == len(set(ids))


@pytest.mark.golden
def test_bench_plan_pairs_stay_inside_their_own_run():
    """Offers of different runs ('b1-..' vs 'b2-..') are separate sessions and must never be paired together."""
    rng = random.Random(3)
    book = {"bench_offers": bench_book(rng, run="b1")["bench_offers"] + bench_book(rng, run="b2")["bench_offers"]}
    for sell, buy, _ in broker.bench_plan(book, 3, broker.Track(), quiet=True):
        assert sell.split("-")[0] == buy.split("-")[0]


@pytest.mark.golden
def test_bench_plan_is_reproducible():
    book = bench_book(random.Random(5))
    a = broker.bench_plan(book, 3, broker.Track(), quiet=True)
    b = broker.bench_plan(book, 3, broker.Track(), quiet=True)
    assert a == b


@pytest.mark.golden
def test_empty_book_and_the_real_phase_0_book_give_an_empty_plan():
    assert broker.bench_plan({}, 1, broker.Track(), quiet=True) == []
    real = json.load(open(os.path.join(FIXTURES, "broker_book.json"), encoding="utf-8"))
    assert real["offers"] == [] and real["bench_offers"] == []
    assert broker.bench_plan(real, 346, broker.Track(), quiet=True) == [] and starter.public_plan(
        dict(real, fee_bps=0, fee_per_card=0)) == []


# ------------------------------------------------------------------ the decision rules (frozen from the selftest)

def two(tr, t, aq, bq):
    offers = [{"id": "b1-1", "want": {"cash": aq}, "give": {"cash": 0}, "expires_tick": 16},
              {"id": "b1-2", "want": {"cash": 0}, "give": {"cash": bq}, "expires_tick": 16}]
    tr.update(t, offers)
    return offers


@pytest.mark.golden
def test_decide_rules_are_frozen(monkeypatch):
    monkeypatch.setattr(broker, "WAIT_ENABLED", True)
    tr = broker.Track()
    s, b = two(tr, 0, 50, 55)
    assert broker.decide(tr, s, b, 0, 15, 16)[0] == "MATCH"            # first sight: do not wait on a guess
    two(tr, 1, 48, 56)
    assert broker.decide(tr, s, b, 1, 14, 16)[0] == "WAIT"             # both observed relaxing
    two(tr, 2, 48, 56); two(tr, 3, 48, 56)
    assert broker.decide(tr, s, b, 3, 12, 16)[0] == "MATCH"            # both flat for 2 observations
    assert broker.decide(tr, s, b, 3, 2, 16)[0] == "MATCH"             # session about to end


@pytest.mark.golden
def test_broker_wait_switch_off_always_matches(monkeypatch):
    monkeypatch.setattr(broker, "WAIT_ENABLED", False)
    tr = broker.Track()
    s, b = two(tr, 0, 50, 55)
    two(tr, 1, 48, 56)
    assert broker.decide(tr, s, b, 1, 14, 16)[0] == "MATCH"


@pytest.mark.golden
def test_synthetic_efficiency_baseline_is_reproducible():
    """BRK-11: 300 fixed-seed synthetic sessions (made-up dynamics). Frozen from the phase-0 selftest output."""
    seeds = range(300)
    imm = sum(broker._simulate(lambda book, t, tr: broker.bench_plan_naive(book), s) for s in seeds) / len(seeds)
    smart = sum(broker._simulate(lambda book, t, tr: broker.bench_plan(book, t, tr, 16, quiet=True), s) for s in seeds) / len(seeds)
    assert (round(imm, 3), round(smart, 3)) == (0.902, 0.871)


# ------------------------------------------------------------------ public offers (card by card)

def pub(offers, fee_bps=500, per_card=1):
    return {"fee_bps": fee_bps, "fee_per_card": per_card, "offers": offers}


def ask(i, ref, cash, maker="rival_01"):
    return {"id": i, "maker": maker, "give": {"cash": 0, "assets": [{"kind": "card", "ref": ref, "id": i}], "types": []},
            "want": {"cash": cash, "assets": [], "types": []}}


def bid(i, ref, cash, maker="rival_02"):
    return {"id": i, "maker": maker, "give": {"cash": cash, "assets": [], "types": []},
            "want": {"cash": 0, "assets": [], "types": [f"card:{ref}"]}}


@pytest.mark.golden
def test_public_plan_buyer_can_always_pay_price_plus_fee():
    rng = random.Random(1)
    for _ in range(100):
        offers = [ask(i, "LAV-01", rng.randint(10, 60)) for i in range(5)] + [bid(100 + i, "LAV-01", rng.randint(10, 90)) for i in range(5)]
        book = pub(offers)
        for sell, buy, price in starter.public_plan(book):
            a = next(o for o in offers if o["id"] == sell)["want"]["cash"]
            bd = next(o for o in offers if o["id"] == buy)["give"]["cash"]
            fee = math.ceil(500 * price / 10000) + 1
            assert a <= price and price + fee <= bd, (a, price, fee, bd)


@pytest.mark.golden
def test_public_plan_never_matches_a_maker_with_itself_or_different_cards():
    book = pub([ask(1, "LAV-01", 20, maker="rival_01"), bid(2, "LAV-01", 80, maker="rival_01"), bid(3, "LAV-02", 80)])
    assert starter.public_plan(book) == []


@pytest.mark.golden
def test_public_plan_is_capped_at_ten_matches():
    offers = [ask(i, "LAV-01", 10) for i in range(15)] + [bid(100 + i, "LAV-01", 90) for i in range(15)]
    assert len(starter.public_plan(pub(offers))) <= 10
