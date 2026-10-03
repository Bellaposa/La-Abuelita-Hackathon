"""sim/ is NOT a source of truth yet: it disagrees with real payloads captured in phase 0.

KNOWN MISMATCH: GET /api/dealers returns {"personas": [...]} (fixtures/dealers.json); sim.world.FakeBazaar.dealers() returns
{"dealers": [...]}. Do not fix sim/ in this phase (REFACTOR_PLAN phase 0.5/1 decision). These tests document both shapes.
"""
import json
import os

import pytest

from helpers import FIXTURES, ROOT

import sys
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def real(name):
    return json.load(open(os.path.join(FIXTURES, name), encoding="utf-8"))


@pytest.mark.golden
def test_real_api_dealers_shape_is_personas():
    d = real("dealers.json")
    assert list(d) == ["personas"]
    ids = [x["id"] for x in d["personas"]]
    assert ids == ["abuela", "chato", "pilar"]
    keys = {"id", "name", "status", "level", "title", "kind", "enabled", "bio", "traits", "unlock", "open_to_all", "menu"}
    assert all(keys <= set(x) for x in d["personas"])
    assert {x["kind"] for x in d["personas"]} == {"dealer", "collector"}


@pytest.mark.golden
def test_real_dealer_unlock_rule_for_pilar_is_what_phase_0_observed():
    pilar = next(x for x in real("dealers.json")["personas"] if x["id"] == "pilar")
    assert pilar["level"] == 3 and pilar["open_to_all"] is False
    assert pilar["unlock"]["early_deals_with"] == "chato" and pilar["unlock"]["early_min_deals"] == 3


@pytest.mark.known_mismatch
def test_KNOWN_MISMATCH_sim_dealers_top_level_key_differs_from_the_real_api():
    """KNOWN MISMATCH — sim/ answers {'dealers': [...]}, the real server answers {'personas': [...]}.
    Desired (after sim/ is corrected): the same top-level key."""
    from sim.world import FakeBazaar
    assert list(FakeBazaar(seed=0).dealers()) == list(real("dealers.json"))


@pytest.mark.known_mismatch
def test_KNOWN_MISMATCH_sim_knows_only_the_dealers_phase_0_found_before_pilar():
    """The real world already has `pilar` (collector, level 3). The simulator has no such dealer."""
    from sim.world import FakeBazaar
    sim_ids = [x["id"] for x in next(iter(FakeBazaar(seed=0).dealers().values()))]
    assert sorted(sim_ids) == sorted(x["id"] for x in real("dealers.json")["personas"]), sim_ids


@pytest.mark.golden
def test_the_simulator_alias_personas_still_points_to_dealers():
    """Documents the other half of the mismatch: sim exposes `personas` only as a method alias (old SDK name)."""
    from sim.world import FakeBazaar
    s = FakeBazaar(seed=0)
    assert s.personas() == s.dealers()
