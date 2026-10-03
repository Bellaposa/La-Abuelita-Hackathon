# Relevo: cómo seguir en otra máquina

La carpeta `relevo/` del repo (privado) trae una foto del estado aprendido por los agentes y de los logs. Se refresca
con `./relevo.sh` en la máquina que tiene los agentes corriendo, justo antes de apagarla; la hora de la foto está en
`relevo/SNAPSHOT_TIME`. Si el repo vuelve a ser público, borrad `relevo/`: lleva valores privados y límites.

## 0. Regla para quien arranque (persona o Claude)

**Arranca solo si existe `relevo/STOPPED` y `relevo/SNAPSHOT_TIME` es igual o posterior a su hora**: la máquina
anterior paró sus agentes y DESPUÉS guardó la foto final. Sin `relevo/STOPPED`, la otra máquina puede seguir corriendo
con la misma key y los dos controladores se pisan. Al arrancar en tu máquina, borra `relevo/STOPPED` en tu próximo
`./relevo.sh` no hace falta: lo reescribe quien pare la próxima vez.

## 1. Antes de arrancar

- **Una sola máquina a la vez.** Si este ordenador sigue encendido con los agentes, apágalos antes
  (`pkill -f smart_agent.py; pkill -f smart_duels.py; pkill -f page_hunter.py; pkill -f broker.py`).
  Dos copias con la misma key se pisan: aceptan dos veces, mezclan precios con los dealers.
- `git pull` en `main`: el código está ahí. Este paquete solo trae el estado.
- La **broker key** NO viene en el paquete: pídela por privado y guárdala en `.broker_key` (una línea).

## 2. Copiar el estado: `cp relevo/*.json relevo/*.jsonl relevo/*.txt . && cp relevo/dashboard/history.json dashboard/`

| Archivo | Qué es |
|---|---|
| `memory.json` | Dealers (lo aprendido, tratos), Workshop, flags enviados |
| `duels_memory.json` | Duelos: historial y perfiles de rivales |
| `broker_memory.json`, `bench_model.json`, `bench_snapshots.jsonl` | Market Test: libros grabados y política aprendida |
| `dashboard/history.json` | Historial de puntuación del dashboard |
| `historial_abuela.txt` | Chat con Abuela |
| `logs/*.log` | Logs (solo historial; los agentes escriben los suyos nuevos) |

## 3. Arrancar (5 procesos)

```bash
export BAZAAR_URL=https://bazaar.causaprima.ai
export BAZAAR_KEY=tk-...            # la key del equipo (por privado)
nohup python3 -u smart_agent.py  >> smart_agent.log 2>&1 &   # dealers, mercado, Workshop, flags, Pícaros
nohup python3 -u smart_duels.py  >> smart_duels.log 2>&1 &   # duelos
nohup python3 -u page_hunter.py  >> page_hunter.log 2>&1 &   # páginas y cartas sueltas
BROKER_KEY=$(cat .broker_key) nohup python3 -u broker.py >> broker.log 2>&1 &   # nuestro mercado v01
nohup python3 -u dashboard/app.py >> dashboard/dashboard.log 2>&1 &              # http://localhost:5051
ps -eo pid,command | grep -E "\.py" | grep -v grep                               # deben salir los 5, una vez cada uno
```

No hace falta instalar nada: todo (agentes y dashboard) usa solo la biblioteca estándar de Python 3.
Comprobaciones rápidas: `python3 smart_agent.py --selftest` (y `smart_duels`, `broker`, `workshop`, `flags`).

## 4. Calendario

Domingo abre a las 09:00 (ticks de 15 s): Chamberí, 150 primas, Duels III y la gran final (14:00).
Los agentes no necesitan nada para empezar; el broker y los duelos se activan solos.
