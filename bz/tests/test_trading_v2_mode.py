"""TRADING_V2 mode switch and its wiring into the legacy trading code.

Default is `off`: nothing changes in production until somebody writes `shadow` or `on`. In `on` the legacy market code
(smart_agent.phase_market, page_hunter) must step aside so exactly one agent trades.
"""
import os
import re

import pytest

from bz.trading import mode
from helpers import ROOT


@pytest.fixture
def mode_env(monkeypatch, tmp_path):
    f = tmp_path / "mode"
    monkeypatch.setenv("TRADING_V2_FILE", str(f))
    monkeypatch.delenv("TRADING_V2", raising=False)
    return f


def test_default_is_off_and_legacy_trades(mode_env):
    assert mode.get_mode() == "off" and mode.legacy_should_trade() is True


@pytest.mark.parametrize("word, expected, legacy", [("off", "off", True), ("shadow", "shadow", True), ("on", "on", False),
                                                    (" ON \n", "on", False), ("Shadow", "shadow", True)])
def test_file_selects_the_mode_and_only_on_silences_legacy(mode_env, word, expected, legacy):
    mode_env.write_text(word)
    assert mode.get_mode() == expected and mode.legacy_should_trade() is legacy


@pytest.mark.parametrize("junk", ["enabled", "1", "true", "on!", "shadow mode"])
def test_anything_else_is_off(mode_env, junk):
    mode_env.write_text(junk)
    assert mode.get_mode() == "off" and mode.legacy_should_trade() is True


def test_the_file_beats_the_environment_and_can_be_flipped_without_restart(mode_env, monkeypatch):
    monkeypatch.setenv("TRADING_V2", "on")
    assert mode.get_mode() == "on"                                    # no file: the environment decides
    mode_env.write_text("off")
    assert mode.get_mode() == "off"                                   # the file wins, effective immediately
    mode_env.write_text("shadow")
    assert mode.get_mode() == "shadow"
    mode_env.write_text("")
    assert mode.get_mode() == "on"                                    # empty file: fall back to the environment


def test_invalid_environment_is_off(mode_env, monkeypatch):
    monkeypatch.setenv("TRADING_V2", "yes please")
    assert mode.get_mode() == "off"


@pytest.mark.parametrize("script, call", [("smart_agent.py", "phase_market(b, me_market"), ("page_hunter.py", "step(b, b.me()")])
def test_legacy_scripts_are_guarded_by_the_mode(script, call):
    """Static wiring check: the legacy trading call is only reachable through legacy_should_trade()."""
    src = open(os.path.join(ROOT, script), encoding="utf-8").read()
    assert "from bz.trading.mode import legacy_should_trade" in src
    lines = src.splitlines()
    idx = next(i for i, l in enumerate(lines) if call in l and not l.lstrip().startswith("def "))
    window = "\n".join(lines[max(0, idx - 6): idx])
    assert re.search(r"legacy_should_trade\(\)", window), f"{script}: the call `{call}` is not guarded"


def test_the_mode_file_is_not_versioned():
    assert ".trading_v2_mode" in open(os.path.join(ROOT, ".gitignore"), encoding="utf-8").read()
