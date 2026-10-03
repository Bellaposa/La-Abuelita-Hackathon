"""Dealer listener: read-only scan of the public feed, the news and our own dealer threads for easter-egg / gift / hidden-card
hints. Stores what it finds in dealer_hints.json. NOT wired into any running agent.

    BAZAAR_KEY=tk-... python3 dealer_listener.py --report     # one live scan (GET only), merge into dealer_hints.json, print
    python3 dealer_listener.py --selftest                     # offline checks, no network

What it looks for
  * events  egg.found / gift.given / badge.awarded  -> a TRIGGER record: who, which dealer, when, and the dealer messages to
            that team in the TRIGGER_WINDOW ticks before it (team texts are null in the public feed, but dealers echo the
            words that worked: the Pícaros answered "Lazarillo, Rinconete ... la estampita ... no tricks for you" right
            before every "Trickster tricked" egg).
  * dealer messages (public thread.message events + our own threads) with hint words (hidden, vault, secret, gift, regalo,
            rumour, legend, chulapa, story, trick, ...), card refs (LAT-13) or card names from the catalog -> a HINT record.
  * news items (Radio Rastro / Boletín / El Tablón) with the same words -> a HINT record (source "news").

Scoring reminder (RULES.md + Payday slides): gifts, easter eggs, badges and pack luck NEVER count toward the score.
This module is for the judges' "ideas and craft" story and for spotting real price news, not for points.

API for other code (pure reads of dealer_hints.json, no network):
    hints_for(dealer, limit=20) -> list of hint dicts for one dealer, newest first
    triggers(dealer=None)       -> list of trigger dicts (eggs, gifts, badges), optional dealer filter
    known_phrases(dealer)       -> phrases believed to trigger an egg/gift with that dealer (curated + learned)
    scan(b, path=...)           -> live GET-only scan with a bazaar_sdk.Bazaar, merges and saves, returns the summary
"""
import json
import os
import re
import sys
import time

HINTS_FILE = os.environ.get("DEALER_HINTS", "dealer_hints.json")
DEALERS = {"abuela": "Abuela Carmen", "chato": "El Chato", "pilar": "Doña Pilar", "picaros": "Los Pícaros",
           "banco": "Don Ernesto"}
TRIGGER_WINDOW = 8          # ticks of dealer talk kept before an egg/gift
KEEP_HINTS = 400
KEEP_TRIGGERS = 200

# Words that mark a hint. Lowercase, accent-insensitive match (see _norm).
HINT_WORDS = ("hidden", "secret", "secreto", "vault", "camara", "cámara", "gift", "regal", "present", "egg", "huevo",
              "rumour", "rumor", "legend", "chulapa", "dorada", "only one", "print run: one", "prestige", "story",
              "historia", "trick", "truco", "estampita", "lazarillo", "rinconete", "galdos", "galdós", "saint", "santo",
              "grandson", "nieto", "album", "álbum", "safe", "caja fuerte", "riddle", "password", "contraseña",
              "who says", "quien diga")
REF_RE = re.compile(r"\b(LAV|MAL|LAT|SAL|RET|CHA)-(\d{2})\b")

# Curated from the live feed of Sat 2026-10-03 (ticks 1214-1243) and the Payday slides. Evidence, not certainty.
KNOWN = {
    "picaros": {
        "egg": "Trickster tricked (badge)",
        "phrases": ["Lazarillo", "Rinconete", "el timo de la estampita"],
        "evidence": "t18/t05/t10, ticks 1227-1231: the Pícaros reply 'Lazarillo, Rinconete, la estampita! You know the old "
                    "trick... no tricks for you, today' and egg.found + badge 'Trickster tricked' follow within 1-5 ticks. "
                    "Topic was a sell of a common (LAT-02 etc.).",
    },
    "abuela": {
        "egg": "gift.given (a card)",
        "phrases": ["kindness: offer MORE than she asks (she sells at 5 P, the team offered 8, three times)",
                    "'¿Has comido?' small talk; she likes kindness"],
        "evidence": "t08, ticks 1215-1232: three threads where Abuela offered Mural de la Esquina (LAV-04) for 5 P and t08 "
                    "kept offering 8; on the third she wrote 'te la regalo, por ser tan amable' and gift.given LAV-04 "
                    "followed. Earlier: Boletín 'Abuela gives out packs for her saint's day'; she gave us a Cartel de "
                    "Conciertos once (thread 22).",
    },
    "banco": {
        "egg": "La Chulapa Dorada (LAT-13), hidden legendary 1/1, already FOUND by Team 2",
        "phrases": ["the gold chulapa", "La Chulapa Dorada"],
        "evidence": "catalog LAT-13: 'Only one was ever printed. Don Ernesto knows where.' minted 1, hidden. Ernesto: "
                    "'the gold chulapa stays in the vault today'. Slides: 'Three teams heard the rumour. One followed it "
                    "all the way.' Prestige only: no dealer buys it.",
    },
    "pilar": {
        "egg": "unknown (her album's stories)",
        "phrases": ["Galdós (tried by t10: 'Flattery well chosen... my album's stories stay in the safe')"],
        "evidence": "t10, tick 1233: no egg followed; the 'safe' and 'stories' suggest a story-for-a-secret egg.",
    },
    "chato": {
        "egg": "unknown",
        "phrases": ["asking his story (t10 got 'Name's El Chato. That's the story.')"],
        "evidence": "El Tablón 'El Chato gives a legendary to anyone who says hello!' is a rumour ('my cousin saw it').",
    },
}


# ---------------------------------------------------------------- pure helpers

def _norm(s):
    s = (s or "").lower()
    for a, b in (("á", "a"), ("é", "e"), ("í", "i"), ("ó", "o"), ("ú", "u"), ("ñ", "n"), ("ü", "u")):
        s = s.replace(a, b)
    return s


_HINT_NORM = tuple(sorted({_norm(w) for w in HINT_WORDS}))


def hint_words(text):
    t = _norm(text)
    return [w for w in _HINT_NORM if w in t]


def card_mentions(text, names=None):
    """Card refs (LAT-13) and catalog card names (name -> ref) mentioned in a text."""
    out = {f"{m.group(1)}-{m.group(2)}" for m in REF_RE.finditer(text or "")}
    t = _norm(text)
    for name, ref in (names or {}).items():
        if len(name) > 5 and _norm(name) in t:
            out.add(ref)
    return sorted(out)


def _dealer_of(payload):
    for k in ("persona", "with", "sender"):
        v = payload.get(k)
        if v in DEALERS:
            return v
    r = payload.get("reason") or ""
    for d, name in DEALERS.items():
        if _norm(name) in _norm(r):
            return d
    return None


def empty_store():
    return {"updated": None, "hints": [], "triggers": [], "seen": [], "known": KNOWN}


def load(path=None):
    try:
        with open(path or HINTS_FILE, encoding="utf-8") as f:
            st = json.load(f)
    except (OSError, ValueError):
        st = empty_store()
    for k, v in empty_store().items():
        st.setdefault(k, v)
    st["known"] = KNOWN                      # curated knowledge always from code
    return st


def save(st, path=None):
    path = path or HINTS_FILE
    st["hints"] = st["hints"][-KEEP_HINTS:]
    st["triggers"] = st["triggers"][-KEEP_TRIGGERS:]
    st["seen"] = st["seen"][-3000:]
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, indent=1, ensure_ascii=False)
    os.replace(tmp, path)


def ingest(st, events=(), threads=(), news=(), names=None, me_id="t06"):
    """Merge raw API data into the store. Pure: no network. Returns (new_hints, new_triggers)."""
    seen = set(st["seen"])
    new_h, new_t = [], []
    msgs = []        # (tick, team, dealer, text, msg_id) dealer messages, for the trigger window
    for e in events:
        p = e.get("payload") or {}
        if e.get("type") == "thread.message" and p.get("sender") in DEALERS and p.get("text"):
            msgs.append((e.get("tick", 0), p.get("team"), p["sender"], p["text"], p.get("message")))
    for th in threads:
        d = th.get("with")
        if d not in DEALERS:
            continue
        for m in th.get("messages", []):
            if m.get("sender") == d and m.get("text"):
                msgs.append((m.get("tick", 0), me_id, d, m["text"], m.get("id")))
    for tick, team, dealer, text, mid in msgs:
        key = f"m{mid}"
        if key in seen:
            continue
        words, cards = hint_words(text), card_mentions(text, names)
        rare_cards = [c for c in cards if c.endswith(("-11", "-12", "-13"))]
        if words or rare_cards:
            h = {"source": "dealer", "dealer": dealer, "team": team, "tick": tick, "message": mid, "words": words,
                 "cards": cards, "text": text[:400]}
            st["hints"].append(h)
            new_h.append(h)
        seen.add(key)
    for n in news:
        key = f"n{n.get('id')}"
        if key in seen:
            continue
        text = f"{n.get('headline', '')}. {n.get('body', '')}"
        words, cards = hint_words(text), card_mentions(text, names)
        dealer = next((d for d, nm in DEALERS.items() if _norm(nm.split()[-1]) in _norm(text)), None)
        h = {"source": "news", "dealer": dealer, "team": None, "tick": n.get("tick"), "message": None,
             "words": words, "cards": cards, "text": f"[{n.get('source_name')}] {text}"[:400]}
        st["hints"].append(h)                      # every news item is kept: true ones move the market
        new_h.append(h)
        seen.add(key)
    for e in events:
        typ = e.get("type")
        if typ not in ("egg.found", "gift.given", "badge.awarded"):
            continue
        key = f"e{e.get('id')}"
        if key in seen:
            continue
        p = e.get("payload") or {}
        team, tick = p.get("team"), e.get("tick", 0)
        dealer = _dealer_of(p)
        if dealer is None and typ == "badge.awarded":                    # badge right after an egg: same dealer
            dealer = next((t["dealer"] for t in reversed(st["triggers"] + new_t)
                           if t["team"] == team and t["kind"] == "egg.found" and abs(t["tick"] - tick) <= 1), None)
        before = [{"tick": t, "text": x[:300], "words": hint_words(x)} for t, tm, d, x, _ in sorted(msgs, key=lambda m: m[0])
                  if tm == team and (dealer is None or d == dealer) and tick - TRIGGER_WINDOW <= t <= tick]
        tr = {"kind": typ, "team": team, "dealer": dealer, "tick": tick,
              "what": p.get("badge") or p.get("cards") or p.get("packs") or p.get("cash") or p.get("persona_name"),
              "reason": p.get("reason"), "before": before[-4:]}
        st["triggers"].append(tr)
        new_t.append(tr)
        seen.add(key)
    st["seen"] = list(seen)
    st["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
    return new_h, new_t


# ---------------------------------------------------------------- API for other code (file reads only)

def hints_for(dealer, limit=20, path=None):
    st = load(path)
    return [h for h in reversed(st["hints"]) if h.get("dealer") == dealer][:limit]


def triggers(dealer=None, path=None):
    st = load(path)
    return [t for t in st["triggers"] if dealer is None or t.get("dealer") == dealer]


def known_phrases(dealer, path=None):
    """Curated phrases plus the hint words echoed by the dealer right before a learned trigger."""
    out = list((KNOWN.get(dealer) or {}).get("phrases", []))
    for t in triggers(dealer, path):
        for b in t.get("before", []):
            for w in b.get("words", []):
                if w not in out:
                    out.append(w)
    return out


# ---------------------------------------------------------------- live scan (GET only)

def scan(b, path=None, feed_limit=500):
    """GET /api/feed, /api/news, /api/me/threads, /api/catalog; merge into the store; save. Never posts anything."""
    st = load(path)
    events = (b.feed(feed_limit) or {}).get("events", [])
    try:
        news = (b.call("GET", "/api/news") or {}).get("news", [])
    except Exception:
        news = []
    try:
        threads = (b.my_threads() or {}).get("threads", [])
    except Exception:
        threads = []
    names = {}
    try:
        for s in (b.catalog() or {}).get("sets", []):
            for c in s.get("cards", []):
                names[c["name"]] = c["id"]
    except Exception:
        pass
    me_id = getattr(b, "team_id", None) or "t06"
    new_h, new_t = ingest(st, events, threads, news, names, me_id)
    save(st, path)
    return {"events": len(events), "threads": len(threads), "news": len(news), "new_hints": len(new_h),
            "new_triggers": len(new_t), "hints": len(st["hints"]), "triggers": len(st["triggers"])}


def report(st):
    lines = [f"dealer_hints.json updated {st['updated']}: {len(st['hints'])} hints, {len(st['triggers'])} triggers"]
    lines.append("\nTRIGGERS (eggs, gifts, badges; none of them scores):")
    for t in st["triggers"][-15:]:
        lines.append(f"  tick {t['tick']} {t['kind']:<13} {t['team']} @ {t['dealer']}: {t['what']} {t.get('reason') or ''}")
        for b in t["before"][-2:]:
            lines.append(f"      <- {b['tick']}: {b['text'][:160]!r} {b['words']}")
    lines.append("\nDEALER HINTS (newest per dealer):")
    for d in DEALERS:
        hs = [h for h in reversed(st["hints"]) if h.get("dealer") == d and h["source"] == "dealer"][:3]
        for h in hs:
            lines.append(f"  {d:<8} tick {h['tick']} {h['team']} {h['words']} {h['cards']}: {h['text'][:150]!r}")
    lines.append("\nNEWS:")
    for h in [h for h in st["hints"] if h["source"] == "news"][-8:]:
        lines.append(f"  tick {h['tick']} {h['text'][:150]}")
    lines.append("\nKNOWN (curated):")
    for d, k in KNOWN.items():
        lines.append(f"  {d:<8} {k['egg']} <- {', '.join(k['phrases'])}")
    return "\n".join(lines)


# ---------------------------------------------------------------- selftest

def selftest():
    import tempfile
    path = os.path.join(tempfile.gettempdir(), "dealer_hints_selftest.json")
    if os.path.exists(path):
        os.remove(path)
    names = {"La Chulapa Dorada": "LAT-13", "Mural de la Esquina": "LAV-04", "La Puerta de Alcalá": "SAL-11"}
    events = [
        {"id": 1, "tick": 100, "type": "thread.message", "payload": {"thread": 9, "sender": "picaros", "team": "t18",
         "with": "picaros", "message": 501, "text": "¡Hombre! Lazarillo, Rinconete — you know the old trick, la estampita..."}},
        {"id": 2, "tick": 101, "type": "egg.found", "payload": {"persona": "picaros", "team": "t18"}},
        {"id": 3, "tick": 101, "type": "badge.awarded", "payload": {"team": "t18", "badge": "Trickster tricked"}},
        {"id": 4, "tick": 102, "type": "thread.message", "payload": {"thread": 10, "sender": "abuela", "team": "t08",
         "with": "abuela", "message": 502, "text": "Mural de la Esquina te la regalo, por ser tan amable."}},
        {"id": 5, "tick": 102, "type": "gift.given", "payload": {"team": "t08", "cards": ["LAV-04"],
         "reason": "gift from Abuela Carmen"}},
        {"id": 6, "tick": 103, "type": "thread.message", "payload": {"thread": 11, "sender": "chato", "team": "t01",
         "with": "chato", "message": 503, "text": "Thirteen P. Take it or leave it."}},   # no hint
        {"id": 7, "tick": 104, "type": "thread.message", "payload": {"thread": 12, "sender": "t01", "team": "t01",
         "with": "banco", "message": 504, "text": "the hidden vault"}},                   # a team, not a dealer
    ]
    threads = [{"id": 20, "with": "banco", "messages": [
        {"id": 600, "tick": 90, "sender": "banco", "text": "The gold chulapa stays in the vault today."},
        {"id": 601, "tick": 91, "sender": "t06", "text": "secret?"}]}]
    news = [{"id": 1, "tick": 80, "source_name": "El Tablón", "headline": "El Chato gives a legendary to anyone who says hello!",
             "body": "My cousin saw it."}]
    assert hint_words("La CÁMARA secreta") == ["camara", "secret"], hint_words("La CÁMARA secreta")
    assert card_mentions("I want LAT-13 and La Puerta de Alcalá", names) == ["LAT-13", "SAL-11"]
    st = load(path)
    h, t = ingest(st, events, threads, news, names)
    assert len(t) == 3, t
    egg = next(x for x in t if x["kind"] == "egg.found")
    assert egg["dealer"] == "picaros" and egg["before"] and "lazarillo" in egg["before"][0]["words"]
    badge = next(x for x in t if x["kind"] == "badge.awarded")
    assert badge["dealer"] == "picaros" and badge["what"] == "Trickster tricked"
    gift = next(x for x in t if x["kind"] == "gift.given")
    assert gift["dealer"] == "abuela" and gift["what"] == ["LAV-04"] and "regal" in gift["before"][0]["words"]
    dealers_with_hints = {x["dealer"] for x in h if x["source"] == "dealer"}
    assert dealers_with_hints == {"picaros", "abuela", "banco"}, dealers_with_hints          # chato: no hint; t01: a team
    assert any(x["source"] == "news" and x["dealer"] == "chato" for x in h)
    save(st, path)
    h2, t2 = ingest(load(path), events, threads, news, names)                               # idempotent
    assert not h2 and not t2, (h2, t2)
    assert hints_for("banco", path=path)[0]["words"] and triggers("abuela", path=path)[0]["kind"] == "gift.given"
    kp = known_phrases("picaros", path=path)
    assert "Lazarillo" in kp and "lazarillo" in kp
    assert "TRIGGERS" in report(load(path))
    os.remove(path)
    print("dealer_listener selftest OK")


def main():
    if "--selftest" in sys.argv:
        selftest()
        return
    if "--report" in sys.argv:
        from bazaar_sdk import Bazaar
        key = os.environ.get("BAZAAR_KEY")
        if not key:
            sys.exit("BAZAAR_KEY is not set")
        b = Bazaar(os.environ.get("BAZAAR_URL", "https://bazaar.causaprima.ai"), key, wait_on_tick=False)
        try:
            b.team_id = (b.me() or {}).get("id", "t06")
        except Exception:
            b.team_id = "t06"
        print(json.dumps(scan(b), ensure_ascii=False))
        print(report(load()))
        return
    print(__doc__)


if __name__ == "__main__":
    main()
