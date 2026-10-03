"""The 'what we have learned' panel: backend view (bz/observe/learning.py) and its rendering in dashboard/index.html.

Read-only by construction: the view is a pure function of the memory dicts, never mutates them and never raises on bad memory.
"""
import copy
import json
import os
import shutil
import subprocess

import pytest

from bz.observe.learning import learning_view
from helpers import FIXTURES, ROOT, new_duel_mem

H = os.path.join(ROOT, "dashboard", "index.html")


def rnd(ask, our, final=False):
    return {"her_ask": ask, "final": final, "our": our, "our_move": None, "her_move": None, "gap_before": None}


def mem_with(*negs, traits=True):
    m = {"observed": {"negotiations": list(negs)}}
    if traits:
        real = json.load(open(os.path.join(FIXTURES, "dealers.json"), encoding="utf-8"))["personas"]
        m["dealer_traits"] = {d["id"]: d["traits"] for d in real}
        m["dealer_menu"] = {"abuela": {"pack:sobre_barrio": 26}, "chato": {"rarity:rare": 77}}
    return m


def deal(key, amount, rounds, sale=False, reason=None):
    n = {"dealer": key, "outcome": "deal", "closed_reason": reason, "rounds": rounds}
    n["received" if sale else "paid"] = amount
    return n


def duel(role, limit, moves, rival=None):
    obs = {"role": role, "limit": limit, "issues": ["price"],
           "history": [{"rival_price": 50, "rival_move": m} for m in moves]}
    if rival:
        obs["rival"] = rival
    return {"observed": obs, "state": {}}


def view(**kw):
    return learning_view(kw.get("mem"), kw.get("duels"), kw.get("intel"))


# ------------------------------------------------------------------ backend

def test_empty_and_broken_memories_give_an_empty_view_not_an_error():
    for junk in (None, {}, "junk", 5, [], {"observed": "x"}):
        v = learning_view(junk, junk, junk)
        assert isinstance(v["dealers"], list) and v["learning_enabled"] in (True, False)
        json.dumps(v)


def test_dealer_rows_carry_evidence_hints_traits_and_menu():
    m = mem_with(deal("abuela", 22, [rnd(25, 15, final=True)]), deal("abuela", 24, [rnd(24, 22)]), deal("abuela", 23, [rnd(23, 22)]))
    row = next(d for d in view(mem=m)["dealers"] if d["key"] == "abuela")
    assert row["name"] == "Abuela Carmen" and row["negotiations"] == 3 and row["deals"] == 3
    assert (row["settle"]["settle_n"], row["settle"]["settle_min"], row["settle"]["settle_max"]) == (4, 22, 25)
    assert row["suggested"]["open_bid"] == 15 and row["suggested"]["soft_cap"] == 27
    assert row["prior_mult"] == pytest.approx(0.585) and row["list_prices"] == {"pack:sobre_barrio": 26}
    names = {p["name"]: p for p in row["params"]}
    assert set(names) == {"tol_share", "open_scale", "block_after_fail"}
    assert names["tol_share"]["need"] == 3 and names["tol_share"]["n"] >= 0


def test_learned_flag_and_bounds_are_reported_honestly():
    few = view(mem=mem_with(deal("abuela", 22, [rnd(22, 22)])))["dealers"][0]
    assert not any(p["learned"] for p in few["params"] if p["name"] == "tol_share")           # one deal: still the default
    many = view(mem=mem_with(*[deal("abuela", 22 + g, [rnd(22 + g, 20)]) for g in (0, 1, 1, 2)]))["dealers"][0]
    tol = next(p for p in many["params"] if p["name"] == "tol_share")
    assert tol["learned"] and tol["lo"] <= tol["value"] <= tol["hi"] and tol["n"] == 4 and tol["value"] != tol["default"]


def test_card_buy_keys_are_labelled_and_use_the_dealers_traits():
    m = mem_with(deal("chato_buy", 9, [rnd(9, 9)]), deal("chato", 18, [rnd(18, 20)], sale=True))
    rows = {d["key"]: d for d in view(mem=m)["dealers"]}
    assert rows["chato_buy"]["name"] == "El Chato · compra cartas" and rows["chato_buy"]["prior_mult"] == rows["chato"]["prior_mult"]


def test_overrides_and_the_learning_switch_are_visible(monkeypatch):
    monkeypatch.setenv("DEALER_PARAMS", json.dumps({"pack_edge": 0.8}))
    monkeypatch.setenv("DEALER_LEARN", "0")
    v = view(mem=mem_with())
    assert v["overrides"] == {"pack_edge": 0.8} and v["learning_enabled"] is False


def test_duel_profiles_by_role_and_alias_with_the_prior_state():
    d = new_duel_mem()
    d["duels"] = {"a": duel("seller", 100, [5] * 7, "Rival Plata"), "b": duel("seller", 100, [0] * 6, "Rival Oro"), "c": duel("buyer", 100, [3, 3])}
    v = view(duels=d)["duels"]
    assert v["total"] == 3 and v["with_alias"] == 2 and v["thresholds"] == {"alias_moves": 6, "role_moves": 12}
    assert v["roles"]["seller"]["moves"] == 13 and v["roles"]["seller"]["prior"]["source"] == "role"
    assert v["roles"]["buyer"]["prior"] is None and {a["alias"] for a in v["aliases"]} == {"Rival Plata", "Rival Oro"}


def test_market_view_summarises_the_intel_file():
    from bz.trading.intel import MarketIntel
    from helpers import bid_offer
    intel = MarketIntel()
    intel.observe(100, [bid_offer(1, "LAV-09", 40, maker="m1"), bid_offer(2, "LAV-10", 40, maker="m1"), bid_offer(3, "LAV-09", 45, maker="m2")])
    v = view(intel=intel.to_state())["market"]
    assert v["makers"] == 2 and v["cards_with_bids"] == 2 and v["liquid"][0]["ref"] == "LAV-09" and v["liquid"][0]["estimate"] >= 45
    assert v["builders"] == {"LAV": ["m1"]}
    assert view(intel={})["market"] is None


def test_the_view_never_mutates_its_inputs_and_is_deterministic():
    m = mem_with(deal("abuela", 22, [rnd(25, 15, final=True)]), deal("abuela", 24, [rnd(24, 22)]))
    d = new_duel_mem()
    d["duels"] = {"a": duel("seller", 100, [5] * 13)}
    before = copy.deepcopy((m, d))
    a, b = learning_view(m, d, None), learning_view(m, d, None)
    assert (m, d) == before and a == b


def test_real_memory_files_render_without_error():
    """The actual memory.json / duels_memory.json of the team (read only): the view must handle real data."""
    def load(name):
        try:
            return json.load(open(os.path.join(ROOT, name), encoding="utf-8"))
        except (OSError, ValueError):
            return None
    v = learning_view(load("memory.json"), load("duels_memory.json"), None)
    json.dumps(v)
    assert v["dealers"] is not None


# ------------------------------------------------------------------ wiring and rendering

def test_dashboard_backend_exposes_the_view_and_stays_read_only():
    src = open(os.path.join(ROOT, "dashboard", "app.py"), encoding="utf-8").read()
    assert "learning_view(" in src and '"learning":' in src
    for write in ("b.accept(", "b.say(", "b.list_offer(", "b.open_thread(", "b.duel_say(", "b.duel_accept(", "b.cancel(", "b.flag("):
        assert write not in src, f"the dashboard must stay read-only: {write}"


def test_the_page_has_a_learning_card_and_calls_the_renderer():
    html = open(H, encoding="utf-8").read()
    assert 'id="learning"' in html and "function drawLearning(s)" in html and "drawLearning(s);" in html


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_the_panel_renders_in_a_fake_dom_with_real_looking_data(tmp_path):
    m = mem_with(deal("abuela", 22, [rnd(25, 15, final=True)]), deal("abuela", 24, [rnd(24, 22)]), deal("abuela", 23, [rnd(23, 22)]),
                 deal("chato", 18, [rnd(20, 26, final=True)], sale=True))
    d = new_duel_mem()
    d["duels"] = {"a": duel("seller", 100, [5] * 13, "Rival Plata")}
    state = {"learning": learning_view(m, d, None)}
    f = tmp_path / "state.json"
    f.write_text(json.dumps(state))
    out = subprocess.run(["node", os.path.join(os.path.dirname(__file__), "js_learning_check.js"), H, str(f)],
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    text = json.loads(out.stdout)["text"]
    for needle in ("Abuela Carmen", "El Chato", "tol_share", "open_scale", "block_after_fail", "Rival Plata", "Nosotros vendemos", "n/necesarias"):
        assert needle in text, needle
    assert "undefined" not in text and "NaN" not in text and "[object" not in text


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_the_panel_renders_with_no_data_at_all(tmp_path):
    f = tmp_path / "state.json"
    f.write_text(json.dumps({"learning": learning_view({}, None, None)}))
    out = subprocess.run(["node", os.path.join(os.path.dirname(__file__), "js_learning_check.js"), H, str(f)], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert "negociaciones con dealers" in json.loads(out.stdout)["text"]
