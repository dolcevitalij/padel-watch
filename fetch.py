"""
Abruf der Belegungsdaten vom Matchpoint-Endpunkt.

Ablauf wie ein echter Browser:
  1. GET  Booking/Grid.aspx?id=<idCuadro>   -> setzt Cookies, enthaelt den `key`
  2. key aus dem HTML extrahieren
  3. POST booking/srvc.aspx/ObtenerCuadro   -> JSON mit den Belegungen

Der `key` wird pro Seitenaufruf frisch aus der Seite gezogen, damit es auch
funktioniert, falls der Token an die Session gebunden ist oder rotiert.
"""
from __future__ import annotations
import re
import requests

BASE = "https://kunden.hallofpadel.com"

# Reihenfolge = Prioritaet. Der erste passende Treffer wird genommen.
# Falls keiner greift: die Grid-Seite im Browser als Quelltext oeffnen,
# nach dem key-Wert suchen und hier das passende Muster ergaenzen.
#
# Verifiziert am 31.07.2026: der Token steht in einem Inline-<script> der
# Grid-Seite als JS-Zuweisung mit obfuskiertem Variablennamen, z.B.
#     hl90njda2b89k='eNEe29kX...JA==';
# Das Wort "key" kommt im Grid-HTML gar nicht vor - benutzt wird die Variable
# erst in booking/js/ajax.js (Funktion obtenerCuadro, Feld "key").
KEY_PATTERNS = [
    # 1. bekannter Variablenname (hoechste Prioritaet)
    re.compile(r'hl90njda2b89k\s*=\s*["\']([A-Za-z0-9+/=]{16,})["\']'),
    # 2. generischer Fallback, falls die Variable umbenannt wird: kurzer
    #    obfuskierter Bezeichner am Statement-Anfang, Wert = laengerer
    #    Base64-String mit '='-Padding.
    re.compile(r'(?m)(?:^|[;{]\s*)(?:[A-Za-z][A-Za-z0-9_]{6,24})\s*=\s*'
               r'["\']([A-Za-z0-9+/]{40,}={1,2})["\']'),
    # 3. bisherige Muster als weitere Fallbacks
    re.compile(r'["\']?key["\']?\s*[:=]\s*["\']([A-Za-z0-9+/=]{16,})["\']'),
    re.compile(r'name=["\']key["\'][^>]*value=["\']([A-Za-z0-9+/=]{16,})["\']'),
    re.compile(r'value=["\']([A-Za-z0-9+/=]{16,})["\'][^>]*name=["\']key["\']'),
    re.compile(r'ObtenerCuadro["\'][^)]*["\']([A-Za-z0-9+/=]{24,})["\']'),
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36",
    "Accept-Language": "de-DE,de;q=0.9,en;q=0.7",
}


def extract_key(html: str) -> str | None:
    for pat in KEY_PATTERNS:
        m = pat.search(html)
        if m:
            return m.group(1)
    return None


def open_session(id_cuadro: str, key_override: str | None = None,
                 timeout: int = 20) -> tuple:
    """
    Einmal Grid.aspx laden: setzt die Cookies und liefert den key.
    Gibt (session, key, grid_url) zurueck - wiederverwendbar fuer mehrere
    Abrufe, damit ein Lauf nicht pro Request eine neue Session aufbaut.
    """
    sess = requests.Session()
    sess.headers.update(HEADERS)

    grid_url = f"{BASE}/Booking/Grid.aspx?id={id_cuadro}"
    r = sess.get(grid_url, timeout=timeout)
    r.raise_for_status()

    key = key_override or extract_key(r.text)
    if not key:
        raise RuntimeError(
            "key konnte nicht aus Grid.aspx extrahiert werden. "
            "Der Token steht als JS-Variable in einem Inline-<script> der "
            "Grid-Seite (nicht unter dem Namen 'key'); den aktuellen "
            "Variablennamen in booking/js/ajax.js nachsehen (Funktion "
            "obtenerCuadro, Feld 'key') und KEY_PATTERNS ergaenzen. "
            "Notfall: KEY_OVERRIDE setzen."
        )
    return sess, key, grid_url


def _post(sess, path: str, body: dict, grid_url: str, timeout: int = 20) -> dict:
    """POST auf einen srvc.aspx-Endpunkt, Header wie im Browser."""
    r = sess.post(
        f"{BASE}{path}",
        json=body,
        headers={
            "Content-Type": "application/json; charset=UTF-8",
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": grid_url,
            "Origin": BASE,
        },
        timeout=timeout,
    )
    r.raise_for_status()
    return r.json()


def _as_id(value):
    """idCuadro schickt der Browser als Zahl - so verhaelt sich der Server erwartbar."""
    s = str(value)
    return int(s) if s.isdigit() else value


def fetch_grid(id_cuadro: str, fecha: str, key_override: str | None = None,
               timeout: int = 20, session: tuple | None = None) -> dict:
    """
    fecha im Format 'T/M/JJJJ' (ohne fuehrende Nullen), z.B. '28/7/2026'.
    Gibt das geparste JSON-dict der Response zurueck.
    `session` = Rueckgabe von open_session(), um Cookies/key wiederzuverwenden.
    """
    sess, key, grid_url = session or open_session(id_cuadro, key_override, timeout)
    return _post(sess, "/booking/srvc.aspx/ObtenerCuadro",
                 {"idCuadro": str(id_cuadro), "fecha": fecha, "key": key},
                 grid_url, timeout)


def fetch_slot_options(id_cuadro: str, court_id: str, modalidad, fecha: str,
                       hora: str, key_override: str | None = None,
                       timeout: int = 20, session: tuple | None = None) -> list[dict]:
    """
    Buchungs-Optionen fuer EINEN freien Slot: je Spieldauer ein Eintrag
    {"Token": "...", "Descripcion": "90min Online"}.

    Aus dem Token wird der direkte Buchungs-Link
    `booking/info.aspx?token=...&return_url=...` gebaut - genau der Weg, den die
    Seite beim Klick auf eine freie Zelle geht (clickBotonPista in ajax.js).
    Parameter wie in ajaxObtenerInformacionHuecoLibre.
    """
    sess, key, grid_url = session or open_session(id_cuadro, key_override, timeout)
    data = _post(sess, "/booking/srvc.aspx/ObtenerInformacionHuecoLibre",
                 {"idCuadro": _as_id(id_cuadro), "idRecurso": str(court_id),
                  "idmodalidad": modalidad, "fecha": fecha, "hora": hora,
                  "key": key},
                 grid_url, timeout)
    return data.get("d", {}).get("Opciones") or []
