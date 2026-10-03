"""Market intelligence from the public boards: what is asked, what is bid, how liquid each card is, who seems to build what.

Everything here is OBSERVED from offers on the boards, or INFERRED from them, and the inferred parts say so:

  observed  asks / bids per card (price, maker pseudonym, tick), offers that vanished before they expired,
            per-maker counts of bids and listings by set.
  inferred  liquidity(ref)           weak proxy: bids seen + 2 x early disappearances + distinct bidders, in a window.
            likely_builders(set)     makers that bid for >= 2 cards of a set and list fewer cards of it than they bid for.
            buyer_estimate(ref)      the highest bid seen for a card (a lower bound on what someone would pay), with a
                                     small boost when the bidder looks like a builder of that set.

Makers on a board are PSEUDONYMS (e.g. `m41383bb2`), not team ids: we cannot address them with `to=` and cannot see what they
hold. All we can do is react to what they put on the board. An early disappearance may be a trade OR a cancellation: it is
only a proxy for turnover.

Persistence: one JSON file, written atomically (temp file + os.replace); a missing or corrupt file means an empty memory.
"""
import json
import os

from agents.trader_agent import classify

WINDOW = 240              # ticks of history kept
MAX_SEEN = 2500           # offers remembered for the next diff


def _set_of(ref):
    return ref.split("-")[0]


class MarketIntel:
    def __init__(self, state=None, liq_scale=6.0, builder_boost=0.25):
        s = state or {}
        self.tick = s.get("tick", 0)
        self.bid_hist = s.get("bid_hist", {})       # ref -> [[tick, price, maker], ...]   (new bids, as first seen)
        self.ask_hist = s.get("ask_hist", {})       # ref -> [[tick, price, maker], ...]
        self.gone = s.get("gone", {})               # ref -> [tick, ...]  offers that vanished before expires_tick
        self.makers = s.get("makers", {})           # maker -> {"sets_bid": {set: n}, "sets_ask": {set: n}, "bids": {ref: max}, "last": tick}
        self.seen = s.get("seen", {})               # offer id (str) -> [ref, kind, price, maker, expires_tick, venue]
        self.extra = s.get("extra", {})             # small runtime memory owned by the engine (e.g. arbitrage inventory)
        self.current_bids, self.current_asks = {}, {}      # this tick's open bids / asks per ref (not persisted)
        self.liq_scale, self.builder_boost = liq_scale, builder_boost

    # ------------------------------------------------------------------ persistence

    def to_state(self):
        seen = dict(list(self.seen.items())[-MAX_SEEN:])
        return {"tick": self.tick, "bid_hist": self.bid_hist, "ask_hist": self.ask_hist, "gone": self.gone,
                "makers": self.makers, "seen": seen, "extra": self.extra,
                "note": "observed from public boards; liquidity/builders/buyer_estimate are inferences, see bz/trading/intel.py"}

    def save(self, path):
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.to_state(), f, ensure_ascii=False)
        os.replace(tmp, path)

    @classmethod
    def load(cls, path, **kw):
        try:
            with open(path, encoding="utf-8") as f:
                return cls(json.load(f), **kw)
        except (OSError, ValueError, AttributeError, TypeError):
            return cls(**kw)

    # ------------------------------------------------------------------ observation

    def observe(self, tick, offers, me_id=None, fresh_venues=None):
        """Feed one tick of public offers (the boards we read this tick, deduplicated by id).
        fresh_venues: the venues read this tick. An offer is only judged 'vanished' if its venue was read now (an offer on a
        board we skipped this tick is not gone). None = every venue was read."""
        fresh = None if fresh_venues is None else set(fresh_venues)
        self.tick = tick
        self.current_bids, self.current_asks = {}, {}
        now_ids = set()
        for o in offers:
            if o.get("status", "open") != "open" or o.get("maker") == me_id or o.get("thread") is not None:
                continue
            kind, g, w = classify(o)
            if kind is None:
                continue
            oid, maker, exp = str(o["id"]), o.get("maker"), o.get("expires_tick")
            now_ids.add(oid)
            if kind == "buy":                                       # they give card(s) for our cash: an ask
                price = w["cash"]
                for a in g["assets"]:
                    ref = a["ref"]
                    self.current_asks.setdefault(ref, []).append((price / max(1, len(g["assets"])), maker))
                    if oid not in self.seen:
                        self.ask_hist.setdefault(ref, []).append([tick, price, maker])
                        self._maker(maker, tick)["sets_ask"][_set_of(ref)] = self._maker(maker, tick)["sets_ask"].get(_set_of(ref), 0) + 1
                    self.seen[oid] = [ref, "ask", price, maker, exp, o.get("venue")]
            elif kind == "sell":                                    # they give cash for our card: a bid
                ref, price = w["types"][0], g["cash"]
                self.current_bids.setdefault(ref, []).append((price, maker))
                if oid not in self.seen:
                    self.bid_hist.setdefault(ref, []).append([tick, price, maker])
                    m = self._maker(maker, tick)
                    m["sets_bid"][_set_of(ref)] = m["sets_bid"].get(_set_of(ref), 0) + 1
                    m["bids"][ref] = max(m["bids"].get(ref, 0), price)
                self.seen[oid] = [ref, "bid", price, maker, exp, o.get("venue")]
            else:                                                   # swap: their card for ours: demand for `ours` without a price
                self.seen[oid] = [w["types"][0], "swap", 0, maker, exp, o.get("venue")]
        for oid in [i for i in self.seen if i not in now_ids]:
            ref, kind, price, maker, exp, venue = (self.seen[oid] + [None])[:6]
            if fresh is not None and venue not in fresh:
                continue                                            # that board was not read this tick: it may still be there
            del self.seen[oid]
            if isinstance(exp, int) and exp > tick and kind in ("ask", "bid"):      # left before it expired: traded or cancelled
                self.gone.setdefault(ref, []).append(tick)
        self._prune(tick)

    def _maker(self, maker, tick):
        m = self.makers.setdefault(maker, {"sets_bid": {}, "sets_ask": {}, "bids": {}, "last": tick})
        m["last"] = tick
        return m

    def _prune(self, tick):
        lo = tick - WINDOW
        for hist in (self.bid_hist, self.ask_hist):
            for ref in list(hist):
                hist[ref] = [x for x in hist[ref] if x[0] >= lo]
                if not hist[ref]:
                    del hist[ref]
        for ref in list(self.gone):
            self.gone[ref] = [t for t in self.gone[ref] if t >= lo]
            if not self.gone[ref]:
                del self.gone[ref]

    # ------------------------------------------------------------------ queries

    def asks(self, ref):
        """Open asks for `ref` this tick, cheapest first: [(price, maker)]."""
        return sorted(self.current_asks.get(ref, []))

    def bids(self, ref):
        """Open bids for `ref` this tick, best first: [(price, maker)]."""
        return sorted(self.current_bids.get(ref, []), reverse=True)

    def best_bid(self, ref):
        b = self.bids(ref)
        return b[0] if b else None

    def liquidity(self, ref):
        """0..1. INFERRED: distinct bids seen + 2 x early disappearances + distinct bidders, over the window, / liq_scale."""
        bids = self.bid_hist.get(ref, [])
        score = len(bids) + 2 * len(self.gone.get(ref, [])) + len({m for _, _, m in bids})
        return min(1.0, score / self.liq_scale)

    def likely_builders(self, set_id):
        """Makers that bid for >= 2 cards of the set and list fewer of its cards than they bid for. INFERRED."""
        return [m for m, p in self.makers.items()
                if p["sets_bid"].get(set_id, 0) >= 2 and p["sets_bid"].get(set_id, 0) > p["sets_ask"].get(set_id, 0)]

    def buyer_estimate(self, ref):
        """(price, maker, builder) or None: the highest bid seen for `ref` in the window, boosted when the bidder looks like a
        builder of the set. INFERRED lower bound of what somebody would pay."""
        hist = self.bid_hist.get(ref, [])
        cur = self.current_bids.get(ref, [])
        pool = [(p, m) for _, p, m in hist] + list(cur)
        if not pool:
            return None
        price, maker = max(pool)
        builder = maker in self.likely_builders(_set_of(ref))
        return {"price": price * (1 + self.builder_boost) if builder else price, "maker": maker, "builder": builder}
