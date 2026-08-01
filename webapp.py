#!/usr/bin/env python3
"""
Padel-Watch  -  lokale Test- & Konfigurationsumgebung (Web-UI).

Starten:   python webapp.py
Dann im Browser:  http://localhost:5000

Zweck (reines Dev-Tool, laeuft nur lokal):
  - config.yaml bequem im Browser bearbeiten und speichern
  - Verbindung + key-Extraktion testen
  - Regeln als Vorschau gegen echte Daten pruefen (Timeline je Court),
    OHNE Telegram-Versand
  - optional eine Telegram-Testnachricht senden

Umgebungsvariablen (optional, nur fuer die jeweilige Funktion):
  KEY_OVERRIDE         fester key, falls Auto-Extraktion scheitert
  TELEGRAM_BOT_TOKEN   fuer die Telegram-Testnachricht
  TELEGRAM_CHAT_ID
"""
from __future__ import annotations
import os
import datetime as dt

import yaml
from flask import Flask, request, jsonify, render_template

from core import parse_courts, find_matches, merge_intervals
from fetch import fetch_grid, open_session
# dieselbe Formatierung, Link-Ermittlung und Telegram-Anbindung wie im Produktivlauf
from padel_watch import (apply_chat_override, build_messages, order_slots,
                         resolve_booking_links, scrub, send_telegram,
                         target_chat_id)

app = Flask(__name__)
# neben webapp.py, nicht relativ zum Arbeitsverzeichnis - damit die UI auch
# startet, wenn sie aus einem anderen Ordner heraus aufgerufen wird
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.yaml")
ENV_PATH = os.path.join(BASE_DIR, ".env")


def load_env(path: str = ENV_PATH) -> None:
    """
    Minimaler .env-Loader (kein python-dotenv noetig): je Zeile KEY=WERT,
    '#' ist Kommentar. Schon gesetzte Umgebungsvariablen gewinnen, man kann also
    beim Start weiterhin ueberschreiben.

    Zweck: TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID / KEY_OVERRIDE muessen nicht
    vor jedem Start in die Shell exportiert werden. Die Datei steht in
    .gitignore - Tokens gehoeren nicht ins Repo, im Produktivlauf kommen sie
    aus den GitHub Secrets.
    """
    if not os.path.exists(path):
        return
    # utf-8-sig: schluckt eine BOM, die PowerShell und Notepad beim Speichern
    # voranstellen - sonst heisst der erste Schluessel '﻿TELEGRAM_BOT_TOKEN'
    # und wird stillschweigend ignoriert.
    with open(path, encoding="utf-8-sig") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


load_env()

# Bekannte Courts (aus den Live-Daten). Wird per "Verbindung testen" aktualisiert.
KNOWN_COURTS = [
    {"id": "16", "name": "BS ZILLMANN"},
    {"id": "17", "name": "BS HAUS DER FINANZEN"},
    {"id": "13", "name": "BS Platz 1"},
    {"id": "14", "name": "BS Platz 2"},
    {"id": "15", "name": "BS KOSATEC"},
]


def load_cfg() -> dict:
    return yaml.safe_load(open(CONFIG_PATH))


def save_cfg(c: dict) -> None:
    yaml.safe_dump(c, open(CONFIG_PATH, "w"), allow_unicode=True, sort_keys=False)


def fmt_fecha(d: dt.date) -> str:
    return f"{d.day}/{d.month}/{d.year}"


def complement(occ: list[tuple[int, int]], lo: int, hi: int) -> list[list[int]]:
    """Freie Luecken (Komplement der Belegung) im Fenster [lo, hi]."""
    gaps, cursor = [], lo
    for s, e in merge_intervals(occ):
        s, e = max(s, lo), min(e, hi)
        if s > cursor:
            gaps.append([cursor, s])
        cursor = max(cursor, e)
    if cursor < hi:
        gaps.append([cursor, hi])
    return gaps


def merge_hits(matches: list[dict]) -> list[list[int]]:
    """
    Aufeinanderfolgende Treffer (Abstand == Raster) zu einem buchbaren Fenster
    [erster Start, letztes Ende] zusammenfassen - numerische Variante von
    padel_watch.merge_starts, fuer die Balken der Timeline.
    """
    out: list[list[int]] = []
    last_start = None
    for m in sorted(matches, key=lambda x: x["start_min"]):
        s, e = m["start_min"], m["start_min"] + m["dur"]
        if out and last_start is not None and s - last_start == m["step"]:
            out[-1][1] = e
        else:
            out.append([s, e])
        last_start = s
    return out


def build_preview(payload: dict, cfg: dict, body: dict,
                  session: tuple | None = None) -> dict:
    """
    Auswertung der Antwort. Netzwerkfrei - ausser wenn body["links"] gesetzt ist,
    dann wird pro Slot ein Buchungs-Token geholt (ein Request je Slot).
    """
    from core import hhmm_to_min
    courts, meta = parse_courts(payload)
    court_ids = body.get("courts") or None
    lo = hhmm_to_min(cfg["day_open"])
    hi = hhmm_to_min(cfg["day_close"])
    is_today = body["date"] == dt.date.today().isoformat()

    matches = find_matches(
        payload,
        court_ids=court_ids,
        win_start_hhmm=body["time_from"],
        win_end_hhmm=body["time_to"],
        duration_min=int(body["duration"]),
        day_open_hhmm=cfg["day_open"],
        day_close_hhmm=cfg["day_close"],
        is_today=is_today,
    )
    by_court: dict[str, list[dict]] = {}
    for m in matches:
        by_court.setdefault(m["court_id"], []).append(m)

    result = []
    for c in courts:
        if court_ids and c.court_id not in court_ids:
            continue
        mine = by_court.get(c.court_id, [])
        result.append({
            "id": c.court_id,
            "name": c.name,
            "occupied": [[s, e] for s, e in c.occupied],
            "free": complement(c.occupied, lo, hi),
            "matches": sorted([m["start"] for m in mine]),
            "hits": merge_hits(mine),          # buchbare Fenster, zusammengefasst
        })

    # Nachrichten genau so aufbauen, wie sie der Produktivlauf schicken wuerde:
    # eine pro Slot. Unterschied: hier gilt ALLES als neu (kein state.json-Diff).
    day = dt.date.fromisoformat(body["date"])
    rule_name = body.get("rule") or "Vorschau"
    limit = int(cfg.get("max_messages", 10))
    slots = order_slots([{**m, "date": day, "rule": rule_name} for m in matches])
    if slots and body.get("links"):
        resolve_booking_links(slots[:limit], cfg["id_cuadro"], session=session)
    messages = build_messages(slots, cfg["id_cuadro"], limit) if slots else []

    return {
        "ok": True,
        "date": body["date"],
        "day_open": lo, "day_close": hi,
        "step": 60 // meta["partes_por_hora"],
        "duration": int(body["duration"]),
        "win": [hhmm_to_min(body["time_from"]), hhmm_to_min(body["time_to"])],
        "courts": result,
        "count": len(matches),
        "messages": messages,
        "max_messages": int(cfg.get("max_messages", 10)),
    }


# ------------------------------------------------------------------ #
#  Routes
# ------------------------------------------------------------------ #
@app.get("/")
def index():
    return render_template("index.html")


@app.get("/api/config")
def get_config():
    return jsonify(load_cfg())


@app.post("/api/config")
def set_config():
    save_cfg(request.get_json())
    return jsonify({"ok": True})


@app.get("/api/courts")
def get_courts():
    return jsonify(KNOWN_COURTS)


@app.post("/api/test-connection")
def test_connection():
    cfg = load_cfg()
    d = dt.date.today() + dt.timedelta(days=3)
    try:
        data = fetch_grid(cfg["id_cuadro"], fmt_fecha(d),
                          os.environ.get("KEY_OVERRIDE") or None)
        cols = data["d"]["Columnas"]
        return jsonify({
            "ok": True, "date": fmt_fecha(d),
            "courts": [{"id": c["Id"], "name": c["TextoPrincipal"],
                        "count": len(c.get("Ocupaciones", []))} for c in cols],
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.post("/api/preview")
def preview():
    body = request.get_json()
    cfg = load_cfg()
    d = dt.date.fromisoformat(body["date"])
    try:
        # eine Session fuer Abruf und (optional) Buchungs-Links
        sess = open_session(cfg["id_cuadro"], os.environ.get("KEY_OVERRIDE") or None)
        data = fetch_grid(cfg["id_cuadro"], fmt_fecha(d), session=sess)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})
    return jsonify(build_preview(data, cfg, body, session=sess))


@app.post("/api/test-telegram")
def test_telegram():
    if not os.environ.get("TELEGRAM_BOT_TOKEN") or not os.environ.get("TELEGRAM_CHAT_ID"):
        return jsonify({"ok": False,
                        "error": "TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID nicht gesetzt."})
    # send_telegram aus dem Produktivlauf: gleiche Fehlerbehandlung, gleiche
    # Behandlung migrierter Gruppen-Ids, Token wird aus Meldungen gefiltert
    apply_chat_override()
    try:
        send_telegram("✅ Padel-Watch Test – Verbindung funktioniert.")
        return jsonify({"ok": True, "chat_id": target_chat_id()})
    except Exception as e:
        return jsonify({"ok": False, "error": scrub(e)})


if __name__ == "__main__":
    app.run(debug=True, port=5000)
