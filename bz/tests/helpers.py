"""Test doubles and builders shared by the P0 safety suite.

Nothing here talks to the network: every `b` is a fake that records the calls an agent makes.
"""
import copy
import importlib
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def prod(name):
    """Import a production module (smart_agent, smart_duels, page_hunter, broker) from the repo root."""
    return importlib.import_module(name)


# ------------------------------------------------------------------ offer / thread builders (real shapes, see fixtures)

def side(cash=0, assets=None, types=None):
    return {"cash": cash, "assets": assets or [], "types": types or []}


def pack_asset(ref="sobre_barrio"):
    return {"kind": "pack", "ref": ref, "id": 900}


def card_asset(ref="LAV-09", asset_id=901):
    return {"kind": "card", "ref": ref, "id": asset_id}


def standing(offer_id, maker, give, want, final=False, status="open"):
    return {"id": offer_id, "maker": maker, "status": status, "give": give, "want": want, "final": final}


def buy_thread(tid, me_id, ours, hers, standing_offers, dealer="abuela", status="open",
               topic=None, closed_reason=None):
    """A thread where WE BUY: we offer cash (give.cash), the dealer asks cash (want.cash).
    ours: [price]; hers: [(price, final)] or [(price, final, text)]; messages alternate ours/hers."""
    msgs = []
    for k in range(max(len(ours), len(hers))):
        if k < len(ours):
            msgs.append({"sender": me_id, "text": "", "offer": {"give": side(ours[k]), "want": side(0)}})
        if k < len(hers):
            h = hers[k]
            price, final, text = (h + ("",))[:3] if len(h) == 2 else h
            msgs.append({"sender": dealer, "text": text,
                         "offer": {"give": side(0), "want": side(price), "final": bool(final)}})
    return {"id": tid, "with": dealer, "status": status, "closed_reason": closed_reason,
            "topic": topic or {"buy": {"pack": "sobre_barrio"}}, "messages": msgs,
            "standing_offers": standing_offers}


def sell_thread(tid, me_id, ours, hers, standing_offers, dealer="chato", status="open", topic=None):
    """A thread where WE SELL: we ask cash (want.cash), the dealer bids cash (give.cash)."""
    msgs = []
    for k in range(max(len(ours), len(hers))):
        if k < len(ours):
            msgs.append({"sender": me_id, "text": "", "offer": {"want": side(ours[k]), "give": side(0)}})
        if k < len(hers):
            h = hers[k]
            price, final, text = (h + ("",))[:3] if len(h) == 2 else h
            msgs.append({"sender": dealer, "text": text,
                         "offer": {"give": side(price), "want": side(0), "final": bool(final)}})
    return {"id": tid, "with": dealer, "status": status, "closed_reason": None,
            "topic": topic or {"sell": {"assets": [9]}}, "messages": msgs, "standing_offers": standing_offers}


# ------------------------------------------------------------------ fake dealer API

class FakeDealerAPI:
    """Serves one thread and records every call. `drift` models a dealer that changes its standing offer after the agent
    has read it: the first `after_reads` reads see the original offer; from then on (including the moment accept()
    arrives) the open standing offer `offer_id` holds `new_cash` instead.

    `server_price_at_accept` is what the SERVER would charge at the moment accept() arrives."""

    def __init__(self, thread, offer_id=None, drift=None, fail_after_reads=None):
        self._thread = thread
        self.reads = 0
        self.calls = []
        self.accepts = []                 # (offer_id, price the server holds at that moment)
        self.closed = []
        self.said = []
        self.offer_id = offer_id
        self.drift = drift                # {"after_reads": int, "new_cash": int, "field": "want"|"give"} or {"after_reads": int, "remove": True}
        self.fail_after_reads = fail_after_reads   # thread() raises a network BazaarError from the read after this many

    def _current(self, drifted=False):
        t = copy.deepcopy(self._thread)
        if self.drift and drifted:
            if self.drift.get("remove"):
                t["standing_offers"] = [o for o in t["standing_offers"] if o["id"] != self.offer_id]
            for o in t["standing_offers"]:
                if o["id"] == self.offer_id:
                    o[self.drift["field"]]["cash"] = self.drift["new_cash"]
        return t

    def thread(self, tid):
        self.reads += 1
        self.calls.append(("thread", tid))
        if self.fail_after_reads is not None and self.reads > self.fail_after_reads:
            raise prod("bazaar_sdk").BazaarError("network", "simulated read failure", 0)
        return self._current(drifted=self.reads > self.drift["after_reads"] if self.drift else False)

    def accept(self, offer_id, assets=None):
        self.calls.append(("accept", offer_id))
        # the dealer moves right after the agent's `after_reads`-th read, so by now (accept time) it has moved
        t = self._current(drifted=self.reads >= self.drift["after_reads"] if self.drift else False)
        o = next(o for o in t["standing_offers"] if o["id"] == offer_id)
        field = self.drift["field"] if self.drift and "field" in self.drift else ("want" if t["topic"].get("buy") else "give")
        self.accepts.append((offer_id, o[field]["cash"]))
        return {"ok": True}

    def close_thread(self, tid):
        self.calls.append(("close_thread", tid))
        self.closed.append(tid)
        return {"ok": True}

    def say(self, tid, text="", price=None, offer=None, topic=None):
        self.calls.append(("say", tid, price))
        self.said.append(price)
        return {"ok": True}

    def open_thread(self, with_, topic=None, venue=None):          # must not be needed when a thread is already open
        self.calls.append(("open_thread", with_))
        raise AssertionError("the agent opened a new thread")

    def open_pack(self, asset_id):
        self.calls.append(("open_pack", asset_id))
        return {"cards": []}


def me_state(tid, cash=100, unlocked=("chato",), tick=10):
    return {"id": "t06", "tick": tick, "cash": cash, "assets": [], "open_threads": [tid], "affinity": {},
            "unlocked": list(unlocked)}


# ------------------------------------------------------------------ fake market (El Rastro) shared by several agents

class FakeMarketServer:
    """One shared 'server' for the market: enforces one accept per tick for the whole team (as the real one does) and
    COUNTS every accept ATTEMPT, so a test can see how many requests reached the API."""

    def __init__(self, offers, cash=500, tick=100, values=None):
        self.offers = offers
        self.cash = cash
        self.tick = tick
        self.values = values or {}
        self.accept_attempts = []         # offer ids, every call
        self.listings = []
        self.accepted_this_tick = False

    def client(self):
        return _MarketClient(self)


class _MarketClient:
    def __init__(self, server):
        self.s = server

    def venues(self):
        return {"venues": [{"venue": "rastro", "fee_bps": 500, "fee_per_card": 1}]}

    def board(self, venue="rastro"):
        return {"offers": copy.deepcopy(self.s.offers)}

    def my_offers(self):
        return {"offers": []}

    def value(self, card):
        return {"card": card, "your_value": self.s.values.get(card)}

    def accept(self, offer_id, assets=None):
        self.s.accept_attempts.append(offer_id)
        if self.s.accepted_this_tick:
            raise _error("wait_for_tick")
        self.s.accepted_this_tick = True
        return {"ok": True}

    def list_offer(self, give, want, venue=None, to=None, expires_in_ticks=40):
        self.s.listings.append((give, want, venue))
        return {"id": 1}


def _error(code):
    return prod("bazaar_sdk").BazaarError(code, "", 429)


def sell_offer(offer_id, ref, cash, maker="rival_01", asset_id=None):
    return {"id": offer_id, "maker": maker, "to": None, "status": "open",
            "give": side(0, assets=[{"kind": "card", "ref": ref, "id": asset_id or offer_id}]), "want": side(cash)}


def bid_offer(offer_id, ref, cash, maker="rival_01"):
    return {"id": offer_id, "maker": maker, "to": None, "status": "open",
            "give": side(cash), "want": side(0, types=[f"card:{ref}"])}


def toy_catalog():
    """Same layout page_hunter's own selftest uses: 5 commons, 3 uncommons, 2 rares, epic, legendary per set."""
    layout = [(10, "common")] * 5 + [(25, "uncommon")] * 3 + [(70, "rare")] * 2 + [(180, "epic"), (450, "legendary")]
    cards = lambda sid: [{"id": f"{sid}-{i + 1:02d}", "book": bk, "page": i < 10, "rarity": r, "name": f"{sid}{i}"}
                         for i, (bk, r) in enumerate(layout)]
    return {"sets": [{"id": "LAV", "released": True, "cards": cards("LAV")},
                     {"id": "LAT", "released": True, "cards": cards("LAT")}],
            "values": {"page_bonus": 0.25, "copy_marginals": [1, 0.25, 0.1]},
            "packs": [{"id": "sobre_barrio", "slots": [{"common": 1.0}, {"common": 1.0}]}]}


def me_with_cards(refs, cash=500, affinity=None):
    return {"id": "t06", "tick": 100, "cash": cash, "affinity": affinity or {"LAV": 1.6, "LAT": 0.7},
            "assets": [{"id": 100 + i, "kind": "card", "ref": r, "serial": i + 1, "rarity": "common"}
                       for i, r in enumerate(refs)]}


# ------------------------------------------------------------------ duels

class FakeDuelAPI:
    def __init__(self):
        self.said = []                    # (price, days)
        self.accepted = []                # rival price at the moment of accepting

    def duel_say(self, rid, text="", price=None, days=None):
        self.said.append((price, days))
        return {"ok": True}

    def duel_accept(self, rid):
        self.accepted.append(rid)
        return {"ok": True}


def new_duel_mem():
    return {"duels": {}, "finished": {}, "raw_samples": []}


def run_duel(role, limit, rival, start_tick=100, total=16, issues=("price",), w=None, rival_days=None, mem=None, alias=None):
    """Play one duel tick by tick against a scripted rival through the real `smart_duels.act`.
    rival: list of rival prices per tick (None = silent). Returns (said, accepted_rival_price, mem)."""
    sd = prod("smart_duels")
    b, accepted_price = FakeDuelAPI(), None
    mem = new_duel_mem() if mem is None else mem
    for k, rp in enumerate(rival):
        tick = start_tick + k
        duel = {"duel": 1, "session": 1, "status": "live", "role": role, "your_limit": limit, "issues": list(issues),
                "your_days_weight": w, "deadline_tick": start_tick + total, "decay_per_round": 0.06, "rival": alias,
                "rival_offer": None if rp is None else ({"price": rp, "days": rival_days} if rival_days is not None else {"price": rp})}
        n_acc = len(b.accepted)
        sd.act(b, duel, tick, mem)
        if len(b.accepted) > n_acc:
            accepted_price = rp
            break
    return b.said, accepted_price, mem
