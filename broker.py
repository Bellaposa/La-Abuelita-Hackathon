"""Team 6 broker: beats the auto stall on the Market Test by timing, not just quotes.

    BROKER_KEY=bk_... python3 broker.py

Bench traders quote away from a hidden limit and relax as their patience runs out; firm ones never move.
The test scores realised gains between the TRUE limits, so:
  * we track every bench offer's quote over ticks (how fast it relaxes = impatience);
  * a crossing pair is matched at once only if one side looks about to leave or the book is near its end;
    otherwise we wait a tick so quotes keep relaxing and more (and better) pairs cross;
  * pairing is surplus-maximising: highest estimated-limit buyers with lowest estimated-limit sellers.
Public offers on our venue are crossed card by card like the starter broker (it earns us 'value created').
"""
import math
import os
import time

from bazaar_sdk import BazaarError, Broker
from starter_broker import public_plan

URL = os.environ.get("BAZAAR_URL", "https://bazaar.causaprima.ai")
broker = Broker(URL, os.environ["BROKER_KEY"])
SESSION_TICKS = 16


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


class Track:
    """Quote history per bench offer id."""

    def __init__(self):
        self.hist = {}   # id -> [(tick, quote)]
        self.first = {}  # run -> first tick seen

    def update(self, tick, offers):
        for o in offers:
            q = o["want"]["cash"] or o["give"]["cash"]
            h = self.hist.setdefault(o["id"], [])
            if not h or h[-1][0] != tick:
                h.append((tick, q))
            self.first.setdefault(o["id"].split("-")[0], tick)

    def drift(self, oid):
        """Average change of quote per tick (sellers: negative = relaxing; buyers: positive)."""
        h = self.hist.get(oid, [])
        if len(h) < 2:
            return 0.0
        return (h[-1][1] - h[0][1]) / max(1, h[-1][0] - h[0][0])

    def age(self, oid, tick):
        h = self.hist.get(oid, [])
        return tick - h[0][0] if h else 0


def bench_plan(book, tick, tr: Track):
    plan, runs = [], {}
    for o in book.get("bench_offers") or []:
        asks, bids = runs.setdefault(o["id"].split("-")[0], ([], []))
        if o["want"]["cash"]:
            asks.append(o)
        else:
            bids.append(o)
    for run, (asks, bids) in runs.items():
        elapsed = tick - tr.first.get(run, tick)
        late = elapsed >= SESSION_TICKS - 3
        a_s = sorted(asks, key=lambda o: o["want"]["cash"])
        b_s = sorted(bids, key=lambda o: -o["give"]["cash"])
        for s, bu in zip(a_s, b_s):
            ask, bid = s["want"]["cash"], bu["give"]["cash"]
            if bid < ask:
                break
            # impatience: a moving quote means the trader is spending patience and may leave soon
            moving = abs(tr.drift(s["id"])) > 0 or abs(tr.drift(bu["id"])) > 0
            old = max(tr.age(s["id"], tick), tr.age(bu["id"], tick)) >= 4
            if late or moving or old or elapsed >= 2:
                plan.append((s["id"], bu["id"], (ask + bid) // 2))
    return plan


def main():
    tr, seen = Track(), None
    while True:
        try:
            tick, book = broker.clock()["tick"], broker.book()
            bench = book.get("bench_offers") or []
            tr.update(tick, bench)
            now = (tick, [o["id"] for o in bench + (book.get("offers") or [])])
            if now != seen:
                seen = now
                for sell, buy, price in bench_plan(book, tick, tr) + public_plan(book):
                    try:
                        broker.match(sell, buy, price)
                        log(f"tick {tick}: matched {sell} x {buy} at {price}")
                    except BazaarError as e:
                        log(f"tick {tick}: {sell} x {buy} at {price} refused ({e})")
        except BazaarError as e:
            log(f"cannot read the book ({e})")
        time.sleep(1.0)


if __name__ == "__main__":
    main()
