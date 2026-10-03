# Fixtures de la Fase 0

Capturados el **2026-10-03 ~11:03 (hora local)**, tick 346 del servidor, sobre el commit `098ff55` (tag `legacy-v1-baseline`). Resultado de los selftests: `BASELINE_TEST_RESULTS.md`.

## Cómo se capturaron

- **Solo lecturas `GET`.** Una petición por endpoint, sin polling, sin abrir conversaciones y sin ninguna escritura contra Bazaar.
- **Sin clave de equipo.** `BAZAAR_KEY` no está definida en este entorno (ni en proceso, ni en usuario, ni en máquina) y no se buscó en ningún otro sitio. Por eso solo se capturó lo que es público y el libro del broker.
- **No se usó el SDK de Python.** Las lecturas se hicieron con `curl` porque `urllib` de Python falla en esta máquina:
  `SSL: CERTIFICATE_VERIFY_FAILED ... certificate has expired`. La verificación de `curl` sí pasa (el certificado del servidor es válido: Let's Encrypt `YE1`, del 2 de octubre al 31 de diciembre de 2026) y la hora de la máquina coincide con la cabecera `Date` del servidor. **No se desactivó la verificación TLS.** Es un hallazgo aparte, ver "Hallazgos".
- Para el libro del broker se leyó la clave local `.broker_key` y se envió solo en la cabecera `X-Broker-Key` de un `GET /api/broker/book`. La clave no se guarda en ningún fixture.
- Comandos equivalentes: `curl -sS https://bazaar.causaprima.ai/api/<endpoint>`; para el libro, la misma URL `/api/broker/book` con la cabecera `X-Broker-Key`.

## Qué hay

| Fixture | Endpoint / origen | Estado |
|---|---|---|
| `clock.json` | `GET /api/clock` | capturado |
| `dealers.json` | `GET /api/dealers` | capturado |
| `levels.json` | `GET /api/levels` | capturado |
| `schedule.json` | `GET /api/schedule` | capturado |
| `venues.json` | `GET /api/venues` | capturado |
| `catalog.json` | `GET /api/catalog` | capturado (no estaba en la lista, es público y necesario para valores y páginas) |
| `broker_book.json` | `GET /api/broker/book` (con `X-Broker-Key`) | capturado; **`offers` y `bench_offers` vacíos** en este momento |
| `history/dealers_intel_sample.json` | `dealers_intel.json` (guardado en el tick 112, 2026-10-02): hojas de Abuela y Chato, `levels`, y 7 hilos: `2` (primer trato), `33` (trato con `final`), `55` (`persona_quota`), `109` (`walked`/`no_progress`), `148` (`cooloff`), `181` (abierto), `194` (Chato, cerrado) | histórico |
| `history/duels_raw_samples.json` | `raw_samples` de `duels_memory.json` (3 duelos de la sesión 1, solo precio) | histórico, con `your_limit` sustituido por `100` |

Los originales (`dealers_intel.json`, `duels_memory.json`) **no se han modificado**.

## No capturado (pendiente)

| Fixture | Motivo |
|---|---|
| `me` | Requiere la clave de equipo, que no está disponible |
| `duels` (activos y `done=true`) | Requiere la clave de equipo |
| `my_offers` | Requiere la clave de equipo |
| `my_threads` y hilos vivos de Abuela y Chato | Requiere la clave de equipo; además no se abren conversaciones para generar ejemplos |
| `broker_book` con `bench_offers` | El Market Test no está en curso. El calendario lo sitúa en `at_hours: 5.0`, y ahora son `t_hours: 4.2083` |
| `GET /api/news` | Aparece en `levels` (Radio Rastro) pero no estaba en la lista; **no se capturó** |

Equivalentes históricos disponibles mientras tanto: los hilos de `history/` (forma de `thread`/`messages`/`standing_offers`) y `duels_raw_samples.json` (forma de `duel`).

## Sanitización

- **Claves y tokens:** ninguna clave aparece en los fixtures (búsqueda de `tk-` y `bk_`: sin resultados). El libro de broker no incluye la clave en su respuesta.
- **Equipos rivales:** los ids `tNN` distintos de `t06` pasan a `rival_NN`, y `Team N` (salvo `Team 6`) a `Rival Team NN`, de forma consistente dentro de cada ejecución. Afecta a `venues.json`, `broker_book.json` (`recent[].parties`, `frm`, `to`) y a los hilos.
- **Nuestro id (`t06`) y nuestro venue (`v01`, "Mercado Team 6")** se mantienen.
- **Nombres de venues y descripciones libres** de otros equipos se mantienen (texto público), salvo la sustitución del nombre de equipo.
- **Alias de duelos** (`Rival Plata`) se mantienen: los genera el juego.
- **Valores privados:** en lo capturado no hay (no se pudo capturar `me`). En `duels_raw_samples.json` se reemplazó `your_limit` por `100`.
- Los datos exactos y sin tocar están en `_private/` (**en `.gitignore`**, no se suben): copias crudas de cada captura, `dealers_intel.exact.json` y `duels_raw_samples.exact.json`.
- **El repositorio es público** (`api.github.com` responde 200 sin autenticar). Por eso `_private/` y `backup/` están ignorados.

## Observed clock limits schema

JSON real y sanitizado de `GET /api/clock` (tick 346, sábado, doors `open`):

```json
{
  "tick": 346,
  "tick_seconds": 30.0,
  "paused": false,
  "next_tick_in": 9.33,
  "limits": {
    "accepts_per_team_per_tick": 1,
    "messages_per_side_per_tick": 1,
    "max_open_threads_per_team": 6,
    "max_open_offers_per_team": 30,
    "offers_per_team_per_tick": 12
  }
}
```
(Recorte: el payload completo está en `clock.json` e incluye `t_hours`, `round`, `round_name`, `min_tick_seconds`, `max_tick_seconds`, `calendar`, `calendar_on`, `doors`, `today`, `today_name`, `closes`, `next_opens`, `next_name` y `days[]`.)

**Campo `limits`:** objeto plano de 5 enteros.

| Campo | Tipo | Valor | Significado | Fuente |
|---|---|---|---|---|
| `limits` | objeto | 5 claves | límites en vigor ahora | **COMPROBADO** (existe y es un objeto) |
| `limits.accepts_per_team_per_tick` | entero | `1` | aceptaciones de ofertas por equipo y tick | **COMPROBADO** el nombre y el valor; la interpretación coincide con `RULES.md` ("your team may accept one offer") → **INFERIDO** que cuenta duelos y dealers también |
| `limits.messages_per_side_per_tick` | entero | `1` | mensajes por lado y tick (por conversación) | **COMPROBADO** el nombre y el valor; **INFERIDO** de `RULES.md` ("one message per conversation") que "por lado" significa por conversación y por parte. No se sabe si cuenta también los mensajes de duelo |
| `limits.max_open_threads_per_team` | entero | `6` | conversaciones abiertas a la vez | **COMPROBADO** valor; coincide con `RULES.md` ("holds up to six conversations") |
| `limits.max_open_offers_per_team` | entero | `30` | ofertas abiertas a la vez | **COMPROBADO** valor; coincide con `RULES.md` ("thirty open offers") |
| `limits.offers_per_team_per_tick` | entero | `12` | listados nuevos por tick (una cancelada también cuenta) | **COMPROBADO** valor; coincide con `RULES.md` ("twelve new listings") |

Otros campos del reloj relevantes para el contrato:

| Campo | Tipo | Valor visto | Fuente |
|---|---|---|---|
| `tick` | entero | 346 | COMPROBADO |
| `t_hours` | número | 4.2083 | COMPROBADO (horas de juego; el calendario de `schedule` usa esta unidad en `at_hours`) |
| `tick_seconds` | número | 30.0 | COMPROBADO (coincide con `RULES.md` para el sábado) |
| `min_tick_seconds`, `max_tick_seconds` | número | 5.0, 60.0 | COMPROBADO (coincide con `RULES.md`: 5–60 s) |
| `next_tick_in` | número | 9.33 | COMPROBADO (segundos) |
| `paused` | booleano | `false` | COMPROBADO |
| `doors` | cadena | `"open"` | COMPROBADO; otros valores posibles: **no observados** |
| `days[]` | lista de objetos | `fri`, `sat`, `sun` con `opens`, `closes`, `tick_seconds` | COMPROBADO; fechas con zona horaria (+02:00) |
| `closes`, `next_opens` | cadena ISO 8601 | `2026-10-03T23:00:00+02:00`, `2026-10-04T09:00:00+02:00` | COMPROBADO |
| `round`, `round_name` | entero, cadena | `2`, `"Saturday · Gran Vía"` | COMPROBADO (UTF-8 correcto; un primer vistazo con `cp1252` lo mostró mal por error mío al leer el archivo) |

Lo que **no** se sabe:
- Si `limits` cambia durante el juego (`RULES.md` dice que los organizadores pueden moverlos y que el feed lo anuncia, pero no se ha observado un cambio).
- Qué ocurre con las claves nuevas: si aparecen otras, un parser estricto fallaría; el parser de la Fase 3 debe ignorar claves desconocidas.
- Los valores actuales **coinciden** con las constantes fijas del código (1 accept, 1 mensaje, 6 hilos, 30 ofertas, 12 listados), así que hoy no hay discrepancia.

## Hallazgos de esta captura (relevantes para el plan, no aplicados)

1. **`/api/dealers` devuelve la clave `personas`, no `dealers`.** El simulador `sim/` asumió `{"dealers": [...]}`; hay que corregirlo cuando se adapte. Cada elemento trae `id`, `name`, `status`, `level`, `kind` (`dealer` o `collector`), `traits`, `unlock`, `open_to_all`, `menu`.
2. **Hay un dealer que el código no conoce y ya está activo: Doña Pilar** (`pilar`, nivel 3, `kind: collector`). Compra a precios altos las cartas que completan sus álbumes y vende `sobre_oro`. Se desbloquea con 3 tratos negociados con Chato y nivel 2 (`early_deals_with: chato`, `early_min_deals: 3`, `early_min_level: 2`) y se abre a todos a las `+5.51 h`. Con el estado actual `smart_agent.py` nunca negocia con ella. Esto confirma el riesgo "dealers futuros no descubiertos".
3. **Hay un nuevo endpoint de noticias: `GET /api/news`** ("Radio Rastro", con tres fuentes, algunas falsas). Ningún agente lo usa. No se capturó.
4. **Calendario próximo** (`schedule.json`): el **Market Test (`bench`) a las `5.0 h`**, **Duels I a las `5.15 h`** (solo precio, 16 ticks, decay 0.06, hasta 3 simultáneos) y la apertura de Pilar a las `5.508 h`. Ahora son `4.2083 h`.
5. **Nuestro venue ya existe:** `v01`, "Mercado Team 6", `mechanism: board`, fianza 250, abierto en el tick 100, comisión 0. Así, el riesgo "sin reserva para el venue" se refiere a la fianza ya pagada; conviene reevaluar su peso antes del hotfix 1.8.
6. **Python no verifica el certificado del servidor en esta máquina** (ver arriba). Si los agentes se ejecutan desde aquí, las llamadas fallarían con error `network` y `smart_agent` solo registraría "error" y reintentaría. No se ha comprobado si los procesos en marcha están afectados. Puede ser una cadena de certificados que la tienda de certificados de Windows aún no confía **(hipótesis)**.
7. **Libro de broker:** `offers: []` y `bench_offers: []` ahora; `recent` trae liquidaciones con `parties`, `items[].frm/to` y `price`. En sesiones del Market Test, `bench_offers` debería estar relleno: **hay que capturarlo en cuanto empiece** (a las 5.0 h).
