# Estrategia de Team 6 (revisada el sábado 2026-10-03, ~21:35)

Revisar antes de la ronda del domingo (09:00). Base: RULES.md y los decks de la organización ("Duels" y "Payday").

## NORMA CLAVE del organizador (vale para todo el juego): no perder dinero, ganarlo

Comprar está bien, incluso es lo esperado. Lo que no se permite es **un movimiento o una estrategia que pierda dinero a la larga**. Por ejemplo, la vuelta de épicas compraba a 143 y vendía a 120 una y otra vez. Se aplica donde se mueve el dinero:
- se compra siempre por debajo de nuestro `your_value`;
- se vende siempre por encima del valor del servidor (con el bonus de página);
- nada de reventa de dealer a dealer con pérdida ni de primas de escalera;
- las pujas abiertas cuentan como caja comprometida.

No hay suelo de caja por defecto (`CASH_RESERVE=0`). El sábado a las ~21:50 se puso un suelo de 541 P por un malentendido y se quitó minutos después.

## La regla que lo decide todo (deck "Payday")

- **Un trato vale = valor que añade a tu colección − precio pagado + precio cobrado** (`your_value` del servidor, con el bonus de página).
- **Con un equipo:** la ganancia cuenta hasta 50 y la pérdida entera.
- **Con un dealer:** la ganancia cuenta en la escalera y la pérdida entera.
- **De dealer a dealer:** comprar no gana nada y vender por debajo del valor resta.
- **No puntúan:** la caja, las cartas, el álbum, los regalos ni los easter eggs.
- **Pesos:** negociación 30 (duelos, escalera de dealers, intercambios), Market Test 22,5, intercambios reales de otros equipos en v01 7,5, jueces 40.
- **Cada día es una ronda:** el viernes vale la mitad, y el sábado y el domingo enteros.

## Lo que hacemos

| Pieza | Qué hace | Salvaguarda |
|---|---|---|
| `smart_agent` (dealers) | Compra cartas de página a Abuela, Chato y Pícaros por debajo de nuestro valor; vende repetidas a Chato, Pilar y Pícaros | Suelos de venta = `your_value` del servidor (con bonus de página), revisados en cada ronda; topes de compra < valor |
| `smart_agent` (cámara de Ernesto) | Compra la legendaria que más vale para nosotros (LAV-12 = 720), negociando con paciencia desde la mitad de la lista | Tope = valor − 30, una por hora; trato de nivel 5 sin pérdida |
| Vuelta de épicas | **Apagada** (`EPIC_LOOP=0`): vender épicas a Ernesto (~120) por debajo de nuestro valor (160–288) resta | Si se activa: compra < valor y la épica espera a un equipo comprador |
| Prima de escalera | **Apagada** (`LADDER_PREMIUM=0`): pagar más que nuestro valor es una pérdida entera | `ladder.py` sigue llevando el libro de la escalera |
| `page_hunter` | Vende copias sueltas a equipos por encima de nuestro valor (RET-11 por 216 valiendo 162) | Suelo = máximo entre nuestro valor y el del servidor |
| Mercado (`smart_agent`) | Compra a equipos por debajo de nuestro valor y vende por encima; opera en v21 (Team 9), no en v07 (Team 10, rival directo) | |
| `smart_duels` | Duelos: curva de concesión hasta la reserva en 8 rondas (cada ronda encoge el pie un 6–8 %), acepta si la oferta vale lo que la nuestra tras una ronda de decay; días con signo según `days_meaning`; la oferta completa (precio + días) nunca deja menos que el margen | Nunca cruza el límite |
| `broker` (v01) | Market Test: casa como el puesto automático (lo mejor en todos los replays), sin esperas | Registra la respuesta de cada casación |
| `venue_promoter` + `team_profiles` | Perfiles de equipos desde el feed (qué buscan, qué les sobra, qué tienen, si responden) y un anuncio cada 20 ticks con las mejores parejas entre equipos para v01 | Nunca durante un Market Test |

## Errores que cometimos y no hay que repetir (sábado)

1. Creer que el valor de las cartas no puntuaba: la vuelta de épicas vendió SAL-11 a Ernesto por 120 valiendo 198 (−78).
2. Pagar primas de escalera por encima de nuestro valor.
3. Usar nuestro valor local (sin bonus de página) como suelo de venta: una común de Lavapiés vale 122 para nosotros.
4. Duelos II: sumar el peso de los días también como comprador (pedíamos el día 10), y ceder días a la vez que precio hasta salir de nuestro margen (−20 a −31 en tres duelos).

## Para el domingo

- **09:00:** +150 P, Ernesto abierto a todos y ronda nueva (la escalera vuelve a cero).
- **Cámara de Ernesto:** con la caja que quede, la siguiente legendaria solo si vale más que su precio para nosotros (SAL-12 = 495 no deja margen a ~470).
- **~11:00, Duelos III:** reloj más corto y más decay. Revisar `SPEED_HORIZON` (quizá 5–6) y ver los resultados de los Duelos II en `duels_memory.json`.
- **Market Test (09:35 el difícil, 22,5 puntos):** sin cambios salvo evidencia nueva de `logs/bench_watch.jsonl`.
- **Mercado:** el deck pide intercambios carta por carta y terminar páginas; el broker aún no cruza intercambios sin dinero.
- **15:00:** cierre. La caja que sobre no vale nada; gastarla solo en tratos por debajo de nuestro valor.
