# Equipo 6 · The Bazaar, Cromos de Madrid

## Resumen

Construimos un conjunto pequeño de agentes en Python, sin LLM en tiempo de ejecución, que negocian, comercian y casan órdenes dentro de los límites privados de nuestro equipo. La idea central es que cada agente se hace en cada ronda la misma pregunta: **qué aprendí del último movimiento del rival y cómo cambia eso mi siguiente decisión**. Abuela responde a nuestras concesiones, el rival de un duelo cede o se planta, y un trader del Market Test se relaja o se va. En cada caso medimos esa reacción y ajustamos el paso siguiente. Lo que no podemos medir todavía no lo damos por sabido.

## El sistema

| Agente | Qué hace | Decisión de diseño clave |
|---|---|---|
| `smart_agent.py` | Negocia con Abuela y El Chato; compra y vende cartas en el mercado | Pasos de concesión dimensionados por la respuesta observada del dealer. Techo de compra a partir de nuestro valor privado, no del precio de lista. Memoria que separa lo observado de lo inferido |
| `smart_duels.py` | Duelos adaptativos (precio y días) | Curva tipo Boulware con exponente que se adapta al rival. Nunca cruza nuestro límite, nunca retira una oferta. Acepta si esperar vale menos, y solo razona con tasas si hay al menos 2 movimientos observados |
| `broker.py` | Broker del Market Test | Controlamos cuándo y con quién casar, no el precio. Casa si la sesión termina, si alguien parece irse o si ambos lados están firmes. Espera solo si ambos se relajan |
| `page_hunter.py` | Completa páginas del álbum | Vende cartas de poco valor para nosotros (sets que no construimos) y compra las que faltan con un techo que incluye la mitad del bonus de página repartido entre las cartas que faltan |
| `dashboard/` | Panel de control de solo lectura | Un único endpoint con caché, así que cualquier número de pestañas no cuesta nada al juego. No opera |

## Oficio

- **Determinista y sin LLM en ejecución.** Las decisiones son reproducibles y no dependen de lo que un dealer diga con palabras: leemos la estructura de la oferta, no el texto.
- **Cada decisión queda registrada con su motivo** en el log de cada agente, y el panel las muestra.
- **Pruebas offline.** `--selftest` en los agentes (sin red) y simulaciones con rivales sintéticos dentro de esos mismos `--selftest` (por ejemplo, el broker contra un mercado inventado).
- **Memoria honesta.** `memory.json` y `duels_memory.json` guardan los hechos observados aparte de las inferencias. Una inferencia solo se calcula con muestra suficiente (por ejemplo `MIN_SAMPLES = 5`, `MIN_DEALS = 3`) y lleva su tamaño de muestra.
- **Reglas de seguridad.** Nunca cruzar un límite privado. Un agente por dealer. Nunca repetir un precio. Pausa tras retirarnos de una conversación. Un solo accept por tick.

## Lo que nos enseñaron los datos (incluidos los errores)

1. **Los sobres no compensan.** Un sobre vale unos 10,6 primas para nosotros y el dealer lo vende a 21-24. Dejamos de comprar sobres después de perder unas 35-40 primas de valor (cifra aproximada). Ahora el techo sale del valor privado de las cartas que aún nos faltan.
2. **Repetir un precio nos castigó.** El agente antiguo repitió el mismo precio con Abuela y acabó en `cooloff` (hilo 148) y en `no_progress` (hilo 109). Desde entonces no repetimos precios. Además, en 4 hilos ella aceptó nuestra oferta cuando estaba a 1 prima de su pedido, y el agente actúa en consecuencia: acepta su pedido en cuanto queda a 1 prima de nuestra última oferta.
3. **Esperar no ganó a casar al momento (en simulación).** En el broker, la regla de espera no superó a casar de inmediato. Por eso es conmutable (`BROKER_WAIT=0`) y la decidiremos con datos reales del Market Test. No tenemos todavía esos datos.
4. **La puntuación parece relativa al mejor equipo**, así que la actividad sin valor no ayuda. Es una lectura nuestra, no un hecho confirmado. Por eso solo operamos cuando hay ganancia sobre nuestro valor privado.

## Duelos de práctica (no puntúan)

De 13 duelos cerrados, 11 acabaron en acuerdo con ganancia sobre nuestro límite privado (de 0,5 a 42,6 puntos) y 2 sin acuerdo, porque el rival no dijo nada en todo el duelo. El peor acuerdo fue a 110 con límite 111: el rival había cedido hasta su propio límite y el margen real era de 1 prima.

En dos duelos de venta el rival aceptó casi de inmediato nuestra primera petición, que era 1,5 veces el coste. Eso sugiere que pedimos poco en esos casos. Con solo 6 duelos de venta no cambiamos todavía la apertura (`OPEN_ANCHOR`): subirla sin datos puede alargar los duelos, y cada ronda de más cuesta un 6 %. Seguimos midiendo y la calibraremos con los datos completos de la práctica.

## Con más tiempo

- Decidir `BROKER_WAIT` con varias sesiones reales del Market Test y medir la ganancia capturada de cada regla.
- Calibrar los duelos con los datos de práctica: ancla de apertura, exponente `BETA` y el signo del peso de días (`DAYS_SIGN`, hoy supuesto).
- Lograr un acuerdo negociado con El Chato (nivel 2), donde todavía no tenemos ninguno, usando una carta distinta por conversación y sin repetir precios.
