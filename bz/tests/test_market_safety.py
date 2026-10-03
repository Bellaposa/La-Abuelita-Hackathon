"""Market safety: never spend what we do not have, never accept a negative trade, never accept twice in a tick.

Real production functions (`smart_agent.phase_market`, `smart_agent.market_opportunities`, `page_hunter.step`,
`page_hunter.buy_choice`) against a fake El Rastro. The cross-process test is the known P0 bug (hotfix 1.4).
"""
import pytest

from helpers import (FakeMarketServer, bid_offer, me_with_cards, prod, sell_offer, side, toy_catalog)

sa, ph = prod("smart_agent"), prod("page_hunter")
VENUE = {"fee_bps": 500, "fee_per_card": 1}


def run_market(offers, values, cash=500, can_accept=True, cards=()):
    server = FakeMarketServer(offers, cash=cash, values=values)
    me = me_with_cards(cards, cash=cash)
    sa.phase_market(server.client(), me, toy_catalog(), can_accept=can_accept)
    return server


# ------------------------------------------------------------------ spending and value

@pytest.mark.golden
def test_buys_a_card_worth_more_to_us_than_it_costs():
    s = run_market([sell_offer(10, "LAV-09", 50)], {"LAV-09": 112.0})
    assert s.accept_attempts == [10]


@pytest.mark.golden
def test_never_buys_at_negative_value():
    """Price 120 + fee 7 for a card worth 112 to us: gain is negative -> no accept."""
    s = run_market([sell_offer(10, "LAV-09", 120)], {"LAV-09": 112.0})
    assert s.accept_attempts == []


@pytest.mark.golden
def test_never_buys_when_price_plus_fee_exceeds_our_cash():
    s = run_market([sell_offer(10, "LAV-09", 50)], {"LAV-09": 112.0}, cash=40)
    assert s.accept_attempts == []


@pytest.mark.golden
def test_unknown_value_means_no_buy():
    """b.value() gave nothing (None): the agent does not guess."""
    s = run_market([sell_offer(10, "LAV-09", 50)], {})
    assert s.accept_attempts == []


@pytest.mark.golden
def test_only_one_accept_per_tick_inside_the_process():
    """Two profitable offers in the same tick: exactly one accept attempt (the team has one per tick)."""
    s = run_market([sell_offer(10, "LAV-09", 50), sell_offer(11, "LAV-10", 52)], {"LAV-09": 112.0, "LAV-10": 112.0})
    assert len(s.accept_attempts) == 1


@pytest.mark.golden
def test_can_accept_false_means_no_accept_at_all():
    """When the caller already used this tick's accept (Abuela/Chato), the market phase must not accept."""
    s = run_market([sell_offer(10, "LAV-09", 50)], {"LAV-09": 112.0}, can_accept=False)
    assert s.accept_attempts == []


@pytest.mark.golden
def test_never_sells_the_only_copy_to_a_bid():
    s = run_market([bid_offer(12, "LAT-04", 80)], {}, cards=["LAT-04"])
    assert s.accept_attempts == []


@pytest.mark.golden
def test_sells_a_duplicate_to_a_profitable_bid():
    s = run_market([bid_offer(12, "LAT-04", 30)], {}, cards=["LAT-04", "LAT-04"])
    assert s.accept_attempts == [12]


@pytest.mark.golden
def test_ignores_our_own_directed_and_closed_offers():
    offers = [dict(sell_offer(10, "LAV-09", 50), maker="t06"),
              dict(sell_offer(11, "LAV-09", 50), to="rival_02"),
              dict(sell_offer(12, "LAV-09", 50), status="closed")]
    s = run_market(offers, {"LAV-09": 112.0})
    assert s.accept_attempts == []


@pytest.mark.golden
def test_ignores_offers_that_are_not_a_plain_card_for_cash():
    """Card for card, cash plus asset, and empty offers are not accepted by phase_market."""
    swap = {"id": 20, "maker": "rival_01", "to": None, "status": "open",
            "give": side(0, assets=[{"kind": "card", "ref": "LAV-09", "id": 20}]), "want": side(0, assets=[{"kind": "card", "ref": "LAT-01", "id": 99}])}
    mixed = {"id": 21, "maker": "rival_01", "to": None, "status": "open",
             "give": side(10, assets=[{"kind": "card", "ref": "LAV-09", "id": 21}]), "want": side(50)}
    s = run_market([swap, mixed], {"LAV-09": 112.0})
    assert s.accept_attempts == []


@pytest.mark.golden
def test_market_opportunities_respects_free_cash_and_cash_reserve(monkeypatch):
    """Price 90 + fee 5% (5) + 1 = 96. With 100 in cash it is an opportunity; with 90, or with a 20 reserve, it is not."""
    cat, offers = toy_catalog(), [sell_offer(10, "LAV-09", 90)]
    value = lambda ref, n: 112.0
    assert [o["offer"] for o in sa.market_opportunities(me_with_cards([], cash=100), cat, VENUE, offers, value)] == [10]
    assert sa.market_opportunities(me_with_cards([], cash=90), cat, VENUE, offers, value) == []
    monkeypatch.setattr(sa, "CASH_RESERVE", 20)
    assert sa.market_opportunities(me_with_cards([], cash=100), cat, VENUE, offers, value) == []


# ------------------------------------------------------------------ page_hunter

def hunter_step(offers, values=None, cash=500):
    server = FakeMarketServer(offers, cash=cash, values=values)
    me = me_with_cards([f"LAV-{i:02d}" for i in range(1, 9)], cash=cash)
    ph.step(server.client(), me, toy_catalog(), {"fee_bps": 500, "fee_per_card": 1})
    return server


@pytest.mark.golden
def test_page_hunter_buys_a_missing_page_card_within_its_cap():
    s = hunter_step([sell_offer(1, "LAV-09", 110)])
    assert s.accept_attempts == [1]


@pytest.mark.golden
def test_page_hunter_never_pays_over_its_cap_or_over_cash():
    assert hunter_step([sell_offer(2, "LAV-10", 200)]).accept_attempts == []            # over the cap
    assert hunter_step([sell_offer(1, "LAV-09", 110)], cash=40).accept_attempts == []   # not enough cash


@pytest.mark.golden
def test_page_hunter_ignores_zero_gain_buys():
    """Cost exactly at the cap is noise: gain must be >= max(2, 10% of cost)."""
    targets = ph.page_targets(toy_catalog(), {"LAV": 1.6, "LAT": 0.7}, {f"LAV-{i:02d}" for i in range(1, 9)})
    cap = {t[0]: t[2] for t in targets}["LAV-09"]
    price = int(cap) - ph.fee_of(VENUE, int(cap))
    s = hunter_step([sell_offer(3, "LAV-09", price)])
    assert s.accept_attempts == []


# ------------------------------------------------------------------ P0: two processes, one tick

@pytest.mark.known_bug
def test_KNOWN_BUG_hotfix_1_4_two_processes_both_try_to_accept_in_the_same_tick():
    """KNOWN BUG — expected to fail before hotfix 1.4 (accept gate shared between processes).
    smart_agent.phase_market and page_hunter.step are separate processes that share one team key and therefore ONE accept
    per tick. Each only protects the limit inside its own process. Desired: at most ONE accept attempt reaches the API
    per tick across both. Today both send one (the server refuses the second with wait_for_tick, but the race decides which
    one wins, and it may be the worse offer)."""
    offers = [sell_offer(1, "LAV-09", 70)]                        # both agents find this one profitable (cost 77 < value 112)
    server = FakeMarketServer(offers, cash=500, values={"LAV-09": 112.0})
    client = server.client()
    me = me_with_cards([f"LAV-{i:02d}" for i in range(1, 9)], cash=500)
    sa.phase_market(client, me, toy_catalog(), can_accept=True)                 # process A
    ph.step(client, me, toy_catalog(), {"fee_bps": 500, "fee_per_card": 1})     # process B, same tick, same server
    assert len(server.accept_attempts) <= 1, f"{len(server.accept_attempts)} accept attempts in one tick: {server.accept_attempts}"
