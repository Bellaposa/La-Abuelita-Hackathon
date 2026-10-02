import os
import random
import json
from bazaar_sdk import Bazaar

MEMORY_FILE = "memory.json"

def load_memory():
    if os.path.exists(MEMORY_FILE):
        try:
            with open(MEMORY_FILE, "r") as f:
                return json.load(f)
        except:
            pass
    return {"abuela_min_price": 999}

def save_memory(mem):
    with open(MEMORY_FILE, "w") as f:
        json.dump(mem, f)

def log_chat(msg: str):
    with open("historial_abuela.txt", "a", encoding="utf-8") as f:
        f.write(msg + "\n")
from bazaar_sdk import Bazaar

import anthropic

# Inizializza il client Claude
client = anthropic.Anthropic()

def get_llm_haggle_message(dealer: str, current_offer: int, max_budget: int, history: list = None) -> str:
    """
    Usa Claude per generare messaggi di negoziazione persuasivi e furbi.
    Riceve anche lo storico (history) per mantenere un contesto coerente.
    """
    history_text = "\n".join(history[-6:]) if history else "(Nessuno storico precedente)"
    
    prompt = f"""
    Sei un negoziatore furbo e persuasivo al mercato de El Rastro (Madrid). 
    Stai contrattando con {dealer.capitalize()}.
    
    STORICO RECENTE DELLE TRATTATIVE CON QUESTO DEALER:
    {history_text}
    
    Devi fare un'offerta ESATTA di {current_offer} primas (P). 
    Il tuo budget reale è {max_budget}. 
    
    Considerando lo storico qui sopra, inventa un'ottima scusa o una frase lusinghiera per 
    farle accettare {current_offer} P. Cerca di dare continuità alla storia (es. se prima le hai detto 
    che hai i soldi contati per un regalo, continua su quella linea).
    
    Rispondi in spagnolo in MASSIMO 1-2 frasi. 
    Menziona esplicitamente il prezzo di {current_offer} primas.
    NON includere spiegazioni, scrivi SOLO la frase che dirai.
    """
    try:
        response = client.messages.create(
            model="claude-3-5-sonnet-20241022",
            max_tokens=150,
            messages=[{"role": "user", "content": prompt}]
        )
        return response.content[0].text.strip()
    except Exception as e:
        print(f"⚠️ Errore Claude ({e}). Uso frase fissa.")
        return f"¡Hola {dealer.capitalize()}! ¿Aceptas {current_offer} P y somos amigos?"

def arbitrage_market(b: Bazaar, me: dict):
    """
    Scansiona El Rastro (il mercato pubblico) per trovare giocatori 
    che vendono carte a prezzi inferiori al nostro valore segreto.
    """
    print("\n🕵️ Scansione del mercato 'rastro' in cerca di asimmetrie di valore...")
    try:
        book = b.board("rastro")
        offers = book.get("offers", [])
    except Exception as e:
        print(f"⚠️ Impossibile leggere il mercato: {e}")
        return

    # Cerchiamo occasioni d'oro (arbitraggio)
    for o in offers:
        if o["maker"] != me["id"] and o.get("give", {}).get("assets") and o.get("want", {}).get("cash"):
            ask_price = o["want"]["cash"]
            card = o["give"]["assets"][0]
            ref = card["ref"]
            
            try:
                # Scopriamo quanto vale DAVVERO questa carta per noi
                val_data = b.value(ref)
                my_value = val_data.get("your_value") or val_data.get("value", 0)
                
                # Calcoliamo le fee di El Rastro (5% + 1 P)
                estimated_fee = int(ask_price * 0.05) + 1
                total_cost = ask_price + estimated_fee
                
                margin = my_value - total_cost
                
                # Se ci guadagniamo un buon margine (> 3 P) e abbiamo i soldi
                if margin >= 3 and me["cash"] >= total_cost:
                    print(f"🚨 ARBITRAGGIO D'ORO! Qualcuno svende {card['name']} a {ask_price}P + fee.")
                    print(f"   Per il nostro team vale ben {my_value}P! Margine netto: {margin}P")
                    try:
                        b.accept(o["id"])
                        print(f"   ✅ Affare accettato (si risolverà al prossimo tick)!")
                        me["cash"] -= total_cost # Aggiorniamo preventivamente i fondi locali
                    except Exception as e:
                        print(f"   ❌ Errore nell'accettare l'offerta: {e}")
            except Exception:
                pass

def run_agent():
    # 1. Inizializzazione
    b = Bazaar(os.environ.get("BAZAAR_URL", "https://bazaar.causaprima.ai"), os.environ["BAZAAR_KEY"])
    
    print("🚀 Smart Agent AVVIATO! (Premere Ctrl+C per fermarlo)\n")
    memory = load_memory()
    
    while True:
        try:
            me = b.me()
            catalog = b.catalog()
            
            print(f"--- ⏱️ Nuova iterazione | Cash: {me['cash']} P | Carte: {len(me['assets'])} ---")
            
            # --- FASE 1: Arbitraggio sul mercato pubblico ---
            arbitrage_market(b, me)
            
            # --- FASE 2: Negoziazione con l'Abuela per salire di livello ---
            try:
                pack = next(p for p in catalog["packs"] if p["id"] == "sobre_barrio")
                budget = min(me["cash"], int(pack["expected_book"] * 0.8))
                
                # Recupera thread aperti
                open_threads = me.get("open_threads", [])
                abuela_thread_id = None
                
                for t_id in open_threads:
                    try:
                        t_data = b.thread(t_id)
                        if t_data.get("with") == "abuela" and t_data.get("status") == "open":
                            abuela_thread_id = t_id
                            break
                    except Exception:
                        continue
                        
                if abuela_thread_id:
                    thread = {"id": abuela_thread_id}
                else:
                    thread = b.open_thread("abuela", topic={"buy": {"pack": "sobre_barrio"}})
                
                # Se siamo riusciti ad agganciare il thread
                t = b.thread(thread["id"])
                status = t["status"]
                hers = [o for o in t["standing_offers"] if o["maker"] == "abuela" and o["status"] == "open"]
                mine = [o for o in t["standing_offers"] if o["maker"] == me["id"] and o["status"] == "open"]
                
                min_known_price = memory.get("abuela_min_price", 999)
                
                if status == "open":
                    if hers:
                        ask = hers[-1]["want"]["cash"]
                        print(f"\n👵 Abuela chiede: {ask} P per un pacchetto (Nostro limite: {budget} P)")
                        log_chat(f"👵 Abuela pide: {ask} P")
                        
                        # MODO AGGRESSIVO: accettiamo SUBITO qualsiasi prezzo <= budget
                        if ask <= budget:
                            print(f"🚀 ACCETTIAMO SUBITO a {ask} P! (Modo aggressivo)")
                            b.accept(hers[-1]["id"])
                            log_chat(f"🤝 ¡Trato cerrado a {ask} P!")
                            
                            if ask < memory.get("abuela_min_price", 999):
                                memory["abuela_min_price"] = ask
                                save_memory(memory)
                        else:
                            # Chiede troppo: controproponiamo DIRETTAMENTE il massimo budget
                            msg = "¡Abuela, eres la mejor! Acepta 27 P por favor, es todo lo que tengo."
                            print(f"🗣️ Controproposta al massimo: {budget} P")
                            log_chat(f"🤖 Nosotros ({budget} P): {msg}")
                            b.say(t["id"], msg, price=budget)
                    elif not mine:
                        # Thread vuoto: partiamo DIRETTAMENTE dal massimo per chiudere subito
                        msg = "¡Hola Abuela! ¿Aceptas 27 P? Tengo prisa, somos amigos."
                        print(f"🗣️ Prima mossa aggressiva: {budget} P (massimo)")
                        log_chat(f"🤖 Nosotros ({budget} P): {msg}")
                        b.say(t["id"], msg, price=budget)
            except Exception as e:
                # Se l'Abuela dà errore (es. persona_quota), lo stampiamo e continuiamo con la FASE 3
                if "persona_quota" in str(e):
                    print("👵 Abuela in pausa (Quota oraria raggiunta). Passo alla speculazione.")
                else:
                    print(f"⚠️ Salto Abuela per questo tick: {e}")
            
            # --- FASE 3: Vendi SOLO doppioni a basso valore privato ---
            held = me["assets"]
            seen = {}

            for a in sorted((a for a in held if a["kind"] == "card"), key=lambda a: a["serial"]):
                ref = a["ref"]
                my_val = a.get("your_value", 999)

                if ref not in seen:
                    seen[ref] = a
                else:
                    # Doppione trovato. Vendi SOLO se vale poco per noi (<= 5 P)
                    if my_val <= 5.0:
                        sell_price = max(int(my_val * 2), 10)
                        try:
                            b.list_offer({"assets": [a["id"]]}, {"cash": sell_price}, venue="rastro")
                            print(f"📈 Vendo doppione '{a['name']}' (val: {my_val}P) per {sell_price} P")
                        except Exception:
                            pass
                    else:
                        print(f"💎 Tengo '{a['name']}' (val: {my_val}P) - troppo prezioso!")
                
            print("\n⏳ Attesa del prossimo tick del server (circa 60s)...")
            b.wait_tick()
            
        except Exception as e:
            print(f"⚠️ Errore nel loop principale: {e}")
            print("🔄 Riavvio ciclo al prossimo tick...")
            try:
                b.wait_tick()
            except:
                pass

if __name__ == "__main__":
    run_agent()
