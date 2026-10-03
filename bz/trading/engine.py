"""Trading V2: ONE decision per tick for all peer-to-peer trading (buy / sell to bids / swap / list / bid / arbitrage).

    plan = decide(snap, cfg, intel, rng)      # pure: no network, no clock reads (pass `now`), deterministic given rng
    execute(b, plan, ctx, cfg)                # the only part that writes; every accept goes through the shared accept gate

It replaces smart_agent.phase_market + page_hunter.step when TRADING_V2 is `on` (see bz/trading/mode.py), reusing what was
already tested: strict offer-shape parsing and the value model (agents/trader_agent.py, agents/portfolio_agent.py).

What it does NOT do, on purpose:
  * It never sells our last copy of a card (except an arbitrage lot it bought to resell), and never accepts a trade whose
    gain is below the dynamic margin (policy.min_margin) at OUR private values.
  * It cannot address a specific rival (board makers are pseudonyms, `to=` needs a team id): "targeted selling" means pricing
    against the bids we have SEEN and accepting those bids when they pay.
  * At most one accept per tick, only from boards read in this very tick, never from our own venue, and at most
    `max_deals_per_maker` deals with one counterparty per window (fair play).
"""
import math
import time

from agents import portfolio_agent as pf
from agents.trader_agent import classify
from bazaar_sdk import BazaarError
from bz.core.accept_gate import try_reserve
from bz.trading import policy


class V2Valuer(pf.Valuer):
    """Adds `base(ref)`: value of one more copy, and `page_credit(...)`: the page credit the policy may add on top.
    Server value (b.value) when available, within the call cap; the model otherwise. Measured on the live server
    (2026-10-03): for the LAST missing card of a page b.value() already includes the whole page bonus (LAV-09 = 218 =
    112 + bonus); with 2+ cards missing it includes none (RET-09 = 63 = book x affinity). So a server value of a page
    closer gets no extra credit; a model value, or a page with 2+ missing, keeps the policy's credit."""

    def __init__(self, b, me, catalog, max_calls=12, shared=None, ttl=20):
        super().__init__(b, me, catalog, max_calls)
        self.base_cache = {}
        self.server = {}                                                     # ref -> True if base() came from b.value()
        self.shared, self.ttl, self.tick = shared, ttl, me.get("tick", 0)    # shared: runner-level cache, saves requests

    def base(self, ref):
        if ref in self.base_cache:
            return self.base_cache[ref]
        held = self.counts.get(ref, 0)
        hit = (self.shared or {}).get(ref)
        if hit and hit[1] == held and 0 <= self.tick - hit[0] < self.ttl:
            self.base_cache[ref] = hit[2]                                    # same holdings, fresh enough: no request
            self.server[ref] = True                                          # the shared cache only keeps server values
            return hit[2]
        v, from_server = None, False
        if self.b is not None and self.calls < self.max_calls:
            self.calls += 1
            try:
                v = self.b.value(ref).get("your_value")
                from_server = v is not None
            except Exception:
                v = None
        if v is None:
            marg = pf.marginals(self.catalog)
            v = pf.base_value(ref, self.catalog, self.aff, self.idx) * marg[min(held, len(marg) - 1)]
        self.base_cache[ref] = float(v)
        self.server[ref] = from_server
        if from_server and self.shared is not None:
            self.shared[ref] = (self.tick, held, float(v))
        return self.base_cache[ref]


    def page_credit(self, ref, n_missing, bonus, cfg):
        """Policy page credit, except for a page closer valued by the server (its value already holds the bonus)."""
        self.base(ref)
        if self.server.get(ref) and n_missing == 1:
            return 0.0
        return policy.page_credit(n_missing, bonus, cfg)


def _page_info(valuer, ref):
    """(is_page_card, n_missing_in_its_page, page bonus) for a card we do not hold; (False, None, 0) otherwise."""
    c = valuer.idx.get(ref)
    if not c or not pf.is_page_card(c) or valuer.counts.get(ref, 0) > 0:
        return False, None, 0.0
    st = pf.page_state(c["set"], valuer.catalog, valuer.counts, valuer.aff, valuer.idx)
    return True, len(st["missing"]), st["bonus"]


def _fees(snap, vid):
    return snap["venues"].get(vid) or {"fee_bps": 500, "fee_per_card": 1}


def decide(snap, cfg, intel, rng):
    me, catalog, valuer = snap["me"], snap["catalog"], snap["valuer"]
    now = snap.get("now")
    press = policy.pressure(snap.get("clock"), now)
    counts, ids = pf.holdings(me)
    my_open = [o for o in snap["my_offers"] if o.get("maker") == me["id"] and o.get("status", "open") == "open"]
    listed = {a["id"] for o in my_open for a in (o.get("give") or {}).get("assets") or [] if isinstance(a, dict)}
    committed = sum(int((o.get("give") or {}).get("cash") or 0) for o in my_open)
    spendable = max(0, me["cash"] - cfg["reserve"] - committed)
    free_ratio = spendable / max(1, me["cash"])
    arb = intel.extra.setdefault("arb", {})                 # ref -> cost basis of lots we bought to resell
    plan = {"tick": me["tick"], "pressure": round(press, 2), "spendable": spendable, "accept": None, "candidates": 0,
            "list": [], "cancel": [], "bids": [], "skipped": []}
    rastro = _fees(snap, "rastro")

    # ---------------------------------------------------------------- accept candidates
    opps = []
    t_now = time.time() if now is None else now.timestamp()
    for vid, offers in snap["boards"].items():
        v = _fees(snap, vid)
        for o in offers:
            if o.get("status", "open") != "open" or o.get("maker") == me["id"] or o.get("thread") is not None:
                continue
            if o.get("to") not in (None, me["id"]):
                continue
            maker = o.get("maker")
            if sum(1 for ts, m in snap.get("deals", []) if m == maker and t_now - ts < cfg["deal_window"]) >= cfg["max_deals_per_maker"]:
                continue
            kind, g, w = classify(o)
            if kind is None:
                continue
            opp = None
            if kind == "buy":
                opp = _eval_buy(o, vid, v, g, w, valuer, intel, spendable, free_ratio, press, cfg, counts, arb, snap)
            elif kind == "sell":
                opp = _eval_sell(o, vid, v, g, w, valuer, intel, press, cfg, counts, ids, listed, arb)
            elif kind == "swap":
                opp = _eval_swap(o, vid, v, g, w, valuer, spendable, press, cfg, counts, ids, listed)
            if opp:
                opps.append(opp)
    plan["candidates"] = len(opps)
    if opps:
        plan["accept"] = policy.rank(opps, spendable, press, cfg)[0]

    # ---------------------------------------------------------------- listings, cancels, bids
    room = max(0, cfg["max_open"] - len(my_open))
    budget = cfg["list_per_tick"]
    taken = set(plan["accept"]["assets"] or []) if plan["accept"] else set()
    for ref, ask, asset, floor, why in _listings(me, catalog, valuer, intel, cfg, rng, press, free_ratio, counts, ids,
                                                 listed | taken, arb, plan, rastro):
        if room <= 0 or budget <= 0:
            break
        plan["list"].append({"asset": asset, "ref": ref, "ask": ask, "floor": floor, "why": why})
        room -= 1
        budget -= 1
    cancels, bids = _bids(me, catalog, valuer, intel, cfg, press, free_ratio, counts, my_open, spendable, rastro, plan,
                          plan["accept"])
    for oid in cancels[:1]:                                  # at most one re-price per tick (a cancel counts as a listing)
        if budget > 0:
            plan["cancel"].append(oid)
            budget -= 1
            room += 1
    for b in bids:
        if room <= 0 or budget <= 0:
            break
        plan["bids"].append(b)
        room -= 1
        budget -= 1
    return plan


def _eval_buy(o, vid, v, g, w, valuer, intel, spendable, free_ratio, press, cfg, counts, arb, snap):
    if len(g["assets"]) != 1:
        return None                                          # bundles are not valued here
    ref, price = g["assets"][0]["ref"], w["cash"]
    if ref not in valuer.idx or price < 1:
        return None
    cost = price + policy.fee(v, price)
    if cost > spendable:
        return None
    v_base = valuer.base(ref)
    is_page, n_missing, bonus = _page_info(valuer, ref)
    credit = valuer.page_credit(ref, n_missing, bonus, cfg) if is_page else 0.0
    v_eff = v_base + credit
    gain = v_eff - cost
    margin = policy.min_margin(intel.liquidity(ref), press, free_ratio, cfg)
    if is_page and n_missing == 1:
        need, cat = cfg["closer_min"], "page_closer"
    else:
        need, cat = max(margin, policy.roi_floor(press, cfg) * cost), ("page_hope" if credit > 0 else "useful")
    if gain >= need:
        return {"kind": "buy", "category": cat, "offer": o["id"], "venue": vid, "maker": o.get("maker"), "ref": ref,
                "price": price, "cost": cost, "value": round(v_eff, 1), "v_base": round(v_base, 1), "credit": round(credit, 1),
                "gain": round(gain, 1), "need": round(need, 1), "assets": None}
    if cfg["arb"] and len(arb) < cfg["arb_max_inventory"] and cost <= cfg["arb_cash_share"] * spendable:
        bid = next(((p, m) for p, m in intel.bids(ref) if m != o.get("maker")), None)
        if bid:
            resale = bid[0] - policy.fee(snap["venues"].get("rastro") or v, bid[0]) - 1
            if resale - cost >= cfg["arb_min"]:
                return {"kind": "arb", "category": "arbitrage", "offer": o["id"], "venue": vid, "maker": o.get("maker"),
                        "ref": ref, "price": price, "cost": cost, "value": resale, "gain": round(resale - cost, 1),
                        "need": cfg["arb_min"], "assets": None, "resale_bid": bid[0]}
    return None


def _spare_asset(ids, ref, listed):
    for a in sorted(ids.get(ref, []), key=lambda a: -(a.get("serial") or 0)):
        if not a.get("locked") and a["id"] not in listed:
            return a
    return None


def _eval_sell(o, vid, v, g, w, valuer, intel, press, cfg, counts, ids, listed, arb):
    ref, bid = w["types"][0], g["cash"]
    n = counts.get(ref, 0)
    net = bid - policy.fee(v, bid)
    spare = _spare_asset(ids, ref, listed)
    if not spare or ref not in valuer.idx:
        return None
    if n >= 2:
        loss = valuer.loss(ref, n)
        gain = net - loss
        margin = min(policy.min_margin(intel.liquidity(ref), press, 1.0, cfg), 2)
        if gain >= max(margin, policy.roi_floor(press, cfg) * loss):
            return {"kind": "sell", "category": "spare copy", "offer": o["id"], "venue": vid, "maker": o.get("maker"), "ref": ref,
                    "price": bid, "cost": 0, "value": round(loss, 1), "gain": round(gain, 1), "need": margin, "assets": [spare["id"]]}
    if ref in arb and n >= 1:                                # a lot we bought to resell: profit is measured against its cost
        gain = net - arb[ref]
        if gain >= 1:
            return {"kind": "arb", "category": "arbitrage resale", "offer": o["id"], "venue": vid, "maker": o.get("maker"), "ref": ref,
                    "price": bid, "cost": 0, "value": net, "gain": round(gain, 1), "need": 1, "assets": [spare["id"]], "resale": True}
    return None


def _eval_swap(o, vid, v, g, w, valuer, spendable, press, cfg, counts, ids, listed):
    their, ours = g["assets"][0]["ref"], w["types"][0]
    if their not in valuer.idx or ours not in valuer.idx or their == ours or counts.get(ours, 0) < 1:
        return None
    spare = _spare_asset(ids, ours, listed)
    fees = 2 * v.get("fee_per_card", 1)
    if not spare or fees > spendable:
        return None
    is_page, n_missing, bonus = _page_info(valuer, their)
    v_in = valuer.base(their) + (valuer.page_credit(their, n_missing, bonus, cfg) if is_page else 0.0)
    loss = valuer.loss(ours, counts[ours])                   # marginal: a spare copy costs us little, our last copy costs its full value
    gain = v_in - loss - fees
    late = press >= cfg["late_pressure"]
    need = cfg["swap_min_late"] if late else cfg["swap_min"]
    if gain >= need and (gain > 0 or (late and v_in > 0)):          # near the close a break-even swap is fine
        return {"kind": "swap", "category": "swap", "offer": o["id"], "venue": vid, "maker": o.get("maker"), "ref": their,
                "price": 0, "cost": fees, "value": round(v_in, 1), "gain": round(gain, 1), "need": need, "assets": [spare["id"]],
                "gives": ours}
    return None


# ---------------------------------------------------------------- our listings

def _listings(me, catalog, valuer, intel, cfg, rng, press, free_ratio, counts, ids, listed, arb, plan, rastro):
    out, seen_assets = [], set()
    dups = pf.sellable_duplicates(me, catalog, listed)
    for d in sorted(dups, key=lambda d: -d["book"]):
        ref, asset, book, loss = d["ref"], d["asset"], d["book"], d["loss"]
        margin = min(policy.min_margin(intel.liquidity(ref), press, free_ratio, cfg), 2)
        floor = int(math.ceil(loss)) + margin
        bids = [p for p, _ in intel.bids(ref)]
        if bids and max(bids) - policy.fee(rastro, max(bids)) >= floor:
            plan["skipped"].append(f"{ref}: a bid of {max(bids)} already pays our floor {floor}; accept it instead of listing")
            continue
        if rng.random() < cfg["skip_list_p"]:
            plan["skipped"].append(f"{ref}: listing delayed on purpose (information hiding)")
            continue
        ask, why = policy.sell_ask(floor, book, [p for p, _ in intel.asks(ref)], intel.buyer_estimate(ref), rng, cfg)
        if ask is None:
            plan["skipped"].append(f"{ref}: {why}")
            continue
        out.append((ref, ask, asset, floor, why))
        seen_assets.add(asset)
    for ref, basis in list(arb.items()):                       # lots bought to resell and not yet matched by a bid
        asset = _spare_asset(ids, ref, listed | seen_assets)
        if not asset or counts.get(ref, 0) < 1:
            continue
        floor = int(math.ceil(basis)) + 1
        ask, why = policy.sell_ask(floor, valuer.idx[ref]["book"], [p for p, _ in intel.asks(ref)], intel.buyer_estimate(ref), rng, cfg)
        if ask is not None:
            out.append((ref, ask, asset["id"], floor, "arbitrage lot: " + why))
    return out


# ---------------------------------------------------------------- our bids

def _bids(me, catalog, valuer, intel, cfg, press, free_ratio, counts, my_open, spendable, rastro, plan, accept):
    cancels, out = [], []
    mine = {}
    for o in my_open:
        g, w = o.get("give") or {}, o.get("want") or {}
        if g.get("cash") and not g.get("assets") and len(w.get("types") or []) == 1:
            mine[w["types"][0].split(":", 1)[-1]] = o
    budget_cap = cfg["bid_budget_share"] * me["cash"]
    committed = sum(int(o["give"]["cash"]) for o in mine.values())
    bought = accept["ref"] if accept and accept["kind"] in ("buy", "arb") else None
    for t in pf.rank_targets(me, catalog)[:cfg["max_bids"] + 4]:
        ref = t["ref"]
        if ref == bought:
            continue
        is_page, n_missing, bonus = _page_info(valuer, ref)
        is_last = bool(is_page and n_missing == 1)
        v_eff = valuer.base(ref) + (valuer.page_credit(ref, n_missing, bonus, cfg) if is_page else 0.0)
        margin = cfg["closer_min"] if is_last else policy.min_margin(intel.liquidity(ref), press, free_ratio, cfg)
        p_max = policy.max_price(v_eff, rastro, margin)
        asks = [p for p, _ in intel.asks(ref)]
        if asks and min(asks) + policy.fee(rastro, min(asks)) <= p_max + policy.fee(rastro, p_max):
            continue                                         # a cheap enough ask exists: the accept path takes it
        others = [p for p, _ in intel.bids(ref)]
        price, tier = policy.bid_price(p_max, others, is_last, cfg)
        if price is None:
            plan["skipped"].append(f"bid {ref}: {tier}")
            continue
        existing = mine.get(ref)
        if existing:
            old = int(existing["give"]["cash"])
            if price >= old + 2 and others and max(others) >= old:
                cancels.append(existing["id"])               # outbid: re-price
                committed -= old
            else:
                continue
        if len(mine) - len(cancels) + len(out) >= cfg["max_bids"] and not existing:
            continue
        if committed + price > budget_cap or price + policy.fee(rastro, price) > spendable:
            plan["skipped"].append(f"bid {ref}: bid budget used up")
            continue
        committed += price
        out.append({"ref": ref, "price": price, "p_max": p_max, "tier": tier, "value": round(v_eff, 1), "last_card": is_last})
    return cancels, out


# ---------------------------------------------------------------- execution

def execute(b, plan, ctx, cfg, log=print):
    """Send the plan. Returns {'accepted': opp|None, 'listed': n, 'bids': n, 'cancelled': n, 'blocked': reason|None}."""
    out = {"accepted": None, "listed": 0, "bids": 0, "cancelled": 0, "blocked": None}
    acc = plan["accept"]
    if acc:
        if not try_reserve(plan["tick"]):
            out["blocked"] = "another process already used this tick's accept"
            log(f"v2: accept skipped: {out['blocked']}")
        else:
            try:
                b.accept(acc["offer"], assets=acc["assets"])
                out["accepted"] = acc
                ctx.setdefault("deals", []).append((time.time(), acc["maker"]))
                ctx["deals"] = ctx["deals"][-200:]
                arb = ctx["intel"].extra.setdefault("arb", {})
                if acc["kind"] == "arb" and not acc.get("resale"):
                    arb[acc["ref"]] = acc["cost"]
                elif acc["kind"] == "arb" and acc.get("resale"):
                    arb.pop(acc["ref"], None)
                log(f"v2: ACCEPT {acc['kind']}/{acc['category']} {acc['ref']} at {acc['price']} (gain {acc['gain']}, need {acc['need']})")
            except BazaarError as e:
                out["blocked"] = e.code
                log("v2: accept refused:", e.code, e.message)
                if e.code == "self_venue":
                    ctx.setdefault("skip_venues", set()).add(acc["venue"])
    for oid in plan["cancel"]:
        try:
            b.cancel(oid)
            out["cancelled"] += 1
            log(f"v2: cancelled outbid bid {oid}")
        except BazaarError as e:
            log("v2: cancel refused:", e.code)
    for l in plan["list"]:
        try:
            b.list_offer({"assets": [l["asset"]]}, {"cash": l["ask"]}, venue="rastro")
            out["listed"] += 1
            log(f"v2: LIST {l['ref']} asset {l['asset']} at {l['ask']} (floor {l['floor']}): {l['why']}")
        except BazaarError as e:
            log("v2: listing refused:", e.code)
            break
    for bd in plan["bids"]:
        try:
            b.list_offer({"cash": bd["price"]}, {"cards": [bd["ref"]]}, venue="rastro")
            out["bids"] += 1
            log(f"v2: BID {bd['price']} for {bd['ref']} (max {bd['p_max']}, {bd['tier']})")
        except BazaarError as e:
            log("v2: bid refused:", e.code)
            break
    return out
