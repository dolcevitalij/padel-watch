# Padel-Watch

Benachrichtigt per Telegram, sobald ein Padelplatz frei wird, der zu deinen
Wunschregeln passt (Uhrzeit, Wochentage, Court, Dauer). Laeuft kostenlos als
GitHub-Actions-Cron-Job – **dein Rechner muss nicht laufen**.

Datenquelle: der (inoffizielle) Matchpoint-Endpunkt hinter
`kunden.hallofpadel.com`. Es wird nur **gelesen**, nichts gebucht.

> **Weiterarbeit in Claude Code?** Siehe **`HANDOFF.md`** — dort stehen alle
> Erkenntnisse (Endpunkt, Payload, offener key-Punkt, Architektur, nächste
> Schritte, Spezifikation der Web-UI) gebündelt.

## Test- & Konfigurationsumgebung (Web-UI)

Lokales Tool zum Einstellen und Prüfen – **kein Deployment nötig**:

```bash
pip install -r requirements-dev.txt
python webapp.py           # dann http://localhost:5000 oeffnen
```

Damit kannst du `config.yaml` im Browser bearbeiten, die Verbindung + den `key`
testen und Regeln als **Timeline-Vorschau** prüfen (belegt/frei/Wunschfenster) –
alles ohne Telegram-Versand. Für die Telegram-Testnachricht vorher
`TELEGRAM_BOT_TOKEN` und `TELEGRAM_CHAT_ID` als Umgebungsvariablen setzen.

## Wie es funktioniert

```
GitHub Actions (Cron alle 10 Min)
   -> Grid.aspx laden, key extrahieren, ObtenerCuadro abrufen (pro Tag)
   -> freie Bloecke berechnen (API liefert nur belegte Slots)
   -> gegen config.yaml filtern
   -> Diff gegen state.json  (nur NEUE Slots)
   -> Telegram-Nachricht
```

## Einrichtung (einmalig, ~10 Min)

### 1. Telegram-Bot anlegen
1. In Telegram **@BotFather** anschreiben -> `/newbot` -> Namen vergeben.
2. Du bekommst einen **Bot-Token** (`123456:ABC...`). Merken.
3. Deine **Chat-ID** herausfinden: schreib deinem neuen Bot irgendeine
   Nachricht, dann oeffne im Browser
   `https://api.telegram.org/bot<TOKEN>/getUpdates` und lies `chat.id` aus.

### 2. Repo anlegen
Dateien in ein **eigenes GitHub-Repo** legen (privat ist ok; bei privaten Repos
sind die Actions-Minuten begrenzt, reichen fuer 10-Min-Takt aber locker).

### 3. Secrets setzen
Repo -> **Settings -> Secrets and variables -> Actions -> New repository secret**:
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`
- `KEY_OVERRIDE` *(nur falls noetig, siehe unten)*

### 4. Regeln anpassen
`config.yaml` bearbeiten: Wochentage, Uhrzeiten, Dauer, Courts.

### 5. Starten
Actions-Tab -> **Padel-Watch** -> **Run workflow** (manuell testen).
Danach laeuft es automatisch alle 10 Minuten.

## Der `key`-Parameter – bitte einmal pruefen

Der Endpunkt verlangt ein `key`-Token. Das Skript zieht es bei jedem Lauf
frisch aus der Grid-Seite (`fetch.py` -> `KEY_PATTERNS`). Ob die automatische
Extraktion greift, **einmal lokal testen**:

```bash
pip install -r requirements.txt
python - <<'PY'
from fetch import fetch_grid
data = fetch_grid("4", "1/8/2026")   # beliebiges zukuenftiges Datum, T/M/JJJJ
print(list(data["d"].keys())[:5], "... OK, key hat funktioniert")
PY
```

- **Klappt** -> nichts weiter zu tun.
- **Fehler „key konnte nicht extrahiert werden"** -> Grid-Seite im Browser
  oeffnen, Quelltext anzeigen, nach dem key-Wert suchen (der lange
  Base64-String aus dem Netzwerk-Tab) und das umgebende Muster in
  `KEY_PATTERNS` ergaenzen. Notfalls den aktuellen key als Secret
  `KEY_OVERRIDE` setzen (Achtung: koennte irgendwann ablaufen).

## Lokaler Testlauf (ohne Telegram)

```bash
TELEGRAM_BOT_TOKEN=x TELEGRAM_CHAT_ID=x python padel_watch.py
```
(Bei Treffern schlaegt das Senden fehl – die Berechnung siehst du trotzdem im Log.)

## Dateien
| Datei | Zweck |
|-------|-------|
| `config.yaml` | deine Regeln (hier stellst du alles ein) |
| `core.py` | Frei-Slot-Berechnung (getestet) |
| `fetch.py` | Abruf + key-Extraktion |
| `padel_watch.py` | Orchestrierung + Telegram + Diff |
| `state.json` | zuletzt gesehene freie Slots (auto-verwaltet) |
| `.github/workflows/padel-watch.yml` | Cron-Job |

## Hinweise
- **Fairness:** 10-Min-Takt + 1 s Pause je Request. Nicht aggressiver stellen –
  schont den Club-Server und reduziert Blocking-Risiko.
- **Robustheit:** Bei Abruf-Fehlern kommt (falls `notify_on_error: true`) eine
  Telegram-Warnung, damit du stille Ausfaelle bemerkst.
- **GitHub-Cron** kann sich bei Last um einige Minuten verzoegern – fuer diesen
  Zweck unkritisch.
