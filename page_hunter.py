"""Page hunter: complete the album pages we value most, financed by cards that are worth little to us.

    BAZAAR_KEY=tk-... python3 page_hunter.py          # El Rastro only; never talks to a dealer
    python3 page_hunter.py --selftest                 # offline checks

Why: a complete page earns a bonus (catalog values.page_bonus x the page's value to us). With 8 of 10 Lavapies page cards
the two missing rares are worth far more to us than their market price, but cash is the limit. So:

  sell   single cards of sets we do not build (affinity < FOCUS_AFF) at a price over what they are worth to us
         (smart_agent.py sells duplicates; this agent only touches single copies, so they never overlap)
  buy    missing page cards of the sets we build, when cost (price + fee) <= buy cap, where
         cap = our value of the card + bonus / (2 * missing cards)   (half the bonus, shared: we may not finish the page)
  bid    a standing bid at BID_FRAC of the cap for each missing card, once we have the cash

It leaves dealers alone (one agent per dealer) and never accepts more than the one accept a tick the team has;
a refused accept costs nothing.
"""
import json
import math
import os
import sys
import time

from bazaar_sdk import Bazaar, BazaarError, _Http
from bz.core.accept_gate import try_reserve        # hotfix 1.4: one accept per tick across processes
from bz.trading.mode import legacy_should_trade    # TRADING_V2=on: trading_v2.py is the only trading authority

URL = os.environ.get("BAZAAR_URL", "https://bazaar.causaprima.ai")
FOCUS_AFF = float(os.environ.get("FOCUS_AFF", "1.0"))   # sets with affinity >= this are the ones we build
BID_FRAC = 0.6
# Organiser's key rule: never lose money over time. Buys stay under our value and our open bids count as committed cash,
# so we never promise more than we hold. No cash floor by default.
CASH_RESERVE = int(os.environ.get("CASH_RESERVE", "0"))


def spendable(me, mine):
    """Cash we may commit: over the reserve, minus what our own open bids already promise."""
    committed = sum((o.get("give") or {}).get("cash") or 0 for o in mine or [] if o.get("maker") == me.get("id"))
    return max(0, me["cash"] - CASH_RESERVE - committed)
MAX_PAY = json.loads(os.environ.get("MAX_PAY", "{}"))   # optional per-card price ceilings (none by default): the cap comes from our value
SELL_MARGIN = 1.1          # we ask at least 1.1x what the card is worth to us, plus 1 prima: sell at the price the market pays
MAX_POSTS = 3
MAX_MISSING = 3            # count the page bonus only when this few page cards are missing
MAX_OPEN = 28


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def fee_of(venue, price):
    return math.ceil(venue.get("fee_bps", 0) * price / 10000) + venue.get("fee_per_card", 0)


def cards_of(catalog, set_id):
    return next(s for s in catalog["sets"] if s["id"] == set_id)["cards"]


def value_of_card(card, set_id, affinity):
    return card["book"] * affinity.get(set_id, 1.0)          # first copy of the card


def focus_sets(catalog, affinity):
    return [s["id"] for s in catalog["sets"] if s.get("released") and affinity.get(s["id"], 0) >= FOCUS_AFF]


def page_targets(catalog, affinity, have):
    """[(ref, our value, cap)] for missing page cards of the sets we build. Cap = value + half the shared bonus."""
    out = []
    for sid in focus_sets(catalog, affinity):
        page = [c for c in cards_of(catalog, sid) if c["page"]]
        page_value = sum(value_of_card(c, sid, affinity) for c in page)
        bonus = catalog["values"]["page_bonus"] * page_value
        missing = [c for c in page if c["id"] not in have]
        share = bonus / (2 * len(missing)) if 0 < len(missing) <= MAX_MISSING else 0.0   # far from done: no bonus credit
        for c in missing:
            v = value_of_card(c, sid, affinity)
            out.append((c["id"], round(v, 1), min(v + share, MAX_PAY.get(c["id"], float("inf"))), sid))
    return out


def sell_stock(catalog, affinity, assets, competitors, listed):
    """[(asset id, ask, ref, our value)]: single copies in sets we do not build, asked above our value."""
    counts = {}
    for a in assets:
        if a["kind"] == "card":
            counts[a["ref"]] = counts.get(a["ref"], 0) + 1
    plan = []
    for a in assets:
        if a["kind"] != "card" or counts[a["ref"]] != 1 or a["id"] in listed:
            continue
        sid = a["ref"].split("-")[0]
        if affinity.get(sid, 1.0) >= FOCUS_AFF:
            continue
        card = next(c for c in cards_of(catalog, sid) if c["id"] == a["ref"])
        v = max(value_of_card(card, sid, affinity), float(a.get("your_value") or 0))   # the server's value has the
        floor = math.ceil(v * SELL_MARGIN + 1)                                        # page bonus ours lacks
        comp = competitors.get(a["ref"])
        target = (min(comp) - 1) if comp else math.ceil(card["book"] * 1.2)
        ask = max(floor, min(target, math.ceil(card["book"] * 1.2)))
        if ask >= floor:
            plan.append((a["id"], ask, a["ref"], round(v, 1)))
    return sorted(plan, key=lambda p: -(p[1] - p[3]))      # biggest gain first


def sell_bid_choice(catalog, affinity, assets, offers, venue, me_id, venues=None):
    """Best existing bid, on any venue we were given, for a card we hold. We accept, so we pay that venue's fee:
    gain = bid - fee - what the copy is worth to us. Worth = the server's your_value for that very copy (page bonus
    included: a card that completes a page is never sold under it), never less than our own estimate. Sets we build
    keep their only copy whatever the bid. `venues`: {venue id: venue dict with fee_bps / fee_per_card}."""
    counts, held = {}, {}
    for a in assets:
        if a["kind"] == "card":
            counts[a["ref"]] = counts.get(a["ref"], 0) + 1
            if a["ref"] not in held or float(a.get("your_value") or 0) < float(held[a["ref"]].get("your_value") or 0):
                held[a["ref"]] = a                       # the copy worth least to us is the one we sell
    best = None
    for o in offers:
        g, w = o["give"], o["want"]
        if o["maker"] == me_id or o.get("status", "open") != "open" or o.get("to") not in (None, me_id):
            continue
        if not g["cash"] or g["assets"] or len(w["types"]) != 1 or not w["types"][0].startswith("card:"):
            continue
        ref = w["types"][0].split(":", 1)[1]
        sid = ref.split("-")[0]
        if ref not in held or (counts[ref] == 1 and affinity.get(sid, 1.0) >= FOCUS_AFF):
            continue
        card = next((c for c in cards_of(catalog, sid) if c["id"] == ref), None)
        if not card:
            continue
        v = max(value_of_card(card, sid, affinity) if counts[ref] == 1 else 0.0, float(held[ref].get("your_value") or 0))
        ov = o.get("venue") or venue
        vinfo = (venues or {}).get(ov) if isinstance(ov, str) else (ov if isinstance(ov, dict) else None)
        fee = (math.ceil((vinfo.get("fee_bps") or 0) * g["cash"] / 10000) + (vinfo.get("fee_per_card") or 0)) if vinfo \
            else fee_of(ov, g["cash"])
        gain = g["cash"] - fee - v
        if gain >= max(2.0, 0.1 * v) and (best is None or gain > best["gain"]):
            best = {"offer": o["id"], "ref": ref, "asset": held[ref]["id"], "bid": g["cash"], "value": round(v, 1),
                    "gain": round(gain, 1), "venue": ov}
    return best


SKIP_VENUES = {"v01", "v07"}       # ours (we cannot trade there) and Team 10's (a trade there scores for our closest rival)


def other_bids(b, me_id):
    """Open cash bids for a card on every open team venue but SKIP_VENUES (public reads), each tagged with its venue,
    and {venue: venue dict} for their fees. Team 10 sells and buys across the board (SAL-11 at 207, LAV-11 at 210):
    the buyers who need our cards are often not on El Rastro."""
    pub = _Http(URL, {}, 15.0, False, 1)                 # keyless public reads: the team key's 5/s stay with the agents
    try:
        venues = {v["venue"]: v for v in pub._call("GET", "/api/venues").get("venues", []) if v.get("status") == "open"}
    except BazaarError:
        return [], {}
    out = []
    for vid in venues:
        if vid in SKIP_VENUES or vid == "rastro":
            continue
        try:
            for o in pub._call("GET", f"/api/venues/{vid}/offers").get("offers", []):
                if (o.get("give") or {}).get("cash") and o.get("maker") != me_id:
                    out.append(dict(o, venue=vid))
        except BazaarError:
            continue
    return out, venues


def buy_choice(targets, offers, venue, cash, me_id):
    """Best acceptable ask for a missing page card: cost within cap and within cash. None if nothing qualifies."""
    cap = {t[0]: t[2] for t in targets}
    best = None
    for o in offers:
        g, w = o["give"], o["want"]
        if o["maker"] == me_id or o["status"] != "open" or o.get("to") not in (None, me_id):
            continue
        if len(g["assets"]) != 1 or g["cash"] or not w["cash"] or w["types"]:
            continue
        ref = g["assets"][0]["ref"]
        if ref not in cap:
            continue
        cost = w["cash"] + fee_of(venue, w["cash"])
        if cost <= cap[ref] and cost <= cash and cap[ref] - cost >= max(2.0, 0.1 * cost):   # a buy at zero gain is noise
            gain = cap[ref] - cost
            if best is None or gain > best["gain"]:
                best = {"offer": o["id"], "ref": ref, "cost": cost, "cap": round(cap[ref], 1), "gain": round(gain, 1)}
    return best


def bid_plan(targets, cash, my_bids):
    """[(ref, price)] standing bids for missing page cards we have no bid on yet and can afford."""
    plan = []
    for ref, v, cap, sid in targets:
        price = int(cap * BID_FRAC)
        if ref not in my_bids and price >= 1 and price <= cash:
            plan.append((ref, price))
    return plan


def step(b, me, catalog, venue):
    offers = b.board("rastro").get("offers", [])
    mine = [o for o in b.my_offers().get("offers", []) if o["maker"] == me["id"]]
    directed = [o for o in b.my_offers().get("offers", []) if o.get("to") == me["id"]]
    listed = {a["id"] for o in mine for a in o["give"]["assets"]}
    my_bids = {t.split(":", 1)[1] for o in mine for t in o["want"]["types"] if t.startswith("card:")}
    have = {a["ref"] for a in me["assets"] if a["kind"] == "card"}
    for o in mine:                                       # a bid for a card we now hold would buy a near-worthless duplicate
        refs = [t.split(":", 1)[1] for t in o["want"].get("types") or [] if t.startswith("card:")]
        if refs and not o["give"].get("assets") and all(r in have for r in refs):
            try:
                b.cancel(o["id"])
                log(f"CANCEL bid {o['id']} for {refs}: we already hold it")
            except BazaarError as e:
                log("cancel refused:", e.code, e.message)
    # Saturday 22:4x: RET-06 was listed at 30 when it was worth 22.5; the page then completed (RET-03 and RET-05 bought),
    # its value jumped to ~82, the stale listing filled and broke the page (-55). So every tick: never offer a card of a
    # complete page, and cancel any of our sell offers that is now under the copy's current value.
    complete = {p["set"] for p in (me.get("album") or {}).get("pages", []) if p.get("complete")}
    worth = {a["id"]: float(a.get("your_value") or 0) for a in me["assets"]}
    for o in list(mine):
        ga = o["give"].get("assets") or []
        if len(ga) != 1 or o.get("status", "open") != "open" or o.get("thread"):
            continue
        ref = ga[0].get("ref", "")
        if not o["want"].get("cash"):                 # smart_agent's swap: only a true spare may leave (2+ copies held)
            stale, why = sum(a.get("ref") == ref for a in me["assets"]) < 2, "it is no longer a spare"
        else:
            under = o["want"]["cash"] < math.ceil(worth.get(ga[0]["id"], 0) * SELL_MARGIN + 1)
            stale, why = under or ref.split("-")[0] in complete, ("under its value now" if under else "its page is complete")
        if stale:
            try:
                b.cancel(o["id"])
                mine.remove(o)
                listed.discard(ga[0]["id"])
                log(f"CANCEL offer {o['id']} {ref}: {why}")
            except BazaarError as e:
                log("cancel refused:", e.code, e.message)
    me = dict(me, assets=[a for a in me["assets"] if a.get("ref", "").split("-")[0] not in complete])  # never sold
    targets = page_targets(catalog, me["affinity"], have)
    # Our cap spread the page bonus over every missing card (CHA-04: 35), but the server puts it only on the LAST missing
    # card (CHA-04: your_value 13), and a team-trade loss counts in full: the cap never goes over the server's value.
    sv = {}
    for t in targets[:2 * MAX_MISSING]:
        try:
            sv[t[0]] = float(b.value(t[0]).get("your_value") or 0)
        except BazaarError:
            sv[t[0]] = t[1]
    targets = [(r, v, min(cap, sv.get(r, v)), sid) for r, v, cap, sid in targets]
    comp = {}
    for o in offers:
        if o["maker"] != me["id"] and len(o["give"]["assets"]) == 1 and o["want"]["cash"]:
            comp.setdefault(o["give"]["assets"][0]["ref"], []).append(o["want"]["cash"])
    log(f"cash {me['cash']} | missing page cards {[(t[0], t[1], round(t[2])) for t in targets]} | open offers {len(mine)}")
    best = buy_choice(targets, offers + directed, venue, spendable(me, mine), me["id"])
    if best:
        if not try_reserve(me["tick"]):
            log("buy skipped: another process already used this tick's accept")
        else:
            try:
                b.accept(best["offer"])
                log(f"BUY {best['ref']}: cost {best['cost']} cap {best['cap']} gain {best['gain']}")
            except BazaarError as e:
                log("buy refused:", e.code, e.message)
    if not best:
        others, vinfo = other_bids(b, me["id"])
        sb = sell_bid_choice(catalog, me["affinity"], me["assets"], offers + directed + others, venue, me["id"], vinfo)
        if sb and not try_reserve(me["tick"]):
            log("sell-to-bid skipped: another process already used this tick's accept")
        elif sb:
            try:
                b.accept(sb["offer"], assets=[sb["asset"]])
                log(f"SELL to bid {sb['ref']}: bid {sb['bid']}, worth {sb['value']} to us, gain {sb['gain']}")
            except BazaarError as e:
                log("sell-to-bid refused:", e.code, e.message)
    posted = 0
    for aid, ask, ref, v in sell_stock(catalog, me["affinity"], me["assets"], comp, listed):
        if posted >= MAX_POSTS or len(mine) + posted >= MAX_OPEN:
            break
        try:
            b.list_offer({"assets": [aid]}, {"cash": ask}, venue="rastro")
            posted += 1
            log(f"SELL single {ref} (asset {aid}) at {ask}; worth {v} to us (not in a set we build)")
        except BazaarError as e:
            log("sell refused:", e.code, e.message)
            break
    for ref, price in bid_plan(targets, spendable(me, mine), my_bids):
        if posted >= MAX_POSTS or len(mine) + posted >= MAX_OPEN:
            break
        try:
            b.list_offer({"cash": price}, {"cards": [ref]}, venue="rastro")
            posted += 1
            log(f"BID for {ref} at {price}")
        except BazaarError as e:
            log("bid refused:", e.code, e.message)
            break


def main():
    b = Bazaar(URL, os.environ["BAZAAR_KEY"], wait_on_tick=False)
    log("page_hunter started (Rastro only, no dealers)")
    while True:
        try:
            if b.clock().get("paused"):
                time.sleep(10)
                continue
            if not legacy_should_trade():                       # TRADING_V2=on: stand aside this tick
                b.wait_tick()
                continue
            venue = next((v for v in b.venues()["venues"] if v["venue"] == "rastro"), {"fee_bps": 500, "fee_per_card": 1})
            step(b, b.me(), b.catalog(), venue)
            b.wait_tick()
        except KeyboardInterrupt:
            break
        except BazaarError as e:
            log("api error:", e.code, e.message)
            time.sleep(3)
        except Exception as e:
            log(f"error {type(e).__name__}: {e}")
            time.sleep(5)


def selftest():
    mk = lambda sid, rar_book: [{"id": f"{sid}-{i + 1:02d}", "book": bk, "page": i < 10, "rarity": r}
                                for i, (bk, r) in enumerate(rar_book)]
    layout = [(10, "c")] * 5 + [(25, "u")] * 3 + [(70, "r")] * 2 + [(180, "e"), (450, "l")]
    catalog = {"sets": [{"id": "LAV", "released": True, "cards": mk("LAV", layout)},
                        {"id": "LAT", "released": True, "cards": mk("LAT", layout)}],
               "values": {"page_bonus": 0.25, "copy_marginals": [1, .25, .1]}}
    aff = {"LAV": 1.6, "LAT": 0.7}
    have = {f"LAV-{i:02d}" for i in range(1, 9)}
    t = page_targets(catalog, aff, have)
    assert [x[0] for x in t] == ["LAV-09", "LAV-10"] and t[0][1] == 112.0
    bonus = 0.25 * (5 * 16 + 3 * 40 + 2 * 112)
    assert abs(t[0][2] - (112 + bonus / 4)) < 1e-6, t
    venue = {"fee_bps": 500, "fee_per_card": 1}
    mkoff = lambda i, ref, cash, maker="tx": {"id": i, "maker": maker, "status": "open", "to": None,
                                               "give": {"cash": 0, "types": [], "assets": [{"ref": ref, "id": i}]},
                                               "want": {"cash": cash, "assets": [], "types": []}}
    offers = [mkoff(1, "LAV-09", 110), mkoff(2, "LAV-10", 200), mkoff(3, "LAT-02", 5)]
    assert buy_choice(t, offers, venue, 40, "t06") is None, "no cash: no buy"
    far = page_targets(catalog, aff, {"LAV-01"})            # 9 missing: no bonus credit, cap = plain value
    assert abs(far[1][2] - far[1][1]) < 1e-9
    ch = buy_choice(t, offers, venue, 500, "t06")
    assert ch and ch["ref"] == "LAV-09" and ch["cost"] == 117, ch      # 110 + 5% + 1; LAV-10 at 200 is over the cap
    assets = [{"id": 90, "kind": "card", "ref": "LAT-10", "serial": 2}, {"id": 91, "kind": "card", "ref": "LAV-01", "serial": 1},
              {"id": 92, "kind": "card", "ref": "LAT-04", "serial": 1}, {"id": 93, "kind": "card", "ref": "LAT-04", "serial": 2}]
    plan = sell_stock(catalog, aff, assets, {}, set())
    refs = [p[2] for p in plan]
    assert refs == ["LAT-10"], f"only single copies of sets we do not build: {refs}"      # LAT-04 is a duplicate; LAV is a focus set
    assert plan[0][1] >= math.ceil(49 * SELL_MARGIN + 1)
    plan2 = sell_stock(catalog, aff, assets, {"LAT-10": [60]}, set())
    assert plan2[0][1] >= math.ceil(49 * SELL_MARGIN + 1), "undercutting never goes under our floor"
    bidoff = lambda i, ref, cash: {"id": i, "maker": "tx", "status": "open", "to": None, "give": {"cash": cash, "assets": [], "types": []},
                                   "want": {"cash": 0, "assets": [], "types": [f"card:{ref}"]}}
    sb = sell_bid_choice(catalog, aff, assets, [bidoff(7, "LAT-10", 55), bidoff(8, "LAT-10", 70), bidoff(9, "LAV-01", 99), bidoff(10, "LAT-04", 50)], venue, "t06")
    assert sb and sb["offer"] == 10 and sb["ref"] == "LAT-04", sb  # a duplicate is trading stock (Payday deck): 50 for it wins
    sb = sell_bid_choice(catalog, aff, assets, [bidoff(7, "LAT-10", 55), bidoff(8, "LAT-10", 70), bidoff(9, "LAV-01", 99)], venue, "t06")
    assert sb and sb["offer"] == 8 and sb["gain"] > 0, sb          # best bid; LAV is a set we build: its only copy stays
    rich = [dict(a, your_value=80.0) if a["ref"] == "LAT-10" else a for a in assets]
    assert sell_bid_choice(catalog, aff, rich, [bidoff(8, "LAT-10", 70)], venue, "t06") is None, "server value 80 > bid 70"
    assert sell_bid_choice(catalog, aff, assets, [bidoff(7, "LAT-10", 52)], venue, "t06") is None, "52 less fee barely covers 49: no deal"
    assert bid_plan(t, 40, set()) == [] and len(bid_plan(t, 500, set())) == 2 and bid_plan(t, 500, {"LAV-09"})[0][0] == "LAV-10"
    print("selftest OK")


if __name__ == "__main__":
    selftest() if "--selftest" in sys.argv else main()
