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
import statistics
import subprocess
import sys
import threading
import time

from flask import Flask, jsonify, send_from_directory

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
from bazaar_sdk import Bazaar, BazaarError  # noqa: E402
from bz.observe.learning import learning_view  # noqa: E402  (read-only: what the agents have learned)

URL = os.environ.get("BAZAAR_URL", "https://bazaar.causaprima.ai")
PORT = int(os.environ.get("DASH_PORT", "5051"))
POLL, SAMPLE = 8, 15
HISTORY_FILE = os.path.join(HERE, "history.json")
LOGS = [("agente", "smart_agent.log"), ("cazador", "page_hunter.log"), ("broker", "broker.log"), ("duelos", "smart_duels.log")]
EVENT = re.compile(r"MARKET|LISTED|SELL|BUY|BID for|Chato|negotiation .* ended|opened pack|MATCHED|ACCEPT|walk|"
                   r"error|Traceback|refused|Abuela thread .*-> (accept|offer|walk)|PILAR|Pilar")
SCRIPTS = ("smart_agent.py", "smart_duels.py", "page_hunter.py", "broker.py", "trading_v2.py")

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
        parsed = [m for m in (re.match(r"(\d\d:\d\d:\d\d) (.*)", l.rstrip()) for l in read_log(fname)) if m]
        day, later = 0, None
        for m in reversed(parsed):                       # logs span midnight: a later line with an earlier clock = new day
            if later is not None and m.group(1) > later:
                day -= 1
            later = m.group(1)
            if EVENT.search(m.group(2)):
                rows.append({"t": m.group(1), "day": day, "src": label, "text": m.group(2)[:240]})
    rows.sort(key=lambda r: (r["day"], r["t"]))
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


MATCHED = re.compile(r"(\d\d:\d\d:\d\d) tick (\d+): MATCHED (b\d+-\d+) x (b\d+-\d+) at (\d+)")


def bench_view():
    """Market Test sessions from our own records: bench_snapshots.jsonl (every book state the broker saw) and
    the MATCHED lines of broker.log. Per run: each trader's quote path, the pairs we locked, and what was left."""
    runs = collections.OrderedDict()
    try:
        with open(os.path.join(ROOT, "bench_snapshots.jsonl"), encoding="utf-8") as f:
            for line in f:
                try:
                    snap = json.loads(line)
                except ValueError:
                    continue
                for o in snap.get("bench") or []:
                    run = o["id"].split("-")[0]
                    r = runs.setdefault(run, {"run": run, "traders": {}, "matches": [], "first": snap["tick"], "last": snap["tick"]})
                    r["last"] = max(r["last"], snap["tick"])
                    is_ask = bool(o["want"]["cash"])
                    q = o["want"]["cash"] if is_ask else o["give"]["cash"]
                    tr = r["traders"].setdefault(o["id"], {"id": o["id"], "side": "ask" if is_ask else "bid", "path": []})
                    if not tr["path"] or tr["path"][-1] != [snap["tick"], q]:
                        tr["path"].append([snap["tick"], q])
    except OSError:
        pass
    for line in read_log("broker.log", 5000):
        m = MATCHED.search(line)
        if not m:
            continue
        _, tick, sell, buy, price = m.groups()
        run = sell.split("-")[0]
        r = runs.setdefault(run, {"run": run, "traders": {}, "matches": [], "first": int(tick), "last": int(tick)})
        r["matches"].append({"tick": int(tick), "sell": sell, "buy": buy, "price": int(price)})
    out = []
    for r in runs.values():
        tr = r["traders"]
        matched = {m["sell"] for m in r["matches"]} | {m["buy"] for m in r["matches"]}
        for m in r["matches"]:                                     # quotes at the moment we locked the pair
            m["ask"] = next((q for t, q in reversed(tr.get(m["sell"], {}).get("path", [])) if t <= m["tick"]), None)
            m["bid"] = next((q for t, q in reversed(tr.get(m["buy"], {}).get("path", [])) if t <= m["tick"]), None)
            m["spread"] = None if m["ask"] is None or m["bid"] is None else m["bid"] - m["ask"]
        out.append({"run": r["run"], "first": r["first"], "last": r["last"], "recorded": bool(tr),
                    "asks": sum(t["side"] == "ask" for t in tr.values()), "bids": sum(t["side"] == "bid" for t in tr.values()),
                    "matches": r["matches"], "unmatched": [t for k, t in tr.items() if k not in matched],
                    "traders": list(tr.values())})
    out.sort(key=lambda r: r["first"])
    return out[-8:]


def broker_book():
    """Live book of OUR venue through the broker key kept locally in .broker_key (read-only: never matches)."""
    try:
        key = open(os.path.join(ROOT, ".broker_key")).read().strip()
    except OSError:
        return None
    return Bazaar(URL, key, wait_on_tick=False, retries=1).broker(key).book()


def market_view(venues, my_v, s):
    book = cached("book", 8, broker_book) or {}
    def side(o):
        g, w = o.get("give") or {}, o.get("want") or {}
        refs = [a.get("ref") for a in g.get("assets") or []] or [t.split(":", 1)[-1] for t in w.get("types") or []]
        return ("vende" if w.get("cash") else "compra"), ", ".join(r for r in refs if r) or "—", w.get("cash") or g.get("cash")
    offers = [dict(zip(("side", "cards", "price"), side(o)), id=o.get("id"), maker=o.get("maker")) for o in book.get("offers") or []]
    bench = book.get("bench_offers") or []
    recent = [{"tick": r.get("tick"), "parties": r.get("parties"), "price": r.get("price"), "fee": r.get("fee"),
               "cards": [i.get("ref") for i in r.get("items") or [] if i.get("kind") == "card"]}
              for r in (book.get("recent") or [])][-15:]
    rank = sorted((v for v in venues.get("venues", []) if not v.get("house")), key=lambda v: (-(v.get("trades") or 0), -(v.get("volume") or 0)))
    table = [{"venue": v["venue"], "name": v.get("name"), "owner": v.get("owner_name"), "fee_bps": v.get("fee_bps"),
              "mechanism": (v.get("rules") or {}).get("mechanism"), "trades": v.get("trades") or 0, "traders": v.get("traders") or 0,
              "volume": v.get("volume") or 0, "value_created": v.get("value_created"), "us": v is my_v} for v in rank]
    house = next((v for v in venues.get("venues", []) if v.get("house")), None)
    return {"offers": offers, "bench_live": len(bench), "bench_run": bench[0]["id"].split("-")[0] if bench else None,
            "recent": recent, "venues": table, "our_rank": next((i + 1 for i, v in enumerate(table) if v["us"]), None),
            "house": house and {"trades": house.get("trades"), "volume": house.get("volume"), "traders": house.get("traders")},
            "points": {k: s.get(k) for k in ("market", "mm_points", "bench_points", "bench_efficiency")}}


def agent_memory():
    try:
        with open(os.path.join(ROOT, "memory.json"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def priorities_view(me, catalog):
    """Neighbourhood order and missing page cards by their server value to us (cached by smart_agent in memory.json)."""
    mem, held = agent_memory(), {}
    for a in me["assets"]:
        if a["kind"] == "card":
            held[a["ref"]] = held.get(a["ref"], 0) + 1
    cache, sets, cards = mem.get("value_cache", {}), [], []
    for st in catalog["sets"]:
        if not st.get("released") or st["id"] not in me["affinity"]:
            continue
        page = [c for c in st["cards"] if c.get("page")]
        have = sum(1 for c in page if held.get(c["id"]))
        sets.append({"id": st["id"], "name": st["name"], "affinity": me["affinity"][st["id"]], "have": have, "of": len(page),
                     "score": round(me["affinity"][st["id"]] * (1 + have / max(1, len(page))), 2)})
        for c in page:
            if not held.get(c["id"]):
                v = cache.get(c["id"])
                cards.append({"ref": c["id"], "name": c["name"], "rarity": c["rarity"], "book": c["book"],
                              "value": v[1] if v else round(c["book"] * me["affinity"][st["id"]], 1), "server": bool(v)})
    sets.sort(key=lambda r: -r["score"])
    cards.sort(key=lambda r: -r["value"])
    dupes = [{"ref": r, "copies": n} for r, n in sorted(held.items()) if n > 1]
    return {"sets": sets, "cards": cards[:10], "dupes": dupes, "flags": mem.get("flags_sent", []),
            "flags_checked": len(mem.get("flags_seen", []))}


def read_json(name):
    """A JSON file next to the agents (memory, intel). Missing or corrupt means None: the panel just shows less."""
    try:
        with open(os.path.join(ROOT, name), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def workshop_view():
    """The Workshop as smart_agent sees it: crafts made (inputs -> output, expected gain, luck), observed sale prices per
    rarity, and the last decision line from the log."""
    mem = agent_memory()
    crafts = []
    for c in mem.get("ws_crafts", [])[-12:]:
        luck = None
        try:
            luck = json.loads(c.get("raw") or "{}").get("luck")
        except ValueError:
            pass
        crafts.append({"tick": c.get("tick"), "from": c.get("from"), "inputs": [i[0] for i in c.get("inputs", [])],
                       "out": c.get("out"), "e_out": c.get("e_out"), "luck": luck})
    obs = {r: {"n": len(v), "median": round(statistics.median(v), 1)} for r, v in (mem.get("ws_obs") or {}).items() if v}
    last = next((l.split(" ", 1)[1].strip() for l in reversed(read_log("smart_agent.log", 600))
                 if "workshop" in l.lower() and re.match(r"\d\d:\d\d:\d\d ", l)), None)
    return {"crafts": crafts[::-1], "n": len(mem.get("ws_crafts", [])), "obs": obs, "last": last,
            "outputs_seen": sum((mem.get("ws_out") or {}).values())}


def duel_memory():
    try:
        with open(os.path.join(ROOT, "duels_memory.json"), encoding="utf-8") as f:
            return json.load(f).get("duels", {})
    except (OSError, ValueError):
        return {}


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
        msgs = [{"who": "nosotros" if m["sender"] == me["id"] else t["with"], "text": (m.get("text") or "")[:300],
                 "price": ((m.get("offer") or {}).get("give") or {}).get("cash") or ((m.get("offer") or {}).get("want") or {}).get("cash"),
                 "final": bool((m.get("offer") or {}).get("final"))} for m in t.get("messages", [])]
        row = {"id": t["id"], "status": t["status"], "reason": t.get("closed_reason"), "topic": t.get("topic"), "seq": seq[-8:],
               "messages": msgs[-14:]}
        d = out.setdefault(t["with"], {"open": None, "last": None, "deals": 0, "cooloffs": 0, "total": 0, "history": []})
        d["history"].append({"id": t["id"], "status": t["status"], "reason": t.get("closed_reason"), "topic": t.get("topic"),
                             "rounds": len(seq), "last_price": seq[-1]["price"] if seq else None})
        d["total"] += 1
        d["deals"] += t["status"] == "deal"
        d["cooloffs"] += t["status"] == "cooloff"
        if t["status"] == "open":
            d["open"] = row
        if d["last"] is None or t["id"] > d["last"]["id"]:
            d["last"] = row
    for d in out.values():
        d["history"] = sorted(d["history"], key=lambda h: -h["id"])[:12]
    return out


def duels_view(duels, tick):
    rows, mem = [], duel_memory()
    for d in duels:
        mine, rival = d.get("your_offer") or {}, d.get("rival_offer") or {}
        rows.append({"id": d.get("duel"), "item": d.get("item"), "role": d.get("role"), "rival": d.get("rival"),
                     "status": d.get("status"), "limit": d.get("your_limit"), "mine": mine.get("price"), "his": rival.get("price"),
                     "rounds": d.get("rounds"), "left": max(0, (d.get("deadline_tick") or tick) - tick),
                     "result": d.get("result"), "price": d.get("price"), "session": d.get("session")})
        side = 1 if d.get("role") == "seller" else -1
        r = rows[-1]
        r["gain"] = None if d.get("price") is None or d.get("your_limit") is None else round(side * (d["price"] - d["your_limit"]), 1)
        hist = (mem.get(str(d.get("duel"))) or {}).get("observed", {}).get("history", [])
        r["path"] = [{"round": h.get("round"), "left": h.get("remaining"), "rival": h.get("rival_price"), "ours": h.get("our_price"),
                      "decision": h.get("decision")} for h in hist][-16:]
    rows.sort(key=lambda r: (r["status"] != "live", -(r["session"] or 0), -(r["id"] or 0)))
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
    live = b.duels().get("duels", [])
    done = cached("duels_done", 30, lambda: b.duels(done=True)).get("duels", [])
    seen = {d.get("duel") for d in live}
    duels = live + [d for d in done if d.get("duel") not in seen]
    catalog = cached("catalog", 120, b.catalog)
    lb = cached("lb", 20, b.leaderboard)
    venues = cached("venues", 20, b.venues)
    sched = cached("sched", 60, b.schedule)
    news = cached("news", 30, lambda: b.call("GET", "/api/news")).get("news", [])
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
        "venue": my_v, "schedule": schedule_view(sched, clock), "bench": bench_view(), "priorities": priorities_view(me, catalog),
        "market": market_view(venues, my_v, s), "workshop": workshop_view(),
        "learning": learning_view(agent_memory(), read_json("duels_memory.json"), read_json("market_intel.json")),
        "news": [{k: n.get(k) for k in ("id", "tick", "source", "source_name", "headline", "body")} for n in news[:15]],
        "processes": processes(), "activity": activity(),
    }


def sample(st):
    if not st.get("ok"):
        return
    HISTORY.append({"ts": int(st["ts"]), "tick": st["clock"]["tick"], "score": st["score"]["score"], "rank": st["score"]["rank"],
                    "cash": st["team"]["cash"], "cv": st["team"]["collection_value"], "leader": st["leader"]["score"],
                    "eff": st["score"]["bench_efficiency"], "duel": st["score"]["duel_points"], "ladder": st["score"]["ladder_points"],
                    "neg": st["score"]["negotiating"], "mkt": st["score"]["market"]})


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
