"""Trading V2 runner: one process, one decision per tick for ALL peer-to-peer trading.

    BAZAAR_KEY=tk-... python trading_v2.py                  # follows the mode in .trading_v2_mode / TRADING_V2 (default off)
    python trading_v2.py --selftest                         # offline checks (the real tests live in bz/tests/)

Modes (bz/trading/mode.py, re-read every tick):
    off     does nothing (the legacy market code trades).
    shadow  reads the boards, decides, and LOGS what it would do to logs/trading_v2_shadow.jsonl. No write against Bazaar.
    on      executes; smart_agent.phase_market and page_hunter step aside, so this is the only trading authority.

Reads per tick: clock, me, venues, my_offers, El Rastro every tick and a few other venues in rotation (never our own), plus at
most `value_calls` b.value() calls. Accepts go through the shared accept gate (bz/core/accept_gate.py).
"""
import json
import os
import random
import sys
import time

from bazaar_sdk import Bazaar, BazaarError
from bz.trading import policy
from bz.trading.engine import V2Valuer, decide, execute
from bz.trading.intel import MarketIntel
from bz.trading.mode import get_mode

URL = os.environ.get("BAZAAR_URL", "https://bazaar.causaprima.ai")
INTEL_FILE = os.environ.get("MARKET_INTEL_FILE", "market_intel.json")
SHADOW_LOG = os.environ.get("TRADING_V2_LOG", os.path.join("logs", "trading_v2_shadow.jsonl"))
VENUES_PER_TICK = 3
BOARD_SPACING = 0.15            # seconds between board reads: keep inside the 5 requests/second limit
CATALOG_TTL = 30                # ticks


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def new_ctx():
    return {"intel": MarketIntel.load(INTEL_FILE), "deals": [], "skip_venues": set(), "rr": 0, "catalog": None, "catalog_tick": -999}


def read_boards(b, me, venues, ctx):
    """Always El Rastro, plus VENUES_PER_TICK others in rotation. Returns ({vid: offers}, {vid: fees})."""
    open_v = [v for v in venues if v.get("status", "open") == "open" and v.get("owner") != me["id"]
              and (v.get("venue") or v.get("id")) not in ctx["skip_venues"]]
    fees = {(v.get("venue") or v.get("id")): {"fee_bps": v.get("fee_bps", 0), "fee_per_card": v.get("fee_per_card", 0)} for v in venues}
    others = [v for v in open_v if (v.get("venue") or v.get("id")) != "rastro"]
    pick = []
    if others:
        for k in range(min(VENUES_PER_TICK, len(others))):
            pick.append(others[(ctx["rr"] + k) % len(others)])
        ctx["rr"] = (ctx["rr"] + VENUES_PER_TICK) % len(others)
    wanted = ["rastro"] + [v.get("venue") or v.get("id") for v in pick]
    boards = {}
    for i, vid in enumerate(wanted):
        if i:
            time.sleep(BOARD_SPACING)
        try:
            boards[vid] = [dict(o, venue=o.get("venue") or vid) for o in b.board(vid).get("offers", [])]
        except BazaarError as e:
            log(f"v2: board {vid}: {e.code}")
    return boards, fees


def step(b, ctx, mode, cfg=None):
    """One tick. Returns the plan (and, in `on` mode, the execution result under plan['result'])."""
    cfg = cfg or policy.cfg_with(arb=os.environ.get("TRADING_V2_ARB") == "1")
    clock = b.clock()
    if clock.get("paused"):
        return None
    me = b.me()
    me.setdefault("tick", clock.get("tick", 0))              # /api/me carries the tick; fall back to the clock if it does not
    if ctx["catalog"] is None or me["tick"] - ctx["catalog_tick"] >= CATALOG_TTL:
        ctx["catalog"], ctx["catalog_tick"] = b.catalog(), me["tick"]
    venues = b.venues().get("venues") or [{"venue": "rastro", "fee_bps": 500, "fee_per_card": 1}]
    my_offers = b.my_offers().get("offers", [])
    boards, fees = read_boards(b, me, venues, ctx)
    offers = {}
    for offs in boards.values():
        for o in offs:
            offers.setdefault(o["id"], o)
    intel = ctx["intel"]
    intel.observe(me["tick"], list(offers.values()), me["id"], fresh_venues=set(boards))
    valuer = V2Valuer(b, me, ctx["catalog"], cfg["value_calls"], shared=ctx.setdefault("value_cache", {}))
    snap = {"me": me, "catalog": ctx["catalog"], "venues": fees, "boards": boards, "my_offers": my_offers, "clock": clock,
            "valuer": valuer, "deals": ctx["deals"], "now": None}
    plan = decide(snap, cfg, intel, random.Random(me["tick"] * 7919 + 13))
    plan["mode"] = mode
    if mode == "on":
        plan["result"] = execute(b, plan, ctx, cfg, log)
    _shadow_log(plan, mode)
    if me["tick"] % 5 == 0:
        try:
            intel.save(INTEL_FILE)
        except OSError as e:
            log("v2: cannot save market intel:", e)
    return plan


def _shadow_log(plan, mode):
    keep = {k: plan[k] for k in ("tick", "mode", "pressure", "spendable", "candidates", "accept", "list", "cancel", "bids", "skipped")}
    if "result" in plan:
        r = dict(plan["result"])
        r["accepted"] = bool(r["accepted"])
        keep["result"] = r
    try:
        os.makedirs(os.path.dirname(SHADOW_LOG) or ".", exist_ok=True)
        with open(SHADOW_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(keep, ensure_ascii=False) + "\n")
    except OSError:
        pass
    if mode == "shadow":
        acc = plan["accept"]
        log(f"v2 shadow tick {plan['tick']}: accept={acc and (acc['kind'], acc['ref'], acc['price'], acc['gain'])} "
            f"list={len(plan['list'])} bids={len(plan['bids'])} cancel={len(plan['cancel'])} candidates={plan['candidates']}")


def run():
    b = Bazaar(URL, os.environ["BAZAAR_KEY"], wait_on_tick=False)
    ctx = new_ctx()
    log("trading_v2 started; mode is read every tick (off|shadow|on)")
    last_mode = None
    while True:
        try:
            mode = get_mode()
            if mode != last_mode:
                log(f"v2 mode: {mode}")
                last_mode = mode
            if mode != "off":
                step(b, ctx, mode)
            b.wait_tick()
        except KeyboardInterrupt:
            break
        except BazaarError as e:
            log("v2 api error:", e.code, e.message)
            time.sleep(3)
        except Exception as e:                                    # keep the loop alive; the error is in the log
            log(f"v2 error {type(e).__name__}: {e}")
            time.sleep(5)


def selftest():
    """Runs the offline test files of this component (bz/tests/test_trading_v2_*.py) with pytest."""
    import pytest
    sys.exit(pytest.main(["-q", os.path.join(os.path.dirname(os.path.abspath(__file__)), "bz", "tests"), "-k", "trading_v2"]))


if __name__ == "__main__":
    selftest() if "--selftest" in sys.argv else run()
