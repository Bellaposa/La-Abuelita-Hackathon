"""Bring other teams' trades to our venue: find concrete pairs on every open board and announce them, by team.

    python3 venue_promoter.py --selftest
    python3 venue_promoter.py --dry-run         # live read, prints what it would announce (needs BAZAAR_KEY)

Our market scores the value OTHER teams create trading on it, and we cannot trade there ourselves. Most cards change
hands on El Rastro, where whoever accepts pays 5 % + 1 P a card, so quotes that already cross often sit there, each side
waiting for the other to pay. From the public boards (offers) and the public feed (which team made each offer, who just
pulled which card from a pack or a gift) we find, per card:

  CROSS   a team bids at least what another team asks          -> "post both on v01, they cross next tick, 0 %"
  NEAR    the gap is small (<= max(2 P, 12 % of the ask))      -> "meet at the midpoint on v01"
  HOLDER  a team bids for a card another team just pulled       -> "t13 just pulled it: list it on v01"

and announce the best few, naming the teams (as Team 10 does on v07). An item is not repeated for REPEAT_TICKS.
Our own offers and v01 itself are skipped: we cannot trade on our venue, and our broker already crosses what is there.
"""
import json
import sys

import team_profiles

VENUE = "v01"
ANNOUNCE_GAP = 20          # the server allows one announcement per venue every 20 ticks: each one carries the best pairs
READ_EVERY = 5             # read boards + feed this often, so the profiles miss nothing of the feed's ~25-tick window
RETRY_TICKS = 3            # after a refused announcement
REPEAT_TICKS = 30          # the same pair is not announced again for this long
PULL_TTL = 80              # ticks a pulled card still counts as "just pulled"
MAX_ITEMS = 3
MAX_CHARS = 480
HEAD = "v01 (Team 6): 0 % fee, 0 P a card; post PUBLIC and our broker crosses bid and ask card by card at the midpoint next tick."


def offers_from(boards, feed_events, exclude_makers=(), exclude_ids=(), alias=None):
    """Open single-card offers on other venues: {id, venue, maker, side, ref, price}. maker = real team id from the
    feed when it listed the offer, else from `alias` (board pseudonym -> team, learned whenever one offer shows both
    and kept by the caller), else the pseudonym itself (still one identity per team and venue)."""
    real = {}
    for e in feed_events or []:
        if e.get("type") == "offer.listed":
            o = (e.get("payload") or {}).get("offer") or {}
            if o.get("id") is not None:
                real[o["id"]] = o.get("maker")
    alias = {} if alias is None else alias
    for offers in (boards or {}).values():
        for o in offers or []:
            if o.get("id") in real and o.get("maker") and o["maker"] != real[o["id"]]:
                alias[o["maker"]] = real[o["id"]]
    real = {**{o["id"]: alias[o["maker"]] for offers in (boards or {}).values() for o in offers or []
               if o.get("maker") in alias and o.get("id") is not None}, **real}
    out = []
    for venue, offers in (boards or {}).items():
        if venue == VENUE:
            continue
        for o in offers or []:
            if o.get("status", "open") != "open" or o.get("to") or o.get("id") in exclude_ids:
                continue
            maker = real.get(o.get("id"), o.get("maker"))
            if maker in exclude_makers:
                continue
            g, w = o.get("give") or {}, o.get("want") or {}
            if len(g.get("assets") or []) == 1 and w.get("cash") and not g.get("cash") and not w.get("types"):
                a = g["assets"][0]
                out.append({"id": o["id"], "venue": venue, "maker": maker, "side": "ask", "ref": a.get("ref"), "price": w["cash"]})
            elif g.get("cash") and len(w.get("types") or []) == 1 and str(w["types"][0]).startswith("card:") and not g.get("assets"):
                out.append({"id": o["id"], "venue": venue, "maker": maker, "side": "bid", "ref": w["types"][0][5:], "price": g["cash"]})
    return [o for o in out if o["ref"]]


def pulls_from(feed_events, now, exclude_teams=()):
    """{ref: (team, tick)} cards a team got recently from a pack (its best card) or a gift, newest kept."""
    out = {}
    for e in sorted(feed_events or [], key=lambda e: e.get("tick") or 0):
        p, t = e.get("payload") or {}, e.get("tick") or 0
        if now - t > PULL_TTL or p.get("team") in exclude_teams:
            continue
        if e.get("type") == "pack.opened" and (p.get("best") or {}).get("ref"):
            out[p["best"]["ref"]] = (p["team"], t)
        elif e.get("type") == "gift.given":
            for ref in p.get("cards") or []:
                out[ref] = (p.get("team"), t)
    return out


def opportunities(offers, pulls=None):
    """Scored items, best first: [(score, key, text)]. Score ~ value that could change hands."""
    by = {}
    for o in offers:
        by.setdefault(o["ref"], {"ask": [], "bid": []})[o["side"]].append(o)
    items = []
    for ref, s in by.items():
        asks, bids = sorted(s["ask"], key=lambda o: o["price"]), sorted(s["bid"], key=lambda o: -o["price"])
        pair = next(((a, b) for a in asks for b in bids if a["maker"] != b["maker"]), None)
        if pair:
            a, b = pair
            gap = a["price"] - b["price"]
            where = "El Rastro" if "rastro" in (a["venue"], b["venue"]) else "other venues"
            if gap <= 0:                                  # tiers: a cross is a trade now, then near pairs, then holders
                items.append((2000 + b["price"], f"cross:{a['id']}:{b['id']}",
                              f"{ref}: {b['maker']} bids {b['price']}, {a['maker']} asks {a['price']} on {where}; "
                              f"post both on v01 and they cross at {(a['price'] + b['price']) // 2}."))
                continue
            if gap <= max(2, round(0.12 * a["price"])):
                items.append((1000 + b["price"], f"near:{a['id']}:{b['id']}",
                              f"{ref}: {a['maker']} asks {a['price']}, {b['maker']} bids {b['price']}; "
                              f"split the {gap} P gap on v01."))
                continue
        if bids and not asks and pulls and ref in pulls:
            b, (team, _t) = bids[0], pulls[ref]
            if team != b["maker"]:
                items.append((b["price"] * 0.6, f"holder:{ref}:{team}:{b['id']}",
                              f"{ref}: {b['maker']} bids {b['price']}; {team} just pulled one: list it on v01."))
    return sorted(items, key=lambda x: -x[0])


def compose(items, sent, now):
    """Announcement text with the best items not sent in the last REPEAT_TICKS, and the keys used ([] = nothing new)."""
    fresh = [(s, k, t) for s, k, t in items if now - sent.get(k, -10 ** 9) >= REPEAT_TICKS]
    text, keys = HEAD, []
    for _s, k, t in fresh[:MAX_ITEMS]:
        if len(text) + 1 + len(t) > MAX_CHARS:
            break
        text, keys = text + " " + t, keys + [k]
    return (text, keys) if keys else (None, [])


def gather(read, me_id, my_offer_ids=(), alias=None, profiles=None):
    """read(path) -> json. Every open board but ours, the feed, and the offers/pulls made from them."""
    venues = [v["venue"] for v in read("/api/venues").get("venues", []) if v.get("status") == "open" and v.get("venue") != VENUE]
    boards = {}
    for v in venues:
        try:
            boards[v] = read(f"/api/venues/{v}/offers").get("offers", [])
        except Exception:
            continue
    feed = read("/api/feed?limit=500").get("events", [])
    now = max([e.get("tick") or 0 for e in feed] or [0])
    offers = offers_from(boards, feed, exclude_makers={me_id}, exclude_ids=set(my_offer_ids), alias=alias)
    if profiles is not None:
        team_profiles.update(profiles, feed)
    return offers, pulls_from(feed, now, exclude_teams={me_id}), now


def step(broker, team, state, tick, log, public=None):
    """Read every READ_EVERY ticks (profiles); announce when ANNOUNCE_GAP ticks passed since the last accepted one and
    there is a fresh concrete item. Never raises. public(path) reads the public routes without a key (60/s per address)
    so the team key's 5/s stay with the agents."""
    due = tick - state.get("last_ok", -10 ** 9) >= ANNOUNCE_GAP and tick - state.get("last_try", -10 ** 9) >= RETRY_TICKS
    if not due and tick - state.get("last_read", -10 ** 9) < READ_EVERY:
        return False
    state["last_read"] = tick
    try:
        me_id = state.get("me") or team.me()["id"]
        state["me"] = me_id
        mine = [o["id"] for o in team.my_offers().get("offers", [])]
        read = public or (lambda p: team.call("GET", p))
        if "profiles" not in state:
            state["profiles"] = team_profiles.load()
        offers, pulls, _now = gather(read, me_id, mine, alias=state.setdefault("alias", {}), profiles=state["profiles"])
        team_profiles.save(state["profiles"])
        live = {}
        for o in offers:
            if o["side"] == "ask" and str(o["maker"]).startswith("t"):
                live.setdefault(o["ref"], []).append((o["maker"], o["price"]))
        items = opportunities(offers, pulls)
        seen_refs = {t.split(":")[0] for _s, _k, t in items}                 # one item per card: live ones win
        items += [(s * 0.5, k, t) for s, k, t in team_profiles.match(state["profiles"], me_id, live)   # history-based pairs,
                  if k.split(":")[3] not in seen_refs]                                               # after live ones
        items.sort(key=lambda x: -x[0])
        state["last_items"] = len(items)
        text, keys = compose(items, state.setdefault("sent", {}), tick)
        if not text or not due:
            return False
        state["last_try"] = tick
        broker.announce(text)
        state["last_ok"] = tick
        for k in keys:
            state["sent"][k] = tick
        log(f"promoted ({len(items)} pairs seen on other venues): {text[len(HEAD) + 1:][:300]}")
        return True
    except Exception as e:
        log(f"promoter: {type(e).__name__}: {e}")
        return False


def selftest():
    feed = [{"type": "offer.listed", "tick": 10, "payload": {"offer": {"id": 1, "maker": "t13"}}},
            {"type": "offer.listed", "tick": 10, "payload": {"offer": {"id": 2, "maker": "t16"}}},
            {"type": "pack.opened", "tick": 50, "payload": {"team": "t04", "best": {"ref": "MAL-10"}}},
            {"type": "gift.given", "tick": 55, "payload": {"team": "t06", "cards": ["SAL-01"]}}]
    ask = lambda i, ref, p, mk="p1": {"id": i, "maker": mk, "status": "open", "give": {"cash": 0, "assets": [{"ref": ref}], "types": []},
                                      "want": {"cash": p, "assets": [], "types": []}}
    bid = lambda i, ref, p, mk="p2": {"id": i, "maker": mk, "status": "open", "give": {"cash": p, "assets": [], "types": []},
                                      "want": {"cash": 0, "assets": [], "types": [f"card:{ref}"]}}
    boards = {"rastro": [ask(1, "LAT-03", 5), bid(2, "LAT-03", 6), ask(3, "LAT-07", 15, "p3"), bid(4, "LAT-07", 14, "p4"),
                         ask(5, "RET-01", 40, "p5"), bid(6, "RET-01", 10, "p6"), bid(7, "MAL-10", 47, "p7"),
                         ask(8, "SAL-02", 9, "t06x"), bid(9, "SAL-02", 12, "p9")],
              "v01": [ask(20, "LAV-01", 3), bid(21, "LAV-01", 9)]}
    alias = {}
    offs = offers_from(boards, feed, exclude_makers={"t06"}, exclude_ids={8}, alias=alias)
    assert {o["maker"] for o in offs if o["id"] in (1, 2)} == {"t13", "t16"}, "real team ids from the feed"
    assert alias == {"p1": "t13", "p2": "t16"}, alias
    later = offers_from({"rastro": [ask(30, "LAV-05", 4, "p1")]}, [], alias=alias)
    assert later[0]["maker"] == "t13", "a pseudonym seen once is remembered after its listing left the feed"
    assert not any(o["venue"] == "v01" for o in offs) and not any(o["id"] == 8 for o in offs), "skip our venue and our offers"
    pulls = pulls_from(feed, 60, exclude_teams={"t06"})
    assert pulls == {"MAL-10": ("t04", 50)}, pulls
    items = opportunities(offs, pulls)
    kinds = [k.split(":")[0] for _s, k, _t in items]
    assert kinds[0] == "cross" and "near" in kinds and "holder" in kinds and len(items) == 3, items   # RET-01 gap 30: nothing
    assert "t16 bids 6, t13 asks 5 on El Rastro" in items[0][2]
    text, keys = compose(items, {}, 100)
    assert text.startswith(HEAD) and len(keys) == 3 and len(text) <= MAX_CHARS
    sent = {k: 100 for k in keys}
    assert compose(items, sent, 110) == (None, []), "nothing new within REPEAT_TICKS"
    assert compose(items, sent, 100 + REPEAT_TICKS)[1] == keys, "repeated after REPEAT_TICKS"
    print("venue_promoter selftest OK")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    elif "--dry-run" in sys.argv:
        import os
        from bazaar_sdk import Bazaar
        b = Bazaar(os.environ.get("BAZAAR_URL", "https://bazaar.causaprima.ai"), os.environ["BAZAAR_KEY"], wait_on_tick=False)
        me = b.me()["id"]
        offers, pulls, now = gather(lambda p: b.call("GET", p), me, [o["id"] for o in b.my_offers().get("offers", [])])
        items = opportunities(offers, pulls)
        print(f"tick {now}: {len(offers)} offers on other venues, {len(pulls)} recent pulls, {len(items)} items")
        for s, k, t in items[:12]:
            print(f"  {s:6.1f} {t}")
        print("would announce:", compose(items, {}, now)[0])
