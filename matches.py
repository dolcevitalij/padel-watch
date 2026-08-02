"""
Play!Match - offene Partien, denen noch Spieler fehlen.

Quelle: https://kunden.hallofpadel.com/Matches/Search.aspx
Die Seite ist serverseitig gerendert, es braucht also weder den `key` noch
einen XHR-Endpunkt - ein GET genuegt. Gelistet werden ausschliesslich Partien
mit freien Plaetzen (kein Eintrag hat 4/4 Spieler).

Bewusst nur lesend: gemeldet wird mit Link, beigetreten wird von Hand.
"""
from __future__ import annotations
import datetime as dt
import re

from fetch import BASE, HEADERS

import requests

SEARCH_URL = f"{BASE}/Matches/Search.aspx"
SPIELER_PRO_MATCH = 4
WOCHENTAGE = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]

MONATE = {"jan": 1, "feb": 2, "mär": 3, "mar": 3, "apr": 4, "mai": 5, "jun": 6,
          "jul": 7, "aug": 8, "sep": 9, "okt": 10, "nov": 11, "dez": 12}


def match_url(match_id: str) -> str:
    return f"{BASE}/Matches/Match.aspx?id={match_id}"


def _zahl(text: str | None) -> float | None:
    """'3,00' -> 3.0"""
    if not text:
        return None
    try:
        return float(text.strip().replace(",", "."))
    except ValueError:
        return None


def _feld(block: str, idx: int, name: str) -> str | None:
    """Wert eines Labels der Kachel, z.B. LabelHoraInicio."""
    m = re.search(rf'id="[^"]*WUCRegistroPartida_{idx}_{name}_{idx}"[^>]*>'
                  r'([^<]*)', block)
    return m.group(1).strip() if m and m.group(1).strip() else None


def _hidden(block: str, idx: int, name: str) -> str | None:
    m = re.search(rf'id="[^"]*WUCRegistroPartida_{idx}_HiddenField{name}_{idx}"'
                  r'[^>]*value="([^"]*)"', block)
    return m.group(1).strip() if m else None


def parse_matches(html: str) -> list[dict]:
    """
    Search.aspx -> Liste offener Partien.

    Die Kacheln werden am Wiederholungs-Index `WUCRegistroPartida_<N>_`
    geschnitten, nicht an einer CSS-Klasse: die versteckten Niveau-Felder
    liegen im Markup vor dem Klassenmarker und wuerden sonst der falschen
    Kachel zugeordnet.
    """
    # Startposition jeder Kachel bestimmen
    pos: dict[int, int] = {}
    for m in re.finditer(r"WUCRegistroPartida_(\d+)_", html):
        n = int(m.group(1))
        if n not in pos:
            pos[n] = m.start()
    if not pos:
        return []

    grenzen = sorted(pos.items())
    matches: list[dict] = []
    for i, (idx, start) in enumerate(grenzen):
        ende = grenzen[i + 1][1] if i + 1 < len(grenzen) else len(html)
        block = html[start:ende]

        tag = _feld(block, idx, "LabelFechaInicio")
        zeit = _feld(block, idx, "LabelHoraInicio")
        if not tag or not zeit:
            continue

        # Monat und Jahr stehen als Klartext im Kalender-Kaestchen
        mon = re.search(r">\s*([A-Za-zÄÖÜäöü]{3})\.?\s*<", block)
        jahr = re.search(r">\s*(20\d\d)\s*<", block)
        monat = MONATE.get(mon.group(1).lower()[:3]) if mon else None
        if not monat or not jahr:
            continue
        try:
            beginn = dt.datetime(int(jahr.group(1)), monat, int(tag),
                                 *map(int, zeit.split(":")))
        except ValueError:
            continue

        mid = re.search(r"Match\.aspx\?id=([0-9a-f]{32})", block)
        # Es gibt immer vier Teilnehmer-Anker; nur die BESETZTEN tragen die
        # Klasse fotoParticipante. Alle Anker zu zaehlen ergaebe stets 4/4.
        spieler = len(re.findall(r'class="fotoParticipante"', block))
        sexo = (_feld(block, idx, "LabelSexoValor") or "").lstrip("- ").strip()

        matches.append({
            "id": mid.group(1) if mid else None,
            "url": match_url(mid.group(1)) if mid else SEARCH_URL,
            "beginn": beginn,
            "wochentag": _feld(block, idx, "LabelDiaSemana"),
            "court": _feld(block, idx, "LabelRecurso"),
            "club": _feld(block, idx, "LabelNombreCentro"),
            "sportart": _feld(block, idx, "LabelDeporte"),
            "art": _feld(block, idx, "LabelTextoPartida"),
            "niveau_text": _feld(block, idx, "LabelNivelValor"),
            "niveau_von": _zahl(_hidden(block, idx, "NivelDesde")),
            "niveau_bis": _zahl(_hidden(block, idx, "NivelHasta")),
            "alle_niveaus": _hidden(block, idx, "TodosLosNiveles") == "True",
            "geschlecht": sexo or None,
            "spieler": spieler,
            "frei": max(0, SPIELER_PRO_MATCH - spieler),
        })
    return matches


def fetch_matches(session: tuple | None = None, timeout: int = 25) -> list[dict]:
    """Search.aspx laden und auswerten. `session` = Rueckgabe von open_session()."""
    if session:
        sess = session[0]
    else:
        sess = requests.Session()
        sess.headers.update(HEADERS)
    r = sess.get(SEARCH_URL, timeout=timeout)
    r.raise_for_status()
    return parse_matches(r.text)


def _minuten(hhmm: str | None) -> int | None:
    """'18:30' -> 1110. Leer/ungueltig -> None (= kein Filter)."""
    if not hhmm or ":" not in str(hhmm):
        return None
    try:
        h, m = str(hhmm).split(":")[:2]
        return int(h) * 60 + int(m)
    except ValueError:
        return None


def _im_zeitfenster(beginn: dt.datetime, von: int | None, bis: int | None) -> bool:
    """
    Liegt die Anfangszeit im Tagesfenster? Ist nur eine Grenze gesetzt, wirkt
    auch nur diese. Ein Fenster ueber Mitternacht (22:00-02:00) wird als
    Umschlag behandelt, weil die Anlage 24/7 offen hat.
    """
    if von is None and bis is None:
        return True
    t = beginn.hour * 60 + beginn.minute
    if von is not None and bis is not None:
        return von <= t <= bis if von <= bis else (t >= von or t <= bis)
    if von is not None:
        return t >= von
    return t <= bis


def filter_matches(matches: list[dict], rule: dict,
                   now: dt.datetime | None = None) -> list[dict]:
    """
    Regel vom Typ Play!Match anwenden.

    Felder der Regel:
      stunden_voraus  nur Partien, die innerhalb dieser Frist beginnen (Default 24)
      zeit_von/bis    Tageszeit-Fenster der Anfangszeit ('HH:MM'), leer = egal;
                      von > bis gilt als Fenster ueber Mitternacht
      weekdays        Wochentage ('Mo'...'So'), leer = alle
      niveau_min      Untergrenze der Partie muss mindestens so hoch sein;
                      die Obergrenze wird bewusst ignoriert
      min_frei        mindestens so viele freie Plaetze (Default 1)
      clubs           Liste von Clubnamen, leer = alle
      courts          Liste von Courtnamen, leer = alle
      sportart        z.B. 'Padel', leer = alle
    """
    now = now or dt.datetime.now()
    frist = now + dt.timedelta(hours=float(rule.get("stunden_voraus", 24)))
    niveau_min = float(rule.get("niveau_min", 0) or 0)
    min_frei = int(rule.get("min_frei", 1) or 1)
    clubs = [c.strip() for c in (rule.get("clubs") or []) if c.strip()]
    courts = [c.strip() for c in (rule.get("courts") or []) if c.strip()]
    sportart = (rule.get("sportart") or "").strip()
    von, bis = _minuten(rule.get("zeit_von")), _minuten(rule.get("zeit_bis"))
    tage = [d.strip() for d in (rule.get("weekdays") or []) if d.strip()]

    treffer = []
    for m in matches:
        if not (now <= m["beginn"] <= frist):
            continue
        if not _im_zeitfenster(m["beginn"], von, bis):
            continue
        if tage and WOCHENTAGE[m["beginn"].weekday()] not in tage:
            continue
        if m["frei"] < min_frei:
            continue
        # Untergrenze der Partie: filtert zu schwache Runden weg
        if niveau_min and (m["niveau_von"] is None or m["niveau_von"] < niveau_min):
            continue
        if clubs and (m["club"] or "") not in clubs:
            continue
        if courts and (m["court"] or "") not in courts:
            continue
        if sportart and (m["sportart"] or "") != sportart:
            continue
        treffer.append(m)
    return sorted(treffer, key=lambda m: m["beginn"])


def build_match_message(m: dict, regel: str = "Play!Match") -> str:
    """Telegram-Nachricht fuer eine offene Partie (HTML, wie send_telegram erwartet)."""
    wt = m.get("wochentag") or m["beginn"].strftime("%A")
    frei = m["frei"]
    wer = "1 Spieler" if frei == 1 else f"{frei} Spieler"
    ort = " · ".join(x for x in (m.get("court"), m.get("club")) if x)
    niveau = m.get("niveau_text") or "-"
    extra = " · ".join(x for x in (m.get("geschlecht"), m.get("art")) if x)

    return (
        "ALAAARRRM ALAAARRRM\n"
        f"🎾 <b>Play!Match sucht {wer}</b>\n"
        f"📅 {wt} {m['beginn']:%d.%m.%Y} · <b>{m['beginn']:%H:%M}</b>\n"
        f"📍 {ort}\n"
        f"📊 Niveau {niveau}{' · ' + extra if extra else ''}\n"
        f"📋 Regel: {regel}\n"
        f"🔗 <a href=\"{m['url']}\">Partie ansehen</a>"
    )
