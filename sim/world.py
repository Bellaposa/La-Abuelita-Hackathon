"""Offline fake of The Bazaar server: FakeBazaar duck-types bazaar_sdk.Bazaar, FakeBroker duck-types Broker.

Deterministic per seed. Time is manual: nothing advances until you call wait_tick() / advance().
See sim/README.md for shapes and assumptions.
"""
from __future__ import annotations

import os
import random
import sys

try:
    from bazaar_sdk import BazaarError
except ImportError:  # run from outside the repo root
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from bazaar_sdk import BazaarError

from .rivals import RIVALS, make_rival

__all__ = ["FakeBazaar", "FakeBroker", "BazaarError", "SimDone", "CATALOG_SETS", "BOOK", "PACKS"]


class SimDone(BaseException):
    """Raised by wait_tick() when the episode's tick budget is spent (BaseException so agents' `except Exception`
    loops do not swallow it)."""


# ------------------------------------------------------------------------------------------------ static data
RAR = ["common"] * 5 + ["uncommon"] * 3 + ["rare"] * 2 + ["epic", "legendary"]
PRINT_RUN = {"common": 300, "uncommon": 90, "rare": 30, "epic": 9, "legendary": 3}
BOOK = {"common": 10, "uncommon": 25, "rare": 77, "epic": 200, "legendary": 500}
CATALOG_SETS = [("LAV", "Lavapies", True), ("MAL", "Malasana", True), ("LAT", "La Latina", True),
                ("SAL", "Salamanca", True), ("RET", "El Retiro", False), ("CHA", "Chamberi", False)]
PACKS = {
    "sobre_barrio": dict(name="Neighbourhood pack", list=26, ask=30, n=3, odds=(0.90, 0.09, 0.01, 0, 0)),
    "sobre_plata": dict(name="Silver pack", list=150, ask=188, n=5, odds=(0.50, 0.35, 0.13, 0.02, 0)),
}
RARITY_ORDER = ["common", "uncommon", "rare", "epic", "legendary"]
FEE_BPS, FEE_PER_CARD = 500, 1  # El Rastro
DUP_FACTOR = 0.3                # a duplicate copy is worth 30 % to its owner
DEFAULT_LIMITS = {"accepts_per_tick": 1, "messages_per_thread_per_tick": 1, "listings_per_tick": 12,
                  "max_threads": 6, "max_offers": 30}

DEALER_CFG = {
    "abuela": dict(
        name="Abuela Carmen", level=1, title="card stall at El Rastro, since 1985",
        traits=dict(patience=0.85, generosity=0.8, shrewdness=0.2, memory=0.15, strictness=0.1, chattiness=0.75),
        sells_rar=("common", "uncommon"), buys_rar=("common", "uncommon"), packs=("sobre_barrio",),
        pack_quota=3, deals_per_hour=8,
        floor_mult=(0.80, 0.86), card_open=1.16, buy_open=0.55, buy_cap=(0.62, 0.80),
        patience=(5, 7), accept_margin=1, k=0.9, thr=0.0, repeat_walk=3, cooloff_ticks=30, kind=True),
    "chato": dict(
        name="El Chato", level=2, title="Rastro regular, silver packs and rare singles",
        traits=dict(patience=0.35, generosity=0.25, shrewdness=0.85, memory=0.9, strictness=0.85, chattiness=0.3),
        sells_rar=("uncommon", "rare"), buys_rar=("uncommon", "rare"), packs=("sobre_plata",),
        pack_quota=2, deals_per_hour=6,
        floor_mult=(1.00, 1.10), card_open=1.25, buy_open=0.50, buy_cap=(0.50, 0.60),
        patience=(3, 4), accept_margin=0, k=0.35, thr=0.10, repeat_walk=2, cooloff_ticks=60, kind=False),
}
POLITE = ("gracias", "thank", "please", "por favor", "hola", "hello", "amable", "kind", "buenos", "sol")


def _side(cash=0, assets=(), types=()):
    return {"cash": int(cash), "assets": list(assets), "types": list(types)}


def card_ids():
    return [(s, f"{s}-{i + 1:02d}", RAR[i]) for s, _n, _r in CATALOG_SETS for i in range(12)]


def pack_expected_book(pid):
    p = PACKS[pid]
    return round(p["n"] * sum(o * BOOK[r] for o, r in zip(p["odds"], RARITY_ORDER)), 2)


# ------------------------------------------------------------------------------------------------ dealers
class DealerModel:
    """Generic haggling dealer (Abuela and El Chato differ only by cfg). Direction s=+1: dealer sells, we buy
    (p rises toward the ask); s=-1: dealer buys, we sell."""

    def __init__(self, world, did):
        self.w, self.did, self.cfg = world, did, DEALER_CFG[did]
        self.grudge_until = 0

    # -- setup of the hidden state of one thread
    def start(self, th):
        w, c, topic = self.w, self.cfg, th["topic"]
        rng = w.rng
        if "buy" in topic:
            s, b = 1, topic["buy"]
            if "pack" in b:
                lst, opn = PACKS[b["pack"]]["list"], PACKS[b["pack"]]["ask"]
            else:
                lst = BOOK[w.rarity_of(b["card"])]
                opn = round(lst * c["card_open"])
            floor = round(lst * rng.uniform(*c["floor_mult"]))
            if self.did == "abuela" and w.beginner_discount and not w.deals:
                floor, opn = 16, 17
            if self.w.tick < self.grudge_until:
                floor = round(floor * 1.03)
        else:
            s = -1
            lst = sum(BOOK[w.assets[a]["rarity"]] for a in topic["sell"]["assets"])
            opn = max(1, round(lst * c["buy_open"]))
            floor = max(opn, round(lst * rng.uniform(*c["buy_cap"])))  # the most the dealer will ever pay
        pat = rng.randint(*c["patience"]) - (1 if self.w.tick < self.grudge_until else 0)
        th["_s"] = dict(dir=s, list=lst, open=opn, floor=floor, ask=None, best=None, rounds=0, repeats=0,
                        final=False, patience=max(2, pat), posted=False, last_text=None)
        self._extra_start(th)

    def _extra_start(self, th):
        pass

    def item_name(self, th):
        b = th["topic"].get("buy")
        if b:
            return PACKS[b["pack"]]["name"] if "pack" in b else b["card"]
        return "your cards"

    # -- one reaction per tick for a thread with a pending message of ours (or the opening)
    def react(self, th, inbox, tick):
        s, c = th["_s"], self.cfg
        d = s["dir"]
        p = inbox["price"] if inbox else None
        if not s["posted"]:
            s["posted"], s["ask"] = True, s["open"]
            if p is not None and self._ok(s, p):
                return dict(deal=p, text="Done, hijo.")
            if p is not None:
                s["best"] = p
            return dict(ask=s["ask"], text=self._say("open", s, th), final=False)
        if inbox is None:
            return None
        if p is None:  # words only: chatter, nothing moves
            return dict(text="Mm-hm. Tell me a price, hijo." if c["kind"] else "Price.")
        if s["final"]:
            if self._ok(s, p):
                return dict(deal=p, text="Done.")
            return dict(close=("walked", "walked", None), text="Then we are done here.")
        if self._ok(s, p):
            return dict(deal=p, text="Deal, hijo.")
        gap = abs(s["ask"] - p)
        if s["best"] is None:  # first priced message after her opening: a small first step
            delta, progress = max(1, round(c["k"] * 0.15 * gap)) / c["k"], True
            s["best"] = p
        else:
            delta = d * (p - s["best"])
            progress = delta > 0 and (delta >= c["thr"] * gap)
            if delta > 0:
                s["best"] = p
        if not progress:
            s["repeats"] += 1
            same = inbox["text"].strip().lower() == (s["last_text"] or "")
            s["last_text"] = inbox["text"].strip().lower()
            if s["repeats"] >= c["repeat_walk"]:
                if same or c["traits"]["strictness"] > 0.5:
                    return dict(close=("cooloff", "cooloff", tick + c["cooloff_ticks"]),
                                text="Same words again? We are finished.")
                return dict(close=("walked", "no_progress", None), text="You are not getting anywhere, hijo.")
            return dict(ask=s["ask"], text=self._say("repeat", s, th), final=False, repost=True)
        s["repeats"] = 0
        s["last_text"] = inbox["text"].strip().lower()
        room = abs(s["ask"] - s["floor"]) / max(1, abs(s["open"] - s["floor"]))
        step = c["k"] * delta * (0.5 + 0.5 * min(1.0, room))
        if c["kind"] and any(wd in inbox["text"].lower() for wd in POLITE) and self.w.rng.random() < 0.5:
            step += 1
        step = max(1, round(step))
        new = s["ask"] - d * step
        if d * (new - s["floor"]) < 0:
            new = s["floor"]
        s["ask"], s["rounds"] = int(new), s["rounds"] + 1
        if s["rounds"] >= s["patience"] or s["ask"] == s["floor"]:
            s["final"] = True
        return dict(ask=s["ask"], text=self._say("final" if s["final"] else "move", s, th), final=s["final"])

    def _ok(self, s, p):
        d, m = s["dir"], self.cfg["accept_margin"]
        return d * (s["ask"] - p) <= m and d * (p - s["floor"]) >= 0

    def _say(self, kind, s, th):
        a, it = s["ask"], self.item_name(th)
        if not self.cfg["kind"]:
            return {"open": f"{it}: {a}.", "repeat": "Same price. No.", "move": f"{a}.",
                    "final": f"{a}. Last one."}[kind]
        return {"open": f"Welcome, hijo! Look, {it}, {a} P. Made for beginners.",
                "repeat": f"Same price again, cielo? Still {a} P.",
                "move": f"Oh, oh... {a} P, and only because you have a kind face.",
                "final": f"My last word, hijo: {a} P. Take it or I walk."}[kind]


class RivalDealer(DealerModel):
    """A dealer whose haggling follows one of the training rivals (sim.rivals)."""

    def __init__(self, world, did, rival_name):
        super().__init__(world, did)
        self.rival_name = rival_name

    def _extra_start(self, th):
        s = th["_s"]
        role = "seller" if s["dir"] > 0 else "buyer"
        T = 8
        s["rival"] = make_rival(self.rival_name, role, s["floor"], random.Random(self.w.rng.random()),
                                ticks=T, ref=s["list"], seed=self.w.seed)
        s["T"], s["t"] = T, 0
        s["posted"] = False
        s["patience"] = T

    def react(self, th, inbox, tick):
        s, riv = th["_s"], th["_s"]["rival"]
        p = inbox["price"] if inbox else None
        if not s["posted"]:
            r = riv.respond(0, None)
            s["posted"], s["ask"], s["open"], s["t"] = True, r["price"], r["price"], 1
            return dict(ask=r["price"], text=r["text"], final=r["final"])
        if inbox is None:
            return None
        if p is None:
            return dict(text="Say a price.")
        if s["t"] > s["T"]:
            return dict(close=("walked", "walked", None), text="Time is up.")
        r = riv.respond(s["t"], p)
        s["t"] += 1
        if r["action"] == "accept":
            return dict(deal=p, text="Deal.")
        s["ask"] = r["price"]
        return dict(ask=r["price"], text=r["text"], final=r["final"])


# ------------------------------------------------------------------------------------------------ the world
class FakeBazaar:
    """Drop-in for bazaar_sdk.Bazaar (the methods agents use). Extra constructor knobs:

    seed, tick_seconds, team ("t00"), dealer_style ("abuela"|rival name: what drives dealer id 'abuela'),
    chato_unlocked, beginner_discount, auto_wait (sleep-through wait_for_tick like the real SDK default),
    max_ticks (wait_tick raises SimDone after it), duel (dict, see add_duel), limits.
    """

    def __init__(self, seed=0, tick_seconds=30, team="t00", dealer_style="abuela", chato_unlocked=False,
                 beginner_discount=False, auto_wait=False, max_ticks=None, duel=None, limits=None, cash=400,
                 market=True):
        self.seed, self.rng = seed, random.Random(seed)
        self.tick, self.tick_seconds, self.team = 0, tick_seconds, team
        self.auto_wait, self.max_ticks = auto_wait, max_ticks
        self.beginner_discount = beginner_discount
        self.limits = {**DEFAULT_LIMITS, **(limits or {})}
        self.cash, self.start_cash = cash, cash
        self._nid = 1000
        self._mid = 1
        self.assets: dict[int, dict] = {}
        self.threads: dict[int, dict] = {}
        self.offers: dict[int, dict] = {}
        self.deals: list[dict] = []        # settled dealer deals (ground truth, for scoring)
        self.feed_log: list[dict] = []
        self.minted: dict[str, int] = {}
        self._used: dict = {}
        self._pending: list = []
        self._opens: dict = {}
        self._cool: dict = {}
        self._brokers: list = []
        self.neg_deals = 0
        self.chato_unlocked = chato_unlocked
        ms = [0.7, 0.85, 1.0, 1.15, 1.3, 1.5]
        self.rng.shuffle(ms)
        self.mult = {s: m for (s, _n, _r), m in zip(CATALOG_SETS, ms)}
        self.cards = {cid: (s, r) for s, cid, r in card_ids()}
        self.dealer_models = {"abuela": DealerModel(self, "abuela") if dealer_style == "abuela"
                              else RivalDealer(self, "abuela", dealer_style),
                              "chato": DealerModel(self, "chato")}
        self.dealer_style = dealer_style
        self.duel_list: list[dict] = []
        self._deal_hour_log: list = []
        self._deal_dealer = {}
        # starting hand: 11 commons, 3 uncommons, 1 rare
        rel = [s for s, _n, r in CATALOG_SETS if r]
        for rar, n in (("common", 11), ("uncommon", 3), ("rare", 1)):
            for _ in range(n):
                cid = self.rng.choice([c for c, (s, r) in self.cards.items() if r == rar and s in rel])
                self._mint(cid)
        self.market = market
        self.npc_offers: list[int] = []
        if market:
            self._npc_refresh()
        self.start_worth = self.net_worth()
        if duel is not None:
            self.add_duel(**duel)

    # ------------------------------------------------------------------ helpers
    def _id(self):
        self._mid += 1
        return self._mid

    def rarity_of(self, ref):
        return self.cards[ref][1]

    def _mint(self, cid, to_me=True):
        s, r = self.cards[cid]
        self._nid += 1
        self.minted[cid] = self.minted.get(cid, 0) + 1
        a = dict(id=self._nid, kind="card", ref=cid, name=f"Card {cid}", rarity=r, set=s,
                 serial=self.rng.randint(1, PRINT_RUN[r]), print_run=PRINT_RUN[r])
        if to_me:
            self.assets[a["id"]] = a
        return a

    def _hour(self):
        return int(self.tick * self.tick_seconds // 3600)

    def _gate_check(self, key, limit):
        while self._used.get((key, self.tick), 0) >= limit:
            if self.auto_wait:
                self.wait_tick()
                continue
            raise BazaarError("wait_for_tick", "one per tick; wait for the next tick", 429,
                              {"next_tick": self.tick + 1, "next_tick_in": float(self.tick_seconds)})

    def _gate_use(self, key, n=1):
        self._used[(key, self.tick)] = self._used.get((key, self.tick), 0) + n

    def card_value(self, ref, held=0):
        s, r = self.cards[ref]
        v = BOOK[r] * self.mult[s]
        return round(v * (DUP_FACTOR if held > 0 else 1.0), 2)

    def _asset_values(self):
        out, seen = {}, {}
        for a in sorted(self.assets.values(), key=lambda a: (a["serial"], a["id"])):
            if a["kind"] == "pack":
                rel = [self.mult[s] for s, _n, r in CATALOG_SETS if r]
                out[a["id"]] = round(pack_expected_book(a["ref"]) * sum(rel) / len(rel) * 0.95, 2)
            else:
                out[a["id"]] = self.card_value(a["ref"], seen.get(a["ref"], 0))
                seen[a["ref"]] = seen.get(a["ref"], 0) + 1
        return out

    def net_worth(self):
        """Cash + private value of everything held (what the sim uses as 'score')."""
        return round(self.cash + sum(self._asset_values().values()), 2)

    def _held(self, ref):
        return sum(1 for a in self.assets.values() if a["kind"] == "card" and a["ref"] == ref)

    def _pub_asset(self, a, vals):
        d = {k: v for k, v in a.items() if not k.startswith("_")}
        d["your_value"] = vals[a["id"]]
        d["locked"] = any(a["id"] in [x["id"] for x in o["give"]["assets"]] and o["status"] == "open"
                          and o["maker"] == self.team for o in self.offers.values())
        return d

    def _mk_offer(self, maker, to, thread, give, want, final=False, venue=None, expires=2):
        oid = self._id()
        o = dict(id=oid, maker=maker, to=to, venue=venue, thread=thread, status="open", give=give, want=want,
                 expires_tick=self.tick + expires, created_tick=self.tick, final=final)
        self.offers[oid] = o
        return o

    def _log(self, kind, **kw):
        self.feed_log.append(dict(tick=self.tick, kind=kind, **kw))
        del self.feed_log[:-300]

    # ------------------------------------------------------------------ public reads
    def health(self):
        return {"ok": True}

    def clock(self):
        return {"tick": self.tick, "t_hours": round(self.tick * self.tick_seconds / 3600, 4),
                "tick_seconds": self.tick_seconds, "paused": False, "next_tick_in": float(self.tick_seconds),
                "limits": dict(self.limits)}

    def catalog(self):
        sets = []
        for s, n, rel in CATALOG_SETS:
            cards = [dict(id=cid, name=f"Card {cid}", rarity=r, book=BOOK[r], print_run=PRINT_RUN[r],
                          minted=self.minted.get(cid, 0)) for ss, cid, r in card_ids() if ss == s]
            sets.append(dict(id=s, name=n, released=rel, cards=cards))
        packs = [dict(id=k, name=v["name"], price=v["list"], list_price=v["list"], cards=v["n"],
                      odds=dict(zip(RARITY_ORDER, v["odds"])), expected_book=pack_expected_book(k))
                 for k, v in PACKS.items()]
        return {"currency_symbol": "P", "sets": sets, "packs": packs,
                "value_rules": {"duplicate_factor": DUP_FACTOR}}

    def leaderboard(self):
        return {"teams": [{"rank": 1, "team": self.team, "score": self.net_worth() - self.start_worth}],
                "refreshed_tick": self.tick}

    def feed(self, limit=150):
        return {"events": self.feed_log[-limit:]}

    def schedule(self):
        return {"events": [{"kind": "duel", "tick": d["start"], "deadline": d["deadline"]} for d in self.duel_list]}

    def levels(self):
        return {"levels": [{"id": "chato", "kind": "persona", "name": "El Chato",
                            "state": "active", "teaser": "Better packs, friendly prices. If I like you.",
                            "how": "Better packs and rare singles; open a thread with him (with: chato).",
                            "open_to_all": self.chato_unlocked}]}

    def dealers(self):
        out = []
        for did, c in DEALER_CFG.items():
            sells = [dict(pack=p, name=PACKS[p]["name"], list_price=PACKS[p]["list"], opening_ask=PACKS[p]["ask"],
                          per_team_per_hour=c["pack_quota"]) for p in c["packs"]]
            sells += [dict(rarity=r, sets="released", list_price=BOOK[r]) for r in c["sells_rar"]]
            out.append(dict(id=did, name=c["name"], status="active", level=c["level"], title=c["title"],
                            kind="dealer", enabled=True, traits=c["traits"],
                            open_to_all=(did == "abuela" or self.chato_unlocked),
                            menu=dict(sells=sells, buys=[dict(rarity=r, sets="released") for r in c["buys_rar"]],
                                      deals_per_team_per_hour=c["deals_per_hour"])))
        return {"dealers": out}

    personas = dealers

    def dealer(self, dealer_id):
        for d in self.dealers()["dealers"]:
            if d["id"] == dealer_id:
                return d
        raise BazaarError("not_found", f"no dealer {dealer_id}", 404)

    def venues(self):
        return {"venues": [{"venue": "rastro", "name": "El Rastro", "fee_bps": FEE_BPS, "fee_per_card": FEE_PER_CARD}]}

    def call(self, method, path, body=None):
        raise BazaarError("not_implemented", f"{method} {path} is not simulated", 501)

    # ------------------------------------------------------------------ your team
    def me(self):
        vals = self._asset_values()
        unlocked = ["abuela"] + (["chato"] if self.chato_unlocked else [])
        album = {}
        for a in self.assets.values():
            if a["kind"] == "card":
                album.setdefault(a["set"], set()).add(a["ref"])
        return {"id": self.team, "name": f"Team {self.team}", "cash": self.cash,
                "level": 1 + int(self.chato_unlocked), "unlocked": unlocked,
                "assets": [self._pub_asset(a, vals) for a in self.assets.values()],
                "affinity": {s: m for s, m in self.mult.items()},
                "open_threads": [t["id"] for t in self.threads.values() if t["status"] == "open"],
                "album": {s: sorted(v) for s, v in album.items()},
                "score": {"score": round(self.net_worth() - self.start_worth, 2), "rank": 1}}

    def value(self, card):
        if card not in self.cards:
            raise BazaarError("not_found", f"unknown card {card}", 404)
        return {"card": card, "your_value": self.card_value(card, self._held(card))}

    def my_threads(self, status=None):
        return {"threads": [self._pub_thread(t) for t in self.threads.values() if status in (None, t["status"])]}

    def my_offers(self):
        return {"offers": [{k: v for k, v in o.items() if not k.startswith("_")} for o in self.offers.values()
                           if o["status"] == "open" and (o["maker"] == self.team or o["to"] == self.team)]}

    # ------------------------------------------------------------------ threads
    def _pub_thread(self, th):
        d = {k: v for k, v in th.items() if not k.startswith("_")}
        d["standing_offers"] = [self.offers[i] for i in th["_offers"][-24:]]
        return d

    def _quota_key(self, did, topic):
        b = topic.get("buy") or {}
        return (did, self._hour(), "pack" if "pack" in b else "card" if b else "sell")

    def open_thread(self, with_, topic=None, venue=None):
        if with_ not in self.dealer_models:
            raise BazaarError("not_found", f"no counterparty {with_}", 404)
        cfg = DEALER_CFG[with_]
        model = self.dealer_models[with_]
        if with_ == "chato" and not self.chato_unlocked:
            raise BazaarError("locked", "El Chato opens after 3 negotiated deals with Abuela Carmen", 403)
        if self._cool.get(with_, 0) > self.tick:
            raise BazaarError("cooloff", "this dealer is not dealing with you for a while", 403,
                              {"until_tick": self._cool[with_]})
        if any(t["with"] == with_ and t["status"] == "open" for t in self.threads.values()):
            raise BazaarError("thread_exists", "one open conversation per dealer", 409)
        if sum(t["status"] == "open" for t in self.threads.values()) >= self.limits["max_threads"]:
            raise BazaarError("too_many_threads", "too many open conversations", 429)
        topic = topic or {"buy": {"pack": cfg["packs"][0]}}
        self._validate_topic(with_, cfg, topic)
        self._gate_check("open", 10 ** 6)
        tid = self._id()
        th = dict(id=tid, kind="persona", team=self.team, **{"with": with_}, venue=None, topic=topic, status="open",
                  created_tick=self.tick, closed_reason=None, messages=[], _offers=[], _inbox=None,
                  _last_say=-1, _model=model)
        self.threads[tid] = th
        k = self._quota_key(with_, topic)
        self._opens[k] = self._opens.get(k, 0) + 1
        quota = cfg["pack_quota"] if k[2] == "pack" else cfg["deals_per_hour"]
        if self._opens[k] > quota:
            th["status"], th["closed_reason"] = "closed", "persona_quota"
            return self._pub_thread(th)
        model.start(th)
        return self._pub_thread(th)

    def _validate_topic(self, did, cfg, topic):
        b, s = topic.get("buy"), topic.get("sell")
        if b:
            if "pack" in b:
                if b["pack"] not in cfg["packs"]:
                    raise BazaarError("not_sold", f"{did} does not sell {b['pack']}", 400)
            elif "card" in b:
                if b["card"] not in self.cards:
                    raise BazaarError("not_found", f"unknown card {b['card']}", 404)
                if self.rarity_of(b["card"]) not in cfg["sells_rar"]:
                    raise BazaarError("not_sold", f"{did} does not sell that rarity", 400)
            else:
                raise BazaarError("invalid", "buy topic needs pack or card", 400)
        elif s:
            ids = s.get("assets") or []
            if not ids:
                raise BazaarError("invalid", "sell topic needs assets", 400)
            for a in ids:
                if a not in self.assets:
                    raise BazaarError("not_owner", f"asset {a} is not yours", 403)
                if self.assets[a]["kind"] != "card" or self.assets[a]["rarity"] not in cfg["buys_rar"]:
                    raise BazaarError("not_bought", f"{did} does not buy asset {a}", 400)
        else:
            raise BazaarError("invalid", "topic needs buy or sell", 400)

    def thread(self, thread_id):
        th = self.threads.get(int(thread_id))
        if th is None:
            raise BazaarError("not_found", f"no thread {thread_id}", 404)
        return self._pub_thread(th)

    def say(self, thread_id, text="", price=None, offer=None, topic=None):
        th = self.threads.get(int(thread_id))
        if th is None:
            raise BazaarError("not_found", f"no thread {thread_id}", 404)
        if th["status"] != "open":
            raise BazaarError("thread_closed", f"thread is {th['status']}", 409)
        if th["_last_say"] == self.tick and self.auto_wait:
            self.wait_tick()
        if th["_last_say"] == self.tick:
            raise BazaarError("wait_for_tick", "one message per conversation per tick", 429,
                              {"next_tick": self.tick + 1, "next_tick_in": float(self.tick_seconds)})
        text = (text or "")[:1200]
        if offer and price is None and isinstance(offer, dict):
            price = offer.get("price")
        if price is not None:
            price = int(price)
            if not 1 <= price <= 10_000_000:
                raise BazaarError("invalid", "price must be 1..10,000,000", 400)
            if "buy" in th["topic"] and price > self.cash:
                raise BazaarError("insufficient_cash", f"{price} is above your cash {self.cash}", 400)
        th["_last_say"] = self.tick
        mid = self._id()
        off = None
        for oid in th["_offers"]:  # our previous offer is withdrawn
            o = self.offers[oid]
            if o["maker"] == self.team and o["status"] == "open":
                o["status"] = "cancelled"
        if price is not None:
            if "buy" in th["topic"]:
                b = th["topic"]["buy"]
                typ = f"pack:{b['pack']}" if "pack" in b else f"card:{b['card']}"
                off = self._mk_offer(self.team, th["with"], th["id"], _side(price), _side(types=[typ]))
            else:
                alist = [{"id": a, "kind": "card", "ref": self.assets[a]["ref"]} for a in th["topic"]["sell"]["assets"]]
                off = self._mk_offer(self.team, th["with"], th["id"], _side(assets=alist), _side(price))
            th["_offers"].append(off["id"])
        th["messages"].append(dict(id=mid, tick=self.tick, sender=self.team, text=text, offer=off))
        th["_inbox"] = dict(price=price, text=text, tick=self.tick)
        return {"id": mid, "offer": off, "thread": th["id"]}

    def close_thread(self, thread_id):
        th = self.threads.get(int(thread_id))
        if th is None:
            raise BazaarError("not_found", f"no thread {thread_id}", 404)
        if th["status"] == "open":
            th["status"], th["closed_reason"] = "closed", "closed_by_team"
            self._cancel_thread_offers(th)
            m = th["_model"]
            if th["with"] == "chato" and isinstance(m, DealerModel):
                m.grudge_until = self.tick + 60  # El Chato remembers people who walk away
        return self._pub_thread(th)

    def _cancel_thread_offers(self, th):
        for oid in th["_offers"]:
            if self.offers[oid]["status"] == "open":
                self.offers[oid]["status"] = "cancelled"

    # ------------------------------------------------------------------ offers
    def accept(self, offer_id, assets=None):
        self._gate_check("accept", self.limits["accepts_per_tick"])
        o = self.offers.get(int(offer_id))
        if o is None:
            raise BazaarError("not_found", f"no offer {offer_id}", 404)
        if o["status"] != "open":
            raise BazaarError("offer_closed", f"offer is {o['status']}", 409)
        if o["maker"] == self.team:
            raise BazaarError("not_allowed", "cannot accept your own offer", 403)
        if o["thread"] is not None:  # a dealer's standing offer
            th = self.threads[o["thread"]]
            if th["status"] != "open":
                raise BazaarError("thread_closed", "conversation is over", 409)
            price = o["want"]["cash"] or o["give"]["cash"]
            if o["want"]["cash"] and o["want"]["cash"] > self.cash:
                raise BazaarError("insufficient_cash", "offer is above your cash", 400)
            if self._deal_quota_hit(th):
                raise BazaarError("persona_quota", "dealer quota for this hour reached", 429)
            self._gate_use("accept")
            self._pending.append(("dealer", o["id"]))
            return {"ok": True, "offer_id": o["id"], "price": price, "settles_tick": self.tick + 1}
        # board offer from an NPC
        cash_ask, cash_bid = o["want"]["cash"], o["give"]["cash"]
        if cash_ask and o["give"]["assets"]:  # NPC sells a card
            fee = (cash_ask * FEE_BPS + 9999) // 10000 + FEE_PER_CARD
            if cash_ask + fee > self.cash:
                raise BazaarError("insufficient_cash", "price plus fee is above your cash", 400)
        else:  # NPC buys a card from us
            ids = assets or []
            ref = o["want"]["types"][0].split(":")[1]
            if not ids or ids[0] not in self.assets or self.assets[ids[0]].get("ref") != ref:
                mine = [a["id"] for a in self.assets.values() if a["kind"] == "card" and a["ref"] == ref]
                if not mine:
                    raise BazaarError("not_owner", "you hold no copy of that card", 403)
                ids = [mine[-1]]
            assets = ids
        self._gate_use("accept")
        self._pending.append(("board", o["id"], assets))
        return {"ok": True, "offer_id": o["id"], "settles_tick": self.tick + 1}

    def _deal_quota_hit(self, th):
        did, cfg = th["with"], DEALER_CFG[th["with"]]
        h = self._hour()
        deals = [d for d in self.deals if d["dealer"] == did and d["hour"] == h]
        if len(deals) >= cfg["deals_per_hour"]:
            return True
        if "buy" in th["topic"] and "pack" in th["topic"]["buy"]:
            return sum(1 for d in deals if d["topic_kind"] == "pack") >= cfg["pack_quota"]
        return False

    def list_offer(self, give, want, venue=None, to=None, expires_in_ticks=40):
        if (venue or "rastro") != "rastro":
            raise BazaarError("venue_not_live", "only rastro exists in the sim", 400)
        self._gate_check("list", self.limits["listings_per_tick"])
        if sum(1 for o in self.offers.values() if o["maker"] == self.team and o["status"] == "open"
               and o["thread"] is None) >= self.limits["max_offers"]:
            raise BazaarError("too_many_offers", "thirty open offers at most", 429)
        g_assets = []
        for a in (give.get("assets") or []):
            a = a["id"] if isinstance(a, dict) else int(a)
            if a not in self.assets:
                raise BazaarError("not_owner", f"asset {a} is not yours", 403)
            if any(a in [x["id"] for x in o["give"]["assets"]] for o in self.offers.values()
                   if o["status"] == "open" and o["maker"] == self.team):
                raise BazaarError("asset_locked", f"asset {a} is already listed", 409)
            g_assets.append({"id": a, "kind": self.assets[a]["kind"], "ref": self.assets[a]["ref"]})
        w_cash, g_cash = int(want.get("cash") or 0), int(give.get("cash") or 0)
        for c in (w_cash, g_cash):
            if c and not 1 <= c <= 10_000_000:
                raise BazaarError("invalid", "cash must be 1..10,000,000", 400)
        if g_cash > self.cash:
            raise BazaarError("insufficient_cash", "offer is above your cash", 400)
        types = [f"card:{r}" if ":" not in r else r for r in (want.get("cards") or want.get("types") or [])]
        self._gate_use("list")
        o = self._mk_offer(self.team, to, None, _side(g_cash, g_assets), _side(w_cash, (), types),
                           venue="rastro", expires=int(expires_in_ticks))
        o["_ceiling"] = None
        return o

    def cancel(self, offer_id):
        o = self.offers.get(int(offer_id))
        if o is None or o["maker"] != self.team:
            raise BazaarError("not_found", f"no offer {offer_id}", 404)
        self._gate_check("list", self.limits["listings_per_tick"])
        self._gate_use("list")
        o["status"] = "cancelled"
        return {"ok": True}

    def board(self, venue="rastro"):
        if venue != "rastro":
            raise BazaarError("not_found", f"no venue {venue}", 404)
        offs = [o for o in self.offers.values() if o["status"] == "open" and o["venue"] == "rastro"]
        return {"venue": "rastro", "offers": [{k: v for k, v in o.items() if not k.startswith("_")} for o in offs],
                "fee_bps": FEE_BPS, "fee_per_card": FEE_PER_CARD}

    def open_pack(self, asset_id):
        a = self.assets.get(int(asset_id))
        if a is None:
            raise BazaarError("not_owner", f"asset {asset_id} is not yours", 403)
        if a["kind"] != "pack":
            raise BazaarError("not_a_pack", "that asset is not a sealed pack", 400)
        del self.assets[a["id"]]
        p = PACKS[a["ref"]]
        rel = [s for s, _n, r in CATALOG_SETS if r]
        out, got = [], 0
        for _ in range(p["n"]):
            rar = self.rng.choices(RARITY_ORDER, weights=[o + 1e-12 for o in p["odds"]])[0]
            cid = self.rng.choice([c for c, (s, r) in self.cards.items() if r == rar and s in rel])
            card = self._mint(cid)
            got += BOOK[rar]
            out.append(dict(card))
        return {"cards": out, "luck": round(got / pack_expected_book(a["ref"]), 2)}

    def broker(self, broker_key=None, **kw):
        """FakeBroker sharing this world's clock (advances with wait_tick/advance)."""
        return FakeBroker(seed=self.seed, tick_seconds=self.tick_seconds, world=self, **kw)

    def flag(self, message_id, reason=""):
        return {"ok": True, "message_id": message_id}

    # ------------------------------------------------------------------ duels
    def add_duel(self, role="seller", rival="aggressor", issues=("price",), ticks=16, decay=0.06, practice=False):
        """Create one live duel. role is OURS; the rival takes the other side. Hidden limits are drawn from the seed."""
        rng = self.rng
        cost = rng.randint(40, 60)
        value = cost + rng.randint(30, 70)
        use_days = "days" in issues
        w_us = round(rng.uniform(-4, 4), 1) if use_days else 0.0
        w_rv = round(rng.uniform(-4, 4), 1) if use_days else 0.0
        side = 1 if role == "seller" else -1
        rrole = "buyer" if role == "seller" else "seller"
        r_limit = value if rrole == "buyer" else cost
        mid = (cost + value) / 2 * rng.uniform(0.85, 1.15)
        riv = make_rival(rival, rrole, r_limit, random.Random(rng.random()), ticks=ticks, ref=mid,
                         w_days=w_rv, use_days=use_days, seed=self.seed)
        pie0 = (value - cost) + (5 * abs(w_us + w_rv) if use_days else 0)
        d = dict(id=len(self.duel_list) + 1, status="open", role=role, side=side, cost=cost, value=value,
                 our_limit=cost if role == "seller" else value, w_us=w_us, w_rv=w_rv, issues=list(issues),
                 start=self.tick, deadline=self.tick + ticks, ticks=ticks, decay=decay, rival=riv,
                 rival_name=rival, pie0=pie0, rival_offer=None, our_offer=None, last_say=-1, pending=None,
                 messages=[], result=None, practice=practice, alias=f"Rival-{rng.randint(10, 99)}")
        self.duel_list.append(d)
        r = riv.respond(0, None)
        self._set_rival_offer(d, r)
        return d["id"]

    def _set_rival_offer(self, d, r):
        d["rival_offer"] = {"price": r["price"], "days": r["days"], "text": r["text"], "final": r["final"],
                            "tick": self.tick}
        d["messages"].append({"id": self._id(), "tick": self.tick, "sender": d["alias"], "text": r["text"],
                              "offer": {"price": r["price"], "days": r["days"]}})

    def _pub_duel(self, d):
        out = dict(id=d["id"], status=d["status"], role=d["role"], your_limit=d["our_limit"], rival=d["alias"],
                   rival_offer=d["rival_offer"], your_offer=d["our_offer"], deadline=d["deadline"],
                   duel_ticks=d["ticks"], decay=d["decay"], issues=d["issues"], round=self.tick - d["start"],
                   practice=d["practice"], messages=list(d["messages"][-30:]))
        if "days" in d["issues"]:
            out["your_days_weight"] = d["w_us"]
        if d["result"]:
            out["result"] = d["result"]
        return out

    def duels(self, done=False):
        want = ("done", "expired") if done else ("open",)
        return {"duels": [self._pub_duel(d) for d in self.duel_list if d["status"] in want]}

    def _duel(self, duel_id):
        for d in self.duel_list:
            if d["id"] == int(duel_id):
                return d
        raise BazaarError("not_found", f"no duel {duel_id}", 404)

    def duel_say(self, duel_id, text="", price=None, days=None):
        d = self._duel(duel_id)
        if d["status"] != "open":
            raise BazaarError("duel_closed", f"duel is {d['status']}", 409)
        if d["last_say"] == self.tick and self.auto_wait:
            self.wait_tick()
        if d["last_say"] == self.tick:
            raise BazaarError("wait_for_tick", "one message per duel per tick", 429, {"next_tick": self.tick + 1})
        if price is not None:
            price = int(price)
            if not 1 <= price <= 10_000_000:
                raise BazaarError("invalid", "price must be 1..10,000,000", 400)
            if "days" in d["issues"]:
                if days is None:
                    raise BazaarError("missing_days", "this session negotiates price and days", 400)
                if not 0 <= int(days) <= 10:
                    raise BazaarError("invalid", "days must be 0..10", 400)
                days = int(days)
            else:
                days = None
            d["our_offer"] = {"price": price, "days": days}
        d["last_say"] = self.tick
        d["messages"].append({"id": self._id(), "tick": self.tick, "sender": self.team, "text": (text or "")[:1200],
                              "offer": d["our_offer"] if price is not None else None})
        d["_fresh"] = price is not None
        return {"ok": True, "tick": self.tick}

    def duel_accept(self, duel_id):
        d = self._duel(duel_id)
        if d["status"] != "open":
            raise BazaarError("duel_closed", f"duel is {d['status']}", 409)
        if d["rival_offer"] is None or d["rival_offer"]["price"] is None:
            raise BazaarError("no_offer", "the rival has no standing offer", 409)
        self._gate_check("accept", self.limits["accepts_per_tick"])
        self._gate_use("accept")
        d["pending"] = dict(d["rival_offer"])
        return {"ok": True, "settles_tick": self.tick + 1}

    def _duel_settle(self, d, price, days):
        us = d["side"] * (price - d["our_limit"])
        rv = -d["side"] * (price - d["our_limit"]) + (d["value"] - d["cost"])  # rival surplus = pie - ours (price part)
        if "days" in d["issues"]:
            dd = 5 if days is None else days
            us += d["w_us"] * (dd - 5)
            rv += d["w_rv"] * (dd - 5)
        shrink = (1 - d["decay"]) ** (self.tick - d["start"])
        d["status"] = "done"
        d["result"] = dict(price=price, days=days, tick=self.tick, shrink=round(shrink, 4),
                           your_surplus=round(us * shrink, 3), rival_surplus=round(rv * shrink, 3),
                           pie0=d["pie0"], your_share=round(us * shrink / d["pie0"], 4),
                           split=round(us / (us + rv), 4) if us + rv else 0.0)

    # ------------------------------------------------------------------ time
    def wait_tick(self):
        if self.max_ticks is not None and self.tick >= self.max_ticks:
            raise SimDone()
        self.advance()
        return self.clock()

    def advance(self, n=1):
        """Advance the world n ticks: settle accepted offers, let dealers/rivals/market react."""
        for _ in range(n):
            self.tick += 1
            self._settle_pending()
            for th in list(self.threads.values()):
                if th["status"] == "open":
                    self._dealer_turn(th)
            self._duel_turn()
            if self.market:
                self._market_turn()
            for b in self._brokers:
                b.advance()

    def _settle_pending(self):
        pend, self._pending = self._pending, []
        for item in pend:
            o = self.offers[item[1]]
            if o["status"] != "open":
                continue
            if item[0] == "dealer":
                th = self.threads[o["thread"]]
                if th["status"] == "open":
                    price = o["want"]["cash"] if o["want"]["cash"] else o["give"]["cash"]
                    self._deal(th, price, o)
            else:
                self._board_settle(o, item[2])

    def _dealer_turn(self, th):
        s = th.get("_s")
        if s is None:
            if th["created_tick"] < self.tick:
                th["_inbox"] = None
            return
        inbox, th["_inbox"] = th["_inbox"], None
        if not s["posted"] and inbox is None and th["created_tick"] == self.tick:
            return
        res = th["_model"].react(th, inbox, self.tick)
        if not res:
            return
        if res.get("deal") is not None:
            # a dealer accepting our price: the deal runs against our standing offer
            mine = [self.offers[i] for i in th["_offers"] if self.offers[i]["maker"] == self.team]
            self._post_dealer_msg(th, res["text"], None)
            self._deal(th, res["deal"], mine[-1] if mine else None)
            return
        offer = None
        if res.get("ask") is not None and not res.get("repost"):
            for oid in th["_offers"]:
                o2 = self.offers[oid]
                if o2["maker"] == th["with"] and o2["status"] == "open":
                    o2["status"] = "cancelled"
            offer = self._dealer_offer(th, res["ask"], res.get("final", False))
        elif res.get("repost"):
            offer = None
        self._post_dealer_msg(th, res["text"], offer)
        if res.get("close"):
            status, reason, until = res["close"]
            th["status"], th["closed_reason"] = status, reason
            self._cancel_thread_offers(th)
            if until is not None:
                th["until_tick"] = until
                self._cool[th["with"]] = until
            if status == "cooloff":
                self._log("cooloff", dealer=th["with"])

    def _dealer_offer(self, th, ask, final):
        t = th["topic"]
        if "buy" in t:
            b = t["buy"]
            typ = f"pack:{b['pack']}" if "pack" in b else f"card:{b['card']}"
            o = self._mk_offer(th["with"], self.team, th["id"], _side(types=[typ]), _side(ask), final=final)
        else:
            alist = [{"id": a, "kind": "card", "ref": self.assets[a]["ref"]} for a in t["sell"]["assets"]]
            o = self._mk_offer(th["with"], self.team, th["id"], _side(ask), _side(assets=alist), final=final)
        th["_offers"].append(o["id"])
        return o

    def _post_dealer_msg(self, th, text, offer):
        th["messages"].append(dict(id=self._id(), tick=self.tick, sender=th["with"], text=text, offer=offer))

    def _deal(self, th, price, offer):
        did, t, s = th["with"], th["topic"], th["_s"]
        h = self._hour()
        if self._deal_quota_hit(th):
            th["status"], th["closed_reason"] = "closed", "persona_quota"
            self._cancel_thread_offers(th)
            return
        if "buy" in t:
            if price > self.cash:
                th["status"], th["closed_reason"] = "closed", "insufficient_cash"
                self._cancel_thread_offers(th)
                return
            self.cash -= price
            b = t["buy"]
            if "pack" in b:
                self._nid += 1
                self.assets[self._nid] = dict(id=self._nid, kind="pack", ref=b["pack"], name=PACKS[b["pack"]]["name"],
                                              rarity=None, set=None, serial=self._nid, print_run=0)
            else:
                self._mint(b["card"])
            kind = "pack" if "pack" in b else "card"
        else:
            for a in t["sell"]["assets"]:
                self.assets.pop(a, None)
            self.cash += price
            kind = "sell"
        if offer is not None:
            offer["status"] = "accepted"
        th["status"], th["closed_reason"] = "deal", None
        self._cancel_thread_offers(th)
        self.deals.append(dict(tick=self.tick, thread=th["id"], dealer=did, side="buy" if "buy" in t else "sell",
                               topic=t, topic_kind=kind, price=price, open=s["open"], floor=s["floor"],
                               hour=h, rounds=s["rounds"], negotiated=(price < s["open"] if "buy" in t
                                                                      else price > s["open"])))
        if did == "abuela" and self.deals[-1]["negotiated"]:
            self.neg_deals += 1
            if self.neg_deals >= 3:
                self.chato_unlocked = True
        self._log("deal", dealer=did, price=price)

    # ------------------------------------------------------------------ duel and market turns
    def _duel_turn(self):
        for d in self.duel_list:
            if d["status"] != "open":
                continue
            if d["pending"]:
                p = d["pending"]
                self._duel_settle(d, p["price"], p["days"])
                d["pending"] = None
                continue
            t = self.tick - d["start"]
            if self.tick > d["deadline"]:
                d["status"] = "expired"
                d["result"] = dict(price=None, your_share=0.0, pie0=d["pie0"])
                continue
            ours = d["our_offer"]
            r = d["rival"].respond(t, ours["price"] if ours else None, ours["days"] if ours else None)
            if r["action"] == "accept":
                self._duel_settle(d, ours["price"], ours["days"])
            else:
                self._set_rival_offer(d, r)

    def _npc_refresh(self):
        # keep ~12 NPC offers on the house board
        live = [i for i in self.npc_offers if self.offers[i]["status"] == "open" and self.offers[i]["expires_tick"] > self.tick]
        self.npc_offers = live
        while len(self.npc_offers) < 12:
            cid = self.rng.choice(list(self.cards))
            book = BOOK[self.rarity_of(cid)]
            if self.rng.random() < 0.5:
                price = max(1, round(book * self.rng.uniform(0.8, 1.5)))
                self._nid += 1
                o = self._mk_offer(f"npc{self.rng.randint(1, 20)}", None, None,
                                   _side(0, [{"id": self._nid, "kind": "card", "ref": cid}]), _side(price),
                                   venue="rastro", expires=self.rng.randint(20, 60))
            else:
                price = max(1, round(book * self.rng.uniform(0.5, 1.1)))
                o = self._mk_offer(f"npc{self.rng.randint(1, 20)}", None, None, _side(price), _side(0, (), [f"card:{cid}"]),
                                   venue="rastro", expires=self.rng.randint(20, 60))
            self.npc_offers.append(o["id"])

    def _board_settle(self, o, assets):
        if o["give"]["assets"] and o["want"]["cash"]:  # we buy a card
            price = o["want"]["cash"]
            fee = (price * FEE_BPS + 9999) // 10000 + FEE_PER_CARD
            if price + fee > self.cash:
                return
            self.cash -= price + fee
            self._mint(o["give"]["assets"][0]["ref"])
        else:  # we sell
            price = o["give"]["cash"]
            a = assets[0] if assets else None
            if a not in self.assets:
                return
            del self.assets[a]
            self.cash += price - ((price * FEE_BPS + 9999) // 10000 + FEE_PER_CARD)
        o["status"] = "accepted"

    def _market_turn(self):
        for o in list(self.offers.values()):
            if o["status"] == "open" and o["expires_tick"] <= self.tick and o["thread"] is None:
                o["status"] = "expired"
        for o in list(self.offers.values()):  # NPCs take (or fill) our listings sometimes
            if o["status"] != "open" or o["maker"] != self.team or o["thread"] is not None:
                continue
            if o["give"]["assets"] and o["want"]["cash"]:
                a = o["give"]["assets"][0]["id"]
                if a not in self.assets:
                    o["status"] = "cancelled"
                    continue
                if "_ceiling" not in o or o["_ceiling"] is None:
                    o["_ceiling"] = BOOK[self.assets[a].get("rarity") or "common"] * self.rng.uniform(0.7, 1.4)
                if o["want"]["cash"] <= o["_ceiling"] and self.rng.random() < 0.15:
                    p = o["want"]["cash"]
                    del self.assets[a]
                    self.cash += p - ((p * FEE_BPS + 9999) // 10000 + FEE_PER_CARD)
                    o["status"] = "accepted"
        self._npc_refresh()


# ------------------------------------------------------------------------------------------------ bench market
class FakeBroker:
    """Duck-types bazaar_sdk.Broker on a board venue during a Market Test session.

    Synthetic traders: sellers have hidden costs, buyers hidden values; each quotes a shaded price that relaxes
    as the trader's patience runs out (style from sim.rivals), some never relax (firm), and every trader leaves
    at its own tick. Score = realised gains between true limits / best possible gains (result()).
    """

    def __init__(self, seed=0, style="all", runs=3, per_side=8, session_ticks=30, tick_seconds=30, scale=40, world=None):
        self.seed, self.rng = seed, random.Random(seed * 7919 + 13)
        self.tick, self.tick_seconds, self.session_ticks = 0, tick_seconds, session_ticks
        self.style, self.runs = style, runs
        self.traders: dict[str, dict] = {}
        self.settlements: list[dict] = []
        self.realised, self.possible = 0.0, 0.0
        self.announcements: list[str] = []
        names = list(RIVALS)
        for r in range(runs):
            ref = self.rng.choice(list(card_ids()))[1]
            book = scale * self.rng.uniform(0.7, 1.3)
            costs, values = [], []
            for i in range(per_side * 2):
                sell = i < per_side
                limit = round(book * (self.rng.uniform(0.5, 1.1) if sell else self.rng.uniform(0.8, 1.5)))
                nm = self.rng.choice(names) if style == "all" else style
                riv = make_rival(nm, "seller" if sell else "buyer", limit, random.Random(self.rng.random()),
                                 ticks=session_ticks, seed=self.rng.randint(0, 10 ** 6))
                leave = self.rng.randint(4, session_ticks)
                firm = self.rng.random() < riv.firm_prob
                tid = f"b{r}-{i + 1}"
                self.traders[tid] = dict(id=tid, run=r, sell=sell, limit=limit, riv=riv, leave=leave, firm=firm,
                                         ref=ref, alive=True, style=nm, maker=f"anon-{self.rng.randint(100, 999)}")
                (costs if sell else values).append(limit)
            costs.sort()
            values.sort(reverse=True)
            self.possible += sum(max(0, v - c) for c, v in zip(costs, values))
        if world is not None:
            world._brokers.append(self)
            self.tick = world.tick

    # -- hidden truth helpers (not in the real API)
    def quote(self, t):
        return t["riv"].quote(self.tick, t["leave"], t["firm"])

    def advance(self):
        self.tick += 1
        for t in self.traders.values():
            if t["alive"] and self.tick >= t["leave"]:
                t["alive"] = False

    def done(self):
        return self.tick >= self.session_ticks or not any(t["alive"] for t in self.traders.values())

    def result(self):
        eff = self.realised / self.possible if self.possible else 0.0
        return {"efficiency": round(eff, 4), "realised": self.realised, "possible": self.possible,
                "matches": len(self.settlements)}

    # -- the Broker API
    def clock(self):
        return {"tick": self.tick, "t_hours": round(self.tick * self.tick_seconds / 3600, 4),
                "tick_seconds": self.tick_seconds, "paused": False, "next_tick_in": float(self.tick_seconds)}

    def book(self):
        bench = []
        for t in self.traders.values():
            if not t["alive"]:
                continue
            q = self.quote(t)
            asset = [{"id": 0, "kind": "card", "ref": t["ref"]}]
            if t["sell"]:
                give, want = _side(0, asset), _side(q)
            else:
                give, want = _side(q), _side(0, (), [f"card:{t['ref']}"])
            bench.append(dict(id=t["id"], maker=t["maker"], venue="bench", give=give, want=want, bench=True,
                              created_tick=0, expires_tick=t["leave"], status="open"))
        return {"offers": [], "bench_offers": bench, "settlements": self.settlements[-20:], "fee_bps": 0,
                "fee_per_card": 0, "tick": self.tick}

    def match(self, sell, buy, price):
        s, b = self.traders.get(str(sell)), self.traders.get(str(buy))
        if s is None or b is None or not s["alive"] or not b["alive"]:
            raise BazaarError("not_found", "an offer is gone or unknown", 404)
        if not s["sell"] or b["sell"] or s["run"] != b["run"]:
            raise BazaarError("invalid", "pair one sell and one buy offer of the same run", 400)
        price = int(price)
        if price < self.quote(s) or price > self.quote(b):
            raise BazaarError("bad_price", "price must satisfy ask <= price <= bid", 400)
        gain = max(0.0, b["limit"] - s["limit"])
        s["alive"] = b["alive"] = False
        self.realised += b["limit"] - s["limit"] if b["limit"] > s["limit"] else 0.0
        self.settlements.append(dict(tick=self.tick, sell=s["id"], buy=b["id"], price=price))
        return {"ok": True, "price": price, "tick": self.tick}

    def announce(self, text):
        self.announcements.append(str(text)[:500])
        return {"ok": True}

    def wait_tick(self):
        self.advance()
        return self.clock()
