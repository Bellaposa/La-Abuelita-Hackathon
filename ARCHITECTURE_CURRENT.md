# ARCHITECTURE_CURRENT.md — auditoría del estado actual (solo lectura)

Auditoría hecha sobre `main` en `88e7faf` (2026-10-03). No se modificó ningún archivo salvo la creación de este documento.
Cada afirmación sale de leer el código, de `grep`, o de ejecutar los `--selftest`. Lo inferido va marcado **(inferido)**.

---

## A. Resumen general

- **Qué es:** un conjunto de **procesos Python independientes**, uno por área, sin orquestador. Cada uno hace su propio bucle `while True` y se despierta en cada tick con `b.wait_tick()`.
- **Comparten:**
  - La **misma clave de equipo** (`BAZAAR_KEY`) y por tanto el mismo límite de 5 req/s y **un solo accept por tick por equipo**. Un `BROKER_KEY` distinto para el broker.
  - El **SDK** `bazaar_sdk.py`, que es la única capa HTTP.
- **No comparten:** estado ni caja. Cada agente relee `b.me()` por su cuenta.
- **LLM:** **no existe ninguno**. Las únicas apariciones de "LLM" están en un docstring y en un `log()` de `smart_agent.py` que dicen "no LLM". No hay prompts, ni parsing de respuestas de modelo. Los textos son plantillas fijas.
- **Estrategia real:** vive en funciones puras dentro de cada script, por ejemplo `next_offer`, `plan_price`, `decide` y `buy_choice`. Las llamadas HTTP están mezcladas con la estrategia en las funciones `phase_*`, `act` y `step`.
- **Lo añadido recientemente y aún sin integrar:** `sim/` (simulador offline) y `agents/` (`trader_agent.py`, `portfolio_agent.py`). **Ningún proceso de producción los importa.** Solo `agents/trader_agent.py` importa `sim` (para su selftest).

## B. Diagrama textual del flujo principal

```
                        (cada proceso lanzado a mano; no hay launcher)
 BAZAAR_KEY ─┬─ smart_agent.py ──► Abuela (packs) ► El Chato (vende cartas) ► mercado Rastro (compra/vende/lista)
             ├─ smart_duels.py ──► /api/duels  (1 respuesta por duelo y tick)
             ├─ page_hunter.py ──► Rastro: compra cartas de página, vende singles, pone bids
             ├─ dashboard/app.py ► solo lectura: me/clock/offers/threads/duels + lee *.log + `ps`
             └─ (agent.py, dashboard.py, web_dashboard.py: alternativos/antiguos)
 BROKER_KEY ─┬─ broker.py ───────► /api/broker/book → bench_plan + public_plan(starter) → /api/broker/matches
             └─ (smart_broker.py: borrador antiguo del mismo trabajo)

 Todos ──► bazaar_sdk.Bazaar/Broker._call() ──► urllib ──► https://bazaar.causaprima.ai
 Estado local: memory.json · duels_memory.json · broker_memory.json (no existe aún) · historial_abuela.txt
 Sin integrar: agents/trader_agent.py, agents/portfolio_agent.py, sim/*
```

## C. Tabla de archivos

| Archivo | Responsabilidad real | Usado por | Estado |
|---|---|---|---|
| `bazaar_sdk.py` | Única capa HTTP. Reintentos (3) para `rate_limited` y `network` (los POST **no** se repiten tras fallo de red), y espera de tick | todos | activo (original, sin modificar) |
| `smart_agent.py` (759 l.) | Abuela (solo `sobre_barrio`), El Chato (vende cartas), mercado Rastro (comprar/vender duplicados), abrir packs, memoria | proceso propio; `dashboard/app.py` lee su log | **activo** (el más reciente) |
| `smart_duels.py` (393 l.) | Duelos: curva Boulware, aceptación por utilidad, días como segunda moneda, memoria | proceso propio | **activo** |
| `broker.py` (315 l.) | Market Test: decide cuándo cruzar pares bench; `public_plan` copiado del starter | proceso propio | **activo** |
| `page_hunter.py` (264 l.) | Completar páginas: compra faltantes, vende singles de sets que no construimos, bids | proceso propio | **activo** |
| `starter_broker.py` | Original. `broker.py` importa `public_plan` de él | `broker.py` | activo como dependencia |
| `dashboard/app.py` + `dashboard/index.html` | Panel Flask de solo lectura (:5051) con caché y `history.json` | proceso propio | activo |
| `agent.py` (245 l.) | Primer agente Abuela, bloqueante: compra cartas, vende, haggling con el bucle propio | nadie lo importa; nombrado en `SCRIPTS` del dashboard | posiblemente antiguo (sustituido por `smart_agent.py`) |
| `smart_broker.py` (127 l.) | Borrador de broker, comentarios en italiano | nadie | posiblemente antiguo |
| `dashboard.py` | Panel de terminal con `rich`, comentarios en italiano | nadie | posiblemente antiguo |
| `web_dashboard.py` + `index.html` (raíz) | Panel Flask, versión anterior de `dashboard/` | nadie | posiblemente antiguo |
| `agents/trader_agent.py` (515 l.), `agents/portfolio_agent.py` (314 l.) | Comercio entre equipos y modelo de valor. Selftests pasan | **nadie** | nuevo, sin integrar |
| `sim/` (`world.py`, `rivals.py`, `run.py`) | Simulador offline con 5 rivales | `agents/trader_agent.py` (selftest) | nuevo, sin integrar |
| `starter_agent.py`, `README.md`, `RULES.md` | Originales | — | referencia |
| `DEALERS.md`, `PITCH.md` | Documentación del equipo | — | documentación |
| `dealers_intel.json` | Volcado de 17 hilos (tick 112) | **ningún código lo escribe ni lo lee** | dato huérfano |

## D. Flujo de Dealers (`smart_agent.py`)

1. **Detección:** **no hay descubrimiento dinámico**. Nunca se llama a `b.dealers()`, `b.levels()` ni `b.schedule()` (`grep`). Los dealers están codificados como `"abuela"` y `"chato"`. Chato solo se activa si `"chato"` está en `me["unlocked"]`. Cualquier dealer nuevo (los levels de sábado y domingo) no se usará.
2. **Abrir conversación:**
   - Abuela: si no hay hilo abierto, y `cap >= min(abuela_min_price, 20)`, abre `{"buy": {"pack": "sobre_barrio"}}`.
   - Chato: `chato_candidate()` elige la carta que vende; abre `{"sell": {"assets": [id]}}`.
3. **Leer ofertas:** `read_thread()` recorre `t["messages"]` y extrae el precio de `m["offer"]` (estructura), nunca del texto. Se asume la forma `o["give"|"want"]["cash"]`.
4. **Decidir precio:**
   - Compra: `next_offer(cap, 26, ours, hers, inferred)`.
     - `cap = min(cash − CASH_RESERVE, 0.9 × valor privado del pack)`.
     - Apertura al 50 % de la lista (26, constante) o al 80 % de la mediana pagada, cuando hay ≥3 tratos.
     - Paso = `gap × k`, con `k = 0.25` por defecto o `best_k` inferido.
     - Se acepta si `ask <= cap` y `ask <= última_oferta + 4 %` (o `final`).
   - Venta a Chato: `next_offer_sell(floor, ...)`, espejo de la compra.
5. **Texto:** plantillas (`haggle_text`, `chato_text`). La amabilidad es fija. Se añade un agradecimiento si el texto de ella contiene "regalo/present/gift". El texto **no** influye en ningún precio.
6. **Aceptar o continuar:**
   - `accept`: busca la última `standing_offer` abierta de ese dealer y llama a `b.accept(id)`. **No comprueba que su `want.cash` coincida con el `price` decidido ni que esté por debajo del cap**; confía en que sea la misma que el último mensaje.
   - `walk`: `close_thread`.
   - `offer`: `b.say(..., price=)`.
7. **Memoria:** `observed.negotiations` (rondas y resultado) → `infer()` calcula `response_ratio`, `best_k` y `paid_median`. Solo se usan con N suficiente (`MIN_SAMPLES=5`, `MIN_DEALS=3`). Ver H.
8. **Dónde está la estrategia real:** `next_offer`, `next_offer_sell`, `pack_cap`, `pack_private_value`, `chato_candidate`.

Observaciones:
- `smart_agent.py` **solo compra sobres a Abuela**. Ya no compra cartas sueltas ni le vende duplicados, que sí hacía `agent.py` (regresión de funcionalidad, ver K).
- En `PITCH.md` el sobre vale ~10,6 P para nosotros y Abuela pide 21–24. Con `cap = 0.9 × valor` el agente casi nunca llega a abrir hilo (guarda `cap < best_ever`). **(inferido)** Si se aplica así en producción, la parte de **ladder de dealers** puntuaría poco, y El Chato exige tratos negociados con Abuela para desbloquearse.
- No se usa `b.flag()` en ningún sitio, aunque las reglas premian marcar mala fe correctamente.

## E. Flujo de Trading entre equipos

Hay **tres implementaciones solapadas** del mismo trabajo, todas en El Rastro:

| | `smart_agent.phase_market` | `page_hunter.step` | `agents/trader_agent.py` (nuevo, no ejecutado) |
|---|---|---|---|
| Leer ofertas | `b.board("rastro")` + `b.my_offers()` (las dirigidas a nosotros) | igual | todas las venues de `b.venues()` |
| Valor | `b.value(ref)` (máx. 12/tick, caché) + `marginal_value` | `book × afinidad` (sin llamar a `b.value`) | `b.value()` + modelo de `portfolio_agent` |
| Compra | cualquier carta con `valor − coste ≥ max(3, 10 %)` | solo faltantes de página, con tope que incluye la mitad del bonus | ídem + trueque |
| Vende | duplicados (n≥2) al bid si `neto − pérdida ≥ 2` | singles de sets que no construimos | duplicados |
| Lista | duplicados, ≤3/tick, ≤28 abiertas | singles ≤3/tick + bids al 60 % del tope | ≤8/tick, ≤28 abiertas |
| Conversaciones con equipos | **ninguna** | ninguna | ninguna |
| Límite de accept/tick | solo dentro del proceso (`can_accept`) | solo dentro del proceso | solo dentro del proceso |

- Se acepta mirando la **estructura** (`give`/`want`/`types`), no el texto.
- No hay cancelación de ofertas propias en ningún lado: `b.cancel()` no se llama. Las ofertas caducan a los 40 ticks.
- No hay hilos con otros equipos (`open_thread(with=<team>)`): nunca se usa.
- `smart_agent` y `trader_agent` no tienen control de "alimentar a otro equipo" salvo `trader_agent` (máx. 3 tratos por contraparte cada 10 min).

## F. Flujo de Broker

- **Versión activa (`broker.py`):**
  - Cada 1 s lee `broker.clock()` y `broker.book()`.
  - Reconstruye por oferta el historial de quotes en `Track`.
  - Para cada par que cruza (mejor bid contra mejor ask, por "run" = prefijo del id `b12-7`) decide `MATCH` o `WAIT` en `decide()`:
    - `MATCH` si quedan ≤2 ticks, si algún lado es "urgente" (edad ≥ 25º percentil observado, o ≥50 % de la sesión), o si no se observa que **ambos** se relajan.
    - `WAIT` solo si ambos se relajan.
  - Precio = punto medio, que según el docstring no cambia las ganancias.
  - Después ejecuta `broker.match` y añade `public_plan(book)` del starter para las ofertas públicas.
- **Versión antigua (`smart_broker.py`):** misma idea con una heurística distinta (`is_yielding`; fuerza el match a las 10 llamadas).
  - Su "edad" cuenta **llamadas al plan**, no ticks.
  - No se importa desde ningún sitio.
- **Evidencia propia:**
  - El `--selftest` de `broker.py` mide `match-at-once 0.902` frente a `this broker 0.871` (300 sesiones sintéticas inventadas): **la regla de espera rinde peor que cruzar al instante en su propio simulador**.
  - `PITCH.md` lo reconoce y deja `BROKER_WAIT=0` como interruptor.
  - El simulador `sim` coincide (el baseline "espera" rinde menos).
- **Duración de sesión:** `expires_tick` del libro → si no, `schedule()` → mediana observada → `SESSION_TICKS=16`. Se llama a `/api/schedule` con `broker._call(...)` privado, porque `Broker` no expone `schedule`.
- **Apertura de venue:** el código **no abre ningún venue**; el broker supone que ya existe uno `board`.

## G. Flujo de Duels (`smart_duels.py`)

- **Detección:** `b.duels()` cada tick; ignora estados distintos de `None/open/live/active`. El id real es `duel` (con `id` de respaldo).
- **Rol:** `side_of(role)` = +1 vendedor, −1 comprador. `your_limit` es el límite privado.
- **Oferta:** curva `anchor → reservation` con exponente `BETA=2`, que se modifica según el rival (firme → `BETA×0.6`; cede → `×1.3`).
  - Nunca retrocede.
  - Nunca cruza el límite (`MIN_MARGIN=1`).
  - Un `assert` lo comprueba justo antes de enviar.
  - Si queda ≤1 ronda, va a la reserva.
- **Aceptar:** `should_accept` compara la utilidad actual con la de la próxima oferta, y con una estimación del valor de esperar (tasa de cesión, decaimiento 6 % por ronda, riesgo por tiempo).
  - Solo razona con tasas si hay ≥2 movimientos del rival.
- **Memoria del rival:** por duelo (`observed.history`). **No existe perfil de rival entre duelos.**
- **Días:** `plan_days` y `utility` suponen `DAYS_SIGN = +1`, sin verificar. Es el supuesto de más riesgo del archivo (el propio código lo marca como tal).
- **LLM:** ninguno. El texto (`message`) es una plantilla.
- **Datos reales guardados:** `duels_memory.json` tiene 24 duelos. Los 3 `raw_samples` son todos de sesiones solo de precio (`your_days_weight: null`), así que **el camino de días nunca se ha ejercitado con datos reales**.
- En `duels_memory.json` hay 24 duelos registrados y 11 tienen `done=True` (los que aceptamos nosotros). `PITCH.md` habla de 13 cerrados con 11 acuerdos. **(inferido)** Los demás acabaron porque aceptó el rival o por plazo; el código no distingue esos casos.

## H. Flujo de Memory

| Archivo | Escribe | Cuándo | Lee | Influye en decisiones | Se guarda pero no se usa |
|---|---|---|---|---|---|
| `memory.json` | `smart_agent.save_memory` (atómico, `os.replace`) | cada tick | `load_memory` al arrancar | `observed.negotiations` → `infer()` (`best_k`, `paid_median`); `abuela_min_price` (umbral para abrir hilo); `active`/`active_chato`; `chato_block_until`; `chato_tried` | `chat_history` (solo se añade y se recorta a 20); `inferred` (se recalcula en cada llamada, el guardado es solo informativo) |
| `duels_memory.json` | `smart_duels.save_mem` (atómico) | cada tick con duelos, y cada 10 pasos | al arrancar | `state` por duelo (`our_last`, `anchor`, `rounds`, `done`) | `inferred`, `finished`, `raw_samples` (solo para análisis manual) |
| `broker_memory.json` | `broker.main` (**no atómico**, `open(...,"w")`) | al terminar una sesión | `Track(load_mem())` | `lifetimes`, `session_lens` → percentil de salida | no existe aún (en `.gitignore`) |
| `historial_abuela.txt` | `smart_agent.log_chat` (append) | cada mensaje | **nadie** | no | todo (127 líneas, crece sin límite, está versionado) |
| `dealers_intel.json` | **nadie** (volcado manual) | — | **nadie** | no | todo (se usó para escribir `DEALERS.md`) |
| `dashboard/history.json` | `dashboard/app.py` (no atómico) | cada 60 s | al arrancar | solo el gráfico | — |

- **Bug confirmado en `smart_duels.record_finished`:** guarda con `d.get("id")`, pero la API usa la clave `duel`. Resultado real: `finished` tiene **una única clave `"None"`**, así que todos los duelos terminados se pisan.
- Cada archivo tiene **un único proceso escritor**, salvo que se lance una instancia duplicada (ver L).

## I. Flujo de API / Clock

- **Centralizado:** todas las llamadas pasan por `bazaar_sdk._Http._call` (urllib). No se usa `requests`.
- **Excepción:** `broker.py:175` llama a `broker._call("GET", "/api/schedule")` (método privado).
- **Autenticación:** cabecera `X-Team-Key` o `X-Broker-Key`, desde variable de entorno.
- **Errores:** `BazaarError(code, message, status)`. Cada agente captura `BazaarError` y registra; casi todos tienen además un `except Exception` genérico con `sleep(5)`.
- **Retries:** el SDK reintenta `rate_limited` (0,25 s × intento), `network` (solo GET) y `wait_for_tick` (solo con `wait_on_tick=True`). Todos los agentes usan `wait_on_tick=False`, y gestionan el tick con `b.wait_tick()` al final del bucle.
- **Clock:** `b.clock()` (`paused`, `tick`, `next_tick_in`). **Nadie lee `clock["limits"]`** (límites de accept, mensajes y ofertas por tick). Están fijados a mano: 3 listados/tick, 28 abiertas, 12 `value()`/tick, 1 accept.
- **Carga de peticiones:**
  - Todos los procesos de equipo se despiertan en el mismo instante del tick y comparten **5 req/s**.
  - `broker.py` y `smart_agent` leen varias veces por tick (`b.thread()` por cada hilo abierto, `b.me()`, `b.catalog()`, `b.venues()`…).
  - El broker consulta 2 req/s con su propia clave.

## J. Duplicaciones

1. **Tres comercios en El Rastro:** `phase_market`, `page_hunter.step`, `agents/trader_agent.py`. Repiten `fee_of`, `marginal_value`, `listing_plan`/`sell_stock`, `rank_pick`/`pick`.
2. **`fee_of` definida 3 veces** (`smart_agent`, `page_hunter`, y su equivalente en `starter_broker`/`smart_broker`).
3. **`log()` redefinida en cada archivo** (6+ copias idénticas).
4. **Carga/guardado de memoria JSON** repetido en 3 archivos con variantes (atómico y no atómico).
5. **Brokers:** `broker.py` y `smart_broker.py` implementan lo mismo. Además, `public_plan` existe en `starter_broker.py` y `smart_broker.py`, y `bench_plan_naive` en `broker.py` repite `starter_broker.bench_plan`.
6. **Agentes Abuela:** `agent.py` (haggling por cartas) y `smart_agent.phase_abuela` (solo packs).
7. **Dashboards:** `dashboard.py` (rich), `web_dashboard.py` + `index.html`, y `dashboard/app.py` + `dashboard/index.html`.
8. **Lógica de valor de cartas:** `smart_agent.marginal_value`, `page_hunter.value_of_card`, `agents/portfolio_agent.py`. Las tres usan fórmulas ligeramente distintas (con o sin `copy_marginals`).
9. **Estado de la caja:** ninguno lo comparte.

## K. Posible código muerto (no demostrable con certeza)

- `agent.py`: ningún archivo lo importa. **POSSIBLY DEAD.** Puede seguir lanzándose a mano; el dashboard lo lista en `SCRIPTS`.
- `smart_broker.py`: ningún import, ningún lanzador. **POSSIBLY DEAD.** Misma salvedad.
- `dashboard.py`, `web_dashboard.py`, `index.html` (raíz): sin imports. **POSSIBLY DEAD.**
- `dealers_intel.json`: ningún código lo toca. Dato histórico.
- `historial_abuela.txt`: solo se escribe.
- `smart_agent.chat_history`, `memory.json["inferred"]`, `duels_memory.json["inferred"]`: solo escritura.
- `broker.bench_plan_naive`: solo se usa en el selftest.
- `smart_agent.bucket`/`PHRASES` están en uso. `starter_agent.py` es referencia.
- `agents/` y `sim/`: sin llamadores productivos; no es código muerto, pero **no está conectado**.
- **Mezcla de pruebas y producción:** los `selftest()` conviven dentro de los scripts de producción (`smart_agent`, `smart_duels`, `broker`, `page_hunter`).
- `smart_duels.py --selftest` **falla en Windows**: usa `MEM_FILE = "/tmp/duels_selftest.json"` (línea 357). Resultado real: `FileNotFoundError`.

## L. Riesgos técnicos

1. **Acciones duplicadas / doble accept entre procesos.** `smart_agent`, `page_hunter` (y `trader_agent`, si se lanza) aceptan en El Rastro. Cada uno protege el "1 accept por tick" **solo dentro de su proceso**. El segundo recibe `wait_for_tick` (inocuo), pero cuál gana es una carrera, y puede ganar una oferta peor. Ningún proceso reserva efectivo para otro.
2. **Estado viejo en el tick.** `smart_agent` lee `me` una vez y se lo pasa a `phase_abuela`, `phase_chato` y `phase_market`. Lo aceptado "se liquida en el siguiente tick": el efectivo de `me` ya no es real. Si hay un trato pendiente con Abuela, el mercado ve un efectivo inflado. Otros procesos tampoco ven lo comprometido. Lo salva `insufficient_cash`, que no cuesta nada, pero puede bloquear una oferta buena.
3. **Aceptar sin verificar el precio.** `phase_abuela` y `phase_chato` aceptan `standing_offers[-1]` del dealer sin comprobar que su importe sea el `price` evaluado ni que cumpla el cap. Es seguro solo si la última oferta abierta es la que se miró.
4. **Instancias duplicadas.** El dashboard detecta duplicados con `ps` (lo cual indica que ya ocurrió). Dos `smart_agent` pisan `memory.json`, y duplican hilos y mensajes. Ninguna instancia usa un lock. **(El `ps` del dashboard no funciona en Windows.)**
5. **Límites del servidor fijos en el código.** Los números de accept, mensajes y listados no se leen de `clock["limits"]`. Si los organizadores los cambian (lo anuncian en las reglas), el agente los violará o los desaprovechará.
6. **Dealers futuros ignorados.** No hay descubrimiento (`dealers()`/`levels()`/`schedule()` no se usan). Los niveles del sábado/domingo no se aprovecharán, y el ladder pesa más en niveles altos.
7. **`record_finished` pisa los duelos terminados** (clave `"None"`), así que se pierde la calibración que `PITCH.md` quiere hacer.
8. **Supuestos sin verificar:** `DAYS_SIGN=+1` y el camino de días nunca ejercitado con datos reales.
9. **Reabrir hilos de Abuela tras `walk`:** `phase_abuela` devuelve a la siguiente iteración y abre otro hilo, lo que consume la cuota (3 packs/hora) y puede causar `cooloff`. **(inferido)** No hay espera tras retirarse, aunque `PITCH.md` dice "pausa tras retirarnos". Esa pausa existe solo para Chato (`chato_block_until`).
10. **Escritura de memoria no atómica** en `broker.py` y `dashboard/app.py`: un corte a mitad deja el JSON truncado. `load_mem` lo tolera devolviendo `{}`, así que se pierde la memoria, sin fallar.
11. **Texto vs. estructura:** bien en general. `tone()` lee palabras del dealer ("final", "walk"), pero solo para elegir `k`, y el `final` real viene de la estructura (`o.get("final")`). **Ningún texto controla dinero.** Falta `b.flag()` para castigar mentiras.
12. **`page_hunter` compra a ganancia 0.** `page_hunter.log` (versión anterior del código) muestra `BUY SAL-01: cost 11 cap 11.0 gain 0.0`; la versión actual exige `gain >= max(2, 10 %)`. El log es anterior al arreglo **(inferido)**.
13. **Selección de copia al vender.** `phase_market` entrega `ids[ref][-1]` (el último del array, no la copia de menor valor), mientras `listing_plan` guarda el menor serial. Pueden contradecirse.
14. **`smart_agent` sin reserva de efectivo por defecto** (`CASH_RESERVE=0`), mientras `agent.py` reservaba 280 P para el bond del venue. Abrir un venue (250+20 P) puede quedar sin fondos.
15. **Broker:** `schedule_ticks` toma el primer evento `bench` futuro como longitud de sesión. Las `lifetimes` incluyen traders que desaparecen por fin de sesión, lo que sesga el percentil hacia abajo.
16. **Datos versionados que cambian sin parar:** `memory.json`, `duels_memory.json`, `dashboard/history.json`, `historial_abuela.txt`, y también `page_hunter.log`, `smart_duels.log` y `dashboard/dashboard.log`, a pesar de `.gitignore`: ya estaban rastreados antes. Generan commits ruidosos y conflictos entre compañeros (hay commits de `AP`, `Cursor Agent` y nosotros sobre los mismos archivos).

## M. Clasificación preliminar

| Etiqueta | Archivos |
|---|---|
| **KEEP** | `bazaar_sdk.py`; `smart_duels.py` (núcleo: `plan_price`, `should_accept`, `utility`); `dashboard/` (app + index); lógica pura de `smart_agent.py` (`next_offer`, `next_offer_sell`, `infer`, `pack_private_value`); `page_hunter.py` (lógica `page_targets`, `buy_choice`); `sim/` |
| **REFACTOR** | `smart_agent.py` (separar fases, descubrimiento de dealers, estado compartido); `smart_duels.py` (`record_finished`, ruta de `/tmp`, memoria entre rivales); `broker.py` (decidir `BROKER_WAIT` con datos, escritura atómica, `schedule` público); `page_hunter.py` + `phase_market` (unificar comercio) |
| **REPLACE** | Las tres implementaciones de comercio en El Rastro → una sola (candidata: `agents/trader_agent.py` + `agents/portfolio_agent.py`, tras validarla en vivo); los `log()`/`fee_of`/carga de memoria repetidos → un módulo común |
| **POSSIBLY DEAD** | `agent.py` (ningún import; sigue en `SCRIPTS` del dashboard); `smart_broker.py` (ningún import ni lanzador); `dashboard.py`, `web_dashboard.py`, `index.html` de la raíz (sin imports; sustituidos por `dashboard/`); `dealers_intel.json` (sin código que lo use; sirvió para `DEALERS.md`); `historial_abuela.txt`; `broker.bench_plan_naive` (solo selftest) |
