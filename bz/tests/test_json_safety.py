"""JSON memory safety: a crash or a concurrent reader must never see a half-written or lost memory file.

Three groups:
  * golden   - the production savers that ALREADY write atomically (smart_agent.save_memory, smart_duels.save_mem);
  * known_bug- the two production writers that are NOT atomic (broker.py, dashboard/app.py), detected by reading their
               source (hotfix 1.6; they cannot be called without running a whole loop / a Flask app);
  * spec     - the future helper `bz.core.jsonio` (atomic_write_json / read_json). PENDING until it exists.
"""
import json
import os
import re
import threading

import pytest

from helpers import ROOT, prod
from spec import load

sa, sd = prod("smart_agent"), prod("smart_duels")


def _boom(*a, **k):
    raise OSError("simulated crash before the file replace")


# ------------------------------------------------------------------ production savers that are already atomic

@pytest.mark.golden
@pytest.mark.parametrize("saver, path_attr, mem", [
    ("save_memory", "MEMORY_FILE", {"observed": {"negotiations": [{"outcome": "deal"}]}, "chat_history": ["x"]}),
    ("save_mem", "MEM_FILE", {"duels": {"1": {"observed": {}, "inferred": {}}}, "finished": {}, "raw_samples": []}),
])
def test_existing_savers_write_valid_json_and_replace_the_old_file(saver, path_attr, mem):
    module = sa if saver == "save_memory" else sd
    path = getattr(module, path_attr)
    open(path, "w").write(json.dumps({"old": True}))
    getattr(module, saver)(mem)
    assert json.load(open(path)) != {"old": True}
    assert json.load(open(path))                                    # parseable


@pytest.mark.golden
@pytest.mark.parametrize("saver, path_attr, mem", [
    ("save_memory", "MEMORY_FILE", {"observed": {"negotiations": []}, "chat_history": []}),
    ("save_mem", "MEM_FILE", {"duels": {}, "finished": {}, "raw_samples": []}),
])
def test_existing_savers_leave_the_previous_file_intact_if_the_replace_fails(monkeypatch, saver, path_attr, mem):
    module = sa if saver == "save_memory" else sd
    path = getattr(module, path_attr)
    open(path, "w").write(json.dumps({"old": True}))
    monkeypatch.setattr(os, "replace", _boom)
    with pytest.raises(OSError):
        getattr(module, saver)(mem)
    assert json.load(open(path)) == {"old": True}


@pytest.mark.golden
def test_load_memory_tolerates_a_missing_or_corrupt_file():
    assert sa.load_memory()["observed"] == {"negotiations": []}               # missing -> defaults
    open(sa.MEMORY_FILE, "w").write('{"observed": {"negotiat')               # truncated JSON
    assert sa.load_memory()["observed"] == {"negotiations": []}               # corrupt -> defaults (memory is lost, no crash)


# ------------------------------------------------------------------ writers that are NOT atomic (static detection)

def _source(rel):
    return open(os.path.join(ROOT, rel), encoding="utf-8").read()


@pytest.mark.known_bug
def test_KNOWN_BUG_hotfix_1_6_broker_memory_is_written_in_place():
    """KNOWN BUG — hotfix 1.6. broker.py does `open(MEM_FILE, "w")` + json.dump directly: a kill during the write leaves a
    truncated file and the learned lifetimes are lost on the next start (load_mem silently returns {}).
    Desired: write a temp file and os.replace."""
    src = _source("broker.py")
    assert not re.search(r'open\(\s*MEM_FILE\s*,\s*["\']w["\']', src), "broker.py writes MEM_FILE with open(..., 'w')"


@pytest.mark.known_bug
def test_KNOWN_BUG_hotfix_1_6_dashboard_history_is_written_in_place():
    src = _source("dashboard/app.py")
    assert not re.search(r'open\(\s*HISTORY_FILE\s*,\s*["\']w["\']', src), "dashboard/app.py writes HISTORY_FILE with open(..., 'w')"


# ------------------------------------------------------------------ spec for the future helper (PENDING)

@pytest.fixture
def jsonio():
    return load("BZ_JSONIO_MODULE", "bz.core.jsonio", "atomic JSON helper")


@pytest.mark.pending
def test_spec_write_then_read_roundtrip(jsonio, tmp_path):
    p = str(tmp_path / "m.json")
    jsonio.atomic_write_json(p, {"a": 1, "ñ": [1, 2, {"b": None}]})
    assert jsonio.read_json(p) == {"a": 1, "ñ": [1, 2, {"b": None}]}


@pytest.mark.pending
def test_spec_replaces_an_existing_file(jsonio, tmp_path):
    p = str(tmp_path / "m.json")
    jsonio.atomic_write_json(p, {"v": 1})
    jsonio.atomic_write_json(p, {"v": 2})
    assert jsonio.read_json(p) == {"v": 2}


@pytest.mark.pending
def test_spec_missing_or_corrupt_file_returns_the_default(jsonio, tmp_path):
    p = str(tmp_path / "m.json")
    assert jsonio.read_json(p, default={"d": 1}) == {"d": 1}
    open(p, "w").write("{broken")
    assert jsonio.read_json(p, default={"d": 1}) == {"d": 1}


@pytest.mark.pending
def test_spec_if_the_replace_fails_the_old_file_stays_intact_and_parseable(jsonio, tmp_path, monkeypatch):
    p = str(tmp_path / "m.json")
    jsonio.atomic_write_json(p, {"v": "old"})
    monkeypatch.setattr(os, "replace", _boom)
    with pytest.raises(OSError):
        jsonio.atomic_write_json(p, {"v": "new"})
    monkeypatch.undo()
    assert json.load(open(p)) == {"v": "old"}


@pytest.mark.pending
def test_spec_unserializable_data_never_touches_the_existing_file(jsonio, tmp_path):
    p = str(tmp_path / "m.json")
    jsonio.atomic_write_json(p, {"v": "old"})
    with pytest.raises(TypeError):
        jsonio.atomic_write_json(p, {"v": object()})
    assert json.load(open(p)) == {"v": "old"}


@pytest.mark.pending
def test_spec_a_reader_never_sees_a_partial_file_while_a_writer_keeps_replacing_it(jsonio, tmp_path):
    p = str(tmp_path / "m.json")
    big = {"rows": list(range(5000))}
    jsonio.atomic_write_json(p, big)
    stop, bad = threading.Event(), []

    def writer():
        i = 0
        while not stop.is_set():
            try:
                jsonio.atomic_write_json(p, {"rows": list(range(5000)), "i": i})
            except Exception as e:      # on Windows os.replace can hit PermissionError while a reader has the file open:
                bad.append("writer: " + repr(e))                            # the helper itself must retry, not leak it
            i += 1

    def reader():
        for _ in range(300):
            try:
                with open(p) as f:
                    d = json.load(f)
                assert len(d["rows"]) == 5000
            except Exception as e:                                           # a torn read
                bad.append(repr(e))

    w, r = threading.Thread(target=writer), threading.Thread(target=reader)
    w.start(); r.start(); r.join(); stop.set(); w.join()
    assert bad == []
