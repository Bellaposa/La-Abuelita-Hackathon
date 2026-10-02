import math, os, time
from collections import defaultdict
from bazaar_sdk import BazaarError, Broker

# Memoria globale per tracciare il comportamento delle offerte
offer_history = defaultdict(list)
offer_age = defaultdict(int)

def is_yielding(offer_id: str, current_quote: int, is_seller: bool) -> bool:
    """
    Determina se un trader sta ancora cedendo (relaxing).
    Un venditore cede se abbassa il prezzo. Un compratore cede se lo alza.
    """
    history = offer_history[offer_id]
    if len(history) < 2:
        return True # Diamo per scontato che stia cedendo all'inizio
    
    # Se il prezzo è cambiato negli ultimi 2 tick, sta ancora cedendo
    last_quote = history[-2]
    if is_seller:
        return current_quote < last_quote
    else:
        return current_quote > last_quote

def bench_plan(book: dict, current_tick: int) -> list:
    """
    Piano intelligente per il Market Test: stima i limiti reali aspettando
    che i bot smettano di cedere (diventino "firm") prima di incrociarli.
    """
    plan = []
    runs = {}
    current_ids = set()
    
    # Aggiorniamo la memoria
    for o in book.get("bench_offers") or []:
        offer_id = o["id"]
        current_ids.add(offer_id)
        
        is_seller = bool(o["want"]["cash"])
        quote = o["want"]["cash"] if is_seller else o["give"]["cash"]
        
        offer_history[offer_id].append(quote)
        offer_age[offer_id] += 1
        
        run_id = offer_id.split("-")[0]
        asks, bids = runs.setdefault(run_id, ([], []))
        if is_seller:
            asks.append((quote, offer_id))
        else:
            bids.append((quote, offer_id))
            
    # Pulizia vecchie offerte
    for old_id in list(offer_history.keys()):
        if old_id not in current_ids:
            del offer_history[old_id]
            del offer_age[old_id]

    # Analisi per ogni run (scenario di mercato)
    for run_id, (asks, bids) in runs.items():
        # Ordiniamo: i venditori più economici prima, i compratori che offrono di più prima
        for (ask, sell_id), (bid, buy_id) in zip(sorted(asks, key=lambda a: a[0]), sorted(bids, key=lambda b: -b[0])):
            if bid >= ask:
                # Possiamo incrociarli, ma DOVREMMO farlo ora?
                seller_yielding = is_yielding(sell_id, ask, is_seller=True)
                buyer_yielding = is_yielding(buy_id, bid, is_seller=False)
                
                # Se entrambi sono fermi (firm), incrociamo!
                # Oppure, se sono in giro da troppo tempo (es. 10 tick), incrociamo per non farli scappare.
                age_sell = offer_age[sell_id]
                age_buy = offer_age[buy_id]
                
                force_match = age_sell >= 10 or age_buy >= 10
                
                if force_match or (not seller_yielding and not buyer_yielding):
                    # Incrociamo al punto medio
                    price = (ask + bid) // 2
                    plan.append((sell_id, buy_id, price))
                else:
                    # Aspettiamo pazientemente che si avvicinino ai loro limiti reali
                    pass
                    
    return plan

def public_plan(book: dict) -> list:
    """Il public_plan originale (incrocia le offerte pubbliche card by card)."""
    def fee(price: int) -> int:
        return math.ceil(book["fee_bps"] * price / 10000) + book["fee_per_card"]
        
    plan = []
    offers = book.get("offers") or []
    bids = sorted((o for o in offers if o["give"]["cash"] and len(o["want"]["types"]) == 1), key=lambda o: -o["give"]["cash"])
    
    for s in sorted((o for o in offers if len(o["give"]["assets"]) == 1 and o["want"]["cash"]), key=lambda o: o["want"]["cash"]):
        ask = s["want"]["cash"]
        card = "{kind}:{ref}".format(**s["give"]["assets"][0])
        b = next((b for b in bids if b["want"]["types"] == [card] and b["maker"] != s["maker"]
                  and ask + fee(ask) <= b["give"]["cash"]), None)
        if b:
            bids.remove(b)
            price = next(p for p in range((ask + b["give"]["cash"]) // 2, ask - 1, -1) if p + fee(p) <= b["give"]["cash"])
            plan.append((s["id"], b["id"], price))
    return plan[:10]

if __name__ == "__main__":
    broker = Broker(os.environ.get("BAZAAR_URL", "https://bazaar.causaprima.ai"), os.environ["BROKER_KEY"])
    seen = None
    print("🚀 Smart Broker avviato! In attesa di distruggere il Market Test...")
    
    while True:
        try:
            clock = broker.clock()
            tick = clock["tick"]
            book = broker.book()
            now = (tick, [o["id"] for o in (book.get("bench_offers") or []) + (book.get("offers") or [])])
            
            if now != seen:
                seen = now
                plan = bench_plan(book, tick) + public_plan(book)
                for sell, buy, price in plan:
                    try:
                        broker.match(sell, buy, price)
                        print(f"[TICK {tick}] 🎯 MATCH ESEGUITO! {sell} x {buy} a {price} P")
                    except BazaarError as e:
                        print(f"tick {tick}: {sell} x {buy} at {price} rifiutato ({e})")
        except BazaarError as e:
            print(f"Errore di lettura book ({e}), riprovo...")
        time.sleep(1.0)
