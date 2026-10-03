"""MarketIntel on the REAL El Rastro board captured on 2026-10-03 (fixtures/board_rastro.json: 76 offers, 9 pseudonymous
makers, 69 asks and 7 bids) plus small synthetic cases for what a single capture cannot show (history, vanishing offers)."""
import json
import os

import pytest

from bz.trading.intel import MarketIntel
from helpers import FIXTURES, bid_offer, sell_offer


@pytest.fixture
def board():
    return json.load(open(os.path.join(FIXTURES, "board_rastro.json"), encoding="utf-8"))["offers"]


def test_real_board_shape_is_what_the_parser_expects(board):
    asks = [o for o in board if o["give"]["assets"]]
    bids = [o for o in board if o["give"]["cash"]]
    assert (len(board), len(asks), len(bids)) == (76, 69, 7)
    assert all(o["want"]["types"] and o["want"]["types"][0].startswith("card:") for o in bids)
    assert all(not o["want"]["assets"] and o["want"]["cash"] > 0 for o in asks)


def test_observe_real_board_counts_asks_and_bids(board):
    intel = MarketIntel()
    intel.observe(672, board, me_id="someone_else")
    assert sum(len(v) for v in intel.current_asks.values()) == 68           # 69 asks, one of them a PACK (sobre_plata): not valued, ignored
    assert sum(len(v) for v in intel.current_bids.values()) == 7
    assert len({m for h in intel.bid_hist.values() for _, _, m in h}) >= 3


def test_real_bid_is_visible_as_a_buyer_estimate(board):
    """Phase-0 board: m41383bb2 bids 47 for LAV-09 and 15 for LAV-06."""
    intel = MarketIntel()
    intel.observe(672, board)
    est = intel.buyer_estimate("LAV-09")
    assert est["price"] >= 47 and est["maker"]
    assert intel.best_bid("LAV-09")[0] >= 47
    assert intel.buyer_estimate("MAL-02") is None or isinstance(intel.buyer_estimate("MAL-02")["price"], (int, float))


def test_our_own_offers_and_dealer_threads_are_not_market_signals(board):
    mine = dict(board[0], maker="me")
    thread_offer = dict(board[1], thread=77)
    intel = MarketIntel()
    intel.observe(1, [mine, thread_offer], me_id="me")
    assert not intel.current_asks and not intel.current_bids and not intel.seen


def test_liquidity_grows_with_bids_and_early_disappearances():
    intel = MarketIntel(liq_scale=6.0)
    assert intel.liquidity("LAV-09") == 0.0
    for t, (price, maker) in enumerate([(20, "m1"), (22, "m2"), (24, "m3")]):
        intel.observe(100 + t, [dict(bid_offer(10 + t, "LAV-09", price, maker=maker), expires_tick=999)])
    assert intel.liquidity("LAV-09") == pytest.approx((3 + 0 + 3) / 6.0)      # 3 bids seen + 3 distinct bidders
    intel.observe(110, [])                                                    # all gone before expiry: traded or cancelled
    assert intel.liquidity("LAV-09") == 1.0                                   # capped at 1


def test_an_offer_on_a_board_we_did_not_read_is_not_counted_as_gone():
    intel = MarketIntel()
    o = dict(bid_offer(1, "LAV-09", 20), venue="v07", expires_tick=999)
    intel.observe(100, [o])
    intel.observe(101, [], fresh_venues={"rastro"})                           # v07 not read this tick
    assert "LAV-09" not in intel.gone and "1" in intel.seen
    intel.observe(102, [], fresh_venues={"rastro", "v07"})                    # now read, and it is not there
    assert len(intel.gone["LAV-09"]) == 1 and "1" not in intel.seen


def test_an_offer_that_expired_naturally_is_not_a_trade():
    intel = MarketIntel()
    intel.observe(100, [dict(bid_offer(1, "LAV-09", 20), expires_tick=105)])
    intel.observe(105, [])
    assert "LAV-09" not in intel.gone


def test_likely_builders_need_two_bids_in_a_set_and_few_listings():
    intel = MarketIntel()
    intel.observe(100, [bid_offer(1, "LAV-09", 30, maker="m1"), bid_offer(2, "LAV-10", 40, maker="m1"),
                        bid_offer(3, "LAV-09", 30, maker="m2"),
                        bid_offer(4, "SAL-01", 9, maker="m3"), bid_offer(5, "SAL-02", 9, maker="m3"),
                        sell_offer(6, "SAL-03", 12, maker="m3"), sell_offer(7, "SAL-04", 12, maker="m3"), sell_offer(8, "SAL-05", 12, maker="m3")])
    assert intel.likely_builders("LAV") == ["m1"]                            # m2 bid once; m3 lists more than it bids
    assert intel.likely_builders("SAL") == []


def test_buyer_estimate_boosts_a_builder_but_not_a_casual_bidder():
    intel = MarketIntel(builder_boost=0.25)
    intel.observe(100, [bid_offer(1, "LAV-09", 40, maker="m1"), bid_offer(2, "LAV-10", 40, maker="m1"), bid_offer(3, "MAL-01", 40, maker="m9")])
    assert intel.buyer_estimate("LAV-09") == {"price": 50.0, "maker": "m1", "builder": True}
    assert intel.buyer_estimate("MAL-01") == {"price": 40, "maker": "m9", "builder": False}


def test_history_is_pruned_to_the_window():
    intel = MarketIntel()
    intel.observe(100, [bid_offer(1, "LAV-09", 30)])
    intel.observe(100 + 241, [])
    assert "LAV-09" not in intel.bid_hist


def test_save_and_load_roundtrip_is_atomic_and_tolerates_corruption(tmp_path):
    path = str(tmp_path / "intel.json")
    intel = MarketIntel()
    intel.observe(100, [bid_offer(1, "LAV-09", 30, maker="m1")])
    intel.extra["arb"] = {"LAV-09": 7}
    intel.save(path)
    again = MarketIntel.load(path)
    assert again.bid_hist == intel.bid_hist and again.extra == {"arb": {"LAV-09": 7}}
    open(path, "w").write('{"bid_hist": {"LAV')                              # truncated
    assert MarketIntel.load(path).bid_hist == {}
    assert MarketIntel.load(str(tmp_path / "missing.json")).bid_hist == {}
