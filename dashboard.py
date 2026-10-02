import os
import time
from rich.live import Live
from rich.table import Table
from rich.panel import Panel
from rich.layout import Layout
from rich import box
from bazaar_sdk import Bazaar

def generate_layout(b: Bazaar) -> Layout:
    try:
        me = b.me()
        clock = b.clock()
    except Exception as e:
        # Se c'è un errore di connessione momentaneo, mostriamo un pannello d'errore
        layout = Layout()
        layout.update(Panel(f"Errore di connessione API: {e}", style="bold red"))
        return layout
        
    layout = Layout()
    layout.split_column(
        Layout(name="header", size=3),
        Layout(name="main")
    )
    layout["main"].split_row(
        Layout(name="inventory", ratio=1),
        Layout(name="threads", ratio=1),
        Layout(name="score", ratio=1)
    )

    # 1. Header (Info Globali)
    is_paused = "⏸️ IN PAUSA" if clock.get("paused") else "▶️ LIVE"
    header_text = f"👑 Team: {me['name']} | 🏆 Livello: {me['level']} | ⏱️ Tick: {clock['tick']} (Prossimo in: {clock['next_tick_in']}s) | {is_paused}"
    layout["header"].update(Panel(header_text, style="bold yellow"))

    # 2. Inventario e Cash
    inv_table = Table(box=box.SIMPLE, expand=True)
    inv_table.add_column("Risorsa", style="cyan")
    inv_table.add_column("Quantità", justify="right", style="green")
    
    inv_table.add_row("💰 Cash (Primas)", f"{me['cash']} P")
    
    # Conta quante carte abbiamo
    cards = [a for a in me["assets"] if a["kind"] == "card"]
    inv_table.add_row("🃏 Carte Totali", str(len(cards)))
    
    # Valore della collezione basato sui moltiplicatori privati
    collection_value = round(sum(c.get("your_value", 0) for c in cards), 1)
    inv_table.add_row("💎 Valore Collezione", f"{collection_value} P")
    
    packs = [a for a in me["assets"] if a["kind"] == "pack"]
    inv_table.add_row("📦 Pacchetti Chiusi", str(len(packs)))
    
    layout["inventory"].update(Panel(inv_table, title="🎒 Zaino", border_style="cyan"))

    # 3. Trattative (Threads)
    threads_table = Table(box=box.SIMPLE, expand=True)
    threads_table.add_column("ID Thread", style="magenta")
    threads_table.add_column("Stato", style="white")
    
    open_threads = me.get("open_threads", [])
    if not open_threads:
         threads_table.add_row("Nessuna", "-")
    else:
         for t_id in open_threads:
             # Mostriamo l'ID, non facciamo altre chiamate API per evitare il rate limit (5/s)
             threads_table.add_row(str(t_id), "🔥 In Corso")
             
    layout["threads"].update(Panel(threads_table, title="💬 Trattative Attive", border_style="magenta"))

    # 4. Punteggio Ufficiale
    score_table = Table(box=box.SIMPLE, expand=True)
    score_table.add_column("Metrica", style="blue")
    score_table.add_column("Punti", justify="right", style="yellow")
    
    s = me.get("score", {})
    score_table.add_row("🏆 Totale", str(round(s.get("score", 0), 2)))
    score_table.add_row("🗣️ Negoziazione", str(round(s.get("negotiating", 0), 2)))
    score_table.add_row("📈 Market Making", str(round(s.get("market", 0), 2)))
    
    album = me.get("album", {})
    score_table.add_row("📚 Carte Album", f"{album.get('filled', 0)}/{album.get('slots', 40)}")
    score_table.add_row("🏅 Rank Globale", f"#{s.get('rank', 'N/A')}")
    
    layout["score"].update(Panel(score_table, title="📊 Punteggio Live", border_style="blue"))

    return layout

def main():
    url = os.environ.get("BAZAAR_URL", "https://bazaar.causaprima.ai")
    key = os.environ.get("BAZAAR_KEY")
    if not key:
        print("❌ ERRORE: BAZAAR_KEY non impostata. Usa 'export BAZAAR_KEY=...' prima di avviare.")
        return
        
    b = Bazaar(url, key)
    
    # Creiamo un'interfaccia a schermo intero che si auto-aggiorna
    with Live(generate_layout(b), refresh_per_second=2, screen=True) as live:
        while True:
            # Aggiorniamo i dati ogni 2 secondi (rispetta ampiamente il limite di 5 req/s)
            time.sleep(2)
            try:
                live.update(generate_layout(b))
            except KeyboardInterrupt:
                break

if __name__ == "__main__":
    main()
