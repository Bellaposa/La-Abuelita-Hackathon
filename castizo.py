"""What we SAY to each dealer: Madrid talk in its own register. Words only: the structured price binds, the words never
change a dealer's price (RULES), but they change what it says, and Saturday showed that dealers answer it: Team 10 talked
vermut, Cascorro and Plaza Mayor to El Chato, rosquillas, cocido and la Paloma to Abuela, the Embassy and violetas to
Doña Pilar, and got gifts, softer finals and three badges. We never repeat a line in a row (Chato and Don Ernesto take
the same words again as spam) and every line carries the price.

    python3 castizo.py --selftest
"""
import sys

LINES = {
    "abuela": {
        "open": ["¡Buenas, Abuela Carmen! Ya he comido, un cocido con sus tres vuelcos, como Dios manda. Vengo por {item}: ¿le parece {p} primas?",
                 "¡Hola, Carmen! Traigo rosquillas tontas y listas para la merienda. ¿Me deja {item} en {p} primas?"],
        "next": ["Ay, Carmen, que me tira de la manga el nieto: {p} primas, y el domingo le traigo churros de San Ginés.",
                 "Usted sí que sabe, Abuela. {p} primas, y me acuerdo de usted en la Paloma.",
                 "Que me lo dijo mi abuela: en el Rastro, con Carmen, siempre se llega a un trato. ¿{p} primas?",
                 "Por los cuarenta años de puesto, Carmen: {p} primas, y un abrazo de un gato de Madrid."],
    },
    "chato": {
        "open": ["Buenas, Chato. Antes del vermut en Cascorro, a lo nuestro: {item}, {p} primas.",
                 "Chato, después un bocata de calamares en la Plaza Mayor con una caña. Ahora: {item}, {p} primas."],
        "next": ["Me muevo, como tú: {p} primas.", "Ni pa' ti ni pa' mí: {p} primas.", "Vermut de grifo y {p} primas, Chato.",
                 "Lo justo en el Rastro: {p} primas.", "Un paso más, castizo: {p} primas."],
    },
    "pilar": {
        "open": ["Buenas tardes, Doña Pilar. Vengo de merendar en el Embassy, tarta de limón y violetas. {item} merece su álbum: {p} primas.",
                 "Doña Pilar, una lámina digna de Serrano y Velázquez: {item}, {p} primas."],
        "next": ["Con todo respeto a su colección: {p} primas.", "Para un álbum guardado desde los sesenta: {p} primas.",
                 "Como diría Lázaro Galdiano, lo bueno se paga: {p} primas.", "Ni una peseta de más, señora: {p} primas."],
    },
    "picaros": {
        "open": ["¡Paco, Nando! Que me sé el timo de la estampita, como el Lazarillo y Rinconete. Sin trucos: {item}, {p} primas.",
                 "Hermanos, que a mí no me la dais con queso: {item}, {p} primas, y la carta exacta."],
        "next": ["Sin estampitas, amigos: {p} primas.", "Que el Lazarillo también regateaba: {p} primas.", "{p} primas, y la carta que pido, no otra.",
                 "Rinconete pagaría {p} primas, y yo también."],
    },
    "banco": {
        "open": ["Buenas tardes, Don Ernesto. Sin prisa, como en Casa Prima: {item}, {p} primas.",
                 "Don Ernesto, desde la calle de Alcalá con respeto: {item}, {p} primas."],
        "next": ["Con su paciencia y la mía: {p} primas.", "Un paso más, caballero: {p} primas.", "Despacio y con criterio: {p} primas.",
                 "Mi cifra sube, como conviene: {p} primas.", "Sin prisa alguna: {p} primas."],
    },
}
NAMES = {"Chato": "chato", "Pilar": "pilar", "Pícaros": "picaros", "Picaros": "picaros", "Don Ernesto": "banco",
         "Banco": "banco", "Abuela": "abuela", "amigos": "picaros"}


def line(dealer, price, n_round, item):
    """Our words for round n_round (0 = the opening) to `dealer`, with the price in them. Rotates; never the same line
    twice in a row."""
    bank = LINES.get(dealer)
    if not bank:
        return None
    pool = bank["open"] if n_round == 0 else bank["next"]
    return pool[(n_round if n_round else 0) % len(pool)].format(p=price, item=item)


def selftest():
    for d in LINES:
        seen = [line(d, 10 + i, i, "LAT-07") for i in range(8)]
        assert all("{" not in s and str(10 + i) in s for i, s in enumerate(seen)), (d, seen)
        assert all(a != b for a, b in zip(seen, seen[1:])), f"{d}: a line repeated in a row"
    assert line("nobody", 5, 0, "x") is None
    print("castizo selftest OK")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
