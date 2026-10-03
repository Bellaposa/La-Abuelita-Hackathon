"""Learn from every Market Test we recorded, and pick the broker's waiting policy by replaying real sessions.

    python3 bench_learn.py              # learn from bench_snapshots.jsonl + broker.log, write bench_model.json
    python3 bench_learn.py --selftest   # offline checks

What the Market Test scores: realised gains between the traders' TRUE limits / the best possible. The price inside
[ask, bid] does not matter; a pair can only be locked while its QUOTES cross. So the broker controls two things:
WHICH crossing pairs it locks (broker.max_pairs: the most pairs, widest spread) and WHEN (lock now, or wait for quotes to
relax so more pairs cross, at the risk that a trader leaves). This module measures, on our own recordings:

  per trader   side, quote path, relax per tick, first/last tick seen, matched by us or left on its own
  per session  traders, our matched pairs, crossings we missed (should be 0), estimated efficiency per policy
  model        relax rate per side, lifetime of traders that left unmatched, best HOLD (ticks to wait) by replay

Limits are hidden: a trader's limit is estimated as its most relaxed observed quote, pushed by its own relax rate over
the ticks it would still have lived. It is an estimate; the replay ranks policies, it does not predict the score.
"""
import json
import re
import statistics
import sys

SNAPS = "bench_snapshots.jsonl"
LOG = "broker.log"
MODEL = "bench_model.json"
HOLDS = (0, 1, 2, 3)          # policies to compare: wait up to H ticks for a pair to widen before locking it


def load_sessions(snaps_path=SNAPS, log_path=LOG):
    """run -> {"traders": id -> {side, path [(tick, quote)], first, last}, "matched": {id: tick}, "ticks": [..]}"""
    runs = {}
    try:
        lines = open(snaps_path, encoding="utf-8").read().splitlines()
    except OSError:
        lines = []
    for line in lines:
        try:
            snap = json.loads(line)
        except ValueError:
            continue
        t = snap["tick"]
        for o in snap.get("bench") or []:
            run = o["id"].split("-")[0]
            r = runs.setdefault(run, {"traders": {}, "matched": {}, "ticks": set()})
            r["ticks"].add(t)
            ask = bool((o.get("want") or {}).get("cash"))
            q = o["want"]["cash"] if ask else o["give"]["cash"]
            tr = r["traders"].setdefault(o["id"], {"side": "ask" if ask else "bid", "path": [], "first": t, "last": t})
            if not tr["path"] or tr["path"][-1][0] != t:
                tr["path"].append([t, q])
            else:
                tr["path"][-1][1] = q
            tr["last"] = max(tr["last"], t)
    try:
        for line in open(log_path, encoding="utf-8", errors="replace"):
            m = re.search(r"tick (\d+): MATCHED (b\d+-\d+) x (b\d+-\d+)", line)
            if m:
                run = m.group(2).split("-")[0]
                if run in runs:
                    runs[run]["matched"][m.group(2)] = int(m.group(1))
                    runs[run]["matched"][m.group(3)] = int(m.group(1))
    except OSError:
        pass
    for r in runs.values():
        r["ticks"] = sorted(r["ticks"])
    return runs


def relax_per_tick(tr):
    """Average move toward the trader's limit per tick (ask down, bid up); None with < 2 observations."""
    p = tr["path"]
    if len(p) < 2 or p[-1][0] == p[0][0]:
        return None
    sign = -1 if tr["side"] == "ask" else 1
    return sign * (p[-1][1] - p[0][1]) / (p[-1][0] - p[0][0])


def fit_model(runs):
    rel = {"ask": [], "bid": []}
    life = []
    for r in runs.values():
        end = r["ticks"][-1] if r["ticks"] else None
        for tid, tr in r["traders"].items():
            v = relax_per_tick(tr)
            if v is not None:
                rel[tr["side"]].append(v)
            if tid not in r["matched"] and tr["last"] != end:          # left on its own before the session ended
                life.append(tr["last"] - tr["first"] + 1)
    med = lambda xs, d: statistics.median(xs) if xs else d
    return {"relax": {s: round(med(v, 0.0), 2) for s, v in rel.items()}, "relax_n": {s: len(v) for s, v in rel.items()},
            "lifetime": med(life, 6), "lifetime_n": len(life)}


def trader_timeline(tr, matched_at, model, session_end):
    """quote(t) for every tick the trader is (or would have been) present, and its estimated limit.
    Observed ticks as recorded; after we matched it, extrapolate with its own relax rate (else the side median) until
    its estimated departure (first + model lifetime, at least what we saw)."""
    rate = relax_per_tick(tr)
    rate = model["relax"][tr["side"]] if rate is None else max(0.0, rate)
    sign = -1 if tr["side"] == "ask" else 1
    obs = {t: q for t, q in tr["path"]}
    if matched_at is None:
        leave = tr["last"] + 1
    else:
        leave = max(tr["last"] + 1, tr["first"] + int(round(model["lifetime"])))
    leave = min(leave, session_end + 1)
    out, last_t, last_q = {}, None, None
    for t in range(tr["first"], leave):
        if t in obs:
            last_t, last_q = t, obs[t]
            out[t] = obs[t]
        elif last_q is not None:
            out[t] = last_q + sign * rate * (t - last_t)
    limit = (min if tr["side"] == "ask" else max)(out.values()) if out else None
    return out, limit


def replay(run, model, hold):
    """Replay one recorded session with the broker locking crossing pairs (most pairs, widest spread), but holding a
    pair while both traders have more than `hold` ticks of expected life left. Returns (est. gain, est. best, pairs)."""
    end = run["ticks"][-1]
    tl, lim = {}, {}
    for tid, tr in run["traders"].items():
        tl[tid], lim[tid] = trader_timeline(tr, run["matched"].get(tid), model, end)
    leave = {tid: (max(q) + 1 if q else 0) for tid, q in tl.items()}
    side = {tid: tr["side"] for tid, tr in run["traders"].items()}
    used, gain, pairs = set(), 0.0, 0
    for t in range(run["ticks"][0], end + 1):
        asks = sorted((tl[i][t], i) for i in tl if t in tl[i] and side[i] == "ask" and i not in used)
        bids = sorted(((tl[i][t], i) for i in tl if t in tl[i] and side[i] == "bid" and i not in used), reverse=True)
        k = 0
        for kk in range(min(len(asks), len(bids)), 0, -1):
            ca, cb = asks[:kk], sorted(bids[:kk])
            if all(a[0] <= b[0] for a, b in zip(ca, cb)):
                k = kk
                break
        if not k:
            continue
        ca, cb = asks[:k], sorted(bids[:k])
        for (aq, ai), (bq, bi) in zip(ca, cb):
            near = min(leave[ai], leave[bi]) - t <= hold or t >= end - hold
            if hold == 0 or near:
                used |= {ai, bi}
                gain += max(0.0, lim[bi] - lim[ai])
                pairs += 1
    # best possible: the efficient set on the estimated limits (k lowest-cost sellers vs k highest-value buyers)
    a_l = sorted(lim[i] for i in lim if side[i] == "ask" and lim[i] is not None)
    b_l = sorted((lim[i] for i in lim if side[i] == "bid" and lim[i] is not None), reverse=True)
    best = sum(max(0.0, b - a) for a, b in zip(a_l, b_l))
    return gain, best, pairs


def learn(snaps_path=SNAPS, log_path=LOG, model_path=MODEL, write=True):
    runs = load_sessions(snaps_path, log_path)
    runs = {k: v for k, v in runs.items() if v["traders"]}
    model = fit_model(runs)
    report, totals = {}, {h: [0.0, 0.0] for h in HOLDS}
    for name, run in runs.items():
        missed = 0
        for t in run["ticks"]:                                         # crossings we left on the table (should be 0)
            q = {i: next((x for tt, x in tr["path"] if tt == t), None) for i, tr in run["traders"].items()}
            live = {i: v for i, v in q.items() if v is not None and run["matched"].get(i, 10 ** 9) > t}
            a = [v for i, v in live.items() if run["traders"][i]["side"] == "ask"]
            b = [v for i, v in live.items() if run["traders"][i]["side"] == "bid"]
            if a and b and min(a) <= max(b) and t not in run["matched"].values():
                missed += 1
        row = {"traders": len(run["traders"]), "our_pairs": len(run["matched"]) // 2, "ticks_with_unlocked_cross": missed}
        for h in HOLDS:
            g, best, p = replay(run, model, h)
            row[f"hold{h}"] = {"pairs": p, "est_eff": round(g / best, 3) if best else None}
            totals[h][0] += g
            totals[h][1] += best
        report[name] = row
    est = {h: (round(g / b, 3) if b else None) for h, (g, b) in totals.items()}
    best_hold = max(HOLDS, key=lambda h: (est[h] or 0, -h))          # ties go to the shorter wait (less risk)
    model.update({"sessions": report, "est_eff_by_hold": est, "best_hold": best_hold, "n_sessions": len(runs)})
    if write:
        with open(model_path, "w") as f:
            json.dump(model, f, indent=1)
    return model


def selftest():
    import os
    import tempfile
    d = tempfile.mkdtemp()
    snaps, log = os.path.join(d, "s.jsonl"), os.path.join(d, "b.log")
    A = lambda i, p: {"id": f"b9-{i}", "want": {"cash": p}, "give": {"cash": 0}}
    B = lambda i, p: {"id": f"b9-{i}", "want": {"cash": 0}, "give": {"cash": p}}
    with open(snaps, "w") as f:   # seller 1 relaxes 60 -> 48 and stays; buyer 2 rises 40 -> 52; they cross at tick 4
        for t in range(1, 7):
            f.write(json.dumps({"tick": t, "bench": [A(1, 60 - 3 * (t - 1)), B(2, 40 + 3 * (t - 1)), A(3, 90)]}) + "\n")
    with open(log, "w") as f:
        f.write("12:00:00 tick 4: MATCHED b9-1 x b9-2 at 50\n")
    m = learn(snaps, log, os.path.join(d, "m.json"))
    assert m["relax"]["ask"] == 1.5 and m["relax"]["bid"] == 3.0, m["relax"]   # trader 3 never moves: median of 0 and 3
    assert m["sessions"]["b9"]["our_pairs"] == 1 and m["sessions"]["b9"]["hold0"]["pairs"] == 1
    assert m["best_hold"] in HOLDS
    print("bench_learn selftest OK")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    else:
        m = learn()
        print(json.dumps({k: m[k] for k in ("relax", "relax_n", "lifetime", "lifetime_n", "est_eff_by_hold", "best_hold",
                                            "n_sessions")}, indent=1))
        for name, row in m["sessions"].items():
            print(name, row)
