"""The Workshop (El Taller): when to turn three spares into one card of the next rarity, and when to buy the third.

    python3 workshop.py --selftest              # offline checks
    python3 workshop.py --dry-run               # live: what we would craft or buy now, and why (no writes)

The rule from the level: POST /api/taller {"assets": [a, b, c]} turns three spare copies of ONE rarity (you keep at least
one of each card) into one random card of the next rarity. "The pull is luck, shown and never scored."

What that means for the score. Holding cards scores nothing; what scores is value GAINED IN TRADES with other teams (price
vs our private value), the dealer ladder and the duels. So a craft is only worth it when the card that comes out is worth
more as a TRADE than the three that go in. Each spare is valued by what we can expect to make by selling it:

    G(card) = P_sell(rarity) x max(0, sale_price(rarity) - fee - our value of that copy)

sale_price and P_sell come from the live market: team bids on the boards, team asks, and the prices dealers actually paid
teams in `settlement` events of the feed (Pilar and Chato buy uncommons and rares, Abuela commons). The output is a random
card of the next rarity among the released sets (uniform until our own and the feed's crafts tell otherwise):

    E_out = mean over candidates c of: G(c) if we already hold c (it is a spare: sellable)
                                         0    if c is new in a set we build (we keep it: no trade, no score)
                                         G(c) at 1.1 x its value + 1 if c is new elsewhere (page_hunter sells singles there)

    craft when  E_out - sum(G of the three cheapest eligible spares) >= CRAFT_MARGIN

Buying for the Workshop. Buying a copy we already hold scores (our value - price), usually negative, so we buy the missing
third input only when the whole operation still clears the margin:

    max price = our value of the bought copy + (E_out - sum(G of the two spares we have) - CRAFT_MARGIN) - fee

Never crafted: the last copy of a card, a card listed for sale or on the table with a dealer (cancel first), a card a team
is bidding for right now above its value (that one is sold, not crafted), or a card from a set where it would still help a
page we build. Every craft is logged with its inputs and output, and the output distribution is learned in memory.
"""
import json
import math
import statistics
import sys

RARITY_UP = {"common": "uncommon", "uncommon": "rare", "rare": "epic", "epic": "legendary"}
CRAFT_MARGIN = 3.0          # primas of expected trade gain a craft (or a buy-to-craft) must clear
SINGLE_SELL = (1.1, 1)      # page_hunter sells singles of sets we do not build at >= 1.1 x value + 1
FOCUS_AFF = 1.0             # sets with affinity >= this are the ones we build (same rule as page_hunter)
DEALER_P = {"common": 0.4, "uncommon": 0.5, "rare": 0.5, "epic": 0.3}   # extra sell probability when a dealer we sell to buys it
DEALER_BUYS = {"common": {"picaros"}, "uncommon": {"chato", "pilar", "picaros"}, "rare": {"chato", "pilar"}, "epic": {"pilar"}}
P_CAP = 0.9
OBS_KEEP = 120              # dealer/team settlement prices remembered per rarity
EVERY = 5                   # ticks between Workshop decisions


# ---------------------------------------------------------------- market model

def card_index(catalog):
    return {c["id"]: dict(c, set=s["id"], released=s.get("released", True)) for s in catalog["sets"] for c in s["cards"]}


def observe_settlements(mem, events, idx):
    """Remember what cards actually sold for (feed `settlement`), per rarity. Dealers buying from teams count too."""
    obs = mem.setdefault("ws_obs", {})
    seen = set(mem.setdefault("ws_seen", []))
    for e in events or []:
        if e.get("type") != "settlement" or e.get("id") in seen:
            continue
        seen.add(e.get("id"))
        p = e.get("payload") or {}
        cards = [i for i in p.get("items") or [] if i.get("kind") == "card"]
        price = p.get("price")
        if len(cards) != 1 or not isinstance(price, (int, float)) or price <= 0:
            continue
        r = cards[0].get("rarity") or (idx.get(cards[0].get("ref")) or {}).get("rarity")
        if r:
            obs.setdefault(r, []).append(float(price))
            obs[r] = obs[r][-OBS_KEEP:]
    mem["ws_seen"] = sorted(seen)[-2000:]


def market_model(boards, idx, mem, me_id, unlocked=("chato", "pilar")):
    """rarity -> {"price": what we can expect to sell one for, "p": probability it sells, "asks", "bids"}."""
    asks, bids = {}, {}
    for offers in boards.values():
        for o in offers:
            if o.get("maker") == me_id or o.get("status", "open") != "open":
                continue
            g, w = o.get("give") or {}, o.get("want") or {}
            ga = g.get("assets") or []
            if len(ga) == 1 and isinstance(ga[0], dict) and w.get("cash") and not g.get("cash"):
                r = ga[0].get("rarity") or (idx.get(ga[0].get("ref")) or {}).get("rarity")
                asks.setdefault(r, []).append(w["cash"])
            elif g.get("cash") and not ga:
                for t in w.get("types") or []:
                    ref = t.split(":", 1)[-1]
                    if ref in idx:
                        bids.setdefault(idx[ref]["rarity"], []).append(g["cash"])
    model = {}
    for r in ("common", "uncommon", "rare", "epic", "legendary"):
        a, b, s = sorted(asks.get(r, [])), sorted(bids.get(r, []), reverse=True), mem.get("ws_obs", {}).get(r, [])
        if s:
            price = statistics.median(s[-30:])                  # what really changed hands lately
        elif b:
            price = statistics.median(b[:3])                    # the best few bids
        elif a:
            price = 0.6 * a[0]                                  # nobody pays the cheapest ask; assume well under it
        else:
            price = 0.0
        if b:
            price = min(price, max(b[0], price * 0.8)) if not s else price
        dealer = DEALER_P.get(r, 0.0) if DEALER_BUYS.get(r, set()) & set(unlocked) else 0.0
        p = min(P_CAP, len(b) / (len(a) + len(b) + 1) + dealer)
        model[r] = {"price": round(price, 1), "p": round(p, 2), "asks": len(a), "bids": len(b), "obs": len(s)}
    return model


# ---------------------------------------------------------------- values

def copy_value(ref, n_held, idx, affinity, marginals):
    """What ONE more (n_held = copies after adding) / the n-th copy is worth to us: book x set affinity x copy marginal."""
    c = idx[ref]
    return c["book"] * affinity.get(c["set"], 1.0) * marginals[min(max(n_held, 1) - 1, len(marginals) - 1)]


def spare_gain(ref, n_held, idx, affinity, marginals, model, fee=0.0):
    """G of the copy we would give up (the n_held-th): expected trade gain if we try to sell it instead."""
    m = model[idx[ref]["rarity"]]
    v = copy_value(ref, n_held, idx, affinity, marginals)
    return m["p"] * max(0.0, m["price"] - fee - v)


def expected_output(rarity, counts, idx, affinity, marginals, model, weights=None):
    """E_out of crafting INTO `rarity`: mean expected trade gain of the card that comes out (uniform over released cards
    of that rarity unless `weights` (ref -> weight) says otherwise). Also returns the per-card breakdown."""
    cands = [ref for ref, c in idx.items() if c["rarity"] == rarity and c.get("released", True)]
    if not cands:
        return 0.0, []
    m = model[rarity]
    rows = []
    for ref in cands:
        n = counts.get(ref, 0)
        if n >= 1:                                                     # a duplicate for us: sellable
            v = copy_value(ref, n + 1, idx, affinity, marginals)
            g = m["p"] * max(0.0, m["price"] - v)
            why = "dup"
        elif affinity.get(idx[ref]["set"], 0) >= FOCUS_AFF:            # new in a set we build: kept, no trade
            v, g, why = copy_value(ref, 1, idx, affinity, marginals), 0.0, "keep"
        else:                                                          # new elsewhere: page_hunter sells singles
            v = copy_value(ref, 1, idx, affinity, marginals)
            floor = SINGLE_SELL[0] * v + SINGLE_SELL[1]
            g = m["p"] * max(0.0, m["price"] - floor) if m["price"] >= floor else 0.0
            why = "single"
        rows.append((ref, round(g, 2), why, round(v, 1)))
    w = weights or {}
    tot = sum(w.get(r, 1.0) for r, *_ in rows)
    e = sum(g * w.get(r, 1.0) for r, g, *_ in rows) / tot
    return e, rows


def eligible_spares(counts, ids, idx, affinity, marginals, model, blocked, bid_refs, rarity):
    """[(G, ref, asset_id)] spare copies of `rarity` we may put in, cheapest expected loss first. Never the last copy,
    never a blocked asset (listed, on a dealer table), never a card someone bids for right now (sell that one)."""
    out = []
    for ref, n in counts.items():
        if idx.get(ref, {}).get("rarity") != rarity or n < 2 or ref in bid_refs:
            continue
        spare = sorted(ids[ref], key=lambda a: -a.get("serial", 0))       # give the highest serials, keep the lowest
        unblocked = [a for a in spare if a["id"] not in blocked]
        # keep at least one UNBLOCKED copy: a listed copy may sell later, and then we would hold none
        free = unblocked[: max(0, len(unblocked) - 1)]
        for k, a in enumerate(free):
            g = spare_gain(ref, n - k, idx, affinity, marginals, model)
            out.append((round(g, 2), ref, a["id"]))
    return sorted(out)


def plan_craft(counts, ids, idx, affinity, marginals, model, blocked=(), bid_refs=(), weights=None):
    """Best craft now: (rarity, [asset ids], surplus, detail) or None. Tries each rarity, lowest first."""
    best = None
    for r, up in RARITY_UP.items():
        sp = eligible_spares(counts, ids, idx, affinity, marginals, model, set(blocked), set(bid_refs), r)
        if len(sp) < 3:
            continue
        e_out, rows = expected_output(up, counts, idx, affinity, marginals, model, weights)
        pick = sp[:3]
        surplus = e_out - sum(g for g, *_ in pick)
        if surplus >= CRAFT_MARGIN and (best is None or surplus > best[2]):
            best = (r, [a for _, _, a in pick], round(surplus, 2),
                    {"e_out": round(e_out, 2), "inputs": [(ref, g) for g, ref, _ in pick], "to": up})
    return best


def buy_cap(rarity, counts, ids, idx, affinity, marginals, model, ref, blocked=(), bid_refs=(), weights=None, fee=0.0):
    """Highest price worth paying for one more copy of `ref` (rarity `rarity`) as the THIRD Workshop input, or None.
    Requires two eligible spares already; the bought copy is itself a spare (we hold >= 1 of `ref`)."""
    if counts.get(ref, 0) < 1 or idx[ref]["rarity"] != rarity or rarity not in RARITY_UP:
        return None
    sp = eligible_spares(counts, ids, idx, affinity, marginals, model, set(blocked), set(bid_refs), rarity)
    if len(sp) < 2:
        return None
    e_out, _ = expected_output(RARITY_UP[rarity], counts, idx, affinity, marginals, model, weights)
    room = e_out - sum(g for g, *_ in sp[:2]) - CRAFT_MARGIN
    v_new = copy_value(ref, counts[ref] + 1, idx, affinity, marginals)     # what the bought copy is worth to us
    cap = min(v_new, v_new + room) - fee     # never above our value: that loss counts in full and the craft scores nothing
    return math.floor(cap) if cap >= 1 else None


def fees_by_venue(b):
    try:
        return {v["venue"]: v for v in b.venues().get("venues", [])}
    except Exception:
        return {}


def plan_buy(boards, me, counts, ids, idx, marg, model, blocked, bid_refs, weights, venues):
    """Cheapest board ask for a card we already hold whose price + taker fee stays under buy_cap(). None if none."""
    best = None
    for vid, offers in boards.items():
        v = venues.get(vid) or ({"fee_bps": 500, "fee_per_card": 1} if vid == "rastro" else {"fee_bps": 0, "fee_per_card": 0})
        for o in offers:
            g, w = o.get("give") or {}, o.get("want") or {}
            ga = g.get("assets") or []
            if o.get("maker") == me["id"] or o.get("to") not in (None, me["id"]) or len(ga) != 1 or g.get("cash") \
                    or not w.get("cash") or w.get("types") or o.get("status", "open") != "open":
                continue
            ref = ga[0].get("ref")
            if ref not in idx or counts.get(ref, 0) < 1:
                continue
            price = w["cash"]
            fee = math.ceil((v.get("fee_bps") or 0) * price / 10000) + (v.get("fee_per_card") or 0)
            cap = buy_cap(idx[ref]["rarity"], counts, ids, idx, me["affinity"], marg, model, ref, blocked, bid_refs, weights, fee)
            if cap is not None and price <= cap and price + fee <= me["cash"]:
                if best is None or price + fee < best["price"] + best["fee"]:
                    best = {"offer": o["id"], "ref": ref, "price": price, "fee": fee, "venue": vid, "cap": cap,
                            "rarity": idx[ref]["rarity"]}
    return best


def learn_output(mem, idx, events):
    """Count crafted outputs (ours and the feed's) per ref, to replace the uniform prior when enough are seen."""
    w = mem.setdefault("ws_out", {})
    seen = set(mem.setdefault("ws_craft_seen", []))
    byname = {c["name"]: ref for ref, c in idx.items()}
    for e in events or []:
        if e.get("type") != "taller.crafted" or e.get("id") in seen:
            continue
        seen.add(e.get("id"))
        ref = byname.get((e.get("payload") or {}).get("card"))
        if ref:
            w[ref] = w.get(ref, 0) + 1
    mem["ws_craft_seen"] = sorted(seen)[-2000:]


def weights_from(mem, idx, rarity, min_seen=30):
    """Learned output weights for `rarity` (Laplace-smoothed) once min_seen crafts are known, else None (uniform)."""
    w = {r: n for r, n in mem.get("ws_out", {}).items() if idx.get(r, {}).get("rarity") == rarity}
    if sum(w.values()) < min_seen:
        return None
    return {r: w.get(r, 0) + 1 for r, c in idx.items() if c["rarity"] == rarity and c.get("released", True)}


# ---------------------------------------------------------------- live step (called from smart_agent)

def gather(b, me, idx, mem, boards_to_read=("rastro", "v07", "v02", "v21")):
    boards = {}
    for vid in boards_to_read:
        try:
            boards[vid] = b.board(vid).get("offers", [])
        except Exception:
            continue
    try:
        events = b.call("GET", "/api/feed?limit=500").get("events", [])
    except Exception:
        events = []
    observe_settlements(mem, events, idx)
    learn_output(mem, idx, events)
    return boards


def step(b, me, catalog, mem, log, dry=False, blocked_extra=(), allow_buy=True):
    """One Workshop decision. Returns a dict describing what happened (or would happen when dry)."""
    if not dry and me["tick"] - mem.get("ws_tick", -999) < EVERY:
        return None
    mem["ws_tick"] = me["tick"]
    idx = card_index(catalog)
    marg = catalog["values"]["copy_marginals"]
    counts, ids = {}, {}
    for a in me["assets"]:
        if a.get("kind") == "card":
            counts[a["ref"]] = counts.get(a["ref"], 0) + 1
            ids.setdefault(a["ref"], []).append(a)
    boards = gather(b, me, idx, mem)
    model = market_model(boards, idx, mem, me["id"], me.get("unlocked", ()))
    mine = b.my_offers().get("offers", [])
    listed = {a["id"]: o["id"] for o in mine if o.get("maker") == me["id"] and o.get("status", "open") == "open"
              for a in (o.get("give") or {}).get("assets") or [] if isinstance(a, dict)}
    bid_refs = set()                                                  # a team pays for this card now: sell it, do not craft it
    for offers in boards.values():
        for o in offers:
            g, w = o.get("give") or {}, o.get("want") or {}
            if o.get("maker") != me["id"] and g.get("cash") and not g.get("assets"):
                for t in w.get("types") or []:
                    ref = t.split(":", 1)[-1]
                    if ref in counts and ref in idx:
                        v = copy_value(ref, counts[ref], idx, me["affinity"], marg)
                        if g["cash"] >= v + 2:
                            bid_refs.add(ref)
    blocked = set(blocked_extra)
    # Output weights: learned from crafts seen (ours + feed) once there are enough, else uniform.
    weights = {r: weights_from(mem, idx, r) for r in set(RARITY_UP.values())}
    learned = next((w for w in weights.values() if w), None)
    # Unlisted spares first; listed ones only if needed (their listing is cancelled before the craft).
    plan = plan_craft(counts, ids, idx, me["affinity"], marg, model, blocked | set(listed), bid_refs, learned)
    if plan is None:
        plan = plan_craft(counts, ids, idx, me["affinity"], marg, model, blocked, bid_refs, learned)
    info = {"model": model, "plan": plan, "bid_refs": sorted(bid_refs)}
    if plan is None and not allow_buy:
        return info
    if plan is None:
        buy = plan_buy(boards, me, counts, ids, idx, marg, model, blocked | set(listed), bid_refs, learned, fees_by_venue(b))
        info["buy"] = buy
        if buy is None:
            log(f"workshop: no craft clears the margin {CRAFT_MARGIN} | market "
                f"{json.dumps({r: (m['price'], m['p']) for r, m in model.items() if m['asks'] or m['bids'] or m['obs']})}")
            return info
        log(f"workshop: buy {buy['ref']} at {buy['price']} (+fee {buy['fee']}) on {buy['venue']} as 3rd {buy['rarity']} input; "
            f"cap {buy['cap']}" + (" (dry run)" if dry else ""))
        if dry:
            return info
        try:
            from bz.core.accept_gate import try_reserve
            if not try_reserve(me["tick"]):
                log("workshop: buy skipped: another process already used this tick's accept")
                return info
        except ImportError:
            pass
        try:
            b.accept(buy["offer"])
            mem.setdefault("ws_buys", []).append(dict(buy, tick=me["tick"]))
            log(f"WORKSHOP BUY {buy['ref']} at {buy['price']}: settles next tick, craft follows")
        except Exception as e:
            log(f"workshop: buy refused ({getattr(e, 'code', type(e).__name__)})")
        return info
    rarity, assets, surplus, detail = plan
    log(f"workshop: craft {rarity} -> {detail['to']} with {detail['inputs']} | E_out {detail['e_out']} surplus {surplus}"
        + (" (dry run)" if dry else ""))
    if dry:
        return info
    for aid in assets:                                              # free any listed input first
        if aid in listed:
            try:
                b.cancel(listed[aid])
                log(f"workshop: cancelled listing {listed[aid]} of asset {aid}")
            except Exception as e:
                log(f"workshop: cannot cancel listing {listed[aid]} ({e}); no craft this tick")
                return info
    try:
        res = b.call("POST", "/api/taller", {"assets": assets})
    except Exception as e:
        log(f"workshop: craft refused ({getattr(e, 'code', type(e).__name__)}: {getattr(e, 'message', e)})")
        info["error"] = str(e)
        return info
    out = res.get("card") or res.get("asset") or res
    ref = out.get("ref") if isinstance(out, dict) else None
    mem.setdefault("ws_crafts", []).append({"tick": me["tick"], "from": rarity, "inputs": detail["inputs"], "out": ref,
                                            "e_out": detail["e_out"], "raw": json.dumps(res)[:400]})
    if ref:
        mem.setdefault("ws_out", {})[ref] = mem["ws_out"].get(ref, 0) + 1
    log(f"WORKSHOP CRAFT {rarity} x3 -> {ref or res}")
    info["result"] = res
    return info


def selftest():
    cat = {"sets": [{"id": "LAV", "released": True, "cards": [
        {"id": "LAV-01", "book": 10, "rarity": "common", "name": "a"}, {"id": "LAV-02", "book": 10, "rarity": "common", "name": "b"},
        {"id": "LAV-06", "book": 25, "rarity": "uncommon", "name": "c"}, {"id": "LAV-07", "book": 25, "rarity": "uncommon", "name": "d"}]},
        {"id": "MAL", "released": True, "cards": [
        {"id": "MAL-01", "book": 10, "rarity": "common", "name": "e"}, {"id": "MAL-06", "book": 25, "rarity": "uncommon", "name": "f"}]}],
        "values": {"copy_marginals": [1.0, 0.25, 0.1]}}
    idx, marg = card_index(cat), cat["values"]["copy_marginals"]
    aff = {"LAV": 1.6, "MAL": 0.5}
    model = {"common": {"price": 3.0, "p": 0.15, "asks": 60, "bids": 10, "obs": 0},
             "uncommon": {"price": 22.0, "p": 0.8, "asks": 10, "bids": 5, "obs": 5},
             "rare": {"price": 65.0, "p": 0.8, "asks": 1, "bids": 3, "obs": 2},
             "epic": {"price": 0.0, "p": 0.0, "asks": 0, "bids": 0, "obs": 0}, "legendary": {"price": 0.0, "p": 0.0, "asks": 0, "bids": 0, "obs": 0}}
    A = lambda i, ref, s: {"id": i, "kind": "card", "ref": ref, "serial": s}
    assets = [A(1, "LAV-01", 1), A(2, "LAV-01", 2), A(3, "MAL-01", 1), A(4, "MAL-01", 2), A(5, "MAL-01", 3), A(6, "LAV-06", 1)]
    counts, ids = {}, {}
    for a in assets:
        counts[a["ref"]] = counts.get(a["ref"], 0) + 1
        ids.setdefault(a["ref"], []).append(a)
    sp = eligible_spares(counts, ids, idx, aff, marg, model, set(), set(), "common")
    assert sorted(a for _, _, a in sp) == [2, 4, 5], sp                       # never the lowest serial / last copy
    plan = plan_craft(counts, ids, idx, aff, marg, model)
    assert plan and plan[0] == "common" and sorted(plan[1]) == [2, 4, 5], plan
    assert plan_craft(counts, ids, idx, aff, marg, model, blocked={4}) is None, "only two free spares: no craft"
    assert plan_craft(counts, ids, idx, aff, marg, model, bid_refs={"MAL-01"}) is None, "a bid pays for it: sell instead"
    dead = dict(model, uncommon={"price": 5.0, "p": 0.1, "asks": 30, "bids": 1, "obs": 0})
    assert plan_craft(counts, ids, idx, aff, marg, dead) is None, "outputs nobody buys: do not craft"
    e, rows = expected_output("uncommon", counts, idx, aff, marg, model)
    assert {r: why for r, _, why, _ in rows} == {"LAV-06": "dup", "LAV-07": "keep", "MAL-06": "single"}, rows
    cap = buy_cap("common", counts, ids, idx, aff, marg, model, "LAV-01", blocked={5})
    assert cap is not None and cap >= 1, cap
    assert buy_cap("common", counts, ids, idx, aff, marg, dead, "LAV-01", blocked={5}) is None, "no room: never buy"
    me = {"id": "t06", "cash": 50, "affinity": aff}
    two = {"LAV-01": 2, "MAL-01": 2, "LAV-06": 1}
    two_ids = {"LAV-01": [A(1, "LAV-01", 1), A(2, "LAV-01", 2)], "MAL-01": [A(3, "MAL-01", 1), A(4, "MAL-01", 2)],
               "LAV-06": [A(6, "LAV-06", 1)]}
    ask = lambda oid, ref, p: {"id": oid, "maker": "m1", "status": "open", "give": {"cash": 0, "assets": [{"ref": ref, "id": 90 + oid}]},
                               "want": {"cash": p, "types": []}}
    cheap = plan_buy({"v02": [ask(1, "LAV-01", 2), ask(2, "MAL-01", 40)]}, me, two, two_ids, idx, marg, model, set(), set(), None,
                     {"v02": {"fee_bps": 0, "fee_per_card": 0}})
    assert cheap is None or cheap["price"] <= 1, cheap          # a 3rd copy is worth ~1 to us: never pay over our value to craft
    assert plan_buy({"v02": [ask(2, "MAL-01", 40)]}, me, two, two_ids, idx, marg, model, set(), set(), None,
                    {"v02": {"fee_bps": 0, "fee_per_card": 0}}) is None, "too expensive: no buy"
    assert plan_buy({"v02": [ask(3, "LAV-07", 1)]}, me, two, two_ids, idx, marg, model, set(), set(), None, {}) is None, \
        "a card we do not hold is not a spare: no buy-to-craft"
    board = {"rastro": [{"maker": "m1", "status": "open", "give": {"cash": 0, "assets": [{"ref": "LAV-01", "rarity": "common"}]},
                         "want": {"cash": 10, "types": []}}]}
    p_before = market_model(board, idx, {}, "t06", ("chato", "pilar"))["common"]["p"]
    p_after = market_model(board, idx, {}, "t06", ("chato", "pilar", "picaros"))["common"]["p"]
    assert p_before == 0.0 and p_after == DEALER_P["common"], (p_before, p_after)
    mem = {}
    observe_settlements(mem, [{"id": 1, "type": "settlement", "payload": {"price": 20, "items": [{"kind": "card", "ref": "LAV-06", "rarity": "uncommon"}]}},
                              {"id": 1, "type": "settlement", "payload": {"price": 20, "items": [{"kind": "card", "ref": "LAV-06", "rarity": "uncommon"}]}}], idx)
    assert mem["ws_obs"] == {"uncommon": [20.0]}, "each settlement counted once"
    print("workshop selftest OK")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    elif "--dry-run" in sys.argv:
        import os
        from bazaar_sdk import Bazaar
        b = Bazaar(os.environ.get("BAZAAR_URL", "https://bazaar.causaprima.ai"), os.environ["BAZAAR_KEY"], wait_on_tick=False)
        me, catalog, mem = b.me(), b.catalog(), {}
        info = step(b, me, catalog, mem, print, dry=True)
        idx = card_index(catalog)
        counts = {}
        for a in me["assets"]:
            if a.get("kind") == "card":
                counts[a["ref"]] = counts.get(a["ref"], 0) + 1
        print("market model:", json.dumps(info["model"]))
        e, rows = expected_output("uncommon", counts, idx, me["affinity"], catalog["values"]["copy_marginals"], info["model"])
        print(f"E_out common->uncommon = {e:.2f}; outputs:", rows)
        print("cards a team bids for now (sold, not crafted):", info["bid_refs"])
