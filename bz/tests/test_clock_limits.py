"""clock["limits"]: the REAL observed contract (fixtures/clock.json, tick 346) and the dynamic-limits spec.

* golden   : freezes the structure observed on 2026-10-03.
* artificial fixture (fixtures/clock_limits_artificial.json): same keys, different numbers. NOT captured from the server.
* spec     : `TickBudget.from_clock(payload)` (future bz.core.clock) must expose the 5 limits under the SAME names the API uses
             and must keep working when the numbers change. PENDING until it exists.
* known gap: production code hard-codes the limits and never reads clock["limits"] (P1 in REFACTOR_PLAN).
"""
import json
import os
import re

import pytest

from helpers import FIXTURES, ROOT
from spec import load

LIMIT_KEYS = ["accepts_per_team_per_tick", "messages_per_side_per_tick", "max_open_threads_per_team",
              "max_open_offers_per_team", "offers_per_team_per_tick"]
REAL_VALUES = {"accepts_per_team_per_tick": 1, "messages_per_side_per_tick": 1, "max_open_threads_per_team": 6,
               "max_open_offers_per_team": 30, "offers_per_team_per_tick": 12}


def fixture(name):
    return json.load(open(os.path.join(FIXTURES, name), encoding="utf-8"))


@pytest.mark.golden
def test_real_clock_limits_structure_is_frozen():
    limits = fixture("clock.json")["limits"]
    assert sorted(limits) == sorted(LIMIT_KEYS)
    assert all(type(v) is int for v in limits.values()), "all five are plain integers"
    assert limits == REAL_VALUES


@pytest.mark.golden
def test_real_clock_has_the_fields_the_agents_depend_on():
    c = fixture("clock.json")
    assert isinstance(c["tick"], int) and isinstance(c["paused"], bool)
    assert isinstance(c["tick_seconds"], (int, float)) and isinstance(c["next_tick_in"], (int, float))
    assert c["min_tick_seconds"] == 5.0 and c["max_tick_seconds"] == 60.0       # RULES.md: 5-60 s


@pytest.mark.golden
def test_artificial_fixture_has_the_same_keys_but_different_numbers():
    real, art = fixture("clock.json")["limits"], fixture("clock_limits_artificial.json")["limits"]
    assert sorted(art) == sorted(real)
    assert all(art[k] != real[k] for k in LIMIT_KEYS), "every number differs so a hard-coded value is always detected"
    assert "ARTIFICIAL" in fixture("clock_limits_artificial.json")["_note"]


@pytest.mark.golden
def test_current_hard_coded_constants_equal_the_real_limits_today():
    """Today there is no discrepancy: 12 value checks/tick is ours, but the 28/30 offers and 1 accept match the server.
    This is what will silently break the day the organisers move a limit."""
    import smart_agent, page_hunter
    assert page_hunter.MAX_OPEN == 28 <= REAL_VALUES["max_open_offers_per_team"]
    assert smart_agent.LISTINGS_PER_TICK == 3 <= REAL_VALUES["offers_per_team_per_tick"]


@pytest.mark.known_bug
def test_KNOWN_GAP_P1_production_code_never_reads_clock_limits():
    """KNOWN GAP (P1, hotfix not scheduled before phase 4). The production scripts hard-code their per-tick limits and never
    read clock["limits"]. If the organisers change a limit (RULES.md says they may), the agents ignore it."""
    readers = []
    for rel in ("smart_agent.py", "smart_duels.py", "page_hunter.py", "broker.py"):
        src = open(os.path.join(ROOT, rel), encoding="utf-8").read()
        if re.search(r'''["']limits["']''', src):
            readers.append(rel)
    assert readers, "none of the four production scripts reads clock['limits']"


# ------------------------------------------------------------------ spec for bz.core.clock.TickBudget (PENDING)

@pytest.fixture
def clock_module():
    return load("BZ_CLOCK_MODULE", "bz.core.clock", "phase 4: dynamic limits")


@pytest.mark.pending
@pytest.mark.parametrize("name, expected", [("clock.json", REAL_VALUES),
                                            ("clock_limits_artificial.json", fixture("clock_limits_artificial.json")["limits"])])
def test_spec_tick_budget_reads_the_values_from_the_payload(clock_module, name, expected):
    budget = clock_module.TickBudget.from_clock(fixture(name))
    assert {k: getattr(budget, k) for k in LIMIT_KEYS} == expected


@pytest.mark.pending
def test_spec_missing_limits_fall_back_to_todays_values(clock_module):
    payload = dict(fixture("clock.json"))
    del payload["limits"]
    budget = clock_module.TickBudget.from_clock(payload)
    assert {k: getattr(budget, k) for k in LIMIT_KEYS} == REAL_VALUES


@pytest.mark.pending
def test_spec_unknown_new_limit_keys_are_ignored_not_fatal(clock_module):
    payload = json.loads(json.dumps(fixture("clock.json")))
    payload["limits"]["some_future_limit"] = 9
    budget = clock_module.TickBudget.from_clock(payload)
    assert getattr(budget, "accepts_per_team_per_tick") == 1
