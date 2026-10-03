"""Hotfix 1.4 wired into the production scripts: every accept site asks the shared gate first.

`other_process_took(tick)` plays the part of ANOTHER process (smart_agent / page_hunter) that already reserved this tick's
accept. The production functions must then not send their accept, must not close the thread or bargain instead, and must not
burn the slot when they decide NOT to accept.
"""
import os

import pytest

from bz.core.accept_gate import AcceptGate
from helpers import (FakeDealerAPI, FakeMarketServer, buy_thread, card_asset, me_state, me_with_cards, prod, sell_offer,
                     sell_thread, side, standing, toy_catalog)

sa, ph = prod("smart_agent"), prod("page_hunter")
TICK = 100


def gate():
    return AcceptGate(os.environ["ACCEPT_GATE_DIR"])


def other_process_took(tick):
    assert gate().reserve(tick) is True


# ------------------------------------------------------------------ market (smart_agent.phase_market)

def market(tick=TICK, can_accept=True):
    server = FakeMarketServer([sell_offer(10, "LAV-09", 70)], cash=500, values={"LAV-09": 112.0})
    me = dict(me_with_cards([]), tick=tick)
    sa.phase_market(server.client(), me, toy_catalog(), can_accept=can_accept)
    return server


@pytest.mark.golden
def test_market_does_not_accept_when_another_process_already_used_the_tick():
    other_process_took(TICK)
    assert market().accept_attempts == []


@pytest.mark.golden
def test_market_accepts_on_the_next_tick():
    other_process_took(TICK)
    assert market(tick=TICK + 1).accept_attempts == [10]


@pytest.mark.golden
def test_market_reserves_the_tick_when_it_accepts():
    assert market().accept_attempts == [10]
    assert gate().reserve(TICK) is False, "the market took this tick's accept: nobody else may"


@pytest.mark.golden
def test_market_does_not_burn_the_slot_when_it_decides_not_to_accept():
    server = FakeMarketServer([sell_offer(10, "LAV-09", 120)], values={"LAV-09": 112.0})     # negative value: no accept
    sa.phase_market(server.client(), dict(me_with_cards([]), tick=TICK), toy_catalog(), can_accept=True)
    assert server.accept_attempts == [] and gate().reserve(TICK) is True


# ------------------------------------------------------------------ page_hunter

def hunter(offers, tick=TICK):
    server = FakeMarketServer(offers, cash=500)
    me = dict(me_with_cards([f"LAV-{i:02d}" for i in range(1, 9)]), tick=tick)
    ph.step(server.client(), me, toy_catalog(), {"fee_bps": 500, "fee_per_card": 1})
    return server


@pytest.mark.golden
def test_page_hunter_buy_is_blocked_when_another_process_used_the_tick():
    other_process_took(TICK)
    assert hunter([sell_offer(1, "LAV-09", 70)]).accept_attempts == []


@pytest.mark.golden
def test_page_hunter_buy_reserves_the_tick_and_works_next_tick():
    assert hunter([sell_offer(1, "LAV-09", 70)]).accept_attempts == [1]
    assert gate().reserve(TICK) is False
    assert hunter([sell_offer(1, "LAV-09", 70)], tick=TICK + 1).accept_attempts == [1]


@pytest.mark.golden
def test_page_hunter_sell_to_bid_is_blocked_when_another_process_used_the_tick():
    from helpers import bid_offer
    other_process_took(TICK)
    server = FakeMarketServer([bid_offer(5, "LAT-10", 90)], cash=500)
    me = dict(me_with_cards([f"LAV-{i:02d}" for i in range(1, 9)] + ["LAT-10"]), tick=TICK)
    ph.step(server.client(), me, toy_catalog(), {"fee_bps": 500, "fee_per_card": 1})
    assert server.accept_attempts == []


@pytest.mark.golden
def test_the_two_production_agents_together_send_at_most_one_accept_per_tick():
    """The scenario of the known-bug test, here as a green invariant over several ticks."""
    offers = [sell_offer(1, "LAV-09", 70)]
    server = FakeMarketServer(offers, cash=500, values={"LAV-09": 112.0})
    client = server.client()
    for tick in (100, 101, 102):
        server.accepted_this_tick = False
        me = dict(me_with_cards([f"LAV-{i:02d}" for i in range(1, 9)]), tick=tick)
        before = len(server.accept_attempts)
        sa.phase_market(client, me, toy_catalog(), can_accept=True)
        ph.step(client, me, toy_catalog(), {"fee_bps": 500, "fee_per_card": 1})
        assert len(server.accept_attempts) - before == 1, f"tick {tick}"


# ------------------------------------------------------------------ dealers (smart_agent.phase_abuela / phase_chato)

def abuela_offer(price=38):
    return standing(501, "abuela", give=side(0, types=["pack:sobre_barrio"]), want=side(price))


def run_abuela(tick=10, drift=None, ours=(37,), hers=((38, False),), price=38, monkeypatch=None):
    thread = buy_thread(7, "t06", list(ours), list(hers), [abuela_offer(price)])
    b = FakeDealerAPI(thread, offer_id=501, drift=drift)
    mem = sa.load_memory()
    mem["active"] = {"thread": 7, "topic": {"buy": {"pack": "sobre_barrio"}}, "cash_start": 100}
    result = sa.phase_abuela(b, me_state(7, tick=tick), {}, mem)
    return b, result


@pytest.fixture
def cap40(monkeypatch):
    monkeypatch.setattr(sa, "pack_cap", lambda me, catalog: 40)


@pytest.mark.golden
def test_abuela_accept_is_blocked_by_the_gate_without_closing_or_bargaining(cap40):
    other_process_took(10)
    b, result = run_abuela()
    assert b.accepts == [] and b.closed == [] and b.said == [] and result is False


@pytest.mark.golden
def test_abuela_accepts_and_reserves_when_the_gate_is_free(cap40):
    b, result = run_abuela()
    assert [a[0] for a in b.accepts] == [501] and result is True
    assert gate().reserve(10) is False


@pytest.mark.golden
def test_abuela_does_not_burn_the_slot_when_it_does_not_accept(cap40):
    """Above the cap: the strategy does not accept, so the gate must stay free for another process."""
    b, _ = run_abuela(ours=(37,), hers=((45, False),), price=45)
    assert b.accepts == [] and gate().reserve(10) is True


@pytest.mark.golden
def test_abuela_revalidation_failure_does_not_burn_the_slot(cap40):
    """Hotfix 1.3 blocks first (the offer changed): the gate is only asked once the accept is really going out."""
    b, _ = run_abuela(drift={"after_reads": 2, "new_cash": 44, "field": "want"})
    assert b.accepts == [] and gate().reserve(10) is True


@pytest.mark.golden
def test_chato_sell_accept_is_blocked_by_the_gate():
    thread = sell_thread(8, "t06", [26], [(22, True)],
                         [standing(601, "chato", give=side(22), want=side(0, assets=[card_asset("MAL-07", 9)]), final=True)])
    b = FakeDealerAPI(thread, offer_id=601)
    mem = sa.load_memory()
    mem["active_chato"] = {"thread": 8, "topic": {"sell": {"assets": [9]}}, "cash_start": 100, "ref": "MAL-07", "floor": 20, "lp": 26, "lost": 5.0}
    other_process_took(10)
    assert sa.phase_chato(b, me_state(8), {}, mem, can_accept=True) is False
    assert b.accepts == [] and b.closed == [] and b.said == []


# ------------------------------------------------------------------ the gate must never be a single point of failure

@pytest.mark.golden
def test_unusable_gate_fails_open_and_says_so(monkeypatch, tmp_path, capsys):
    broken = tmp_path / "not_a_directory"
    broken.write_text("x")                                      # a FILE where the gate directory should be
    monkeypatch.setenv("ACCEPT_GATE_DIR", str(broken))
    assert market().accept_attempts == [10], "the server is the final authority: do not block trading if the gate breaks"
    assert "accept gate unavailable" in capsys.readouterr().err


@pytest.mark.golden
def test_old_reservations_are_pruned():
    g = gate()
    for t in (1, 2, 3):
        assert g.reserve(t)
    assert g.reserve(1000)                                      # far in the future: ticks 1..3 are older than the window
    assert sorted(os.listdir(os.environ["ACCEPT_GATE_DIR"])) == ["tick_1000"]
