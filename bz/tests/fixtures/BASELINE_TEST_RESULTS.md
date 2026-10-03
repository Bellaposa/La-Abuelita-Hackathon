# BASELINE_TEST_RESULTS.md — selftests actuales (Fase 0)

- **Fecha:** 2026-10-03, ~11:04 (hora local de la máquina).
- **Commit probado:** `098ff55636a548882ae8cc9e0fe2f7e0ee0e3075` (tag `legacy-v1-baseline`).
- **Entorno:** Windows 11, Python 3.14.6, shell Git Bash, directorio de trabajo = raíz del repo.
- **Sin red:** los selftests no hacen peticiones al servidor. No se arregló nada; un fallo es un resultado válido de baseline.
- **Archivos de producción** (`smart_agent.py`, `smart_duels.py`, `page_hunter.py`, `broker.py`, `bazaar_sdk.py`) y los JSON de memoria: hash SHA-256 idéntico antes y después de ejecutar los selftests.

| Script | Comando | Exit code | Resultado |
|---|---|---|---|
| `smart_agent.py` | `python smart_agent.py --selftest` | **0** | `selftest OK` |
| `smart_duels.py` | `python smart_duels.py --selftest` | **1** | **FALLA** (`FileNotFoundError`, ruta `/tmp`) |
| `broker.py` | `python broker.py --selftest` | **0** | `selftest OK` |
| `page_hunter.py` | `python page_hunter.py --selftest` | **0** | `selftest OK` |

## Salida completa

### `python smart_agent.py --selftest` → exit 0
```
floor 17, she concedes half  -> (17, [8, 11, 13, 15, 16], 'deal')
she never concedes           -> (17, [8, 14, 15, 16, 17], 'she takes our offer')
her floor above our cap      -> (None, [8, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24], 'walk')
she announces final          -> (17, [8, 11], 'deal')
sell: he pays up to 22 -> (20, 'he takes our ask') | up to 15 (below floor) -> (None, 'walk') | final at round 2 -> (20, 'he takes our ask')
11:04:11 Chato negotiation 7 ended: closed no_progress received None
selftest OK
```
Valores de referencia útiles para `REGRESSION_CHECKLIST.md` (DLR-05, DLR-06, DLR-08, DLR-12): la secuencia de ofertas del primer caso es `[8, 11, 13, 15, 16]`.

### `python smart_duels.py --selftest` → exit 1
```
11:04:11 duel 1 seller r1 left 15 | rival 30.0 dNone (move None) stats None | U now -20.0 next 25.0 | rival offer below our margin
11:04:11   OFFER duel 1: 75 dNone (beta 2.0, frac 0.0)
Traceback (most recent call last):
  File "C:\Users\alexs\OneDrive\Escritorio\AbuelaMadrid2026\smart_duels.py", line 391, in <module>
    selftest()
    ~~~~~~~~^^
  File "C:\Users\alexs\OneDrive\Escritorio\AbuelaMadrid2026\smart_duels.py", line 373, in selftest
    step(sim, mem, t, strict=True)
    ~~~~^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "C:\Users\alexs\OneDrive\Escritorio\AbuelaMadrid2026\smart_duels.py", line 288, in step
    save_mem(mem)
    ~~~~~~~~^^^^^^^^^^^
  File "C:\Users\alexs\OneDrive\Escritorio\AbuelaMadrid2026\smart_duels.py", line 63, in save_mem
    with open(tmp, "w") as f:
         ~~~~^^^^^^^^^^
FileNotFoundError: [Errno 2] No such file or directory: '/tmp/duels_selftest.json.tmp'
```
Causa: `smart_duels.py:357` fija `MEM_FILE = "/tmp/duels_selftest.json"`; esa ruta no existe en Windows. Es el defecto ya conocido (hotfix 1.7 de la Fase 1, **no corregido aquí**). El selftest se interrumpe en el primer paso, así que **la lógica de duelos no queda comprobada por este selftest**.

### `python broker.py --selftest` → exit 0
```
efficiency over 300 synthetic sessions: match-at-once 0.902 | this broker 0.871
selftest OK (the simulation uses made-up dynamics: it checks the logic, not real-world performance)
```
Referencia para BRK-11: `0.902` (cruzar al instante) frente a `0.871` (este broker).

### `python page_hunter.py --selftest` → exit 0
```
selftest OK
```
