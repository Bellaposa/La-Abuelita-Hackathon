"""PENDING: tests that cannot be written yet because the real payload was not captured (REFACTOR_PLAN phase 0 / 0.5).

Each test is skipped on purpose and names exactly what is missing. When the fixture exists, replace the skip with the test.
"""
import os

import pytest

from helpers import FIXTURES

pytestmark = pytest.mark.pending
PRIVATE = os.path.join(FIXTURES, "_private")


def need(name, why):
    path = os.path.join(PRIVATE, name)
    if not os.path.exists(path):
        pytest.skip(f"PENDING: {name} not captured ({why})")
    pytest.skip(f"PENDING: {name} exists locally but no test has been written for it yet")


def test_me_payload_shape_for_state_and_values():
    need("me.json", "needs BAZAAR_KEY; GET /api/me: assets[].your_value, affinity, open_threads, unlocked, cash")


def test_duels_payload_with_two_issues_and_days_weight():
    need("duels.json", "needs BAZAAR_KEY and a live duel; Duels I starts at 5.15 h; the sign of your_days_weight is unverified")


def test_my_offers_payload_for_directed_offers():
    need("my_offers.json", "needs BAZAAR_KEY; GET /api/me/offers (open and queued, plus offers addressed to us)")


def test_live_dealer_thread_standing_offer_shape():
    need("thread_abuela_open.json", "needs BAZAAR_KEY and an open thread; the Phase-0 history has the shape but not a live payload")


def test_bench_offers_in_the_market_test():
    need("broker_book_bench.json", "the Market Test (bench) starts at 5.0 h; the book had empty bench_offers in phase 0")


def test_news_endpoint_payload():
    need("news.json", "GET /api/news (Radio Rastro) was not requested in phase 0")


def test_want_a_card_offer_shape_types_vs_cards():
    need("board_rastro.json", "ofertas 'quiero una carta': `types: [card:REF]` vs `cards` is inferred from sim and code, not verified")
