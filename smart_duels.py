import os
import time
from bazaar_sdk import Bazaar, BazaarError

BAZAAR_URL = os.environ.get("BAZAAR_URL", "https://bazaar.causaprima.ai")
BAZAAR_KEY = os.environ.get("BAZAAR_KEY")

def evaluate_duel(b, duel):
    d_id = duel["id"]
    role = duel["role"]
    my_limit = duel["your_limit"]
    rival_offer = duel.get("rival_offer")
    deadline = duel["deadline"]
    issues = duel.get("issues", ["price"])
    days_weight = duel.get("your_days_weight", 0)
    
    # 1. Determina il giorno ideale se applicabile
    best_day = 10 if days_weight > 0 else 0
    days_args = {"days": best_day} if "days" in issues else {}

    # 2. Calcola l'offerta di prezzo ideale
    # Se compriamo, vogliamo pagare poco (es. metà del nostro limite)
    # Se vendiamo, vogliamo incassare tanto (es. il doppio del nostro limite)
    if role == "buyer":
        target_price = int(my_limit * 0.5)
        worst_acceptable = int(my_limit * 0.9) # Accettiamo fino al 90% del nostro valore
    else: # seller
        target_price = int(my_limit * 1.5)
        worst_acceptable = int(my_limit * 1.1) # Accettiamo se ci pagano almeno il 10% in più

    print(f"⚔️ Duello {d_id} | Ruolo: {role} | Limite: {my_limit} P | Target: {target_price} P")

    if rival_offer:
        rival_price = rival_offer.get("price")
        rival_days = rival_offer.get("days")
        print(f"   Rival offre: {rival_price} P (Giorni: {rival_days})")
        
        # Calcoliamo il nostro valore effettivo dell'offerta rivale
        actual_rival_value = rival_price
        if rival_days is not None and days_weight != 0:
            if role == "buyer":
                # Se compriamo, i giorni in più potrebbero svalutare l'offerta o valorizzarla
                actual_rival_value -= (rival_days * days_weight)
            else:
                actual_rival_value += (rival_days * days_weight)

        is_profitable = False
        if role == "buyer" and actual_rival_value <= worst_acceptable:
            is_profitable = True
        elif role == "seller" and actual_rival_value >= worst_acceptable:
            is_profitable = True
            
        if is_profitable:
            print(f"   ✅ L'offerta del rivale è vantaggiosa. ACCETTIAMO!")
            b.duel_accept(d_id)
            return

    # Se non c'è offerta o non è conveniente, facciamo la nostra
    msg = "Soy el mejor negociador de Madrid. Toma o déjalo." if role == "seller" else "Mi presupuesto es limitado, hazme un favor."
    
    print(f"   ➡️ Controbattiamo con {target_price} P")
    try:
        b.duel_say(d_id, text=msg, price=target_price, **days_args)
    except BazaarError as e:
        print(f"   ⚠️ Errore nell'inviare offerta al duello: {e}")

def main():
    if not BAZAAR_KEY:
        print("Errore: BAZAAR_KEY non impostata.")
        return
        
    b = Bazaar(BAZAAR_URL, BAZAAR_KEY)
    print("⚔️ Smart Duels Agent AVVIATO! In attesa dei duelli...")

    while True:
        try:
            active_duels = b.duels(done=False)
            
            if active_duels:
                print(f"\n--- Trovati {len(active_duels)} duelli attivi! ---")
                for duel in active_duels:
                    evaluate_duel(b, duel)
            
            # Aspetta il prossimo tick
            b.wait_tick()
            
        except KeyboardInterrupt:
            print("\nSpegnimento Smart Duels...")
            break
        except Exception as e:
            print(f"Errore critico: {e}")
            time.sleep(5)

if __name__ == "__main__":
    main()
