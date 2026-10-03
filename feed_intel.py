"""Learn every dealer from ALL teams' REAL deals (feed settlements), not from what anyone says.

    python3 feed_intel.py --selftest
    python3 feed_intel.py --dry-run        # live: the deals in the current feed window, per dealer / side / rarity

Only `settlement` events with a `persona` are stored: a deal that actually happened (card, price, who sold to whom).
What dealers SAY (opening quotes, "final" offers, switched cards) and what teams list on the boards are not stored:
they are often bluffs or noise. Each deal is kept with its tick, card, set, rarity and team, so profiles can be as
concrete as the data allows: per card when there are enough deals of that card, else per set, else per rarity.

hints(...) needs at least MIN_DEALS deals and returns deal_min / deal_max / deal_med / recent_med, n_deals and the
level it used ("card", "set" or "rarity"). smart_agent uses it to skip conversations that cannot end in a deal and to
open a sale at no less than what the dealer paid other teams.
"""
import statistics
import sys

DEALERS = ("abuela", "chato", "pilar", "picaros")
EVERY = 3                    # ticks between feed reads (the feed keeps ~30-40 ticks)
MIN_DEALS = 3                # observations before a hint is used
KEEP = 200                   # observations kept per dealer/side/rarity
SEEN_KEEP = 6000


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
    return observe(mem, events, rarity_of)


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
        print(f"{n} real dealer deals in the current feed window")
        for r in summary(mem):
            print(r)
