"""SPEC (executable) for the shared accept gate: one accept per tick across PROCESSES (hotfix 1.4).
Implemented in bz/core/accept_gate.py; these tests were PENDING (skipped) until then.

Interface:

    gate = AcceptGate(path)        # path: a file or directory shared by every process of the team
    gate.reserve(tick) -> bool     # True: this process may send ONE accept in `tick`; False: someone already took it

Every `AcceptGate(path)` instance models a separate process: they share nothing except the filesystem.
"""
import concurrent.futures
import os
import subprocess
import sys
import textwrap
import threading

import pytest

from helpers import ROOT
from spec import load


@pytest.fixture
def gate_module():
    return load("BZ_GATE_MODULE", "bz.core.accept_gate", "hotfix 1.4: shared accept gate")


def test_case_a_second_process_cannot_reserve_the_same_tick(gate_module, tmp_path):
    p1, p2 = gate_module.AcceptGate(str(tmp_path / "gate")), gate_module.AcceptGate(str(tmp_path / "gate"))
    assert p1.reserve(100) is True
    assert p2.reserve(100) is False


def test_case_a2_the_same_process_cannot_reserve_twice_either(gate_module, tmp_path):
    g = gate_module.AcceptGate(str(tmp_path / "gate"))
    assert g.reserve(100) is True
    assert g.reserve(100) is False


def test_case_b_a_new_tick_allows_one_more_accept(gate_module, tmp_path):
    p1, p2 = gate_module.AcceptGate(str(tmp_path / "gate")), gate_module.AcceptGate(str(tmp_path / "gate"))
    assert p1.reserve(100) is True
    assert p2.reserve(100) is False
    assert p2.reserve(101) is True
    assert p1.reserve(101) is False


def test_case_c_simultaneous_attempts_exactly_one_wins_threads(gate_module, tmp_path):
    n = 16
    barrier, results = threading.Barrier(n), []

    def attempt():
        g = gate_module.AcceptGate(str(tmp_path / "gate"))          # own instance, as a separate process would have
        barrier.wait()
        results.append(g.reserve(300))

    ts = [threading.Thread(target=attempt) for _ in range(n)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert results.count(True) == 1 and results.count(False) == n - 1


def test_case_c2_simultaneous_attempts_exactly_one_wins_real_processes(gate_module, tmp_path):
    """Real OS processes racing for the same tick. They are started in parallel; exactly one prints True."""
    code = textwrap.dedent(f"""
        import importlib, os, sys
        sys.path.insert(0, {ROOT!r})
        m = importlib.import_module({gate_module.__name__!r})
        print(m.AcceptGate({str(tmp_path / 'gate')!r}).reserve(400))
    """)
    env = dict(os.environ, PYTHONPATH=os.pathsep.join(filter(None, [ROOT, os.environ.get("PYTHONPATH", "")])))

    def run(_):
        return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, timeout=60).stdout.strip()

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as ex:
        outs = list(ex.map(run, range(6)))
    assert outs.count("True") == 1 and outs.count("False") == 5, outs
