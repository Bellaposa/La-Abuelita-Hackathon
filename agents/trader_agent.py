"""Team-to-team trader: scans venue boards, accepts offers that raise OUR private value, lists our duplicates, bids for gaps.

    BAZAAR_KEY=tk-... python -m agents.trader_agent       # runs forever, one pass per tick
    python -m agents.trader_agent --selftest              # offline: synthetic books + FakeBazaar smoke run

Rules it keeps (see RULES.md "Trading with other teams"):
  * words never count: an offer is accepted only if its give/want structure matches one of a few strict shapes, and the gain is
    computed from the structure alone (unknown keys, packs, bare asset ids, mixed cash+cards -> ignored);
  * gain = our value of what we get - our value of what we give up - cash paid - fee; must be >= MIN_GAIN and >= 8 % of the cost;
  * never sell our last copy cheaply (loss includes the page credit), never accept at negative value;
  * one accept per tick, <= LISTINGS_PER_TICK new listings, <= MAX_OPEN offers;
  * fair play: at most MAX_DEALS_PER_MAKER deals with the same counterparty per DEAL_WINDOW seconds, no gifts ever;
  * spend cap: BUDGET_FILE json {"trader": max cash} (re-read every tick) limits what the trader may spend.
"""
from __future__ import annotations

import json
import math
import os
import sys
import time

from bazaar_sdk import BazaarError

try:
    from agents import portfolio_agent as pf
except ImportError:  # run as a script from inside agents/
    import portfolio_agent as pf  # type: ignore

URL = os.environ.get("BAZAAR_URL", "https://bazaar.causaprima.ai")
BUDGET_FILE = os.environ.get("BUDGET_FILE", "")
CASH_RESERVE = int(os.environ.get("CASH_RESERVE", "0"))
MIN_GAIN = pf.MIN_GAIN
MIN_ROI = 0.08
LISTINGS_PER_TICK = 8        # server allows 12; leave headroom for the 5 req/s limit
MAX_OPEN = 28                # server allows 30
MAX_BIDS = 3
BID_SHARE = 0.6              # we bid 60 % of our buy ceiling: the rest is our gain
MIN_BID_VALUE = 12
MAX_DEALS_PER_MAKER = 3
DEAL_WINDOW = 600.0
PREFILTER_SLACK = 2.0        # skip b.value() when the model says the offer is hopeless
MAX_VALUE_CALLS = 12
LIST_MARKUP = 0.95           # no competing ask: ask this share of book (a team missing the card values it at ~book)
LIST_CAP = 1.3               # never ask more than this x book


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


# ------------------------------------------------------------------ strict structure parsing

_SIDE_KEYS = {"cash", "assets", "types", "cards"}


def parse_side(side):
    """-> {'cash': int, 'cards': [ref], 'assets': [asset dicts], 'types': [ref]} or None when anything is unexpected."""
    if side is None:
        return {"cash": 0, "assets": [], "types": []}
    if not isinstance(side, dict):
        return None
    for k, v in side.items():
        if k not in _SIDE_KEYS and v not in (None, 0, [], {}, ""):
            return None
    cash = side.get("cash") or 0
    if isinstance(cash, bool) or not isinstance(cash, (int, float)) or cash < 0:
        return None
    assets = []
    for a in side.get("assets") or []:
        if not isinstance(a, dict) or a.get("kind") != "card" or not a.get("ref") or a.get("id") is None:
            return None                      # packs, bare ids, or unreadable assets: not valued, not accepted
        assets.append({"id": a["id"], "ref": a["ref"]})
    types = []
    for t in list(side.get("types") or []) + list(side.get("cards") or []):
        if not isinstance(t, str):
            return None
        if ":" in t:
            kind, ref = t.split(":", 1)
            if kind != "card":
                return None
        else:
            ref = t
        types.append(ref)
    return {"cash": int(cash), "assets": assets, "types": types}


def classify(o):
    """('buy'|'sell'|'swap'|None, give, want). buy: they give cards for our cash; sell: they give cash for our card;
    swap: their card for our card (no cash)."""
    g, w = parse_side(o.get("give")), parse_side(o.get("want"))
    if g is None or w is None:
        return None, None, None
    if g["assets"] and not g["cash"] and not g["types"] and w["cash"] and not w["assets"] and not w["types"]:
        return "buy", g, w
    if g["cash"] and not g["assets"] and not g["types"] and len(w["types"]) == 1 and not w["cash"] and not w["assets"]:
        return "sell", g, w
    if g["assets"] and not g["cash"] and not g["types"] and len(w["types"]) == 1 and not w["cash"] and not w["assets"] \
            and len(g["assets"]) == 1:
        return "swap", g, w
    return None, None, None


# ------------------------------------------------------------------ evaluating one offer

def evaluate(o, me, venue, valuer, catalog, spendable, counts, ids, deals=None, now=None):
    """Opportunity dict {kind, offer, gain, roi, cost, assets, why} or None. Pure given `valuer` (needs next_copy/model_next/loss)."""
    me_id = me["id"]
    if o.get("status", "open") != "open" or o.get("maker") == me_id:
        return None
    if o.get("to") not in (None, me_id):
        return None
    if o.get("thread") is not None:            # dealer standing offers belong to the dealer agents
        return None
    maker = o.get("maker")
    if deals is not None and maker is not None:
        t = time.time() if now is None else now
        if sum(1 for ts, m in deals if m == maker and t - ts < DEAL_WINDOW) >= MAX_DEALS_PER_MAKER:
            return None
    kind, g, w = classify(o)
    if kind is None:
        return None
    marg = pf.marginals(catalog)
    aff = me.get("affinity") or {}
    idx = valuer.idx

    if kind == "buy":
        price = w["cash"]
        refs = [a["ref"] for a in g["assets"]]
        if any(r not in idx for r in refs):
            return None
        cost = price + pf.fee_of(venue, price, len(refs))
        if cost > spendable or price < 1:
            return None
        model = 0.0
        seen = {}
        for r in refs:
            k = seen.get(r, 0)
            model += pf.marginal_value(r, counts.get(r, 0) + k, catalog, aff, counts, idx)
            seen[r] = k + 1
        if model * PREFILTER_SLACK + 10 < cost:
            return None
        value, seen = 0.0, {}
        for r in refs:
            k = seen.get(r, 0)
            value += valuer.next_copy(r) if k == 0 else pf.marginal_value(r, counts.get(r, 0) + k, catalog, aff, counts, idx)
            seen[r] = k + 1
        gain = value - cost
        if gain < max(MIN_GAIN, MIN_ROI * cost):
            return None
        return {"kind": "buy", "offer": o["id"], "maker": maker, "refs": refs, "price": price, "cost": cost,
                "value": round(value, 1), "gain": round(gain, 1), "roi": round(gain / cost, 3), "assets": None}

    if kind == "sell":
        ref, bid = w["types"][0], g["cash"]
        n = counts.get(ref, 0)
        if n < 2 or ref not in idx or not ids.get(ref):
            return None                         # never sell our only copy to a bid
        loss = valuer.loss(ref, n)
        net = bid - pf.fee_of(venue, bid)       # worst case: we pay the fee as taker
        gain = net - loss
        if gain < max(MIN_GAIN, 0.1 * loss) or net < pf.sell_floor(ref, n, catalog, aff, counts):
            return None
        spare = sorted(ids[ref], key=lambda a: -(a.get("serial") or 0))[0]
        if spare.get("locked"):
            return None
        return {"kind": "sell", "offer": o["id"], "maker": maker, "refs": [ref], "price": bid, "cost": 0,
                "value": round(loss, 1), "gain": round(gain, 1), "roi": round(gain, 2), "assets": [spare["id"]]}

    # swap: their card for one of ours
    their, ours = g["assets"][0]["ref"], w["types"][0]
    if their not in idx or ours not in idx or their == ours or counts.get(ours, 0) < 1 or not ids.get(ours):
        return None
    fees = 2 * venue.get("fee_per_card", 1)
    v_in = valuer.next_copy(their)
    n = counts.get(ours, 0)
    loss = valuer.loss(ours, n)
    gain = v_in - loss - fees
    if gain < max(MIN_GAIN, 0.1 * loss) or fees > spendable:
        return None
    spare = sorted(ids[ours], key=lambda a: -(a.get("serial") or 0))[0]
    if spare.get("locked"):
        return None
    return {"kind": "swap", "offer": o["id"], "maker": maker, "refs": [their], "price": 0, "cost": fees,
            "value": round(v_in, 1), "gain": round(gain, 1), "roi": round(gain / max(1, fees + loss), 3), "assets": [spare["id"]]}


def pick(opps):
    """Best by rank-sum of absolute gain and roi."""
    if not opps:
        return None
    by_gain = {id(x): r for r, x in enumerate(sorted(opps, key=lambda x: -x["gain"]))}
    by_roi = {id(x): r for r, x in enumerate(sorted(opps, key=lambda x: -x["roi"]))}
    return min(opps, key=lambda x: (by_gain[id(x)] + by_roi[id(x)], -x["gain"]))


# ------------------------------------------------------------------ our listings

def competing_asks(offers, me_id):
    comp = {}
    for o in offers:
        if o.get("maker") == me_id or o.get("status", "open") != "open":
            continue
        k, g, w = classify(o)
        if k == "buy" and len(g["assets"]) == 1:
            comp.setdefault(g["assets"][0]["ref"], []).append(w["cash"])
    return comp


def listing_plan(me, catalog, offers, listed_assets, max_new):
    """[(asset id, ask, ref, floor)] : spare copies, ask undercuts the cheapest rival ask (never below our floor)."""
    comp = competing_asks(offers, me["id"])
    plan = []
    for d in sorted(pf.sellable_duplicates(me, catalog, listed_assets), key=lambda d: -d["book"]):
        ref, floor, book = d["ref"], d["floor"], d["book"]
        target = (min(comp[ref]) - 1) if comp.get(ref) else math.ceil(book * LIST_MARKUP)
        ask = max(floor, min(target, math.ceil(book * LIST_CAP)))
        if ask < floor:
            continue
        plan.append((d["asset"], ask, ref, floor))
        if len(plan) >= max_new:
            break
    return plan


def bid_plan(me, catalog, venue, valuer, open_bid_refs, spendable, max_new):
    """[(ref, price, value)] for the best missing cards: we offer BID_SHARE of what we could pay."""
    out = []
    for t in pf.rank_targets(me, catalog)[:max_new + 3]:
        ref = t["ref"]
        if ref in open_bid_refs or t["value"] < MIN_BID_VALUE:
            continue
        v = valuer.next_copy(ref)
        price = int(BID_SHARE * pf.buy_ceiling(v, venue))
        if price < 1 or price + pf.fee_of(venue, price) > spendable:
            continue
        spendable -= price
        out.append((ref, price, round(v, 1)))
        if len(out) >= max_new:
            break
    return out


# ------------------------------------------------------------------ runtime

def read_budget():
    if not BUDGET_FILE:
        return None
    try:
        with open(BUDGET_FILE) as f:
            v = json.load(f).get("trader")
        return None if v is None else max(0, int(v))
    except (OSError, ValueError, AttributeError, TypeError):
        return None


def venue_id(v):
    return v.get("venue") or v.get("id")


def step(b, ctx):
    """One pass: open packs, accept the best offer across venues, list duplicates, post a few bids. Returns a small dict."""
    out = {"accepted": None, "listed": 0, "bids": 0}
    ctx.setdefault("deals", [])
    ctx.setdefault("skip_venues", set())
    me = b.me()
    catalog = ctx.get("catalog")
    if catalog is None:
        catalog = ctx["catalog"] = b.catalog()
    pf.open_packs(b, me, log)
    try:
        venues = b.venues().get("venues") or [{"venue": "rastro", "fee_bps": 500, "fee_per_card": 1}]
        mine = b.my_offers().get("offers", [])
    except BazaarError as e:
        log("trader: unreadable:", e.code)
        return out
    me_id = me["id"]
    my_open = [o for o in mine if o.get("maker") == me_id]
    listed = {a["id"] for o in my_open for a in (o.get("give") or {}).get("assets") or [] if isinstance(a, dict)}
    committed = sum(int((o.get("give") or {}).get("cash") or 0) for o in my_open)
    budget = read_budget()
    spendable = me["cash"] - CASH_RESERVE - committed
    if budget is not None:
        spendable = min(spendable, budget - committed)
    spendable = max(0, spendable)

    valuer = pf.Valuer(b, me, catalog, MAX_VALUE_CALLS)
    counts, ids = pf.holdings(me)
    all_offers, opps = [], []
    boards = {}
    for v in venues:
        vid = venue_id(v)
        if not vid or vid in ctx["skip_venues"]:
            continue
        try:
            offers = b.board(vid).get("offers", [])
        except BazaarError as e:
            log("trader: board", vid, e.code)
            continue
        boards[vid] = (v, offers)
        if vid == "rastro":
            all_offers = offers
        for o in offers:
            op = evaluate(o, me, v, valuer, catalog, spendable, counts, ids, ctx["deals"])
            if op:
                op["venue"] = vid
                opps.append(op)
    seen = {o["id"] for _, offs in boards.values() for o in offs}
    for o in mine:                              # offers addressed to us that are not on a public board
        if o.get("to") == me_id and o["id"] not in seen:
            op = evaluate(o, me, venues[0], valuer, catalog, spendable, counts, ids, ctx["deals"])
            if op:
                op["venue"] = o.get("venue", "rastro")
                opps.append(op)
    best = pick(opps)
    if best:
        log(f"trader: best {best['kind']} {best['refs']} at {best['price']} gain {best['gain']} ({len(opps)} candidates)")
        try:
            b.accept(best["offer"], assets=best["assets"])
            out["accepted"] = best
            ctx["deals"].append((time.time(), best["maker"]))
            ctx["deals"] = ctx["deals"][-200:]
        except BazaarError as e:
            log("trader: accept refused:", e.code)
            if e.code == "self_venue":
                ctx["skip_venues"].add(best["venue"])

    room = MAX_OPEN - len(my_open)
    rastro = boards.get("rastro", ({"fee_bps": 500, "fee_per_card": 1}, []))[0]
    if room > 0:
        for asset, ask, ref, floor in listing_plan(me, catalog, all_offers, listed | {a for a in (best or {}).get("assets") or []},
                                                   min(LISTINGS_PER_TICK, room)):
            try:
                b.list_offer({"assets": [asset]}, {"cash": ask}, venue="rastro")
                out["listed"] += 1
                room -= 1
                log(f"trader: listed {ref} (asset {asset}) at {ask} (floor {floor})")
            except BazaarError as e:
                log("trader: listing refused:", e.code)
                break
    open_bid_refs = {t for o in my_open for t in (parse_side(o.get("want")) or {}).get("types", [])}
    if room > 0 and len(my_open) + out["listed"] < MAX_OPEN and not (best and best["kind"] == "buy"):
        for ref, price, v in bid_plan(me, catalog, rastro, valuer, open_bid_refs, spendable,
                                      min(MAX_BIDS - len(open_bid_refs), room, LISTINGS_PER_TICK - out["listed"])):
            if len(open_bid_refs) >= MAX_BIDS:
                break
            try:
                b.list_offer({"cash": price}, {"cards": [ref]}, venue="rastro")
                out["bids"] += 1
                open_bid_refs.add(ref)
                log(f"trader: bid {price} for {ref} (worth {v} to us)")
            except BazaarError as e:
                log("trader: bid refused:", e.code)
                break
    return out


def run_forever():
    from bazaar_sdk import Bazaar
    b = Bazaar(URL, os.environ["BAZAAR_KEY"], wait_on_tick=False)
    ctx = {}
    while True:
        try:
            step(b, ctx)
        except BazaarError as e:
            log("trader error:", e)
        except Exception as e:  # keep the loop alive
            log("trader crash:", repr(e))
        try:
            b.wait_tick()
        except Exception:
            time.sleep(5)


# ------------------------------------------------------------------ selftest

def _selftest_world():
    cat = pf.toy_catalog()
    aff = {"LAV": 2.0, "MAL": 0.5, "RET": 1.0}
    assets = [{"id": 1, "kind": "card", "ref": "LAV-01", "serial": 5}, {"id": 2, "kind": "card", "ref": "LAV-01", "serial": 9},
              {"id": 3, "kind": "card", "ref": "MAL-01", "serial": 1}, {"id": 4, "kind": "card", "ref": "MAL-01", "serial": 2},
              {"id": 5, "kind": "card", "ref": "MAL-02", "serial": 2}]
    for i in range(2, 10):
        assets.append({"id": 100 + i, "kind": "card", "ref": f"LAV-{i:02d}", "serial": 1})   # LAV page missing only LAV-10
    me = {"id": "t1", "cash": 300, "affinity": aff, "assets": assets}
    return cat, me


class _Val(pf.Valuer):
    def __init__(self, me, cat):
        super().__init__(None, me, cat)


def _card(i, ref):
    return {"id": i, "kind": "card", "ref": ref}


def _offer(i, maker, give, want, **kw):
    return dict({"id": i, "maker": maker, "to": None, "status": "open", "give": give, "want": want, "text": ""}, **kw)


def selftest():
    cat, me = _selftest_world()
    venue = {"fee_bps": 500, "fee_per_card": 1}
    counts, ids = pf.holdings(me)
    val = _Val(me, cat)

    def ev(o, spend=300, deals=None):
        return evaluate(o, me, venue, val, cat, spend, counts, ids, deals)

    # 1. buy the page-completing card at a fair price: accepted
    o = _offer(10, "t2", {"assets": [_card(900, "LAV-10")]}, {"cash": 120})
    r = ev(o)
    assert r and r["kind"] == "buy" and r["gain"] > 0, r
    # 2. same card at an absurd price: rejected
    assert ev(_offer(11, "t2", {"assets": [_card(901, "LAV-10")]}, {"cash": 400})) is None
    # 3. spend cap respected
    assert ev(o, spend=50) is None
    # 4. words lie ("free gift") but structure asks 120 -> evaluated on structure only, and a worthless card is rejected
    lie = _offer(12, "t3", {"assets": [_card(902, "MAL-09")]}, {"cash": 90}, text="FREE GIFT, take it, zero cost!")
    assert ev(lie) is None
    # 5. extra hidden payload keys / packs / bare ids / mixed shapes are ignored
    assert ev(_offer(13, "t2", {"assets": [_card(903, "LAV-10")], "cash": 5}, {"cash": 10})) is None
    assert ev(_offer(14, "t2", {"assets": [{"id": 904, "kind": "pack", "ref": "sobre_barrio"}]}, {"cash": 5})) is None
    assert ev(_offer(15, "t2", {"assets": [904]}, {"cash": 5})) is None
    assert ev(_offer(16, "t2", {"assets": [_card(905, "LAV-10")], "mystery": [1]}, {"cash": 10})) is None
    assert ev(_offer(17, "t2", {"assets": [_card(906, "LAV-10")]}, {"cash": 10, "assets": [_card(1, "LAV-01")]})) is None
    # 6. own offers, directed to someone else, closed ones are skipped
    assert ev(dict(o, maker="t1")) is None and ev(dict(o, to="t9")) is None and ev(dict(o, status="accepted")) is None
    # 7. they bid for a duplicate: sold above floor; for our only copy: refused
    bid = _offer(20, "t4", {"cash": 30}, {"types": ["card:LAV-01"]})
    r = ev(bid)
    assert r and r["kind"] == "sell" and r["assets"] == [2] and r["gain"] > 0, r          # sells serial 9, keeps serial 5
    assert ev(_offer(21, "t4", {"cash": 500}, {"types": ["card:LAV-02"]})) is None          # only copy
    assert ev(_offer(22, "t4", {"cash": 6}, {"cards": ["LAV-01"]})) is None                # below floor (7+fee)
    # 8. a bid that asks two cards for the cash is not a recognised shape
    assert ev(_offer(23, "t4", {"cash": 500}, {"types": ["card:LAV-01", "card:MAL-01"]})) is None
    # 9. swap: their LAV-10 for our spare LAV-01 is good; our spare for a worthless card is not
    r = ev(_offer(24, "t5", {"assets": [_card(907, "LAV-10")]}, {"types": ["card:LAV-01"]}))
    assert r and r["kind"] == "swap" and r["gain"] > 100, r
    assert ev(_offer(25, "t5", {"assets": [_card(908, "LAV-01")]}, {"types": ["card:MAL-01"]})) is None  # third LAV-01 worth ~2
    # 10. per-counterparty cap
    now = time.time()
    assert ev(o, deals=[(now, "t2")] * MAX_DEALS_PER_MAKER) is None
    assert ev(o, deals=[(now - 10 * DEAL_WINDOW, "t2")] * 9) is not None
    # 11. pick prefers the stronger of two
    opps = [x for x in (ev(o), ev(bid)) if x]
    assert pick(opps) in opps
    # 12. listing plan: asks never below floor, undercut rivals, never the last copy
    board = [_offer(30, "t6", {"assets": [_card(950, "LAV-01")]}, {"cash": 12})]
    plan = listing_plan(me, cat, board, set(), 5)
    assert {p[2] for p in plan} == {"LAV-01", "MAL-01"}, plan
    for asset, ask, ref, floor in plan:
        assert ask >= floor and asset in (2, 3, 4), plan
    assert [p for p in plan if p[2] == "LAV-01"][0][1] == 11, plan
    assert listing_plan(me, cat, board, {2, 3, 4}, 5) == []
    # 13. bids: top target first, price well under value
    bp = bid_plan(me, cat, venue, val, set(), 300, 3)
    assert bp and bp[0][0] == "LAV-10" and bp[0][1] <= pf.buy_ceiling(bp[0][2], venue) * BID_SHARE + 1, bp
    assert bid_plan(me, cat, venue, val, {"LAV-10"}, 300, 3)[0][0] != "LAV-10"
    # 14. step() against a scripted board: one accept, listings, bids, never exceeding limits
    calls = {"accept": [], "list": []}

    class B:
        def me(self): return json.loads(json.dumps(me))
        def catalog(self): return cat
        def venues(self): return {"venues": [dict(venue, venue="rastro"), dict(venue, venue="mine")]}
        def my_offers(self): return {"offers": []}
        def board(self, v):
            if v == "mine":
                raise BazaarError("not_found", "x", 404)
            return {"offers": [o, lie, bid]}
        def value(self, ref): return {"your_value": val.model_next(ref)}
        def accept(self, i, assets=None): calls["accept"].append((i, assets)); return {"ok": True}
        def list_offer(self, give, want, venue=None, **kw): calls["list"].append((give, want, venue)); return {"ok": True}
        def open_pack(self, i): return {"cards": []}

    ctx = {}
    out = step(B(), ctx)
    assert len(calls["accept"]) == 1 and out["accepted"], calls
    assert 0 < len(calls["list"]) <= 12, calls
    assert all(c[2] == "rastro" for c in calls["list"])
    assert out["bids"] <= MAX_BIDS
    # 15. step() survives self_venue refusal
    class B2(B):
        def accept(self, i, assets=None): raise BazaarError("self_venue", "mine", 403)
    ctx2 = {}
    step(B2(), ctx2)
    assert "rastro" in ctx2["skip_venues"]
    print("trader_agent unit selftest OK")

    # 16. smoke run against the offline sim (its board is rough NPC-only)
    try:
        from sim.world import FakeBazaar, SimDone
        w = FakeBazaar(seed=3, market=True, max_ticks=40)
        c = {}
        n = 0
        try:
            while True:
                step(w, c)
                w.wait_tick()
                n += 1
        except SimDone:
            pass
        print(f"sim smoke OK: {n} ticks, cash {w.me()['cash']}, net worth {w.net_worth():.1f}")
    except ImportError:
        print("sim not importable, smoke skipped")
    print("trader_agent selftest OK")
    return True


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(0 if selftest() else 1)
    run_forever()
