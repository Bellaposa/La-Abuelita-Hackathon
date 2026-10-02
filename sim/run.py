"""Run agents against the offline simulator and print a score table.

    python -m sim.run --scenario all --seeds 3
    python -m sim.run --scenario duel --rival trickster --seeds 20 --agent mypkg.mymod:my_step

Plug-in point: an agent is a callable  step(env, ctx) -> None  called once per tick.
  env  FakeBazaar (dealer / duel scenarios) or FakeBroker (broker scenario); same method names as the SDK.
  ctx  dict that persists for the whole episode: ctx["scenario"], ctx["task"], ctx["mem"] (free scratch dict).
After each step the runner advances the world one tick. Raise nothing; BazaarError you do not catch is
counted and the episode continues.

    from sim.run import run_scenario
    res = run_scenario(my_step, "duel", rival="patient", seed=3)   # -> {"score": 0.41, ...}
"""
from __future__ import annotations

import argparse
import importlib
import statistics
import sys
import traceback

from .rivals import RIVALS
from .world import BazaarError, FakeBazaar, FakeBroker, PACKS

DEALER_RIVALS = ["abuela", "chato"] + list(RIVALS)


# ======================================================================================== baseline agents
def _hers(t, dealer):
    return [o for o in t["standing_offers"] if o["maker"] == dealer and o["status"] == "open"]


def _ask(o):
    return o["want"]["cash"] or o["give"]["cash"]


def starter_dealer(b, ctx):
    """starter_agent.py logic: opens at 60 % of budget, +2 per round, accepts ask <= offer+1 or a final within budget."""
    m, task = ctx["mem"], ctx["task"]
    if "tid" not in m:
        m["budget"] = min(b.me()["cash"], task["budget"])
        m["offer"] = int(m["budget"] * 0.6)
        m["tid"] = b.open_thread(task["dealer"], topic=task["topic"])["id"]
        return
    t = b.thread(m["tid"])
    if t["status"] != "open":
        return
    hers = _hers(t, task["dealer"])
    if not hers:
        return
    ask = _ask(hers[-1])
    if ask <= min(m["budget"], m["offer"] + 1) or (hers[-1].get("final") and ask <= m["budget"]):
        b.accept(hers[-1]["id"])
    else:
        b.say(t["id"], f"Hola! Would {m['offer']} P be all right? Thank you very much.", price=m["offer"])
        m["offer"] = min(m["budget"], m["offer"] + 2)


def stubborn_dealer(b, ctx):
    """Repeats the same price every tick (the mistake that earned no_progress / cooloff)."""
    m, task = ctx["mem"], ctx["task"]
    if "tid" not in m:
        m["tid"] = b.open_thread(task["dealer"], topic=task["topic"])["id"]
        m["price"] = int(task["budget"] * 0.6)
        return
    t = b.thread(m["tid"])
    if t["status"] != "open":
        return
    hers = _hers(t, task["dealer"])
    if hers and _ask(hers[-1]) <= m["price"]:
        b.accept(hers[-1]["id"])
    elif hers:
        b.say(t["id"], f"{m['price']} primas.", price=m["price"])


def stepper_dealer(b, ctx):
    """Small honest steps (+3, +2, +1...), never repeats, accepts ask <= bid+1 or any final under budget."""
    m, task = ctx["mem"], ctx["task"]
    if "tid" not in m:
        m["tid"] = b.open_thread(task["dealer"], topic=task["topic"])["id"]
        m["bid"], m["step"] = int(task["budget"] * 0.42), 4
        return
    t = b.thread(m["tid"])
    if t["status"] != "open":
        return
    hers = _hers(t, task["dealer"])
    if not hers:
        return
    ask = _ask(hers[-1])
    if ask <= m["bid"] + 1 or (hers[-1].get("final") and ask <= task["budget"]):
        b.accept(hers[-1]["id"])
        return
    m["bid"] = min(task["budget"] - 1, m["bid"] + m["step"])
    m["step"] = max(1, m["step"] - 1)
    b.say(t["id"], f"Hola! How about {m['bid']}? Gracias.", price=m["bid"])


def _utility_side(d):
    return 1 if d["role"] == "seller" else -1


def linear_duel(b, ctx):
    """Concedes linearly from an extreme opening to a 5 % margin at the deadline; accepts when the rival's standing
    offer is at least as good as our next planned price."""
    for d in b.duels()["duels"]:
        side, lim, T, t = _utility_side(d), float(d["your_limit"]), d["duel_ticks"], d["round"]
        use_days = "days" in d["issues"]
        ro = d["rival_offer"] or {}
        rp = ro.get("price")
        start, resv = lim * (1 + side * 0.6), lim * (1 + side * 0.05)
        plan = start + (resv - start) * min(1.0, t / max(1, T - 1))
        plan = int(round(plan))
        if rp is not None and side * (rp - plan) >= 0 and side * (rp - lim) > 0:
            b.duel_accept(d["id"])
        elif rp is not None and t >= T - 1 and side * (rp - lim) >= 0:
            b.duel_accept(d["id"])
        else:
            if d.get("your_offer") and d["your_offer"]["price"] == plan:
                plan += -side  # never repeat a price
            b.duel_say(d["id"], f"I can do {plan}.", price=plan, days=5 if use_days else None)


def stubborn_duel(b, ctx):
    """Holds a greedy line and accepts only at the very end."""
    for d in b.duels()["duels"]:
        side, lim, T, t = _utility_side(d), float(d["your_limit"]), d["duel_ticks"], d["round"]
        use_days = "days" in d["issues"]
        rp = (d["rival_offer"] or {}).get("price")
        if rp is not None and t >= T - 1 and side * (rp - lim) >= 0:
            b.duel_accept(d["id"])
        elif t == 0:
            p = int(lim * (1 + side * 0.5))
            b.duel_say(d["id"], f"{p}, final.", price=p, days=5 if use_days else None)


def bench_plan(book):
    """Copy of starter_broker.bench_plan: cross by quote at the midpoint, run by run."""
    plan, runs = [], {}
    for o in book.get("bench_offers") or []:
        asks, bids = runs.setdefault(o["id"].split("-")[0], ([], []))
        if o["want"]["cash"]:
            asks.append((o["want"]["cash"], o["id"]))
        else:
            bids.append((o["give"]["cash"], o["id"]))
    for asks, bids in runs.values():
        for (ask, sell), (bid, buy) in zip(sorted(asks, key=lambda a: a[0]), sorted(bids, key=lambda b: -b[0])):
            if bid < ask:
                break
            plan.append((sell, buy, (ask + bid) // 2))
    return plan


def starter_broker(br, ctx):
    """starter_broker.py: cross the bench book by quote every tick."""
    for sell, buy, price in bench_plan(br.book()):
        try:
            br.match(sell, buy, price)
        except BazaarError:
            pass


def wait_broker(br, ctx):
    """Waits for quotes to relax (until tick 8), then crosses by quote like the starter."""
    if br.clock()["tick"] >= 8:
        starter_broker(br, ctx)


AGENTS = {
    "dealer": {"starter": starter_dealer, "stubborn": stubborn_dealer, "stepper": stepper_dealer},
    "duel": {"linear": linear_duel, "stubborn": stubborn_duel},
    "broker": {"starter": starter_broker, "wait8": wait_broker},
}


# ======================================================================================== scenarios
def _drive(env, agent_step, ctx, max_ticks, finished):
    errors, crash = 0, None
    for _ in range(max_ticks):
        try:
            agent_step(env, ctx)
        except BazaarError as e:
            errors += 1
            ctx.setdefault("errors", []).append(e.code)
        except Exception:  # an agent bug: stop the episode, score what is there
            crash = traceback.format_exc(limit=3)
            break
        env.advance()
        if finished():
            break
    return errors, crash


def run_dealer(agent_step, rival="abuela", seed=0, max_ticks=60, **kw):
    if rival == "chato":
        env = FakeBazaar(seed, chato_unlocked=True, market=False, **kw)
        task = dict(dealer="chato", topic={"buy": {"pack": "sobre_plata"}}, list=PACKS["sobre_plata"]["list"])
    else:
        env = FakeBazaar(seed, dealer_style=rival, market=False, **kw)
        task = dict(dealer="abuela", topic={"buy": {"pack": "sobre_barrio"}}, list=PACKS["sobre_barrio"]["list"])
    task["budget"] = int(task["list"] * 1.1)  # the most we would pay (proxy for our private value)
    ctx = {"scenario": "dealer", "task": task, "mem": {}}

    def finished():
        return any(d["side"] == "buy" for d in env.deals) or all(
            t["status"] != "open" for t in env.threads.values()) and env.threads and not env._pending

    errors, crash = _drive(env, agent_step, ctx, max_ticks, finished)
    shares = []
    for d in env.deals:
        span = max(1, d["open"] - d["floor"])
        shares.append(min(1.0, max(0.0, (d["open"] - d["price"]) / span)))
    th = list(env.threads.values())
    return {"score": max(shares) if shares else 0.0, "deal": bool(shares), "ticks": env.tick,
            "price": env.deals[0]["price"] if env.deals else None, "errors": errors, "crash": crash,
            "end": (th[-1]["status"] + ":" + str(th[-1]["closed_reason"])) if th else "no thread"}


def run_duel(agent_step, rival="aggressor", seed=0, issues=("price",), ticks=16, max_ticks=None, **kw):
    scores, deals, detail = [], 0, []
    errors, crash = 0, None
    for k, role in enumerate(("seller", "buyer")):
        env = FakeBazaar(seed * 2 + k, market=False,
                         duel=dict(role=role, rival=rival, issues=issues, ticks=ticks), **kw)
        ctx = {"scenario": "duel", "task": {"role": role, "issues": list(issues)}, "mem": {}}
        d = env.duel_list[0]
        e, c = _drive(env, agent_step, ctx, (max_ticks or ticks) + 3, lambda: d["status"] != "open")
        errors, crash = errors + e, crash or c
        res = d["result"] or {}
        scores.append(res.get("your_share") or 0.0)
        deals += d["status"] == "done"
        detail.append(res.get("price"))
    return {"score": statistics.mean(scores), "deal": deals == 2, "deals": deals, "ticks": ticks, "errors": errors,
            "crash": crash, "prices": detail}


def run_broker(agent_step, rival="all", seed=0, max_ticks=None, **kw):
    env = FakeBroker(seed, style=rival, **kw)
    ctx = {"scenario": "broker", "task": {"session_ticks": env.session_ticks}, "mem": {}}
    errors, crash = _drive(env, agent_step, ctx, max_ticks or env.session_ticks + 2, env.done)
    r = env.result()
    return {"score": r["efficiency"], "deal": r["matches"] > 0, "matches": r["matches"], "ticks": env.tick,
            "errors": errors, "crash": crash}


def run_scenario(agent_step, scenario="dealer", rival=None, seed=0, **kw):
    """Run one episode. agent_step(env, ctx) is called once per tick. Returns a dict with 'score' (value captured
    share: dealer = share of the dealer's price range, duel = share of the initial pie after round shrinkage averaged
    over seller and buyer roles, broker = Market Test efficiency) plus diagnostics."""
    fn = {"dealer": run_dealer, "duel": run_duel, "broker": run_broker}[scenario]
    default = {"dealer": "abuela", "duel": "aggressor", "broker": "all"}[scenario]
    return fn(agent_step, rival or default, seed, **kw)


def run_suite(agents, scenarios, rivals, seeds, **kw):
    """agents: {scenario: {name: step}}. rivals: name list or 'all'. Returns rows of dicts."""
    rows = []
    for sc in scenarios:
        pool = {"dealer": DEALER_RIVALS, "duel": list(RIVALS), "broker": list(RIVALS)}[sc]
        rv = pool if rivals in (None, "all") else [r for r in rivals if r in pool]
        if sc == "broker" and rivals in (None, "all"):
            rv = ["all"] + list(RIVALS)
        for name, fn in agents.get(sc, {}).items():
            for r in rv:
                res = [run_scenario(fn, sc, r, s, **kw.get(sc, {})) for s in range(seeds)]
                rows.append(dict(scenario=sc, agent=name, rival=r, n=seeds,
                                 mean=statistics.mean(x["score"] for x in res),
                                 lo=min(x["score"] for x in res), hi=max(x["score"] for x in res),
                                 deal=sum(bool(x["deal"]) for x in res) / seeds,
                                 errors=sum(x["errors"] for x in res), crashes=sum(bool(x["crash"]) for x in res),
                                 crash=next((x["crash"] for x in res if x["crash"]), None)))
    return rows


def print_table(rows, out=sys.stdout):
    for sc in dict.fromkeys(r["scenario"] for r in rows):
        sub = [r for r in rows if r["scenario"] == sc]
        print(f"\n=== {sc.upper()}  (score = value captured share; higher is better) ===", file=out)
        rivals = list(dict.fromkeys(r["rival"] for r in sub))
        agents = list(dict.fromkeys(r["agent"] for r in sub))
        w = max(10, max(len(a) for a in agents) + 1)
        print("rival".ljust(11) + "".join(a.rjust(w + 12) for a in agents), file=out)
        for rv in rivals:
            line = rv.ljust(11)
            for a in agents:
                r = next((x for x in sub if x["rival"] == rv and x["agent"] == a), None)
                line += (f"{r['mean']:.3f} ({r['deal']:.0%}){'!' if r['crashes'] else ''}".rjust(w + 12) if r else "-".rjust(w + 12))
            print(line, file=out)
        line = "AVERAGE".ljust(11)
        for a in agents:
            xs = [x["mean"] for x in sub if x["agent"] == a]
            line += f"{statistics.mean(xs):.3f}".rjust(w + 12)
        print(line, file=out)
        print("cell = mean score (deal rate); '!' = agent crashed in some seed", file=out)
        for r in sub:
            if r["crash"]:
                print(f"  CRASH {r['agent']} vs {r['rival']}:\n{r['crash']}", file=out)
                break


def _load(spec):
    mod, _, fn = spec.partition(":")
    return getattr(importlib.import_module(mod), fn or "step")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m sim.run")
    ap.add_argument("--scenario", default="all", choices=["dealer", "duel", "broker", "all"])
    ap.add_argument("--rival", default="all", help="abuela|chato|aggressor|softie|trickster|patient|erratic|all")
    ap.add_argument("--seeds", type=int, default=3)
    ap.add_argument("--agent", action="append", default=[], help="module:function step(env, ctx); repeatable. "
                    "Added to the baselines of the chosen scenario(s)")
    ap.add_argument("--days", action="store_true", help="duels negotiate price and days")
    ap.add_argument("--no-baselines", action="store_true")
    a = ap.parse_args(argv)
    scenarios = ["dealer", "duel", "broker"] if a.scenario == "all" else [a.scenario]
    agents = {sc: ({} if a.no_baselines else dict(AGENTS[sc])) for sc in scenarios}
    for spec in a.agent:
        for sc in scenarios:
            agents[sc][spec] = _load(spec)
    rivals = "all" if a.rival == "all" else [a.rival]
    kw = {"duel": {"issues": ("price", "days")}} if a.days else {}
    print_table(run_suite(agents, scenarios, rivals, a.seeds, **kw))


if __name__ == "__main__":
    main()
