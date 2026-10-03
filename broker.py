"""Team 6 broker for the Market Test.

    BROKER_KEY=bk_... python3 broker.py          # on a venue opened with mechanism "board"
    python3 broker.py --selftest                 # offline simulation, no network

The Market Test scores the gains between the traders' TRUE limits, and the price inside [ask, bid] does not change
those gains. What we control is WHEN to lock a crossing pair and with WHOM. Per pair, each tick:

  MATCH  the session is about to end            (every unmatched pair is worth 0 after it)
  MATCH  one side looks about to leave          (its age passed what we have seen traders live, or half the session)
  MATCH  both sides are firm                    (>= 2 flat observations each: waiting buys nothing)
  WAIT   only if BOTH sides were observed relaxing their quotes and none is at risk (BROKER_WAIT=0 disables waiting)
  MATCH  anything else (mixed, or too few observations): we never wait on a guess

There is no "elapsed >= 2" rule: a pair is never matched merely because time passed.

Session length is read from the offers (`expires_tick`) when the book carries it, else from the schedule
(`bench` events: params.ticks), else from what we observed on past sessions, else from SESSION_TICKS (env, default 16).
Observed facts (lifetimes of traders that vanished unmatched, session lengths) go to broker_memory.json.
"""
import json
import math
import os
import sys
import time
from collections import defaultdict

from bazaar_sdk import BazaarError, Broker
from starter_broker import public_plan

URL = os.environ.get("BAZAAR_URL", "https://bazaar.causaprima.ai")
MEM_FILE = os.environ.get("BROKER_MEMORY", "broker_memory.json")
SESSION_TICKS = int(os.environ.get("SESSION_TICKS", "16"))   # last-resort default, not a truth
URGENT_AGE_FRAC = 0.5      # with no lifetime data: a trader older than this share of the session may leave soon
LATE_TICKS = 2             # this close to the end of a session, match everything that crosses
WAIT_ENABLED = os.environ.get("BROKER_WAIT", "1") != "0"   # BROKER_WAIT=0: never wait, match crossing pairs at once
WAIT_FORCED_OFF = not WAIT_ENABLED                          # an explicit BROKER_WAIT=0 always wins over what we learn


ANNOUNCE_EVERY_H = float(os.environ.get("ANNOUNCE_EVERY_H", "0.333"))   # game clock hours (= real open time): ~20 min
ANNOUNCEMENTS = [                                     # facts only; rotated so the feed never sees the same words twice in a row
    "Team 6 · v01: 0 % fee, no per-card charge. Our broker matches every crossing pair each tick, card by card, "
    "at the midpoint, any copy included.",
    "Selling spares or hunting a card? Post it on v01 (Team 6): 0 % fee, and a broker crosses bids and asks every tick, "
    "any copy of the card counts.",
    "v01 · Mercado Team 6: zero fees, matched every tick at the midpoint, so both sides beat their own quote. "
    "Bids and asks welcome.",
]


def maybe_announce(broker, clock, state):
    """Publish one of ANNOUNCEMENTS every ANNOUNCE_EVERY_H hours of game clock (stops by itself while doors are closed)."""
    t = clock.get("t_hours")
    if ANNOUNCE_EVERY_H <= 0 or not isinstance(t, (int, float)) or clock.get("paused"):
        return False
    if state.get("last_t") is not None and t - state["last_t"] < ANNOUNCE_EVERY_H:
        return False
    text = ANNOUNCEMENTS[state.get("i", 0) % len(ANNOUNCEMENTS)]
    try:
        broker.announce(text)
        log(f"announced ({t:.2f} h): {text[:70]}...")
    except BazaarError as e:
        log(f"announce refused ({e})")
    state["last_t"], state["i"] = t, state.get("i", 0) + 1
    return True


def apply_learned(model, why):
    """bench_learn.py replays our recorded sessions and picks the best HOLD; 0 = lock every crossing pair at once."""
    global WAIT_ENABLED
    if not model:
        return
    hold = model.get("best_hold", 0)
    WAIT_ENABLED = (hold > 0) and not WAIT_FORCED_OFF
    log(f"learned ({why}): best hold {hold} -> waiting {'on' if WAIT_ENABLED else 'off'}; est. eff by hold "
        f"{model.get('est_eff_by_hold')}; relax {model.get('relax')}; lifetime {model.get('lifetime')} "
        f"over {model.get('n_sessions')} sessions")


def relearn(why):
    try:
        import bench_learn
        apply_learned(bench_learn.learn(), why)
    except Exception as e:                       # learning must never stop the broker
        log(f"learning failed ({type(e).__name__}: {e}); keeping the current policy")
MIN_LIFETIMES = 5          # observed lifetimes needed before we trust the leave-time estimate


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


# ---------------------------------------------------------------- tracking

class Track:
    """Per-offer quote history, age and the observed lifetimes of traders that vanished."""

    def __init__(self, mem=None):
        self.hist = {}        # id -> [(tick, quote)] one entry per tick
        self.first = {}       # id -> first tick we saw it (or its created_tick)
        self.side = {}        # id -> "ask" | "bid"
        self.expires = {}     # id -> expires_tick if the book says so
        self.lifetimes = list((mem or {}).get("lifetimes", []))
        self.session_lens = list((mem or {}).get("session_lens", []))
        self.run_first = {}   # run -> first tick seen
        self.run_last = {}

    def update(self, tick, offers, matched=()):
        seen = set()
        for o in offers:
            oid = o["id"]
            seen.add(oid)
            is_ask = bool(o["want"]["cash"])
            q = o["want"]["cash"] if is_ask else o["give"]["cash"]
            self.side[oid] = "ask" if is_ask else "bid"
            h = self.hist.setdefault(oid, [])
            if not h or h[-1][0] != tick:
                h.append((tick, q))
            self.first.setdefault(oid, o.get("created_tick", tick) if isinstance(o.get("created_tick"), int) else tick)
            if isinstance(o.get("expires_tick"), int):
                self.expires[oid] = o["expires_tick"]
            run = oid.split("-")[0]
            self.run_first.setdefault(run, tick)
            self.run_last[run] = tick
        for oid in [i for i in self.hist if i not in seen]:          # vanished: matched by us, or it left
            if oid not in matched:
                self.lifetimes.append(self.age(oid, tick - 1))
            for d in (self.hist, self.first, self.side, self.expires):
                d.pop(oid, None)
        self.lifetimes = self.lifetimes[-200:]

    def age(self, oid, tick):
        return max(0, tick - self.first.get(oid, tick))

    def relax_moves(self, oid):
        """Per-tick relaxation of the quote: >0 = moved toward its limit (ask down, bid up)."""
        h, sign = self.hist.get(oid, []), (-1 if self.side.get(oid) == "ask" else 1)
        return [sign * (b[1] - a[1]) for a, b in zip(h, h[1:])]

    def end_session(self, run):
        n = self.run_last.get(run, 0) - self.run_first.get(run, 0) + 1
        if n > 1:
            self.session_lens.append(n)
            self.session_lens = self.session_lens[-20:]

    def dump(self):
        return {"lifetimes": self.lifetimes, "session_lens": self.session_lens,
                "note": "observed facts only: lifetimes of traders that vanished unmatched; session lengths in ticks"}


def session_left(tr, run, tick, offers_of_run, sched_ticks):
    """(ticks left, source). Prefer the book's own expires_tick, then schedule, observation, default."""
    ex = [tr.expires[o["id"]] for o in offers_of_run if o["id"] in tr.expires]
    if ex:
        return max(0, min(ex) - tick), "book"
    total, src = None, None
    if sched_ticks:
        total, src = sched_ticks, "schedule"
    elif len(tr.session_lens) >= 2:
        total, src = int(sorted(tr.session_lens)[len(tr.session_lens) // 2]), "observed"
    else:
        total, src = SESSION_TICKS, "default"
    return max(0, total - (tick - tr.run_first.get(run, tick))), src


def classify(tr, oid, tick, total_len):
    """status: yielding | firm | unknown, plus urgent (may leave soon). Needs >= 2 observations, no guessing."""
    moves = tr.relax_moves(oid)
    age = tr.age(oid, tick)
    if len(moves) < 1:
        status = "unknown"
    elif moves[-1] > 0:
        status = "yielding"
    elif moves[-1] < 0 or (len(moves) >= 2 and moves[-2] == 0):
        status = "firm"                 # moved away, or flat for two observations
    else:
        status = "unknown"              # one flat observation is not enough
    if len(tr.lifetimes) >= MIN_LIFETIMES:
        cutoff = sorted(tr.lifetimes)[max(0, len(tr.lifetimes) // 4 - 1)]   # ~25th percentile of observed stays
        urgent, why = age >= max(1, cutoff), f"age {age} >= observed 25th pct {cutoff}"
    else:
        urgent, why = age >= URGENT_AGE_FRAC * total_len, f"age {age} >= {URGENT_AGE_FRAC} x session {total_len}"
    return {"status": status, "urgent": urgent, "age": age, "why": why}


def decide(tr, ask_o, bid_o, tick, left, total_len):
    """('MATCH'|'WAIT', reason) for one crossing pair. WAIT only when BOTH sides are observed relaxing and none is at risk;
    anything mixed or not yet observed (< 2 ticks) matches: we do not wait on a guess."""
    if left <= LATE_TICKS:
        return "MATCH", f"session ends in {left} ticks"
    a, b = classify(tr, ask_o["id"], tick, total_len), classify(tr, bid_o["id"], tick, total_len)
    for who, c in (("ask", a), ("bid", b)):
        if c["urgent"]:
            return "MATCH", f"{who} may leave ({c['why']})"
    if a["status"] == "yielding" and b["status"] == "yielding" and WAIT_ENABLED:
        return "WAIT", f"both still relaxing, {left} ticks left"
    return "MATCH", f"ask {a['status']}, bid {b['status']}: not both observed relaxing"


def max_pairs(asks, bids):
    """Most crossing pairs, and among those the widest total spread: the k cheapest asks with the k highest bids,
    both ascending, paired in order (ask_i <= bid_i for every i), for the largest feasible k. Every crossing pair adds
    real gains (bids sit under values, asks over costs), so more pairs never lose; zipping cheapest ask with highest bid
    can strand a pair (asks 28, 73 / bids 29, 76: zip gives 1 pair, this gives 2)."""
    a = sorted(asks, key=lambda o: o["want"]["cash"])
    b = sorted(bids, key=lambda o: -o["give"]["cash"])
    for k in range(min(len(a), len(b)), 0, -1):
        ca, cb = a[:k], sorted(b[:k], key=lambda o: o["give"]["cash"])
        if all(x["want"]["cash"] <= y["give"]["cash"] for x, y in zip(ca, cb)):
            return list(zip(ca, cb))
    return []


def bench_plan(book, tick, tr, sched_ticks=None, quiet=False):
    """[(sell id, buy id, price)] for pairs we decide to lock now. Highest bids against lowest asks, per run."""
    plan, runs = [], {}
    for o in book.get("bench_offers") or []:
        asks, bids = runs.setdefault(o["id"].split("-")[0], ([], []))
        (asks if o["want"]["cash"] else bids).append(o)
    for run, (asks, bids) in runs.items():
        left, src = session_left(tr, run, tick, asks + bids, sched_ticks)
        total = (tick - tr.run_first.get(run, tick)) + left
        for s, bu in max_pairs(asks, bids):
            ask, bid = s["want"]["cash"], bu["give"]["cash"]
            action, why = decide(tr, s, bu, tick, left, max(1, total))
            if not quiet:
                log(f"tick {tick} {run}: {s['id']}@{ask} x {bu['id']}@{bid} -> {action} ({why}; left from {src})")
            if action == "MATCH":
                plan.append((s["id"], bu["id"], (ask + bid) // 2))
    return plan


def schedule_ticks(broker):
    """Session length from the public schedule (bench events), None if not announced."""
    try:
        for ev in broker._call("GET", "/api/schedule").get("upcoming", []):
            if ev.get("action") == "bench" and ev.get("params", {}).get("ticks"):
                return int(ev["params"]["ticks"])
    except BazaarError:
        pass
    return None


# ---------------------------------------------------------------- live loop

def load_mem():
    try:
        with open(MEM_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def main():
    broker = Broker(URL, os.environ["BROKER_KEY"])
    tr, seen, sched, matched, t_sched = Track(load_mem()), None, None, set(), 0
    logged_keys = False
    relearn("startup")
    run_deadline, pending = {}, set()           # a run is over only after its offers' expires_tick, not when the book empties
    announce_state = {}
    while True:
        try:
            clock = broker.clock()
            tick, book = clock["tick"], broker.book()
            maybe_announce(broker, clock, announce_state)
            if not logged_keys:
                log("book keys:", sorted(book.keys()))
                logged_keys = True
            if time.time() - t_sched > 120:                       # the schedule drops past events: remember the last value
                sched, t_sched = schedule_ticks(broker) or sched, time.time()
            bench = book.get("bench_offers") or []
            for o in bench:
                if isinstance(o.get("expires_tick"), int):
                    r = o["id"].split("-")[0]
                    run_deadline[r] = max(run_deadline.get(r, 0), o["expires_tick"])
            before = set(tr.hist)
            tr.update(tick, bench, matched)
            for run in {i.split("-")[0] for i in before} - {i.split("-")[0] for i in tr.hist}:
                tr.end_session(run)
                with open(MEM_FILE, "w") as f:
                    json.dump(tr.dump(), f, indent=1)
                pending.add(run)
            live_runs = {o["id"].split("-")[0] for o in bench}
            for run in sorted(pending):
                if run not in live_runs and tick > run_deadline.get(run, 0):
                    pending.discard(run)
                    relearn(f"session {run} ended")
            now = (tick, [o["id"] for o in bench + (book.get("offers") or [])])
            if now != seen:                                       # decide again every tick, not every poll
                seen = now
                if bench:                                         # raw record of every bench book state, for offline analysis
                    with open("bench_snapshots.jsonl", "a") as f:
                        f.write(json.dumps({"tick": tick, "bench": bench, "recent": book.get("recent")}) + "\n")
                for sell, buy, price in bench_plan(book, tick, tr, sched) + public_plan(book):
                    try:
                        broker.match(sell, buy, price)
                        matched.update((sell, buy))
                        log(f"tick {tick}: MATCHED {sell} x {buy} at {price}")
                    except BazaarError as e:
                        log(f"tick {tick}: {sell} x {buy} at {price} refused ({e})")
        except BazaarError as e:
            log(f"cannot read the book ({e})")
        time.sleep(1.0)


# ---------------------------------------------------------------- offline simulation (made-up trader dynamics)

def _test_max_pairs():
    A = lambda i, p: {"id": f"b1-{i}", "want": {"cash": p}, "give": {"cash": 0}}
    B = lambda i, p: {"id": f"b1-{i}", "want": {"cash": 0}, "give": {"cash": p}}
    got = max_pairs([A(1, 28), A(2, 73), A(3, 87)], [B(4, 76), B(5, 29)])
    assert [(x["id"], y["id"]) for x, y in got] == [("b1-1", "b1-5"), ("b1-2", "b1-4")], got   # 2 pairs, not 1
    assert max_pairs([A(1, 50)], [B(2, 40)]) == []
    assert len(max_pairs([A(1, 10), A(2, 20), A(3, 30)], [B(4, 35), B(5, 25), B(6, 15)])) == 3
    for x, y in max_pairs([A(1, 10), A(2, 60), A(3, 30)], [B(4, 35), B(5, 70), B(6, 5)]):
        assert x["want"]["cash"] <= y["give"]["cash"]


def _simulate(policy, seed, n=10, ticks=16):
    """Synthetic session: n sellers + n buyers, quotes shaded away from hidden limits, firm or relaxing, some leaving early."""
    import random
    rnd = random.Random(seed)
    T = []
    for i in range(2 * n):
        ask = i < n
        limit = rnd.uniform(20, 60) if ask else rnd.uniform(40, 80)
        T.append({"id": f"b1-{i}", "ask": ask, "limit": limit, "shade": rnd.uniform(5, 25),
                  "rate": 0 if rnd.random() < 0.3 else rnd.uniform(1, 3), "leave": rnd.choice([5, 8, 12, 16, 16, 16]),
                  "q": None})
    sellers, buyers = sorted(t["limit"] for t in T if t["ask"]), sorted((t["limit"] for t in T if not t["ask"]), reverse=True)
    possible = sum(b - s for s, b in zip(sellers, buyers) if b > s)
    tr, matched, gain = Track(), set(), 0.0
    for t in range(ticks):
        offers = []
        for o in T:
            if o["id"] in matched or t >= o["leave"]:
                continue
            shade = max(0.0, o["shade"] - o["rate"] * t)
            q = round(o["limit"] + shade) if o["ask"] else round(o["limit"] - shade)
            o["q"] = q
            offers.append({"id": o["id"], "want": {"cash": q} if o["ask"] else {"cash": 0},
                           "give": {"cash": 0} if o["ask"] else {"cash": q}, "expires_tick": ticks})
        tr.update(t, offers, matched)
        book = {"bench_offers": offers}
        for sell, buy, price in policy(book, t, tr):
            if sell in matched or buy in matched:
                continue
            s = next(o for o in T if o["id"] == sell)
            b = next(o for o in T if o["id"] == buy)
            matched.update((sell, buy))
            gain += max(0.0, b["limit"] - s["limit"])
    return gain / possible if possible else 0.0


def _test_announce():
    class B:
        def __init__(self): self.sent = []
        def announce(self, text): self.sent.append(text)
    b, st = B(), {}
    assert maybe_announce(b, {"t_hours": 9.0}, st) and len(b.sent) == 1
    assert not maybe_announce(b, {"t_hours": 9.2}, st), "less than 20 min later: quiet"
    assert maybe_announce(b, {"t_hours": 9.34}, st) and b.sent[0] != b.sent[1], "rotated text"
    assert not maybe_announce(b, {"t_hours": 12.0, "paused": True}, st), "doors closed: quiet"


def selftest():
    _test_max_pairs()
    _test_announce()
    # unit checks of the decision rules
    tr = Track()
    def offs(t, aq, bq):
        return [{"id": "b1-1", "want": {"cash": aq}, "give": {"cash": 0}, "expires_tick": 16},
                {"id": "b1-2", "want": {"cash": 0}, "give": {"cash": bq}, "expires_tick": 16}]
    tr.update(0, offs(0, 50, 55))
    s, b = offs(0, 50, 55)
    assert decide(tr, s, b, 0, 15, 16)[0] == "MATCH", "first sight: too few observations, do not wait on a guess"
    tr.update(1, offs(1, 48, 56))
    if WAIT_ENABLED:
        assert decide(tr, s, b, 1, 14, 16)[0] == "WAIT", "both observed relaxing: wait (and not match merely because time passed)"
    tr.update(2, offs(2, 48, 56)); tr.update(3, offs(3, 48, 56))
    assert decide(tr, s, b, 3, 12, 16)[0] == "MATCH", "both flat for 2 observations: match"
    assert decide(tr, s, b, 3, 2, 16)[0] == "MATCH", "session about to end: match"
    tr2 = Track(); tr2.update(0, offs(0, 50, 55)); tr2.update(1, offs(1, 49, 56))
    s2, b2 = offs(1, 49, 56)
    tr2.update(9, offs(9, 45, 60))
    assert decide(tr2, s2, b2, 9, 7, 16)[0] == "MATCH", "old trader (>= half the session): match before it leaves"
    # policy comparison on synthetic sessions
    def immediate(book, t, tr):                       # the starter: match every crossing pair at once
        return bench_plan_naive(book)
    def smart(book, t, tr):
        return bench_plan(book, t, tr, ticks_arg(), quiet=True)
    ticks_arg = lambda: 16
    seeds = range(300)
    e_imm = sum(_simulate(immediate, s) for s in seeds) / len(seeds)
    e_smart = sum(_simulate(smart, s) for s in seeds) / len(seeds)
    print(f"efficiency over {len(seeds)} synthetic sessions: match-at-once {e_imm:.3f} | this broker {e_smart:.3f}")
    print("selftest OK (the simulation uses made-up dynamics: it checks the logic, not real-world performance)")


def bench_plan_naive(book):
    """The starter's rule, for comparison: highest bid against lowest ask while they cross."""
    plan, runs = [], {}
    for o in book.get("bench_offers") or []:
        asks, bids = runs.setdefault(o["id"].split("-")[0], ([], []))
        (asks if o["want"]["cash"] else bids).append(o)
    for asks, bids in runs.values():
        for s, b in zip(sorted(asks, key=lambda o: o["want"]["cash"]), sorted(bids, key=lambda o: -o["give"]["cash"])):
            if b["give"]["cash"] < s["want"]["cash"]:
                break
            plan.append((s["id"], b["id"], (s["want"]["cash"] + b["give"]["cash"]) // 2))
    return plan


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    else:
        main()
