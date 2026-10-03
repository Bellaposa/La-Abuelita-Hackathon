# TRADING_V2 — estrategia de mercado entre equipos

Un solo proceso (`trading_v2.py`) decide **todo** el trading entre equipos: compras, ventas a pujas, swaps, listados, pujas propias y (opcional) arbitraje. Sustituye a `smart_agent.phase_market` y a `page_hunter` cuando se activa (`on`). **Por defecto está en `shadow`: solo observa y registra, no escribe nada.**

## Cómo se usa

```bash
BAZAAR_KEY=tk-... python trading_v2.py          # un proceso, deja que lea el modo cada tick
echo shadow > .trading_v2_mode                  # off | shadow | on  (se relee cada tick, sin reiniciar)
```

| Modo | Qué pasa |
|---|---|
| `off` | `trading_v2.py` no hace nada. El trading antiguo sigue como siempre. |
| `shadow` (por defecto) | Lee los tableros, decide y **solo registra** en `logs/trading_v2_shadow.jsonl`. No escribe nada contra Bazaar. El trading antiguo sigue operando. |
| `on` | Ejecuta. `smart_agent` (fase de mercado) y `page_hunter` se apartan solos, así que hay una única autoridad. |

Volver atrás: `echo off > .trading_v2_mode` (efecto en el siguiente tick). Si el modo es `on` y `trading_v2.py` **no** está corriendo, nadie hace trading entre equipos: arrancad el proceso o poned `off`.

Recomendación: 20–30 minutos en `shadow`, leer el log, y solo entonces `on`. Que `trading_v2` no esté corriendo a la vez que `agents/trader_agent.py`.

## Qué hace, punto por punto de la propuesta

| # | Idea | Estado | Cómo |
|---|---|---|---|
| 1 | Valor efectivo en vez de "libro × 1,3" | **Hecho** | Valor de una copia más (servidor `b.value`, con el modelo de respaldo) + 65 % del bonus de página esperado. Se calculan suelos por el valor marginal de la copia. Faltan `strategic_value` y `scarcity`. |
| 2 | Perfiles por equipo | **Parcial** | `MarketIntel` guarda, por maker, pujas y listados por set y deduce "constructores probables". Los makers del tablero son **seudónimos** (`m41383bb2`), no ids de equipo: no se ve qué cartas tienen. |
| 3 | Vender páginas, no cartas | **Parcial** | Si el mejor postor de una carta parece constructor de ese set, su estimación sube un 25 %. No se conoce la colección del comprador, así que no se aplica un α sobre el bonus. |
| 4 | Venta dirigida | **Parcial** | **No se puede dirigir una oferta a un seudónimo** (`to=` pide id de equipo). Se fija el precio contra las pujas vistas (80 % de la estimación) y se aceptan las pujas que ya pagan. |
| 5 | Arbitraje | **Hecho, apagado** | Comprar una oferta y revender a una puja viva que pague más. Activar con `TRADING_V2_ARB=1`. Apagado porque **no sabemos si el efectivo cuenta en la puntuación** (`RULES.md` habla de "valor ganado a vuestros valores privados"). |
| 6 | Reglas de compra menos conservadoras | **Hecho** | Márgenes dinámicos (mín. 1), ROI mínimo 4 % (antes 10 %), cierre de página con ganancia ≥ 1. |
| 7 | Pujas adaptativas | **Hecho** | 60 % del máximo sin competencia; +1 sobre un rival débil; hasta el 90 % con competencia fuerte; última carta de página hasta el máximo menos 1. Re-puja si nos superan (una por tick). |
| 8 | Sin guerra de precios | **Hecho** | Se iguala el ask rival más barato (a veces −1 para variar); si está por debajo de nuestro suelo, no se lista. |
| 9 | Liquidez | **Hecho (proxy)** | Pujas vistas + 2 × desapariciones tempranas + pujadores distintos. Una desaparición puede ser trato o cancelación. |
| 10 | No filtrar información | **Parcial** | 20 % de los listados se retrasan, ±1 en precios, máx. 4 acciones de escritura por tick. |
| 11 | Un único cerebro | **Hecho** | `decide()` produce un plan por tick; el legacy se aparta en modo `on`. |
| 12 | Una sola autoridad | **Hecho en la misma máquina** | Modo `on` + puerta de accept compartida (hotfix 1.4). No cubre agentes de otras máquinas con la misma clave. |
| 13 | Swaps por valor marginal | **Hecho** | Lo que damos se valora a la pérdida marginal de la copia; basta ganancia ≥ 1 (≥ 0 al cierre del día). |
| 14 | Bundles | **No hecho** | Las ofertas con varias cartas se ignoran. |
| 15 | EV por minuto | **Parcial** | Puntuación = ganancia × probabilidad × urgencia / capital bloqueado, y márgenes dinámicos. No hay objetivo de volumen. |
| 16 | Fases del torneo | **Parcial** | Solo la presión de los últimos 90 minutos de cada día (reduce márgenes). No hay fase temprana/media. |
| 17 | Opportunity Engine | **Hecho** | Buys, ventas a puja, swaps y arbitraje se ordenan con una puntuación común; solo uno se acepta por tick. |
| 18 | Estrategia concreta | **Hecho** | Ver los puntos 1, 4–8 y 13. |

## Garantías (con tests)

- Nunca vende la última copia (salvo un lote de arbitraje que compró para revender).
- Nunca acepta una operación con ganancia por debajo del margen dinámico, a nuestros valores privados.
- Un solo accept por tick, solo de tableros leídos en ese tick, nunca de nuestro propio venue, y a través de la puerta de accept.
- Máx. 3 tratos con la misma contraparte por ventana de 10 minutos (fair play).
- Pujas abiertas: como mucho el 50 % del efectivo. Listados y pujas: máx. 4 escrituras por tick.
- Ignora packs, paquetes de varias cartas, ofertas con claves desconocidas, ofertas de hilos de dealers y las dirigidas a otro equipo.
- Lee El Rastro cada tick y 3 venues más en rotación (para no pasar de 5 peticiones por segundo).

## Qué NO sabemos (y por qué las constantes son hipótesis)

Todas están en `bz/trading/policy.py` (`CFG`). Ninguna se ha medido en este juego: 0,65 del bonus, 60/90 % de las pujas, 80 % de la estimación del comprador, 4 % de ROI, 25 % de boost de constructor, etc.

- **Si el efectivo cuenta en la puntuación:** condiciona el arbitraje y las ventas por precio puro.
- **Si `b.value()` ya incluye algún efecto de página:** si lo incluye, el crédito de página se cuenta en parte dos veces. El log `shadow` guarda ambos valores para comprobarlo.
- **La forma de `standing_offers` en un hilo vivo** no se usa aquí (solo tableros públicos), pero sí en el hotfix 1.3.
- **Los tableros reales** traen packs (`sobre_plata` a 128) que se ignoran: puede haber oportunidad ahí.

## Ficheros

`trading_v2.py` (proceso) · `bz/trading/mode.py` · `bz/trading/intel.py` · `bz/trading/policy.py` · `bz/trading/engine.py` · tests `bz/tests/test_trading_v2_*.py` (110) · fixture real `bz/tests/fixtures/board_rastro.json`. Datos locales: `market_intel.json` y `logs/` (no se versionan).
