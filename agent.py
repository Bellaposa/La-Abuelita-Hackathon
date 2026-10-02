"""Team 6 dealer agent: haggles with Abuela Carmen for cards we value, packs, and sells our near-worthless duplicates.

    BAZAAR_KEY=tk-... python3 agent.py      # runs forever, logs to stdout

Plan per deal (one conversation with a dealer at a time):
  buy : open low, concede in shrinking steps, accept once her ask meets our next bid (or her final is under our cap)
  sell: mirror image
Targets are re-ranked every round from b.me() / b.value(), so the agent adapts as cards arrive.
"""
import os
import random
import time
import traceback

from bazaar_sdk import Bazaar, BazaarError

URL = os.environ.get("BAZAAR_URL", "https://bazaar.causaprima.ai")
b = Bazaar(URL, os.environ["BAZAAR_KEY"], wait_on_tick=False)
DEALER = "abuela"
PACK_VALUE_EST = 30.0  # expected private value of a sobre_barrio (book 33.8, our affinities avg ~0.97, some dups)

FIRST = ["¡Hola, Abuela Carmen! Qué alegría verla, ¿cómo está hoy? Vengo por {item}; ¿le parece bien {mine} primas?",
         "Buenas tardes, Abuela, ¿ha comido ya? Me encantaría llevarme {item}. ¿Le parece bien {mine} primas?"]
# follow-ups never re-greet: they thank her for what she just said and move one small, honest step
BUY_NEXT = ["Muchas gracias por su paciencia, Abuela. Con cariño, ¿podríamos dejarlo en {mine} primas?",
            "Es usted un sol. Me estiro un poquito más: {mine} primas. ¿Le parece justo?",
            "Entiendo, Carmen, y se lo agradezco. Subo a {mine} primas, que es lo que puedo.",
            "Qué bien se está con usted. ¿Qué tal {mine} primas, de corazón?"]
SELL_NEXT = ["Gracias por escucharme, Abuela. ¿Y si fuera por {mine} primas?",
             "Le agradezco mucho, Carmen. Bajo un poquito: {mine} primas.",
             "Usted manda, pero ¿le valdría {mine} primas? Gracias de corazón."]
FIRST_SELL = "¡Hola, Abuela Carmen! Tengo {item} repetida y quizá a usted le sirva. ¿Le parece bien {mine} primas?"


def compose(side, mine, first, i, item):
    if first:
        t = FIRST_SELL if side == "sell" else FIRST[i % len(FIRST)]
        return t.format(mine=mine, item=item)
    pool = BUY_NEXT if side == "buy" else SELL_NEXT
    return pool[i % len(pool)].format(mine=mine)


def item_label(topic):
    if "pack" in topic.get("buy", {}):
        return "un sobre de barrio"
    if "card" in topic.get("buy", {}):
        return f"la carta {topic['buy']['card']}"
    return "una carta"


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def wait_running():
    """Block until the clock is ticking."""
    while True:
        try:
            c = b.clock()
            if not c.get("paused"):
                return c
        except BazaarError as e:
            log("clock error", e)
        time.sleep(10)


def plan_targets(me, catalog):
    """Ranked list of (gain, topic, side, cap, list_price). side: buy|sell."""
    held = [a for a in me["assets"] if a["kind"] == "card"]
    RESERVE = 280  # venue bond 250 + opening fee 20, plus a little
    cash = max(0, me["cash"] - RESERVE)
    targets = []
    released = {s["id"] for s in catalog["sets"] if s.get("released")}
    list_price = {"common": 10, "uncommon": 25}
    # buy single cards Abuela sells (commons/uncommons of released sets) when our value clearly beats list price
    for s in catalog["sets"]:
        if s["id"] not in released:
            continue
        for c in s["cards"]:
            if c["rarity"] not in list_price:
                continue
            try:
                v = b.value(c["id"])["your_value"]
            except BazaarError:
                continue
            lp = list_price[c["rarity"]]
            if v >= lp * 1.1:
                cap = min(int(v * 0.9), cash)
                targets.append((v - lp * 0.7, {"buy": {"card": c["id"]}}, "buy", cap, lp))
    # packs: stopped, luck does not score and the price beats no better than a card we pick
    # sell duplicates / cards worth little to us (commons & uncommons only: that is what she buys)
    for a in held:
        if a["rarity"] in ("common", "uncommon") and a["your_value"] <= 0.5 * list_price[a["rarity"]]:
            floor = max(1, int(a["your_value"]) + 1)
            targets.append((list_price[a["rarity"]] * 0.5 - a["your_value"], {"sell": {"assets": [a["id"]]}}, "sell",
                            floor, list_price[a["rarity"]]))
    targets.sort(key=lambda t: -t[0])
    return targets


def her_open_offer(t):
    hers = [o for o in t["standing_offers"] if o["maker"] == DEALER and o["status"] == "open"]
    return hers[-1] if hers else None


def haggle(thread_id, side, cap, list_price, start=None, item="una carta"):
    """Run one conversation to the end. Returns final thread status."""
    if side == "buy":
        mine = start if start is not None else max(1, int(list_price * 0.45))
    else:
        mine = start if start is not None else int(list_price * 1.6)
    last_sent_tick, her_prev = -1, None
    sent_first = start is not None
    n_msgs = 0
    while True:
        try:
            t = b.thread(thread_id)
        except BazaarError as e:
            log("thread read error", e)
            time.sleep(3)
            continue
        if t["status"] != "open":
            log(f"thread {thread_id} -> {t['status']} ({t.get('closed_reason')})")
            return t["status"], t
        clock = b.clock()
        tick = clock["tick"]
        if clock.get("paused"):
            time.sleep(10)
            continue
        o = her_open_offer(t)
        ask = None
        if o:
            ask = o["want"]["cash"] if side == "buy" else o["give"]["cash"]
            final = o.get("final")
            good = (ask <= cap) if side == "buy" else (ask >= cap)
            meets = (ask <= mine + max(1, int(0.04 * list_price))) if side == "buy" else (ask >= mine - max(1, int(0.04 * list_price)))
            if good and (meets or final):
                try:
                    b.accept(o["id"])
                    log(f"ACCEPT {side} at {ask} (final={final}) thread {thread_id}")
                    b.wait_tick()
                    continue
                except BazaarError as e:
                    log("accept refused", e.code, e.message)
                    if e.code == "wait_for_tick":
                        b.wait_tick()
                        continue
            if final and not good:
                log(f"her final {ask} beyond our cap {cap}: walking")
                b.close_thread(thread_id)
                return "closed", t
        if tick != last_sent_tick:
            if sent_first and ask is not None:
                gap = abs(ask - mine)
                step = max(1, round(gap * 0.22))  # shrinking concessions: she mirrors small steps
                if her_prev is not None and abs(her_prev - ask) <= 1:
                    step = max(1, round(gap * 0.3))
                mine = min(cap, mine + step) if side == "buy" else max(cap, mine - step)
            her_prev = ask
            text = compose(side, mine, not sent_first, n_msgs, item)
            n_msgs += 1
            try:
                b.say(thread_id, text, price=mine)
                sent_first = True
                last_sent_tick = tick
                log(f"thread {thread_id} {side}: me {mine} her {ask} cap {cap}")
            except BazaarError as e:
                if e.code != "wait_for_tick":
                    log("say refused", e.code, e.message)
                    if e.code in ("thread_closed", "not_open", "cooloff", "persona_quota"):
                        return e.code, t
        b.wait_tick()


def main():
    catalog = b.catalog()
    wait_running()
    blocked = {}  # topic repr -> tick until which we skip it
    while True:
        try:
            wait_running()
            me = b.me()
            tick = me["tick"]
            for a in me["assets"]:  # open any sealed pack at once, whatever deal it came from
                if a["kind"] == "pack":
                    try:
                        r = b.open_pack(a["id"])
                        log("opened pack:", [(c.get("ref") or c.get("id"), c["rarity"]) for c in r["cards"]])
                    except BazaarError as e:
                        log("open_pack", e)
            # adopt an already-open dealer thread
            open_th = [t for t in b.my_threads("open")["threads"] if t["with"] == DEALER]
            if open_th:
                t = open_th[0]
                topic = t["topic"]
                side = "buy" if "buy" in topic else "sell"
                tg = next((x for x in plan_targets(me, catalog) if x[1] == topic), None)
                cap, lp = (tg[3], tg[4]) if tg else ((24, 26) if side == "buy" else (2, 10))
                mine = [m["offer"]["give"]["cash"] if side == "buy" else m["offer"]["want"]["cash"]
                        for m in t["messages"] if m["sender"] == me["id"] and m.get("offer")]
                log(f"resuming thread {t['id']} {topic}")
                status, _ = haggle(t["id"], side, cap, lp, start=mine[-1] if mine else None, item=item_label(topic))
                continue
            for gain, topic, side, cap, lp in plan_targets(me, catalog):
                key = repr(topic)
                if blocked.get(key, -1) > tick:
                    continue
                try:
                    th = b.open_thread(DEALER, topic=topic)
                except BazaarError as e:
                    log("open refused", topic, e.code, e.message)
                    if e.code in ("persona_quota", "cooloff", "locked"):
                        wait = 60 if b.clock()["tick_seconds"] >= 60 else 120
                        log(f"dealer unavailable ({e.code}); sleeping {wait}s")
                        time.sleep(wait)
                        break
                    blocked[key] = tick + 30
                    continue
                log(f"OPEN thread {th['id']} {side} {topic} cap {cap} gain~{gain:.1f}")
                status, t = haggle(th["id"], side, cap, lp, item=item_label(topic))
                if status != "deal":
                    blocked[key] = tick + 20
                else:
                    now = b.me()
                    for a in now["assets"]:
                        if a["kind"] == "pack":
                            try:
                                r = b.open_pack(a["id"])
                                log("opened pack:", [(c["ref"] if "ref" in c else c.get("id"), c["rarity"]) for c in r["cards"]])
                            except BazaarError as e:
                                log("open_pack", e)
                    log(f"cash {now['cash']} score {(now.get('score') or {}).get('score')}")
                break
            else:
                time.sleep(30)
        except BazaarError as e:
            log("error", e.code, e.message)
            time.sleep(5)
        except Exception:
            traceback.print_exc()
            time.sleep(10)


if __name__ == "__main__":
    main()
