"""Loader for components that do NOT exist yet (executable specification).

Each future component is imported by module name; if it is missing the test is skipped as PENDING. The module names can be
overridden with environment variables, which is how the suite itself was validated against a throw-away reference
implementation kept OUTSIDE the repo:

    BZ_GATE_MODULE   (default bz.core.accept_gate)  expects  AcceptGate(path).reserve(tick: int) -> bool
    BZ_JSONIO_MODULE (default bz.core.jsonio)       expects  atomic_write_json(path, obj), read_json(path, default=None),
                                                             acquire_lock(name, lock_dir) -> bool, release_lock(name, lock_dir)
    BZ_CLOCK_MODULE  (default bz.core.clock)        expects  TickBudget.from_clock(clock_payload: dict) with the 5 limits
                                                             as attributes (see test_clock_limits.py)
"""
import importlib
import os

import pytest


def load(env, default, what):
    name = os.environ.get(env, default)
    try:
        return importlib.import_module(name)
    except ModuleNotFoundError as e:
        if e.name and name.startswith(e.name):
            pytest.skip(f"PENDING: {name} does not exist yet ({what})")
        raise
