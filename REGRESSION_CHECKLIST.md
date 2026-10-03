# REGRESSION_CHECKLIST.md — comportamiento actual que debemos proteger

Cómo usar este documento:
- Cada fila es un test futuro. **Origen** dice de dónde sale la expectativa: `selftest` (ya existe una aserción equivalente en el código), `código` (se deduce leyendo la función, hay que escribir el test), o `dato real` (se comprueba contra un fixture).
- Hay dos tipos de test:
  - **Invariante** (⚖): debe ser cierto siempre, sea cual sea la estrategia. No se discute.
  - **Caracterización** (📸): fija lo que el código hace hoy para detectar cambios no queridos. Si una fase cambia a propósito un 📸, se actualiza el test **en el mismo commit y con la razón escrita**.
- Columna Fase: primera fase en la que debe estar en verde. Los marcados `1` protegen los hotfixes P0.
- Referencia a fixtures: ver `REFACTOR_PLAN.md`, fase 0.

---

## 1. API y reloj

| ID | Tipo | Escenario | Esperado | Origen | Fase |
|---|---|---|---|---|---|
| API-01 | ⚖ | Respuesta `429` o `wait_for_tick` | No hay reintento inmediato en bucle: la acción se difiere al tick siguiente; el número de peticiones por tick no crece | código (`bazaar_sdk`, `wait_on_tick=False`) | 6 |
| API-02 | ⚖ | Fallo de red en un `POST` | No se repite a ciegas (el SDK lanza `network`); el orchestrator no reenvía el mismo `ActionProposal.id` | código (`bazaar_sdk._call`) | 6 |
| API-03 | ⚖ | Una acción inválida (rechazada por la API) | El estado local, la memoria y el `PendingLedger` no cambian | código | 6 |
| API-04 | ⚖ | Servidor devuelve `BazaarError` en una lectura | El agente de ese dominio se omite en ese tick; no se actúa con datos viejos | código | 6 |
| API-05 | ⚖ | Más de 6 peticiones por tick desde el orchestrator | Test falla (presupuesto de peticiones) | nuevo | 6 |
| API-06 | ⚖ | Juego en pausa (`paused`) | Ningún agente envía acciones | selftest implícito (`smart_agent` duerme 10 s) | 1 |
| CLK-01 | 📸 | `clock["limits"]` ausente | Se usan los valores actuales (1 accept, 1 mensaje/hilo, 12 listados, 30 ofertas, 6 hilos) y se avisa | nuevo | 4 |
| CLK-02 | ⚖ | `limits` cambia (p. ej. 1 conversación a la vez) | El presupuesto del tick lo refleja sin reiniciar | nuevo | 4 |
| CLK-03 | ⚖ | Dos acciones que necesitan el accept en el mismo tick | Solo una se ejecuta | código (`RULES.md`) | 1 (puerta entre procesos), 6 |

## 2. Dealers

| ID | Tipo | Escenario | Esperado | Origen | Fase |
|---|---|---|---|---|---|
| DLR-01 | ⚖ | Compra: oferta del dealer por encima del tope (`ask > cap`) y no final | Nunca se acepta | selftest (`play`: `"must not buy above the cap"`) | 1 |
| DLR-02 | ⚖ | Compra: `final` por encima del tope | Acción `walk` (cerrar hilo), no aceptar | código (`next_offer`) | 1 |
| DLR-03 | ⚖ | Compra: `final` ≤ tope | Aceptar | código (`next_offer`) | 1 |
| DLR-04 | ⚖ | Ninguna oferta enviada supera el tope | `price <= cap` en todas las rondas | selftest (`assert price <= cap`) | 1 |
| DLR-05 | 📸 | Compra: el dealer concede la mitad por cada paso | Se llega a un trato ≤ tope y los pasos varían con el hueco | selftest (`r1`) | 7 |
| DLR-06 | 📸 | Compra: el dealer no concede nunca | La salida es `walk` o `stuck`, nunca aceptar por encima del tope | selftest (`r2`) | 7 |
| DLR-07 | 📸 | Compra: su `ask` está a ≤ 4 % de lista de nuestra última oferta | Se acepta su `ask` si ≤ tope | código (`tol = max(1, int(0.04 × lista))`); datos de `DEALERS.md` (4 hilos aceptados a 1 P) | 7 |
| DLR-08 | ⚖ | Nuestra oferta vigente ya supera el tope (heredada) | `walk`: se empieza de cero | código (`last > cap`) | 1 |
| DLR-09 | ⚖ | No se envía el mismo precio dos veces seguidas | Cada oferta nueva es estrictamente mayor que la anterior (compra) o menor (venta) | código (`new <= last → walk`) y `PITCH.md` (cooloff por repetir) | 1 |
| DLR-10 | ⚖ | Aún sin respuesta a nuestra última oferta | `wait`; no se envía otro mensaje | código (`len(ours) > len(hers)`) | 1 |
| DLR-11 | ⚖ | Venta a Chato: oferta por debajo del suelo | Nunca se envía ni se acepta por debajo del suelo | selftest (`play_sell`: `"ask below floor"`) | 1 |
| DLR-12 | 📸 | Venta: su `final` ≥ suelo | Aceptar; si es menor, `walk` | selftest (`s3`, `s2`) | 7 |
| DLR-13 | ⚖ | Aceptar una oferta del dealer | Solo si la oferta abierta tiene el importe evaluado y cumple el tope (P0, revalidación) | **nuevo** (hotfix 1.3) | 1 |
| DLR-14 | 📸 | `infer` con 1 muestra | Sin conclusiones (`paid_median` y `response_ratio` vacíos) | selftest | 9 |
| DLR-15 | 📸 | `infer` con 3 tratos (21, 24, 17) | `paid_median = 21` y apertura = `round(0.8 × 21) = 17` | selftest | 9 |
| DLR-16 | 📸 | `infer` en ventas (`received`, movimientos negativos) | `response_ratio["mid"] = {mean 0.5, n 6}`, `paid_median = 20`, `paid_n = 3` | selftest (`learned`) | 9 |
| DLR-17 | ⚖ | `infer` de una compra con movimientos "al revés" | No cuenta como concesión (`response_ratio` vacío) | selftest (`buy_down`) | 9 |
| DLR-18 | 📸 | Tope de pack: pack de cartas que ya tenemos | `pack_private_value` baja (5,0 frente a 20,0 en el ejemplo) | selftest | 7 |
| DLR-19 | ⚖ | Hilo de Chato termina sin trato (`no_progress`) | Se registra, se bloquea Chato 30 ticks y esa carta 90 | selftest (`_EndedChato`: `chato_block_until == 70`) | 1 |
| DLR-20 | 📸 | Candidato a Chato | Solo la carta barata para nosotros y realista para él | selftest (`chato_candidate` → `MAL-07`) | 10 |
| DLR-21 | ⚖ | Nunca se llama a un dealer `locked` | `chato` solo si está en `unlocked` | código | 10 |
| DLR-22 | ⚖ | Dealer nuevo detectado | Se registra; **no se negocia** hasta decisión manual | nuevo (fase 10) | 10 |
| DLR-23 | ⚖ | Cuota/`cooloff` (`persona_quota`, `cooloff`) | No se abre otro hilo hasta pasado `until_tick` o la hora de juego | `DEALERS.md`, `agent.py` (espera) | 10 |
| DLR-24 | ⚖ | El texto del dealer dice "final" pero la estructura no lleva `final` | No cambia el precio ni la decisión de aceptar | código (`tone` solo afecta a `k`) | 7 |

## 3. Duelos

| ID | Tipo | Escenario | Esperado | Origen | Fase |
|---|---|---|---|---|---|
| DUE-01 | ⚖ | Vendedor | Ninguna oferta enviada < `limit + MIN_MARGIN` | código (`reservation`, `assert` en `act`) | 1 |
| DUE-02 | ⚖ | Comprador | Ninguna oferta enviada > `limit − MIN_MARGIN` | ídem | 1 |
| DUE-03 | ⚖ | Nunca se retrocede | Vendedor: `price ≤ our_last`; comprador: `price ≥ our_last` | código (`plan_price`) | 7 |
| DUE-04 | ⚖ | `should_accept`: la oferta del rival da utilidad `< MIN_MARGIN` | No se acepta | código | 7 |
| DUE-05 | 📸 | `should_accept`: la oferta del rival ≥ nuestra próxima oferta | Se acepta | código | 7 |
| DUE-06 | 📸 | `should_accept`: queda ≤ 1 ronda y la utilidad es positiva | Se acepta | código | 7 |
| DUE-07 | 📸 | `should_accept` con estadísticas: `u_now ≥ valor de esperar` | Se acepta; si no, se mantiene | código | 7 |
| DUE-08 | 📸 | Última ronda con oferta del rival | Se envía el precio de reserva (`reservation`), dentro del límite | código (`remaining <= 1`) | 7 |
| DUE-09 | 📸 | Rival firme (últimos 2 movimientos ≤ ε) | `BETA × 0.6` (concede antes); si cede, `BETA × 1.3` | código (`plan_price`) | 7 |
| DUE-10 | 📸 | **Los 24 duelos guardados** (`duels_memory.json`) | Para cada paso, la política nueva da la misma acción, precio y días que la vieja | dato real | 7 |
| DUE-11 | ⚖ | Un mensaje por duelo y tick | `st["last_tick"] == tick` impide repetir | código (`act`) | 7 |
| DUE-12 | ⚖ | `wait_for_tick` al enviar | Se reintenta al tick siguiente (`last_tick = None`) | código | 7 |
| DUE-13 | ⚖ | Un duelo con error no detiene a los demás | `step` captura por duelo | código | 7 |
| DUE-14 | ⚖ | Duelo de solo precio | `days` no se envía (`plan_days` → `None`) | código | 7 |
| DUE-15 | 📸 | Duelo con días: el mensaje con precio incluye `days` | Si no, la API lo rechaza con `missing_days` | `RULES.md`, código | 7 |
| DUE-16 | ⚖ | `DAYS_SIGN` | Está en config; con `probe` el primer duelo de días registra la evidencia | **nuevo** | 7 |
| DUE-17 | ⚖ | `record_finished` | Los duelos terminados se guardan con su clave `duel` (no `"None"`), sin pisarse | **nuevo** (hotfix 1.5) | 1 |
| DUE-18 | ⚖ | `--selftest` de duelos | Pasa en Windows (sin `/tmp`) | **nuevo** (hotfix 1.7) | 1 |
| DUE-19 | ⚖ | Memoria de duelos | Guardada atómicamente: un corte no la corrompe | código (`os.replace`) | 9 |

## 4. Trading entre equipos

| ID | Tipo | Escenario | Esperado | Origen | Fase |
|---|---|---|---|---|---|
| TRD-01 | ⚖ | Operación con ganancia negativa a nuestros valores privados | Nunca se acepta | selftest (`trader_agent`: "absurd price rejected") y código | 8 |
| TRD-02 | ⚖ | Coste (precio + comisión) > efectivo libre | No se acepta | código (`cost > free_cash`) | 1 |
| TRD-03 | ⚖ | Oferta con forma desconocida (paquete, id suelto, claves extra, mezcla de cash y cartas) | Se ignora | selftest (`trader_agent`) | 8 |
| TRD-04 | ⚖ | Oferta "regalo" con texto engañoso | Se decide por la estructura, no por el texto | selftest (`trader_agent`: "FREE GIFT") | 8 |
| TRD-05 | ⚖ | Oferta propia, dirigida a otro equipo o cerrada | No se acepta | código | 8 |
| TRD-06 | 📸 | Compra de carta con `valor − coste ≥ max(3, 10 % del coste)` | Aparece como oportunidad; el ejemplo del selftest da `("buy", 10)` | selftest (`smart_agent`) | 8 |
| TRD-07 | 📸 | Venta a un bid de una carta con 2 copias y `neto − pérdida ≥ 2` | Oportunidad `("sell", 12)` | selftest (`smart_agent`) | 8 |
| TRD-08 | ⚖ | Nunca se vende la única copia | `n < 2` ⇒ se ignora el bid (en `phase_market`); en `page_hunter`, solo singles de sets que no construimos | selftest | 8 |
| TRD-09 | 📸 | Duplicado rentable | Se lista el de mayor serial, conservando el menor | selftest (`listing_plan`) | 8 |
| TRD-10 | ⚖ | Precio de listado | `ask ≥ suelo` (valor de la copia perdida + margen), nunca por debajo | selftest | 8 |
| TRD-11 | 📸 | Objetivo de página correcto | Con 8 de 10 de `LAV`: faltan `LAV-09`, `LAV-10`; el tope de `LAV-09` es `112 + bono/4` | selftest (`page_hunter`) | 8 |
| TRD-12 | 📸 | Página lejos de completarse (9 faltan) | Sin crédito de bonus: tope = valor simple | selftest (`page_hunter`) | 8 |
| TRD-13 | 📸 | `buy_choice` | Elige `LAV-09` a coste 117 (110 + 5 % + 1); descarta `LAV-10` a 200 por superar el tope; sin efectivo, no compra | selftest (`page_hunter`) | 8 |
| TRD-14 | 📸 | `sell_stock` | Solo singles de sets que no construimos (`LAT-10`); no el duplicado ni `LAV` | selftest (`page_hunter`) | 8 |
| TRD-15 | ⚖ | Compra con ganancia 0 (`page_hunter`) | No se compra ("ruido") | selftest + `page_hunter.log` anterior (`gain 0.0`) | 1 |
| TRD-16 | ⚖ | Un solo accept por tick, aunque varios procesos lo intenten | Solo uno llega a la API | **nuevo** (hotfix 1.4) | 1 |
| TRD-17 | ⚖ | Dos procesos de trading a la vez | Se impide (lock de instancia única) | **nuevo** (hotfix 1.2) | 1 |
| TRD-18 | ⚖ | Máx. 3 tratos con la misma contraparte en 10 min | Se respeta (fair play) | selftest (`trader_agent`) | 8 |
| TRD-19 | ⚖ | Límites de listados y ofertas abiertas | ≤ `TickBudget` (hoy: 12 listados/tick y 30 abiertas; los agentes usan 3/tick y 28) | código | 4 |
| TRD-20 | ⚖ | Bids pendientes y efectivo | Una oferta de compra no deja comprometido más efectivo del libre | **nuevo** | 8 |
| TRD-21 | 📸 | Valor de una copia | Mismo resultado en `marginal_value` de `smart_agent` y `portfolio_agent` salvo el crédito de página; **cada diferencia con el inventario real se documenta** | dato real | 8 |
| TRD-22 | ⚖ | Fallo de `b.value()` | Se usa el modelo y se marca baja confianza; no se acepta con confianza baja salvo margen mayor | código (`trader_agent`) | 8 |

## 5. Broker

| ID | Tipo | Escenario | Esperado | Origen | Fase |
|---|---|---|---|---|---|
| BRK-01 | ⚖ | Pares imposibles (`bid < ask`) | Nunca se cruzan | código (`if bid < ask: break`) | 1 |
| BRK-02 | ⚖ | Precio del cruce | `ask ≤ precio ≤ bid` | código (punto medio) | 1 |
| BRK-03 | ⚖ | Cruce en la oferta pública | `precio + comisión ≤ bid` | código (`public_plan`) | 1 |
| BRK-04 | ⚖ | Un id aparece en un solo par por plan | Sin duplicados en el plan | código (`zip`) | 1 |
| BRK-05 | 📸 | Primera observación | `MATCH` (no se espera por una conjetura) | selftest (`decide`) | 7 |
| BRK-06 | 📸 | Ambos se relajan en 2 observaciones y nadie es urgente | `WAIT` si `WAIT_ENABLED` | selftest | 7 |
| BRK-07 | 📸 | Ambos planos 2 observaciones | `MATCH` | selftest | 7 |
| BRK-08 | ⚖ | Quedan ≤ `LATE_TICKS` (2) | `MATCH` | selftest | 7 |
| BRK-09 | 📸 | Trader viejo (≥ 50 % de la sesión) | `MATCH` antes de que se vaya | selftest | 7 |
| BRK-10 | 📸 | Con `BROKER_WAIT=0` | Siempre `MATCH` | código | 7 |
| BRK-11 | 📸 | Simulación sintética (300 sesiones, semillas fijas) | Eficiencia reproducible: `match-at-once 0.902`, `este broker 0.871`; no puede **empeorar** sin explicación | selftest (salida actual) | 7 |
| BRK-12 | ⚖ | Reproducible | Mismo libro y mismo `Track` ⇒ mismo plan | código | 7 |
| BRK-13 | ⚖ | Match rechazado por el servidor | Se registra y no se reintenta cada segundo (se planifica una vez por estado del libro) | código (`seen`) | 1 |
| BRK-14 | ⚖ | `broker_memory.json` | Escritura atómica | **nuevo** (hotfix 1.6) | 1 |
| BRK-15 | ⚖ | Libro real con `bench_offers` | El plan sale igual sobre el fixture real que sobre el sintético en forma (ids `b12-7`, `want.cash`/`give.cash`) | dato real | 3 |
| BRK-16 | ⚖ | Dos instancias del broker | Se impide con lock | **nuevo** (hotfix 1.2) | 1 |

## 6. Estado, efectivo, memoria y operación

| ID | Tipo | Escenario | Esperado | Origen | Fase |
|---|---|---|---|---|---|
| STA-01 | ⚖ | Trato aceptado en este tick | `cash_free` ya lo descuenta antes de que se liquide | **nuevo** | 5 |
| STA-02 | ⚖ | Acción pendiente que no se liquida al tick siguiente | El reconciler la marca fallida/expirada y libera el compromiso | **nuevo** | 5 |
| STA-03 | ⚖ | Reserva de venue | `cash_free = cash − pendientes − reserva` | **nuevo** | 5 |
| STA-04 | ⚖ | Dos agentes piden el mismo efectivo | Solo uno se aprueba | **nuevo** | 6 |
| STA-05 | ⚖ | Validator: oferta cambió entre decidir y aceptar | Rechazada | **nuevo** (hotfix 1.3 y fase 6) | 1, 6 |
| STA-06 | ⚖ | Propuesta repetida (mismo `id`) | No se reenvía | **nuevo** | 6 |
| MEM-01 | ⚖ | Arrancar con los `memory.json` y `duels_memory.json` actuales | La memoria interna es equivalente (mismos `observed`, mismos `inferred` recalculados) | dato real | 9 |
| MEM-02 | ⚖ | Escritura interrumpida | El archivo anterior sigue siendo válido | código (`os.replace`) | 1, 9 |
| MEM-03 | ⚖ | Una política escribe memoria | Imposible: solo el orchestrator escribe | **nuevo** | 6 |
| OPS-01 | ⚖ | Segunda instancia de un proceso | Sale con mensaje claro | **nuevo** (hotfix 1.2) | 1 |
| OPS-02 | ⚖ | Ejecutar con memoria o logs ausentes | Arranca con memoria vacía | código (`load_memory`) | 1 |
| OPS-03 | ⚖ | `git status` tras ejecutar | Sin cambios en archivos dinámicos | **nuevo** (hotfix 1.1) | 1 |
| OPS-04 | ⚖ | Archivo `STOP` o `KILL=true` | El orchestrator no ejecuta nada | **nuevo** | 6 |
| OPS-05 | ⚖ | `USE_NEW_X=old` | El dominio vuelve al legacy en el tick siguiente | **nuevo** | 7 |
| OPS-06 | ⚖ | Flag en `new` y proceso legacy del mismo dominio vivo | El orchestrator se niega a ejecutar ese dominio | **nuevo** | 7 |
| OPS-07 | ⚖ | Modo sombra | **Nunca** se ejecuta la acción nueva | **nuevo** | 6 |
| OPS-08 | ⚖ | Modo sombra | El registro contiene la acción vieja, la nueva, la diferencia y el estado | **nuevo** | 6 |
| OPS-09 | ⚖ | Dashboard | Solo lectura: no importa el orchestrator ni tiene permiso de escritura | **nuevo** | 5 |

---

## 7. Cómo convertir esto en tests (sin tocar código ahora)

1. **Primero los 📸 con salida actual:** escribir un test que ejecute la función legacy con la entrada fijada y **guarde** su salida en `bz/tests/golden/*.json`. Con eso se protege el comportamiento aunque no entendamos aún por qué es así.
2. **Después los ⚖ invariantes** como propiedades: para muchas entradas aleatorias con semilla (`random.Random(seed)`), comprobar que nunca se viola la regla (por ejemplo, ningún precio de compra supera el tope).
3. **Los de dato real** usan los fixtures de la fase 0 (`duels_memory.json`, `dealers_intel.json`, libro de broker, `clock`).
4. **Los marcados "nuevo"** se escriben junto con su hotfix o su fase, y se pueden ejecutar con la misma orden: `python -m unittest discover bz/tests` (solo biblioteca estándar, igual que el resto del repo).
5. **Lo que `sim` aporta:** comparar el rendimiento relativo (por ejemplo, capturado/posible en duelos y broker) de legacy frente a nuevo con las mismas semillas. Es una señal de regresión, **no** una medida de calidad real, porque `sim` está basado en supuestos.

## 8. Lo que este checklist NO garantiza

- No prueba si la estrategia es **buena**; solo que no cambia sin que lo decidamos.
- Las formas reales de algunas respuestas (`clock["limits"]`, ofertas `types` frente a `cards`, `days` en duelos) no están confirmadas; los tests que dependen de ellas se escriben **después** de capturar los payloads reales (fase 0).
- Los números de los 📸 de este documento son los que dan hoy los selftests o el código; si cambian por un commit ajeno al refactor, hay que revisar el 📸 antes de darlo por válido.
