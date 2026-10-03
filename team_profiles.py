"""Profiles of the other teams, accumulated from the public feed (it only keeps ~25 ticks, so we keep what it shows).

    python3 team_profiles.py --selftest
    python3 team_profiles.py --report           # print the profiles in team_profiles.json

Per team:
  wants      cards it bid for (best price, last tick) and cards it tried to buy from a dealer; dropped once it gets one
  spares     cards it listed for sale (lowest ask, last tick) or sold to a dealer
  holds      card copies we saw it receive (settlements, its best pack pull, gifts) and not give away: asset id -> ref
  venues     where it lists offers (counts), and its trades by venue
  named      times a venue announcement named it, and how many of those it answered by listing on THAT venue within
             RESPOND_TICKS: a team that answers announcements is worth naming; one that never does is not

match(...) pairs wants with supply across teams: a live ask, a recent pull, a known holder that also lists spares.
The promoter announces those pairs to bring them to our venue instead of El Rastro.
"""
import json
import re
import sys

KEEP_TICKS = 400           # wants/spares older than this are forgotten
RESPOND_TICKS = 12
SEEN_KEEP = 8000
TEAM_RE = re.compile(r"\bt\d{2}\b")
DEALERS = {"abuela", "chato", "pilar", "picaros", "banco"}


def _team(p, k):
    v = p.get(k)
    return v if isinstance(v, str) and TEAM_RE.fullmatch(v) else None


def _prof(P, t):
    return P.setdefault("teams", {}).setdefault(t, {"wants": {}, "spares": {}, "holds": {}, "venues": {}, "trades": {},
                                                     "named": 0, "answered": 0})


def update(P, events):
    """Fold new feed events into the profiles P (in place). Returns how many events were new."""
    seen = set(P.get("seen", []))
    pending = P.setdefault("pending_mentions", [])           # [team, venue, tick] waiting for an answer
    new = 0
    for e in sorted(events or [], key=lambda e: e.get("id") or 0):
        if e.get("id") in seen:
            continue
        seen.add(e.get("id"))
        new += 1
        typ, p, tick = e.get("type"), e.get("payload") or {}, e.get("tick") or 0
        if typ == "offer.listed":
            o = p.get("offer") or {}
            t = _team(o, "maker")
            if not t:
                continue
            pr = _prof(P, t)
            v = o.get("venue") or p.get("venue")
            pr["venues"][v] = pr["venues"].get(v, 0) + 1
            g, w = o.get("give") or {}, o.get("want") or {}
            if len(g.get("assets") or []) == 1 and w.get("cash"):
                a = g["assets"][0]
                ref = a.get("ref")
                old = pr["spares"].get(ref)
                pr["spares"][ref] = [min(w["cash"], old[0]) if old and tick - old[1] < KEEP_TICKS else w["cash"], tick]
                if a.get("id") is not None:
                    pr["holds"][str(a["id"])] = ref
            elif g.get("cash") and len(w.get("types") or []) == 1 and str(w["types"][0]).startswith("card:"):
                ref = w["types"][0][5:]
                old = pr["wants"].get(ref)
                pr["wants"][ref] = [max(g["cash"], old[0]) if old and tick - old[1] < KEEP_TICKS else g["cash"], tick]
            for m in [m for m in pending if m[0] == t and m[1] == v and tick - m[2] <= RESPOND_TICKS]:
                pr["answered"] += 1
                pending.remove(m)
        elif typ == "settlement":
            v = p.get("venue") or (p.get("persona") and "dealer")
            for it in p.get("items") or []:
                if it.get("kind") != "card":
                    continue
                frm, to, ref = it.get("frm"), it.get("to"), it.get("ref")
                if TEAM_RE.fullmatch(str(frm or "")):
                    pr = _prof(P, frm)
                    pr["holds"].pop(str(it.get("id")), None)
                    pr["trades"][v] = pr["trades"].get(v, 0) + 1
                    if to in DEALERS:
                        pr["spares"][ref] = [p.get("price") or 0, tick]
                if TEAM_RE.fullmatch(str(to or "")):
                    pr = _prof(P, to)
                    pr["holds"][str(it.get("id"))] = ref
                    pr["wants"].pop(ref, None)                       # it got one
                    pr["trades"][v] = pr["trades"].get(v, 0) + 1
        elif typ == "pack.opened" and _team(p, "team") and (p.get("best") or {}).get("ref"):
            b = p["best"]
            _prof(P, p["team"])["holds"][str(b.get("id"))] = b["ref"]
        elif typ == "gift.given" and _team(p, "team"):
            pr = _prof(P, p["team"])
            for i, ref in enumerate(p.get("cards") or []):
                pr["holds"][f"gift{e.get('id')}-{i}"] = ref
        elif typ == "thread.opened" and p.get("with") in DEALERS and _team(p, "team"):
            ref = ((p.get("topic") or {}).get("buy") or {}).get("card")
            if ref:
                pr = _prof(P, p["team"])
                pr["wants"].setdefault(ref, [0, tick])[1] = tick
        elif typ == "venue.announcement":
            v = p.get("venue")
            for t in set(TEAM_RE.findall(p.get("text") or "")):
                _prof(P, t)["named"] += 1
                pending.append([t, v, tick])
    now = max([e.get("tick") or 0 for e in events or []] + [P.get("tick", 0)])
    P["tick"] = now
    P["pending_mentions"] = [m for m in pending if now - m[2] <= RESPOND_TICKS]
    for pr in P.get("teams", {}).values():
        for k in ("wants", "spares"):
            pr[k] = {r: v for r, v in pr[k].items() if now - v[1] <= KEEP_TICKS}
    P["seen"] = sorted(seen)[-SEEN_KEEP:]
    return new


def responsiveness(pr):
    """Share of the announcements naming this team that it answered on the named venue (None: never named)."""
    return round(pr["answered"] / pr["named"], 2) if pr.get("named") else None


def match(P, me=None, live_asks=None):
    """[(score, key, text)] wants of one team that another team can supply, best first.
    Supply: a live ask (live_asks: {ref: [(team, price)]}), a listed spare, or a known copy held by a team that sells."""
    teams = {t: pr for t, pr in (P.get("teams") or {}).items() if t != me}
    out = []
    for buyer, bp in teams.items():
        for ref, (bid, wt) in bp["wants"].items():
            sup = []
            for seller, sp in teams.items():
                if seller == buyer:
                    continue
                if ref in sp["spares"]:
                    sup.append((2, seller, sp["spares"][ref][0]))
                elif ref in sp["holds"].values() and sp["spares"]:
                    sup.append((1, seller, None))
            for seller, price in (live_asks or {}).get(ref, []):
                if seller != buyer and seller != me:
                    sup.append((3, seller, price))
            if not sup:
                continue
            kind, seller, price = max(sup, key=lambda s: (s[0], -(s[2] or 10 ** 6)))
            if kind >= 2 and price is not None and bid and price - bid > max(3, 0.15 * price):
                continue                                  # too far apart to meet: naming it is noise
            resp = max(responsiveness(bp) or 0, responsiveness(teams.get(seller, {})) or 0)
            score = (bid or 10) * (1 + resp) + 5 * kind
            if kind >= 2 and price is not None and bid:
                text = (f"{ref}: {buyer} wants it (bid {bid}), {seller} sells it at {price}; "
                        + ("they cross: post both on v01." if bid >= price else f"split the {price - bid} P on v01."))
            elif kind >= 2:
                text = f"{ref}: {buyer} wants it, {seller} sells it at {price}: post both on v01."
            else:
                text = f"{ref}: {buyer} wants it" + (f" (bid {bid})" if bid else "") + f"; {seller} holds a copy: list it on v01."
            out.append((score, f"profile:{buyer}:{seller}:{ref}", text))
    return sorted(out, key=lambda x: -x[0])


def load(path="team_profiles.json"):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save(P, path="team_profiles.json"):
    with open(path + ".tmp", "w") as f:
        json.dump(P, f)
    import os
    os.replace(path + ".tmp", path)


def report(P, me="t06"):
    rows = []
    for t, pr in sorted((P.get("teams") or {}).items()):
        if t == me:
            continue
        ven = sorted(pr["venues"].items(), key=lambda kv: -kv[1])[:3]
        rows.append(f"{t}: wants {len(pr['wants'])} {sorted(pr['wants'])[:6]} | spares {len(pr['spares'])} {sorted(pr['spares'])[:6]} | "
                    f"holds {len(pr['holds'])} | lists on {ven} | trades {pr['trades']} | named {pr['named']} answered {pr['answered']}")
    return "\n".join(rows)


def selftest():
    P = {}
    ev = [{"id": 1, "tick": 10, "type": "offer.listed", "payload": {"offer": {"maker": "t12", "venue": "rastro",
           "give": {"cash": 20}, "want": {"types": ["card:LAT-06"]}}}},
          {"id": 2, "tick": 11, "type": "offer.listed", "payload": {"offer": {"maker": "t08", "venue": "rastro",
           "give": {"assets": [{"id": 838, "ref": "LAT-06"}]}, "want": {"cash": 22}}}},
          {"id": 3, "tick": 12, "type": "pack.opened", "payload": {"team": "t13", "best": {"id": 900, "ref": "MAL-10"}}},
          {"id": 4, "tick": 12, "type": "offer.listed", "payload": {"offer": {"maker": "t09", "venue": "v21",
           "give": {"cash": 56}, "want": {"types": ["card:MAL-10"]}}}},
          {"id": 5, "tick": 13, "type": "offer.listed", "payload": {"offer": {"maker": "t13", "venue": "rastro",
           "give": {"assets": [{"id": 5, "ref": "LAV-01"}]}, "want": {"cash": 3}}}},
          {"id": 6, "tick": 14, "type": "venue.announcement", "payload": {"venue": "v07", "text": "t12 bids 20, t08 asks 22: post on v07"}},
          {"id": 7, "tick": 16, "type": "offer.listed", "payload": {"offer": {"maker": "t08", "venue": "v07",
           "give": {"assets": [{"id": 838, "ref": "LAT-06"}]}, "want": {"cash": 21}}}},
          {"id": 8, "tick": 18, "type": "thread.opened", "payload": {"team": "t15", "with": "chato", "topic": {"buy": {"card": "RET-09"}}}}]
    assert update(P, ev) == 8 and update(P, ev) == 0, "events are folded once"
    t = P["teams"]
    assert t["t12"]["wants"]["LAT-06"][0] == 20 and t["t08"]["spares"]["LAT-06"][0] == 21
    assert t["t13"]["holds"]["900"] == "MAL-10" and "RET-09" in t["t15"]["wants"]
    assert t["t08"]["named"] == 1 and t["t08"]["answered"] == 1 and responsiveness(t["t08"]) == 1.0
    assert t["t12"]["named"] == 1 and t["t12"]["answered"] == 0 and responsiveness(t["t12"]) == 0.0
    m = match(P, me="t06")
    keys = [k for _s, k, _t in m]
    assert "profile:t12:t08:LAT-06" in keys and "profile:t09:t13:MAL-10" in keys, keys
    assert "split the 1 P on v01" in next(x for _s, k, x in m if k == "profile:t12:t08:LAT-06")
    # a settlement moves the copy and satisfies the want
    update(P, [{"id": 9, "tick": 20, "type": "settlement", "payload": {"venue": "v07", "items": [
        {"kind": "card", "id": 838, "ref": "LAT-06", "frm": "t08", "to": "t12"}], "price": 21}}])
    assert "LAT-06" not in t["t12"]["wants"] and t["t12"]["holds"]["838"] == "LAT-06" and "838" not in t["t08"]["holds"]
    print("team_profiles selftest OK")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    elif "--report" in sys.argv:
        print(report(load()))
