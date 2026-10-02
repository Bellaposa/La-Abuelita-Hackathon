"""Team dashboard: one JSON endpoint (/api/state) and one page (/).

    BAZAAR_KEY=tk-... python3 dashboard/app.py          # http://localhost:5051

A background thread polls the API every POLL seconds (team key: me, clock, offers, threads = 4 requests; public data is
cached longer), so any number of browser tabs cost the game nothing. Read-only: it never trades.
Score history is sampled every SAMPLE seconds and kept in dashboard/history.json across restarts.
"""
import collections
import json
import os
import re
import subprocess
import sys
import threading
import time

from flask import Flask, jsonify, send_from_directory

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
from bazaar_sdk import Bazaar, BazaarError  # noqa: E402

URL = os.environ.get("BAZAAR_URL", "https://bazaar.causaprima.ai")
PORT = int(os.environ.get("DASH_PORT", "5051"))
POLL, SAMPLE = 8, 15
HISTORY_FILE = os.path.join(HERE, "history.json")
LOGS = [("agente", "smart_agent.log"), ("cacciatore", "page_hunter.log"), ("broker", "broker.log")]
EVENT = re.compile(r"MARKET|LISTED|SELL|BUY|BID for|Chato|negotiation .* ended|opened pack|MATCHED|ACCEPT|walk|"
                   r"error|Traceback|refused|Abuela thread .*-> (accept|offer|walk)")
SCRIPTS = ("smart_agent.py", "smart_duels.py", "page_hunter.py", "broker.py", "agent.py", "smart_broker.py")

app = Flask(__name__)
LOCK = threading.Lock()
STATE = {"ok": False, "error": "starting"}
HISTORY = collections.deque(maxlen=3000)
CACHE = {}          # name -> (ts, value) for slow-moving public data


def cached(name, ttl, fn):
    ts, val = CACHE.get(name, (0, None))
    if val is None or time.time() - ts > ttl:
        val = fn()
        CACHE[name] = (time.time(), val)
    return val


def read_log(name, limit=400):
    path = os.path.join(ROOT, name)
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.readlines()[-limit:]
    except OSError:
        return []


def activity():
    rows = []
    for label, fname in LOGS:
        for line in read_log(fname):
            m = re.match(r"(\d\d:\d\d:\d\d) (.*)", line.rstrip())
            if m and EVENT.search(m.group(2)):
                rows.append({"t": m.group(1), "src": label, "text": m.group(2)[:240]})
    rows.sort(key=lambda r: r["t"])
    return rows[-70:]


def processes():
    out, seen = [], collections.Counter()
    try:
        ps = subprocess.run(["ps", "-eo", "pid,etime,command"], capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return {"list": [], "duplicates": []}
    for line in ps.splitlines()[1:]:
        m = re.match(r"\s*(\d+)\s+(\S+)\s+(.*)", line)
        if not m:
            continue
        pid, etime, cmd = m.groups()
        first = cmd.split()[0].lower() if cmd.split() else ""
        if first.endswith(("zsh", "bash", "/sh")) or "grep" in cmd:
            continue
        sm = re.search(r"[Pp]ython\S*\s+(?:-\S+\s+)*(\S+\.py)\b", cmd)
        if sm and os.path.basename(sm.group(1)) in SCRIPTS:
            name = os.path.basename(sm.group(1))
            seen[name] += 1
            out.append({"pid": int(pid), "script": name, "uptime": etime})
    return {"list": out, "duplicates": [n for n, c in seen.items() if c > 1]}


def album(me, catalog):
    held = {}
    for a in me["assets"]:
        if a["kind"] == "card":
            held[a["ref"]] = held.get(a["ref"], 0) + 1
    sets = []
    for s in catalog["sets"]:
        if not s.get("released") or s["id"] not in me["affinity"]:
            continue
        aff = me["affinity"][s["id"]]
        page = [c for c in s["cards"] if c["page"]]
        page_value = sum(c["book"] * aff for c in page)
        missing = [{"ref": c["id"], "name": c["name"], "rarity": c["rarity"], "value": round(c["book"] * aff, 1)}
                   for c in page if c["id"] not in held]
        sets.append({"id": s["id"], "name": s["name"], "affinity": aff, "have": len(page) - len(missing), "of": len(page),
                     "missing": missing, "bonus": round(catalog["values"]["page_bonus"] * page_value, 1),
                     "extras": sum(max(0, n - 1) for r, n in held.items() if r.startswith(s["id"] + "-"))})
    return sorted(sets, key=lambda x: -x["affinity"])


def holdings(me):
    counts = {}
    for a in me["assets"]:
        if a["kind"] == "card":
            counts[a["ref"]] = counts.get(a["ref"], 0) + 1
    rows = [{"ref": a["ref"], "name": a["name"], "rarity": a["rarity"], "serial": a["serial"], "print_run": a["print_run"],
             "value": a["your_value"], "copies": counts[a["ref"]]} for a in me["assets"] if a["kind"] == "card"]
    return sorted(rows, key=lambda r: -r["value"])


def threads_view(me, threads):
    out = {}
    for t in threads:
        if t.get("kind") != "persona":
            continue
        seq = []
        for m in t.get("messages", []):
            o = m.get("offer") or {}
            if o:
                cash = o["give"]["cash"] or o["want"]["cash"]
                seq.append({"who": "noi" if m["sender"] == me["id"] else t["with"], "price": cash, "final": bool(o.get("final"))})
        row = {"id": t["id"], "status": t["status"], "reason": t.get("closed_reason"), "topic": t.get("topic"), "seq": seq[-8:]}
        d = out.setdefault(t["with"], {"open": None, "last": None, "deals": 0, "cooloffs": 0, "total": 0})
        d["total"] += 1
        d["deals"] += t["status"] == "deal"
        d["cooloffs"] += t["status"] == "cooloff"
        if t["status"] == "open":
            d["open"] = row
        if d["last"] is None or t["id"] > d["last"]["id"]:
            d["last"] = row
    return out


def duels_view(duels, tick):
    rows = []
    for d in duels:
        mine, rival = d.get("your_offer") or {}, d.get("rival_offer") or {}
        rows.append({"id": d.get("duel"), "item": d.get("item"), "role": d.get("role"), "rival": d.get("rival"),
                     "status": d.get("status"), "limit": d.get("your_limit"), "mine": mine.get("price"), "his": rival.get("price"),
                     "rounds": d.get("rounds"), "left": max(0, (d.get("deadline_tick") or tick) - tick),
                     "result": d.get("result"), "price": d.get("price"), "session": d.get("session")})
    return rows


def schedule_view(sched, clock):
    now_h, tick_s = clock["t_hours"], clock["tick_seconds"]
    rows = []
    for e in sched.get("upcoming", []):
        if e["action"] in ("announce", "grant_all", "day_closes", "day_opens"):
            continue
        eta = max(0.0, (e["at_hours"] - now_h) * 60 * tick_s)       # game minute = one tick at 60 s/tick
        rows.append({"action": e["action"], "note": e.get("note", ""), "eta_s": round(eta)})
    return rows[:7]


def poll_once(b):
    me, clock = b.me(), b.clock()
    offers = b.my_offers().get("offers", [])
    threads = b.my_threads().get("threads", [])
    duels = b.duels().get("duels", [])
    catalog = cached("catalog", 120, b.catalog)
    lb = cached("lb", 20, b.leaderboard)
    venues = cached("venues", 20, b.venues)
    sched = cached("sched", 60, b.schedule)
    mine = next((t for t in lb["teams"] if t["team"] == me["id"]), me["score"] or {})
    leader = lb["teams"][0] if lb["teams"] else {}
    my_v = next((v for v in venues["venues"] if v.get("owner") == me["id"]), None)
    s = me["score"] or {}
    return {
        "ok": True, "ts": time.time(),
        "team": {"id": me["id"], "name": me["name"], "level": me["level"], "cash": me["cash"],
                 "collection_value": me["collection_value"], "unlocked": me["unlocked"]},
        "score": {k: s.get(k) for k in ("score", "negotiating", "market", "neg_points", "mm_points", "duel_points",
                                         "ladder_points", "bench_points", "bench_efficiency", "deals", "luck", "rank",
                                         "album_filled", "album_slots", "pages_complete")},
        "rank_of": len(lb["teams"]), "leader": {"name": leader.get("name"), "score": leader.get("score")},
        "clock": {k: clock.get(k) for k in ("tick", "t_hours", "tick_seconds", "paused", "next_tick_in", "round_name", "doors", "closes")},
        "board": [{"rank": t["rank"], "name": t["name"], "score": t["score"], "us": t["team"] == me["id"]} for t in lb["teams"]],
        "snapshot_tick": lb.get("snapshot_tick"), "next_refresh_tick": lb.get("next_refresh_tick"),
        "album": album(me, catalog), "holdings": holdings(me),
        "offers_open": len([o for o in offers if o["maker"] == me["id"]]),
        "dealers": threads_view(me, threads), "duels": duels_view(duels, clock["tick"]),
        "venue": my_v, "schedule": schedule_view(sched, clock),
        "processes": processes(), "activity": activity(),
    }


def sample(st):
    if not st.get("ok"):
        return
    HISTORY.append({"ts": int(st["ts"]), "tick": st["clock"]["tick"], "score": st["score"]["score"], "rank": st["score"]["rank"],
                    "cash": st["team"]["cash"], "cv": st["team"]["collection_value"], "leader": st["leader"]["score"]})


def poller():
    key = os.environ.get("BAZAAR_KEY")
    b = Bazaar(URL, key, wait_on_tick=False, retries=1) if key else None
    last_sample, last_save = 0, 0
    while True:
        try:
            if not b:
                raise RuntimeError("BAZAAR_KEY is not set in the environment")
            st = poll_once(b)
        except (BazaarError, RuntimeError, KeyError, TypeError) as e:
            st = {"ok": False, "error": f"{type(e).__name__}: {e}", "ts": time.time()}
        with LOCK:
            if st["ok"] or not STATE.get("ok"):
                STATE.clear()
                STATE.update(st)
            else:
                STATE["stale_error"] = st["error"]
            if st["ok"] and time.time() - last_sample >= SAMPLE:
                sample(st)
                last_sample = time.time()
            if time.time() - last_save > 60 and HISTORY:
                try:
                    with open(HISTORY_FILE, "w") as f:
                        json.dump(list(HISTORY), f)
                    last_save = time.time()
                except OSError:
                    pass
        time.sleep(POLL)


@app.route("/")
def index():
    return send_from_directory(HERE, "index.html")


@app.route("/api/state")
def state():
    with LOCK:
        return jsonify({**STATE, "history": list(HISTORY)[-1500:], "server_time": time.time()})


if __name__ == "__main__":
    try:
        with open(HISTORY_FILE) as f:
            HISTORY.extend(json.load(f))
    except (OSError, ValueError):
        pass
    threading.Thread(target=poller, daemon=True).start()
    print(f"dashboard on http://localhost:{PORT}")
    app.run(host="127.0.0.1", port=PORT, debug=False, threaded=True)
