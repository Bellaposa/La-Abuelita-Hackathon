"""Team agent: Abuela negotiation + market trading. Deterministic, no LLM, no API key beyond the team key.

    BAZAAR_KEY=tk-... python3 smart_agent.py        # runs forever, one pass per tick
    python3 smart_agent.py --selftest               # offline checks, no network

Phase 1  market   buy cards that are worth more to us than they cost (value - price - fee), sell surplus copies for
                  more than what the copy is worth to us (never `book * k`: floors come from OUR value of the copy).
Phase 2  Abuela   adaptive haggling. Every round we ask: how did she answer our last concession?
                  - her move per our move (response ratio) sizes the next step;
                  - she stopped moving -> we stop paying for nothing;  `final` -> take it under our cap or walk.
                  Prices and limits are plain code; the tone read of her messages is a keyword/number classifier.
Memory   memory.json keeps OBSERVED facts (every round, every outcome) apart from INFERENCES (computed from them,
         each with its sample size, and only when the sample is large enough).
"""
import json
import math
import os
import statistics
import sys
import time

from bazaar_sdk import Bazaar, BazaarError

URL = os.environ.get("BAZAAR_URL", "https://bazaar.causaprima.ai")
MEMORY_FILE = os.environ.get("AGENT_MEMORY", "memory.json")
CASH_RESERVE = int(os.environ.get("CASH_RESERVE", "0"))  # primas never spent (e.g. 270 for a venue bond)
DEFAULT_K = 0.25          # share of the gap we close per round until we have data
MIN_SAMPLES = 5           # observations per step-size bucket before we trust a response ratio
MIN_DEALS = 3             # finished deals before we adapt the opening bid
BUY_MIN_GAIN = 3          # primas of private-value gain we need to buy from another team
SELL_MIN_GAIN = 2         # primas over what the copy is worth to us to sell it
LISTINGS_PER_TICK = 3
MAX_VALUE_CHECKS = 12     # b.value() calls per tick (rate limit friendly)


# ---------------------------------------------------------------- memory: observed vs inferred

def load_memory():
    try:
        with open(MEMORY_FILE) as f:
            mem = json.load(f)
    except (OSError, ValueError):
        mem = {}
    mem.setdefault("abuela_min_price", 999)           # legacy key, kept
    mem.setdefault("chat_history", [])                # legacy key, kept
    mem.setdefault("observed", {"negotiations": []})  # facts: rounds and outcomes as seen
    mem.setdefault("inferred", {})                    # conclusions, each with n
    mem.setdefault("active", None)                    # the negotiation in progress
    return mem


def save_memory(mem):
    mem["chat_history"] = mem["chat_history"][-20:]
    mem["observed"]["negotiations"] = mem["observed"]["negotiations"][-100:]
    tmp = MEMORY_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(mem, f, indent=1, ensure_ascii=False)
    os.replace(tmp, MEMORY_FILE)


def log_chat(msg: str):
    with open("historial_abuela.txt", "a", encoding="utf-8") as f:
        f.write(msg + "\n")


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def bucket(step, gap):
    r = step / gap if gap > 0 else 0
    return "small" if r < 0.15 else "mid" if r < 0.35 else "large"


def infer(mem, dealer="abuela"):
    """Conclusions from observed rounds of ONE dealer. A bucket/number is only reported with n >= its minimum; otherwise absent."""
    resp = {"small": [], "mid": [], "large": []}
    paid = []
    for n in mem["observed"]["negotiations"]:
        if n.get("dealer", "abuela") != dealer:
            continue
        # A sale concedes by lowering the ask, so the stored move and gap are negative, and the
        # cash that changed hands is `received` rather than `paid`. Measure both toward the other side.
        sale = "received" in n
        for r in n.get("rounds", []):
            our_move = r.get("our_move") or 0
            her_move = r.get("her_move")
            gap = r.get("gap_before")
            if sale and our_move < 0 and her_move is not None and gap and gap < 0:
                our_move, her_move, gap = -our_move, -her_move, -gap
            if our_move > 0 and her_move is not None and gap:
                resp[bucket(our_move, gap)].append(max(0.0, her_move) / our_move)
        amount = n.get("paid") or n.get("received")
        if n.get("outcome") == "deal" and amount:
            paid.append(amount)
    inf = {"response_ratio": {}, "best_k": None, "paid_median": None, "paid_n": len(paid)}
    for name, xs in resp.items():
        if len(xs) >= MIN_SAMPLES:
            inf["response_ratio"][name] = {"mean": round(statistics.mean(xs), 3), "n": len(xs)}
    if len(inf["response_ratio"]) >= 2:           # compare buckets only when at least two are measured
        best = max(inf["response_ratio"], key=lambda k: inf["response_ratio"][k]["mean"])
        inf["best_k"] = {"small": 0.1, "mid": 0.25, "large": 0.5}[best]
    if len(paid) >= MIN_DEALS:
        inf["paid_median"] = statistics.median(paid)
    return inf


# ---------------------------------------------------------------- Abuela: pure decision logic

def read_thread(t, me_id, dealer="abuela", sell=False):
    """Chronological facts of a thread: our offers and the dealer's (price, final, text).
    Buy thread: we offer cash (give.cash), they ask cash (want.cash). Sell thread: we ask (want.cash), they bid (give.cash)."""
    ours, hers = [], []
    mine, theirs = ("want", "give") if sell else ("give", "want")
    for m in t.get("messages", []):
        o = m.get("offer") or {}
        if not o:
            continue
        if m["sender"] == me_id:
            ours.append(o[mine]["cash"])
        elif m["sender"] == dealer:
            hers.append((o[theirs]["cash"], bool(o.get("final")), m.get("text", "")))
    return ours, hers


def rounds_of(ours, hers):
    """One record per answered concession: our move, her move, the gap before it."""
    rounds = []
    for k, (ask, final, _) in enumerate(hers):
        r = {"her_ask": ask, "final": final, "our": ours[k] if k < len(ours) else None,
             "our_move": None, "her_move": None, "gap_before": None}
        if 1 <= k < len(ours):
            r["our_move"] = ours[k] - ours[k - 1]
            r["her_move"] = hers[k - 1][0] - ask
            r["gap_before"] = hers[k - 1][0] - ours[k - 1]
        rounds.append(r)
    return rounds


def tone(ours, hers):
    """yielding | firm | near_final | unknown, from numbers first, then keywords in her last words."""
    if not hers:
        return "unknown"
    if hers[-1][1]:
        return "near_final"
    text = hers[-1][2].lower()
    if any(w in text for w in ("last offer", "final", "última", "ultima", "take it or", "walk")):
        return "near_final"
    if len(hers) >= 3 and hers[-1][0] == hers[-2][0] == hers[-3][0]:
        return "firm"
    if len(hers) >= 2 and hers[-1][0] < hers[-2][0]:
        return "yielding"
    if len(hers) >= 2 and hers[-1][0] == hers[-2][0] and len(ours) >= 2 and ours[-1] > ours[-2]:
        return "firm"                       # she saw us move and did not
    return "unknown"


def opening_bid(list_price, inferred):
    med = inferred.get("paid_median")
    return max(1, round(0.8 * med)) if med else max(1, round(0.5 * list_price))


def next_offer(cap, list_price, ours, hers, inferred):
    """(action, price, reason). action: accept | offer | wait | walk. Never above cap."""
    if not hers:
        if ours:
            return "wait", None, "no reply yet"
        return "offer", min(cap, opening_bid(list_price, inferred)), "opening bid"
    if len(ours) > len(hers):
        return "wait", None, "she has not answered our last offer"
    ask, final, _ = hers[-1]
    last = ours[-1] if ours else 0
    mood = tone(ours, hers)
    tol = max(1, int(0.04 * list_price))
    if ask <= cap and (ask <= last + tol or final):
        return "accept", ask, ("her final is under our cap" if final else "her ask is within reach of our own bid")
    if final:
        return "walk", None, f"her final {ask} is above our cap {cap}"
    if last > cap:
        return "walk", None, f"our standing offer {last} is above our cap {cap} (inherited): start clean"
    if last >= cap:
        stalled = len(hers) >= 2 and hers[-1][0] >= hers[-2][0]
        if stalled:
            return "walk", None, f"at our cap {cap}, she asks {ask}"
        return "wait", None, "at our cap, letting her move"
    gap = ask - last
    k = inferred.get("best_k") or DEFAULT_K
    why = [f"k={k}"]
    her_move = (hers[-2][0] - ask) if len(hers) >= 2 else None
    our_move = (ours[-1] - ours[-2]) if len(ours) >= 2 else None
    if mood == "yielding" and her_move is not None and our_move and her_move >= our_move:
        k *= 0.5                           # she gave at least as much as we did: do not run ahead of her
        why.append("she out-conceded us, slow down")
    if mood == "firm":
        if ask <= cap:
            k = max(k, 0.5)                # she stopped and the price is workable: close the deal
            why.append("she is firm and ask <= cap, close")
        else:
            k = 0.0                        # she stopped above our cap: one token step
            why.append("she is firm above cap, token step")
    step = max(1, round(gap * k))
    new = min(cap, last + step)
    if new <= last:
        return "walk", None, "no room left under the cap"
    return "offer", new, f"mood {mood}, gap {gap}, step {step} (" + ", ".join(why) + ")"


def next_offer_sell(floor, list_price, ours, hers, inferred, k0=0.35):
    """Mirror of next_offer for selling to a dealer: we ask, they bid. (action, price, reason); never below floor."""
    if not hers:
        if ours:
            return "wait", None, "no reply yet"
        return "offer", max(floor, list_price), "opening ask at his list price"
    if len(ours) > len(hers):
        return "wait", None, "he has not answered our last ask"
    bid, final, _ = hers[-1]
    last = ours[-1] if ours else None
    tol = max(1, int(0.04 * list_price))
    if last is None and not (bid >= floor and final):
        return "offer", max(floor, list_price), "opening ask (he spoke first)"
    if bid >= floor and (bid >= last - tol or final):
        return "accept", bid, "his final is over our floor" if final else "his bid is within reach of our ask"
    if final:
        return "walk", None, f"his final {bid} is below our floor {floor}"
    if last <= floor:
        stalled = len(hers) >= 2 and hers[-1][0] <= hers[-2][0]
        return ("walk", None, f"at our floor {floor}, he bids {bid}") if stalled else ("wait", None, "at our floor, letting him move")
    gap = last - bid
    k = inferred.get("best_k") or k0
    mood = "firm" if (len(hers) >= 2 and hers[-1][0] <= hers[-2][0]) else "yielding"
    if mood == "firm" and len(hers) >= 3:
        k = max(k, 0.5)
    step = max(1, round(gap * k))
    new = max(floor, last - step)
    if new >= last:
        return "walk", None, "no room left above the floor"
    return "offer", new, f"he is {mood}, gap {gap}, step {step} (k={k})"


def chato_text(side, price, n_round, item):
    """Short and plain: he talks little, has a long memory and punishes cleverness."""
    if n_round == 0:
        return (f"Buenas, Chato. Te ofrezco {item}: {price} primas." if side == "sell"
                else f"Buenas, Chato. Busco {item}. Te ofrezco {price} primas.")
    return f"Entendido. {price} primas." if side == "buy" else f"Entendido. Bajo a {price} primas."


PHRASES = ["Muchas gracias por su paciencia, Abuela. ¿Podríamos dejarlo en {p} primas?",
           "Es usted un sol. Me estiro un poquito más: {p} primas. ¿Le parece justo?",
           "Se lo agradezco de corazón, Carmen. Subo a {p} primas, que es lo que puedo.",
           "Qué bien se está con usted. ¿Qué tal {p} primas, de verdad?"]


def haggle_text(price, n_round, gift=False, item="un sobre de barrio"):
    if n_round == 0:
        return f"¡Hola, Abuela Carmen! Qué alegría verla, ¿ha comido ya? Vengo por {item}: ¿le parece bien {price} primas?"
    base = PHRASES[n_round % len(PHRASES)].format(p=price)
    return ("¡Y gracias por el regalito! " + base) if gift else base


# ---------------------------------------------------------------- market: values, fees, opportunities

def fee_of(venue, price):
    return math.ceil(venue.get("fee_bps", 0) * price / 10000) + venue.get("fee_per_card", 0)


def marginal_value(ref, count, catalog, affinity):
    """What the `count`-th copy of `ref` is worth to us: book * set affinity * copy marginal."""
    card = next((c for s in catalog["sets"] for c in s["cards"] if c["id"] == ref), None)
    if not card:
        return 0.0
    marg = catalog["values"]["copy_marginals"]
    return card["book"] * affinity.get(ref.split("-")[0], 1.0) * marg[min(max(count, 1) - 1, len(marg) - 1)]


def card_counts(me):
    counts, ids = {}, {}
    for a in me["assets"]:
        if a["kind"] == "card":
            counts[a["ref"]] = counts.get(a["ref"], 0) + 1
            ids.setdefault(a["ref"], []).append(a)
    return counts, ids


def ask_floor(value_lost):
    """Least ask (we are the maker, the taker pays the fee) that clears SELL_MIN_GAIN over what the copy is worth."""
    return math.ceil(value_lost + SELL_MIN_GAIN)


def rank_pick(opps):
    """Best opportunity by rank-sum of absolute gain and ROI (gain per prima of cash needed); one accept per tick."""
    if not opps:
        return None
    by_gain = {id(o): r for r, o in enumerate(sorted(opps, key=lambda o: -o["gain"]))}
    by_roi = {id(o): r for r, o in enumerate(sorted(opps, key=lambda o: -o["roi"]))}
    return min(opps, key=lambda o: (by_gain[id(o)] + by_roi[id(o)], -o["gain"]))


def market_opportunities(me, catalog, venue, offers, value_of):
    """Accept-able offers from others. buy: their ask vs our value of one more copy. sell: their bid vs the copy we would lose."""
    counts, ids = card_counts(me)
    free_cash = me["cash"] - CASH_RESERVE
    opps = []
    for o in offers:
        if o["maker"] == me["id"] or o.get("to") not in (None, me["id"]) or o["status"] != "open":
            continue
        g, w = o["give"], o["want"]
        if len(g["assets"]) == 1 and g["cash"] == 0 and w["cash"] > 0 and not w["types"]:      # they sell a card
            ref, ask = g["assets"][0]["ref"], w["cash"]
            cost = ask + fee_of(venue, ask)
            if cost > free_cash:
                continue
            v = value_of(ref, counts.get(ref, 0))
            if v is None:
                continue
            gain = v - cost
            if gain >= max(BUY_MIN_GAIN, 0.1 * cost):
                opps.append({"kind": "buy", "offer": o["id"], "ref": ref, "price": ask, "cost": cost, "value": round(v, 1),
                             "gain": round(gain, 1), "roi": round(gain / cost, 2)})
        elif g["cash"] > 0 and not g["assets"] and len(w["types"]) == 1 and w["types"][0].startswith("card:"):  # they bid for a card
            ref, bid = w["types"][0].split(":", 1)[1], g["cash"]
            n = counts.get(ref, 0)
            if n < 2:
                continue                                   # never sell our only copy
            lost = marginal_value(ref, n, catalog, me["affinity"])
            net = bid - fee_of(venue, bid)
            gain = net - lost
            if gain >= SELL_MIN_GAIN:
                opps.append({"kind": "sell", "offer": o["id"], "ref": ref, "price": bid, "asset": ids[ref][-1]["id"],
                             "value": round(lost, 1), "gain": round(gain, 1), "roi": round(gain, 2)})   # no cash needed
    return opps


def listing_plan(me, catalog, venue, offers, listed_assets):
    """[(asset id, ask, ref, value lost)] for surplus copies: floor from OUR value of the copy; target from rivals' asks and book."""
    counts, ids = card_counts(me)
    comp = {}
    for o in offers:
        if o["maker"] != me["id"] and len(o["give"]["assets"]) == 1 and o["want"]["cash"] and o["status"] == "open":
            comp.setdefault(o["give"]["assets"][0]["ref"], []).append(o["want"]["cash"])
    plan = []
    for ref, n in counts.items():
        if n < 2:
            continue
        card = next(c for s in catalog["sets"] for c in s["cards"] if c["id"] == ref)
        lost = marginal_value(ref, n, catalog, me["affinity"])
        floor = ask_floor(lost)
        target = (min(comp[ref]) - 1) if ref in comp else card["book"]     # undercut the cheapest rival, else book value
        ask = max(floor, min(target, math.ceil(card["book"] * 1.2)))
        spare = sorted(ids[ref], key=lambda a: -a["serial"])[:n - 1]       # keep the lowest serial
        for a in spare:
            if a["id"] not in listed_assets:
                plan.append((a["id"], ask, ref, round(lost, 1)))
    return plan


# ---------------------------------------------------------------- runtime

def current_venue(b):
    for v in b.venues().get("venues", []):
        if v["venue"] == "rastro":
            return v
    return {"fee_bps": 500, "fee_per_card": 1}


def phase_market(b, me, catalog, can_accept):
    try:
        venue = current_venue(b)
        offers = b.board("rastro").get("offers", [])
        mine = b.my_offers()
        listed = {a["id"] for o in mine.get("offers", []) if o.get("maker") == me["id"] for a in o["give"]["assets"]}
        directed = [o for o in mine.get("offers", []) if o.get("to") == me["id"]]
    except BazaarError as e:
        log("market unreadable:", e)
        return False
    seen = {o["id"] for o in offers}
    offers += [o for o in directed if o["id"] not in seen]
    cache, checks = {}, [0]

    def value_of(ref, count):
        key = (ref, count)
        if key not in cache:
            if checks[0] >= MAX_VALUE_CHECKS:
                return None
            checks[0] += 1
            try:
                cache[key] = b.value(ref).get("your_value")
            except BazaarError:
                cache[key] = None
        return cache[key]

    accepted = False
    opps = market_opportunities(me, catalog, venue, offers, value_of)
    if opps:
        log("market opportunities:", json.dumps(sorted(opps, key=lambda o: -o["gain"])[:5]))
    if can_accept:
        best = rank_pick(opps)
        if best:
            try:
                b.accept(best["offer"], assets=[best["asset"]] if best["kind"] == "sell" else None)
                accepted = True
                log(f"MARKET {best['kind'].upper()} {best['ref']} at {best['price']}: value {best['value']} gain {best['gain']} roi {best['roi']}")
            except BazaarError as e:
                log("market accept refused:", e.code, e.message)
    posted = 0
    for asset, ask, ref, lost in listing_plan(me, catalog, venue, offers, listed):
        if posted >= LISTINGS_PER_TICK or len(listed) + posted >= 28:
            break
        try:
            b.list_offer({"assets": [asset]}, {"cash": ask}, venue="rastro")
            posted += 1
            log(f"LISTED spare {ref} (asset {asset}) at {ask}; the copy is worth {lost} to us")
        except BazaarError as e:
            log("listing refused:", e.code, e.message)
            break
    return accepted


def pack_private_value(me, catalog, pack_id="sobre_barrio"):
    """Expected value of a pack TO US: for each slot, the average (over released cards of each rarity) of
    book * affinity * marginal of the copy we would add (first copy 1.0, second 0.25, ...). Duplicates are worth little."""
    pack = next(p for p in catalog["packs"] if p["id"] == pack_id)
    counts, _ = card_counts(me)
    marg = catalog["values"]["copy_marginals"]
    by_rarity = {}
    for s in catalog["sets"]:
        if not s.get("released") or s["id"] not in me["affinity"]:
            continue
        for c in s["cards"]:
            n = counts.get(c["id"], 0)
            by_rarity.setdefault(c["rarity"], []).append(c["book"] * me["affinity"][s["id"]] * marg[min(n, len(marg) - 1)])
    ev = 0.0
    for slot in pack["slots"]:
        for rarity, p in slot.items():
            vals = by_rarity.get(rarity)
            if vals:
                ev += p * statistics.mean(vals)
    return ev


PACK_EDGE = 0.9           # we pay at most this share of what the pack is worth to us


def pack_cap(me, catalog):
    return max(0, min(me["cash"] - CASH_RESERVE, int(PACK_EDGE * pack_private_value(me, catalog))))


def phase_abuela(b, me, catalog, mem):
    """One step of the Abuela negotiation. Returns True if we accepted something this tick."""
    inferred = mem["inferred"] = infer(mem)
    act = mem["active"]
    tid = next((t for t in me.get("open_threads", []) if b.thread(t).get("with") == "abuela"), None)
    if act and (tid is None or act["thread"] != tid):                   # our negotiation ended: record the outcome
        t = b.thread(act["thread"])
        outcome = t["status"]
        paid = act["cash_start"] - me["cash"] if outcome == "deal" else None
        ours, hers = read_thread(t, me["id"])
        rec = {"dealer": "abuela", "thread": act["thread"], "topic": act["topic"], "outcome": outcome, "closed_reason": t.get("closed_reason"),
               "paid": paid, "rounds": rounds_of(ours, hers)}
        mem["observed"]["negotiations"].append(rec)
        mem["active"], act = None, None
        if paid:
            mem["abuela_min_price"] = min(mem["abuela_min_price"], paid)
        log(f"negotiation {rec['thread']} ended: {outcome} {rec['closed_reason'] or ''} paid {paid}")
        log_chat(f"🏁 {rec['topic']}: {outcome} {rec['closed_reason'] or ''} pagado {paid}")
    for a in me["assets"]:                                              # open any sealed pack we hold
        if a["kind"] == "pack":
            try:
                cards = b.open_pack(a["id"])["cards"]
                log("opened pack:", [c.get("ref") or c.get("id") for c in cards])
            except BazaarError as e:
                log("open_pack:", e)
    cap = pack_cap(me, catalog)
    if tid is None:
        best_ever = min(mem.get("abuela_min_price", 999), 20)         # the lowest she ever sold a pack to us (capped at 20)
        if cap < best_ever:
            log(f"Abuela: a pack is worth {pack_private_value(me, catalog):.1f} to us (cap {cap}) and she never goes under {best_ever}: no deal")
            return False
        try:
            th = b.open_thread("abuela", topic={"buy": {"pack": "sobre_barrio"}})
        except BazaarError as e:
            log("Abuela unavailable:", e.code)
            return False
        tid = th["id"]
        mem["active"] = {"thread": tid, "topic": {"buy": {"pack": "sobre_barrio"}}, "cash_start": me["cash"]}
    elif mem["active"] is None:
        mem["active"] = {"thread": tid, "topic": b.thread(tid)["topic"], "cash_start": me["cash"]}
    t = b.thread(tid)
    if t["status"] != "open":
        return False
    ours, hers = read_thread(t, me["id"])
    action, price, why = next_offer(cap, 26, ours, hers, inferred)
    log(f"Abuela thread {tid}: ours {ours} hers {[h[0] for h in hers]} mood {tone(ours, hers)} cap {cap} -> {action} {price} ({why})")
    if hers:
        log_chat(f"👵 Abuela pide: {hers[-1][0]} P{' (final)' if hers[-1][1] else ''}")
    if action == "accept":
        offer = next((o for o in reversed(t["standing_offers"]) if o["maker"] == "abuela" and o["status"] == "open"), None)
        if offer is None:                                   # already accepted (it settles next tick): nothing to do
            log(f"Abuela thread {tid}: no open offer of hers left, already accepted?")
            return True
        b.accept(offer["id"])
        log_chat(f"🤝 aceptado a {price} P")
        return True
    if action == "walk":
        b.close_thread(tid)
        log_chat(f"🚶 nos vamos: {why}")
        return False
    if action == "offer":
        gift = bool(hers) and any(w in hers[-1][2].lower() for w in ("present", "regalo", "gift"))
        text = haggle_text(price, len(ours), gift)
        b.say(tid, text, price=price)
        log_chat(f"🤖 Nosotros ({price} P): {text}")
        mem["chat_history"].append(f"{price}P: {text}")
    return False


CHATO = {"uncommon": 26, "rare": 77}      # his list prices; he buys these two rarities


def chato_candidate(me, catalog):
    """The card to offer him: worth little to us (we ask 1.4x our value + margin) and realistic for him (<= 85% of his list)."""
    counts, ids = card_counts(me)
    best = None
    for ref, n in counts.items():
        a = ids[ref][-1]
        lp = CHATO.get(a["rarity"])
        if not lp:
            continue
        lost = marginal_value(ref, n, catalog, me["affinity"])
        floor = math.ceil(lost * 1.4 + SELL_MIN_GAIN)
        if floor <= 0.85 * lp:
            score = 0.8 * lp - lost
            if best is None or score > best[0]:
                best = (score, ref, a, floor, lp, lost)
    return best


def phase_chato(b, me, catalog, mem, can_accept):
    """One step with El Chato: sell him a card that is worth little to us. Returns True if we accepted something."""
    if "chato" not in me.get("unlocked", []):
        return False
    if mem.get("chato_block_until", -1) > me["tick"]:
        return False
    inferred = infer(mem, "chato")
    act = mem.get("active_chato")
    tid = next((t for t in me.get("open_threads", []) if b.thread(t).get("with") == "chato"), None)
    if act and (tid is None or act["thread"] != tid):
        t = b.thread(act["thread"])
        ours, hers = read_thread(t, me["id"], "chato", sell=True)
        got = me["cash"] - act["cash_start"] if t["status"] == "deal" else None
        mem["observed"]["negotiations"].append({"dealer": "chato", "thread": act["thread"], "topic": act["topic"], "outcome": t["status"],
                                                "closed_reason": t.get("closed_reason"), "paid": None, "received": got,
                                                "rounds": rounds_of(ours, hers)})
        reason = t.get("closed_reason")
        log(f"Chato negotiation {t['id']} ended: {t['status']} {reason or ''} received {got}")
        if t["status"] != "deal":
            mem["chato_block_until"] = me["tick"] + 30            # any ending but a deal: do not pester him, he remembers
            mem.setdefault("chato_tried", {})[act["ref"]] = me["tick"]
        mem["active_chato"], act = None, None
    if tid is None:
        cand = chato_candidate(me, catalog)
        if not cand:
            return False
        _, ref, asset, floor, lp, lost = cand
        if me["tick"] - mem.get("chato_tried", {}).get(ref, -999) < 90:
            return False                                          # same card, no new reason: stay quiet for a while
        try:
            th = b.open_thread("chato", topic={"sell": {"assets": [asset["id"]]}})
        except BazaarError as e:
            log("Chato unavailable:", e.code, e.message)
            mem["chato_block_until"] = me["tick"] + 30
            return False
        tid = th["id"]
        mem["active_chato"] = {"thread": tid, "topic": {"sell": {"assets": [asset["id"]]}}, "cash_start": me["cash"],
                               "ref": ref, "floor": floor, "lp": lp, "lost": round(lost, 1)}
        log(f"Chato: offering {ref} (worth {lost:.1f} to us), floor {floor}, his list {lp}")
    elif mem.get("active_chato") is None:
        return False                                              # a thread we did not open: leave it alone
    act = mem["active_chato"]
    t = b.thread(tid)
    if t["status"] != "open":
        return False
    ours, hers = read_thread(t, me["id"], "chato", sell=True)
    action, price, why = next_offer_sell(act["floor"], act["lp"], ours, hers, inferred)
    log(f"Chato thread {tid}: ours {ours} his {[h[0] for h in hers]} floor {act['floor']} -> {action} {price} ({why})")
    if action == "accept" and can_accept:
        offer = next((o for o in reversed(t["standing_offers"]) if o["maker"] == "chato" and o["status"] == "open"), None)
        if offer is None:
            return True
        b.accept(offer["id"])
        log(f"CHATO SELL {act['ref']} at {price}: it was worth {act['lost']} to us")
        return True
    if action == "walk":
        b.close_thread(tid)
    elif action == "offer":
        b.say(tid, chato_text("sell", price, len(ours), act["ref"]), price=price)
    return False


def run_agent():
    b = Bazaar(URL, os.environ["BAZAAR_KEY"], wait_on_tick=False)
    mem = load_memory()
    log("smart_agent started (deterministic, no LLM)")
    while True:
        try:
            if b.clock().get("paused"):
                time.sleep(10)
                continue
            me, catalog = b.me(), b.catalog()
            log(f"--- tick {me['tick']} | cash {me['cash']} | cards {sum(a['kind'] == 'card' for a in me['assets'])} ---")
            accepted = False
            try:
                accepted = phase_abuela(b, me, catalog, mem)
            except BazaarError as e:
                log("Abuela step:", e.code, e.message)
            try:
                accepted = phase_chato(b, me, catalog, mem, can_accept=not accepted) or accepted
            except BazaarError as e:
                log("Chato step:", e.code, e.message)
            phase_market(b, me, catalog, can_accept=not accepted)
            save_memory(mem)
            b.wait_tick()
        except KeyboardInterrupt:
            break
        except BazaarError as e:
            log("api error:", e.code, e.message)
            time.sleep(3)
        except Exception as e:
            log(f"error {type(e).__name__}: {e}")
            time.sleep(5)


# ---------------------------------------------------------------- offline checks

def selftest():
    def play(cap, open_ask, floor, resp, final_after=None, list_price=26, inferred=None):
        ours, hers, ask = [], [], open_ask
        for rnd in range(30):
            hers.append((ask, final_after is not None and rnd >= final_after, ""))
            act, price, why = next_offer(cap, list_price, ours, hers, inferred or {})
            assert price is None or price <= cap, f"offer {price} above cap {cap}"
            if act == "accept":
                return price, ours, "deal"
            if act == "walk":
                return None, ours, "walk"
            if act == "offer":
                ours.append(price)
                if price >= floor:
                    return price, ours, "she takes our offer"
                ask = max(floor, ask - max(0, round((ask - price) * resp)))
        return None, ours, "stuck"
    r1 = play(24, 30, 17, 0.5)
    r2 = play(24, 30, 17, 0.0)
    r3 = play(24, 30, 28, 0.5)
    r4 = play(24, 30, 17, 0.5, final_after=2)
    print("floor 17, she concedes half  ->", r1)
    print("she never concedes           ->", r2)
    print("her floor above our cap      ->", r3)
    print("she announces final          ->", r4)
    assert r1[0] is not None and r1[0] <= 24
    assert r3[0] is None, "must not buy above the cap"
    assert r2[2] in ("walk", "deal", "she takes our offer", "stuck")
    steps = [b - a for a, b in zip(r1[1], r1[1][1:])]
    assert len(set(steps)) > 1 or len(steps) < 2, "steps should adapt to the gap"
    mem = {"observed": {"negotiations": [{"outcome": "deal", "paid": 17, "rounds": [{"her_ask": 30}]}]}}
    assert infer(mem)["paid_median"] is None and infer(mem)["response_ratio"] == {}, "no conclusions from 1 sample"
    mem["observed"]["negotiations"] += [{"outcome": "deal", "paid": p, "rounds": [{"her_ask": 30}]} for p in (21, 24)]
    assert infer(mem)["paid_median"] == 21 and opening_bid(26, infer(mem)) == 17
    catalog = {"sets": [{"id": "LAT", "cards": [{"id": "LAT-04", "book": 10}, {"id": "LAT-05", "book": 10}]},
                        {"id": "LAV", "cards": [{"id": "LAV-03", "book": 10}]}], "values": {"copy_marginals": [1.0, 0.25, 0.1]}}
    me = {"id": "t06", "cash": 100, "affinity": {"LAT": 0.7, "LAV": 1.6},
          "assets": [{"id": 1, "kind": "card", "ref": "LAT-04", "serial": 3}, {"id": 2, "kind": "card", "ref": "LAT-04", "serial": 9},
                     {"id": 3, "kind": "card", "ref": "LAT-05", "serial": 1}]}
    venue = {"fee_bps": 500, "fee_per_card": 1}
    assert marginal_value("LAT-04", 2, catalog, me["affinity"]) == 10 * 0.7 * 0.25
    assert fee_of(venue, 20) == 2
    mk = lambda i, give, want, maker="tx", to=None: {"id": i, "maker": maker, "to": to, "status": "open", "give": give, "want": want}
    offers = [mk(10, {"cash": 0, "assets": [{"ref": "LAV-03", "id": 50}], "types": []}, {"cash": 9, "assets": [], "types": []}),
              mk(11, {"cash": 0, "assets": [{"ref": "LAV-03", "id": 51}], "types": []}, {"cash": 30, "assets": [], "types": []}),
              mk(12, {"cash": 8, "assets": [], "types": []}, {"cash": 0, "assets": [], "types": ["card:LAT-04"]}),
              mk(13, {"cash": 50, "assets": [], "types": []}, {"cash": 0, "assets": [], "types": ["card:LAT-05"]})]
    opps = market_opportunities(me, catalog, venue, offers, lambda ref, n: 16.0 if ref == "LAV-03" else None)
    kinds = sorted((o["kind"], o["offer"]) for o in opps)
    assert kinds == [("buy", 10), ("sell", 12)], kinds
    plan = listing_plan(me, catalog, venue, offers, set())
    assert [p[0] for p in plan] == [2], "list the higher serial, keep the lowest"
    assert plan[0][1] >= ask_floor(1.75), "ask must clear what the copy is worth to us"
    def play_sell(floor, open_bid, ceiling, resp, final_after=None, lp=26):
        ours, hers, bid = [], [], open_bid
        a0, p0, _ = next_offer_sell(floor, lp, ours, hers, {})        # we speak first
        ours.append(p0)
        for rnd in range(30):
            hers.append((bid, final_after is not None and rnd >= final_after, ""))
            act, price, why = next_offer_sell(floor, lp, ours, hers, {})
            assert price is None or price >= floor, f"ask {price} below floor {floor}"
            if act == "accept":
                return price, "deal"
            if act == "walk":
                return None, "walk"
            if act == "offer":
                ours.append(price)
                if price <= ceiling:
                    return price, "he takes our ask"
                bid = min(ceiling, bid + max(0, round((price - bid) * resp)))
        return None, "stuck"
    s1, s2, s3 = play_sell(20, 8, 22, 0.4), play_sell(20, 8, 15, 0.4), play_sell(20, 8, 24, 0.4, final_after=2)
    print("sell: he pays up to 22 ->", s1, "| up to 15 (below floor) ->", s2, "| final at round 2 ->", s3)
    assert s1[0] is not None and s1[0] >= 20 and s2[0] is None
    catalog2 = {"sets": [{"id": "MAL", "cards": [{"id": "MAL-07", "book": 25}, {"id": "LAT-10", "book": 70}]},
                         {"id": "LAT", "cards": [{"id": "LAT-10", "book": 70}]}], "values": {"copy_marginals": [1.0, 0.25, 0.1]}}
    me2 = {"affinity": {"MAL": 0.5, "LAT": 0.7}, "assets": [{"id": 9, "kind": "card", "ref": "MAL-07", "serial": 1, "rarity": "uncommon"},
                                                          {"id": 8, "kind": "card", "ref": "LAT-10", "serial": 1, "rarity": "rare"}]}
    cand = chato_candidate(me2, catalog2)
    assert cand and cand[1] == "MAL-07", "only the card that is cheap for us and realistic for him"
    cat3 = {"sets": [{"id": "LAV", "released": True, "cards": [{"id": f"LAV-{i:02d}", "book": 10, "rarity": "common"} for i in range(1, 6)]}],
            "packs": [{"id": "sobre_barrio", "slots": [{"common": 1.0}, {"common": 1.0}]}], "values": {"copy_marginals": [1.0, 0.25, 0.1]}}
    empty = {"affinity": {"LAV": 1.0}, "assets": []}
    full = {"affinity": {"LAV": 1.0}, "assets": [{"kind": "card", "ref": f"LAV-{i:02d}", "id": i, "serial": 1} for i in range(1, 6)]}
    assert pack_private_value(empty, cat3) == 20.0 and pack_private_value(full, cat3) == 5.0, "a pack of cards we own is worth little"

    class _EndedChato:
        def thread(self, tid):
            return {"id": tid, "with": "chato", "status": "closed", "closed_reason": "no_progress",
                    "messages": [
                        {"sender": "t06", "text": "", "offer": {"want": {"cash": 26}, "give": {"cash": 0}}},
                        {"sender": "chato", "text": "", "offer": {"give": {"cash": 13}, "want": {"cash": 0}}},
                        {"sender": "t06", "text": "", "offer": {"want": {"cash": 22}, "give": {"cash": 0}}},
                        {"sender": "chato", "text": "", "offer": {"give": {"cash": 15}, "want": {"cash": 0}}},
                    ]}

    mem_c = {"observed": {"negotiations": []}, "active_chato": {
        "thread": 7, "topic": {"sell": {"assets": [9]}}, "cash_start": 100,
        "ref": "MAL-07", "floor": 16, "lp": 26, "lost": 8}}
    me_c = {"id": "t06", "tick": 40, "cash": 100, "unlocked": ["chato"], "open_threads": [],
            "assets": [], "affinity": {}}
    assert phase_chato(_EndedChato(), me_c, {}, mem_c, True) is False
    assert mem_c["active_chato"] is None and mem_c["chato_tried"]["MAL-07"] == 40
    assert mem_c["chato_block_until"] == 70
    assert mem_c["observed"]["negotiations"][-1]["outcome"] == "closed"
    # Same shape phase_chato stores: the ask fell, and the cash is `received`, not `paid`.
    concession = {"her_ask": 15, "final": False, "our": 22, "our_move": -4, "her_move": -2, "gap_before": -16}
    opening = {"her_ask": 13, "final": False, "our": 26, "our_move": None, "her_move": None, "gap_before": None}
    learned = {"observed": {"negotiations": [
        {"dealer": "chato", "outcome": "deal", "paid": None, "received": got, "rounds": [opening, concession, concession]}
        for got in (18, 20, 22)]}}
    inf = infer(learned, "chato")
    assert inf["response_ratio"]["mid"] == {"mean": 0.5, "n": 6}, inf
    assert inf["paid_median"] == 20 and inf["paid_n"] == 3
    buy_down = {"observed": {"negotiations": [
        {"outcome": "deal", "paid": 21, "rounds": [concession] * 5}]}}
    assert infer(buy_down)["response_ratio"] == {}, "a buy that moved the wrong way is not a concession"
    print("selftest OK")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    else:
        run_agent()
