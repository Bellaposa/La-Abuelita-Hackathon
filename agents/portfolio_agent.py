"""Portfolio logic: what our cards are worth to us, what to sell, what to hunt, pack opening.

Pure functions (no network) plus `step(b, ctx)` that opens sealed packs and refreshes a cached report in ctx.

    python -m agents.portfolio_agent --selftest

Value model (private values; the server's b.value(card) is authoritative when a Valuer wraps b):
  value of the k-th copy of a card = book * set affinity * copy_marginals[k-1]
  completing a page (commons+uncommons+rares of a set) adds catalog values.page_bonus x page value; a missing page card
  gets credit for the bonus only when the page is close (<= MAX_MISSING left): half a share, the last card the whole bonus.
"""
from __future__ import annotations

import math
import os
import sys
import time

try:
    from bazaar_sdk import BazaarError
except Exception:  # pragma: no cover
    class BazaarError(Exception):
        code = "error"

PAGE_RARITIES = ("common", "uncommon", "rare")
DEFAULT_MARGINALS = (1.0, 0.25, 0.1)
DEFAULT_PAGE_BONUS = 0.25
MAX_MISSING = 4            # page bonus credit only when at most this many page cards are missing
MIN_GAIN = 3               # primas of private-value gain required for any trade
SELL_MARGIN = 1.1          # sell a duplicate for >= 110 % of what the copy is worth to us ...
MIN_SELL_ADD = 2           # ... and at least this many primas more


# ------------------------------------------------------------------ catalog helpers

def card_index(catalog):
    return {c["id"]: dict(c, set=s["id"], released=s.get("released", True))
            for s in catalog.get("sets", []) for c in s["cards"]}


def is_page_card(c):
    return bool(c["page"]) if "page" in c else c.get("rarity") in PAGE_RARITIES


def marginals(catalog):
    return list((catalog.get("values") or {}).get("copy_marginals") or DEFAULT_MARGINALS)


def page_bonus_rate(catalog):
    return float((catalog.get("values") or {}).get("page_bonus", DEFAULT_PAGE_BONUS))


def affinity_of(me):
    return me.get("affinity") or {}


def fee_of(venue, price, n_cards=1):
    return math.ceil(venue.get("fee_bps", 500) * price / 10000) + venue.get("fee_per_card", 1) * n_cards


def holdings(me):
    """(counts {ref: n}, ids {ref: [asset dicts]}) for cards only."""
    counts, ids = {}, {}
    for a in me.get("assets", []):
        if a.get("kind") == "card":
            counts[a["ref"]] = counts.get(a["ref"], 0) + 1
            ids.setdefault(a["ref"], []).append(a)
    return counts, ids


# ------------------------------------------------------------------ value model

def base_value(ref, catalog, affinity, idx=None):
    c = (idx or card_index(catalog)).get(ref)
    if not c:
        return 0.0
    return c["book"] * affinity.get(c["set"], 1.0)


def page_state(set_id, catalog, counts, affinity, idx=None):
    idx = idx or card_index(catalog)
    page = [c for c in idx.values() if c["set"] == set_id and is_page_card(c)]
    page_value = sum(c["book"] * affinity.get(set_id, 1.0) for c in page)
    missing = [c["id"] for c in page if counts.get(c["id"], 0) == 0]
    return {"size": len(page), "missing": missing, "bonus": page_bonus_rate(catalog) * page_value}


def bonus_credit(ref, catalog, counts, affinity, idx=None):
    """Share of the page bonus credited to acquiring `ref` (0 unless a missing page card of a near-complete page)."""
    idx = idx or card_index(catalog)
    c = idx.get(ref)
    if not c or not is_page_card(c) or counts.get(ref, 0) > 0:
        return 0.0
    st = page_state(c["set"], catalog, counts, affinity, idx)
    n = len(st["missing"])
    if n == 0 or n > MAX_MISSING:
        return 0.0
    return st["bonus"] if n == 1 else st["bonus"] / (2 * n)


def marginal_value(ref, count_held, catalog, affinity, me_counts=None, idx=None):
    """Model value of copy number `count_held + 1` of `ref` (the next one we would get).
    count_held = 0: first copy (with page-bonus credit); >= 1: a duplicate (book x affinity x marginal)."""
    idx = idx or card_index(catalog)
    marg = marginals(catalog)
    v = base_value(ref, catalog, affinity, idx) * marg[min(count_held, len(marg) - 1)]
    if count_held == 0:
        v += bonus_credit(ref, catalog, me_counts if me_counts is not None else {}, affinity, idx)
    return v


def copy_loss(ref, count_held, catalog, affinity, me_counts=None, idx=None):
    """What we lose by giving away one of `count_held` copies: the value of the last copy.
    Giving the only copy loses its first-copy value and the page credit it supports."""
    idx = idx or card_index(catalog)
    k = max(1, count_held)
    marg = marginals(catalog)
    v = base_value(ref, catalog, affinity, idx) * marg[min(k - 1, len(marg) - 1)]
    if k == 1:
        c = idx.get(ref)
        if c and is_page_card(c):
            cnt = dict(me_counts or {})
            cnt[ref] = 0
            v += bonus_credit(ref, catalog, cnt, affinity, idx)
    return v


class Valuer:
    """Value of one more copy of a card: b.value() first (cached, call-capped), model as fallback."""

    def __init__(self, b, me, catalog, max_calls=12):
        self.b, self.me, self.catalog = b, me, catalog
        self.idx = card_index(catalog)
        self.aff = affinity_of(me)
        self.counts, _ = holdings(me)
        self.cache, self.calls, self.max_calls = {}, 0, max_calls

    def next_copy(self, ref):
        if ref in self.cache:
            return self.cache[ref]
        v = None
        if self.b is not None and self.calls < self.max_calls:
            self.calls += 1
            try:
                v = self.b.value(ref).get("your_value")
            except Exception:
                v = None
        if v is None:
            v = self.model_next(ref)
        self.cache[ref] = float(v)
        return self.cache[ref]

    def model_next(self, ref):
        return marginal_value(ref, self.counts.get(ref, 0), self.catalog, self.aff, self.counts, self.idx)

    def loss(self, ref, held=None):
        n = self.counts.get(ref, 0) if held is None else held
        return copy_loss(ref, n, self.catalog, self.aff, self.counts, self.idx)


# ------------------------------------------------------------------ decisions

def sell_floor(ref, count_held, catalog, affinity, me_counts=None):
    """Least net price we accept for one of `count_held` copies. Never below what the copy is worth to us."""
    loss = copy_loss(ref, count_held, catalog, affinity, me_counts)
    return math.ceil(max(loss * SELL_MARGIN, loss + MIN_SELL_ADD))


def buy_ceiling(value, venue=None, n_cards=1, min_gain=MIN_GAIN):
    """Highest price p with value - p - fee(p) >= min_gain (0 if none)."""
    venue = venue or {"fee_bps": 500, "fee_per_card": 1}
    p = int(value - min_gain)
    while p > 0 and value - p - fee_of(venue, p, n_cards) < min_gain:
        p -= 1
    return max(0, p)


def rank_targets(me, catalog, valuer=None):
    """Missing cards of released sets, best first: [{ref, value, set, page_card, page_missing, completes_page}].
    Near-complete page cards first, then by value. `valuer(ref)` may override the model value."""
    idx = card_index(catalog)
    counts, _ = holdings(me)
    aff = affinity_of(me)
    out = []
    for ref, c in idx.items():
        if counts.get(ref, 0) or not c["released"]:
            continue
        st = page_state(c["set"], catalog, counts, aff, idx)
        v = marginal_value(ref, 0, catalog, aff, counts, idx)
        if valuer:
            try:
                v = float(valuer(ref))
            except Exception:
                pass
        pg = is_page_card(c)
        nm = len(st["missing"]) if pg else None
        out.append({"ref": ref, "value": round(v, 1), "set": c["set"], "page_card": pg, "page_missing": nm,
                    "completes_page": pg and nm == 1})
    out.sort(key=lambda t: (not t["completes_page"], not (t["page_missing"] is not None and t["page_missing"] <= MAX_MISSING),
                            -t["value"]))
    return out


def sellable_duplicates(me, catalog, listed_assets=()):
    """[{asset, ref, floor, loss, book}] spare copies (never the last copy; keeps the lowest serial)."""
    idx = card_index(catalog)
    counts, ids = holdings(me)
    aff = affinity_of(me)
    out = []
    for ref, n in counts.items():
        if n < 2 or ref not in idx:
            continue
        spare = sorted(ids[ref], key=lambda a: -(a.get("serial") or 0))[:n - 1]
        for k, a in enumerate(spare):          # each further copy sold costs us the value of a lower copy slot
            if a.get("locked") or a["id"] in listed_assets:
                continue
            cnt = n - k
            out.append({"asset": a["id"], "ref": ref, "floor": sell_floor(ref, cnt, catalog, aff, counts),
                        "loss": round(copy_loss(ref, cnt, catalog, aff, counts), 1), "book": idx[ref]["book"]})
    return out


def open_packs(b, me, log=print):
    """Open every sealed pack we hold. Returns refs pulled."""
    pulled = []
    for a in me.get("assets", []):
        if a.get("kind") == "pack" and not a.get("locked"):
            try:
                cards = b.open_pack(a["id"]).get("cards", [])
                refs = [c.get("ref") or c.get("id") for c in cards]
                pulled += refs
                log("opened pack", a["id"], refs)
            except BazaarError as e:
                log("open_pack refused:", getattr(e, "code", e))
    return pulled


def report(me, catalog, valuer=None):
    counts, _ = holdings(me)
    return {"targets": rank_targets(me, catalog, valuer)[:10], "spares": sellable_duplicates(me, catalog)[:20],
            "cash": me.get("cash"), "n_cards": sum(counts.values())}


def step(b, ctx):
    """One pass per tick: open packs, refresh ctx['portfolio'] with ranked targets and spare copies."""
    me = b.me()
    open_packs(b, me)
    catalog = ctx.get("catalog") or ctx.setdefault("catalog", b.catalog())
    ctx["portfolio"] = report(me, catalog)
    return ctx["portfolio"]


def run_forever():
    from bazaar_sdk import Bazaar
    b = Bazaar(os.environ.get("BAZAAR_URL", "https://bazaar.causaprima.ai"), os.environ["BAZAAR_KEY"], wait_on_tick=False)
    ctx = {}
    while True:
        try:
            r = step(b, ctx)
            print(time.strftime("%H:%M:%S"), "targets", [(t["ref"], t["value"]) for t in r["targets"][:5]], flush=True)
        except BazaarError as e:
            print("error", e, flush=True)
        try:
            b.wait_tick()
        except Exception:
            time.sleep(5)


# ------------------------------------------------------------------ selftest

def toy_catalog():
    sets = []
    book = {"common": 10, "uncommon": 25, "rare": 77, "epic": 200, "legendary": 500}
    for sid, rel in (("LAV", True), ("MAL", True), ("RET", False)):
        cards = []
        for i, r in enumerate(["common"] * 5 + ["uncommon"] * 3 + ["rare"] * 2 + ["epic", "legendary"], 1):
            cards.append({"id": f"{sid}-{i:02d}", "rarity": r, "book": book[r], "page": r in PAGE_RARITIES})
        sets.append({"id": sid, "released": rel, "cards": cards})
    return {"sets": sets, "values": {"page_bonus": 0.25, "copy_marginals": [1, 0.25, 0.1]}}


def selftest():
    cat = toy_catalog()
    aff = {"LAV": 2.0, "MAL": 0.5, "RET": 1.0}
    assets = [{"id": 1, "kind": "card", "ref": "LAV-01", "serial": 5}, {"id": 2, "kind": "card", "ref": "LAV-01", "serial": 9},
              {"id": 3, "kind": "card", "ref": "MAL-01", "serial": 1}]
    for i in range(2, 11):
        assets.append({"id": 100 + i, "kind": "card", "ref": f"LAV-{i:02d}", "serial": 1})   # LAV page 01..10 complete
    me = {"id": "t1", "cash": 100, "affinity": aff, "assets": assets}
    assert abs(marginal_value("LAV-01", 1, cat, aff) - 5.0) < 1e-9
    assert sell_floor("LAV-01", 2, cat, aff) == 7, sell_floor("LAV-01", 2, cat, aff)
    d = sellable_duplicates(me, cat)
    assert len(d) == 1 and d[0]["asset"] == 2, d                       # keeps the lowest serial
    me2 = {"id": "t1", "cash": 100, "affinity": aff, "assets": [a for a in assets if a["id"] != 110]}
    counts, _ = holdings(me2)
    assert "LAV-10" not in counts
    v = marginal_value("LAV-10", 0, cat, aff, counts)
    assert v > 77 * 2, v                                               # book*aff + the whole page bonus
    t = rank_targets(me2, cat)
    assert t[0]["ref"] == "LAV-10" and t[0]["completes_page"], t[:2]
    assert all(x["set"] != "RET" for x in t)                           # unreleased set excluded
    assert copy_loss("LAV-09", 1, cat, aff, counts) > 25 * 2           # last copy: value + page credit
    venue = {"fee_bps": 500, "fee_per_card": 1}
    p = buy_ceiling(100, venue)
    assert 100 - p - fee_of(venue, p) >= MIN_GAIN and 100 - (p + 1) - fee_of(venue, p + 1) < MIN_GAIN, p
    assert buy_ceiling(2, venue) == 0
    print("portfolio_agent selftest OK")
    return True


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(0 if selftest() else 1)
    run_forever()
