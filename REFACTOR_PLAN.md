# REFACTOR_PLAN.md — plan de migración por fases (nada implementado)

Referencias: `ARCHITECTURE_CURRENT.md` (qué hay), `ARCHITECTURE_TARGET.md` (a dónde vamos), `REGRESSION_CHECKLIST.md` (qué proteger).

---

## 0. Restricción que manda sobre todo lo demás: el juego está en marcha

- Hoy es **sábado 3 de octubre de 2026**. El horario es sáb 09:00–23:00 y dom 09:00–15:00. Cada tick que el sistema actual funcione bien puntúa.
- Una refactorización completa (fases 0–12) no cabe con seguridad en lo que queda de juego. Por eso el plan tiene **dos líneas de corte**:

| Línea | Qué incluye | Cuándo tiene sentido |
|---|---|---|
| **Corte A: endurecer lo que corre** (fases 0–1) | Tag de línea base y hotfixes P0 pequeños sobre el código actual | **Ahora.** Es lo único que mejora la puntuación esperada sin riesgo estructural |
| **Corte B: base nueva sin tocar producción** (fases 2–6) | `bz/core` con modelos, reloj, estado, validator y orchestrator **que solo observan y registran** | Mientras el juego sigue con el sistema actual |
| **Corte C: migración real** (fases 7–12) | Mover dominios uno a uno, bajo sombra | Solo si hay margen; si no, **se hace después del juego**, para la entrega a jueces (el criterio "craft" pesa 40 %) |

- Regla de oro: **ningún cambio funcional se enciende en producción durante un tramo de juego sin haber pasado por sombra con datos reales** (fase 7 en adelante).
- Los hotfixes de la fase 1 sí tocan el código vivo: son pequeños (≤30 líneas), con test, uno por commit, y cada uno se puede revertir con `git revert`.

---

## 1. Convenciones válidas para todas las fases

- **Un cambio = un commit**, con el mensaje `refactor(fase-N): <qué>` o `fix(P0): <qué>`, y con `git pull` antes y `git push` después (acordado).
- **Rama.** El equipo trabaja sobre `main` con varios autores (`AP`, `Cursor Agent`, nosotros). Para evitar pisarnos: `bz/` y los documentos se tocan solo en ramas `refactor/*` y se fusionan por PR. Los hotfixes P0 van directos a `main`, porque corrigen producción.
- **Tag de rollback antes de cada fase:** `pre-fase-N`. Volver atrás = `git checkout pre-fase-N -- <archivos>` o `git revert`.
- **Ningún archivo legacy se mueve ni se borra** hasta la fase 12 y solo con la regla `DELETE_ONLY_AFTER_VERIFICATION` (sección 9).
- **Datos dinámicos** (`memory.json`, `duels_memory.json`, logs, `history.json`) se hacen copia en `backup/<fecha>/` antes de cualquier cambio de formato, y el formato nuevo siempre **lee** el viejo.
- **Cada fase termina con `python -m <selftest>` de todo lo que toca y la lista de `REGRESSION_CHECKLIST.md` correspondiente en verde.**

---

## 2. Fases

### FASE 0 — Línea base y red de seguridad
- **Objetivo:** poder volver al estado actual en segundos y saber qué comportamiento queremos preservar.
- **Archivos afectados:** ninguno de código. Crea tag `legacy-v1-baseline`, carpeta `bz/tests/fixtures/`, copias en `backup/`.
- **Pasos:**
  1. `git tag legacy-v1-baseline` en `main` y `git push --tags`.
  2. Capturar **payloads reales** de la API (`/api/clock`, `/api/me`, `/api/dealers`, `/api/levels`, `/api/schedule`, `/api/duels`, un hilo con Abuela y otro con Chato, un libro de broker con `bench_offers`) a `bz/tests/fixtures/`. Quitar claves y nombres de equipos rivales.
  3. Convertir `dealers_intel.json` y las 3 `raw_samples` de `duels_memory.json` en fixtures.
  4. Ejecutar y guardar la salida de los 4 selftests existentes como referencia (`smart_agent`, `broker`, `page_hunter` pasan hoy; `smart_duels` falla por `/tmp`).
- **No debe cambiar:** nada.
- **Tests que deben pasar:** los selftests tal cual están hoy.
- **Terminada cuando:** existe el tag, hay fixtures de al menos `clock`, `me`, `duels` y un libro de broker, y la captura de `clock` muestra **la forma real de `limits`** (que no conocemos).
- **Rollback:** borrar la carpeta de fixtures. El tag no se toca.

### FASE 1 — Hotfixes P0 sobre el código vivo
Cada punto es un commit independiente. Orden de más a menos daño por unidad de riesgo:

| # | Fix | Archivo | Cambio mínimo | Test |
|---|---|---|---|---|
| 1.1 | Sacar datos dinámicos del control de versiones | `.gitignore`, `git rm --cached` de `*.log`, `dashboard/history.json`, `historial_abuela.txt`, y decidir sobre `memory.json`/`duels_memory.json` (ver nota) | ninguno de código | `git status` limpio tras una ejecución |
| 1.2 | Lock de instancia única | `smart_agent.py`, `smart_duels.py`, `page_hunter.py`, `broker.py` (una función `acquire_lock(name)` de ~15 líneas, archivo con PID) | si el lock existe y el PID vive → salir con mensaje | lanzar dos veces: la segunda sale |
| 1.3 | Revalidar antes de aceptar a un dealer | `smart_agent.py` (`phase_abuela` y `phase_chato`) | antes de `b.accept`, comprobar que la oferta abierta tiene el importe decidido y ≤ cap (o ≥ suelo en venta); si no, no aceptar | test con hilo falso donde la oferta cambió |
| 1.4 | Puerta de accept entre procesos | `smart_agent.py`, `page_hunter.py` (y cualquier proceso futuro) | archivo `.accept_tick` con el tick: crear con `O_EXCL` antes de `b.accept`; si existe para este tick, no aceptar | test que simula dos procesos en el mismo tick |
| 1.5 | `record_finished` con clave `duel` | `smart_duels.py` | `d.get("duel", d.get("id"))` | test con payload real de duelo terminado |
| 1.6 | Escrituras atómicas | `broker.py`, `dashboard/app.py` | `tmp` + `os.replace` | test que interrumpe a mitad |
| 1.7 | Selftest de `smart_duels` en Windows | `smart_duels.py` | `tempfile` en vez de `/tmp` | `--selftest` pasa |
| 1.8 | Reserva para venue | `smart_agent.py`, `page_hunter.py` | `CASH_RESERVE` por defecto con un valor decidido por el equipo (270 P propone `agent.py`) | test de `pack_cap` y `buy_choice` con reserva |

- **No debe cambiar:** ninguna decisión de precio ni de aceptación en condiciones normales. Solo se bloquean casos que hoy son erróneos o dudosos.
- **Criterio de terminada:** los 8 commits hechos y la parte "P0" de `REGRESSION_CHECKLIST.md` en verde.
- **Rollback:** `git revert <commit>` de cada uno (son independientes).
- **Nota sobre 1.1:** `memory.json` y `duels_memory.json` son lo único que conserva el aprendizaje. Si se dejan de versionar hay que respaldarlos fuera de git, o el equipo pierde el historial si cambia de máquina. Decisión del equipo; mi propuesta: no versionarlos y copiarlos a `backup/` cada hora.
- **Nota sobre 1.8:** gastar menos puede reducir la ganancia a corto plazo. Es una decisión de estrategia: dejarla **configurable** y no cambiar el valor por defecto sin aprobación.

### FASE 2 — `bz/core` base: config, log, jsonio
- **Objetivo:** utilidades comunes sin lógica de negocio.
- **Archivos:** nuevos `bz/core/{config,log,jsonio}.py`. Los scripts legacy **no** se modifican.
- **No debe cambiar:** el comportamiento de ningún proceso actual.
- **Tests:** unitarios de `jsonio` (atómico, lock), `config` (precedencia archivo > env > defecto) y `log`.
- **Terminada cuando:** los tests pasan y ningún script legacy importa `bz` todavía.
- **Rollback:** borrar `bz/`.

### FASE 3 — Modelos y parser sobre datos reales
- **Objetivo:** que toda forma de la API pase por un único lugar.
- **Archivos:** nuevos `bz/core/{models,parser}.py` y tests con los fixtures de la fase 0.
- **No debe cambiar:** nada en producción.
- **Tests:** cada fixture real → modelo → comparación con lo que hoy extraen `read_thread`, `parse_offer`, `market_opportunities` (deben dar lo mismo). Casos con campos ausentes devuelven `None` y aviso, sin excepción.
- **Terminada cuando:** el parser reproduce las decisiones de entrada de las funciones legacy sobre todos los fixtures, **y se confirma la forma real de ofertas "quiero una carta"** (`types` frente a `cards`), que hoy está inferida.
- **Rollback:** borrar los dos archivos.

### FASE 4 — Reloj y límites dinámicos
- **Objetivo:** `TickBudget` a partir de `clock["limits"]` real.
- **Archivos:** `bz/core/clock.py`.
- **Bloqueo previo:** se necesita la forma real de `limits` (fase 0). Si no viene o difiere, el módulo usa los valores actuales (1 accept, 1 mensaje por hilo, 12 listados, 30 ofertas, 6 hilos) y avisa.
- **No debe cambiar:** nada en producción.
- **Tests:** con `limits` distintos (p. ej. 1 conversación a la vez) el presupuesto cambia; sin `limits`, valores por defecto.
- **Terminada cuando:** hay un test por cada límite de `RULES.md` y se ha registrado al menos un cambio real de límites en vivo (el feed los anuncia).
- **Rollback:** borrar el archivo.

### FASE 5 — Estado compartido, pendientes y snapshot (solo observación)
- **Objetivo:** un `StateManager` y un `PendingLedger` que **no actúan**: leen y registran.
- **Archivos:** `bz/core/{state,pending}.py`, `bz/observe/snapshot.py`.
- **Cómo se prueba en vivo sin riesgo:** un proceso nuevo `python -m bz.observe` que cada tick lee con la `team key` (una petición por fuente) y escribe `snapshot.json`. Gasta cuota (4–5 peticiones por tick), por lo que se activa solo con un flag y se vigila el límite de 5 req/s.
- **No debe cambiar:** nada de lo que hacen los procesos actuales.
- **Tests:** el `cash_free` calculado con un trato pendiente es menor que el `cash` real; un pendiente se liquida al tick siguiente.
- **Terminada cuando:** `snapshot.json` coincide con `b.me()` en 20 ticks seguidos y el dashboard puede leerlo.
- **Rollback:** detener el proceso y borrar el archivo.

### FASE 6 — Propuestas, validator, risk y orchestrator mínimo (sin ejecutar)
- **Objetivo:** el esqueleto del tick completo, **con ejecución desactivada** (`USE_ORCHESTRATOR=shadow`): corre los pasos 1–8 y registra qué habría hecho.
- **Archivos:** `bz/core/{actions,risk,validator,reconciler,orchestrator,recorder,shadow}.py`.
- **No debe cambiar:** los procesos legacy siguen siendo los únicos que ejecutan.
- **Tests:** el validator rechaza cada una de estas propuestas: precio sobre el tope, forma de oferta desconocida, importe distinto del esperado, efectivo insuficiente, segundo accept en el mismo tick, contraparte repetida, propuesta duplicada (mismo `id`). `RecordingClient` no hace red.
- **Terminada cuando:** el orchestrator corre 50 ticks en `sim` y 20 contra el servidor real en modo sombra sin excepciones y sin gastar más de 6 peticiones por tick.
- **Rollback:** `USE_ORCHESTRATOR=off` (o detener el proceso).

### FASE 7 — Migrar duelos (primer dominio)
- **Por qué primero:** es el dominio más aislado y puro. No mueve efectivo (la puntuación depende del trato, no del cash), tiene un único tipo de acción (`duel_say`/`duel_accept`) y ya hay 24 duelos reales guardados para comparar.
- **Pasos:**
  1. Extraer `plan_price`, `plan_days`, `should_accept`, `utility`, `reservation` a `bz/duels/policy.py` **sin cambios de comportamiento** (copia, no mover; el original sigue lanzable).
  2. Tests de caracterización: para cada paso de los 24 duelos guardados, la política nueva y la vieja dan la misma decisión.
  3. Modo `shadow`: la vieja ejecuta; la nueva se registra (sección 4).
  4. Tras ≥1 sesión de duelo en sombra con 0 diferencias no explicadas, pasar a `new` para **solo ese dominio**.
- **No debe cambiar:** precios, aceptaciones y momentos. La diferencia permitida es `0`; cualquier diferencia debe tener una explicación escrita.
- **Cambios aparte (después, no mezclados):** `DAYS_SIGN` como sonda, perfil de rival entre duelos.
- **Terminada cuando:** una sesión completa en `new` sin rechazos del validator no explicados y con puntuación igual o mejor que la sombra.
- **Rollback:** `USE_NEW_DUELS=old`, efecto en el siguiente tick.

### FASE 8 — Unificar el trading
- **Por qué antes que dealers:** hoy es donde hay carrera real entre procesos (`smart_agent` y `page_hunter`), así que reduce un riesgo P0 en cuanto el orchestrator ejecuta.
- **Pasos:**
  1. `bz/intel/values.py` desde `portfolio_agent`. Comparar con `marginal_value` de `smart_agent` y `value_of_card` de `page_hunter` sobre el inventario real: **listar y explicar cada diferencia** antes de seguir.
  2. `bz/trading/policy.py` (de `trader_agent`) y `pages.py` (de `page_hunter`).
  3. Sombra: `phase_market` y `page_hunter` ejecutan; la política nueva se registra.
  4. Pasar a `new` y **detener** `page_hunter` y la fase de mercado de `smart_agent` (flag `USE_NEW_TRADING`).
- **No debe cambiar:** nunca se acepta una operación de valor negativo a nuestros valores privados; se mantiene el tope de 1 accept por tick y el de listados.
- **Cambios de comportamiento aceptados (documentados):** una sola regla de ranking; tope por contraparte; formas de oferta estrictas.
- **Terminada cuando:** 2 horas de juego en `new` sin operaciones de ganancia negativa y con el valor de colección igual o mayor que el de referencia.
- **Rollback:** `USE_NEW_TRADING=old` y relanzar los dos procesos viejos.

### FASE 9 — Memoria V2
- **Objetivo:** un `MemoryStore` con una sola API y formato versionado (`schema_version`), con lectura compatible de los JSON actuales.
- **Archivos:** `bz/intel/memory.py`; migra `memory.json` y `duels_memory.json` a `data/` con copia previa.
- **No debe cambiar:** lo que los agentes leen hoy (`observed.negotiations`, `abuela_min_price`, `chato_*`, `state` de duelos).
- **Se eliminan solo escrituras sin lectura** (`chat_history`, `inferred` duplicado), tras comprobar con `grep` y con la sombra que nadie las usa.
- **Terminada cuando:** arrancar con los JSON actuales produce la misma memoria interna y los tests de los 2 formatos pasan.
- **Rollback:** restaurar los archivos desde `backup/` y poner `USE_NEW_MEMORY=off`.

### FASE 10 — Dealers (el dominio más delicado)
- **Por qué casi al final:** mueve efectivo, gasta cuotas horarias, y los errores se pagan con `cooloff` o con la memoria del dealer (Chato "recuerda"). Es difícil de revertir en sentido práctico (una conversación quemada no se recupera).
- **Pasos:**
  1. `dealers/policy.py` con `next_offer`, `next_offer_sell`, `pack_cap`, `chato_candidate` **sin cambios**.
  2. `dealers/discovery.py`: solo registra los dealers nuevos en el log. **No negocia** con dealers que no conoce hasta que haya una decisión manual.
  3. Sombra larga (≥ 3 ciclos de hilo completos con Abuela y ≥1 con Chato).
  4. `new` solo para Abuela primero; Chato después.
- **No debe cambiar:** nunca aceptar sobre el tope; nunca repetir un precio; pausa tras retirarse; un hilo por dealer.
- **Terminada cuando:** hilos completos en `new` con las mismas aceptaciones que la sombra y sin `cooloff`.
- **Rollback:** `USE_NEW_DEALERS=old`; si había un hilo abierto, el proceso viejo lo "adopta" (`smart_agent` ya hace esto con `open_threads`).

### FASE 11 — Opportunity engine
- **Objetivo:** que una sola moneda (utilidad esperada) ordene las propuestas de dealers, trading y duelos para el único accept del tick.
- **No debe cambiar:** nunca se rebaja la urgencia de un duelo que vence ni de un `final` de dealer.
- **Terminada cuando:** en `sim` y en sombra, el orden elegido es explicable en cada tick y no pierde ninguna acción con plazo.
- **Rollback:** orden fijo por dominio (duelo > dealer > trading) mediante flag.

### FASE 12 — Limpieza final
- **Objetivo:** archivar lo sustituido (`agent.py`, `smart_broker.py`, `dashboard.py`, `web_dashboard.py`, `index.html` de la raíz, y los scripts migrados).
- **Regla:** `ARCHIVE_LATER` → mover a `legacy/` (no borrar). `DELETE_ONLY_AFTER_VERIFICATION` solo cuando: (a) el sustituto está en `new` ≥ 1 jornada completa, (b) `grep` demuestra que nadie lo importa ni lo lanza, (c) el tag `legacy-v1-baseline` sigue existiendo.
- **Terminada cuando:** el árbol raíz solo tiene `bz/`, `dashboard/`, `sim/`, `legacy/`, documentación y los archivos del organizador.
- **Rollback:** `git revert` del commit de movimiento, o `git checkout legacy-v1-baseline -- <archivo>`.

### El broker (hilo aparte)
- Se migra a `bz/broker/` **después de la fase 7** y **solo** si se decide cambiar su lógica; mientras tanto se deja tal cual (solo recibe los fixes 1.2 y 1.6).
- Flag propio `USE_NEW_BROKER`, y sombra con el libro real de la sesión del Market Test (cada 2 h).
- Es el único componente cuyo error de hoy cuesta una sesión entera de 2 horas sin posibilidad de repetirla.

---

## 3. Orden resumido y por qué

| Orden | Fase | Razón del puesto |
|---|---|---|
| 1 | 0 línea base | Sin ella no hay rollback ni referencia |
| 2 | 1 hotfixes P0 | Reduce riesgo real sin cambiar estructura |
| 3 | 2–6 base nueva en modo observación | Todo lo nuevo se prueba sin ejecutar |
| 4 | 7 duelos | Aislado, puro, sin efectivo, con datos reales |
| 5 | 8 trading | Aquí está la carrera entre procesos |
| 6 | 9 memoria | Antes de dealers, porque dealers depende de la memoria |
| 7 | 10 dealers | El dominio con más riesgo |
| 8 | 11 opportunity engine | Solo tiene sentido con ≥2 dominios ya migrados |
| 9 | 12 limpieza | Última, y sin borrar nada de golpe |

---

## 4. Modo sombra (shadow mode)

**Objetivo:** ejecutar la decisión vieja (A) y calcular la nueva (B) con los mismos datos, y comparar sin ejecutar B.

Cómo se obtiene la "decisión" de código legacy sin modificarlo:

1. **`RecordingClient`** (`bz/core/recorder.py`): tiene la misma superficie que `bazaar_sdk.Bazaar`. Las **lecturas** devuelven los datos reales del tick (ya capturados por `StateManager`, para no gastar peticiones), y las **escrituras** (`say`, `accept`, `duel_say`, `list_offer`, …) no hacen red: se anotan y devuelven una respuesta enlatada.
2. Las funciones legacy (`act`, `phase_abuela`, `phase_market`) se invocan con `RecordingClient` y **una copia profunda de su memoria**, así no la contaminan.
3. Lo que el cliente grabó es la **acción A** (lista de escrituras con sus argumentos).
4. La política nueva produce `ActionProposal[]` = **acción B**.
5. Un `ActionDiff` los compara (tipo, objetivo, precio, días) y registra.

Registro (JSONL, `logs/shadow/<dominio>.jsonl`), una línea por tick y dominio:

```
tick, domain, state_hash,
old_action, new_action, same (bool),
diff_fields[], old_reason, new_reason,
old_expected_utility, new_expected_utility,
validator_verdict_new (aprobaría o no, y por qué),
executed: "old"
```

Dos modos:
- **`shadow` (A ejecuta, B se registra):** `USE_NEW_X=shadow`.
- **`inverse` (B ejecuta, A queda como referencia):** `USE_NEW_X=new`, con el mismo registro pero `executed: "new"` y la vieja grabando en sombra.

Reglas:
- Si `old` ≠ `new`, se anota la diferencia, pero **nunca** se ejecuta la nueva en modo `shadow`.
- Una diferencia solo se acepta como "esperada" si hay un motivo escrito (p. ej. "el validator rechaza porque la forma de oferta es desconocida").
- Limitación honesta: la comparación solo vale si el estado de entrada es el mismo. Por eso `StateManager` pasa **la misma foto** a ambas, y el hash de estado queda en el registro. Las decisiones que dependen del tiempo (lo que el rival responderá) no se pueden comparar más allá del primer paso.
- La memoria que el legacy escribe durante la sombra es la verdadera (el proceso viejo sigue siendo el que ejecuta); la nueva escribe en un archivo aparte.

---

## 5. Feature flags

Archivo `flags.json` en la raíz (no versionado, con un `flags.example.json` versionado), releído **cada tick**. Las variables de entorno con el mismo nombre lo sobrescriben solo en el arranque.

| Flag | Valores | Qué controla | Por defecto |
|---|---|---|---|
| `USE_ORCHESTRATOR` | `off` / `shadow` / `on` | Si el orchestrator ejecuta, solo registra, o no corre | `off` |
| `USE_NEW_DUELS` | `old` / `shadow` / `new` | Quién decide en duelos | `old` |
| `USE_NEW_DEALERS` | `old` / `shadow` / `new` | Quién negocia con dealers (puede ir por dealer: `{"abuela": "shadow", "chato": "old"}`) | `old` |
| `USE_NEW_TRADING` | `old` / `shadow` / `new` | Trading entre equipos | `old` |
| `USE_NEW_MEMORY` | `off` / `read` / `on` | Si se lee/escribe el formato V2 (`read` = lee V2, sigue escribiendo V1) | `off` |
| `USE_NEW_BROKER` | `old` / `shadow` / `new` | Broker | `old` |
| `BROKER_WAIT` | `0` / `1` | Espera en el Market Test | actual (`1`) hasta decidir con datos |
| `DAYS_SIGN` | `+1` / `-1` / `probe` | Signo de la utilidad por días | `+1` (actual) |
| `CASH_RESERVE` | entero | Primas reservadas (venue) | `0` (actual) hasta aprobación |
| `KILL` | `true` | Detiene toda acción de escritura del orchestrator | `false` |

Cómo volver al sistema anterior en segundos:
1. Editar `flags.json` (cualquier `USE_NEW_* → "old"`), efecto **en el siguiente tick**, sin reiniciar.
2. Si se pierde el control: crear un archivo `STOP` (el orchestrator no ejecuta nada) o poner `KILL=true`.
3. Los procesos legacy **no se detienen** al introducir flags: mientras el flag diga `old`, el legacy sigue siendo quien actúa. Al pasar un dominio a `new`, se detiene su proceso viejo (o su fase) a mano, en el mismo cambio de flag.
4. Un solo dueño por dominio: si el orchestrator detecta que el proceso legacy sigue activo (lock de instancia) y el flag dice `new`, **se niega a ejecutar** ese dominio y lo registra. Así no puede haber dos actores a la vez.

---

## 6. Qué queda como proceso separado (decisiones)

- **Team orchestrator** (uno por team key).
- **Broker** (`BROKER_KEY`): separado. Motivos: otra clave y otro límite de peticiones, cadencia de 1 s, y aislamiento de fallos (30 pts del Market Test).
- **Dashboard**: separado, solo lectura, lee `snapshot.json`.
- **Sim / replay**: herramientas aparte.

---

## 7. Qué NO se toca todavía

- `bazaar_sdk.py` (original del organizador; solo se envuelve).
- Las reglas de estrategia (anclas, `BETA`, `PACK_EDGE`, `MIN_SAMPLES`): se migran **iguales**. Calibrarlas es otro trabajo, con datos.
- El cap de packs de `smart_agent` (que casi nunca abre hilo). Es una cuestión de estrategia, no de estructura.
- El broker, salvo los fixes 1.2 y 1.6.
- `starter_agent.py`, `starter_broker.py`, `RULES.md`, `README.md`.
- Apertura del venue (cuesta 270 P y es irreversible): sigue siendo manual.

---

## 8. Riesgos de la propia migración

| Riesgo | Mitigación |
|---|---|
| Hay que tocar producción durante el juego | La fase 1 son cambios mínimos, uno por commit; el resto no toca producción hasta la fase 7 |
| Cuota de 5 req/s | El orchestrator hace **una** lectura por fuente y tick; el modo observación se activa por flag y se mide |
| Payloads reales distintos de lo supuesto | Fase 0 (fixtures) antes de escribir el parser; **no** se confía en el simulador para formas |
| Varios autores sobre los mismos archivos | `bz/` solo en ramas `refactor/*`; hotfixes pequeños en `main` con `pull --rebase` |
| Dos actores sobre el mismo dominio | Regla "un solo dueño por dominio" con lock (sección 5) |
| Pérdida de memoria acumulada | `backup/` antes de cambiar formato, y lectura compatible |
| Falta de tiempo | Línea de corte A/B/C: lo más valioso (fase 1) se hace primero |

---

## 9. Condiciones para borrar (`DELETE_ONLY_AFTER_VERIFICATION`)

Un archivo solo se borra si **todas** se cumplen:
1. Su sustituto lleva al menos una jornada completa en `new`.
2. `grep -rn` no encuentra ninguna importación ni lanzamiento (incluido `SCRIPTS` del dashboard).
3. Está en `legacy/` al menos 24 h sin que nadie lo reclame.
4. El tag `legacy-v1-baseline` existe y contiene el archivo.
5. Un segundo miembro del equipo lo ha revisado.
