"""Read-only view of what we have learned: dealers, rivals in duels, market makers. Feeds the dashboard panel.

Pure function of the memory dicts (no network, no writes, never mutates its inputs): `learning_view(mem, duels_mem, intel_state)`.
Everything is plain JSON-serialisable data. A missing, empty or corrupt memory gives an empty view, not an error.

For every figure it says how much evidence is behind it (`n`), because with a handful of negotiations most numbers are still the
defaults and the panel must not pretend otherwise.
"""
import importlib
import os
import sys

from bz.dealers import params as dp

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
NAMES = {"abuela": "Abuela Carmen", "chato": "El Chato", "pilar": "Doña Pilar", "duende": "El Duende"}


def _agent():
    """smart_agent (profile_hints, infer, trait_multiplier), imported lazily so this module stays light and testable."""
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    return importlib.import_module("smart_agent")


def _label(key):
    base = key[:-4] if key.endswith("_buy") else key
    return NAMES.get(base, base.title()) + (" · compra cartas" if key.endswith("_buy") else "")


def _param_rows(mem, key, list_price):
    d = dp.derive(mem, key, list_price)
    rows = []
    for name in dp.LEARNED:
        default, lo, hi, doc = dp.SPEC[name]
        value = d["values"].get(name, dp.static(name))
        rows.append({"name": name, "value": value, "default": dp.static(name), "lo": lo, "hi": hi, "n": d["evidence"].get(name, 0),
                     "need": dp.static("min_evidence") if name == "tol_share" else 1,          # open_scale / block_after_fail move from the first sign
                     "learned": name in d["values"], "doc": doc})
    return rows, d["notes"]


def dealers_view(mem):
    sa = _agent()
    negs = (mem.get("observed") or {}).get("negotiations") or []
    keys = sorted({n.get("dealer", "abuela") for n in negs} | set(mem.get("dealer_traits") or {}))
    menu = mem.get("dealer_menu") or {}
    out = []
    for key in keys:
        base = key[:-4] if key.endswith("_buy") else key
        mine = [n for n in negs if n.get("dealer", "abuela") == key]
        lp = (menu.get(base) or {}).get("pack:sobre_barrio") if key == "abuela" else None
        hints = sa.profile_hints({"observed": {"negotiations": negs}}, key)
        inf = sa.infer({"observed": {"negotiations": negs}}, key)
        traits = (mem.get("dealer_traits") or {}).get(base)
        rows, notes = _param_rows({"observed": {"negotiations": negs}}, key, lp)
        out.append({
            "key": key, "name": _label(key),
            "negotiations": len(mine), "deals": sum(1 for n in mine if n.get("outcome") == "deal"),
            "cooloffs": sum(1 for n in mine if n.get("closed_reason") == "cooloff"),
            "rounds": sum(len(n.get("rounds") or []) for n in mine),
            "settle": {k: hints.get(k) for k in ("settle_n", "settle_min", "settle_max")},
            "rejected": hints.get("rejected"),
            "suggested": {k: hints.get(k) for k in ("open_bid", "open_ask", "soft_cap")},
            "paid_median": inf.get("paid_median"), "best_k": inf.get("best_k"), "response": inf.get("response_ratio") or {},
            "traits": traits, "prior_mult": sa.trait_multiplier(traits) if traits else None,
            "list_prices": menu.get(base) or {},
            "params": rows, "notes": notes,
        })
    return out


def duels_view(duels_mem):
    sa = _agent_duels()
    duels = (duels_mem or {}).get("duels") or {}
    mem = {"duels": duels}
    roles = {}
    for role in ("seller", "buyer"):
        p = sa.rival_profile(mem, role=role)
        roles[role] = dict(p, prior=sa.rival_prior(mem, None, role, 100.0))
    aliases = sorted({d["observed"].get("rival") for d in duels.values() if (d.get("observed") or {}).get("rival")})
    return {"roles": roles, "total": len(duels), "with_alias": sum(1 for d in duels.values() if (d.get("observed") or {}).get("rival")),
            "aliases": [dict(sa.rival_profile(mem, alias=a), alias=a) for a in aliases],
            "thresholds": {"alias_moves": sa.PRIOR_ALIAS_MOVES, "role_moves": sa.PRIOR_ROLE_MOVES}}


def _agent_duels():
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    return importlib.import_module("smart_duels")


def market_view(intel_state, top=6):
    """Summary of market_intel.json (written by trading_v2.py): who bids, which cards are liquid, who looks like a builder."""
    if not intel_state:
        return None
    from bz.trading.intel import MarketIntel
    intel = MarketIntel(intel_state)
    refs = sorted(set(intel.bid_hist) | set(intel.gone), key=lambda r: -intel.liquidity(r))[:top]
    sets = sorted({r.split("-")[0] for r in intel.bid_hist})
    return {"tick": intel.tick, "makers": len(intel.makers), "cards_with_bids": len(intel.bid_hist),
            "liquid": [{"ref": r, "liquidity": round(intel.liquidity(r), 2), "bids": len(intel.bid_hist.get(r, [])),
                        "gone": len(intel.gone.get(r, [])), "estimate": (intel.buyer_estimate(r) or {}).get("price")} for r in refs],
            "builders": {s: intel.likely_builders(s) for s in sets if intel.likely_builders(s)}}


def learning_view(mem=None, duels_mem=None, intel_state=None):
    mem = mem if isinstance(mem, dict) else {}
    try:
        dealers = dealers_view(mem)
    except Exception as e:                                      # a broken memory must never take the dashboard down
        dealers = []
    try:
        duels = duels_view(duels_mem if isinstance(duels_mem, dict) else {})
    except Exception:
        duels = None
    try:
        market = market_view(intel_state if isinstance(intel_state, dict) else None)
    except Exception:
        market = None
    overrides = {k: dp.static(k) for k in dp.SPEC if dp.static(k) != dp.SPEC[k][0]}
    return {"learning_enabled": dp.learning_enabled(), "overrides": overrides, "dealers": dealers, "duels": duels, "market": market}
