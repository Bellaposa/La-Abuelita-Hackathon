"""Read-only observer for a Market Test: the broker book every ~0.5 s and our venue's settlements, with wall times.

    BROKER_KEY=... BAZAAR_KEY=... python3 bench_watch.py      # writes logs/bench_watch.jsonl; never matches anything

It answers what broker.log cannot: when each bench offer appears and disappears inside a tick, and whether the pairs
we matched were really settled (feed `settlement` events on our venue) or vanished.
"""
import json
import os
import time

from bazaar_sdk import Bazaar, BazaarError, Broker

URL = os.environ.get("BAZAAR_URL", "https://bazaar.causaprima.ai")
OUT = os.path.join("logs", "bench_watch.jsonl")
VENUE = os.environ.get("WATCH_VENUE", "v01")


def main():
    os.makedirs("logs", exist_ok=True)
    br, b = Broker(URL, os.environ["BROKER_KEY"]), Bazaar(URL, os.environ["BAZAAR_KEY"], wait_on_tick=False)
    seen_ev, last_book, n = set(), None, 0
    while True:
        rec = {"t": round(time.time(), 3)}
        try:
            book = br.book()
            bench = [(o["id"], o["want"]["cash"], o["give"]["cash"], o.get("expires_tick")) for o in book.get("bench_offers") or []]
            rec.update(tick=br.clock().get("tick"), bench=bench, recent=book.get("recent"))
            if n % 4 == 0:                                       # settlements on our venue, every ~2 s
                ev = [e for e in b.feed(150).get("events", []) if e.get("type") == "settlement"
                      and (e.get("payload") or {}).get("venue") == VENUE and e.get("id") not in seen_ev]
                seen_ev.update(e["id"] for e in ev)
                if ev:
                    rec["settlements"] = [e["payload"] for e in ev]
        except BazaarError as e:
            rec["error"] = str(e)
        key = (rec.get("tick"), json.dumps(rec.get("bench")), bool(rec.get("settlements")), rec.get("error"))
        if key != last_book:                                     # write only changes
            with open(OUT, "a") as f:
                f.write(json.dumps(rec) + "\n")
            last_book = key
        n += 1
        time.sleep(0.5)


if __name__ == "__main__":
    main()
