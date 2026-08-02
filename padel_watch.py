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
from matches import build_match_messages, fetch_matches, filter_matches

# Pfade relativ zu dieser Datei, nicht zum Arbeitsverzeichnis - damit auch die
# Web-UI build_message() importieren kann, ohne im Projektordner zu stehen.
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "config.yaml")
STATE_FILE = os.path.join(BASE_DIR, "state.json")
WEEKDAYS = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]


# ------------------------------------------------------------------ #
#  Telegram
# ------------------------------------------------------------------ #
def scrub(value: object) -> str:
    """
    Bot-Token aus Log-Ausgaben entfernen.

    Notwendig, weil die Actions-Logs eines oeffentlichen Repos fuer jeden lesbar
    sind und Telegram-Fehler die komplette URL enthalten
    (`api.telegram.org/bot<TOKEN>/sendMessage`). GitHub maskiert registrierte
    Secrets automatisch mit ***, aber das ist eine zweite Absicherung und keine
    Garantie - hier wird der Wert unabhaengig davon ersetzt.
    """
    s = str(value)
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if token:
        s = s.replace(token, "<TELEGRAM_BOT_TOKEN>")
    # Zusaetzlich nach Muster, falls der Token anders zusammengesetzt auftaucht
    return re.sub(r"bot\d{6,}:[A-Za-z0-9_-]{20,}", "bot<TELEGRAM_BOT_TOKEN>", s)


# Wird eine Telegram-Gruppe zur Supergruppe hochgestuft, aendert sich ihre Id
# (aus -123... wird -100123...). Telegram liefert die neue Id in der Fehlerantwort
# mit, wir uebernehmen sie sofort und merken sie in state.json - sonst waere der
# Versand ab diesem Moment tot, und die Fehlermeldung ginge an dieselbe kaputte Id.
CHAT_ID_OVERRIDE: str | None = None
LAST_MIGRATION: str | None = None


def apply_chat_override(state: dict | None = None) -> None:
    """Bereits migrierte Chat-Id aus state.json uebernehmen."""
    global CHAT_ID_OVERRIDE
    st = state if state is not None else load_state()
    CHAT_ID_OVERRIDE = st.get("meta", {}).get("chat_id") or None


def target_chat_id() -> str:
    return CHAT_ID_OVERRIDE or os.environ["TELEGRAM_CHAT_ID"]


def _post_telegram(token: str, chat_id: str, text: str):
    return requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": text,
              "parse_mode": "HTML", "disable_web_page_preview": True},
        timeout=20,
    )


def send_telegram(text: str) -> None:
    global CHAT_ID_OVERRIDE, LAST_MIGRATION
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = target_chat_id()

    resp = _post_telegram(token, chat_id, text)
    if resp.ok:
        return

    new_id = None
    try:
        new_id = resp.json().get("parameters", {}).get("migrate_to_chat_id")
    except Exception:
        pass

    if not new_id:
        resp.raise_for_status()
        return

    # Gruppe wurde hochgestuft: neue Id uebernehmen und dieselbe Nachricht
    # erneut schicken, damit dieser Lauf nichts verliert.
    print(f"Chat-Id migriert: {chat_id} -> {new_id}", file=sys.stderr)
    CHAT_ID_OVERRIDE = str(new_id)
    LAST_MIGRATION = str(new_id)
    _post_telegram(token, CHAT_ID_OVERRIDE, text).raise_for_status()


# ------------------------------------------------------------------ #
#  Zustandsspeicher (Diff, damit nicht jeder Lauf denselben Slot meldet)
# ------------------------------------------------------------------ #
def load_state() -> dict:
    try:
        return json.load(open(STATE_FILE, encoding="utf-8-sig"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=0, sort_keys=True)


def keep_state(old_state: dict, meta: dict) -> dict:
    """
    Alt-Zustand unveraendert weiterschreiben, nur den Merker aktualisieren.
    Fuer alle Abbruchpfade: die Slots sollen weiter als ungemeldet gelten,
    eine schon gesendete Ablaufwarnung sich aber nicht wiederholen.
    """
    out = dict(old_state)
    if meta:
        out["meta"] = meta
    return out


# ------------------------------------------------------------------ #
#  Hilfsfunktionen
# ------------------------------------------------------------------ #
def fmt_fecha(d: dt.date) -> str:
    """date -> 'T/M/JJJJ' ohne fuehrende Nullen (Matchpoint-Format)."""
    return f"{d.day}/{d.month}/{d.year}"


def target_dates(cfg: dict) -> list[dt.date]:
    today = dt.date.today()
    return [today + dt.timedelta(days=i) for i in range(cfg["days_ahead"] + 1)]


def check_token_expiry(cfg: dict, state: dict) -> str | None:
    """
    Warnt, wenn der PAT des externen Cron-Ausloesers bald ablaeuft.

    Notwendig, weil dieser Ausfall sonst voellig lautlos ist: laeuft der Token ab,
    startet kein Lauf mehr - und `notify_on_error` greift nur bei Fehlern INNERHALB
    eines Laufs. Kein Lauf heisst also auch keine Fehlermeldung.

    Gibt den Nachrichtentext zurueck oder None. Sendet hoechstens einmal pro Tag
    (Merker in state.json), sonst kaeme die Warnung bei jedem Lauf.
    """
    raw = cfg.get("token_expires")
    if not raw:
        return None
    try:
        expires = dt.date.fromisoformat(str(raw))
    except ValueError:
        print(f"token_expires ist kein Datum (JJJJ-MM-TT): {raw!r}", file=sys.stderr)
        return None

    today = dt.date.today()
    days = (expires - today).days
    if days > int(cfg.get("token_warn_days", 7)):
        return None
    if state.get("meta", {}).get("token_warned") == today.isoformat():
        return None                     # heute schon gewarnt

    # Kein <b> innerhalb von <b> - Telegram lehnt verschachtelte Tags ab (HTTP 400)
    if days < 0:
        wann = f"ist seit {abs(days)} Tag(en) abgelaufen"
    elif days == 0:
        wann = "läuft HEUTE ab"
    else:
        wann = f"läuft in {days} Tag(en) ab"

    return (
        f"⏳ <b>Padel-Watch: Zugangs-Token {wann}</b>\n"
        f"Ablaufdatum: {expires:%d.%m.%Y}\n\n"
        "Danach löst der externe Cron-Dienst keine Läufe mehr aus — und zwar "
        "ohne weitere Fehlermeldung, weil dann gar nichts mehr läuft.\n\n"
        "Zu tun:\n"
        "1. Neuen Fine-grained Token auf GitHub erzeugen "
        "(nur dieses Repo, Actions: Read and write)\n"
        "2. Beim Cron-Dienst im Authorization-Header eintragen\n"
        "3. <code>token_expires</code> in config.yaml auf das neue Datum setzen"
    )


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
    # encoding explizit: der Lauf laeuft unter Linux/UTF-8, die Konsole
    # schreibt unter Windows - ohne Angabe kollidieren die Locale-Defaults
    cfg = yaml.safe_load(open(CONFIG_FILE, encoding="utf-8-sig"))
    key_override = os.environ.get("KEY_OVERRIDE") or None

    old_state = load_state()
    # Datum -> Slot-Ids, plus der Sonderschluessel "meta" fuer Merker
    new_state: dict[str, object] = {}
    fresh: list[dict] = []          # neu aufgetauchte Treffer
    today = dt.date.today()

    # Merker aus dem alten Zustand uebernehmen, sonst geht er jeden Lauf verloren
    meta = dict(old_state.get("meta", {}))
    apply_chat_override(old_state)       # ggf. schon migrierte Gruppen-Id nutzen

    # Vor dem Abruf: eine ablaufende Berechtigung ist wichtiger als die Slots,
    # und die Warnung soll auch rausgehen, wenn der Abruf gleich scheitert.
    warning = check_token_expiry(cfg, old_state)
    if warning:
        try:
            send_telegram(warning)
            meta["token_warned"] = today.isoformat()
            print("Token-Ablaufwarnung gesendet.")
        except Exception as e:
            print(f"Token-Ablaufwarnung konnte nicht gesendet werden: {scrub(e)}",
                  file=sys.stderr)

    # Eine Session fuer den ganzen Lauf: Grid.aspx nur einmal laden statt pro Tag
    try:
        sess = open_session(cfg["id_cuadro"], key_override)
    except Exception as e:
        msg = f"⚠️ Padel-Watch: Verbindungsaufbau fehlgeschlagen: {scrub(e)}"
        print(msg, file=sys.stderr)
        if cfg.get("notify_on_error"):
            try:
                send_telegram(msg)
            except Exception:
                pass
        # Slot-Zustand unveraendert lassen, aber den Warn-Merker sichern -
        # sonst kaeme die Ablaufwarnung beim naechsten Lauf erneut
        save_state(keep_state(old_state, meta))
        return 1

    for d in target_dates(cfg):
        rules = rules_for_weekday(cfg, d)
        if not rules:
            continue

        fecha = fmt_fecha(d)
        try:
            payload = fetch_grid(cfg["id_cuadro"], fecha, key_override, session=sess)
        except Exception as e:
            msg = f"⚠️ Padel-Watch: Abruf fuer {fecha} fehlgeschlagen: {scrub(e)}"
            print(msg, file=sys.stderr)
            if cfg.get("notify_on_error"):
                try:
                    send_telegram(msg)
                except Exception:
                    pass
            # abbrechen: Diff nicht auf Basis kaputter Daten schreiben,
            # aber den Warn-Merker behalten
            save_state(keep_state(old_state, meta))
            return 1

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

    limit = int(cfg.get("max_messages", 10))

    # ---------------- Play!Match: offene Partien ----------------
    # Eigener Abschnitt und eigener Fehlerpfad: faellt die Match-Suche aus,
    # sollen die Platz-Alarme trotzdem rausgehen.
    fresh_matches: list[dict] = []
    seen_matches: list[str] = list(old_state.get("matches", []))
    match_rules = [r for r in (cfg.get("match_rules") or [])
                   if r.get("enabled", True)]
    if match_rules:
        try:
            alle_partien = fetch_matches(session=sess)
            bekannt = set(old_state.get("matches", []))
            gesehen: list[str] = []
            for r in match_rules:
                for m in filter_matches(alle_partien, r):
                    # Die Zahl der freien Plaetze gehoert zur Kennung: tritt
                    # spaeter noch jemand aus, ist das eine neue Meldung wert.
                    sid = f"{m['id']}@{m['frei']}"
                    if sid in gesehen:
                        continue
                    gesehen.append(sid)
                    if sid not in bekannt:
                        fresh_matches.append({**m, "regel": r.get("name")
                                              or "Play!Match"})
            seen_matches = gesehen      # ersetzt den Altstand nur bei Erfolg
        except Exception as e:
            msg = f"⚠️ Padel-Watch: Match-Suche fehlgeschlagen: {scrub(e)}"
            print(msg, file=sys.stderr)
            if cfg.get("notify_on_error"):
                try:
                    send_telegram(msg)
                except Exception:
                    pass

    # Versand VOR dem Schreiben des Zustands: sonst gelten Slots als gemeldet,
    # obwohl die Nachricht nie ankam - und der Persist-Schritt committet das mit
    # `if: always()`, die Meldung waere dauerhaft verloren.
    nachrichten: list[str] = []
    groups: list[dict] = []
    if fresh:
        # Zusammenhaengende Startzeiten zu Fenstern buendeln, dann je Fenster
        # den Link fuer den ERSTEN Slot holen
        groups = group_slots(fresh)
        resolve_booking_links(groups[:limit], cfg["id_cuadro"], session=sess)
        nachrichten += build_messages(groups, cfg["id_cuadro"], limit)
    if fresh_matches:
        nachrichten += build_match_messages(fresh_matches, limit)

    for i, text in enumerate(nachrichten):
        if i:
            time.sleep(0.4)     # Telegram nicht mit einem Burst bewerfen
        try:
            send_telegram(text)
        except Exception as e:
            print(f"Telegram-Versand fehlgeschlagen bei Nachricht "
                  f"{i + 1}/{len(nachrichten)}: {scrub(e)}", file=sys.stderr)
            # Zustand NICHT fortschreiben, damit der naechste Lauf alles
            # erneut meldet. Preis: bereits gesendete Nachrichten kommen
            # doppelt - besser als verlorene Meldungen.
            save_state(keep_state(old_state, meta))
            print("Zustand nicht fortgeschrieben, die Meldungen werden beim "
                  "naechsten Lauf wiederholt.", file=sys.stderr)
            return 1

    if fresh or fresh_matches:
        print(f"{len(fresh)} neue Slots in {len(groups)} Fenster(n), "
              f"{len(fresh_matches)} neue offene Partien, "
              f"{len(nachrichten)} Nachricht(en) gesendet.")
    else:
        print("Keine neuen freien Slots und keine neuen offenen Partien.")

    if seen_matches:
        new_state["matches"] = sorted(seen_matches)

    # Migrierte Gruppen-Id sichern und einmal darauf hinweisen: state.json ist
    # ein Repo-Artefakt und kann verloren gehen, das GitHub-Secret nicht.
    if LAST_MIGRATION and meta.get("chat_id") != LAST_MIGRATION:
        meta["chat_id"] = LAST_MIGRATION
        try:
            send_telegram(
                "ℹ️ <b>Padel-Watch: Gruppen-Id hat sich geändert</b>\n"
                f"Neue Id: <code>{LAST_MIGRATION}</code>\n\n"
                "Telegram hat die Gruppe zur Supergruppe hochgestuft. Der Versand "
                "läuft bereits über die neue Id — sie steht in state.json.\n\n"
                "Bitte trotzdem das GitHub-Secret <code>TELEGRAM_CHAT_ID</code> "
                "darauf ändern, sonst greift der alte Wert wieder, sobald "
                "state.json neu angelegt wird."
            )
        except Exception as e:
            print(f"Hinweis zur Migration nicht gesendet: {scrub(e)}", file=sys.stderr)

    if meta:
        new_state["meta"] = meta
    save_state(new_state)
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
                  f"nicht ermittelbar: {scrub(e)}", file=sys.stderr)
            m["book_url"] = None


def build_slot_message(m: dict, id_cuadro: str) -> str:
    """
    Eine Nachricht fuer EIN buchbares Fenster (Ergebnis von group_slots).
    Funktioniert auch mit einem einzelnen Slot - dann fehlen `count`/`end_min`
    und es wird der Block selbst angezeigt.
    """
    d = m["date"]
    count = m.get("count", 1)
    end = _hhmm(m.get("end_min", m["start_min"] + m["dur"]))

    if count > 1:
        # durchgehend freie Strecke: Fenster nennen, Startzeiten nur als Anzahl
        zeile = (f"📅 {WEEKDAYS[d.weekday()]} {d.strftime('%d.%m.%Y')} · "
                 f"<b>{m['start']}–{end}</b> frei\n"
                 f"📋 Regel: {m['rule']} · {count} mögliche Starts à {m['dur']} Min")
        text_link = f"Ab {m['start']} buchen ({m['dur']} Min)"
    else:
        zeile = (f"📅 {WEEKDAYS[d.weekday()]} {d.strftime('%d.%m.%Y')} · "
                 f"<b>{m['start']}–{end}</b> ({m['dur']} Min)\n"
                 f"📋 Regel: {m['rule']}")
        text_link = "Diesen Slot buchen"

    if m.get("book_url"):
        link = f"🔗 <a href=\"{m['book_url']}\">{text_link}</a>"
    else:
        link = (f"🔗 <a href=\"{booking_url(id_cuadro)}\">Buchungsplan öffnen</a> "
                f"— dort das Datum {d.strftime('%d.%m.')} im Kalender wählen")

    return (
        f"ALAAARRRM ALAAARRRM\n"
        f"🎾 <b>{m['court_name']}</b> frei\n"
        f"{zeile}\n"
        f"{link}"
    )


def order_slots(fresh: list[dict]) -> list[dict]:
    """Chronologisch, damit Nachrichten und Link-Abruf dieselbe Reihenfolge haben."""
    return sorted(fresh, key=lambda m: (m["date"], m["start_min"], m["court_name"]))


def group_slots(slots: list[dict]) -> list[dict]:
    """
    Aufeinanderfolgende Startzeiten desselben Courts am selben Tag zu EINEM
    buchbaren Fenster zusammenfassen.

    Beispiel: die Treffer 13:00 und 13:30 (je 90 Min) beschreiben nicht zwei
    Angebote, sondern eine freie Strecke 13:00-15:00. Zwei Nachrichten dafuer
    waeren Rauschen. Gebucht wird der erste Slot des Fensters - fuer den wird
    auch der Buchungs-Token geholt.

    Die Gruppe traegt alle Felder ihres ersten Slots weiter (court_id, modalidad,
    start, dur ...), damit resolve_booking_links() unveraendert damit arbeitet.
    """
    groups: list[dict] = []
    # court-weise sortieren, sonst reissen dazwischenliegende Courts die Kette
    for m in sorted(slots, key=lambda x: (x["date"], x["court_id"], x["start_min"])):
        g = groups[-1] if groups else None
        anschluss = (g and g["court_id"] == m["court_id"] and g["date"] == m["date"]
                     and m["start_min"] - g["last_start_min"] == m["step"])
        if anschluss:
            g["last_start_min"] = m["start_min"]
            g["end_min"] = max(g["end_min"], m["start_min"] + m["dur"])
            g["count"] += 1
            g["slots"].append(m)
        else:
            groups.append({**m,
                           "last_start_min": m["start_min"],
                           "end_min": m["start_min"] + m["dur"],
                           "count": 1,
                           "slots": [m]})
    groups.sort(key=lambda g: (g["date"], g["start_min"], g["court_name"]))
    return groups


def build_messages(groups: list[dict], id_cuadro: str, limit: int) -> list[str]:
    """
    Alle Nachrichten eines Laufs: eine pro zusammenhaengendem Fenster
    (Ergebnis von group_slots), chronologisch.
    Ueber `limit` hinaus wird zusammengefasst statt geflutet - wichtig beim ersten
    Lauf (leeres state.json) oder nach einer Regelaenderung, wo alles "neu" ist.
    """
    msgs = [build_slot_message(g, id_cuadro) for g in groups[:limit]]
    rest = [s for g in groups[limit:] for s in g.get("slots", [g])]
    if rest:
        msgs.append(build_message(
            rest, id_cuadro,
            title=f"➕ <b>{len(groups) - limit} weitere freie Fenster</b>"))
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
    try:
        sys.exit(run())
    except Exception:
        # Traceback erhalten, aber gefiltert: er kann die Telegram-URL samt
        # Token enthalten, und Actions-Logs sind bei oeffentlichen Repos oeffentlich.
        import traceback
        print(scrub(traceback.format_exc()), file=sys.stderr)
        sys.exit(1)
