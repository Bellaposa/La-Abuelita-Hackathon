"""Learn every dealer from ALL teams' REAL deals (feed settlements), not from what anyone says.

    python3 feed_intel.py --selftest
    python3 feed_intel.py --dry-run        # live: the deals in the current feed window, per dealer / side / rarity

Only `settlement` events with a `persona` are stored: a deal that actually happened (card, price, who sold to whom).
What dealers SAY (opening quotes, "final" offers, switched cards) and what teams list on the boards are not stored:
they are often bluffs or noise. Each deal is kept with its tick, card, set, rarity and team, so profiles can be as
concrete as the data allows: per card when there are enough deals of that card, else per set, else per rarity.

Behaviour (how a dealer negotiates, not what it says a price is) is learned from each public conversation followed from
`thread.opened` to its end (a matched settlement, or STALE ticks of silence). Per conversation we keep FEATURES only:
anchor (first quote), concession per round, fixed (never moved), switch (offered another card than the topic), whether
a "final" was broken (a better price came after it), rounds and outcome. Quoted prices are never used as prices.
behavior(...) aggregates them per dealer/side; with enough conversations it flags `final_unreliable` (finals broken in
>= half of >= MIN_FINALS cases) and `fixed_bidder` (a buyer that stays on its first bid in >= 80 % of >= MIN_THREADS).

hints(...) needs at least MIN_DEALS deals and returns deal_min / deal_max / deal_med / recent_med, n_deals and the
level it used ("card", "set" or "rarity"). smart_agent uses it to skip conversations that cannot end in a deal and to
open a sale at no less than what the dealer paid other teams.
"""
import json
import statistics
import sys

DEALERS = ("abuela", "chato", "pilar", "picaros", "banco")   # banco = Don Ernesto (level 5)
EVERY = 3                    # ticks between feed reads (the feed keeps ~30-40 ticks)
MIN_DEALS = 3                # observations before a hint is used
KEEP = 200                   # observations kept per dealer/side/rarity
SEEN_KEEP = 6000
STALE = 10                   # ticks of silence after which a conversation without a deal is over
MIN_THREADS = 3              # conversations before a behaviour flag is used
MIN_FINALS = 3               # "final" offers seen before we judge whether they are real
BEHAV_KEEP = 300


def _card(side_dict):
    """(ref, rarity, set) of the single card in an offer side, or None."""
    side_dict = side_dict or {}
    for a in side_dict.get("assets") or []:
        if isinstance(a, dict) and a.get("ref"):
            return a["ref"], a.get("rarity"), a.get("set") or a["ref"].split("-")[0]
    for t in side_dict.get("types") or []:
        if isinstance(t, str) and t.startswith("card:"):
            ref = t.split(":", 1)[1]
            return ref, None, ref.split("-")[0]
    return None


def _store(fi, kind, dealer, side, rarity, setid, price):
    if not rarity or not isinstance(price, (int, float)) or price <= 0:
        return
    lst = fi.setdefault(kind, {}).setdefault(f"{dealer}|{side}|{rarity}", [])
    lst.append([setid, float(price)])
    del lst[:-KEEP]


def observe(mem, events, rarity_of):
    """Store each real dealer deal once (by event id): settlement events with a persona. rarity_of: ref -> rarity."""
    fi = mem.setdefault("feed_intel", {})
    for k in ("openings", "finals", "threads"):                # older versions kept quotes: drop them, they are not deals
        fi.pop(k, None)
    seen = set(fi.get("seen", []))
    deals = fi.setdefault("deals", {})
    new = 0
    for e in events or []:
        eid = e.get("id")
        if eid in seen:
            continue
        seen.add(eid)
        p = e.get("payload") or {}
        if e.get("type") != "settlement" or p.get("persona") not in DEALERS:
            continue
        dealer = p["persona"]
        cards = [i for i in p.get("items") or [] if i.get("kind") == "card"]
        price = p.get("price")
        if len(cards) != 1 or not isinstance(price, (int, float)) or price <= 0:
            continue
        c = cards[0]
        side = "sells" if c.get("frm") == dealer else "buys" if c.get("to") == dealer else None
        ref = c.get("ref")
        rarity = c.get("rarity") or (rarity_of(ref) if ref else None)
        if not side or not ref or not rarity:
            continue
        team = c.get("to") if side == "sells" else c.get("frm")
        lst = deals.setdefault(f"{dealer}|{side}|{rarity}", [])
        lst.append({"t": e.get("tick"), "ref": ref, "set": c.get("set") or ref.split("-")[0], "price": float(price),
                    "team": team})
        del lst[:-KEEP]
        new += 1
    fi["seen"] = sorted(seen)[-SEEN_KEEP:]
    return new


def _price_card(side_dict, other):
    """(card ref, cash) of an offer seen from the dealer: the card side and the cash side."""
    c = _card(side_dict)
    return (c[0] if c else None), (other or {}).get("cash")


def track(mem, events, now=None):
    """Follow every public dealer conversation; when one ends, keep only its behaviour features."""
    fi = mem.setdefault("feed_intel", {})
    seen = set(fi.get("seen_conv", []))
    conv = fi.setdefault("open", {})
    for e in events or []:
        eid, p, typ, tick = e.get("id"), e.get("payload") or {}, e.get("type"), e.get("tick") or 0
        if eid in seen:
            continue
        seen.add(eid)
        if typ == "thread.opened" and p.get("with") in DEALERS:
            topic = p.get("topic") or {}
            conv[str(p.get("thread"))] = {"dealer": p["with"], "team": p.get("team"), "last": tick, "side": None,
                                          "topic_card": (topic.get("buy") or {}).get("card"), "d": [], "deal": None}
        elif typ == "thread.message" and str(p.get("thread")) in conv:
            c = conv[str(p.get("thread"))]
            c["last"] = max(c["last"], tick)
            o = p.get("offer") or {}
            if p.get("sender") == c["dealer"] and o:
                g, w = o.get("give") or {}, o.get("want") or {}
                if _card(g) and w.get("cash"):
                    c["side"] = c["side"] or "sells"
                    c["d"].append([tick, w["cash"], bool(o.get("final")), _card(g)[0]])
                elif _card(w) and g.get("cash"):
                    c["side"] = c["side"] or "buys"
                    c["d"].append([tick, g["cash"], bool(o.get("final")), _card(w)[0]])
        elif typ == "settlement" and p.get("persona") in DEALERS:
            parties = set(p.get("parties") or [])
            open_ = [(k, c) for k, c in conv.items() if c["dealer"] == p["persona"] and c["team"] in parties and c["deal"] is None]
            if open_:
                k, c = max(open_, key=lambda kc: kc[1]["last"])
                c["deal"], c["last"] = p.get("price"), max(c["last"], tick)
                _finish(fi, conv.pop(k))
    now = now if now is not None else max([e.get("tick") or 0 for e in events or []] or [0])
    for k in [k for k, c in conv.items() if now - c["last"] > STALE]:
        _finish(fi, conv.pop(k))
    fi["seen_conv"] = sorted(seen)[-SEEN_KEEP:]


def _finish(fi, c):
    """Behaviour features of one finished conversation (no quoted price is kept as a price)."""
    d, side = c["d"], c["side"]
    if not d or not side:
        return
    prices = [x[1] for x in d]
    first, n = prices[0], len(prices)
    better = (lambda a, b: a < b) if side == "sells" else (lambda a, b: a > b)    # better FOR THE TEAM
    best = min(prices) if side == "sells" else max(prices)
    conc = abs(best - first) / first / (n - 1) if n >= 2 and first else None
    finals = [i for i, x in enumerate(d) if x[2]]
    broken = None
    if finals:
        fp = d[finals[0]][1]
        later = prices[finals[0] + 1:] + ([c["deal"]] if c["deal"] is not None else [])
        broken = any(better(x, fp) for x in later)
    switch = None
    if side == "sells" and c.get("topic_card"):
        switch = any(x[3] and x[3] != c["topic_card"] for x in d)
    feat = {"rounds": n, "fixed": n >= 2 and len(set(prices)) == 1, "conc": conc, "final_seen": bool(finals),
            "final_broken": broken, "switch": switch, "deal": c["deal"] is not None,
            "deal_vs_first": round(c["deal"] / first, 3) if c["deal"] is not None and first else None}
    lst = fi.setdefault("behav", {}).setdefault(f"{c['dealer']}|{side}", [])
    lst.append(feat)
    del lst[:-BEHAV_KEEP]


def behavior(mem, dealer, side):
    """Aggregate behaviour of `dealer` on `side`, plus the two flags the agent uses (only with enough evidence)."""
    rows = ((mem.get("feed_intel") or {}).get("behav") or {}).get(f"{dealer}|{side}", [])
    if not rows:
        return {}
    multi = [r for r in rows if r["rounds"] >= 2]
    fin = [r for r in rows if r["final_seen"] and r["final_broken"] is not None]
    sw = [r for r in rows if r["switch"] is not None]
    concs = [r["conc"] for r in rows if r["conc"] is not None]
    out = {"threads": len(rows), "deal_rate": round(sum(r["deal"] for r in rows) / len(rows), 2),
           "rounds_med": statistics.median(r["rounds"] for r in rows),
           "fixed_rate": round(sum(r["fixed"] for r in multi) / len(multi), 2) if multi else None,
           "conc_med": round(statistics.median(concs), 3) if concs else None,
           "finals": len(fin), "final_broken_rate": round(sum(r["final_broken"] for r in fin) / len(fin), 2) if fin else None,
           "switch_rate": round(sum(r["switch"] for r in sw) / len(sw), 2) if sw else None}
    out["final_unreliable"] = len(fin) >= MIN_FINALS and (out["final_broken_rate"] or 0) >= 0.5
    out["fixed_bidder"] = side == "buys" and len(multi) >= MIN_THREADS and (out["fixed_rate"] or 0) >= 0.8
    return out


def _rows(fi, key):
    out = []
    for r in (fi.get("deals") or {}).get(key, []):
        if isinstance(r, dict):
            out.append(r)
        elif isinstance(r, (list, tuple)) and len(r) == 2:      # older format [set, price]
            out.append({"t": None, "ref": None, "set": r[0], "price": float(r[1]), "team": None})
    return out


def hints(mem, dealer, side, rarity, setid=None, ref=None):
    """What all teams' REAL deals say about `dealer` on `side` ("sells"/"buys") for `rarity`: per card when that card
    has MIN_DEALS deals, else per set, else per rarity. Empty dict until MIN_DEALS deals are known."""
    rows = _rows(mem.get("feed_intel") or {}, f"{dealer}|{side}|{rarity}")
    level, use = "rarity", rows
    by_set = [r for r in rows if setid and r["set"] == setid]
    by_ref = [r for r in rows if ref and r["ref"] == ref]
    if len(by_ref) >= MIN_DEALS:
        level, use = "card", by_ref
    elif len(by_set) >= MIN_DEALS:
        level, use = "set", by_set
    prices = [r["price"] for r in use]
    if len(prices) < MIN_DEALS:
        return {}
    return {"n_deals": len(prices), "deal_min": min(prices), "deal_max": max(prices),
            "deal_med": statistics.median(prices), "recent_med": statistics.median(prices[-10:]), "level": level}


def buy_is_futile(h, cap):
    """True when our cap is under every price this dealer ever sold this rarity at (to anyone)."""
    return bool(h) and cap < h["deal_min"]


def sell_is_futile(h, floor):
    """True when our floor is over every price this dealer ever paid for this rarity (to anyone)."""
    return bool(h) and floor > h["deal_max"]


def step(b, mem, tick, rarity_of, log=print):
    if tick - mem.get("feed_intel_tick", -999) < EVERY:
        return 0
    mem["feed_intel_tick"] = tick
    try:
        events = b.call("GET", "/api/feed?limit=500").get("events", [])
    except Exception as e:
        log(f"feed intel: feed unreadable ({e})")
        return 0
    n = observe(mem, events, rarity_of)
    track(mem, events, tick)
    return n


def summary(mem):
    rows = []
    fi = mem.get("feed_intel") or {}
    for key in sorted(fi.get("deals") or {}):
        dealer, side, rar = key.split("|")
        d = [r["price"] for r in _rows(fi, key)]
        if d:
            rows.append({"dealer": dealer, "side": side, "rarity": rar, "deals": len(d), "deal_min": min(d),
                         "deal_med": statistics.median(d), "deal_max": max(d)})
    return rows


def selftest():
    rar = {"RET-09": "rare", "LAT-02": "common", "SAL-07": "uncommon", "LAV-07": "uncommon"}.get
    quote = {"id": 1, "type": "thread.message", "payload": {"thread": 10, "sender": "picaros", "offer": {
        "give": {"types": ["card:RET-09"]}, "want": {"cash": 73}, "final": True}}}
    st = lambda i, dealer, ref, rarity, price, frm, to, tick=900: {"id": i, "type": "settlement", "tick": tick, "payload": {
        "persona": dealer, "price": price, "items": [{"kind": "card", "ref": ref, "rarity": rarity,
                                                     "set": ref.split("-")[0], "frm": frm, "to": to}]}}
    ev = [quote] + [st(100 + i, "picaros", "RET-09", "rare", p, "picaros", "t1") for i, p in enumerate((57, 60, 63))] \
        + [st(200 + i, "pilar", "SAL-07", "uncommon", p, "t2", "pilar") for i, p in enumerate((19, 23, 22))] \
        + [st(300, "pilar", "LAV-07", "uncommon", 15, "t3", "pilar")]
    mem = {"feed_intel": {"openings": {"x": [1]}, "finals": {"x": [1]}, "threads": {"1": {}}}}
    assert observe(mem, ev, rar) == 7, "settlements only: the dealer's quote is not stored"
    assert observe(mem, ev, rar) == 0, "each event once"
    assert set(mem["feed_intel"]) == {"seen", "deals"}, "quotes from older versions are dropped"
    h = hints(mem, "picaros", "sells", "rare", "RET", "RET-09")
    assert h["deal_min"] == 57 and h["deal_max"] == 63 and h["level"] == "card", h
    assert mem["feed_intel"]["deals"]["picaros|sells|rare"][0]["team"] == "t1"
    hp = hints(mem, "pilar", "buys", "uncommon", "SAL")
    assert hp["deal_max"] == 23 and hp["level"] == "set", hp                 # Salamanca alone: Pilar pays more there
    hr = hints(mem, "pilar", "buys", "uncommon", "LAV")
    assert hr["level"] == "rarity" and hr["n_deals"] == 4, hr               # only 1 LAV deal: fall back to the rarity
    assert buy_is_futile(h, 50) and not buy_is_futile(h, 58) and not buy_is_futile({}, 1)
    assert sell_is_futile(hp, 30) and not sell_is_futile(hp, 20)
    # behaviour: a Pícaros-like seller that breaks its "final" and switches cards; a Chato-like fixed buyer
    msg = lambda i, t, th, who, team, give, want, final=False: {"id": i, "type": "thread.message", "tick": t, "payload": {
        "thread": th, "sender": who, "team": team, "with": who if who in DEALERS else "x",
        "offer": {"give": give, "want": want, "final": final}}}
    ev2 = []
    for k in range(3):
        th, base = 50 + k, 1000 + 100 * k
        ev2 += [{"id": base, "type": "thread.opened", "tick": 10, "payload": {"thread": th, "team": "t1", "with": "picaros",
                                                                            "topic": {"buy": {"card": "RET-09"}}}},
                msg(base + 1, 11, th, "picaros", "t1", {"types": ["card:RET-09"]}, {"cash": 73}),
                msg(base + 2, 12, th, "picaros", "t1", {"types": ["card:RET-06"]}, {"cash": 60}, True),
                msg(base + 3, 13, th, "picaros", "t1", {"types": ["card:RET-09"]}, {"cash": 57})]
        th2 = 60 + k
        ev2 += [{"id": base + 50, "type": "thread.opened", "tick": 10, "payload": {"thread": th2, "team": "t2", "with": "chato",
                                                                                 "topic": {"sell": {"assets": [7]}}}},
                msg(base + 51, 11, th2, "chato", "t2", {"cash": 13}, {"assets": [{"ref": "LAV-08"}]}),
                msg(base + 52, 12, th2, "chato", "t2", {"cash": 13}, {"assets": [{"ref": "LAV-08"}]})]
    m2 = {}
    track(m2, ev2, now=40)                                                  # 40 - 13 > STALE: all conversations ended
    bp = behavior(m2, "picaros", "sells")
    assert bp["threads"] == 3 and bp["final_unreliable"] and bp["switch_rate"] == 1.0 and not bp["fixed_bidder"], bp
    bc = behavior(m2, "chato", "buys")
    assert bc["fixed_bidder"] and bc["fixed_rate"] == 1.0, bc
    feats = {"rounds", "fixed", "conc", "final_seen", "final_broken", "switch", "deal", "deal_vs_first"}
    assert all(set(r) == feats for rows in m2["feed_intel"]["behav"].values() for r in rows), "features only, no quotes"
    assert m2["feed_intel"]["open"] == {}, "finished conversations are not kept"
    print("feed_intel selftest OK")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    elif "--dry-run" in sys.argv:
        import os
        from bazaar_sdk import Bazaar
        b = Bazaar(os.environ.get("BAZAAR_URL", "https://bazaar.causaprima.ai"), os.environ["BAZAAR_KEY"], wait_on_tick=False)
        cat = b.catalog()
        rarity = {c["id"]: c["rarity"] for s in cat["sets"] for c in s["cards"]}
        mem = {}
        n = observe(mem, b.call("GET", "/api/feed?limit=500").get("events", []), rarity.get)
        track(mem, b.call("GET", "/api/feed?limit=500").get("events", []))
        print(f"{n} real dealer deals in the current feed window")
        for r in summary(mem):
            print(r)
        for d in DEALERS:
            for side in ("sells", "buys"):
                bh = behavior(mem, d, side)
                if bh:
                    print("behaviour", d, side, bh)
