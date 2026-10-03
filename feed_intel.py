"""Learn every dealer from ALL teams' public conversations, not only ours.

    python3 feed_intel.py --selftest
    python3 feed_intel.py --dry-run        # live: what the current feed window teaches, per dealer / side / rarity

The public feed (GET /api/feed, last ~500 events, about 30-40 ticks) carries every team's dealer conversations:
`thread.opened` (team, dealer, topic), `thread.message` (each offer: card, price, final) and `settlement` with a
`persona` (the deal: card, price, who sold to whom). We read it every few ticks and accumulate, per dealer, side
(the dealer `sells` to a team or `buys` from it) and rarity (and set, since Pilar pays more for the sets she loves):

    openings   the dealer's first price in each conversation
    finals     prices the dealer marked `final`
    deals      prices that actually settled

hints(...) turns that into numbers the agent uses, only with at least MIN_DEALS deals observed:
    deal_min / deal_max / deal_med, open_med, final_med, n_deals, n_threads.
Two decisions read them (smart_agent): skip a conversation that cannot end in a deal (our buy cap under the lowest
price the dealer ever sold that rarity at; our sell floor over the highest it ever paid), and open a sale at no less
than what the dealer has paid other teams for that rarity (and set).
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
    """Accumulate dealer behaviour from feed events (idempotent: each event id once). rarity_of: ref -> rarity."""
    fi = mem.setdefault("feed_intel", {})
    seen = set(fi.get("seen", []))
    threads = fi.setdefault("threads", {})
    new = 0
    for e in events or []:
        eid = e.get("id")
        if eid in seen:
            continue
        seen.add(eid)
        p = e.get("payload") or {}
        typ = e.get("type")
        if typ == "thread.message" and p.get("sender") in DEALERS and p.get("offer"):
            dealer, o = p["sender"], p["offer"]
            g, w = o.get("give") or {}, o.get("want") or {}
            if _card(g) and w.get("cash"):
                side, (ref, rar, setid), price = "sells", _card(g), w["cash"]
            elif _card(w) and g.get("cash"):
                side, (ref, rar, setid), price = "buys", _card(w), g["cash"]
            else:
                continue
            rar = rar or rarity_of(ref)
            key = str(p.get("thread"))
            th = threads.setdefault(key, {"dealer": dealer, "side": side, "first": None})
            if th["first"] is None:
                th["first"] = price
                _store(fi, "openings", dealer, side, rar, setid, price)
            if o.get("final"):
                _store(fi, "finals", dealer, side, rar, setid, price)
            new += 1
        elif typ == "settlement" and p.get("persona") in DEALERS:
            dealer = p["persona"]
            cards = [i for i in p.get("items") or [] if i.get("kind") == "card"]
            if len(cards) != 1:
                continue
            c = cards[0]
            side = "sells" if c.get("frm") == dealer else "buys" if c.get("to") == dealer else None
            if side:
                _store(fi, "deals", dealer, side, c.get("rarity") or rarity_of(c.get("ref")),
                       c.get("set") or (c.get("ref") or "-").split("-")[0], p.get("price"))
                new += 1
    fi["seen"] = sorted(seen)[-SEEN_KEEP:]
    if len(threads) > 3000:                                    # keep the newest conversations only
        for k in sorted(threads, key=lambda x: int(x) if x.isdigit() else 0)[:-2000]:
            threads.pop(k, None)
    return new


def hints(mem, dealer, side, rarity, setid=None):
    """What all teams' conversations say about `dealer` on `side` ("sells"/"buys") for `rarity` (and `setid` when there
    are enough deals in that set). Empty dict until MIN_DEALS deals are known."""
    fi = mem.get("feed_intel") or {}
    key = f"{dealer}|{side}|{rarity}"

    def pick(kind):
        rows = (fi.get(kind) or {}).get(key, [])
        if setid:
            same = [p for s, p in rows if s == setid]
            if kind != "deals" or len(same) >= MIN_DEALS:
                return same or [p for _, p in rows]
        return [p for _, p in rows]

    deals = pick("deals")
    if len(deals) < MIN_DEALS:
        return {}
    out = {"n_deals": len(deals), "deal_min": min(deals), "deal_max": max(deals), "deal_med": statistics.median(deals)}
    opens, finals = pick("openings"), pick("finals")
    if opens:
        out["open_med"] = statistics.median(opens)
    if finals:
        out["final_med"] = statistics.median(finals)
    return out


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
    for key in sorted(set((fi.get("deals") or {})) | set((fi.get("openings") or {}))):
        dealer, side, rar = key.split("|")
        d = [p for _, p in (fi.get("deals") or {}).get(key, [])]
        o = [p for _, p in (fi.get("openings") or {}).get(key, [])]
        f = [p for _, p in (fi.get("finals") or {}).get(key, [])]
        rows.append({"dealer": dealer, "side": side, "rarity": rar, "deals": len(d),
                     "deal_med": statistics.median(d) if d else None, "deal_min": min(d) if d else None,
                     "deal_max": max(d) if d else None, "open_med": statistics.median(o) if o else None,
                     "final_med": statistics.median(f) if f else None, "threads": len(o)})
    return rows


def selftest():
    rar = {"RET-09": "rare", "LAT-02": "common", "SAL-07": "uncommon"}.get
    ev = [
        {"id": 1, "type": "thread.message", "payload": {"thread": 10, "sender": "picaros", "offer": {
            "give": {"types": ["card:RET-09"]}, "want": {"cash": 73}, "final": False}}},
        {"id": 2, "type": "thread.message", "payload": {"thread": 10, "sender": "picaros", "offer": {
            "give": {"types": ["card:RET-09"]}, "want": {"cash": 60}, "final": True}}},
        {"id": 3, "type": "thread.message", "payload": {"thread": 11, "sender": "picaros", "offer": {
            "give": {"cash": 4}, "want": {"assets": [{"id": 5, "ref": "LAT-02", "rarity": "common", "set": "LAT"}]}}}},
        {"id": 4, "type": "thread.message", "payload": {"thread": 12, "sender": "t07", "offer": {"give": {"cash": 50}}}},
    ] + [{"id": 100 + i, "type": "settlement", "payload": {"persona": "picaros", "price": p, "items": [
        {"kind": "card", "ref": "RET-09", "rarity": "rare", "set": "RET", "frm": "picaros", "to": "t1"}]}} for i, p in enumerate((57, 60, 63))] \
      + [{"id": 200 + i, "type": "settlement", "payload": {"persona": "pilar", "price": p, "items": [
        {"kind": "card", "ref": "SAL-07", "rarity": "uncommon", "set": "SAL", "frm": "t2", "to": "pilar"}]}} for i, p in enumerate((19, 23, 22))]
    mem = {}
    assert observe(mem, ev, rar) == 9
    assert observe(mem, ev, rar) == 0, "each event once"
    h = hints(mem, "picaros", "sells", "rare")
    assert h["deal_min"] == 57 and h["deal_max"] == 63 and h["open_med"] == 73 and h["final_med"] == 60, h
    assert hints(mem, "picaros", "buys", "common") == {}, "one opening, no deals: no hint yet"
    hp = hints(mem, "pilar", "buys", "uncommon", "SAL")
    assert hp["deal_max"] == 23 and hp["n_deals"] == 3, hp
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
        print(f"{n} observations from the current feed window")
        for r in summary(mem):
            print(r)
