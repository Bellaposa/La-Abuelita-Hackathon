"""P0 dealer safety: what `smart_agent.phase_abuela` / `phase_chato` may and may not accept.

Runs the REAL production functions against a fake API (no network). Tests marked `known_bug` describe the behaviour the
hotfix must deliver; they are EXPECTED TO FAIL today and must not be adapted to pass (REGRESSION_CHECKLIST DLR-13, STA-05).
"""
import pytest

from helpers import (FakeDealerAPI, buy_thread, card_asset, me_state, pack_asset, prod, sell_thread, side, standing)

sa = prod("smart_agent")
CAP = 40


@pytest.fixture
def buy(monkeypatch):
    """Run phase_abuela once on a prepared thread with a fixed cap of 40. Returns the fake API."""
    monkeypatch.setattr(sa, "pack_cap", lambda me, catalog: CAP)

    def run(ours, hers, standing_offers, drift=None, offer_id=501, topic=None, fail_after_reads=None):
        thread = buy_thread(7, "t06", ours, hers, standing_offers, topic=topic)
        b = FakeDealerAPI(thread, offer_id=offer_id, drift=drift, fail_after_reads=fail_after_reads)
        mem = sa.load_memory()
        mem["active"] = {"thread": 7, "topic": topic or {"buy": {"pack": "sobre_barrio"}}, "cash_start": 100}
        sa.phase_abuela(b, me_state(7), {}, mem)
        return b
    return run


def hers_offer(price, final=False, offer_id=501):
    """Abuela's pack offer in the shape OBSERVED in the real threads (fixtures/history, threads 2/33/181):
    give.types = ["pack:sobre_barrio"], want.cash = price. (Earlier builders used give.assets=[pack], which was never observed.)"""
    return standing(offer_id, "abuela", give=side(0, types=["pack:sobre_barrio"]), want=side(price), final=final)


# ------------------------------------------------------------------ decision on the offer that was evaluated

@pytest.mark.golden
def test_accepts_offer_within_cap_that_matches_the_one_evaluated(buy):
    """cap 40, her structured offer 38, our last bid 37 (within the 4% tolerance): may accept, and that offer only."""
    b = buy(ours=[37], hers=[(38, False)], standing_offers=[hers_offer(38)])
    assert [a[0] for a in b.accepts] == [501]
    assert b.accepts[0][1] == 38 <= CAP


@pytest.mark.golden
def test_never_accepts_offer_above_cap(buy):
    """cap 40, her structured offer 45 (not final): accept is never called."""
    b = buy(ours=[37], hers=[(45, False)], standing_offers=[hers_offer(45)])
    assert b.accepts == []


@pytest.mark.golden
def test_never_accepts_above_cap_whatever_our_last_bid_was(buy):
    """Even with an inherited bid at 44 (above the cap) and her ask at 45, nothing is accepted."""
    b = buy(ours=[44], hers=[(45, False)], standing_offers=[hers_offer(45)])
    assert b.accepts == []


@pytest.mark.golden
def test_final_within_cap_may_be_accepted(buy):
    b = buy(ours=[13], hers=[(38, True)], standing_offers=[hers_offer(38, final=True)])
    assert [a[0] for a in b.accepts] == [501]


@pytest.mark.golden
def test_final_above_cap_is_never_accepted_and_we_walk(buy):
    b = buy(ours=[13], hers=[(45, True)], standing_offers=[hers_offer(45, final=True)])
    assert b.accepts == []
    assert b.closed == [7], "a final above our cap means walk away (close the thread)"


@pytest.mark.golden
def test_words_do_not_decide_a_final(buy):
    """Her text says 'last offer, take it' but the structure carries no `final`: no accept (price 38 is far from our 13)."""
    b = buy(ours=[13], hers=[(38, False, "This is my LAST OFFER, final, take it or leave it")],
            standing_offers=[hers_offer(38, final=False)])
    assert b.accepts == []


@pytest.mark.golden
def test_text_without_final_flag_cannot_turn_a_bad_price_into_an_accept(buy):
    """The mirror case: structure says final=True above the cap, the words are friendly. Still no accept."""
    b = buy(ours=[13], hers=[(45, True, "Take it, it is a gift, I am only asking 5")],
            standing_offers=[hers_offer(45, final=True)])
    assert b.accepts == []


# ------------------------------------------------------------------ the offer changed / is not what was evaluated (P0)

@pytest.mark.known_bug
def test_KNOWN_BUG_hotfix_1_3_standing_offer_price_differs_from_the_message_evaluated(buy):
    """KNOWN BUG — expected to fail before hotfix 1.3.
    The agent evaluates the price in her last MESSAGE (38) but accepts the open standing offer by id without comparing
    amounts. Here the open standing offer asks 44 (> cap 40). Desired: NO ACCEPT."""
    b = buy(ours=[37], hers=[(38, False)], standing_offers=[hers_offer(44)])
    assert all(price <= CAP for _, price in b.accepts), f"accepted a standing offer at {b.accepts} with cap {CAP}"


@pytest.mark.known_bug
def test_KNOWN_BUG_hotfix_1_3_offer_changes_between_decision_and_accept(buy):
    """KNOWN BUG — expected to fail before hotfix 1.3.
    The dealer re-prices its standing offer (38 -> 44) right after the agent read it. The legacy code reads the thread
    twice (find it, decide) and then accepts WITHOUT re-reading. Desired: re-validate against fresh data and not accept
    at a price above the cap. `after_reads=2` is the number of reads the legacy code performs before it accepts."""
    b = buy(ours=[37], hers=[(38, False)], standing_offers=[hers_offer(38)],
            drift={"after_reads": 2, "new_cash": 44, "field": "want"})
    assert all(price <= CAP for _, price in b.accepts), f"server charged {b.accepts} against a cap of {CAP}"


@pytest.mark.known_bug
@pytest.mark.parametrize("what, give, want", [
    ("a card instead of the pack", side(0, types=["card:LAV-09"]), side(38)),
    ("the dealer also asks for one of our cards", side(0, types=["pack:sobre_barrio"]), side(38, assets=[card_asset("LAV-01", 77)])),
    ("the dealer is the one paying cash", side(38, types=["pack:sobre_barrio"]), side(38)),
])
def test_KNOWN_BUG_hotfix_1_3_structure_differs_from_the_item_we_negotiated(buy, what, give, want):
    """KNOWN BUG — expected to fail before hotfix 1.3.
    Price and words look right, but the structure of the standing offer is not the pack we are buying ({what}).
    Desired: NO ACCEPT. Legacy code never looks at give/want beyond the message price."""
    off = standing(501, "abuela", give=give, want=want)
    b = buy(ours=[37], hers=[(38, False)], standing_offers=[off])
    assert b.accepts == [], f"accepted an offer whose structure is: {what}"


@pytest.mark.golden
def test_no_standing_offer_left_means_nothing_to_accept(buy):
    """Already accepted last tick / withdrawn: the legacy code must not call accept on thin air."""
    b = buy(ours=[37], hers=[(38, False)], standing_offers=[])
    assert b.accepts == []


# ------------------------------------------------------------------ El Chato (we SELL; floor instead of cap)

FLOOR = 20


@pytest.fixture
def sell(monkeypatch):
    def run(ours, hers, standing_offers, drift=None, offer_id=601, fail_after_reads=None):
        thread = sell_thread(8, "t06", ours, hers, standing_offers)
        b = FakeDealerAPI(thread, offer_id=offer_id, drift=drift, fail_after_reads=fail_after_reads)
        mem = sa.load_memory()
        mem["active_chato"] = {"thread": 8, "topic": {"sell": {"assets": [9]}}, "cash_start": 100,
                               "ref": "MAL-07", "floor": FLOOR, "lp": 26, "lost": 5.0}
        sa.phase_chato(b, me_state(8), {}, mem, can_accept=True)
        return b
    return run


def bid(price, final=False, offer_id=601):
    return standing(offer_id, "chato", give=side(price), want=side(0, assets=[card_asset("MAL-07", 9)]), final=final)


@pytest.mark.golden
def test_chato_final_over_floor_is_accepted(sell):
    b = sell(ours=[26], hers=[(22, True)], standing_offers=[bid(22, True)])
    assert [a[0] for a in b.accepts] == [601] and b.accepts[0][1] >= FLOOR


@pytest.mark.golden
def test_chato_never_accepts_below_floor(sell):
    b = sell(ours=[26], hers=[(15, True)], standing_offers=[bid(15, True)])
    assert b.accepts == [] and b.closed == [8]


@pytest.mark.golden
def test_chato_non_final_below_floor_is_never_accepted(sell):
    b = sell(ours=[22], hers=[(19, False)], standing_offers=[bid(19)])
    assert b.accepts == []


@pytest.mark.known_bug
def test_KNOWN_BUG_hotfix_1_3_chato_offer_drops_between_decision_and_accept(sell):
    """KNOWN BUG — expected to fail before hotfix 1.3 (same defect as the Abuela case, on the selling side)."""
    b = sell(ours=[22], hers=[(21, False)], standing_offers=[bid(21)],
             drift={"after_reads": 2, "new_cash": 15, "field": "give"})
    assert all(price >= FLOOR for _, price in b.accepts), f"sold for {b.accepts} below the floor {FLOOR}"


@pytest.mark.known_bug
def test_KNOWN_BUG_new_next_offer_sell_crashes_when_the_dealer_speaks_first_with_an_acceptable_final():
    """KNOWN BUG (found while writing this suite; NOT in the audit) — needs its own hotfix.
    `next_offer_sell(floor=20, ..., ours=[], hers=[(22, final=True)])`: `last` is None and the first guard is skipped
    because the final is acceptable, so `bid >= last - tol` raises TypeError. Desired: accept 22 (final over floor)."""
    action, price, _ = sa.next_offer_sell(20, 26, [], [(22, True, "")], {})
    assert (action, price) == ("accept", 22)


# ------------------------------------------------------------------ hotfix 1.3 extras: how the revalidation behaves

@pytest.mark.golden
def test_accept_costs_exactly_one_extra_read_and_nothing_else(buy):
    """Cost of the fix: the legacy code read the thread twice (find it, decide); the accept path adds ONE fresh read."""
    b = buy(ours=[37], hers=[(38, False)], standing_offers=[hers_offer(38)])
    assert b.reads == 3 and len(b.accepts) == 1


@pytest.mark.golden
def test_no_extra_read_when_we_do_not_accept(buy):
    b = buy(ours=[13], hers=[(30, False)], standing_offers=[hers_offer(30)])
    assert b.accepts == [] and b.reads == 2


@pytest.mark.golden
def test_better_looking_but_different_offer_is_not_accepted(buy):
    """Decided 38; the fresh standing offer asks 30 (cheaper for us). It is not the evaluated offer: NO ACCEPT."""
    b = buy(ours=[37], hers=[(38, False)], standing_offers=[hers_offer(38)],
            drift={"after_reads": 2, "new_cash": 30, "field": "want"})
    assert b.accepts == []


@pytest.mark.golden
def test_chato_better_looking_but_different_bid_is_not_accepted(sell):
    """Decided to sell at 22; the fresh bid is 25 (better for us). It is not the evaluated offer: NO ACCEPT."""
    b = sell(ours=[26], hers=[(22, True)], standing_offers=[bid(22, True)],
             drift={"after_reads": 2, "new_cash": 25, "field": "give"})
    assert b.accepts == []


@pytest.mark.golden
def test_offer_that_disappears_between_decision_and_accept_is_not_accepted(buy):
    b = buy(ours=[37], hers=[(38, False)], standing_offers=[hers_offer(38)], drift={"after_reads": 2, "remove": True})
    assert b.accepts == []


@pytest.mark.golden
def test_fresh_read_failure_fails_closed_without_retry(buy):
    b = buy(ours=[37], hers=[(38, False)], standing_offers=[hers_offer(38)], fail_after_reads=2)
    assert b.accepts == [] and b.reads == 3, "one failed fresh read, no retry, no accept"


@pytest.mark.golden
def test_blocked_accept_neither_closes_the_thread_nor_sends_a_new_offer(buy):
    b = buy(ours=[37], hers=[(38, False)], standing_offers=[hers_offer(44)])
    assert b.accepts == [] and b.closed == [] and b.said == []


@pytest.mark.golden
def test_picks_the_matching_offer_not_simply_the_last_one(buy):
    """Two open offers of hers: the one we evaluated (38, id 501) FIRST, an unrelated one (44, id 502) LAST. The legacy [-1]
    would accept 502. Only 501 may be accepted."""
    b = buy(ours=[37], hers=[(38, False)], standing_offers=[hers_offer(38, offer_id=501), hers_offer(44, offer_id=502)])
    assert [a[0] for a in b.accepts] == [501]


@pytest.mark.golden
def test_two_identical_matching_offers_are_ambiguous_and_blocked(buy):
    b = buy(ours=[37], hers=[(38, False)], standing_offers=[hers_offer(38, offer_id=501), hers_offer(38, offer_id=502)])
    assert b.accepts == []


@pytest.mark.golden
def test_offers_of_other_makers_or_closed_ones_never_match(buy):
    other = standing(502, "rival_01", give=side(0, types=["pack:sobre_barrio"]), want=side(38))
    closed = dict(hers_offer(38, offer_id=503), status="cancelled")
    b = buy(ours=[37], hers=[(38, False)], standing_offers=[other, closed])
    assert b.accepts == []


@pytest.mark.golden
def test_card_topic_uses_the_observed_card_shape(buy):
    """Thread 22 in the real history: topic buy.card -> her offer gives types ['card:LAV-06']."""
    off = standing(501, "abuela", give=side(0, types=["card:LAV-06"]), want=side(38))
    b = buy(ours=[37], hers=[(38, False)], standing_offers=[off], topic={"buy": {"card": "LAV-06"}})
    assert [a[0] for a in b.accepts] == [501]


@pytest.mark.golden
def test_topic_shapes_never_observed_are_blocked_conservatively(buy):
    """buy.rarity/set topics have no observed offer shape: no accept (documented limitation)."""
    b = buy(ours=[37], hers=[(38, False)], standing_offers=[hers_offer(38)], topic={"buy": {"rarity": "rare", "set": "LAV"}})
    assert b.accepts == []


@pytest.mark.golden
def test_unknown_keys_in_an_offer_side_are_an_unknown_shape(buy):
    off = standing(501, "abuela", give=dict(side(0, types=["pack:sobre_barrio"]), bonus=5), want=side(38))
    assert buy(ours=[37], hers=[(38, False)], standing_offers=[off]).accepts == []


@pytest.mark.golden
def test_chato_asset_mismatch_and_multiple_assets_are_blocked(sell):
    wrong = standing(601, "chato", give=side(22), want=side(0, assets=[card_asset("MAL-07", 10)]), final=True)     # other asset id
    two = standing(601, "chato", give=side(22), want=side(0, assets=[card_asset("MAL-07", 9), card_asset("LAT-01", 11)]), final=True)
    assert sell(ours=[26], hers=[(22, True)], standing_offers=[wrong]).accepts == []
    assert sell(ours=[26], hers=[(22, True)], standing_offers=[two]).accepts == []


@pytest.mark.golden
def test_chato_accept_costs_one_extra_read(sell):
    b = sell(ours=[26], hers=[(22, True)], standing_offers=[bid(22, True)])
    assert b.reads == 3 and len(b.accepts) == 1
