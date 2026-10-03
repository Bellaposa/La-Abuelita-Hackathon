"""SPEC (executable) for single-instance locks of smart_agent, smart_duels, page_hunter and broker (hotfix 1.2).

Interface expected (lives next to the atomic JSON helper in bz.core.jsonio; nothing implements it yet -> PENDING):

    acquire_lock(name, lock_dir) -> bool      # True: we are the only instance named `name`; False: another LIVE one exists
    release_lock(name, lock_dir) -> None      # after this, acquire_lock(name, ...) is True again

A lock held by a process that has died (crash, kill, power cut) must NOT block the next start (stale lock).
The lock file format is deliberately not specified: stale detection is tested with real processes.
"""
import os
import subprocess
import sys
import textwrap
import time

import pytest

from helpers import ROOT
from spec import load

pytestmark = pytest.mark.pending
NAMES = ["smart_agent", "smart_duels", "page_hunter", "broker"]


@pytest.fixture
def jsonio():
    return load("BZ_JSONIO_MODULE", "bz.core.jsonio", "hotfix 1.2: single-instance lock")


def _child(jsonio, lock_dir, name, how):
    """Run `acquire_lock` in ANOTHER process. how: 'hold' (stay alive), 'die' (exit without releasing)."""
    code = textwrap.dedent(f"""
        import importlib, os, sys, time
        sys.path.insert(0, {ROOT!r})
        m = importlib.import_module({jsonio.__name__!r})
        ok = m.acquire_lock({name!r}, {lock_dir!r})
        print(ok, flush=True)
        if {how!r} == "hold":
            time.sleep(60)
        else:
            os._exit(0)        # no release, no atexit: what a crash looks like
    """)
    env = dict(os.environ, PYTHONPATH=os.pathsep.join(filter(None, [ROOT, os.environ.get("PYTHONPATH", "")])))
    return subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True, env=env)


@pytest.mark.parametrize("name", NAMES)
def test_second_acquire_fails_then_succeeds_after_release(jsonio, tmp_path, name):
    d = str(tmp_path)
    assert jsonio.acquire_lock(name, d) is True
    assert jsonio.acquire_lock(name, d) is False
    jsonio.release_lock(name, d)
    assert jsonio.acquire_lock(name, d) is True
    jsonio.release_lock(name, d)


def test_locks_are_per_name(jsonio, tmp_path):
    d = str(tmp_path)
    assert jsonio.acquire_lock("smart_agent", d) is True
    assert jsonio.acquire_lock("smart_duels", d) is True                  # a different script is not blocked
    for n in ("smart_agent", "smart_duels"):
        jsonio.release_lock(n, d)


def test_live_holder_in_another_process_blocks_us(jsonio, tmp_path):
    d = str(tmp_path)
    child = _child(jsonio, d, "smart_agent", "hold")
    try:
        assert child.stdout.readline().strip() == "True"
        assert jsonio.acquire_lock("smart_agent", d) is False
    finally:
        child.kill()
        child.wait()


def test_stale_lock_of_a_dead_process_does_not_block(jsonio, tmp_path):
    """The previous instance died without releasing: its PID no longer exists. We must be able to start."""
    d = str(tmp_path)
    child = _child(jsonio, d, "smart_agent", "die")
    assert child.stdout.readline().strip() == "True"
    child.wait(timeout=30)
    time.sleep(0.2)
    assert jsonio.acquire_lock("smart_agent", d) is True
    jsonio.release_lock("smart_agent", d)


def test_release_of_a_lock_we_do_not_hold_is_harmless(jsonio, tmp_path):
    jsonio.release_lock("broker", str(tmp_path))                            # must not raise
    assert jsonio.acquire_lock("broker", str(tmp_path)) is True
    jsonio.release_lock("broker", str(tmp_path))
