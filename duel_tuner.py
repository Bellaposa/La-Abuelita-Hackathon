"""Tune smart_duels' own parameters from our finished duels.

    python3 duel_tuner.py --selftest
    python3 duel_tuner.py                  # fit rivals from duels_memory.json, grid-search, write duel_params.json

The server scores a duel as our utility x (1 - decay) ^ rounds (checked on Saturday: duel 5770, (71 - 58 - 4.43 x 9) x
0.92^3 = -20.9; duel 5889, -30.7 x 0.92^5 = -20.2). We cannot replay a duel with another strategy, because the rival
would have answered differently, so:

  1. every finished duel becomes a RIVAL PROFILE, relative to our limit L: where it opened, how much it moved per round,
     how often it stood still or stayed silent, and how far its limit reached (its best offer, or the deal price when
     it took ours);
  2. a simulated rival is drawn from those profiles (bootstrap) and plays smart_duels.act, the real negotiator,
     for a grid of OUR parameters (SPEED_HORIZON, BETA, OPEN_ANCHOR, OFFER_KEEP) under given conditions
     (ticks, decay: Duels III is announced as a shorter clock and a harder decay);
  3. the parameters with the best mean score win, per role, and are written to duel_params.json, which smart_duels
     reads at start. A setting only replaces the current one if it beats it by MIN_GAIN in the simulation.

The simulated rival accepts one of our offers when it is inside its limit and no worse for it than what it would offer
itself next round, or when two ticks are left; otherwise it concedes its observed rate toward its limit.
"""
import json
import random
import statistics
import sys

import smart_duels as sd

PARAMS_FILE = "duel_params.json"
GRID = {"SPEED_HORIZON": [3, 4, 5, 6, 8, 10], "BETA": [0.8, 1.0, 1.5, 2.0], "OPEN_ANCHOR": [0.2, 0.35, 0.5, 0.7],
        "OFFER_KEEP": [0.05, 0.10, 0.20]}
CONDITIONS = {"duels2": (16, 0.08), "duels3": (12, 0.12)}     # (ticks, decay); Duels III: "shorter clock, harder decay"
MIN_GAIN = 0.03                                                # relative improvement needed to replace current params
STRETCH = {"seller": 0.08, "buyer": 0.08}       # how far past its best offer a rival's limit may lie, calibrated per role
STRETCH_GRID = [0.0, 0.08, 0.15, 0.25, 0.35, 0.5, 0.7]
DUELS1_PARAMS = {"SPEED_HORIZON": 16, "BETA": 2.0, "OPEN_ANCHOR": 0.5, "OFFER_KEEP": 0.10}   # what played Duels I


def real_score(mem, role, price_only=True):
    """What we really scored per 100 of limit in finished duels of this role (no deal = 0)."""
    rows = [f for f in (mem.get("finished") or {}).values()
            if f.get("role") == role and f.get("status") in ("deal", "no_deal") and f.get("your_limit")
            and ((f.get("issues") or ["price"]) == ["price"]) == price_only]
    return (statistics.mean((f.get("result") or 0) / f["your_limit"] * 100 for f in rows), len(rows)) if rows else (None, 0)


def calibrate(mem, profs, n=300, seed=11):
    """Pick STRETCH per role so the simulator, playing Duels I's parameters under Duels I's conditions, scores what we
    really scored in Duels I (price only, 16 ticks, decay 0.06)."""
    out = {}
    for role in ("seller", "buyer"):
        real, k = real_score(mem, role)
        if real is None:
            continue
        best = None
        for s in STRETCH_GRID:
            STRETCH[role] = s
            sim = play(DUELS1_PARAMS, profs, role, 16, 0.06, n, seed)
            if sim is not None and (best is None or abs(sim - real) < best[1]):
                best = (s, abs(sim - real), sim)
        STRETCH[role] = best[0]
        out[role] = {"stretch": best[0], "sim": round(best[2], 2), "real": round(real, 2), "duels": k}
    return out


def profiles(mem):
    """Rival profiles from finished duels (relative to our limit L)."""
    out = []
    for did, rec in (mem.get("duels") or {}).items():
        o = rec.get("observed") or {}
        L, role = o.get("limit"), o.get("role")
        hist = o.get("history") or []
        if not L or role not in ("seller", "buyer") or not hist:
            continue
        side = sd.side_of(role)                                # +1: we sell (rival buys)
        prices = [h["rival_price"] for h in hist if h.get("rival_price") is not None]
        silent = sum(1 for h in hist if h.get("rival_price") is None) / len(hist)
        if not prices:
            out.append({"role": role, "open": None, "rate": 0.0, "limit": None, "silent": 1.0, "firm": 1.0})
            continue
        moves = [side * (b - a) for a, b in zip(prices, prices[1:])]     # > 0: toward us
        rate = max(0.0, statistics.mean(moves)) if moves else 0.0
        firm = (sum(1 for m in moves if abs(m) < 0.5) / len(moves)) if moves else 1.0
        fin = (mem.get("finished") or {}).get(did) or {}
        best = max(prices) if side > 0 else min(prices)               # best for us the rival ever offered
        deal = fin.get("price") if fin.get("status") == "deal" else None
        lim = best if deal is None else (max(best, deal) if side > 0 else min(best, deal))
        out.append({"role": role, "open": prices[0] / L, "rate": rate / L, "limit": lim / L, "silent": silent, "firm": firm})
    return out


class Rival:
    """A Bazaar stand-in for one duel against a profile; records the score as the server computes it."""

    def __init__(self, role, L, prof, ticks, decay, rng):
        self.role, self.L, self.ticks, self.decay, self.rng = role, L, ticks, decay, rng
        side = sd.side_of(role)
        lim = prof["limit"] if prof["limit"] is not None else (1 + side * rng.uniform(0.05, 0.4))
        lim *= 1 + side * rng.uniform(0.0, STRETCH.get(role, 0.08))     # its true limit lies past its best offer
        self.rl = lim * L
        op = prof["open"] if prof["open"] is not None else (1 - side * rng.uniform(0.2, 0.5))
        self.rp = op * L
        self.rate, self.silent, self.firm = prof["rate"] * L, prof["silent"], prof["firm"]
        self.tick, self.rounds, self.result, self.done = 0, 0, 0.0, False
        self.spoke = False

    def _better_for_rival(self, a, b):                                  # is price a at least as good as b for the rival?
        return a <= b if self.role == "seller" else a >= b              # rival buys when we sell

    def duels(self, done=False):
        if self.done or done or self.tick >= self.ticks:
            return {"duels": []}
        return {"duels": [{"duel": 1, "role": self.role, "your_limit": self.L, "deadline_tick": self.ticks,
                           "decay_per_round": self.decay, "issues": ["price"],
                           "rival_offer": ({"price": round(self.rp)} if self.spoke else None)}]}

    def _settle(self, price):
        side = sd.side_of(self.role)
        self.result = side * (price - self.L) * (1 - self.decay) ** self.rounds
        self.done = True

    def duel_say(self, did, text="", price=None, days=None):
        within = (price <= self.rl) if self.role == "seller" else (price >= self.rl)
        nxt = self.rp + (self.rate if self.role == "seller" else -self.rate)
        if within and (self._better_for_rival(price, nxt) or self.ticks - self.tick <= 2):
            self._settle(price)
            return
        self.rounds += 1
        if self.rng.random() >= self.silent:
            self.spoke = True
            if self.rng.random() >= self.firm:
                self.rp = min(nxt, self.rl) if self.role == "seller" else max(nxt, self.rl)

    def duel_accept(self, did):
        self._settle(round(self.rp))


def play(params, profs, role, ticks, decay, n, seed):
    """Mean score of smart_duels.act with `params` against n rivals drawn from profs (this role)."""
    pool = [p for p in profs if p["role"] == role]
    if not pool:
        return None
    saved = {k: getattr(sd, k) for k in params}
    log, sd.log = sd.log, (lambda *a: None)
    saved_tuned, saved_defaults = sd.TUNED, dict(sd.DEFAULTS)
    sd.TUNED = {}                                    # act() must play exactly `params`, not what is already tuned
    sd.DEFAULTS.clear()
    sd.DEFAULTS.update({k: getattr(sd, k) for k in GRID}, **params)
    rng, total = random.Random(seed), 0.0
    try:
        for k, v in params.items():
            setattr(sd, k, v)
        for _ in range(n):
            L = rng.uniform(30, 200)
            r = Rival(role, L, rng.choice(pool), ticks, decay, rng)
            mem = {"duels": {}, "finished": {}, "raw_samples": [{}] * 3}
            while not r.done and r.tick < ticks:
                for d in r.duels()["duels"]:
                    sd.act(r, d, r.tick, mem)
                r.tick += 1
            total += r.result / L * 100                                 # per 100 of limit: duels of all sizes weigh alike
    finally:
        for k, v in saved.items():
            setattr(sd, k, v)
        sd.log = log
        sd.TUNED = saved_tuned
        sd.DEFAULTS.clear()
        sd.DEFAULTS.update(saved_defaults)
    return total / n


def tune(mem, n=300, seed=7, conditions=None):
    """{condition: {role: {"params", "score", "current", "current_score"}}}"""
    profs = profiles(mem)
    current = {k: getattr(sd, k) for k in GRID}
    out = {}
    for cname, (ticks, decay) in (conditions or CONDITIONS).items():
        out[cname] = {}
        for role in ("seller", "buyer"):
            cur = play(current, profs, role, ticks, decay, n, seed)
            best, best_s = dict(current), cur
            for h in GRID["SPEED_HORIZON"]:
                for be in GRID["BETA"]:
                    for an in GRID["OPEN_ANCHOR"]:
                        for kp in GRID["OFFER_KEEP"]:
                            p = {"SPEED_HORIZON": h, "BETA": be, "OPEN_ANCHOR": an, "OFFER_KEEP": kp}
                            s = play(p, profs, role, ticks, decay, n, seed)
                            if s is not None and (best_s is None or s > best_s):
                                best, best_s = p, s
            out[cname][role] = {"params": best, "score": round(best_s, 2) if best_s is not None else None,
                                "current": current, "current_score": round(cur, 2) if cur is not None else None}
    return out, len(profs)


def write_params(result, path=PARAMS_FILE):
    """duel_params.json: per condition and role, only settings that beat the current ones by MIN_GAIN."""
    keep = {}
    for cname, roles in result.items():
        for role, r in roles.items():
            cur, best = r["current_score"], r["score"]
            if best is not None and cur is not None and best > cur + MIN_GAIN * max(1.0, abs(cur)):
                keep.setdefault(cname, {})[role] = r["params"]
    with open(path, "w") as f:
        json.dump({"note": "smart_duels parameters tuned by duel_tuner.py on our finished duels (simulated rivals)",
                   "params": keep, "detail": result}, f, indent=1)
    return keep


def selftest():
    mem = {"duels": {}, "finished": {}}
    rng = random.Random(1)
    for i in range(40):                                                 # firm rivals that open far and give little
        role = "seller" if i % 2 else "buyer"
        side, L = sd.side_of(role), 100.0
        prices = [L * (1 - side * 0.4) + side * 2 * k for k in range(6)]
        mem["duels"][str(i)] = {"observed": {"role": role, "limit": L, "history": [{"rival_price": p} for p in prices]}}
        mem["finished"][str(i)] = {"status": "no_deal"}
    profs = profiles(mem)
    assert len(profs) == 40 and all(0 < p["rate"] < 0.05 for p in profs), profs[:2]
    s = play({"SPEED_HORIZON": 5, "BETA": 1.0, "OPEN_ANCHOR": 0.35, "OFFER_KEEP": 0.1}, profs, "seller", 12, 0.12, 50, 3)
    assert s is not None and s > -100, s
    res, n = tune(mem, n=20, conditions={"t": (12, 0.12)})
    assert n == 40 and set(res["t"]) == {"seller", "buyer"}
    assert res["t"]["seller"]["score"] >= res["t"]["seller"]["current_score"], "the search never returns worse than current"
    print("duel_tuner selftest OK")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    else:
        mem = sd.load_mem()
        print("calibration on Duels I:", calibrate(mem, profiles(mem)))
        res, n = tune(mem)
        keep = write_params(res)
        print(f"{n} rival profiles")
        for cname, roles in res.items():
            for role, r in roles.items():
                print(f"{cname:<7} {role:<6} current {r['current_score']} -> best {r['score']} with {r['params']}")
        print("written to", PARAMS_FILE, "(only improvements):", keep)
