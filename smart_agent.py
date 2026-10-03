"""Team agent: Abuela negotiation + market trading. Deterministic, no LLM, no API key beyond the team key.

    BAZAAR_KEY=tk-... python3 smart_agent.py        # runs forever, one pass per tick
    python3 smart_agent.py --selftest               # offline checks, no network

Phase 1  market   buy cards that are worth more to us than they cost (value - price - fee), fill bids and swaps
                  that gain at our own values, and sell surplus copies (floors from OUR value of the copy, never
                  `book * k`). Cash asks and bids go to FAIR (v21, Team 9, 0 fee, midpoint match; v07 would score for Team 10) for 120 ticks.
                  Swaps stay on El Duende (v02): the fair venue's broker crosses a bid with an ask, not card for card.
Phase 2  Abuela   adaptive haggling. Every round we ask: how did she answer our last concession?
                  - her move per our move (response ratio) sizes the next step;
                  - she stopped moving -> we stop paying for nothing;  `final` -> take it under our cap or walk.
                  Prices and limits are plain code; the tone read of her messages is a keyword/number classifier.
Memory   memory.json keeps OBSERVED facts (every round, every outcome) apart from INFERENCES (computed from them,
         each with its sample size, and only when the sample is large enough).
"""
import json
import re
import math
import os
import statistics
import sys
import time

from bazaar_sdk import Bazaar, BazaarError
from bz.core.accept_gate import try_reserve        # hotfix 1.4: one accept per tick across processes
from bz.dealers import params as dparams          # every dealer-negotiation number, with bounds, in one table
from bz.trading.mode import legacy_should_trade    # TRADING_V2=on: trading_v2.py is the only trading authority
from flags import is_item_lie, is_lie, is_switch
import feed_intel
import ladder
import workshop

URL = os.environ.get("BAZAAR_URL", "https://bazaar.causaprima.ai")
MEMORY_FILE = os.environ.get("AGENT_MEMORY", "memory.json")
# Organiser's rule (Sat 2026-10-03 ~21:50, the key one): never lose, make money. Every deal must gain value AND we must
# end with more cash than the 541 P we held then. So 541 P are never spent: only what we earn above them (sales over
# our value, Sunday's +150) can buy, and a buy must still be under our value.
CASH_RESERVE = int(os.environ.get("CASH_RESERVE", "541"))  # primas never spent
DEFAULT_K = dparams.SPEC["default_k"][0]      # kept for compatibility; the strategy reads dparams.static("default_k")
MIN_SAMPLES = 3           # observations per step-size bucket before we trust a response ratio (was 5: we have so little data
                          # that nothing was ever inferred; the choice it drives, k in {0.1, 0.25, 0.5}, is bounded anyway)
MIN_DEALS = 2             # finished deals before we adapt the opening bid (was 3)
BUY_MIN_GAIN = 3          # primas of private-value gain we need to buy from another team
SELL_MIN_GAIN = 2         # primas over what the copy is worth to us to sell it
LISTINGS_PER_TICK = 3
BUYER_MULT = 1.3          # ask for a duplicate with no competition: book x this (upper half of the buyers' multipliers)
MAX_VALUE_CHECKS = 12     # b.value() calls per tick (rate limit friendly)
BOARD_TTL = 120           # board offers; an offer inside a thread dies after 2 ticks
DUENDE = "v02"            # El Duende: 0% and 0 P per card; swaps live here
TRECE = "v03"             # Mercado Trece: 1% , 0 P per card (swaps are free)
FAIR = os.environ.get("FAIR_VENUE", "v21")   # 0% broker venue for our cash bids/asks: Team 9 (v21). Not v07: a trade there scores for Team 10, our closest rival


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
    return "small" if r < dparams.static("bucket_small") else "mid" if r < dparams.static("bucket_mid") else "large"


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
        if len(xs) >= dparams.static("min_samples"):
            inf["response_ratio"][name] = {"mean": round(statistics.mean(xs), 3), "n": len(xs)}
    if len(inf["response_ratio"]) >= 2:           # compare buckets only when at least two are measured
        best = max(inf["response_ratio"], key=lambda k: inf["response_ratio"][k]["mean"])
        inf["best_k"] = {"small": dparams.static("k_small"), "mid": dparams.static("k_mid"), "large": dparams.static("k_large")}[best]
    if len(paid) >= dparams.static("min_deals"):
        inf["paid_median"] = statistics.median(paid)
    return inf


def profile_hints(mem, key):
    """Evidence about WHERE a dealer settles, from every negotiation we have had with `key` (no new stored state).
    settle prices = prices at which it showed it WOULD trade: deal prices and its own `final` offers. They sit ABOVE its hidden
    floor (buy side; below it on the sell side), so they are an upper bound of how low we can go, never a measurement of it.
      open_bid   (buy)  0.7 x the lowest settle price: open well under what it has ever settled at, saving rounds.
      open_ask   (sell) 1.2 x the highest settle price, never above its list price (applied by next_offer_sell).
      soft_cap   (buy)  1.05 x the highest settle price (>= 3 observations): beyond it we only creep up 1 prima at a time.
      rejected   the most informative offer of ours it turned down (highest on the buy side, lowest on the sell side).
    Empty dict when there is not enough evidence (>= 2 settle prices)."""
    prices, rejected, sale = [], [], False
    for n in mem["observed"]["negotiations"]:
        if n.get("dealer", "abuela") != key:
            continue
        is_sale = "received" in n
        sale = sale or is_sale
        amount = n.get("paid") or n.get("received")
        if n.get("outcome") == "deal" and amount:
            prices.append(amount)
        for r in n.get("rounds", []):
            if r.get("final") and r.get("her_ask"):
                prices.append(r["her_ask"])
            our, her = r.get("our"), r.get("her_ask")
            if our is not None and her is not None and ((not is_sale and her > our) or (is_sale and her < our)):
                rejected.append(our)
    out = {}
    if len(prices) >= dparams.static("min_settle"):
        lo, hi = min(prices), max(prices)
        out.update(settle_n=len(prices), settle_min=lo, settle_max=hi)
        if sale:
            out["open_ask"] = math.ceil(dparams.static("open_ask_mult") * hi)
        else:
            out["open_bid"] = max(1, round(dparams.static("open_frac_settle") * lo))
            if len(prices) >= dparams.static("min_settle_cap"):
                out["soft_cap"] = math.ceil(dparams.static("soft_cap_mult") * hi)
    if rejected:
        out["rejected"] = min(rejected) if sale else max(rejected)
    return out


def trait_multiplier(traits):
    """Prior for how big our concession steps should be, from the dealer's published traits (patience, generosity, shrewdness...).
    1.0 = the default step. A generous, patient, unshrewd dealer (Abuela) -> smaller steps; a shrewd one (Pilar) -> bigger.
    HYPOTHESIS, bounded to [0.5, 1.6]; it only applies while we have no learned `best_k` for that dealer."""
    ref = {"shrewdness": dparams.static("ref_shrew"), "generosity": dparams.static("ref_gen"), "patience": dparams.static("ref_pat")}
    g = lambda k, d=None: float((traits or {}).get(k, ref[k] if d is None else d))
    m = (1 + dparams.static("trait_shrew") * (g("shrewdness") - ref["shrewdness"]) - dparams.static("trait_gen") * (g("generosity") - ref["generosity"])
         - dparams.static("trait_pat") * (g("patience") - ref["patience"]))
    return round(max(dparams.static("prior_lo"), min(dparams.static("prior_hi"), m)), 3)


def menu_list_price(mem, dealer, kind, default=None):
    """The dealer's published list price for `kind` ("pack:sobre_barrio", "rarity:uncommon"), else `default`."""
    return ((mem.get("dealer_menu") or {}).get(dealer) or {}).get(kind, default)


def log_param_changes(mem, key, list_price=None):
    """Say, once per change, which learned parameters moved away from their defaults and why (so learning is never silent)."""
    notes = dparams.derive(mem, key, list_price)["notes"]
    seen = mem.setdefault("params_logged", {})
    text = "; ".join(notes)
    if text and seen.get(key) != text:
        seen[key] = text
        log(f"learned parameters for {key}: {text}")


def learned(mem, key, list_price=None):
    """Everything we know about dealer `key` ("abuela", "chato", "abuela_buy", ...): inferred ratios + profile hints + trait prior
    + the learned parameters (tol_share, open_scale, block_after_fail: bz/dealers/params.py)."""
    inf = infer(mem, key)
    inf.update(profile_hints(mem, key))
    inf["params"] = dparams.learned_params(mem, key, list_price)
    base = key[:-4] if key.endswith("_buy") else key
    traits = (mem.get("dealer_traits") or {}).get(base)
    if traits:
        inf["prior_mult"] = trait_multiplier(traits)
    # behaviour of this dealer with ALL teams (public conversations): the dealer sells to us on "abuela" (packs) and
    # "<x>_buy" keys, and buys from us on the plain sell keys ("chato", "pilar", "picaros")
    side = "sells" if (key.endswith("_buy") or key == "abuela") else "buys"
    bh = feed_intel.behavior(mem, base, side)
    inf["final_unreliable"] = bool(bh.get("final_unreliable"))
    inf["fixed_bidder"] = bool(bh.get("fixed_bidder"))
    return inf


TRAITS_EVERY = dparams.SPEC["traits_every"][0]        # compat; refresh_traits reads dparams.static("traits_every")


def refresh_traits(b, mem, tick):
    """Store each dealer's traits from GET /api/dealers (real shape: {"personas": [{id, traits{...}}]}). Cheap: 1 read per
    TRAITS_EVERY ticks. Any failure leaves the previous values in place."""
    if tick - mem.get("dealer_traits_tick", -10 ** 9) < dparams.static("traits_every"):
        return False
    mem["dealer_traits_tick"] = tick                    # even on failure: do not hammer the endpoint
    try:
        resp = b.dealers()
    except BazaarError as e:
        log("dealer traits unavailable:", e.code)
        return False
    rows = resp.get("personas") or resp.get("dealers") or []
    traits = {d["id"]: d["traits"] for d in rows if isinstance(d, dict) and d.get("id") and isinstance(d.get("traits"), dict)}
    menu = {}
    for d in rows:                                              # list prices the dealer publishes: no hand-written copies
        if isinstance(d, dict) and d.get("id"):
            for e in ((d.get("menu") or {}).get("sells") or []):
                if isinstance(e, dict) and isinstance(e.get("list_price"), (int, float)):
                    kind = f"pack:{e['pack']}" if e.get("pack") else f"rarity:{e['rarity']}" if e.get("rarity") else None
                    if kind:
                        menu.setdefault(d["id"], {})[kind] = e["list_price"]
    if menu:
        mem["dealer_menu"] = menu
    if traits:
        mem["dealer_traits"] = traits
        log("dealer traits:", {k: trait_multiplier(v) for k, v in traits.items()})
    return bool(traits)


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


def ladder_round(mem):
    return (mem.get("ladder") or {}).get("round")


def ladder_open(mem, dealer, rarity, hers=None, fallback=None):
    """The dealer's opening price in this talk (its first quote), else the last opening it gave us for this rarity."""
    opens = mem.setdefault("ladder", {}).setdefault("opens", {})
    if hers:
        opens[f"{dealer}|{rarity}"] = hers[0][0]
        return hers[0][0]
    return opens.get(f"{dealer}|{rarity}", fallback)


def range_hints(mem, dealer, side, rarity, setid=None, ref=None):
    """feed_intel hints for the ladder range; with fewer than its MIN_DEALS deals, the raw real deals of this
    dealer/side/rarity still bound the range better than a guess from the list (Don Ernesto: 2 deals on Saturday)."""
    fh = feed_intel.hints(mem, dealer, side, rarity, setid, ref)
    if fh:
        return fh
    ps = [r["price"] for r in ((mem.get("feed_intel") or {}).get("deals") or {}).get(f"{dealer}|{side}|{rarity}", [])
          if isinstance(r, dict) and isinstance(r.get("price"), (int, float))]
    return {"n_deals": len(ps), "deal_min": min(ps), "deal_max": max(ps), "level": "raw"} if ps else {}


def ladder_record(mem, dealer, thread, rarity, setid, ref, hers, price, buy):
    """A finished deal enters this round's ladder book with the share it captured (opening = its first quote)."""
    if not hers or price is None or ladder_round(mem) is None:
        return
    fh = range_hints(mem, dealer, "sells" if buy else "buys", rarity, setid, ref)
    open_ = hers[0][0]
    s = ladder.record_deal(mem, ladder_round(mem), dealer, thread, open_, ladder.limit_estimate(fh, open_, buy), price, buy)
    if s is not None:
        log(f"ladder: {dealer} deal at {price} (opened {open_}) captured {s:.2f} of its range; "
            f"worst of our best three now {ladder.worst_of_best3(mem, ladder_round(mem), dealer):.2f}")


# Organisers' Payday deck (Sat 2026-10-03): "a deal = value it adds to your collection - price paid + price received;
# DEAL WITH A DEALER: a gain counts on the ladder, a loss counts in full; dealer to dealer: buying gains nothing, selling
# below value costs; never sell below your value". So a ladder premium (paying over our value, selling under it) is a
# loss counted in full: off by default. LADDER_PREMIUM=1 brings the old behaviour back.
LADDER_PREMIUM = os.environ.get("LADDER_PREMIUM", "0") == "1"


def buy_cap_with_ladder(mem, me, dealer, rarity, lp, fh, value_cap, hers, ref):
    """Our private-value cap; with LADDER_PREMIUM, lifted while the ladder gain at that price covers the premium."""
    rnd = ladder_round(mem)
    if rnd is None or not LADDER_PREMIUM:
        return value_cap
    open_ = ladder_open(mem, dealer, rarity, hers, fallback=round(lp * 1.25) if lp else None)
    limit = ladder.limit_estimate(fh, open_, True, lp)
    free = me["cash"] - CASH_RESERVE
    premium_free = max(0, free - EPIC_LOOP_RESERVE)       # ladder premiums never spend Sunday's reserve either
    cap, pts, prem = ladder.ladder_cap(mem, rnd, dealer, open_, limit, value_cap, premium_free)
    cap = min(free, max(value_cap, cap))
    if cap > value_cap and hers is not None:
        log(f"ladder: {dealer} {ref} cap {value_cap} -> {cap} (+{cap - value_cap} P for ~{pts:.2f} pts; "
            f"range {open_}->{limit}, S {ladder.slope(mem):.1f})")
    return cap


def sell_floor_with_ladder(mem, me, dealer, rarity, fh, value_floor, hers, ref):
    """Our private-value floor; with LADDER_PREMIUM, lowered while the ladder gain covers what we give up."""
    rnd = ladder_round(mem)
    if rnd is None or not hers or not LADDER_PREMIUM:
        return value_floor
    open_ = ladder_open(mem, dealer, rarity, hers)
    limit = ladder.limit_estimate(fh, open_, False)
    fl, pts, prem = ladder.ladder_floor(mem, rnd, dealer, open_, limit, value_floor, me["cash"] - CASH_RESERVE)
    if fl < value_floor:
        log(f"ladder: {dealer} {ref} floor {value_floor} -> {fl} (-{value_floor - fl} P for ~{pts:.2f} pts; "
            f"range {open_}->{limit}, S {ladder.slope(mem):.1f})")
    return fl


def round_start_tick(clock, seen_change, history_file=os.path.join("dashboard", "history.json")):
    """First tick of today's round. Seen live (the round changed under us): this tick. Started mid-round: the first
    dashboard sample at or after today's opening time (it can only be late, which drops a few early deals, never adds
    yesterday's). Unknown: this tick."""
    if seen_change:
        return clock["tick"]
    import datetime
    day = next((d for d in clock.get("days", []) if d.get("day") == clock.get("today")), None)
    try:
        opens = datetime.datetime.fromisoformat(day["opens"]).timestamp()
        with open(history_file) as f:
            return min(p["tick"] for p in json.load(f) if p.get("ts", 0) >= opens and p.get("tick") is not None)
    except (TypeError, KeyError, OSError, ValueError):
        return clock["tick"]


def ladder_backfill(b, mem, me_id, rarity_of, start_tick):
    """Once per round: our dealer deals already done in it (threads since start_tick) enter the ladder book."""
    lad = mem.setdefault("ladder", {})
    if lad.get("backfilled") == lad.get("round"):
        return
    for t in b.my_threads().get("threads", []):
        d = t.get("with")
        if t.get("status") != "deal" or d not in ladder.LEVEL or t.get("created_tick", 0) < start_tick:
            continue
        topic = t.get("topic") or {}
        buy = "buy" in topic
        if buy:
            ref = (topic["buy"] or {}).get("card")
            rarity = rarity_of(ref) if ref else None
        else:
            asset = next((o["give"]["assets"][0] for m in t.get("messages", []) for o in [m.get("offer") or {}]
                          if o.get("maker") == me_id and (o.get("give") or {}).get("assets")), None)
            ref, rarity = (asset.get("ref"), asset.get("rarity")) if isinstance(asset, dict) else (None, None)
        _ours, hers = read_thread(t, me_id, d, sell=not buy)
        if not ref and not (buy and (topic["buy"] or {}).get("pack")):
            continue
        ladder_record(mem, d, t["id"], rarity, ref.split("-")[0] if ref else None, ref, hers, deal_price(t, me_id), buy)
    lad["backfilled"] = lad.get("round")


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
    n_firm = dparams.static("firm_rounds")
    if len(hers) >= n_firm and len({h[0] for h in hers[-n_firm:]}) == 1:
        return "firm"
    if len(hers) >= 2 and hers[-1][0] < hers[-2][0]:
        return "yielding"
    if len(hers) >= 2 and hers[-1][0] == hers[-2][0] and len(ours) >= 2 and ours[-1] > ours[-2]:
        return "firm"                       # she saw us move and did not
    return "unknown"


def opening_bid(list_price, inferred):
    scale = (inferred.get("params") or {}).get("open_scale", 1.0)       # learned from how our past openings fared
    if inferred.get("open_bid"):                                   # evidence about where it settles (profile_hints)
        base = inferred["open_bid"]
    else:
        med = inferred.get("paid_median")
        base = round(dparams.static("open_frac_median") * med) if med else round(dparams.static("open_frac_list") * list_price)
    return max(1, round(base * scale))


def next_offer(cap, list_price, ours, hers, inferred, open_cap=None):
    """(action, price, reason). action: accept | offer | wait | walk. Never above cap. open_cap: the opening bid never
    goes above it (the private-value cap: a ladder premium is room to haggle into, never where we start)."""
    if not hers:
        if ours:
            return "wait", None, "no reply yet"
        return "offer", min(cap, open_cap if open_cap is not None else cap, opening_bid(list_price, inferred)), "opening bid"
    if len(ours) > len(hers):
        return "wait", None, "she has not answered our last offer"
    ask, final, _ = hers[-1]
    last = ours[-1] if ours else 0
    mood = tone(ours, hers)
    tol = max(1, int((inferred.get("params") or {}).get("tol_share", dparams.static("tol_share")) * list_price))
    if ask <= cap and (ask <= last + tol or final):
        return "accept", ask, ("her final is under our cap" if final else "her ask is within reach of our own bid")
    if final and not inferred.get("final_unreliable"):
        return "walk", None, f"her final {ask} is above our cap {cap}"
    if last > cap:
        return "walk", None, f"our standing offer {last} is above our cap {cap} (inherited): start clean"
    if last >= cap:
        stalled = len(hers) >= 2 and hers[-1][0] >= hers[-2][0]
        if stalled:
            return "walk", None, f"at our cap {cap}, she asks {ask}"
        return "wait", None, "at our cap, letting her move"
    gap = ask - last
    k = inferred.get("best_k") or round(dparams.static("default_k") * inferred.get("prior_mult", 1.0), 3)
    why = [f"k={k}"]
    her_move = (hers[-2][0] - ask) if len(hers) >= 2 else None
    our_move = (ours[-1] - ours[-2]) if len(ours) >= 2 else None
    if mood == "yielding" and her_move is not None and our_move and her_move >= our_move:
        k *= dparams.static("slow_mult")    # she gave at least as much as we did: do not run ahead of her
        why.append("she out-conceded us, slow down")
    if mood == "firm":
        if ask <= cap:
            k = max(k, dparams.static("firm_k"))   # she stopped and the price is workable: close the deal
            why.append("she is firm and ask <= cap, close")
        else:
            k = dparams.static("token_k")   # she stopped above our cap: one token step
            why.append("she is firm above cap, token step")
    step = max(1, round(gap * k))
    soft = inferred.get("soft_cap")
    if soft is not None and last >= soft:
        step = 1                           # past the highest price it has ever settled at: creep, do not run toward the cap
        why.append(f"past its usual ceiling {soft}, creeping")
    new = min(cap, last + step)
    if new <= last:
        return "walk", None, "no room left under the cap"
    return "offer", new, f"mood {mood}, gap {gap}, step {step} (" + ", ".join(why) + ")"


def deal_price(t, me_id):
    """Price of a dealer thread that ended in a deal, from the thread itself: the last priced offer was the one accepted
    (the dealer's when we accepted it, ours when the dealer accepted). Never a cash difference, which other trades in the
    same ticks would pollute. None when the thread has no priced offer."""
    last = None
    for m in t.get("messages", []):
        o = m.get("offer") or {}
        p = (o.get("give") or {}).get("cash") or (o.get("want") or {}).get("cash")
        if p:
            last = p
    return last


def next_offer_sell(floor, list_price, ours, hers, inferred, k0=None, soft_floor=None, hold=False):
    """Mirror of next_offer for selling to a dealer: we ask, they bid. (action, price, reason); never below floor.
    hold=True (Doña Pilar): no tolerance, no soft floor and one prima per round: accept only her `final` or a bid that
    meets our ask. Her pattern (6 talks): opens 16 (22 for SAL/RET), holds two rounds, then +1 a round; our old 3-2-1
    steps gave the ground away and closing at her opening captured ~0 of her range (the ladder measures that share)."""
    ask0 = min(list_price, inferred["open_ask"]) if inferred.get("open_ask") else list_price      # open_ask: profile_hints
    if inferred.get("open_ask"):
        ask0 = min(list_price, round(ask0 * (inferred.get("params") or {}).get("open_scale", 1.0)))
    if not hers:
        if ours:
            return "wait", None, "no reply yet"
        return "offer", max(floor, ask0), "opening ask at his list price"
    if len(ours) > len(hers):
        return "wait", None, "he has not answered our last ask"
    bid, final, _ = hers[-1]
    last = ours[-1] if ours else None
    tol = 0 if hold else max(1, int((inferred.get("params") or {}).get("tol_share", dparams.static("tol_share")) * list_price))
    if hold:
        soft_floor = None
    if inferred.get("fixed_bidder"):                       # all teams saw it never move from its first bid: no haggling
        return (("accept", bid, f"fixed bidder: {bid} clears our floor {floor}") if bid >= floor
                else ("walk", None, f"fixed bidder: {bid} under our floor {floor}, it will not move"))
    if last is None and not (bid >= floor and final):
        return "offer", max(floor, ask0), "opening ask (he spoke first)"
    if soft_floor is not None and bid >= soft_floor and len(hers) >= dparams.static("firm_rounds") and len({h[0] for h in hers[-dparams.static("firm_rounds"):]}) == 1:
        return "accept", bid, f"he repeated {bid} three times and it clears our soft floor {soft_floor}"
    if bid >= floor and (final or bid >= last - tol):          # `final` first: with ours empty `last` is None (he spoke first)
        return "accept", bid, "his final is over our floor" if final else "his bid is within reach of our ask"
    if final and not inferred.get("final_unreliable"):
        return "walk", None, f"his final {bid} is below our floor {floor}"
    if last <= floor:
        stalled = len(hers) >= 2 and hers[-1][0] <= hers[-2][0]
        return ("walk", None, f"at our floor {floor}, he bids {bid}") if stalled else ("wait", None, "at our floor, letting him move")
    gap = last - bid
    k0 = dparams.static("sell_k0") if k0 is None else k0
    k = inferred.get("best_k") or round(k0 * inferred.get("prior_mult", 1.0), 3)
    mood = "firm" if (len(hers) >= 2 and hers[-1][0] <= hers[-2][0]) else "yielding"
    if mood == "firm" and len(hers) >= dparams.static("firm_rounds"):
        k = max(k, dparams.static("firm_k"))
    step = max(1, round(gap * k))
    if hold:
        step = 1                                          # Pilar: she moves +1 a round after holding; we match her pace,
                                                          # the minimum that still counts as a new offer (a repeat earns nothing)
    new = max(floor, last - step)
    if new >= last:
        return "walk", None, "no room left above the floor"
    return "offer", new, f"he is {mood}, gap {gap}, step {step} (k={k})"


def chato_text(side, price, n_round, item, name="Chato"):
    """Short and plain: he talks little, has a long memory and punishes cleverness."""
    if n_round == 0:
        return (f"Buenas, {name}. Te ofrezco {item}: {price} primas." if side == "sell"
                else f"Buenas, {name}. Busco {item}. Te ofrezco {price} primas.")
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


def card_refs(side):
    """Card refs a side asks for: want.types is 'card:REF' on the board; want.cards is how we post a bid."""
    if not isinstance(side, dict):
        return []
    out = []
    for t in side.get("types") or []:
        if isinstance(t, str) and t.startswith("card:"):
            ref = t.split(":", 1)[1]
            if ref and ref not in out:
                out.append(ref)
    for c in side.get("cards") or []:
        if isinstance(c, str) and c:
            ref = c.split(":", 1)[1] if c.startswith("card:") else c
            if ref and ref not in out:
                out.append(ref)
    return out


def offer_fees(offer, default):
    v = offer.get("_venue")
    return v if isinstance(v, dict) else default


def swap_taker_fee(venue):
    """The side that accepts pays. A 1-for-1 swap moves two cards and no cash, so only the per-card fee counts."""
    return int(venue.get("fee_per_card") or 0) * 2


def bid_price(value):
    """Most cash we will offer for a missing card. We pay the cash; the team that accepts pays the venue fee."""
    price = int(value - BUY_MIN_GAIN)
    while price > 0 and (value - price) < max(BUY_MIN_GAIN, 0.1 * price):
        price -= 1
    return max(0, price)


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


def market_opportunities(me, catalog, venue, offers, value_of, free_cash=None):
    """Accept-able offers from others. buy: their ask vs our value of one more copy. sell: their bid vs the copy we
    would lose. swap: their card for one surplus copy of ours. The venue on the offer (else `venue`) is the fee."""
    counts, ids = card_counts(me)
    cash = (me["cash"] - CASH_RESERVE) if free_cash is None else free_cash
    opps = []
    for o in offers:
        if o.get("maker") == me["id"] or o.get("to") not in (None, me["id"]) or o.get("status", "open") != "open":
            continue
        if o.get("thread"):                                 # dealer and team threads stay with that phase
            continue
        g, w = o.get("give") or {}, o.get("want") or {}
        fees = offer_fees(o, venue)
        g_assets, g_cash = g.get("assets") or [], g.get("cash") or 0
        w_cash, w_assets = w.get("cash") or 0, w.get("assets") or []
        refs = card_refs(w)
        if len(g_assets) == 1 and isinstance(g_assets[0], dict) and g_assets[0].get("ref") and not g_cash and w_cash > 0 and not refs:
            ref, ask = g_assets[0]["ref"], w_cash
            cost = ask + fee_of(fees, ask)
            if cost > cash:
                continue
            v = value_of(ref, counts.get(ref, 0))
            if v is None:
                continue
            gain = v - cost
            if gain >= max(BUY_MIN_GAIN, 0.1 * cost):
                opps.append({"kind": "buy", "offer": o["id"], "ref": ref, "price": ask, "cost": cost, "value": round(v, 1),
                             "gain": round(gain, 1), "roi": round(gain / cost, 2)})
        elif g_cash > 0 and not g_assets and len(refs) == 1 and not w_cash and not w_assets:  # they bid for a card
            ref, bid = refs[0], g_cash
            n = counts.get(ref, 0)
            if n < 2:
                continue                                   # never sell our only copy
            lost = marginal_value(ref, n, catalog, me["affinity"])
            net = bid - fee_of(fees, bid)
            gain = net - lost
            if gain >= SELL_MIN_GAIN:
                opps.append({"kind": "sell", "offer": o["id"], "ref": ref, "price": bid, "asset": ids[ref][-1]["id"],
                             "value": round(lost, 1), "gain": round(gain, 1), "roi": round(gain, 2)})   # no cash needed
        elif len(g_assets) == 1 and isinstance(g_assets[0], dict) and g_assets[0].get("ref") and not g_cash \
                and len(refs) == 1 and not w_cash and not w_assets:  # their card for one of ours
            their, ours = g_assets[0]["ref"], refs[0]
            n = counts.get(ours, 0)
            if n < 2 or their == ours:
                continue
            v = value_of(their, counts.get(their, 0))
            if v is None:
                continue
            lost = marginal_value(ours, n, catalog, me["affinity"])
            fee = swap_taker_fee(fees)
            if fee > cash:
                continue
            gain = v - lost - fee
            if gain >= SELL_MIN_GAIN:
                opps.append({"kind": "swap", "offer": o["id"], "ref": their, "give": ours, "price": 0, "cost": fee,
                             "asset": ids[ours][-1]["id"], "value": round(v, 1), "gain": round(gain, 1),
                             "roi": round(gain / fee, 2) if fee else round(gain, 2)})
    return opps


BID_PREMIUM = 1.15        # a team's cash bid is a floor on its value (nobody bids all of it): ask it this much above the bid


def demand_from(offers, me_id, own_ids=()):
    """ref -> (best cash bid, team id or None) from other teams' bids for one card: their proof of what it is worth to them.
    Boards show makers as pseudonyms ("m2eb45963"), so our own offers are excluded by offer id; `to` is only a real team id."""
    best, own = {}, set(own_ids)
    for o in offers:
        g, w = o.get("give") or {}, o.get("want") or {}
        maker = o.get("maker")
        if maker == me_id or o.get("id") in own or o.get("status", "open") != "open":
            continue
        refs = card_refs(w)
        if len(refs) != 1 or not g.get("cash") or g.get("assets") or w.get("cash"):
            continue
        team = maker if isinstance(maker, str) and re.fullmatch(r"t\d+", maker) else None
        if refs[0] not in best or g["cash"] > best[refs[0]][0]:
            best[refs[0]] = (g["cash"], team)
    return best


def listing_plan(me, catalog, venue, offers, listed_assets, own_ids=()):
    """[(asset id, ask, ref, value lost, to)] for surplus copies. Floor = OUR value of the copy + margin.
    With a team bidding for the card: a directed ask to that team, BID_PREMIUM above its bid (its value is higher than
    what it bid). Otherwise: undercut the cheapest rival ask, or book x BUYER_MULT when alone."""
    counts, ids = card_counts(me)
    demand = demand_from(offers, me["id"], own_ids)
    comp = {}
    for o in offers:
        g, w = o.get("give") or {}, o.get("want") or {}
        assets = g.get("assets") or []
        if o.get("maker") != me["id"] and len(assets) == 1 and isinstance(assets[0], dict) and assets[0].get("ref") \
                and w.get("cash") and o.get("status", "open") == "open":
            comp.setdefault(assets[0]["ref"], []).append(w["cash"])
    plan = []
    for ref, n in counts.items():
        if n < 2:
            continue
        card = next(c for s in catalog["sets"] for c in s["cards"] if c["id"] == ref)
        lost = marginal_value(ref, n, catalog, me["affinity"])
        floor = ask_floor(lost)
        # A team missing this card values it at book x its own set multiplier (the same six, shuffled: 1.6 ... 0.5) plus any
        # page bonus. Alone on the board we price for the upper half of those buyers; with rivals we undercut the cheapest.
        target = (min(comp[ref]) - 1) if ref in comp else math.ceil(card["book"] * BUYER_MULT)
        ask = max(floor, min(target, math.ceil(card["book"] * 1.5)))
        to = None
        if ref in demand:                                                  # a bidder told us a floor on its value
            bid, to = demand[ref]
            ask = max(floor, bid + 1, math.ceil(bid * BID_PREMIUM))
        spare = sorted(ids[ref], key=lambda a: -a["serial"])[:n - 1]       # keep the lowest serial
        for i, a in enumerate(spare):
            if a["id"] not in listed_assets:
                plan.append((a["id"], ask, ref, round(lost, 1), to if i == 0 else None))
    return plan


def holders_from(offers, me_id):
    """ref -> team id, from cards someone is offering. Only used to address a bid with to=."""
    found = {}
    for o in offers:
        maker = o.get("maker")
        if not isinstance(maker, str) or maker == me_id or not maker.startswith("t") or o.get("status", "open") != "open":
            continue
        for a in (o.get("give") or {}).get("assets") or []:
            if isinstance(a, dict) and a.get("ref") and a["ref"] not in found:
                found[a["ref"]] = maker
    return found


def bid_plan(me, catalog, open_wants, free_cash, holders, limit):
    """[(ref, price, value, to or None)] for missing cards. Price leaves BUY_MIN_GAIN; `to` only when we saw a holder."""
    counts, _ = card_counts(me)
    cands = []
    for s in catalog.get("sets", []):
        if not s.get("released", True):
            continue
        for c in s["cards"]:
            ref = c["id"]
            if counts.get(ref, 0) or ref in open_wants:
                continue
            v = marginal_value(ref, 0, catalog, me["affinity"])
            price = bid_price(v)
            if price >= 1 and price <= free_cash:
                cands.append((v - price, ref, price, round(v, 1), holders.get(ref)))
    cands.sort(key=lambda row: -row[0])
    plan, cash = [], free_cash
    for _gain, ref, price, v, holder in cands:
        if len(plan) >= limit or price > cash:
            continue
        plan.append((ref, price, v, holder))
        cash -= price
    return plan


def swap_plan(me, catalog, listed, skip_wants, holders, limit):
    """[(asset, give_ref, want_ref, to or None)] surplus we don't need for a card we are missing."""
    counts, ids = card_counts(me)
    spares = []
    for ref, n in counts.items():
        if n < 2:
            continue
        lost = marginal_value(ref, n, catalog, me["affinity"])
        for a in sorted(ids[ref], key=lambda a: -a["serial"])[:n - 1]:
            if a["id"] not in listed:
                spares.append((lost, a["id"], ref))
    spares.sort()
    wants = []
    for s in catalog.get("sets", []):
        if not s.get("released", True):
            continue
        for c in s["cards"]:
            ref = c["id"]
            if counts.get(ref, 0) or ref in skip_wants:
                continue
            wants.append((marginal_value(ref, 0, catalog, me["affinity"]), ref))
    wants.sort(key=lambda row: -row[0])
    plan, used = [], set()
    for v, wref in wants:
        if len(plan) >= limit:
            break
        for lost, aid, gref in spares:
            if aid in used or gref == wref:
                continue
            if v - lost >= SELL_MIN_GAIN:
                plan.append((aid, gref, wref, holders.get(wref)))
                used.add(aid)
                break
    return plan


# ---------------------------------------------------------------- runtime

def venue_table(rows):
    table = {}
    for v in rows or []:
        vid = v.get("venue") or v.get("id")
        if vid:
            table[vid] = v
    return table


def fees_for(vid, table):
    """Fees from the live venue list. Fallbacks match the day-2 announcement and the boards as published."""
    v = table.get(vid)
    if isinstance(v, dict) and v.get("fee_bps") is not None:
        return v
    if vid == DUENDE:
        return {"venue": DUENDE, "fee_bps": 0, "fee_per_card": 0}
    if vid == TRECE:
        return {"venue": TRECE, "fee_bps": 100, "fee_per_card": 0}
    if vid == FAIR:
        return {"venue": FAIR, "fee_bps": 0, "fee_per_card": 0}
    return {"venue": vid or "rastro", "fee_bps": 500, "fee_per_card": 1}


def boards_to_scan(table, me_id):
    """Every market we can trade on, plus v02, v03 and FAIR even if the venue list is stale. Never our own stall."""
    scan = [vid for vid, v in table.items() if v.get("owner") != me_id]
    for vid in (DUENDE, TRECE, FAIR, "rastro"):
        if vid not in scan and table.get(vid, {}).get("owner") != me_id:
            scan.append(vid)
    return scan


def _has_cash(side):
    return bool((side or {}).get("cash"))


def place_board(b, give, want, to=None):
    """Cash bids and asks go to FAIR (v21, Team 9). Its broker matches a bid above an ask at the midpoint, any copy, every tick.
    Swaps stay on El Duende: that match is bid against ask, not card for card. One venue only — a refusal falls through,
    the same offer is never posted on two boards."""
    order = (FAIR, DUENDE, "rastro") if (_has_cash(give) or _has_cash(want)) else (DUENDE, "rastro")
    last = None
    for vid in order:
        try:
            b.list_offer(give, want, venue=vid, to=to, expires_in_ticks=BOARD_TTL)
            return vid
        except BazaarError as e:
            last = e
            if e.code not in ("self_venue", "venue_not_live"):
                raise
            log(f"{vid} post refused ({e.code})")
    raise last


def phase_market(b, me, catalog, can_accept):
    try:
        table = venue_table(b.venues().get("venues", []))
        mine = b.my_offers().get("offers", [])
    except BazaarError as e:
        log("market unreadable:", e)
        return False
    offers, seen = [], set()
    for vid in boards_to_scan(table, me["id"]):
        try:
            board = b.board(vid).get("offers", [])
        except BazaarError as e:
            log("board", vid, e.code)
            continue
        fees = fees_for(vid, table)
        for o in board:
            if o.get("id") in seen:
                continue
            seen.add(o.get("id"))
            stamped = dict(o)
            stamped["_venue"] = fees
            offers.append(stamped)
    for o in mine:
        if o.get("to") != me["id"] or o.get("id") in seen:
            continue
        seen.add(o.get("id"))
        stamped = dict(o)
        stamped["_venue"] = fees_for(o.get("venue") or "rastro", table)
        offers.append(stamped)
    listed, open_wants, committed, open_n = set(), set(), 0, 0
    for o in mine:
        if o.get("maker") != me["id"] or o.get("status", "open") != "open":
            continue
        open_n += 1
        committed += int((o.get("give") or {}).get("cash") or 0)
        for a in (o.get("give") or {}).get("assets") or []:
            if isinstance(a, dict) and a.get("id") is not None:
                listed.add(a["id"])
        open_wants.update(card_refs(o.get("want") or {}))
    rastro = fees_for("rastro", table)
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

    free = max(0, me["cash"] - CASH_RESERVE - committed)
    accepted, best = False, None
    opps = market_opportunities(me, catalog, rastro, offers, value_of, free_cash=free)
    if opps:
        log("market opportunities:", json.dumps(sorted(opps, key=lambda o: -o["gain"])[:5]))
    if can_accept:
        best = rank_pick(opps)
        if best and not try_reserve(me["tick"]):
            log("market accept skipped: another process already used this tick's accept")
            best = None
        if best:
            try:
                b.accept(best["offer"], assets=[best["asset"]] if best["kind"] in ("sell", "swap") else None)
                accepted = True
                log(f"MARKET {best['kind'].upper()} {best['ref']} at {best['price']}: value {best['value']} gain {best['gain']} roi {best['roi']}")
            except BazaarError as e:
                log("market accept refused:", e.code, e.message)
                best = None
    if accepted and best.get("asset"):
        listed.add(best["asset"])
    if accepted and best["kind"] == "buy":
        free = max(0, free - best["cost"])
    got = {best["ref"]} if accepted else set()
    holders = holders_from(offers, me["id"])
    # One spare becomes a swap when we are missing something; the other spares stay cash asks.
    raw_swaps = swap_plan(me, catalog, listed, open_wants | got, holders, 1)
    swap_assets = {row[0] for row in raw_swaps}
    asks = [("ask", asset, ask, ref, lost, to) for asset, ask, ref, lost, to in listing_plan(me, catalog, rastro, offers, listed,
                                                                               {o.get("id") for o in mine})
            if asset not in swap_assets]
    bids = [("bid", ref, price, v, holder) for ref, price, v, holder in
            bid_plan(me, catalog, open_wants | got | {row[2] for row in raw_swaps}, free, holders, LISTINGS_PER_TICK)]
    swaps = [("swap", asset, gref, wref, holder) for asset, gref, wref, holder in raw_swaps]
    queues, posted = [asks, bids, swaps], 0
    while posted < LISTINGS_PER_TICK and open_n + posted < 28 and any(queues):
        for q in queues:
            if not q or posted >= LISTINGS_PER_TICK or open_n + posted >= 28:
                continue
            item = q.pop(0)
            try:
                if item[0] == "ask":
                    _, asset, ask, ref, lost, to = item
                    where = place_board(b, {"assets": [asset]}, {"cash": ask}, to=to)
                    log(f"LISTED spare {ref} (asset {asset}) at {ask} on {where}" + (f" to bidder {to}" if to else "")
                        + f"; the copy is worth {lost} to us")
                elif item[0] == "bid":
                    _, ref, price, v, holder = item
                    where = place_board(b, {"cash": price}, {"cards": [ref]}, to=holder)
                    log(f"BID {price} for {ref} on {where}" + (f" to {holder}" if holder else "") + f"; worth {v} to us")
                else:
                    _, asset, gref, wref, holder = item
                    where = place_board(b, {"assets": [asset]}, {"cards": [wref]}, to=holder)
                    log(f"SWAP asset {asset} ({gref}) for {wref} on {where}" + (f" to {holder}" if holder else ""))
                posted += 1
            except BazaarError as e:
                log("listing refused:", e.code, e.message)
                return accepted
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


PACK_EDGE = dparams.SPEC["pack_edge"][0]          # compatibility: the cap reads dparams.static("pack_edge")


def pack_cap(me, catalog):
    return max(0, min(me["cash"] - CASH_RESERVE, int(dparams.static("pack_edge") * pack_private_value(me, catalog))))


def _key(cash=0, assets=(), types=()):
    """Comparable form of one side (give/want) of an offer: (cash, asset ids, types)."""
    return (cash, tuple(sorted(str(a) for a in assets)), tuple(sorted(types)))


def _side_key(side):
    """_key() of a real offer side, or None when it has keys we have not observed (unknown shape: never accept)."""
    if not isinstance(side, dict) or set(side) - {"cash", "assets", "types"}:
        return None
    return _key(side.get("cash", 0), [a.get("id") for a in side.get("assets") or []], side.get("types") or [])


def fresh_accept_target(b, tid, dealer, me_id, expect_give, expect_want, labels):
    """Hotfix 1.3. Right before an accept, re-read the thread ONCE and find the standing offer we decided on.
    expect_give / expect_want: _key() of what the dealer gives / wants in the offer the strategy evaluated.
    Returns (offer_id, None) only if exactly ONE open offer of `dealer` has exactly that structure; otherwise (None, reason).
    Fail closed: a failed read, a closed thread, no match or several matches all mean NO accept. One extra GET, no retry."""
    try:
        t = b.thread(tid)
    except BazaarError as e:
        return None, f"fresh read failed ({e.code})"
    if t.get("status") != "open":
        return None, f"thread is {t.get('status')}"
    mine = [o for o in t.get("standing_offers") or []
            if o.get("maker") == dealer and o.get("status") == "open" and o.get("to") in (None, me_id)]
    if not mine:
        return None, "no open standing offer"
    hits, why = [], []
    for o in mine:
        g, w = _side_key(o.get("give")), _side_key(o.get("want"))
        if g is None or w is None:
            why.append("unknown offer shape")
        elif g != expect_give:
            why.append(f"{labels[0]} mismatch")
        elif w != expect_want:
            why.append(f"{labels[1]} mismatch")
        else:
            hits.append(o)
    if len(hits) == 1:
        return hits[0]["id"], None
    return None, (f"{len(hits)} standing offers match the evaluated one" if hits else "; ".join(why))


def open_packs(b, me):
    """Open every sealed pack we hold (gifts, grants, purchases). Runs every tick, whatever any dealer phase is doing."""
    opened = False
    for a in me["assets"]:
        if a["kind"] == "pack":
            try:
                cards = b.open_pack(a["id"])["cards"]
                log("opened pack:", [c.get("ref") or c.get("id") for c in cards])
                opened = True
            except BazaarError as e:
                log("open_pack:", e)
    return opened


def phase_abuela(b, me, catalog, mem):
    """One step of the Abuela negotiation. Returns True if we accepted something this tick."""
    if mem.get("active_buy_abuela"):
        return False                                   # a card buy holds her one conversation: leave it to phase_card_buy
    list_pack = menu_list_price(mem, "abuela", "pack:sobre_barrio", dparams.static("fallback_list"))     # the dealer's own list price
    inferred = mem["inferred"] = learned(mem, "abuela", list_pack)
    log_param_changes(mem, "abuela", list_pack)
    act = mem["active"]
    tid = next((t for t in me.get("open_threads", []) if b.thread(t).get("with") == "abuela"), None)
    if act and (tid is None or act["thread"] != tid):                   # our negotiation ended: record the outcome
        t = b.thread(act["thread"])
        outcome = t["status"]
        paid = deal_price(t, me["id"]) if outcome == "deal" else None
        ours, hers = read_thread(t, me["id"])
        rec = {"dealer": "abuela", "thread": act["thread"], "topic": act["topic"], "outcome": outcome, "closed_reason": t.get("closed_reason"),
               "paid": paid, "rounds": rounds_of(ours, hers)}
        mem["observed"]["negotiations"].append(rec)
        mem["active"], act = None, None
        if paid:
            mem["abuela_min_price"] = min(mem["abuela_min_price"], paid)
        log(f"negotiation {rec['thread']} ended: {outcome} {rec['closed_reason'] or ''} paid {paid}")
        log_chat(f"🏁 {rec['topic']}: {outcome} {rec['closed_reason'] or ''} pagado {paid}")
    cap = pack_cap(me, catalog)
    if tid is None:
        best_ever = min(mem.get("abuela_min_price", 999), dparams.static("reach_cap"))         # the lowest she ever sold a pack to us (capped)
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
    action, price, why = next_offer(cap, list_pack, ours, hers, inferred)
    log(f"Abuela thread {tid}: ours {ours} hers {[h[0] for h in hers]} mood {tone(ours, hers)} cap {cap} -> {action} {price} ({why})")
    if hers:
        log_chat(f"👵 Abuela pide: {hers[-1][0]} P{' (final)' if hers[-1][1] else ''}")
    if action == "accept":
        buy = (mem["active"]["topic"] or {}).get("buy") or {}
        item = [f"{k}:{buy[k]}" for k in ("pack", "card") if k in buy]
        if len(buy) != 1 or len(item) != 1 or price is None or price > cap:
            log(f"Abuela thread {tid}: accept blocked: topic or price not verifiable (topic {mem['active']['topic']}, price {price}, cap {cap})")
            return False
        offer_id, why = fresh_accept_target(b, tid, "abuela", me["id"], _key(types=item), _key(cash=price), ("item", "price"))
        if offer_id is None:
            log(f"Abuela thread {tid}: accept blocked: {why} (decided {price})")
            return why == "no open standing offer"          # legacy: nothing open = probably already accepted, settles next tick
        if not try_reserve(me["tick"]):
            log(f"Abuela thread {tid}: accept skipped: another process already used this tick's accept")
            return False
        b.accept(offer_id)
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


def menu_rarity_prices(mem, dealer):
    """{rarity: list price} from the dealer's published menu (empty until refresh_traits has run)."""
    return {k.split(":", 1)[1]: v for k, v in ((mem.get("dealer_menu") or {}).get(dealer) or {}).items() if k.startswith("rarity:")}


def chato_candidate(me, catalog, menu=None):
    """The card to offer him: worth little to us (we ask 1.4x our value + margin) and realistic for him (<= 85% of his list)."""
    counts, ids = card_counts(me)
    best = None
    for ref, n in counts.items():
        a = ids[ref][-1]
        lp = (menu or {}).get(a["rarity"]) or CHATO.get(a["rarity"])      # published menu first, the old table as a fallback
        if not lp or n < 2:                               # never our only copy: the page bonus is not in its value
            continue
        lost = marginal_value(ref, n, catalog, me["affinity"])
        floor = math.ceil(lost * dparams.static("floor_mult") + dparams.static("floor_add"))
        if floor <= dparams.static("cand_floor_share") * lp:
            score = dparams.static("cand_score_share") * lp - lost
            if best is None or score > best[0]:
                best = (score, ref, a, floor, lp, lost)
    return best


def learned_block(mem, dealer):
    """Ticks to leave a dealer alone after a conversation that ended without a deal (learned: more after cooloffs)."""
    return learned_params_for(mem, dealer)["block_after_fail"]


def learned_params_for(mem, key):
    return dparams.learned_params(mem, key)


def phase_chato(b, me, catalog, mem, can_accept, dealer="chato", candidate_fn=None):
    """One step with a card buyer (El Chato by default, Doña Pilar via phase_pilar): sell him a card worth little to us.
    Returns True if we accepted something. Memory keys are per dealer (chato_*, pilar_*)."""
    candidate_fn = candidate_fn or chato_candidate
    name = DEALER_NAMES.get(dealer, dealer.title())
    if dealer not in me.get("unlocked", []):
        return False
    if mem.get(f"{dealer}_block_until", -1) > me["tick"]:
        return False
    inferred = learned(mem, dealer)
    log_param_changes(mem, dealer)
    act = mem.get(f"active_{dealer}")
    tid = next((t for t in me.get("open_threads", []) if b.thread(t).get("with") == dealer), None)
    if act and (tid is None or act["thread"] != tid):
        t = b.thread(act["thread"])
        ours, hers = read_thread(t, me["id"], dealer, sell=True)
        got = deal_price(t, me["id"]) if t["status"] == "deal" else None
        mem["observed"]["negotiations"].append({"dealer": dealer, "thread": act["thread"], "topic": act["topic"], "outcome": t["status"],
                                                "closed_reason": t.get("closed_reason"), "paid": None, "received": got,
                                                "rounds": rounds_of(ours, hers)})
        reason = t.get("closed_reason")
        log(f"{name} negotiation {t['id']} ended: {t['status']} {reason or ''} received {got}")
        if t["status"] == "deal":
            ladder_record(mem, dealer, t["id"], act.get("rarity"), act["ref"].split("-")[0], act["ref"], hers, got, False)
        if t["status"] != "deal":
            mem[f"{dealer}_block_until"] = me["tick"] + learned_block(mem, dealer)            # any ending but a deal: do not pester him, he remembers
            mem.setdefault(f"{dealer}_tried", {})[act["ref"]] = me["tick"]
        mem[f"active_{dealer}"], act = None, None
    if tid is None:
        on_tables = {aid for d in ("chato", "pilar", "picaros", "banco") if d != dealer        # a card is on ONE dealer's table at a time
                     for aid in ((mem.get(f"active_{d}") or {}).get("topic") or {}).get("sell", {}).get("assets", [])}
        cand = candidate_fn(dict(me, assets=[a for a in me["assets"] if a.get("id") not in on_tables]), catalog,
                            menu_rarity_prices(mem, dealer))
        if not cand:
            return False
        _, ref, asset, floor, lp, lost = cand
        server_value = float(asset.get("your_value") or 0)        # has the page bonus our marginal_value lacks
        if server_value > lost:                                   # a dealer-deal loss counts in full (Payday deck)
            lost, floor = server_value, max(floor, math.ceil(server_value) + 1)
            if floor > dparams.static("cand_floor_share") * lp and floor > lp:
                mem.setdefault(f"{dealer}_tried", {})[ref] = me["tick"]
                log(f"{name}: not offering {ref}: it is worth {server_value:.0f} to us, over what the dealer pays ({lp})")
                return False
        if me["tick"] - mem.get(f"{dealer}_tried", {}).get(ref, -999) < dparams.static("retry_same_card"):
            return False                                          # same card, no new reason: stay quiet for a while
        fh = feed_intel.hints(mem, dealer, "buys", asset.get("rarity"), ref.split("-")[0], ref)
        if feed_intel.sell_is_futile(fh, floor):                  # it never paid that much to any team: do not ask
            mem.setdefault(f"{dealer}_tried", {})[ref] = me["tick"]
            log(f"{name}: skip selling {ref}: our floor {floor} > most it paid any team for a {asset.get('rarity')} "
                f"({fh['deal_max']:.0f}, {fh['n_deals']} deals, by {fh['level']})")
            return False
        if fh:
            lp = max(lp, math.ceil(fh["deal_max"]))               # open at no less than what it paid other teams
        try:                                                      # a market listing of this copy would sell it under the dealer
            for o in b.my_offers().get("offers", []):
                if o.get("maker") == me["id"] and o.get("status", "open") == "open" and \
                        any(isinstance(a, dict) and a.get("id") == asset["id"] for a in (o.get("give") or {}).get("assets") or []):
                    b.cancel(o["id"])
                    log(f"{name}: cancelled listing {o['id']} of {ref} before offering it")
        except BazaarError as e:
            log(f"{name}: could not clear listings of {ref}: {e.code}")
            return False
        try:
            th = b.open_thread(dealer, topic={"sell": {"assets": [asset["id"]]}})
        except BazaarError as e:
            log(f"{name} unavailable:", e.code, e.message)
            mem[f"{dealer}_block_until"] = me["tick"] + learned_block(mem, dealer)
            return False
        tid = th["id"]
        mem[f"active_{dealer}"] = {"thread": tid, "topic": {"sell": {"assets": [asset["id"]]}}, "cash_start": me["cash"],
                               "ref": ref, "floor": floor, "lp": lp, "lost": round(lost, 1), "rarity": asset.get("rarity")}
        log(f"{name}: offering {ref} (worth {lost:.1f} to us), floor {floor}, his list {lp}")
    elif mem.get(f"active_{dealer}") is None:
        return False                                              # a thread we did not open: leave it alone
    act = mem[f"active_{dealer}"]
    t = b.thread(tid)
    if t["status"] != "open":
        return False
    ours, hers = read_thread(t, me["id"], dealer, sell=True)
    soft = None if (mem.get(f"{dealer}_soft_done") or dealer == "banco") else math.ceil(act["lost"] + dparams.static("soft_add"))     # one firm-bid deal per dealer, for the ladder
    fh = range_hints(mem, dealer, "buys", act.get("rarity"), act["ref"].split("-")[0], act["ref"])
    floor = sell_floor_with_ladder(mem, me, dealer, act.get("rarity"), fh, act["floor"], hers, act["ref"])
    sold = ((act.get("topic") or {}).get("sell") or {}).get("assets") or []
    now_value = max([float(a.get("your_value") or 0) for a in me["assets"] if a.get("id") in sold] or [0.0])
    floor = max(floor, math.ceil(now_value) + 1) if now_value else floor   # never under the server's value, every round
    action, price, why = next_offer_sell(floor, act["lp"], ours, hers, inferred, soft_floor=soft, hold=(dealer in ("pilar", "banco")))
    log(f"{name} thread {tid}: ours {ours} his {[h[0] for h in hers]} floor {floor} -> {action} {price} ({why})")
    if action == "accept" and can_accept:
        assets = ((act["topic"] or {}).get("sell") or {}).get("assets") or []
        least = floor if soft is None else min(floor, soft)     # the soft floor may close one deal under the floor
        if len(assets) != 1 or price is None or price < least:
            log(f"{name} thread {tid}: accept blocked: topic or price not verifiable (topic {act['topic']}, price {price}, floor {least})")
            return False
        offer_id, why = fresh_accept_target(b, tid, dealer, me["id"], _key(cash=price), _key(assets=assets), ("price", "item"))
        if offer_id is None:
            log(f"{name} thread {tid}: accept blocked: {why} (decided {price})")
            return why == "no open standing offer"
        if not try_reserve(me["tick"]):
            log(f"{name} thread {tid}: accept skipped: another process already used this tick's accept")
            return False
        try:
            b.accept(offer_id)
        except BazaarError as e:
            if e.code in ("asset_gone", "not_owner", "asset_locked"):     # the card left (sold elsewhere): end cleanly
                log(f"{name}: {act['ref']} is no longer ours ({e.code}); closing thread {tid}")
                b.close_thread(tid)
                mem[f"active_{dealer}"] = None
                return False
            raise
        if price < act["floor"]:
            mem[f"{dealer}_soft_done"] = True
        log(f"{name.upper()} SELL {act['ref']} at {price}: it was worth {act['lost']} to us")
        return True
    if action == "walk":
        b.close_thread(tid)
    elif action == "offer":
        b.say(tid, chato_text("sell", price, len(ours), act["ref"], name), price=price)
    return False


def pilar_candidate(me, catalog, menu=None):
    """Doña Pilar buys uncommon, rare and epic cards over book (SAL and RET most). Her prices are not published:
    we assume her list at book (x1.1 for SAL/RET) and let next_offer_sell protect the floor."""
    counts, ids = card_counts(me)
    book = {c["id"]: c for st in catalog["sets"] for c in st["cards"]}
    best = None
    for ref, n in counts.items():
        a = ids[ref][-1]
        if a["rarity"] not in ("uncommon", "rare", "epic") or ref not in book or n < 2:   # never our only copy
            continue
        lp = int(book[ref]["book"] * dparams.static("pilar_list_mult") * (dparams.static("pilar_focus_mult") if ref.split("-")[0] in ("SAL", "RET") else 1.0))
        lost = marginal_value(ref, n, catalog, me["affinity"])
        floor = math.ceil(lost * dparams.static("floor_mult") + dparams.static("floor_add"))
        if floor <= dparams.static("cand_floor_share") * lp:
            score = dparams.static("cand_score_share") * lp - lost
            if best is None or score > best[0]:
                best = (score, ref, a, floor, lp, lost)
    return best


def phase_pilar(b, me, catalog, mem, can_accept):
    return phase_chato(b, me, catalog, mem, can_accept, dealer="pilar", candidate_fn=pilar_candidate)


def picaros_candidate(me, catalog, menu=None):
    """Los Pícaros buy commons and uncommons of released sets: the outlet our spare commons lack (the boards are flooded).
    Spares only (never our only copy); their price list is not published, so we ask book and let the floor protect us."""
    counts, ids = card_counts(me)
    book = {c["id"]: c for st in catalog["sets"] for c in st["cards"]}
    best = None
    for ref, n in counts.items():
        a = ids[ref][-1]
        if a["rarity"] != "common" or ref not in book or n < 2:     # feed: they pay 10-11 for uncommons, Pilar 16-23
            continue
        lp = int((menu or {}).get(a["rarity"]) or book[ref]["book"])           # published menu price if any, else book
        lost = marginal_value(ref, n, catalog, me["affinity"])
        floor = math.ceil(lost * 1.4 + SELL_MIN_GAIN)
        if floor <= 0.85 * lp:
            score = 0.8 * lp - lost
            if best is None or score > best[0]:
                best = (score, ref, a, floor, lp, lost)
    return best


# ---------------------------------------------------------------- the epic loop (Don Ernesto's ladder level)
#
# STRATEGY NOTE, Sat 2026-10-03. OFF BY DEFAULT since ~21:30. REVIEW BEFORE TURNING IT BACK ON.
# Turned on at ~20:45 after the 400 P payday on a wrong premise ("cards never score, only the ladder"). The organisers'
# Payday deck says a dealer deal scores value added - price paid + price received, a loss counts in full, and selling
# below value costs: SAL-11 bought at 143 (worth 198 to us) and sold to Ernesto at 120 cost ~-78. Ernesto pays ~113-120
# for epics our value puts at 160-200, so he is never a buyer for them (banco_candidate now demands our value).
# What did work: RET-11 bought from Los Pícaros at 137 (worth 162) and sold by page_hunter to a team at 216 (team-trade
# gains count up to 50). If turned back on, buys are capped under our value and the epic waits for a team buyer.
# Sunday 09:00 (deck): +150 P and a new round; Ernesto's vault legendary (~470) is worth buying only under our value
# (LAV-12 was 720 to us on Saturday).
EPIC_LOOP = os.environ.get("EPIC_LOOP", "0") == "1"
EPIC_LOOP_RESERVE = int(os.environ.get("EPIC_LOOP_RESERVE", "250"))   # primas kept for Sunday's round
EPIC_LOOP_CAP = int(os.environ.get("EPIC_LOOP_CAP", "150"))           # most we pay Los Pícaros for an epic (deals 128-147)
EPIC_LOOP_FULL = 0.9                                                  # Ernesto's worst-of-best-three share that ends the loop
EPIC_REFS = ("SAL-11", "RET-11", "LAV-11", "LAT-11", "MAL-11")        # SAL/RET first: Pilar is a fallback buyer for them
EPIC_LIST = 162                                                       # Los Pícaros' epic list price
BANCO_FLOOR, BANCO_ASK = 114, 132     # never at his opening 113 (it would not count); open high, then one prima a round
DEALER_NAMES = {"banco": "Don Ernesto", "picaros": "Pícaros"}


def epic_loop_held(me, mem):
    """Epics the loop bought that we still hold (asset dicts)."""
    ids = set((mem.get("epic_loop") or {}).get("held", []))
    return [a for a in me["assets"] if a.get("id") in ids and a.get("kind") == "card"]


def epic_loop_wants_one(me, mem):
    """(True, why) when the loop should buy another epic now."""
    rnd = ladder_round(mem)
    if not EPIC_LOOP or rnd is None:
        return False, "off"
    if ladder.worst_of_best3(mem, rnd, "banco") >= EPIC_LOOP_FULL:
        return False, "Ernesto's level is full this round"
    if epic_loop_held(me, mem):
        return False, "an epic is already waiting for Ernesto"
    if me["cash"] - EPIC_LOOP_CAP < EPIC_LOOP_RESERVE:
        return False, f"cash {me['cash']} would fall under the reserve {EPIC_LOOP_RESERVE}"
    return True, "level 5 has room"


def phase_epic_buy(b, me, catalog, mem, can_accept):
    """Buy one epic from Los Pícaros for the loop. Their talk is shared with the page-card buys and sales: one thread at a
    time. Every accept is re-checked against the exact card (they switch cards). Returns (accepted, busy)."""
    dealer, key = "picaros", "active_epic_buy"
    if dealer not in me.get("unlocked", []):
        return False, False
    act = mem.get(key)
    tid = next((t for t in me.get("open_threads", []) if b.thread(t).get("with") == dealer), None)
    loop = mem.setdefault("epic_loop", {"held": [], "tried": {}})
    if act and (tid is None or act["thread"] != tid):                     # our buy ended: record it
        t = b.thread(act["thread"])
        ours, hers = read_thread(t, me["id"], dealer)
        paid = deal_price(t, me["id"]) if t["status"] == "deal" else None
        log(f"epic loop: Pícaros buy {t['id']} ({act['ref']}) ended: {t['status']} {t.get('closed_reason') or ''} paid {paid}")
        if t["status"] == "deal":
            ladder_record(mem, dealer, t["id"], "epic", act["ref"].split("-")[0], act["ref"], hers, paid, True)
            got = [a for a in me["assets"] if a.get("ref") == act["ref"] and a.get("id") not in loop["held"]]
            if got:
                loop["held"].append(max(got, key=lambda a: a["id"])["id"])
            loop["pending_ref"] = act["ref"] if not got else None          # settles next tick: pick it up then
        else:
            loop["tried"][act["ref"]] = me["tick"]
        mem[key], act = None, None
    if loop.get("pending_ref"):                                           # the bought epic arrived after settlement
        got = [a for a in me["assets"] if a.get("ref") == loop["pending_ref"] and a.get("id") not in loop["held"]]
        if got:
            loop["held"].append(max(got, key=lambda a: a["id"])["id"])
            loop["pending_ref"] = None
    if tid is not None and act is None:
        return False, True
    if tid is None:
        want, why = epic_loop_wants_one(me, mem)
        if not want or loop.get("pending_ref"):
            return False, False
        ref = next((r for r in EPIC_REFS if me["tick"] - loop["tried"].get(r, -999) >= dparams.static("buy_retry")), None)
        if ref is None:
            return False, False
        try:
            th = b.open_thread(dealer, topic={"buy": {"card": ref}})
        except BazaarError as e:
            log(f"epic loop: Pícaros unavailable: {e.code} {e.message}")
            loop["tried"][ref] = me["tick"]
            return False, False
        tid = th["id"]
        act = mem[key] = {"thread": tid, "ref": ref}
        log(f"epic loop: buying {ref} from Los Pícaros for Don Ernesto, cap {EPIC_LOOP_CAP} ({why})")
    t = b.thread(tid)
    if t["status"] != "open":
        return False, True
    try:                                                  # never pay our value or more (a dealer-deal loss counts in full)
        value = float(b.value(act["ref"]).get("your_value") or 0)
    except BazaarError:
        value = 0.0
    cap = min(EPIC_LOOP_CAP, me["cash"] - EPIC_LOOP_RESERVE, math.floor(value) - 1)
    ours, hers = read_thread(t, me["id"], dealer)
    action, price, why = next_offer(cap, EPIC_LIST, ours, hers, {})        # no learned profile: theirs is from rares
    log(f"epic loop: Pícaros {tid} {act['ref']}: ours {ours} theirs {[h[0] for h in hers]} cap {cap} -> {action} {price} ({why})")
    if action == "accept" and can_accept:
        if price is None or price > cap:
            return False, True
        offer_id, why = fresh_accept_target(b, tid, dealer, me["id"], _key(types=[f"card:{act['ref']}"]), _key(cash=price),
                                            ("item", "price"))
        if offer_id is None:
            log(f"epic loop: accept blocked: {why} (decided {price})")
            return False, True
        if not try_reserve(me["tick"]):
            return False, True
        b.accept(offer_id)
        log(f"EPIC LOOP BUY {act['ref']} at {price} from Los Pícaros")
        return True, True
    if action == "walk":
        b.close_thread(tid)
    elif action == "offer":
        b.say(tid, chato_text("buy", price, len(ours), act["ref"], "amigos"), price=price)
    return False, True


# Don Ernesto's vault: a legendary for a patient negotiator (Payday deck: ~470; list 585). Bought only under our value,
# so the deal has no loss, and it is a level-5 ladder deal (the heaviest level). One legendary per team per hour.
VAULT = os.environ.get("VAULT", "1") == "1"
VAULT_LIST = 585
VAULT_MIN_EDGE = 30            # buy a legendary only if our value is at least this much over the price we may pay
VAULT_REFS = ("LAV-12", "SAL-12", "RET-12", "LAT-12", "MAL-12")


def phase_vault(b, me, catalog, mem, can_accept):
    """Buy the legendary worth most to us from Don Ernesto, haggling patiently, never at or over our value."""
    dealer, key = "banco", "active_vault"
    if not VAULT or dealer not in me.get("unlocked", []) or ladder_round(mem) is None:
        return False
    act = mem.get(key)
    tid = next((t for t in me.get("open_threads", []) if b.thread(t).get("with") == dealer), None)
    if act and (tid is None or act["thread"] != tid):
        t = b.thread(act["thread"])
        ours, hers = read_thread(t, me["id"], dealer)
        paid = deal_price(t, me["id"]) if t["status"] == "deal" else None
        log(f"vault: Don Ernesto {t['id']} ({act['ref']}) ended: {t['status']} {t.get('closed_reason') or ''} paid {paid} "
            f"(worth {act['value']:.0f} to us)")
        if t["status"] == "deal":
            ladder_record(mem, dealer, t["id"], "legendary", act["ref"].split("-")[0], act["ref"], hers, paid, True)
        mem["vault_block_until"] = me["tick"] + (120 if t["status"] == "deal" else 30)   # one an hour; else let him rest
        mem[key], act = None, None
    if tid is not None and act is None:
        return False                                              # another talk with him is open
    if tid is None:
        if mem.get("vault_block_until", -1) > me["tick"]:
            return False
        owned = {a.get("ref") for a in me["assets"]}
        best = None
        for ref in VAULT_REFS:
            if ref in owned:
                continue
            try:
                v = float(b.value(ref).get("your_value") or 0)
            except BazaarError:
                continue
            cap = min(me["cash"] - CASH_RESERVE, math.floor(v) - VAULT_MIN_EDGE)
            if cap >= 470 and (best is None or v > best[1]):     # ~470 is what a patient negotiator pays (deck)
                best = (ref, v)
        if not best:
            return False
        ref, v = best
        try:
            th = b.open_thread(dealer, topic={"buy": {"card": ref}})
        except BazaarError as e:
            log(f"vault: Don Ernesto unavailable: {e.code} {e.message}")
            mem["vault_block_until"] = me["tick"] + 30
            return False
        tid = th["id"]
        act = mem[key] = {"thread": tid, "ref": ref, "value": v}
        log(f"vault: buying {ref} from Don Ernesto, worth {v:.0f} to us")
    t = b.thread(tid)
    if t["status"] != "open":
        return False
    cap = min(me["cash"] - CASH_RESERVE, math.floor(act["value"]) - VAULT_MIN_EDGE)
    ours, hers = read_thread(t, me["id"], dealer)
    action, price, why = next_offer(cap, VAULT_LIST, ours, hers, {})
    log(f"vault: Don Ernesto {tid} {act['ref']}: ours {ours} his {[h[0] for h in hers]} cap {cap} -> {action} {price} ({why})")
    if action == "accept" and can_accept:
        if price is None or price > cap or price >= act["value"]:
            return False
        offer_id, why = fresh_accept_target(b, tid, dealer, me["id"], _key(types=[f"card:{act['ref']}"]), _key(cash=price),
                                            ("item", "price"))
        if offer_id is None:
            log(f"vault: accept blocked: {why} (decided {price})")
            return False
        if not try_reserve(me["tick"]):
            return False
        b.accept(offer_id)
        log(f"VAULT BUY {act['ref']} at {price} from Don Ernesto: worth {act['value']:.0f} to us")
        return True
    if action == "walk":
        b.close_thread(tid)
    elif action == "offer":
        b.say(tid, chato_text("buy", price, len(ours), act["ref"], "Don Ernesto"), price=price)
    return False


def banco_candidate(me, catalog, menu=None, mem=None):
    """The loop's epic for Don Ernesto: (score, ref, asset, floor, opening ask, value lost)."""
    held = epic_loop_held(me, mem or {})
    if not held:
        return None
    a = held[0]
    value = float(a.get("your_value") or 0)
    floor = max(BANCO_FLOOR, math.ceil(value) + 1)        # never under our value: a dealer-deal loss counts in full
    if floor > BANCO_ASK:
        return None                                       # Ernesto pays ~113-120: he cannot be the buyer for this one
    return (1.0, a["ref"], a, floor, BANCO_ASK, value)


def phase_banco(b, me, catalog, mem, can_accept):
    """Sell the loop's epic to Don Ernesto: one prima a round from BANCO_ASK, his `final` taken over BANCO_FLOOR."""
    return phase_chato(b, me, catalog, mem, can_accept, dealer="banco",
                       candidate_fn=lambda me_, cat_, menu=None: banco_candidate(me_, cat_, menu, mem))


def phase_picaros(b, me, catalog, mem, can_accept):
    return phase_chato(b, me, catalog, mem, can_accept, dealer="picaros", candidate_fn=picaros_candidate)


# ---------------------------------------------------------------- buying the cards we value most from dealers

DEALER_SELLS = {"abuela": {"common": 10, "uncommon": 25}, "chato": {"uncommon": 26, "rare": 77},
                "picaros": {"rare": 63, "epic": 162}}       # list prices (dealer menus). Los Pícaros lie about cards and
                                                            # deadlines: every accept is re-checked against the exact card
CARD_EDGE = dparams.SPEC["card_edge"][0]          # (compat; the cap reads dparams.static) we pay at most this share of the card's value TO US (server value, page bonus included), and
                          # always at least 1 prima under it. The ladder scores the share of the dealer's range we capture,
                          # so a deal under our value that the dealer can reach is worth more than a tight cap with no deal.
VALUE_TTL = 20            # ticks a server value stays cached
MAX_PAY = json.loads(os.environ.get("MAX_PAY", "{}"))   # optional per-card price ceilings (none by default): the cap comes from our value


def set_priority(me, catalog):
    """Neighbourhoods by what they can still give us: affinity x page progress (a nearly complete page carries the bonus).
    [(set id, score, have, of)] best first. Only released sets."""
    counts, _ = card_counts(me)
    rows = []
    for st in catalog["sets"]:
        if not st.get("released") or st["id"] not in me["affinity"]:
            continue
        page = [c for c in st["cards"] if c.get("page")]
        have = sum(1 for c in page if counts.get(c["id"], 0))
        rows.append((st["id"], round(me["affinity"][st["id"]] * (1 + have / max(1, len(page))), 2), have, len(page)))
    return sorted(rows, key=lambda r: -r[1])


def buy_priorities(b, me, catalog, mem, limit=6):
    """Missing page cards, ranked by their SERVER value to us (includes the page bonus), checked at most every VALUE_TTL
    ticks. [(ref, rarity, value)] best first."""
    counts, _ = card_counts(me)
    cache = mem.setdefault("value_cache", {})
    order = {sid: i for i, (sid, *_r) in enumerate(set_priority(me, catalog))}
    cands = []
    for st in catalog["sets"]:
        if st["id"] not in order:
            continue
        for c in st["cards"]:
            if c.get("page") and not counts.get(c["id"], 0):
                cands.append((order[st["id"]], -c["book"] * me["affinity"][st["id"]], c["id"], c["rarity"]))
    out = []
    for _o, _v, ref, rarity in sorted(cands)[:limit * 2]:
        hit = cache.get(ref)
        if not hit or me["tick"] - hit[0] > VALUE_TTL:
            try:
                hit = cache[ref] = [me["tick"], float(b.value(ref).get("your_value") or 0)]
            except BazaarError:
                continue
        out.append((ref, rarity, hit[1]))
    out.sort(key=lambda r: -r[2])
    return out[:limit]


def phase_card_buy(b, me, catalog, mem, can_accept, dealer):
    """Buy the highest-value missing card this dealer sells, at most CARD_EDGE x its value to us.
    Never repeats a price (next_offer walks instead), takes a final only under our cap. Returns (accepted, busy)."""
    if dealer not in me.get("unlocked", []):
        return False, False
    key = f"active_buy_{dealer}"
    act = mem.get(key)
    tid = next((t for t in me.get("open_threads", []) if b.thread(t).get("with") == dealer), None)
    name = dealer.title()
    if act and (tid is None or act["thread"] != tid):                     # our buy ended: record it
        t = b.thread(act["thread"])
        ours, hers = read_thread(t, me["id"], dealer)
        paid = deal_price(t, me["id"]) if t["status"] == "deal" else None
        mem["observed"]["negotiations"].append({"dealer": f"{dealer}_buy", "thread": act["thread"], "topic": act["topic"],
                                                "outcome": t["status"], "closed_reason": t.get("closed_reason"),
                                                "paid": paid, "rounds": rounds_of(ours, hers)})
        log(f"{name} card buy {t['id']} ({act['ref']}) ended: {t['status']} {t.get('closed_reason') or ''} paid {paid} "
            f"(worth {act['value']:.0f} to us)")
        if t["status"] == "deal":
            ladder_record(mem, dealer, t["id"], act.get("rarity"), act["ref"].split("-")[0], act["ref"], hers, paid, True)
        if t["status"] != "deal":
            mem.setdefault(f"{dealer}_buy_tried", {})[act["ref"]] = me["tick"]
            mem[f"{dealer}_buy_block_until"] = me["tick"] + dparams.static("buy_block")
        mem[key], act = None, None
    if tid is not None and act is None:
        return False, True                                                # a sell thread (or someone else's) is open
    if tid is None:
        if mem.get(f"{dealer}_buy_block_until", -1) > me["tick"]:
            return False, False
        sells = {**DEALER_SELLS.get(dealer, {}), **menu_rarity_prices(mem, dealer)}      # published menu wins over the old table
        pick = None
        busy_refs = {(mem.get(f"active_buy_{d}") or {}).get("ref") for d in DEALER_SELLS if d != dealer}
        for ref, rarity, value in buy_priorities(b, me, catalog, mem):
            lp = sells.get(rarity)
            if not lp or ref in busy_refs or me["tick"] - mem.get(f"{dealer}_buy_tried", {}).get(ref, -999) < dparams.static("buy_retry"):
                continue                                                  # one dealer per card: never buy it twice
            cap = min(me["cash"] - CASH_RESERVE, min(int(dparams.static("card_edge") * value), int(value) - dparams.static("card_margin")), MAX_PAY.get(ref, 10 ** 9))
            fh = feed_intel.hints(mem, dealer, "sells", rarity, ref.split("-")[0], ref)
            cap = buy_cap_with_ladder(mem, me, dealer, rarity, lp, fh, cap, None, ref)
            if feed_intel.buy_is_futile(fh, cap):                      # every team paid more: this talk cannot end in a deal
                if mem.setdefault("feed_futile_logged", {}).get(f"{dealer}|{ref}") != cap:
                    mem["feed_futile_logged"][f"{dealer}|{ref}"] = cap
                    log(f"{name}: skip buying {ref}: our cap {cap} < lowest price it sold a {rarity} to any team "
                        f"({fh['deal_min']:.0f}, {fh['n_deals']} deals, by {fh['level']})")
                continue
            if cap >= dparams.static("buy_realistic") * lp:              # realistic for the dealer, a real gain for us
                pick = (ref, rarity, value, lp, cap)
                break
        if not pick:
            return False, False
        ref, rarity, value, lp, cap = pick
        topic = {"buy": {"card": ref}}
        try:
            th = b.open_thread(dealer, topic=topic)
        except BazaarError as e:
            log(f"{name} card buy unavailable: {e.code} {e.message}")
            mem[f"{dealer}_buy_block_until"] = me["tick"] + dparams.static("buy_block")
            return False, False
        tid = th["id"]
        act = mem[key] = {"thread": tid, "topic": topic, "cash_start": me["cash"], "ref": ref, "value": value, "lp": lp,
                          "rarity": rarity}
        log(f"{name}: buying {ref} ({rarity}), worth {value:.0f} to us, cap {cap}, his list {lp}")
    t = b.thread(tid)
    if t["status"] != "open":
        return False, True
    cap = min(me["cash"] - CASH_RESERVE, min(int(dparams.static("card_edge") * act["value"]), int(act["value"]) - dparams.static("card_margin")), MAX_PAY.get(act["ref"], 10 ** 9))
    ours, hers = read_thread(t, me["id"], dealer)
    rarity = act.get("rarity") or {c["id"]: c["rarity"] for st in catalog["sets"] for c in st["cards"]}.get(act["ref"])
    fh = feed_intel.hints(mem, dealer, "sells", rarity, act["ref"].split("-")[0], act["ref"])
    value_cap = cap
    cap = buy_cap_with_ladder(mem, me, dealer, rarity, act["lp"], fh, cap, hers, act["ref"])
    action, price, why = next_offer(cap, act["lp"], ours, hers, learned(mem, f"{dealer}_buy"), open_cap=value_cap)
    log(f"{name} buy {tid} {act['ref']}: ours {ours} his {[h[0] for h in hers]} cap {cap} -> {action} {price} ({why})")
    if action == "accept" and can_accept:
        if price is None or price > cap:
            log(f"{name} buy {tid}: accept blocked: price {price} over cap {cap}")
            return False, True
        offer_id, why = fresh_accept_target(b, tid, dealer, me["id"], _key(types=[f"card:{act['ref']}"]), _key(cash=price),
                                            ("item", "price"))
        if offer_id is None:
            log(f"{name} buy {tid}: accept blocked: {why} (decided {price})")
            return False, True
        if not try_reserve(me["tick"]):
            log(f"{name} buy {tid}: accept skipped: another process already used this tick's accept")
            return False, True
        b.accept(offer_id)
        log(f"{name.upper()} BUY {act['ref']} at {price}: worth {act['value']:.0f} to us (+{act['value'] - price:.0f})")
        return True, True
    if action == "walk":
        b.close_thread(tid)
    elif action == "offer":
        text = haggle_text(price, len(ours), item=act["ref"]) if dealer == "abuela" else \
            chato_text("buy", price, len(ours), act["ref"], name)
        b.say(tid, text, price=price)
    return False, True


FLAG_EVERY = 4            # ticks between scans of our dealer threads for bad faith


def phase_flags(b, me, mem, catalog=None):
    """Flag a dealer message only when its words state a different (worse for us) price than its own structured offer.
    Each message is judged once; a wrong flag costs score, so flags.is_lie is deliberately strict."""
    if me["tick"] - mem.get("flags_scan_tick", -999) < FLAG_EVERY:
        return
    mem["flags_scan_tick"] = me["tick"]
    seen = set(mem.setdefault("flags_seen", []))
    for t in b.my_threads().get("threads", []):
        if t.get("kind") != "persona":
            continue
        dealer_sells = "buy" in (t.get("topic") or {})
        ours = []
        for m in t.get("messages", []):
            o = m.get("offer") or {}
            price = ((o.get("give") or {}).get("cash") or (o.get("want") or {}).get("cash")) if o else None
            if m["sender"] == me["id"]:
                if price:
                    ours.append(price)
                continue
            mid = m.get("id")
            if mid is None or mid in seen:
                continue
            seen.add(mid)
            lie, why = is_lie(m.get("text") or "", price, ours, dealer_sells)
            if not lie and catalog and dealer_sells and o:                   # bait-and-switch: asked card in words, another given
                names = {c["id"]: c["name"] for st in catalog["sets"] for c in st["cards"]}
                topic_card = ((t.get("topic") or {}).get("buy") or {}).get("card")
                held = {x.get("ref") for x in me["assets"] if x.get("kind") == "card"}
                lie, why = is_item_lie(m.get("text") or "", o, names, topic_card, held)
                if not lie and t.get("with") == "picaros":                   # their documented trick: another card than asked
                    lie, why = is_switch(topic_card, o, names)
            if lie:
                try:
                    b.flag(mid, f"bad faith: {why}")
                    mem.setdefault("flags_sent", []).append({"message": mid, "dealer": t["with"], "thread": t["id"], "why": why})
                    log(f"FLAG {t['with']} message {mid} (thread {t['id']}): {why}")
                except BazaarError as e:
                    log(f"flag refused for message {mid}: {e.code} {e.message}")
    mem["flags_seen"] = sorted(seen)[-3000:]


def run_agent():
    b = Bazaar(URL, os.environ["BAZAAR_KEY"], wait_on_tick=False)
    mem = load_memory()
    log("smart_agent started (deterministic, no LLM)")
    while True:
        try:
            clock = b.clock()
            if clock.get("paused"):
                time.sleep(10)
                continue
            me, catalog = b.me(), b.catalog()
            refresh_traits(b, mem, me["tick"])
            try:                                                           # the ladder book of this round (a day = a round)
                lad = mem.setdefault("ladder", {})
                if clock.get("round") is not None and lad.get("round") != clock["round"]:
                    lad["round_start"] = round_start_tick(clock, seen_change=lad.get("round") is not None)
                    lad["round"] = clock["round"]
                ladder.learn_slope(mem, me.get("score"))
                ladder_backfill(b, mem, me["id"], {c["id"]: c["rarity"] for st in catalog["sets"] for c in st["cards"]}.get,
                                lad.get("round_start") or 0)
            except Exception as e:
                log(f"ladder: {type(e).__name__}: {e}")
            log(f"--- tick {me['tick']} | cash {me['cash']} | cards {sum(a['kind'] == 'card' for a in me['assets'])} ---")
            accepted = False
            try:
                if open_packs(b, me):
                    me = b.me()                                            # the new cards are in our hand now
            except BazaarError as e:
                log("packs:", e.code, e.message)
            try:
                accepted = phase_abuela(b, me, catalog, mem)
            except BazaarError as e:
                log("Abuela step:", e.code, e.message)
            busy = {}
            try:                                                           # the epic loop first: it holds Los Pícaros' one thread
                got, _busy = phase_epic_buy(b, me, catalog, mem, not accepted)
                accepted = got or accepted
            except BazaarError as e:
                log("epic loop step:", e.code, e.message)
            for d in ("picaros", "chato", "abuela"):                       # priority cards first (Abuela: only when no pack talk)
                if d == "abuela" and mem.get("active"):
                    continue
                try:
                    got, busy[d] = phase_card_buy(b, me, catalog, mem, not accepted, d)
                    accepted = got or accepted
                except BazaarError as e:
                    log(f"{d} card buy step:", e.code, e.message)
            try:
                if not busy.get("chato") or mem.get("active_chato"):
                    accepted = phase_chato(b, me, catalog, mem, can_accept=not accepted) or accepted
            except BazaarError as e:
                log("Chato step:", e.code, e.message)
            try:
                accepted = phase_pilar(b, me, catalog, mem, can_accept=not accepted) or accepted
            except BazaarError as e:
                log("Pilar step:", e.code, e.message)
            try:
                accepted = phase_vault(b, me, catalog, mem, can_accept=not accepted) or accepted
            except BazaarError as e:
                log("vault step:", e.code, e.message)
            try:
                if not mem.get("active_vault"):
                    accepted = phase_banco(b, me, catalog, mem, can_accept=not accepted) or accepted
            except BazaarError as e:
                log("Don Ernesto step:", e.code, e.message)
            try:
                if not busy.get("picaros") or mem.get("active_picaros"):
                    accepted = phase_picaros(b, me, catalog, mem, can_accept=not accepted) or accepted
            except BazaarError as e:
                log("Picaros step:", e.code, e.message)
            reserved = {aid for d in ("chato", "pilar", "picaros", "banco")       # cards on the table with a dealer: the market must not sell them
                        for aid in ((mem.get(f"active_{d}") or {}).get("topic") or {}).get("sell", {}).get("assets", [])}
            me_market = dict(me, assets=[a for a in me["assets"] if a.get("id") not in reserved]) if reserved else me
            if legacy_should_trade():
                phase_market(b, me_market, catalog, can_accept=not accepted)
            try:                                                           # all teams' dealer talks, from the public feed
                rarity_of = {c["id"]: c["rarity"] for st in catalog["sets"] for c in st["cards"]}.get
                feed_intel.step(b, mem, me["tick"], rarity_of, log)
            except Exception as e:
                log(f"feed intel: {type(e).__name__}: {e}")
            try:
                phase_flags(b, me, mem, catalog)
            except BazaarError as e:
                log("flags step:", e.code, e.message)
            try:                                                           # The Workshop: craft only when the trade value rises
                workshop.step(b, dict(me, cash=max(0, me["cash"] - CASH_RESERVE)), catalog, mem, log, blocked_extra=reserved, allow_buy=legacy_should_trade() and not accepted)
            except Exception as e:
                log(f"workshop step: {type(e).__name__}: {e}")
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

def _test_candidates_take_menu():
    cat = {"sets": [{"id": "MAL", "released": True, "cards": [{"id": "MAL-01", "book": 10, "rarity": "common", "name": "x"},
                                                               {"id": "MAL-06", "book": 25, "rarity": "uncommon", "name": "y"}]}],
           "values": {"copy_marginals": [1.0, 0.25, 0.1]}}
    me = {"affinity": {"MAL": 0.5}, "assets": [{"id": i, "kind": "card", "ref": r, "serial": i, "rarity": rr}
                                                for i, (r, rr) in enumerate([("MAL-01", "common")] * 3 + [("MAL-06", "uncommon")] * 2)]}
    for fn in (chato_candidate, pilar_candidate, picaros_candidate):
        fn(me, cat, {"common": 10, "uncommon": 25})                     # the dealer phase always passes the menu
    pc = picaros_candidate(me, cat, {"common": 10})
    assert pc is not None and pc[1] == "MAL-01", pc                    # commons only: uncommons go to Pilar


def _test_deal_price_and_hold():
    msg = lambda who, p, final=False: {"sender": who, "offer": {"give": {"cash": 0}, "want": {"cash": p}, "final": final}}
    assert deal_price({"messages": [msg("t06", 20), msg("pilar", 22, True)]}, "t06") == 22, "we accepted her final"
    assert deal_price({"messages": [msg("pilar", 22), msg("t06", 21)]}, "t06") == 21, "she accepted ours"
    assert deal_price({"messages": []}, "t06") is None
    inf = {"params": {"tol_share": 0.1}}
    ours, hers = [25, 18], [(16, False, ""), (17, False, "")]
    assert next_offer_sell(10, 25, ours, hers, inf)[0] == "accept", "default: 17 is within tolerance of 18"
    act, price, _ = next_offer_sell(10, 25, ours, hers, inf, hold=True)
    assert act == "offer" and price < 18, (act, price)                     # Pilar: keep conceding, she keeps raising
    assert next_offer_sell(10, 25, ours, [(16, False, ""), (17, True, "")], inf, hold=True)[0] == "accept", "her final"
    assert next_offer_sell(10, 25, [25, 18], [(16, False, ""), (18, False, "")], inf, hold=True)[0] == "accept", "meets ask"
    rep = [(16, False, "")] * 3
    assert next_offer_sell(20, 25, [25, 22, 21], rep, inf, soft_floor=12, hold=True)[0] != "accept", "no soft floor"


def _test_pilar_one_prima():
    act, price, _ = next_offer_sell(16, 25, [25], [(16, False, "")], {}, hold=True)
    assert (act, price) == ("offer", 24), (act, price)                  # was 25 -> 22 (step 3)
    act, price, _ = next_offer_sell(16, 25, [25, 24], [(16, False, ""), (16, False, "")], {}, hold=True)
    assert (act, price) == ("offer", 23), (act, price)
    assert next_offer_sell(16, 25, [25, 24], [(16, False, ""), (17, True, "")], {}, hold=True)[0] == "accept"


def _test_behaviour_flags():
    hers = [(13, False, ""), (13, False, "")]
    assert next_offer_sell(10, 26, [26], hers[:1], {"fixed_bidder": True})[0] == "accept", "fixed 13 >= floor 10: close"
    assert next_offer_sell(16, 26, [26], hers[:1], {"fixed_bidder": True})[0] == "walk", "fixed 13 < floor 16: leave"
    fin = [(70, False, ""), (66, True, "")]
    assert next_offer(60, 63, [40, 50], fin, {})[0] == "walk", "a trusted final over our cap: walk"
    act = next_offer(60, 63, [40, 50], fin, {"final_unreliable": True})
    assert act[0] == "offer" and act[1] <= 60, ("broken finals: keep conceding inside the cap", act)
    sfin = [(10, False, ""), (12, True, "")]
    assert next_offer_sell(15, 26, [26, 20], sfin, {})[0] == "walk"
    assert next_offer_sell(15, 26, [26, 20], sfin, {"final_unreliable": True})[0] in ("offer", "wait")


def _test_ladder_open_cap():
    inf = {"open_bid": 59, "params": {"open_scale": 1.0}}                  # a profile learned on rares, used on an uncommon
    assert next_offer(26, 26, [], [], inf, open_cap=16) == ("offer", 16, "opening bid"), "never open into the ladder premium"
    assert next_offer(26, 26, [], [], inf)[1] == 26, "without open_cap the old behaviour stands"


def _test_epic_loop():
    global EPIC_LOOP
    assert not EPIC_LOOP or os.environ.get("EPIC_LOOP") == "1", "off by default since the Payday deck"
    m0 = {"ladder": {"round": 2, "deals": []}, "epic_loop": {"held": [9], "tried": {}}}
    me0 = {"cash": 461, "assets": [{"id": 9, "kind": "card", "ref": "RET-11", "rarity": "epic", "your_value": 162}]}
    assert banco_candidate(me0, {}, None, m0) is None, "Ernesto never gets an epic under our value"
    assert not LADDER_PREMIUM or os.environ.get("LADDER_PREMIUM") == "1", "no ladder premium by default"
    saved, EPIC_LOOP = EPIC_LOOP, True
    try:
        _test_epic_loop_on()
    finally:
        EPIC_LOOP = saved


def _test_epic_loop_on():
    mem = {"ladder": {"round": 2, "deals": []}, "epic_loop": {"held": [], "tried": {}},
           "feed_intel": {"deals": {"banco|buys|epic": [{"price": 116.0}, {"price": 120.0}]}}}
    me = {"cash": 461, "assets": [{"id": 9, "kind": "card", "ref": "SAL-11", "rarity": "epic"}]}
    assert epic_loop_wants_one(me, mem)[0], "level 5 empty, cash over reserve + cap"
    assert not epic_loop_wants_one(dict(me, cash=EPIC_LOOP_RESERVE + EPIC_LOOP_CAP - 1), mem)[0], "keep Sunday's reserve"
    mem["epic_loop"]["held"] = [9]
    assert not epic_loop_wants_one(me, mem)[0], "one epic at a time"
    cand = banco_candidate(me, {}, None, mem)
    assert cand[1] == "SAL-11" and cand[3] == BANCO_FLOOR > 113 and cand[4] == BANCO_ASK
    assert range_hints(mem, "banco", "buys", "epic")["deal_max"] == 120.0, "two real deals bound the range"
    mem["epic_loop"]["held"] = []
    for t in (1, 2, 3):
        ladder.record_deal(mem, 2, "banco", t, 113, 120, 120, False)
    assert not epic_loop_wants_one(me, mem)[0], "Ernesto's level full: the loop stops"


def selftest():
    global CASH_RESERVE
    CASH_RESERVE = 0                                     # offline fixtures hold little cash: test the logic without the floor
    _test_ladder_open_cap()
    _test_epic_loop()
    _test_pilar_one_prima()
    _test_candidates_take_menu()
    _test_behaviour_flags()
    _test_deal_price_and_hold()
    import tempfile
    os.environ["ACCEPT_GATE_DIR"] = tempfile.mkdtemp(prefix="gate_selftest_")     # never reserve real ticks in the repo-root gate
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
    dem = demand_from(offers, me["id"])
    assert dem["LAT-04"] == (8, None), dem                                  # "tx" is not a team id: demand, but no `to`
    p = plan[0]
    assert p[2] == "LAT-04" and p[4] is None, p                              # open ask (cannot address a pseudonym)
    assert p[1] == max(ask_floor(p[3]), 9, math.ceil(8 * BID_PREMIUM)), p    # above its bid, never under our floor
    offers_t = [dict(o, maker="t09") if i == 2 else o for i, o in enumerate(offers)]
    p2 = listing_plan(me, catalog, venue, offers_t, set())[0]
    assert p2[4] == "t09" and p2[1] == p[1], p2                              # a real team id: directed to that bidder
    assert demand_from([dict(offers[2], maker=me["id"])], me["id"]) == {}, "our own bids are not demand"
    assert demand_from([dict(offers[2], maker="m2eb45963")], me["id"]) == {"LAT-04": (8, None)}, "pseudonymous bids count"
    assert demand_from([dict(offers[2], maker="m2eb45963")], me["id"], {offers[2]["id"]}) == {}, "our own offer by id"
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
                                                          {"id": 7, "kind": "card", "ref": "MAL-07", "serial": 2, "rarity": "uncommon"},
                                                          {"id": 8, "kind": "card", "ref": "LAT-10", "serial": 1, "rarity": "rare"}]}
    cand = chato_candidate(me2, catalog2)
    assert cand and cand[1] == "MAL-07", "only the card that is cheap for us and realistic for him"
    me2["assets"] = [a for a in me2["assets"] if a["id"] != 7]
    assert chato_candidate(me2, catalog2) is None, "never our only copy"
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

    # A bid stored as want.cards (how we post it) is the same opportunity as want.types (how the board returns it).
    cards_bid = mk(14, {"cash": 8, "assets": [], "types": []}, {"cash": 0, "assets": [], "cards": ["LAT-04"]})
    got_cards = [o for o in market_opportunities(me, catalog, venue, [cards_bid], lambda ref, n: None) if o["kind"] == "sell"]
    assert got_cards and got_cards[0]["ref"] == "LAT-04" and got_cards[0]["asset"] == 2, got_cards
    v03 = {"venue": "v03", "fee_bps": 100, "fee_per_card": 0}
    thin = mk(15, {"cash": 0, "assets": [{"ref": "LAV-03", "id": 70}], "types": []},
              {"cash": 0, "assets": [], "types": ["card:LAT-04"]})
    # Incoming card worth 4, our spare LAT-04 worth 1.75. v03 charges no per-card fee; El Rastro charges 1 P per card.
    swap_v3 = market_opportunities(me, catalog, venue, [dict(thin, _venue=v03)], lambda ref, n: 4.0)
    swap_r = market_opportunities(me, catalog, venue, [dict(thin, _venue=venue)], lambda ref, n: 4.0)
    assert swap_v3 and swap_v3[0]["kind"] == "swap" and swap_v3[0]["gain"] >= SELL_MIN_GAIN, swap_v3
    assert swap_r == [], swap_r
    assert swap_taker_fee(v03) == 0 and swap_taker_fee(venue) == 2
    assert fee_of(v03, 20) == 1, "Mercado Trece is 1% and the acceptor pays it"

    class _Mkt:
        def __init__(self):
            self.posts, self.accepted, self.boards = [], [], []
        def venues(self):
            return {"venues": [
                {"venue": "v01", "fee_bps": 0, "fee_per_card": 0, "owner": "t06"},
                {"venue": "v02", "fee_bps": 0, "fee_per_card": 0, "owner": "t12"},
                {"venue": "v03", "fee_bps": 100, "fee_per_card": 0, "owner": "t13"},
                {"venue": "rastro", "fee_bps": 500, "fee_per_card": 1, "owner": "world"}]}
        def my_offers(self):
            return {"offers": [{"id": 99, "maker": "t09", "to": "t06", "status": "open", "venue": "v03", "thread": None,
                                "give": {"cash": 20, "assets": [], "types": []},
                                "want": {"cash": 0, "assets": [], "types": ["card:LAT-04"]}}]}
        def board(self, vid):
            self.boards.append(vid)
            if vid == "v01":
                raise AssertionError("must not scan our own venue")
            if vid == "v02":
                return {"offers": [{"id": 7, "maker": "t03", "to": None, "status": "open", "thread": None,
                                    "give": {"cash": 0, "assets": [{"id": 70, "ref": "LAV-03"}], "types": []},
                                    "want": {"cash": 0, "assets": [], "cards": ["LAT-04"]}}]}
            if vid == "rastro":
                return {"offers": [{"id": 8, "maker": "t04", "to": None, "status": "open", "thread": None,
                                    "give": {"cash": 0, "assets": [{"id": 80, "ref": "LAV-09"}], "types": []},
                                    "want": {"cash": 4, "assets": [], "types": []}}]}
            return {"offers": []}
        def value(self, ref):
            return {"your_value": 30.0 if ref == "LAV-09" else 3.0}
        def accept(self, oid, assets=None):
            self.accepted.append((oid, assets))
            return {"ok": True}
        def list_offer(self, give, want, venue=None, to=None, expires_in_ticks=40):
            self.posts.append((give, want, venue, to, expires_in_ticks))
            return {"ok": True}

    mkt_cat = {"sets": [{"id": "LAT", "released": True, "cards": [{"id": "LAT-04", "book": 10}, {"id": "LAT-05", "book": 10}]},
                        {"id": "LAV", "released": True, "cards": [{"id": "LAV-01", "book": 10}, {"id": "LAV-03", "book": 20},
                                                                 {"id": "LAV-09", "book": 40}]}],
               "values": {"copy_marginals": [1.0, 0.25, 0.1]}}
    mkt_me = {"id": "t06", "cash": 100, "tick": 1, "affinity": {"LAT": 0.7, "LAV": 1.6},
              "assets": [{"id": 1, "kind": "card", "ref": "LAT-04", "serial": 3},
                         {"id": 2, "kind": "card", "ref": "LAT-04", "serial": 9},
                         {"id": 3, "kind": "card", "ref": "LAT-05", "serial": 1},
                         {"id": 4, "kind": "card", "ref": "LAT-05", "serial": 4}]}
    mkt = _Mkt()
    assert phase_market(mkt, mkt_me, mkt_cat, True) is True
    assert mkt.accepted == [(8, None)], mkt.accepted          # one accept, and it is the ask that gains at our value
    assert "v01" not in mkt.boards and {"v02", "v03", FAIR} <= set(mkt.boards)
    asks = [p for p in mkt.posts if p[0].get("assets") and "cash" in p[1]]
    bids = [p for p in mkt.posts if p[0].get("cash")]
    swaps = [p for p in mkt.posts if p[0].get("assets") and p[1].get("cards")]
    assert asks and asks[0][0]["assets"] == [4] and asks[0][1] == {"cash": math.ceil(10 * BUYER_MULT)} and asks[0][2] == FAIR and asks[0][4] == 120, asks
    assert swaps and swaps[0][0] == {"assets": [2]} and swaps[0][1] == {"cards": ["LAV-03"]} and swaps[0][2] == "v02" and swaps[0][3] == "t03", swaps
    assert bids and bids[0][1] == {"cards": ["LAV-01"]} and bids[0][2] == FAIR and bids[0][3] is None and bids[0][4] == 120, bids
    assert len({p[1]["cards"][0] for p in bids}) == len(bids), "the same bid is not posted twice"
    assert phase_market(mkt, mkt_me, mkt_cat, False) is False and len(mkt.accepted) == 1, "a second call must not take the accept"
    assert fees_for(FAIR, {})["fee_bps"] == 0 and fees_for(FAIR, {})["fee_per_card"] == 0
    assert FAIR in boards_to_scan({}, "t06") and FAIR not in boards_to_scan({FAIR: {"owner": "t06"}}, "t06")

    class _Book:
        def __init__(self, refuse=()):
            self.calls, self.refuse = [], set(refuse)
        def list_offer(self, give, want, venue=None, to=None, expires_in_ticks=40):
            self.calls.append(venue)
            if venue in self.refuse:
                raise BazaarError("venue_not_live" if venue == FAIR else "self_venue", "no", 400)
            return {"ok": True}
    cash, swap, down = _Book(), _Book(), _Book((FAIR,))
    assert place_board(cash, {"cash": 5}, {"cards": ["LAT-04"]}) == FAIR and cash.calls == [FAIR]
    assert place_board(swap, {"assets": [2]}, {"cards": ["LAV-03"]}) == "v02" and swap.calls == ["v02"]
    assert place_board(down, {"cash": 5}, {"cards": ["LAT-04"]}) == "v02" and down.calls == [FAIR, "v02"]
    print("selftest OK")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    else:
        run_agent()
