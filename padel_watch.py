#!/usr/bin/env python3
"""
Padel-Watch  -  benachrichtigt ueber neu freigewordene Padelplaetze.

Laeuft als Cron-Job (z.B. GitHub Actions). Pro Lauf:
  1. fuer jeden relevanten Tag die Belegung abrufen
  2. freie Bloecke gemaess Regeln finden
  3. gegen den letzten Zustand diffen (nur NEUE Slots melden)
  4. Treffer per Telegram schicken

Benoetigte Umgebungsvariablen (als GitHub Secrets):
  TELEGRAM_BOT_TOKEN   Token vom @BotFather
  TELEGRAM_CHAT_ID     eigene Chat-ID
  KEY_OVERRIDE         (optional) fester key, falls Auto-Extraktion scheitert
"""
from __future__ import annotations
import os
import re
import sys
import json
import time
import datetime as dt

import yaml
import requests

from fetch import BASE, fetch_grid, fetch_slot_options, open_session
from core import find_matches

# Pfade relativ zu dieser Datei, nicht zum Arbeitsverzeichnis - damit auch die
# Web-UI build_message() importieren kann, ohne im Projektordner zu stehen.
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "config.yaml")
STATE_FILE = os.path.join(BASE_DIR, "state.json")
WEEKDAYS = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]


# ------------------------------------------------------------------ #
#  Telegram
# ------------------------------------------------------------------ #
def send_telegram(text: str) -> None:
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    resp = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": text,
              "parse_mode": "HTML", "disable_web_page_preview": True},
        timeout=20,
    )
    resp.raise_for_status()


# ------------------------------------------------------------------ #
#  Zustandsspeicher (Diff, damit nicht jeder Lauf denselben Slot meldet)
# ------------------------------------------------------------------ #
def load_state() -> dict:
    try:
        return json.load(open(STATE_FILE))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state: dict) -> None:
    json.dump(state, open(STATE_FILE, "w"), indent=0, sort_keys=True)


# ------------------------------------------------------------------ #
#  Hilfsfunktionen
# ------------------------------------------------------------------ #
def fmt_fecha(d: dt.date) -> str:
    """date -> 'T/M/JJJJ' ohne fuehrende Nullen (Matchpoint-Format)."""
    return f"{d.day}/{d.month}/{d.year}"


def target_dates(cfg: dict) -> list[dt.date]:
    today = dt.date.today()
    return [today + dt.timedelta(days=i) for i in range(cfg["days_ahead"] + 1)]


def rules_for_weekday(cfg: dict, d: dt.date) -> list[dict]:
    """
    Regeln, die an diesem Wochentag greifen. Pausierte Regeln (enabled: false)
    bleiben in config.yaml erhalten, melden aber nichts - fehlt das Feld, gilt
    die Regel als aktiv, damit aeltere Konfigurationen unveraendert laufen.
    """
    wd = WEEKDAYS[d.weekday()]
    return [r for r in cfg["rules"]
            if wd in r["weekdays"] and r.get("enabled", True)]


# ------------------------------------------------------------------ #
#  Hauptlauf
# ------------------------------------------------------------------ #
def run() -> int:
    cfg = yaml.safe_load(open(CONFIG_FILE))
    key_override = os.environ.get("KEY_OVERRIDE") or None

    old_state = load_state()
    new_state: dict[str, list[str]] = {}
    fresh: list[dict] = []          # neu aufgetauchte Treffer
    today = dt.date.today()

    # Eine Session fuer den ganzen Lauf: Grid.aspx nur einmal laden statt pro Tag
    try:
        sess = open_session(cfg["id_cuadro"], key_override)
    except Exception as e:
        msg = f"⚠️ Padel-Watch: Verbindungsaufbau fehlgeschlagen: {e}"
        print(msg, file=sys.stderr)
        if cfg.get("notify_on_error"):
            try:
                send_telegram(msg)
            except Exception:
                pass
        return 1

    for d in target_dates(cfg):
        rules = rules_for_weekday(cfg, d)
        if not rules:
            continue

        fecha = fmt_fecha(d)
        try:
            payload = fetch_grid(cfg["id_cuadro"], fecha, key_override, session=sess)
        except Exception as e:
            msg = f"⚠️ Padel-Watch: Abruf fuer {fecha} fehlgeschlagen: {e}"
            print(msg, file=sys.stderr)
            if cfg.get("notify_on_error"):
                try:
                    send_telegram(msg)
                except Exception:
                    pass
            return 1        # abbrechen: Diff nicht auf Basis kaputter Daten schreiben

        day_key = d.isoformat()
        seen_today: list[str] = []
        fresh_today: set[str] = set()   # gegen Doppel-Meldung bei ueberlappenden Regeln

        for r in rules:
            matches = find_matches(
                payload,
                court_ids=r["courts"] or None,
                win_start_hhmm=r["time_from"],
                win_end_hhmm=r["time_to"],
                duration_min=r["duration"],
                day_open_hhmm=cfg["day_open"],
                day_close_hhmm=cfg["day_close"],
                is_today=(d == today),
            )
            for m in matches:
                slot_id = f"{m['court_id']}@{m['start']}"
                if slot_id not in seen_today:
                    seen_today.append(slot_id)
                # NEU = war im letzten Lauf fuer diesen Tag nicht vorhanden
                if (slot_id not in old_state.get(day_key, [])
                        and slot_id not in fresh_today):
                    fresh_today.add(slot_id)
                    fresh.append({**m, "date": d, "rule": r["name"]})

        if seen_today:
            new_state[day_key] = sorted(seen_today)

        time.sleep(1.0)     # fair gegenueber dem Club-Server

    save_state(new_state)

    if fresh:
        limit = int(cfg.get("max_messages", 10))
        ordered = order_slots(fresh)
        # Buchungs-Links nur fuer die Slots, die eine Einzelnachricht bekommen
        resolve_booking_links(ordered[:limit], cfg["id_cuadro"], session=sess)
        msgs = build_messages(ordered, cfg["id_cuadro"], limit)
        for i, text in enumerate(msgs):
            if i:
                time.sleep(0.4)     # Telegram nicht mit einem Burst bewerfen
            send_telegram(text)
        print(f"{len(fresh)} neue freie Slots in {len(msgs)} Nachricht(en) gemeldet.")
    else:
        print("Keine neuen freien Slots.")
    return 0


def _hhmm(x: int) -> str:
    return f"{x // 60:02d}:{x % 60:02d}"


def merge_starts(slots: list[dict]) -> list[str]:
    """
    Aufeinanderfolgende buchbare Startzeiten (Abstand == Raster) zu einem
    freien Fenster zusammenfassen: [erster Start .. letzter Start + Dauer].
    """
    slots = sorted(slots, key=lambda s: s["start_min"])
    out, run = [], []
    for s in slots:
        if run and s["start_min"] - run[-1]["start_min"] == run[-1]["step"]:
            run.append(s)
        else:
            if run:
                out.append(run)
            run = [s]
    if run:
        out.append(run)
    return [f"{_hhmm(r[0]['start_min'])}\u2013{_hhmm(r[-1]['start_min'] + r[-1]['dur'])}"
            for r in out]


def booking_url(id_cuadro: str) -> str:
    """Der Buchungsplan als Ganzes - Fallback, wenn kein Slot-Token vorliegt."""
    return f"{BASE}/Booking/Grid.aspx?id={id_cuadro}"


def slot_url(token: str, id_cuadro: str) -> str:
    """
    Direkter Link auf EINEN Slot - genau die URL, die die Seite beim Klick auf eine
    freie Zelle aufruft (clickBotonPista in booking/js/ajax.js). Ohne Login leitet
    sie auf Login.aspx weiter und traegt den Token in return_url mit, man landet
    also nach dem Anmelden wieder auf diesem Slot. return_url wird wie im
    Original-JS nicht encodiert.
    """
    return f"{BASE}/booking/info.aspx?token={token}&return_url={booking_url(id_cuadro)}"


def pick_slot_token(opciones: list[dict], duration_min: int) -> str | None:
    """Token der Option, deren Dauer zur Regel passt ('90min Online' -> 90)."""
    for o in opciones:
        m = re.search(r"(\d+)\s*min", o.get("Descripcion", ""), re.I)
        if m and int(m.group(1)) == duration_min:
            return o.get("Token")
    return None


def resolve_booking_links(slots: list[dict], id_cuadro: str,
                          session: tuple | None = None, pause: float = 0.5) -> None:
    """
    Holt fuer jeden Slot einen echten Buchungs-Link (ein Request pro Slot) und
    haengt ihn als `book_url` an. Scheitert das, bleibt `book_url` None und die
    Nachricht verlinkt den Plan - der Lauf bricht deswegen nicht ab.
    """
    for i, m in enumerate(slots):
        if i:
            time.sleep(pause)       # fair gegenueber dem Club-Server
        try:
            opts = fetch_slot_options(id_cuadro, m["court_id"], m.get("modalidad"),
                                      fmt_fecha(m["date"]), m["start"],
                                      session=session)
            token = pick_slot_token(opts, m["dur"])
            m["book_url"] = slot_url(token, id_cuadro) if token else None
            if not token:
                print(f"Keine {m['dur']}-Min-Option fuer {m['court_name']} "
                      f"{m['start']}: {[o.get('Descripcion') for o in opts]}",
                      file=sys.stderr)
        except Exception as e:
            print(f"Buchungs-Link fuer {m['court_name']} {m['start']} "
                  f"nicht ermittelbar: {e}", file=sys.stderr)
            m["book_url"] = None


def build_slot_message(m: dict, id_cuadro: str) -> str:
    """Eine Nachricht fuer genau EINEN buchbaren Slot."""
    d = m["date"]
    end = _hhmm(m["start_min"] + m["dur"])
    if m.get("book_url"):
        link = f"🔗 <a href=\"{m['book_url']}\">Diesen Slot buchen</a>"
    else:
        link = (f"🔗 <a href=\"{booking_url(id_cuadro)}\">Buchungsplan öffnen</a> "
                f"— dort das Datum {d.strftime('%d.%m.')} im Kalender wählen")
    return (
        f"ALAAARRRM ALAAARRRM\n"
        f"🎾 <b>{m['court_name']}</b> frei\n"
        f"📅 {WEEKDAYS[d.weekday()]} {d.strftime('%d.%m.%Y')} · "
        f"<b>{m['start']}–{end}</b> ({m['dur']} Min)\n"
        f"📋 Regel: {m['rule']}\n"
        f"{link}"
    )


def order_slots(fresh: list[dict]) -> list[dict]:
    """Chronologisch, damit Nachrichten und Link-Abruf dieselbe Reihenfolge haben."""
    return sorted(fresh, key=lambda m: (m["date"], m["start_min"], m["court_name"]))


def build_messages(fresh: list[dict], id_cuadro: str, limit: int) -> list[str]:
    """
    Alle Nachrichten eines Laufs: eine pro Slot, chronologisch.
    Ueber `limit` hinaus wird zusammengefasst statt geflutet - wichtig beim ersten
    Lauf (leeres state.json) oder nach einer Regelaenderung, wo alles "neu" ist.
    """
    ordered = order_slots(fresh)
    msgs = [build_slot_message(m, id_cuadro) for m in ordered[:limit]]
    rest = ordered[limit:]
    if rest:
        msgs.append(build_message(
            rest, id_cuadro,
            title=f"➕ <b>{len(rest)} weitere freie Slots</b>"))
    return msgs


def build_message(fresh: list[dict], id_cuadro: str,
                  title: str = "🎾 <b>Freie Padelplaetze</b>") -> str:
    """Sammel-Nachricht: gruppiert nach Datum -> Court, freie Fenster zusammengefasst."""
    by_date: dict[dt.date, dict[str, list[dict]]] = {}
    for m in fresh:
        by_date.setdefault(m["date"], {}).setdefault(m["court_name"], []).append(m)

    lines = [title]
    for d in sorted(by_date):
        wd = WEEKDAYS[d.weekday()]
        lines.append(f"\n📅 <b>{wd} {d.strftime('%d.%m.%Y')}</b>")
        for court in sorted(by_date[d]):
            windows = ", ".join(merge_starts(by_date[d][court]))
            lines.append(f"  • {court}: {windows}")
    lines.append(f"\n🔗 {booking_url(id_cuadro)}")
    return "\n".join(lines)


if __name__ == "__main__":
    sys.exit(run())
