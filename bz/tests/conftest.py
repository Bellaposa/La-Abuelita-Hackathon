"""Shared pytest setup for the P0 safety suite.

* Puts the repo root on sys.path so the production scripts import as modules.
* Isolates EVERY test from production data: cwd is a temp dir, memory files and chat logs are redirected, and a guard
  compares SHA-256 hashes of the production data files before and after the session (a change fails the run).
* Registers the markers that classify results (see the summary printed at the end):
    known_bug       a test that detects a bug that exists today; it is expected to be RED until its hotfix.
    known_mismatch  documents a mismatch between sim/ (or another helper) and real payloads; RED until fixed.
    golden          freezes what the code does TODAY (characterization). Not a statement that it is correct.
    pending         the component or the real payload does not exist yet; the test is skipped.
No test touches the network and none performs a write against Bazaar.
"""
import hashlib
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

PROTECTED = ["memory.json", "duels_memory.json", "broker_memory.json", "dealers_intel.json", "historial_abuela.txt",
             "dashboard/history.json", "smart_agent.py", "smart_duels.py", "page_hunter.py", "broker.py", "bazaar_sdk.py"]


def _hashes():
    out = {}
    for rel in PROTECTED:
        p = os.path.join(ROOT, rel)
        out[rel] = hashlib.sha256(open(p, "rb").read()).hexdigest() if os.path.exists(p) else None
    return out


# Taken when this conftest is imported (collection time, before any test runs). pytest_sessionstart cannot be used: for a
# conftest that is not an "initial" one (plain `pytest` from the repo root) that hook is never called.
_BEFORE = _hashes()


def pytest_configure(config):
    for m, d in [("known_bug", "detects a bug that exists today (RED until its hotfix)"),
                 ("known_mismatch", "documents a mismatch with real payloads (RED until fixed)"),
                 ("golden", "characterization: freezes current behaviour"),
                 ("pending", "cannot be written/run yet: missing payload or component")]:
        config.addinivalue_line("markers", f"{m}: {d}")


def pytest_sessionfinish(session, exitstatus):
    after = _hashes()
    changed = [k for k in after if after[k] != _BEFORE.get(k)]
    if changed:
        print("\n!!! PRODUCTION FILES CHANGED DURING THE TEST RUN:", changed)
        session.exitstatus = 3


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    """cwd and every production memory/log path point to a temp dir; chat logging is captured."""
    monkeypatch.chdir(tmp_path)
    sys.path.insert(0, ROOT) if ROOT not in sys.path else None
    import smart_agent, smart_duels, broker  # noqa: E402  (imports have no side effects)
    monkeypatch.setattr(smart_agent, "MEMORY_FILE", str(tmp_path / "memory.json"))
    monkeypatch.setattr(smart_agent, "log_chat", lambda msg: None)
    monkeypatch.setattr(smart_duels, "MEM_FILE", str(tmp_path / "duels_memory.json"))
    monkeypatch.setattr(broker, "MEM_FILE", str(tmp_path / "broker_memory.json"))
    yield


def pytest_terminal_summary(terminalreporter):
    """GREEN / RED known / RED unexpected / PENDING, by outcome and marker."""
    tr, stats = terminalreporter, terminalreporter.stats
    def marks(rep):
        return set(getattr(rep, "keywords", {}).keys())
    green = [r for r in stats.get("passed", []) if r.when == "call"]
    failed = [r for r in stats.get("failed", []) if r.when in ("call", "setup")]
    skipped = stats.get("skipped", [])
    known = [r for r in failed if marks(r) & {"known_bug", "known_mismatch"}]
    unexpected = [r for r in failed if r not in known]
    tr.write_sep("=", "P0 safety baseline classification")
    tr.write_line(f"GREEN                 {len(green)}")
    tr.write_line(f"RED - KNOWN BUG       {len(known)}   (known_bug / known_mismatch: detect real defects, NOT adapted to pass)")
    tr.write_line(f"RED - UNEXPECTED      {len(unexpected)}   (a failure nobody expected: investigate)")
    tr.write_line(f"PENDING (skipped)     {len(skipped)}")
    for r in known:
        tr.write_line(f"  known: {r.nodeid}")
    for r in unexpected:
        tr.write_line(f"  UNEXPECTED: {r.nodeid}")
