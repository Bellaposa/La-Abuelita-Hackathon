"""Strict bad-faith detector for dealer messages: the words name a price that the structured offer does not carry.

    python3 flags.py --selftest

Only one pattern is flagged, the one the SDK itself names ("the offer is not what the words say"):
the text states exactly ONE price tied to a currency word ("17 P", "diecisiete primas", "seventeen primas"),
that price is not one of OUR earlier prices (dealers echo ours: "Veintidós, no."), the structured offer of the same
message carries a different price, the gap is >= MIN_GAP primas, the structured price is the WORSE one for us,
and the structured price is not mentioned anywhere in the text. Anything ambiguous is not flagged:
a wrong flag costs, a missed one costs nothing.
"""
import re
import sys

MIN_GAP = 2
UNITS = {"cero": 0, "un": 1, "uno": 1, "una": 1, "dos": 2, "tres": 3, "cuatro": 4, "cinco": 5, "seis": 6, "siete": 7, "ocho": 8,
         "nueve": 9, "diez": 10, "once": 11, "doce": 12, "trece": 13, "catorce": 14, "quince": 15, "dieciseis": 16,
         "diecisiete": 17, "dieciocho": 18, "diecinueve": 19, "veinte": 20, "veintiun": 21, "veintiuno": 21, "veintiuna": 21,
         "veintidos": 22, "veintitres": 23, "veinticuatro": 24, "veinticinco": 25, "veintiseis": 26, "veintisiete": 27,
         "veintiocho": 28, "veintinueve": 29,
         "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
         "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17,
         "eighteen": 18, "nineteen": 19}
TENS = {"treinta": 30, "cuarenta": 40, "cincuenta": 50, "sesenta": 60, "setenta": 70, "ochenta": 80, "noventa": 90,
        "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90}
HUNDREDS = {"cien": 100, "ciento": 100, "doscientos": 200, "doscientas": 200, "trescientos": 300, "trescientas": 300,
            "cuatrocientos": 400, "cuatrocientas": 400, "quinientos": 500, "quinientas": 500}
CURRENCY = {"p", "primas", "prima", "pesetas"}
ACCENTS = str.maketrans("áéíóúü", "aeiouu")


def _tokens(text):
    t = text.lower().translate(ACCENTS)
    return re.findall(r"\d+|[a-zñ]+", t)


def numbers(text):
    """[(value, index of the token after the number)] for digits and Spanish/English number words (0-999)."""
    toks, out, i = _tokens(text), [], 0
    while i < len(toks):
        w = toks[i]
        if w.isdigit():
            out.append((int(w), i + 1))
            i += 1
            continue
        val, j, seen = 0, i, False
        if j < len(toks) and toks[j] in HUNDREDS:
            val += HUNDREDS[toks[j]]; j += 1; seen = True
        if j < len(toks) and toks[j] in TENS:
            val += TENS[toks[j]]; j += 1; seen = True
            if j + 1 < len(toks) and toks[j] == "y" and toks[j + 1] in UNITS and UNITS[toks[j + 1]] < 10:
                val += UNITS[toks[j + 1]]; j += 2
            elif j < len(toks) and toks[j] in UNITS and UNITS[toks[j]] < 10:   # english "twenty two"
                val += UNITS[toks[j]]; j += 1
        elif j < len(toks) and toks[j] in UNITS and not (toks[j] in ("un", "una", "uno", "one") and not seen):
            val += UNITS[toks[j]]; j += 1; seen = True
        if seen and j > i:
            out.append((val, j))
            i = j
        else:
            i += 1
    return out, toks


def priced(text):
    """Numbers followed (within one token) by a currency word: the prices the words state."""
    nums, toks = numbers(text)
    out = []
    for v, nxt in nums:
        if any(k < len(toks) and toks[k] in CURRENCY for k in (nxt, nxt + 1)):
            out.append(v)
    return out, [v for v, _ in nums]


def is_lie(text, offer_price, our_prices, dealer_sells):
    """(True, reason) only for a clear words-vs-offer mismatch that hurts us; else (False, why not)."""
    if offer_price is None or not text:
        return False, "no structured price"
    stated, mentioned = priced(text)
    stated = [v for v in stated if v not in set(our_prices)]
    if len(set(stated)) != 1:
        return False, f"{len(set(stated))} stated prices (need exactly one)"
    q = stated[0]
    if offer_price in mentioned:
        return False, "the structured price is also in the words"
    if abs(q - offer_price) < MIN_GAP:
        return False, "gap too small"
    worse = offer_price > q if dealer_sells else offer_price < q
    if not worse:
        return False, "the offer is better for us than the words"
    return True, f"words say {q} P, the structured offer is {offer_price} P"


def selftest():
    assert priced("Venga, 22 primas y nos damos un abrazo")[0] == [22]
    assert priced("My offer stands: sixteen primas, paid at once")[0] == [16]
    assert priced("Veintidós, no. Le ofrezco dieciséis primas")[0] == [16]
    assert priced("Cine Doré. 97 P. Take it or leave it.")[0] == [97]
    assert priced("cuarenta y cinco primas")[0] == [45]
    assert priced("ciento veinte primas")[0] == [120]
    assert is_lie("Venga, 22 primas y cerramos", 22, [20], True)[0] is False                      # honest
    assert is_lie("Venga, 20 primas y cerramos", 25, [18], True)[0] is True                       # says 20, asks 25
    assert is_lie("Veintidós, no. Sixteen primas.", 16, [22], False)[0] is False                  # echoes ours, honest
    assert is_lie("Te lo dejo en 20 primas", 25, [20], True)[0] is False                          # 20 was OUR price
    assert is_lie("Diecinueve no; diecisiete primas", 18, [19], False)[0] is False                # gap 1
    assert is_lie("Te pago 20 primas", 17, [22], False)[0] is True                                # says 20, bids 17
    assert is_lie("Te pago 20 primas, o 25 primas si", 17, [], False)[0] is False                 # two stated: ambiguous
    assert is_lie("Te pago 20 primas", 23, [], False)[0] is False                                 # offer better for us
    assert is_lie("20 primas, es decir 25 en total", 25, [], True)[0] is False                    # offer price in words
    print("flags selftest OK")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
