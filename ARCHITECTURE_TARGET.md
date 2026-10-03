# ARCHITECTURE_TARGET.md — arquitectura V2 (propuesta, nada implementado)

Fuente: `ARCHITECTURE_CURRENT.md` (commit `b1160d7`). Este documento solo propone. El orden y los criterios de migración están en `REFACTOR_PLAN.md`, y los comportamientos a proteger en `REGRESSION_CHECKLIST.md`.

---

## 0. Principios y dónde me aparto de tu esquema

Tu esquema (CORE / INTELLIGENCE / AGENTS / STRATEGIES / LEARNING) es válido como mapa mental. En el repo propongo **no** crear una carpeta `strategies/` separada de `agents/`:

- La estrategia real que existe hoy (`next_offer`, `plan_price`, `decide`, `buy_choice`) es **específica de cada dominio** y ya son funciones puras. Separarla de su adaptador (el código que lee estado y propone acciones) añade ficheros sin separar nada nuevo.
- Lo que sí es compartido por varios dominios (valor privado, modelo de rival, memoria, priorización) va a `intel/`.
- Por eso: **un paquete por dominio**, y dentro de cada uno `policy.py` (puro, sin I/O) y `agent.py` (adaptador fino). La frontera "estrategia vs. ejecución" se mantiene, pero dentro de cada dominio.

Reglas que valen para todo el código nuevo:

1. **Solo `core/api_client.py` hace HTTP.** Ningún otro módulo importa `urllib` ni `bazaar_sdk`.
2. **Las políticas son puras:** `(vista de estado, memoria de solo lectura) → lista de ActionProposal`. No escriben archivos, no llaman a la API, no leen el reloj del sistema.
3. **Solo el orchestrator ejecuta acciones**, y solo después de que el validator las apruebe.
4. **Un solo escritor por archivo de datos**, siempre con escritura atómica.
5. Nada nuevo se mueve ni se borra de la raíz hasta que su sustituto esté verificado (ver plan).

---

## 1. Estructura de carpetas

Paquete nuevo `bz/` (nombre corto, no choca con `agents/` ni `sim/`, que ya existen). Los scripts actuales de la raíz siguen intactos durante la migración.

```
bz/
  core/
    config.py          # flags + parámetros (lee flags.json cada tick, env como override)
    log.py             # log común (reemplaza las ~6 copias de log())
    jsonio.py          # lectura/escritura JSON atómica + lock de instancia única
    api_client.py      # único punto HTTP: envuelve bazaar_sdk, cuenta peticiones, modo grabación
    models.py          # dataclasses: Offer, Thread, DealerState, DuelState, Asset, ...
    parser.py          # respuestas crudas de la API → models (tolerante a campos que faltan)
    clock.py           # reloj + límites dinámicos (clock["limits"]) → TickBudget
    state.py           # StateManager: vista única de me/offers/threads/duels + pendientes
    pending.py         # PendingLedger: acciones enviadas y aún no liquidadas
    actions.py         # ActionProposal, Decision, ActionResult
    risk.py            # RiskManager: presupuesto de efectivo, reservas, límites por tick
    validator.py       # FinalValidator: revalida cada acción contra estado fresco
    reconciler.py      # SettlementReconciler: compara lo esperado con el estado del tick siguiente
    orchestrator.py    # bucle único por team key
    shadow.py          # modo sombra: ejecuta A, registra B
    recorder.py        # RecordingClient: cliente falso que graba escrituras (para sombra y replay)
  intel/
    values.py          # motor de valor/utilidad privado (portfolio + páginas)
    opponents.py       # modelo de rival/dealer: respuesta a concesiones, firmeza
    memory.py          # MemoryStore: hechos observados vs. inferidos (API única)
    opportunities.py   # OpportunityEngine: puntúa y prioriza propuestas de todos los agentes
  dealers/
    policy.py          # next_offer, next_offer_sell, pack_cap (puras)
    agent.py           # descubre dealers, mantiene hilos, propone acciones
    discovery.py       # dealers()/levels()/schedule() → catálogo de dealers activos
  trading/
    policy.py          # evaluate, listing_plan, bid_plan, trueque (puras)
    agent.py
    pages.py           # page_targets (objetivos de página)
  duels/
    policy.py          # plan_price, plan_days, should_accept, utility (puras)
    buyer.py           # política de comprador (parámetros y anclas)
    seller.py          # política de vendedor
    agent.py
  broker/
    policy.py          # decide, classify, bench_plan, public_plan (puras)
    runner.py          # proceso separado con BROKER_KEY
    track.py           # Track: historial de quotes y vidas observadas
  observe/
    metrics.py         # contadores y series (ticks, acciones, rechazos, p&l por dominio)
    snapshot.py        # escribe snapshot.json (estado del equipo) para el dashboard
    replay.py          # reproduce un JSONL de decisiones contra una política
    experiments.py     # registro de variantes (flag → resultados)
  tests/
    fixtures/          # payloads reales capturados (duels_memory raw_samples, hilos de dealers_intel, clock)
    golden/            # tests de caracterización del código legacy (ver REGRESSION_CHECKLIST.md)
dashboard/             # se mantiene; pasa a leer snapshot.json en vez de llamar a la API
sim/                   # se mantiene; se adapta para probar bz.* contra FakeBazaar
legacy/                # (al final) destino de los scripts archivados
```

Una sola excepción a "un paquete por dominio": `duels/` tiene `buyer.py` y `seller.py` porque la asimetría vendedor/comprador es real (ancla de apertura, signo de utilidad) y hoy está escondida en `side_of`/`OPEN_ANCHOR`.

---

## 2. Módulos: responsabilidad, entradas, salidas, llamadores y prohibiciones

### core

| Módulo | Responsabilidad única | Recibe | Devuelve | Lo llama | NO debe hacer |
|---|---|---|---|---|---|
| `config.py` | Resolver flags y parámetros en el momento (archivo → env → defecto) | nombre de flag | valor tipado | todos | llamar a la API; guardar estado |
| `log.py` | Log con formato común y JSONL opcional de decisiones | nivel, mensaje, dict | — | todos | decidir nada |
| `jsonio.py` | Leer/escribir JSON atómico; lock de instancia única por nombre | ruta, objeto | objeto / bool de lock | memory, snapshot, orchestrator | conocer el formato de ningún archivo |
| `api_client.py` | Único HTTP: envuelve `bazaar_sdk`, reintentos, contador de peticiones, modo `record` | método, ruta, cuerpo | JSON crudo o `BazaarError` | state, orchestrator (ejecutar), discovery | decidir, interpretar respuestas, ejecutar sin pasar por el validator |
| `models.py` | Definir las estructuras de dominio (sección 3) | — | dataclasses inmutables | todos | tener lógica de negocio ni I/O |
| `parser.py` | Normalizar JSON crudo a `models`; un solo lugar que conoce las formas de la API | dict crudo | modelos, o `ParseWarning` | api_client→state | lanzar excepción por un campo que falta (devuelve `None` y avisa) |
| `clock.py` | Mantener tick, `next_tick_in` y límites efectivos (`TickBudget`) | respuesta de `/api/clock` | `ClockView` + `TickBudget` | orchestrator, risk | tener números mágicos: si `limits` no viene, usa el último valor conocido y avisa |
| `state.py` | Fuente única de verdad del equipo en el tick actual | respuestas parseadas | `State` (solo lectura para agentes) | orchestrator; los agentes lo reciben | ejecutar acciones; modificar memoria |
| `pending.py` | Registrar acciones enviadas y su efecto esperado hasta liquidar | `ActionResult` | compromisos de efectivo/activos | state, risk, reconciler | decidir si una acción es buena |
| `actions.py` | Definir `ActionProposal`, `Decision`, `ActionResult` | — | dataclasses | agentes, validator, orchestrator | lógica |
| `risk.py` | Presupuesto y reglas globales: efectivo libre, reservas, límites por tick, contraparte | `State`, `TickBudget`, propuestas | propuestas filtradas con motivo | orchestrator | ver el contenido de la estrategia (solo importes y tipos) |
| `validator.py` | Revalidar cada acción **justo antes** de ejecutarla, contra datos frescos | `ActionProposal`, estado fresco | `Decision` (aprobada/rechazada + motivo) | orchestrator | modificar la propuesta; elegir otra acción |
| `reconciler.py` | Tick siguiente: ¿lo que esperábamos pasó? | `PendingLedger`, `State` nuevo | liquidadas, fallidas, expiradas | orchestrator | reintentar acciones por su cuenta |
| `orchestrator.py` | Secuenciar el tick (sección 6) | — | — | `main` | contener estrategia; hacer HTTP directo |
| `shadow.py` | Ejecutar política vieja y nueva, ejecutar una, registrar ambas | dos funciones + contexto | decisión ejecutable + registro | orchestrator | ejecutar la acción de la política en sombra |
| `recorder.py` | Cliente falso con la misma superficie que `Bazaar` que graba escrituras | llamadas | respuestas enlatadas + lista de escrituras | shadow, tests, replay | tocar la red |

### intel

| Módulo | Responsabilidad única | Recibe | Devuelve | Lo llama | NO debe hacer |
|---|---|---|---|---|---|
| `values.py` | Valor privado de cartas, packs, páginas y copia marginal | `Portfolio` (de `State`), catálogo, afinidades | valor, pérdida al vender, techo de compra, suelo de venta | todas las políticas | llamar a `b.value()` directamente (lo pide el state y lo cachea) |
| `opponents.py` | Perfil de rival o dealer por observaciones | historial de rondas | respuesta a concesiones, firmeza, n de muestras | dealers, duels | guardar nada; sacar conclusiones con n bajo |
| `memory.py` | Persistencia de hechos y de inferencias, separados | eventos observados | lectura de hechos e inferencias con n | agentes (lectura), orchestrator (escritura) | decidir; ser escrito desde una política |
| `opportunities.py` | Puntuar y ordenar propuestas de todos los agentes con una moneda común | propuestas con utilidad esperada | lista priorizada | orchestrator | ejecutar; saltarse risk/validator |

### agents (por dominio)

| Módulo | Responsabilidad | Recibe | Devuelve | NO debe hacer |
|---|---|---|---|---|
| `dealers/policy.py` | Precio y acción de una negociación con un dealer (compra y venta) | listas `ours`/`hers`, tope o suelo, `inferred` | `(acción, precio, motivo)` | I/O; leer texto como señal de dinero |
| `dealers/discovery.py` | Qué dealers existen, cuáles están abiertos, y su menú | `dealers()`, `levels()`, `schedule()` ya parseados | lista de `DealerInfo` | abrir hilos |
| `dealers/agent.py` | Mantener un hilo por dealer; proponer abrir, ofertar, aceptar o cerrar | `State`, `DealerInfo`, memoria (lectura) | `ActionProposal[]` | llamar a la API; abrir hilos fuera de la cuota |
| `trading/policy.py` | Evaluar ofertas ajenas; planificar listados y bids | ofertas, valores, venue (fees) | oportunidades con ganancia | I/O |
| `trading/pages.py` | Objetivos de página y tope con bonus compartido | catálogo, tenencias, afinidades | objetivos con tope | I/O |
| `trading/agent.py` | Convertir oportunidades en propuestas | `State`, tableros de venues | `ActionProposal[]` | aceptar directamente; ignorar el presupuesto de `risk` |
| `duels/policy.py` | Precio, días y aceptación de un duelo | `DuelState`, historial, parámetros | `(acción, precio, días, motivo)` | I/O; fijar el signo de los días por su cuenta (viene de config) |
| `duels/buyer.py` / `seller.py` | Parámetros de ancla y reserva por rol | límite, rol | ancla, reserva | lógica de aceptación |
| `duels/agent.py` | Un duelo → una propuesta por tick | `State.duels` | `ActionProposal[]` | enviar más de un mensaje por duelo y tick |
| `broker/policy.py` | Cuándo y con quién casar | libro, `Track`, tick | `BrokerMatch[]` | I/O |
| `broker/runner.py` | Bucle del broker (proceso aparte, `BROKER_KEY`) | libro | llamadas `match` (a través de su propio validator) | compartir clave o estado con el orchestrator del equipo |

### observe

| Módulo | Responsabilidad | NO debe hacer |
|---|---|---|
| `metrics.py` | Contar acciones, rechazos por motivo, peticiones por tick, ganancia estimada por dominio | influir en decisiones |
| `snapshot.py` | Escribir `snapshot.json` atómico en cada tick (estado + pendientes + últimas decisiones) | llamar a la API |
| `replay.py` | Reproducir decisiones grabadas (JSONL) contra una política | ejecutar en vivo |
| `experiments.py` | Asociar una variante (flag) con sus métricas | decidir ganadores automáticamente |

---

## 3. Contratos entre capas

Cadena de datos (cada flecha es un contrato; nada salta capas):

```
API ──raw JSON──► parser ──models──► StateManager ──State (solo lectura)──► agente/política
  ▲                                                                              │
  │                                                                     ActionProposal[]
  │                                                                              ▼
  │                                               OpportunityEngine (puntúa) ──► RiskManager (filtra)
  │                                                                              ▼
  └──── api_client ◄── orchestrator (ejecuta) ◄── FinalValidator (revalida con estado fresco)
                              │
                              ▼
                  PendingLedger ──► (tick siguiente) Reconciler ──► State + Memory
```

Estructuras conceptuales (campos mínimos; los nombres son orientativos):

**`State`** (inmutable durante el tick; lo crea `StateManager`)
- `tick`, `clock_view` (segundos al siguiente tick, `paused`), `budget: TickBudget`.
- `team`: `id`, `cash`, `cash_free` (= `cash` − compromisos pendientes − reservas), `level`, `unlocked`, `affinity`.
- `assets`: lista de `Asset` (`id`, `kind`, `ref`, `serial`, `rarity`, `your_value`, `locked_by`).
- `offers_mine`, `offers_to_me`, `boards[venue]` (ofertas por venue, con `fee_bps` y `fee_per_card`).
- `threads[dealer|team]`: `Thread`.
- `duels`: lista de `DuelState`.
- `pending`: acciones enviadas y no liquidadas.
- `fetched_at` por cada fuente (para detectar datos viejos).

**`Offer`**
- `id`, `maker`, `to`, `status`, `venue`, `give: Side`, `want: Side`, `final`, `created_tick`, `expires_tick`.
- `Side` = `{cash, assets[], types[]}`. El `parser` normaliza `types` y `cards` a una sola forma.
- `shape`: `buy_card | sell_card_to_bid | swap | pack | cash_only | unknown`. `unknown` nunca se acepta.

**`DealerState`**
- `dealer_id`, `status` (`locked | announced | open`), `unlock_rule`, `menu`, `list_prices`.
- `thread: Thread | None` y `quota_used/quota_hour`.
- `cooloff_until`, `closed_reason` del último hilo.
- `observations`: rondas `(nuestro movimiento, respuesta, hueco)`.

**`DuelState`**
- `duel_id`, `session`, `role`, `limit` (nuestro límite privado), `issues`, `days_weight`, `days_sign_source` (`config | probed | assumed`).
- `deadline_tick`, `decay_per_round`, `rounds`.
- `rival_offer: (price, days)`, `our_offer`, `history`, `status`, `result`.

**`TradeOpportunity`**
- `kind` (`buy | sell | swap | list | bid`), `offer_id`, `ref`, `price`, `fee`, `counterparty`.
- `our_value_gain`, `cash_needed`, `roi`, `confidence` (valor autoritativo de `b.value()` o estimado), `expires_tick`.

**`BrokerMatch`**
- `sell_id`, `buy_id`, `price`, `run`, `reason` (`session_end | may_leave | both_firm | default`), `ticks_left`, `crossing_checked: bool`.

**`ActionProposal`** (lo que produce un agente; no se ejecuta nunca directamente)
- `id` (determinista: dominio + objetivo + tick → clave de idempotencia).
- `domain` (`dealer | trading | duel | broker | housekeeping`), `kind` (`accept | say | open_thread | close_thread | list_offer | cancel | duel_say | duel_accept | open_pack | match`).
- `target` (id de oferta/hilo/duelo) y `payload` (precio, texto, días, activos).
- `expected` (estructura esperada de la oferta que se va a aceptar: importe, qué da, qué pide).
- `cash_commit` (importe que compromete), `limit_check` (el límite o tope contra el que se comparó).
- `expected_utility`, `urgency` (ej. duelo a punto de vencer), `reason` (texto para el log).
- `needs_accept_slot`, `needs_message_slot`, `needs_listing_slot`.

**`Decision`** (resultado del validator/risk)
- `proposal_id`, `verdict` (`approve | reject | defer`), `reasons[]`, `checked` (lista de comprobaciones y su resultado), `fresh_data` usado.

**`ActionResult`** (lo que devuelve la ejecución)
- `proposal_id`, `ok`, `error_code` (ej. `wait_for_tick`, `insufficient_cash`), `response`, `tick_sent`, `settles_tick` (normalmente `tick_sent + 1`).

Todas las estructuras son **inmutables y serializables** (JSON), para poder grabarlas, reproducirlas y compararlas en modo sombra.

---

## 4. Decisiones sobre solapamientos

### 4.1 Trading: una sola implementación

Lo bueno de cada pieza:

| Pieza | Qué aporta | Veredicto |
|---|---|---|
| `smart_agent.phase_market` | Rank por suma de rangos de ganancia y ROI (`rank_pick`); `value_of` con caché y tope de 12 llamadas | **KEEP** la idea del tope y de la caché; el ranking se sustituye por la utilidad del `OpportunityEngine` |
| `page_hunter` | `page_targets` (tope = valor + mitad del bonus repartido entre las que faltan), `buy_choice`, `sell_stock` solo de singles de sets que no construimos | **KEEP** `page_targets` y la regla de singles; el resto se absorbe |
| `agents/trader_agent` | Clasificación **estricta de formas** de oferta (`parse_side`/`classify`), tope por contraparte, `bid_plan`, trueque, presupuesto por archivo | **KEEP** como base de `trading/policy.py`: es la única con validación estructural completa y con tests |
| `agents/portfolio_agent` | Modelo de valor con bonus de página por copia marginal (`marginal_value`, `copy_loss`, `Valuer`, `sell_floor`, `buy_ceiling`) | **KEEP** como base de `intel/values.py`: es el modelo más completo; sustituye a las tres fórmulas actuales |

**Diseño final:** `trading/` = `policy.py` (de `trader_agent`) + `pages.py` (de `page_hunter`), usando `intel/values.py` (de `portfolio_agent`). `phase_market` y `page_hunter` se retiran cuando el nuevo trading pase la sombra (ver plan). Hasta entonces **solo uno de los tres puede estar lanzado a la vez** (hoy, `smart_agent` + `page_hunter` conviven; `trader_agent` no debe lanzarse a la vez que ellos).

Advertencia sobre lo nuevo: `trader_agent` y `portfolio_agent` no se han ejecutado nunca contra el servidor real, y sus formas de oferta (`types` frente a `cards`) están inferidas del simulador. Antes de que sean la implementación única hay que validarlos con **payloads reales** (fixtures).

### 4.2 Broker: `broker.py` frente a `smart_broker.py`

- **Se conserva `broker.py`** (`Track`, `classify`, `decide`, `session_left`, `bench_plan`).
- `smart_broker.py` pasa a archivo. Su "edad" cuenta llamadas y no ticks, y no tiene noción de fin de sesión.
- `public_plan` se copia desde `starter_broker.py` a `broker/policy.py` para dejar de depender de un archivo original del organizador.
- Decisión de espera: se deja `BROKER_WAIT` como flag. Evidencia disponible (selftest propio 0,871 frente a 0,902, y el baseline de `sim`) sugiere **desactivar la espera** hasta tener sesiones reales. Esto es una hipótesis de simulaciones inventadas, no un hecho.

### 4.3 `agent.py` frente a `smart_agent.py`

- `agent.py` aporta lo que `smart_agent` perdió: **comprar cartas sueltas a Abuela** y **venderle duplicados** (comunes y poco comunes), con topics `{"buy": {"card": id}}` y `{"sell": {"assets": [...]}}`.
- Se extrae esa **idea** (qué comprar y qué vender a Abuela) a `dealers/agent.py`, y se retira `agent.py`. Su bucle bloqueante (`haggle`) no se conserva: es lo que impide coordinar con el resto.

### 4.4 Dashboards

- Se conserva **`dashboard/`** (el único con caché, historial y estado de procesos).
- `dashboard.py` (rich, terminal), `web_dashboard.py` y `index.html` de la raíz son anteriores y se archivan.
- Cambio de diseño: el dashboard leerá `snapshot.json` (escrito por el orchestrator) en vez de llamar a la API. Así no gasta cuota de 5 req/s, y muestra lo que el orchestrator **cree** (pendientes, decisiones rechazadas).
- `ps` no funciona en Windows: la detección de procesos pasa a leer los lock files de `jsonio` (un archivo por proceso con PID y hora).

---

## 5. Qué se conserva (decisión por función)

Leyenda: **KEEP** (se traslada tal cual con sus tests) · **KEEP BUT FIX** · **REWRITE** · **DROP**.

### `smart_agent.py`

| Pieza | Decisión | Por qué |
|---|---|---|
| `next_offer` | **KEEP** (a `dealers/policy.py`) | Pura, probada con `play()` en el selftest. Su regla de aceptar a ≤ última oferta + 4 % viene de datos reales (hilos donde aceptó a 1 P) |
| `next_offer_sell` | **KEEP** | Espejo correcto; el selftest comprueba que nunca baja del suelo |
| `infer` | **KEEP BUT FIX** | La lógica de signos para ventas es correcta, pero `MIN_SAMPLES=5` con ≤14 hilos rara vez produce nada. Hay que hacerla configurable y mostrar `n` |
| `rounds_of`, `read_thread` | **KEEP BUT FIX** | `read_thread` asume `o["give"|"want"]["cash"]`; debe pasar por `parser`, tolerando formas distintas |
| `tone` | **KEEP BUT FIX** | Mezcla números y palabras. Dejar solo la parte numérica como señal; las palabras, solo para el log |
| `pack_private_value`, `pack_cap` | **KEEP BUT FIX** | La esperanza por ranura es correcta. Hay que revisar el criterio "no comprar si `cap` < 20": evita pérdidas pero deja el ladder a cero |
| `chato_candidate` | **KEEP** (a `dealers/policy.py`) | Selección clara por (valor perdido, precio de lista) |
| Memoria de dealer (`observed` frente a `inferred`, con `n`) | **KEEP** la idea | Es el mejor patrón del proyecto. Se mueve a `intel/memory.py` y se elimina lo que se guarda sin usarse |
| `chat_history`, `historial_abuela.txt` | **DROP** (como memoria) | Solo escritura; pasa al log JSONL |
| `phase_abuela`/`phase_chato` (el HTTP) | **REWRITE** | Mezcla lectura, decisión, memoria y ejecución. Se reparte entre `agent.py`, `orchestrator` y `memory` |
| `phase_market`, `market_opportunities`, `listing_plan`, `rank_pick` | **REWRITE** (en `trading/`) | Sustituidas por la implementación única de trading |
| Plantillas de texto (`haggle_text`, `PHRASES`, `chato_text`) | **KEEP** (a `dealers/texts.py`) | Funcionan; Abuela "likes kindness" |

### `smart_duels.py`

| Pieza | Decisión | Por qué |
|---|---|---|
| `plan_price` | **KEEP** | Curva Boulware con ajuste por rival; nunca retrocede ni cruza el límite |
| `should_accept` | **KEEP** | Compara con la oferta siguiente y con el valor de esperar. Es la parte más delicada: se congela con tests de caracterización antes de moverla |
| `utility`, `reservation`, `side_of` | **KEEP** | Triviales y correctas; `DAYS_SIGN` pasa a config |
| `rival_stats` (lógica de concesión del rival) | **KEEP BUT FIX** | Solo mira los últimos 4 movimientos; añadir perfil entre duelos (`intel/opponents.py`) |
| `plan_days` | **KEEP BUT FIX** | Nunca se ha ejercitado con datos reales. Hay que sondear el signo en el primer duelo de días en lugar de suponerlo |
| `time_left`, `parse_offer`, `duel_id` | **KEEP BUT FIX** | Pasan al `parser` |
| `record_finished` | **REWRITE** | Bug de clave `None` |
| `act` | **REWRITE** | Mezcla memoria, decisión y ejecución. La decisión queda en `duels/policy.py` |
| Selftest con `/tmp` | **KEEP BUT FIX** | Ruta que falla en Windows |

### `page_hunter.py`

| Pieza | Decisión | Por qué |
|---|---|---|
| `page_targets` | **KEEP** (a `trading/pages.py`) | Tope = valor + mitad del bonus repartido; es la única pieza que trata el bonus de página como objetivo |
| `buy_choice` | **KEEP BUT FIX** | Debe usar el valor autoritativo y el estado de cash compartido |
| `sell_stock`, `sell_bid_choice` | **KEEP BUT FIX** | La regla "solo singles de sets que no construimos" es útil; debe convivir con la venta de duplicados de la política única |
| `bid_plan` | **KEEP BUT FIX** | No reserva efectivo; sin compromiso pendiente puede dejar bids que no se pueden pagar |
| `step`/`main` | **DROP** | Se sustituye por `trading/agent.py` |

### `broker.py`

| Pieza | Decisión | Por qué |
|---|---|---|
| `Track`, `classify`, `decide`, `session_left`, `bench_plan` | **KEEP** | Lógica útil y comprobada con el selftest; `decide` solo espera si ambos lados se relajan |
| `WAIT_ENABLED` | **KEEP BUT FIX** | Valor por defecto y decisión según datos reales |
| `schedule_ticks` | **KEEP BUT FIX** | Evitar `_call` privado y tomar el evento de bench correcto |
| Escritura de `broker_memory.json` | **KEEP BUT FIX** | Pasar a `jsonio` atómico |
| `bench_plan_naive`, `_simulate` | **KEEP** como test | Se mueven a `tests/` |
| `public_plan` (importado de `starter_broker`) | **KEEP** (copiado a `broker/policy.py`) | Para no depender del archivo del organizador |

### `agents/portfolio_agent.py` y `agents/trader_agent.py`

| Pieza | Decisión | Por qué |
|---|---|---|
| Modelo de valor (`marginal_value`, `copy_loss`, `bonus_credit`, `page_state`, `Valuer`) | **KEEP** (a `intel/values.py`) | Más completo que las tres fórmulas actuales |
| `sell_floor`, `buy_ceiling`, `rank_targets`, `sellable_duplicates` | **KEEP** | Funciones puras con selftests |
| `parse_side`, `classify`, `evaluate`, `pick` | **KEEP** | Validación estricta de forma de oferta |
| `listing_plan`, `bid_plan`, `read_budget` | **KEEP BUT FIX** | `read_budget` pasa a `risk.py` (presupuesto único) |
| `step`/`run_forever`, `open_packs` en el tick | **REWRITE** | La apertura de packs pasa a una acción de mantenimiento del orchestrator; evita que dos procesos la hagan a la vez |
| Formas de oferta supuestas | **KEEP BUT FIX** | Validar contra payloads reales antes de confiar |

---

## 6. Orchestrator futuro

**Un coordinador por team key.** Hoy hay N bucles `while True` que comparten la clave y se despiertan a la vez.

Ciclo de un tick:

```
1. Tick empieza        clock.refresh() → ¿tick nuevo? ¿pausado? → TickBudget (límites del tick)
2. Reconciliar         Reconciler compara PendingLedger con me()/offers/threads del tick anterior:
                         liquidada / fallida / expirada → actualiza state y memory
3. Refresco mínimo     StateManager pide solo lo necesario: me, my_offers, my_threads(open), duels,
                         y tableros de venue si hay trading activo. Una petición por fuente y tick.
4. Observar            cada agente activo recibe State (solo lectura) y la memoria (solo lectura)
5. Proponer            cada agente devuelve ActionProposal[] (nunca ejecuta)
6. Puntuar             OpportunityEngine ordena por utilidad esperada y urgencia
7. Seleccionar         RiskManager aplica TickBudget y presupuesto de efectivo:
                         1 accept por tick, 1 mensaje por hilo, N listados, efectivo libre, reserva de venue,
                         tope por contraparte, un solo agente "dueño" por hilo/duelo
8. Validar             FinalValidator revalida cada acción elegida con datos frescos
                         (re-lee la oferta a aceptar y compara su estructura con `expected`; límite; cash)
9. Ejecutar            api_client envía; cada ActionResult se anota en PendingLedger
10. Pendiente          las acciones aceptadas quedan "pending" con `settles_tick`
11. Persistir          memory (hechos), snapshot.json, métricas
12. wait_tick          dormir hasta el siguiente tick y volver al paso 1
```

Detalles de diseño:

- **Prioridad de aceptación.** Como solo hay un accept por tick, el orchestrator **compite** entre dominios con un solo criterio (utilidad esperada y urgencia, p. ej. un `final` de dealer o un duelo que vence). Hoy la competencia se resuelve por carrera entre procesos.
- **Idempotencia.** `ActionProposal.id` es determinista. Si el mismo id ya está `sent` o `pending`, el orchestrator no lo reenvía, ni siquiera tras un fallo de red (los POST no se repiten a ciegas, como hace el SDK).
- **Efectivo.** `cash_free = cash − compromisos pendientes − reserva de venue`. Todos los agentes ven el mismo valor, que resuelve el estado de caja no compartido.
- **Errores de tick.** `wait_for_tick` o 429 → la acción se difiere un tick, sin reintento inmediato (no hay spam).
- **Pausa y kill switch.** Archivo `PAUSE` o flag `USE_ORCHESTRATOR=off` → el orchestrator no ejecuta nada pero sigue observando.
- **Degradación.** Si una fuente falla (p. ej. `board`), se omite ese agente en el tick y se registra; no se actúa con datos viejos (`fetched_at` supera un umbral).

Qué sigue siendo proceso separado:

| Proceso | Separado | Razón |
|---|---|---|
| **Team orchestrator** | Sí (uno) | Es el dueño de la `team key`: único que ejecuta acciones del equipo |
| **Broker** (`BROKER_KEY`) | **Sí** | Usa otra clave y por tanto otro límite de peticiones. Su cadencia es de 1 s, no por tick. Una caída del orchestrator no debe parar el Market Test (30 pts de puntuación) ni al revés. Comparte con el orchestrator solo `core/log`, `core/config`, `core/jsonio` y el validator de límites |
| **Dashboard** | Sí, **solo lectura** | Lee `snapshot.json`; no tiene clave de escritura ni importa el orchestrator |
| **Simulador / replay** | Sí | Herramientas de desarrollo |

Un punto a decidir (no es obvio): **apertura del venue propio** y gestión de su broker key. Es una acción irreversible que cuesta 270 P. Propongo que sea una acción manual (con confirmación) y no del orchestrator, y que `risk.py` solo mantenga la **reserva**.

---

## 7. Prioridades de seguridad (resumen; el detalle de fases está en el plan)

| P | Riesgo | Solución objetivo |
|---|---|---|
| **P0** | Doble accept entre procesos / acciones competidoras (`smart_agent`, `page_hunter`, `trader_agent`) | Hoy: puerta de accept por tick con archivo (`O_EXCL`), y no lanzar `trader_agent` a la vez. Objetivo: orchestrator único |
| **P0** | Aceptar `standing_offers[-1]` sin revalidar | `FinalValidator` con `expected` (importe, qué da, qué pide, ≤ cap). Hoy: comprobación mínima en `smart_agent` |
| **P0** | Instancias duplicadas | Lock de instancia única por script (`jsonio`) |
| **P0** | Escritura JSON no atómica (`broker.py`, `dashboard`) | `jsonio` atómico |
| **P0** | Datos dinámicos versionados (logs, memorias, history) | Sacarlos del control de versiones |
| **P1** | Límites hardcodeados | `clock.TickBudget` desde `clock["limits"]` (primero capturar la forma real) |
| **P1** | Estado de efectivo no compartido y settlements pendientes | `PendingLedger` + `cash_free` |
| **P1** | Dealers futuros no descubiertos | `dealers/discovery.py` |
| **P1** | `record_finished` con clave `None` | Corregir clave `duel` |
| **P1** | `DAYS_SIGN` sin verificar | Sonda en el primer duelo de días + flag |
| **P1** | Sin reserva para venue | Reserva en `risk.py` (270 P por defecto, configurable) |
| **P2** | Sesgos del broker (`lifetimes`, `schedule`), `chat_history`, `ps` en Windows, selección de copia al vender | Mejoras posteriores |

Nota sobre `DELETE_ONLY_AFTER_VERIFICATION`: nada se borra hasta que su sustituto haya pasado sombra con comportamiento equivalente y se haya revisado a mano.

---

## 8. Tabla de archivos: destino y acción

Acciones: `KEEP_AS_IS`, `KEEP_TEMPORARILY`, `EXTRACT`, `MERGE`, `REFACTOR`, `REPLACE_LATER`, `ARCHIVE_LATER`, `DELETE_ONLY_AFTER_VERIFICATION`.

| Archivo actual | Destino futuro | Acción |
|---|---|---|
| `bazaar_sdk.py` | se queda donde está; `bz/core/api_client.py` lo envuelve | **KEEP_AS_IS** |
| `smart_agent.py` | `bz/dealers/*` (política, texts, agent), `bz/intel/memory.py`, `bz/trading/*` (mercado) | **EXTRACT** progresivamente; el script sigue lanzable hasta el final |
| `smart_duels.py` | `bz/duels/{policy,buyer,seller,agent}.py` | **EXTRACT** (primero componente migrado) |
| `broker.py` | `bz/broker/{policy,track,runner}.py` | **EXTRACT** (proceso separado) |
| `page_hunter.py` | `bz/trading/pages.py` + `bz/trading/policy.py` | **MERGE** en el trading único |
| `agents/trader_agent.py` | `bz/trading/policy.py` y `agent.py` | **MERGE** (es la base) |
| `agents/portfolio_agent.py` | `bz/intel/values.py` | **MERGE** (es la base) |
| `agent.py` | idea de cartas con Abuela → `bz/dealers/agent.py` | **ARCHIVE_LATER** (tras extraer la idea) |
| `smart_broker.py` | `legacy/` | **ARCHIVE_LATER** |
| `starter_broker.py` | `public_plan` copiado a `bz/broker/policy.py` | **KEEP_TEMPORARILY** (el original del organizador, no se edita) |
| `starter_agent.py` | — | **KEEP_AS_IS** (referencia) |
| `dashboard/app.py`, `dashboard/index.html` | se quedan; pasan a leer `snapshot.json` | **REFACTOR** |
| `dashboard.py`, `web_dashboard.py`, `index.html` (raíz) | `legacy/` | **ARCHIVE_LATER** → **DELETE_ONLY_AFTER_VERIFICATION** |
| `sim/` | se queda; se adapta a `bz.*` | **KEEP_AS_IS** y luego **REFACTOR** |
| `memory.json` | `data/` (con formato V2 y lectura compatible) | **REFACTOR** (formato) |
| `duels_memory.json` | `data/` | **REFACTOR** (formato) |
| `dealers_intel.json` | `tests/fixtures/` | **KEEP_TEMPORARILY** (se usará como fixture) |
| `historial_abuela.txt` | log JSONL de decisiones | **ARCHIVE_LATER** |
| `*.log`, `dashboard/history.json` | fuera del control de versiones | **KEEP_AS_IS** en disco, **no versionar** |
| `DEALERS.md`, `PITCH.md`, `README.md`, `RULES.md` | se quedan | **KEEP_AS_IS** |
| `ARCHITECTURE_CURRENT.md` | `docs/` | **KEEP_AS_IS** |
| `.gitignore` | ampliar | **REFACTOR** |

---

## 9. Qué NO cubre este diseño (a propósito)

- No define una estrategia nueva: **la migración es de estructura y seguridad, no de comportamiento.** La mejora de estrategias (por ejemplo, el cap de packs, o el ladder) viene después y con medición.
- No resuelve si el nivel de "pausa tras retirarse de Abuela" es el correcto; hay que medirlo en sombra.
- No incluye un LLM. Todo es determinista; si se quisiera añadir, iría solo en la generación de texto, nunca en precios.
