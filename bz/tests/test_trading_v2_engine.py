"""Trading V2 engine: decisions (pure `decide`) and execution (`execute` / trading_v2.step) against fakes. No network.

Scenario used throughout: we hold LAV-01..08 (one copy each, affinity 1.6) so LAV-09 and LAV-10 (rares, book 70) are the two cards
missing from the LAV page, plus two copies of LAT-04 (affinity 0.7, book 10). Hand-derived numbers:
  LAV-09/10 base value 70*1.6 = 112;  page value 424, bonus 106;  credit with 2 missing = 0.65*106*0.5 = 34.45  -> 146.45
  LAV-10 alone missing:                credit 0.65*106 = 68.9                                              -> 180.9
  second copy of LAT-04 is worth 10*0.7*0.25 = 1.75 to us.
"""
import copy
import random
from datetime import datetime, timedelta, timezone

import pytest

import trading_v2
from agents import portfolio_agent as pf
from bazaar_sdk import BazaarError
from bz.core.accept_gate import AcceptGate
from bz.trading import policy
from bz.trading.engine import V2Valuer, decide, execute
from bz.trading.intel import MarketIntel
from helpers import bid_offer, card_asset, sell_offer, side, toy_catalog

CFG = policy.cfg_with()
RASTRO = {"fee_bps": 500, "fee_per_card": 1}


class Rng:
    def __init__(self, *vals):
        self.vals = list(vals)

    def random(self):
        return self.vals.pop(0) if self.vals else 0.99


def me_with(extra=(), lav=range(1, 9), cash=300, tick=100):
    refs = [f"LAV-{i:02d}" for i in lav] + ["LAT-04", "LAT-04"] + list(extra)
    return {"id": "t06", "tick": tick, "cash": cash, "affinity": {"LAV": 1.6, "LAT": 0.7},
            "assets": [{"id": 100 + i, "kind": "card", "ref": r, "serial": i + 1, "rarity": "common"} for i, r in enumerate(refs)]}


def snap(me, offers=(), my_offers=(), clock=None, deals=(), venue="rastro", boards=None):
    catalog = toy_catalog()
    return {"me": me, "catalog": catalog, "venues": {"rastro": RASTRO}, "boards": boards if boards is not None else {venue: list(offers)},
            "my_offers": list(my_offers), "clock": clock or {}, "valuer": V2Valuer(None, me, catalog, 12), "deals": list(deals), "now": None}


def run(me, offers=(), intel=None, cfg=None, rng=None, **kw):
    intel = intel or MarketIntel()
    s = snap(me, offers, **kw)
    return decide(s, cfg or CFG, intel, rng or Rng())


# ------------------------------------------------------------------ buying

def test_buys_a_page_card_when_value_plus_page_credit_beats_the_cost():
    plan = run(me_with(), [sell_offer(10, "LAV-09", 100)])
    acc = plan["accept"]
    assert acc["kind"] == "buy" and acc["category"] == "page_hope" and acc["offer"] == 10
    assert acc["value"] == pytest.approx(146.4, abs=0.1) and acc["cost"] == 106 and acc["gain"] == pytest.approx(40.4, abs=0.1)


def test_does_not_buy_when_the_credit_does_not_cover_the_price():
    assert run(me_with(), [sell_offer(10, "LAV-09", 141)])["accept"] is None      # cost 149 > value 146.45


def test_the_last_card_of_a_page_needs_a_gain_of_only_one():
    me = me_with(lav=range(1, 10))                                                # only LAV-10 missing: value 180.9
    acc = run(me, [sell_offer(10, "LAV-10", 169)])["accept"]                      # cost 169 + fee 10 = 179 -> gain 1.9
    assert acc["category"] == "page_closer" and acc["gain"] == pytest.approx(1.9, abs=0.1)
    assert run(me, [sell_offer(10, "LAV-10", 170)])["accept"] is None             # cost 180 -> gain 0.9 < 1


def test_a_plain_card_needs_the_dynamic_margin_and_liquidity_lowers_it():
    me = me_with()
    offer = [sell_offer(10, "LAT-01", 3)]                                         # value 7, cost 3 + fee 2 = 5 -> gain 2
    assert run(me, offer)["accept"] is None                                       # base margin is 3
    intel = MarketIntel()
    for i, m in enumerate(("m1", "m2", "m3", "m4")):
        intel.observe(90 + i, [bid_offer(900 + i, "LAT-01", 8 + i, maker=m)])     # liquid card: margin 3 -> 2
    intel.observe(95, [])
    assert intel.liquidity("LAT-01") == 1.0
    assert run(me, offer, intel=intel)["accept"]["gain"] == pytest.approx(2.0)


def test_near_the_close_the_margin_shrinks():
    now = datetime(2026, 10, 3, 22, 55, tzinfo=timezone.utc)
    s = snap(me_with(), [sell_offer(10, "LAT-01", 3)], clock={"closes": (now + timedelta(minutes=5)).isoformat()})
    s["now"] = now
    plan = decide(s, CFG, MarketIntel(), Rng())
    assert plan["pressure"] > 0.9 and plan["accept"]["gain"] == pytest.approx(2.0)   # margin 3 -> 1 at the close


def test_never_buys_beyond_spendable_cash_and_open_bids_reduce_it():
    me = me_with(cash=100)
    assert run(me, [sell_offer(10, "LAV-09", 100)])["accept"] is None             # 106 > 100
    committed = [{"id": 1, "maker": "t06", "status": "open", "give": side(60), "want": side(0, types=["card:LAV-10"])}]
    assert run(me_with(cash=120), [sell_offer(10, "LAV-09", 100)], my_offers=committed)["accept"] is None   # 120 - 60 < 106


def test_only_one_accept_and_it_is_the_best_ranked():
    offers = [sell_offer(10, "LAV-09", 100), sell_offer(11, "LAV-10", 60)]        # gains ~40 and ~80 (cheaper, same value)
    plan = run(me_with(), offers)
    assert plan["candidates"] == 2 and plan["accept"]["offer"] == 11


@pytest.mark.parametrize("what, offer", [
    ("our own offer", dict(sell_offer(10, "LAV-09", 50), maker="t06")),
    ("addressed to another team", dict(sell_offer(10, "LAV-09", 50), to="t99")),
    ("a closed offer", dict(sell_offer(10, "LAV-09", 50), status="cancelled")),
    ("a dealer thread offer", dict(sell_offer(10, "LAV-09", 50), thread=7)),
    ("a pack", {"id": 10, "maker": "m1", "to": None, "status": "open", "give": side(0, assets=[{"kind": "pack", "ref": "sobre_plata", "id": 5}]), "want": side(128)}),
    ("an unknown key on a side", dict(sell_offer(10, "LAV-09", 50), give=dict(sell_offer(10, "LAV-09", 50)["give"], gift=9))),
    ("a bundle of two cards", dict(sell_offer(10, "LAV-09", 50), give=side(0, assets=[card_asset("LAV-09", 1), card_asset("LAV-10", 2)]))),
])
def test_offers_that_must_never_be_accepted(what, offer):
    assert run(me_with(), [offer])["accept"] is None, what


def test_fair_play_stops_feeding_one_counterparty():
    import time
    deals = [(time.time() - 30, "m1")] * 3
    assert run(me_with(), [sell_offer(10, "LAV-09", 50, maker="m1")], deals=deals)["accept"] is None
    assert run(me_with(), [sell_offer(10, "LAV-09", 50, maker="m2")], deals=deals)["accept"] is not None
    old = [(time.time() - 10_000, "m1")] * 3
    assert run(me_with(), [sell_offer(10, "LAV-09", 50, maker="m1")], deals=old)["accept"] is not None


def test_only_boards_read_this_tick_can_be_traded():
    s = snap(me_with(), boards={})                                                # nothing fresh
    assert decide(s, CFG, MarketIntel(), Rng())["accept"] is None


# ------------------------------------------------------------------ selling to bids

def test_sells_a_spare_copy_to_a_bid_and_never_the_last_copy():
    plan = run(me_with(), [bid_offer(12, "LAT-04", 12)])                          # net 12-2 = 10, loss 1.75
    acc = plan["accept"]
    assert acc["kind"] == "sell" and acc["gain"] == pytest.approx(8.2, abs=0.1) and len(acc["assets"]) == 1
    only_one = me_with()
    only_one["assets"] = [a for a in only_one["assets"] if a["id"] != 109]        # drop one LAT-04: a single copy is left
    assert sum(a["ref"] == "LAT-04" for a in only_one["assets"]) == 1
    assert run(only_one, [bid_offer(12, "LAT-04", 80)])["accept"] is None


def test_does_not_sell_a_spare_for_less_than_it_costs_us():
    assert run(me_with(), [bid_offer(12, "LAT-04", 3)])["accept"] is None         # net 1 < loss 1.75 + margin


# ------------------------------------------------------------------ swaps

def test_swap_a_spare_for_a_missing_page_card_by_marginal_value():
    their = {"id": 20, "maker": "m5", "to": None, "status": "open",
             "give": side(0, assets=[card_asset("LAV-09", 77)]), "want": side(0, types=["card:LAT-04"])}
    acc = run(me_with(), [their])["accept"]
    assert acc["kind"] == "swap" and acc["gives"] == "LAT-04" and acc["gain"] == pytest.approx(142.7, abs=0.2)


def test_swap_that_gives_away_a_valuable_single_card_is_refused():
    their = {"id": 20, "maker": "m5", "to": None, "status": "open",
             "give": side(0, assets=[card_asset("LAT-01", 77)]), "want": side(0, types=["card:LAV-01"])}   # our only LAV-01 (112/..)
    assert run(me_with(), [their])["accept"] is None


def test_break_even_swaps_are_allowed_only_when_the_day_is_closing():
    """Cheap tastes (LAT affinity 0.3): a spare LAT-04 costs us 0.75, LAT-02 is worth 3 and the swap fees are 2 -> gain 0.25."""
    me = me_with()
    me["affinity"]["LAT"] = 0.3
    their = {"id": 20, "maker": "m5", "to": None, "status": "open",
             "give": side(0, assets=[card_asset("LAT-02", 77)]), "want": side(0, types=["card:LAT-04"])}
    assert run(me, [their])["accept"] is None                                     # needs a gain of 1 in a normal tick
    now = datetime(2026, 10, 3, 22, 59, tzinfo=timezone.utc)
    s = snap(me, [their], clock={"closes": (now + timedelta(minutes=1)).isoformat()})
    s["now"] = now
    acc = decide(s, CFG, MarketIntel(), Rng())["accept"]
    assert acc["kind"] == "swap" and acc["gain"] == pytest.approx(0.25, abs=0.06)      # gain is reported rounded to 0.1


def test_a_swap_for_the_same_card_is_never_taken():
    their = {"id": 20, "maker": "m5", "to": None, "status": "open",
             "give": side(0, assets=[card_asset("LAT-04", 77)]), "want": side(0, types=["card:LAT-04"])}
    assert run(me_with(), [their])["accept"] is None


# ------------------------------------------------------------------ listings

def test_lists_a_spare_near_book_when_nobody_else_sells_it():
    plan = run(me_with(), [])
    (l,) = plan["list"]
    assert l["ref"] == "LAT-04" and l["floor"] == 2 + 2 and l["ask"] == 10 and "near book" in l["why"]


def test_listing_matches_a_rival_ask_or_is_abandoned_below_the_floor():
    intel = MarketIntel()
    intel.observe(99, [sell_offer(50, "LAT-04", 8, maker="m3")])
    assert run(me_with(), [], intel=intel)["list"][0]["ask"] == 8
    intel2 = MarketIntel()
    intel2.observe(99, [sell_offer(51, "LAT-04", 3, maker="m3")])                 # below our floor 4: no price war
    plan = run(me_with(), [], intel=intel2)
    assert plan["list"] == [] and any("abandon" in s for s in plan["skipped"])


def test_no_listing_when_a_bid_already_pays_the_floor():
    intel = MarketIntel()
    intel.observe(99, [bid_offer(52, "LAT-04", 12, maker="m4")])
    plan = run(me_with(), [], intel=intel)
    assert plan["list"] == [] and any("accept it instead" in s for s in plan["skipped"])


def test_some_listings_are_delayed_on_purpose_to_hide_information():
    plan = run(me_with(), [], rng=Rng(0.0))                                       # first random() < 0.2 -> delayed
    assert plan["list"] == [] and any("delayed" in s for s in plan["skipped"])


def test_never_lists_the_last_copy_nor_an_asset_we_already_listed():
    me = me_with()
    listed = [{"id": 1, "maker": "t06", "status": "open", "give": side(0, assets=[{"id": 109, "kind": "card"}]), "want": side(9)}]
    assert run(me, [], my_offers=listed)["list"] == []                           # the spare LAT-04 is already on the board; one copy stays
    only_one = me_with()
    only_one["assets"] = [a for a in only_one["assets"] if a["ref"] != "LAT-04"] + [{"id": 1, "kind": "card", "ref": "LAT-04", "serial": 1}]
    assert run(only_one, [])["list"] == []


# ------------------------------------------------------------------ bids

def test_bids_for_missing_page_cards_at_sixty_percent_without_competition():
    plan = run(me_with(cash=600), [])
    by_ref = {b["ref"]: b for b in plan["bids"]}
    assert {"LAV-09", "LAV-10"} <= set(by_ref)
    for b in (by_ref["LAV-09"], by_ref["LAV-10"]):
        assert b["tier"] == "no competition" and b["price"] == int(round(0.6 * b["p_max"])) and b["price"] <= b["p_max"]
        assert b["value"] == pytest.approx(146.4, abs=0.1)


def test_bid_climbs_against_competition_and_stops_at_the_maximum():
    intel = MarketIntel()
    intel.observe(99, [bid_offer(60, "LAV-09", 90, maker="m7"), bid_offer(61, "LAV-09", 70, maker="m8")])
    plan = run(me_with(), [], intel=intel)
    b = next(x for x in plan["bids"] if x["ref"] == "LAV-09")
    assert b["tier"] == "strong competition" and 90 < b["price"] <= int(0.9 * b["p_max"])


def test_the_last_card_of_a_page_gets_the_aggressive_tier():
    plan = run(me_with(lav=range(1, 10)), [])
    b = next(x for x in plan["bids"] if x["ref"] == "LAV-10")
    assert b["tier"] == "last card of the page" and b["last_card"] and b["price"] <= b["p_max"] - 1


def test_no_bid_when_a_cheap_enough_ask_exists_because_the_accept_path_takes_it():
    intel = MarketIntel()
    intel.observe(99, [sell_offer(70, "LAV-09", 100, maker="m9")])
    plan = run(me_with(), [sell_offer(70, "LAV-09", 100, maker="m9")], intel=intel)
    assert plan["accept"]["ref"] == "LAV-09" and all(b["ref"] != "LAV-09" for b in plan["bids"])


def test_an_outbid_bid_is_cancelled_and_replaced_once_per_tick():
    intel = MarketIntel()
    intel.observe(99, [bid_offer(60, "LAV-09", 95, maker="m7"), bid_offer(61, "LAV-09", 70, maker="m8")])
    mine = [{"id": 5, "maker": "t06", "status": "open", "give": side(30), "want": side(0, types=["card:LAV-09"])}]
    plan = run(me_with(), [], intel=intel, my_offers=mine)
    assert plan["cancel"] == [5] and any(b["ref"] == "LAV-09" and b["price"] > 95 for b in plan["bids"])


def test_open_bids_never_commit_more_than_the_budget_share_of_cash():
    plan = run(me_with(cash=100), [])
    assert sum(b["price"] for b in plan["bids"]) <= CFG["bid_budget_share"] * 100


# ------------------------------------------------------------------ invariants over random markets

def random_market(rng):
    refs = [f"{s}-{i:02d}" for s in ("LAV", "LAT") for i in range(1, 13)]
    offers = []
    for oid in range(rng.randint(0, 25)):
        ref, maker = rng.choice(refs), f"m{rng.randint(1, 5)}"
        offers.append(sell_offer(oid, ref, rng.randint(1, 200), maker=maker) if rng.random() < 0.6 else bid_offer(oid, ref, rng.randint(1, 200), maker=maker))
    have = rng.sample(refs, rng.randint(2, 18)) + rng.sample(refs, rng.randint(0, 4))
    me = {"id": "t06", "tick": 100, "cash": rng.randint(0, 600), "affinity": {"LAV": rng.choice([0.7, 1.0, 1.6]), "LAT": rng.choice([0.7, 1.0, 1.6])},
          "assets": [{"id": 500 + i, "kind": "card", "ref": r, "serial": i + 1, "rarity": "common"} for i, r in enumerate(have)]}
    return me, offers


def test_invariants_over_many_random_markets():
    rng = random.Random(20261003)
    cat = toy_catalog()
    for n in range(300):
        me, offers = random_market(rng)
        intel = MarketIntel()
        intel.observe(99, offers)
        s = snap(me, offers)
        plan = decide(s, CFG, intel, random.Random(n))
        counts = {}
        for a in me["assets"]:
            counts[a["ref"]] = counts.get(a["ref"], 0) + 1
        acc = plan["accept"]
        if acc:
            assert acc["gain"] >= 0 and acc["gain"] >= acc["need"] - 1e-9, acc
            assert acc["cost"] <= plan["spendable"], acc
            if acc["kind"] == "buy":
                assert abs(acc["value"] - acc["cost"] - acc["gain"]) <= 0.11, acc          # gain is value minus cost (rounding only)
            if acc["kind"] == "sell":
                assert counts[acc["ref"]] >= 2, "sold the last copy"
        for l in plan["list"]:
            assert counts[l["ref"]] >= 2 and l["ask"] >= l["floor"] >= 1, l
        assert len({l["asset"] for l in plan["list"]}) == len(plan["list"])
        for b in plan["bids"]:
            assert 1 <= b["price"] <= b["p_max"], b
        assert sum(b["price"] for b in plan["bids"]) <= CFG["bid_budget_share"] * me["cash"] + 1e-9
        assert len(plan["list"]) + len(plan["bids"]) + len(plan["cancel"]) <= CFG["list_per_tick"]


# ------------------------------------------------------------------ arbitrage (off by default)

# LAT-01 is worth 7 to us: asked at 12 it is no normal buy, but a live bid of 25 makes it a resale
ARB_MARKET = [sell_offer(30, "LAT-01", 12, maker="m1"), bid_offer(31, "LAT-01", 25, maker="m2")]


def test_arbitrage_is_off_by_default():
    me = me_with()
    intel = MarketIntel()
    intel.observe(99, ARB_MARKET)
    assert run(me, ARB_MARKET, intel=intel)["accept"] is None


def test_arbitrage_buys_when_a_live_bid_pays_more_than_cost_plus_fees_and_resells_at_cost_basis():
    cfg = policy.cfg_with(arb=True)
    me = me_with()
    intel = MarketIntel()
    intel.observe(99, ARB_MARKET)
    acc = run(me, ARB_MARKET, intel=intel, cfg=cfg)["accept"]
    assert acc["kind"] == "arb" and acc["price"] == 12 and acc["gain"] >= cfg["arb_min"]
    # next ticks: we hold the lot; a bid that pays more than its cost basis is accepted even though it is our only copy
    me2 = me_with(extra=["LAT-01"])
    intel.extra["arb"] = {"LAT-01": acc["cost"]}
    intel.observe(100, [bid_offer(31, "LAT-01", 25, maker="m2")])
    sell = run(me2, [bid_offer(31, "LAT-01", 25, maker="m2")], intel=intel, cfg=cfg)["accept"]
    assert sell["kind"] == "arb" and sell["resale"] and sell["gain"] == pytest.approx(25 - 3 - acc["cost"])


def test_arbitrage_inventory_and_cash_are_capped():
    cfg = policy.cfg_with(arb=True)
    intel = MarketIntel()
    intel.observe(99, ARB_MARKET)
    intel.extra["arb"] = {"X-1": 5, "X-2": 5}
    assert run(me_with(), ARB_MARKET, intel=intel, cfg=cfg)["accept"] is None        # inventory limit (2) reached


# ------------------------------------------------------------------ execution

class FakeAPI:
    """Records every call; `refuse` maps a method to an error code to raise."""
    def __init__(self, refuse=None):
        self.writes, self.refuse = [], refuse or {}

    def _w(self, name, *a, **k):
        self.writes.append((name, a, k))
        if name in self.refuse:
            raise BazaarError(self.refuse[name], "refused", 400)
        return {"ok": True}

    def accept(self, offer_id, assets=None):
        return self._w("accept", offer_id, assets)

    def list_offer(self, give, want, venue=None, to=None, expires_in_ticks=40):
        return self._w("list_offer", give, want, venue)

    def cancel(self, offer_id):
        return self._w("cancel", offer_id)


def plan_with_everything():
    plan = run(me_with(), [sell_offer(10, "LAV-09", 100)])
    plan["cancel"] = [5]
    return plan


def test_execute_sends_accept_cancel_listing_and_bids_in_that_order():
    ctx = {"intel": MarketIntel()}
    b = FakeAPI()
    out = execute(b, plan_with_everything(), ctx, CFG, log=lambda *a: None)
    names = [w[0] for w in b.writes]
    assert names[0] == "accept" and names[1] == "cancel" and "list_offer" in names
    assert out["accepted"]["offer"] == 10 and out["listed"] == 1 and out["bids"] >= 1 and out["cancelled"] == 1
    assert ctx["deals"] and ctx["deals"][0][1] == "m1" or ctx["deals"][0][1] == "rival_01"


def test_execute_respects_the_shared_accept_gate():
    import os
    AcceptGate(os.environ["ACCEPT_GATE_DIR"]).reserve(100)                         # another process took this tick
    b = FakeAPI()
    out = execute(b, plan_with_everything(), {"intel": MarketIntel()}, CFG, log=lambda *a: None)
    assert out["accepted"] is None and "another process" in out["blocked"]
    assert not any(w[0] == "accept" for w in b.writes) and any(w[0] == "list_offer" for w in b.writes)


def test_a_refused_accept_does_not_crash_and_self_venue_blacklists_the_venue():
    ctx = {"intel": MarketIntel()}
    b = FakeAPI(refuse={"accept": "self_venue"})
    out = execute(b, plan_with_everything(), ctx, CFG, log=lambda *a: None)
    assert out["accepted"] is None and out["blocked"] == "self_venue" and "rastro" in ctx["skip_venues"]


def test_arbitrage_lot_bookkeeping_on_execute():
    cfg = policy.cfg_with(arb=True)
    me = me_with()
    intel = MarketIntel()
    intel.observe(99, ARB_MARKET)
    plan = run(me, ARB_MARKET, intel=intel, cfg=cfg)
    ctx = {"intel": intel}
    execute(FakeAPI(), plan, ctx, cfg, log=lambda *a: None)
    assert intel.extra["arb"] == {"LAT-01": plan["accept"]["cost"]}


# ------------------------------------------------------------------ the runner: modes

class FakeWorld(FakeAPI):
    def __init__(self, offers, me=None, **kw):
        super().__init__(**kw)
        self.offers, self._me = offers, me or me_with()
        self.reads = []

    def clock(self):
        self.reads.append("clock")
        return {"tick": 100, "paused": False}

    def me(self):
        return copy.deepcopy(self._me)

    def catalog(self):
        return toy_catalog()

    def venues(self):
        return {"venues": [{"venue": "rastro", "fee_bps": 500, "fee_per_card": 1, "status": "open", "owner": None},
                           {"venue": "v02", "fee_bps": 0, "fee_per_card": 0, "status": "open", "owner": "t12"},
                           {"venue": "v01", "fee_bps": 0, "fee_per_card": 0, "status": "open", "owner": "t06"}]}

    def my_offers(self):
        return {"offers": []}

    def board(self, vid):
        self.reads.append(("board", vid))
        return {"offers": copy.deepcopy(self.offers) if vid == "rastro" else []}

    def value(self, card):
        return {"your_value": None}


@pytest.fixture
def v2(monkeypatch, tmp_path):
    monkeypatch.setattr(trading_v2, "INTEL_FILE", str(tmp_path / "intel.json"))
    monkeypatch.setattr(trading_v2, "SHADOW_LOG", str(tmp_path / "logs" / "shadow.jsonl"))
    monkeypatch.setattr(trading_v2, "BOARD_SPACING", 0)
    return trading_v2


def test_shadow_mode_decides_and_logs_but_never_writes(v2, tmp_path):
    b = FakeWorld([sell_offer(10, "LAV-09", 100)])
    plan = v2.step(b, v2.new_ctx(), "shadow")
    assert plan["accept"]["offer"] == 10 and plan["mode"] == "shadow"
    assert b.writes == [], "shadow mode performed a write"
    assert (tmp_path / "logs" / "shadow.jsonl").read_text().count("\n") == 1


def test_on_mode_executes_through_the_same_decision(v2):
    b = FakeWorld([sell_offer(10, "LAV-09", 100)])
    plan = v2.step(b, v2.new_ctx(), "on")
    assert ("accept", (10, None), {}) in b.writes and plan["result"]["accepted"]["offer"] == 10


def test_the_runner_never_reads_our_own_venue_and_always_reads_rastro(v2):
    b = FakeWorld([])
    v2.step(b, v2.new_ctx(), "shadow")
    boards = [r[1] for r in b.reads if isinstance(r, tuple)]
    assert "rastro" in boards and "v01" not in boards and "v02" in boards


def test_paused_game_means_no_decisions(v2):
    b = FakeWorld([sell_offer(10, "LAV-09", 100)])
    b.clock = lambda: {"tick": 100, "paused": True}
    assert v2.step(b, v2.new_ctx(), "on") is None and b.writes == []


def test_the_market_intel_file_is_saved_on_every_fifth_tick(v2, tmp_path):
    b = FakeWorld([bid_offer(10, "LAV-09", 30, maker="m1")], me=me_with(tick=105))
    b.clock = lambda: {"tick": 105, "paused": False}
    v2.step(b, v2.new_ctx(), "shadow")
    assert (tmp_path / "intel.json").exists()


# ------------------------------------------------------------------ the real board

def test_the_real_phase_0_board_produces_a_sane_plan():
    """76 real offers from 9 pseudonymous makers against an inventory built from cards that appear on that board."""
    import json, os
    from helpers import FIXTURES
    board = json.load(open(os.path.join(FIXTURES, "board_rastro.json"), encoding="utf-8"))["offers"]
    refs = sorted({o["give"]["assets"][0]["ref"] for o in board if o["give"]["assets"] and o["give"]["assets"][0].get("kind") == "card"})
    rng = random.Random(7)
    have = rng.sample(refs, 20) + rng.sample(refs, 4)
    me = {"id": "t06", "tick": 672, "cash": 250, "affinity": {s: 1.0 for s in ("LAV", "LAT", "MAL", "RET", "SAL", "LAP")},
          "assets": [{"id": 5000 + i, "kind": "card", "ref": r, "serial": i + 1, "rarity": "common"} for i, r in enumerate(have)]}
    catalog = toy_catalog()
    catalog["sets"] = [dict(toy_catalog()["sets"][0], id=sid, cards=[dict(c, id=c["id"].replace("LAV", sid)) for c in toy_catalog()["sets"][0]["cards"]])
                       for sid in ("LAV", "LAT", "MAL", "RET", "SAL")]
    s = {"me": me, "catalog": catalog, "venues": {"rastro": RASTRO}, "boards": {"rastro": board}, "my_offers": [], "clock": {},
         "valuer": V2Valuer(None, me, catalog, 12), "deals": [], "now": None}
    intel = MarketIntel()
    intel.observe(672, board, me_id="t06")
    plan = decide(s, CFG, intel, random.Random(1))
    assert plan["candidates"] >= 0 and plan["spendable"] == 250
    if plan["accept"]:
        assert plan["accept"]["gain"] >= plan["accept"]["need"] > 0
    assert len(plan["list"]) + len(plan["bids"]) + len(plan["cancel"]) <= CFG["list_per_tick"]


# ------------------------------------------------------------------ value requests

class CountingValueAPI:
    def __init__(self, value=50.0):
        self.calls, self.value_ = [], value

    def value(self, ref):
        self.calls.append(ref)
        return {"your_value": self.value_}


def test_server_values_are_cached_across_ticks_while_holdings_are_unchanged():
    api, cache, cat = CountingValueAPI(), {}, toy_catalog()
    me = me_with()
    v1 = V2Valuer(api, me, cat, 8, shared=cache)
    assert v1.base("LAV-09") == 50.0 and v1.base("LAV-09") == 50.0 and api.calls == ["LAV-09"]
    v2 = V2Valuer(api, dict(me, tick=105), cat, 8, shared=cache)
    assert v2.base("LAV-09") == 50.0 and api.calls == ["LAV-09"], "five ticks later: still cached"
    v3 = V2Valuer(api, dict(me, tick=130), cat, 8, shared=cache)
    assert v3.base("LAV-09") == 50.0 and api.calls == ["LAV-09", "LAV-09"], "expired after the ttl"
    held_more = me_with(extra=["LAV-09"])
    v4 = V2Valuer(api, held_more, cat, 8, shared=cache)
    v4.base("LAV-09")
    assert len(api.calls) == 3, "holdings changed (one more copy): the value must be asked again"


def test_value_requests_per_tick_are_capped_and_the_model_takes_over():
    api, cat = CountingValueAPI(), toy_catalog()
    v = V2Valuer(api, me_with(), cat, 2)
    vals = [v.base(r) for r in ("LAV-09", "LAV-10", "LAT-01", "LAT-02")]
    assert len(api.calls) == 2 and vals[0] == vals[1] == 50.0 and vals[2] == pytest.approx(7.0)     # model: 10 * 0.7
