"""Every tunable number of the dealer negotiation, in ONE place, each with bounds. No magic numbers inside the strategy code.

Three kinds of value:

  static     a bounded default (SPEC), overridable with the DEALER_PARAMS environment variable (JSON, e.g.
             '{"pack_edge": 0.85}'). Always clamped to [lo, hi], so a typo cannot push a parameter outside its safe range.
  learned    DERIVED from the negotiation history we already store (no new state, so it is reproducible and cannot drift away
             from the evidence): tol_share, open_scale, block_after_fail. Clamped to their bounds, never below `lo` or
             above `hi`, and equal to the default until there is enough evidence. DEALER_LEARN=0 switches learning off.
  safety     pack_edge / card_edge (how much of OUR value we are willing to pay) are bounded but deliberately NOT learned:
             they are the line between a deal and a loss at our private values, and a learner that only sees prices could talk
             itself into paying too much. Change them by hand (DEALER_PARAMS), within their bounds.

The defaults equal the numbers the code used before this table existed, so introducing it changed no behaviour.
"""
import json
import os
import statistics

# name: (default, lo, hi, doc)
SPEC = {
    # ---- accepting
    "tol_share":        (0.04, 0.0, 0.10, "accept her ask when it is within this share of the list price above our last bid [learned]"),
    # ---- how fast we concede
    "default_k":        (0.25, 0.08, 0.60, "share of the gap we close per round on the buy side until the dealer's response is learned"),
    "sell_k0":          (0.35, 0.10, 0.60, "same on the sell side"),
    "k_small":          (0.10, 0.05, 0.20, "step share measured for 'small' steps (best_k candidate)"),
    "k_mid":            (0.25, 0.15, 0.40, "step share for 'mid' steps"),
    "k_large":          (0.50, 0.35, 0.80, "step share for 'large' steps"),
    "firm_k":           (0.50, 0.30, 0.80, "when the dealer stopped moving and the price is workable, close with this share of the gap"),
    "slow_mult":        (0.50, 0.20, 0.90, "multiplier on k when the dealer conceded at least as much as we did"),
    "token_k":          (0.00, 0.00, 0.10, "share of the gap when the dealer is firm ABOVE our cap (a token step)"),
    # ---- opening
    "open_frac_list":   (0.50, 0.30, 0.80, "opening bid as a share of the list price when we know nothing"),
    "open_frac_median": (0.80, 0.50, 0.95, "opening bid as a share of the median price we paid"),
    "open_frac_settle": (0.70, 0.40, 0.90, "opening bid as a share of the LOWEST price the dealer ever settled at"),
    "open_ask_mult":    (1.20, 1.00, 1.50, "opening ask as a multiple of the HIGHEST price the dealer ever settled at (sell side)"),
    "open_scale":       (1.00, 0.60, 1.40, "multiplier on whatever opening the rules produce [learned]"),
    "soft_cap_mult":    (1.05, 1.00, 1.20, "past this multiple of the highest settle price we only creep up one prima per round"),
    # ---- how much of our value we are willing to pay (safety: NOT learned)
    "pack_edge":        (0.90, 0.50, 0.99, "we pay at most this share of what a pack is worth to us"),
    "card_edge":        (0.95, 0.50, 0.99, "same for a single card"),
    "floor_mult":       (1.40, 1.00, 2.50, "sell floor = what the copy costs us x this + floor_add"),
    "floor_add":        (2.0, 0.0, 10.0,   "primas of margin added to the sell floor"),
    # ---- which cards to offer / buy
    "cand_floor_share": (0.85, 0.50, 1.00, "offer a card only if our floor is at most this share of the dealer's list price"),
    "cand_score_share": (0.80, 0.50, 1.00, "score of a candidate = this share of the list price minus what the copy costs us"),
    "buy_realistic":    (0.80, 0.40, 1.00, "buy a card only if our cap reaches this share of the dealer's list price"),
    "pilar_list_mult":  (1.00, 0.80, 1.50, "assumed list of Doña Pilar relative to book (her prices are not published)"),
    "pilar_focus_mult": (1.10, 1.00, 1.50, "extra multiple for the sets she collects"),
    # ---- patience with dealers that said no
    "block_after_fail": (30, 10, 120, "ticks we leave a dealer alone after a conversation that did not end in a deal [learned]"),
    "retry_same_card":  (90, 30, 300, "ticks before offering the same card again to a dealer that refused it"),
    "buy_block":        (20, 5, 120, "ticks before trying to buy from a dealer again after a failed buy"),
    "buy_retry":        (60, 20, 300, "ticks before trying to buy the same card again"),
    # ---- priors from the dealers' published traits
    "trait_shrew":      (0.8, 0.0, 2.0, "weight of shrewdness in the step prior"),
    "trait_gen":        (0.6, 0.0, 2.0, "weight of generosity"),
    "trait_pat":        (0.3, 0.0, 2.0, "weight of patience"),
    "prior_lo":         (0.5, 0.2, 1.0, "lowest step prior"),
    "prior_hi":         (1.6, 1.0, 3.0, "highest step prior"),
    "ref_shrew":        (0.4, 0.0, 1.0, "shrewdness of a 'neutral' dealer (the trait prior is relative to this)"),
    "ref_gen":          (0.5, 0.0, 1.0, "generosity of a neutral dealer"),
    "ref_pat":          (0.6, 0.0, 1.0, "patience of a neutral dealer"),
    "traits_every":     (200, 50, 1000, "ticks between refreshes of the dealers' published traits and menus"),
    # ---- when to call a dealer firm / how to read the response
    "firm_rounds":      (3, 2, 6, "identical dealer prices in a row before we call it firm (and a soft-floor deal may close)"),
    "bucket_small":     (0.15, 0.05, 0.30, "a concession below this share of the gap is a 'small' step"),
    "bucket_mid":       (0.35, 0.20, 0.60, "below this share (and above small) it is a 'mid' step; above, 'large'"),
    "soft_add":         (1, 0, 5, "primas over what the copy costs us for the one soft-floor deal per dealer"),
    "card_margin":      (1, 0, 5, "we always pay at least this many primas under what a card is worth to us"),
    "reach_cap":        (20, 5, 200, "do not open a pack thread if our cap is under the lowest price she ever sold at, capped at this"),
    # ---- how much evidence before we trust it
    "min_samples":      (3, 2, 10, "observations per step-size bucket before its response ratio counts"),
    "min_deals":        (2, 1, 10, "finished deals before the median price paid is used"),
    "min_settle":       (2, 2, 10, "settle prices (deals + finals) before we open from them"),
    "min_settle_cap":   (3, 2, 10, "settle prices before a soft ceiling is used"),
    "min_evidence":     (3, 2, 10, "observations before a LEARNED parameter leaves its default"),
    # ---- last resort
    "fallback_list":    (26, 1, 5000, "list price of a pack when the dealer's menu is unknown"),
}
LEARNED = ("tol_share", "open_scale", "block_after_fail")


def clamp(name, v):
    d, lo, hi, _ = SPEC[name]
    try:
        v = float(v)
    except (TypeError, ValueError):
        return d
    v = max(lo, min(hi, v))
    return int(round(v)) if isinstance(d, int) else v


def _overrides():
    try:
        raw = json.loads(os.environ.get("DEALER_PARAMS", "") or "{}")
    except ValueError:
        return {}
    return {k: clamp(k, v) for k, v in raw.items() if k in SPEC} if isinstance(raw, dict) else {}


def static(name):
    """The bounded value of a parameter: its default, or the DEALER_PARAMS override (clamped). Cheap enough to call anywhere."""
    ov = _overrides()
    return ov[name] if name in ov else SPEC[name][0]


def learning_enabled():
    return os.environ.get("DEALER_LEARN", "1") != "0"


# ------------------------------------------------------------------ learned values: derived from stored history

def _recent(mem, key, n=10):
    return [x for x in mem["observed"]["negotiations"] if x.get("dealer", "abuela") == key][-n:]


def derive(mem, key, list_price=None):
    """{'values': {name: value}, 'notes': [str]} with the learned parameters for dealer `key`. Only names that moved away from the
    default appear in `values`. Evidence needed: `min_evidence` observations; every value is clamped to its bounds."""
    values, notes = {}, []
    if not learning_enabled():
        return {"values": values, "notes": ["learning disabled (DEALER_LEARN=0)"]}
    negs = _recent(mem, key)
    sale = any("received" in n for n in negs)
    lp = list_price or static("fallback_list")
    tol_abs = max(1, int(static("tol_share") * lp))

    # tol_share: how far above our last bid her ask was on the deals we closed by accepting it (finals excluded: they are taken
    # regardless of the tolerance). The tolerance should cover the usual gap, with a margin of 50%.
    shares = []
    for n in negs:
        amount = n.get("paid") or n.get("received")
        rounds = n.get("rounds") or []
        if n.get("outcome") != "deal" or not amount or not rounds:
            continue
        last = rounds[-1]
        if last.get("final") or last.get("our") is None:
            continue
        gap = (last["our"] - amount) if sale else (amount - last["our"])
        shares.append(max(0.0, gap) / lp)
    if len(shares) >= static("min_evidence"):
        values["tol_share"] = clamp("tol_share", 1.5 * statistics.median(shares))
        notes.append(f"tol_share {values['tol_share']:.3f} from {len(shares)} deals (median gap {statistics.median(shares):.3f} of list)")

    # open_scale: deals that closed almost at our opening mean we opened too generously (scale down); long haggles that ended far
    # from our opening mean we opened too stingily (scale up). On the sell side the signs are mirrored.
    quick = slow = 0
    for n in negs:
        amount = n.get("paid") or n.get("received")
        rounds = n.get("rounds") or []
        if n.get("outcome") != "deal" or not amount or not rounds or rounds[0].get("our") is None:
            continue
        opening, far = rounds[0]["our"], abs(amount - rounds[0]["our"])
        near = far <= tol_abs
        if near and len(rounds) <= 3:
            quick += 1
        elif len(rounds) >= 6 and far > 0.3 * amount:
            slow += 1
    if quick or slow:
        up = (quick - 0.75 * slow) if sale else (0.75 * slow - quick)           # +: ask more / bid more
        scale = clamp("open_scale", 1.0 + 0.04 * up)
        if scale != SPEC["open_scale"][0]:
            values["open_scale"] = scale
            notes.append(f"open_scale {scale:.3f} ({quick} quick deals, {slow} slow ones, {'sell' if sale else 'buy'} side)")

    # block_after_fail: every recent cooloff doubles our patience a little.
    cool = sum(1 for n in negs if n.get("closed_reason") == "cooloff")
    if cool:
        values["block_after_fail"] = clamp("block_after_fail", SPEC["block_after_fail"][0] * (1 + 0.5 * cool))
        notes.append(f"block_after_fail {values['block_after_fail']} after {cool} cooloffs")
    return {"values": values, "notes": notes}


def learned_params(mem, key, list_price=None):
    """Static values overlaid with the learned ones: the dict the strategy reads (always inside the bounds)."""
    out = {n: static(n) for n in LEARNED}
    out.update(derive(mem, key, list_price)["values"])
    return out
