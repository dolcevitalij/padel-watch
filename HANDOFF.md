# Padel-Watch — Projekt-Handoff

Kontext-Dokument für die Weiterarbeit in **Claude Code**. Enthält alle bisherigen
Erkenntnisse, damit nichts neu hergeleitet werden muss.

**Stand 31.07.2026:** Abruf inkl. `key` ist live verifiziert, die Frei-Slot-Logik gegen
echte Daten gegengerechnet, die lokale Web-UI läuft. Offen sind nur noch Telegram und
das GitHub-Actions-Setup (§5).

---

## 1. Ziel

Eine Anwendung, die **benachrichtigt, sobald ein Padelplatz frei wird**, der zu
konfigurierbaren Regeln passt (Uhrzeit, Wochentage, Court, Spieldauer).
Randbedingungen: **kein dauerhaft laufender Rechner**, Benachrichtigung aufs
Handy. Es wird ausschließlich **gelesen**, nichts gebucht.

---

## 2. Datenquelle — reverse-engineered

Buchungsseite: `https://kunden.hallofpadel.com/Booking/Grid.aspx?id=4`
System: **Matchpoint** (spanische Vereinssoftware, ASP.NET WebForms). Der
`__type` in der Antwort lautet `Matchpoint.Web.Library.Views.CuadroReservasNuevo`.

**Es gibt keine offizielle API, aber einen nutzbaren XHR-Endpunkt:**

| Eigenschaft | Wert |
|---|---|
| URL | `https://kunden.hallofpadel.com/booking/srvc.aspx/ObtenerCuadro` |
| Methode | `POST` |
| Request-Content-Type | `application/json; charset=UTF-8` |
| Response-Content-Type | `application/json` (sauber, kein HTML-Scraping nötig) |
| Body | `{"idCuadro":"4","fecha":"28/7/2026","key":"<TOKEN>"}` |

**Wichtige Details:**
- `fecha`-Format ist `T/M/JJJJ` **ohne führende Nullen** (z.B. `28/7/2026`, `1/8/2026`).
- Antwort ist in `{"d": ...}` verpackt (ASP.NET-ScriptService-Muster).
- Buchbarer Horizont: **21 Tage** (`VisualizacionCuadro_DiasVista: 21`; am 31.07.2026
  war `StrFechaMax` = `21/08/2026`) → `days_ahead: 14` liegt sicher darunter.
- Raster: `PartesPorHora: 2` → **30-Minuten-Slots**.
- `StrHoraActualDelCentro` = aktuelle Uhrzeit der Anlage → für „heute" dürfen
  keine vergangenen Startzeiten gemeldet werden.

**Kern-Erkenntnis zur Datenstruktur:**
Die Antwort listet je Court (`Columnas[]`) nur die **belegten** Zeiten
(`Ocupaciones[]`, jeweils mit `StrHoraInicio`/`StrHoraFin` als `"HH:MM"`).
**Freie** Slots liefert die API nicht — sie werden als **Komplement** der
Belegungen innerhalb der Öffnungszeiten berechnet. Genau das macht `core.py`.

**Courts (Stand der Live-Daten), Plan `id=4`:**
`16` BS ZILLMANN · `17` BS HAUS DER FINANZEN · `13` BS Platz 1 ·
`14` BS Platz 2 · `15` BS KOSATEC.

### ✅ Punkt Nr. 1: der `key` — geklärt am 31.07.2026
Die Seite blockiert **nicht**: `GET Booking/Grid.aspx?id=4` liefert HTTP 200.
Der Token steht in einem **Inline-`<script>` der Grid-Seite als JS-Zuweisung mit
obfuskiertem Variablennamen** — das Wort „key" kommt im Grid-HTML **null** mal vor:

```
hl90njda2b89k='eNEe29kXfZA3jrcfRqZse3HO0TxEsybmuaptqTsIR1vglvloaKL4JA==';
```

56 Zeichen Base64. Verwendet wird die Variable erst in `booking/js/ajax.js?ver=2.9.5`
(Funktion `obtenerCuadro` → Feld `"key"`); dieselbe Variable versorgt auch
`ObtenerCuadros`, `ObtenerCuadroSoloContenido_HTML` und
`ObtenerInformacionGeneralEspacioOcupado`.

Genau das war die Ursache des Fehlschlags: die vier ursprünglichen `KEY_PATTERNS`
suchten alle nach dem Namen „key". `fetch.py` hat jetzt zwei Muster davor:
den bekannten Variablennamen und einen **generischen Fallback** (kurzer
obfuskierter Bezeichner am Statement-Anfang + Base64-Wert), der auch nach einer
Umbenennung greift — offline gegen eine HTML-Kopie mit umbenannter Variable geprüft.
Die alten Muster bleiben als weitere Fallbacks, Notfall-Weg bleibt `KEY_OVERRIDE`.

Der key war bei mehreren Aufrufen identisch, wirkt also nicht rotierend; `fetch.py`
zieht ihn trotzdem pro Lauf frisch aus derselben `requests.Session` (Cookies
inklusive) — das ist verifiziert funktionierend und robust gegen Rotation.

### ✅ Deeplink auf einen einzelnen Slot — verifiziert am 31.07.2026
**Zweiter nutzbarer Endpunkt**, gefunden über `ajaxObtenerInformacionHuecoLibre` in
`booking/js/ajax.js` (das ist der Klick auf eine freie Zelle):

| Eigenschaft | Wert |
|---|---|
| URL | `https://kunden.hallofpadel.com/booking/srvc.aspx/ObtenerInformacionHuecoLibre` |
| Body | `{"idCuadro":4,"idRecurso":"16","idmodalidad":4,"fecha":"3/8/2026","hora":"08:00","key":"<TOKEN>"}` |
| Antwort | `d.Opciones[]` = je Spieldauer ein Eintrag: `{"Token":"…","Descripcion":"90min Online"}` |

- `idRecurso` = Court-Id, `idmodalidad` = `IdModalidadFijaParaReservas` der Spalte
  (bei Plan 4 durchgehend `4`) — deshalb trägt `core.Court` das Feld `modalidad` mit.
- `hora` ist die **freie** Startzeit; es gab drei Optionen (60/90/120 min), die Tokens
  unterscheiden sich nur im letzten Block → der Token kodiert Court + Datum + Zeit + Dauer.
- Daraus wird der Link gebaut, genau wie `clickBotonPista` es tut:
  `booking/info.aspx?token=<Token>&return_url=<Grid-URL>` (`return_url` bleibt
  unencodiert, wie im Original-JS).
- **Ohne Login antwortet der Link mit 302 auf `Login.aspx`** und trägt den Token in
  dessen `return_url` mit — nach dem Anmelden landet man also auf genau diesem Slot.
  Auf dem eingeloggten Handy-Browser geht es direkt in den Buchungsdialog.
- Kosten: **ein Request pro Slot**. Deshalb holt `resolve_booking_links()` Tokens nur
  für die Slots, die auch eine Einzelnachricht bekommen (≤ `max_messages`), mit 0,5 s
  Pause; in der Web-UI ist es hinter einer Checkbox.

Was dagegen **nicht** geht (auch geprüft): `Grid.aspx` per URL auf ein Datum setzen —
`?fecha=…`, `&dia=…`, `&date=…` liefern alle den Kalenderstart „heute", und das
Client-JS liest weder `location.search` noch `location.hash`. Der Plan-Link ist deshalb
nur noch Fallback, wenn kein Token zu holen war.

### Zwei Details, die die Live-Daten ergänzt haben
- **Öffnungszeiten: die Anlage ist 24/7 offen** (von Vitalij bestätigt; passt zu den
  API-Werten `StrHoraInicio`/`StrHoraFin` = `00:00`/`00:00`). Die API liefert also keine
  nutzbaren Grenzen, es zählen allein `day_open`/`day_close` aus `config.yaml` — dort
  jetzt `'00:00'` bis `'24:00'`. `24:00` ist verifiziert: das Fenster endet bei Minute
  1440, ein 90-Minuten-Block ab 22:30 gilt als Treffer.
  **Achtung:** Uhrzeiten in `config.yaml` müssen quotiert sein — PyYAML liest
  unquotiertes `24:00` als Zahl 1440 und `18:00` als 1080 (YAML-Sexagesimal), was den
  Lauf mit einem Typfehler abbricht. Die Web-UI quotet beim Speichern korrekt.
- **`Ocupaciones[].Tipo`** kennt u.a. `reserva_individual`, `reserva_partida`,
  `reserva_actividad_abierta`, `clase_colectiva`, `partido_torneo`, `reserva_club`.
  `core.py` behandelt alle gleich als belegt (konservativ, korrekt für „Platz frei?").
  Offene Partien tragen in `Texto1` Hinweise wie „1 freie Plätze" — Basis für ein
  späteres Feature „mitspielen statt Platz buchen".

---

## 3. Architektur-Entscheidungen

| Frage | Entscheidung | Begründung |
|---|---|---|
| Ausführung ohne eigenen Rechner | **GitHub Actions**, angestoßen von einem **externen Cron-Dienst** (alle 10 Min) | kostenlos, kein Server; GitHubs eigener Scheduler löste nie aus, siehe §9 |
| Benachrichtigung | **Telegram-Bot** (direkter `sendMessage`-Call) | kein App-Store/Dev-Account, echter Push |
| Empfänger | **Telegram-Gruppe** statt Einzelchat (`TELEGRAM_CHAT_ID` = negative Gruppen-Id) | Freunde mitnutzen lassen = in die Gruppe einladen, keine Codeänderung, keine Liste von Chat-Ids zu pflegen |
| Nachrichten-Zuschnitt | **eine Nachricht pro Slot**, chronologisch, 0,4 s Pause dazwischen | jede Meldung ist einzeln abtippbar/teilbar; Sammel-Nachricht erst über `max_messages` (Default 10), damit der erste Lauf mit leerem `state.json` nicht flutet |
| Link in der Nachricht | **direkter Slot-Link** über einen frisch geholten Token (§2) | ein Tap führt in den Buchungsdialog dieses Slots; Fallback ist der Plan-Link |
| HTTP-Sessions | **eine Session pro Lauf** (`open_session`) | Grid.aspx wird einmal statt 15× geladen — spart Requests trotz der neuen Token-Abrufe |
| Automatisierungstool (n8n/Zapier)? | **Nein** | überflüssige Schicht; die Logik ist maßgeschneiderter Code |
| Doppel-Benachrichtigung vermeiden | **State-Diff** (`state.json`) | meldet nur *neu* freie Slots |
| Zustand vs. Versand | **erst senden, dann `save_state()`** | scheitert Telegram, gelten die Slots weiter als ungemeldet und werden nachgeholt; Preis sind mögliche Doppel-Nachrichten statt verlorener Meldungen |
| State-Persistenz in Actions | State wird **ins Repo zurückcommittet** | zuverlässiger als Actions-Cache |

**Fairness/Robustheit:** 10-Min-Takt + 1 s Pause je Request; bei Abruf-Fehlern
optionale Telegram-Warnung (`notify_on_error`), damit stille Ausfälle auffallen.

---

## 4. Dateien

| Datei | Rolle | Status |
|---|---|---|
| `config.yaml` | **Alle** Nutzereinstellungen (Regeln, Zeiten, Courts, `max_messages`) | fertig |
| `core.py` | Frei-Slot-Berechnung (Komplement, Filter) | **gegen Live-Daten verifiziert** ✅ |
| `fetch.py` | `open_session` (Cookies + key), `fetch_grid`, `fetch_slot_options` (Buchungs-Token) | **live getestet** ✅ |
| `padel_watch.py` | Orchestrierung: Abruf → Filter → Diff → Telegram | Logik getestet; Telegram-Pfad noch nicht live |
| `test_key.py` | Einmaliger Verbindungs-/key-Test | **grün** ✅ |
| `webapp.py` + `templates/` | **Test-/Konfig-Weboberfläche** (siehe §6) | **läuft lokal** ✅, Vorschau verifiziert |
| `.github/workflows/padel-watch.yml` | Cron-Job | fertig |
| `requirements.txt` | Runtime-Deps (`requests`, `PyYAML`) | fertig |
| `requirements-dev.txt` | + `Flask` (nur für die Web-UI) | fertig |
| `README.md` | Einrichtungsanleitung | fertig |

Dazu neu: `.venv/` (lokale Dev-Umgebung, siehe §8 — via `.gitignore` **nicht** im Repo)
und `.claude/launch.json` (startet die Web-UI über das Preview-Tool; nur relative
Pfade, kann also mit ins Repo).

**Gegen echte Beispieldaten getestet:** Belegungen mergen, freie Fenster berechnen,
Court-Filter, Vergangenheits-Filter, State-Diff, Telegram-Nachrichtenaufbau,
Web-Config-Roundtrip, Vorschau-Logik.

**Am 31.07.2026 zusätzlich live verifiziert:** HTTP-Abruf inkl. key-Extraktion
(`test_key.py` grün: Plan „Padel", 5 Courts), Verbindungstest in der Web-UI und die
Regel-Vorschau — Letztere doppelt gegengerechnet:
- **Mo 03.08., 18:00–21:30, 90 min → 0 Treffer.** Court 16 belegt 06:30–08:00,
  11:00–13:00, 14:30–22:00 (gemergt) → abends komplett ausgebucht, korrekt.
- **Fr 07.08., 18:00–21:30, 90 min → 2 Treffer:** BS ZILLMANN 20:00, BS Platz 2 20:00.
  Unabhängig aus den Rohbelegungen nachgerechnet, identisches Ergebnis inkl. der
  Nicht-Treffer (BS Platz 1 bis 21:00 belegt → 20:00–21:30 überlappt).

**Ebenfalls live verifiziert (31.07.2026):** der Buchungs-Token-Abruf und der daraus
gebaute Slot-Link — pro Court eigener Token, 90-Min-Option korrekt ausgewählt, Link
antwortet mit 302 auf `Login.aspx` und trägt den Token in `return_url` mit.

**Noch nicht live getestet:** Telegram-Versand und der Lauf unter GitHub Actions.

---

## 5. Nächste Schritte (Stand 31.07.2026)

- [x] **Deps installieren** — Python 3.13 + venv, siehe §8.
- [x] **key verifizieren** — `test_key.py` grün, `KEY_PATTERNS` gefixt (§2).
- [x] **Web-UI starten** — läuft, Verbindungstest und Regel-Vorschau verifiziert (§4).
- [x] **Öffnungszeiten geklärt** — 24/7, `config.yaml` steht auf `'00:00'`–`'24:00'`.
- [ ] **Telegram einrichten:** Bot bei `@BotFather` (`/newbot`) anlegen, Token notieren;
      Chat-ID über `https://api.telegram.org/bot<TOKEN>/getUpdates` nach einer
      Nachricht an den Bot ablesen. Lokal als `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID`
      setzen, dann „Telegram-Testnachricht" in der UI (siehe auch README §1).
- [ ] **GitHub-Repo:** `git init`, Repo anlegen (kein `gh` CLI auf dem Rechner →
      über den Browser), Secrets setzen (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`,
      optional `KEY_OVERRIDE`), Workflow per `workflow_dispatch` auslösen.
- [ ] **Beobachten** und ggf. Regeln nachschärfen.

---

## 6. Test- & Konfigurationsumgebung (Web-App) — Spezifikation

Lokales Dev-Tool (Flask), **kein Deployment**. Start:
`.\.venv\Scripts\python.exe webapp.py` (siehe §8).
Wiederverwendet `core.py` und `fetch.py` (keine Logik-Duplikation).

### Kernfunktionen (implementiert)
- **Konfig-Editor:** `config.yaml` im Browser bearbeiten/speichern — Plan-ID,
  Tage-Voraus, Öffnungszeiten, Fehler-Benachrichtigung sowie beliebig viele
  **Regeln** (Name, Wochentage, Zeitfenster, Dauer, Court-Auswahl).
- **Verbindungs-/key-Test:** ruft ein Datum live ab, zeigt Court-Liste +
  Belegungszahl oder die Fehlermeldung.
- **Regel-Vorschau ohne Telegram:** Datum + Fenster + Dauer + Courts wählen →
  **Timeline je Court** mit drei Zuständen: belegt = rot, frei = grüner Schleier,
  **buchbar = kräftiger grüner Balken** (aufeinanderfolgende Treffer zu einem Fenster
  zusammengefasst, Tooltip mit Uhrzeit); Wunschfenster als blaue gestrichelte Klammer.
  Rechts neben dem Court steht das buchbare Fenster plus Anzahl der Startzeiten statt
  einer langen Liste. Die Zeitachse dünnt bei langen Spannen automatisch auf
  2-Stunden-Schritte aus (nötig seit 24/7).
- **Telegram-Nachrichten-Vorschau:** unter den Timelines wird **jede** Nachricht als
  eigene Chat-Blase gerendert (inkl. Fettschrift und klickbarem Link) — gebaut von
  `padel_watch.build_messages`, also identisch zum Produktivlauf, keine zweite
  Formatierung. Ist eine Regel geladen, steht ihr Name in den Nachrichten.
  Unterschied zum echten Lauf: hier gelten alle Treffer als neu (kein
  `state.json`-Diff), und es ist immer nur der gewählte Tag.
- **Checkbox „echte Buchungs-Links holen"** (Standard aus): holt pro Slot einen Token
  (§2) und zeigt die Links, die auch im Produktivlauf verschickt werden. Aus bleibt sie
  beim Regel-Tüfteln, damit nicht jeder Klick zusätzliche Requests auslöst.
- **„→ Vorschau" pro Regel:** übernimmt Zeitfenster, Dauer und Courts der Regel in die
  Vorschau und springt auf das **nächste passende Datum** dieses Wochentags.
  Ohne diesen Knopf sind Regeln und Vorschau entkoppelt — genau die Fehlerquelle,
  gegen die er gebaut wurde.
- **Kontextzeile unter dem Datum:** nennt Wochentag und die Regeln, die an diesem Tag
  greifen, und warnt rot, wenn (a) die geladene Regel diesen Wochentag nicht abdeckt,
  (b) überhaupt keine Regel greift, (c) das Datum weiter voraus liegt als `days_ahead`
  oder (d) ungespeicherte Änderungen vorliegen — die Vorschau rechnet serverseitig
  immer mit der **gespeicherten** `config.yaml`.
- **Speicherstand-Anzeige** neben dem Speichern-Knopf: `gespeichert` / `ungespeichert`.
- **Telegram-Testnachricht:** einzelner Button, sendet eine Testnachricht.

### Optionale/spätere Erweiterungen (bewusst noch nicht gebaut)
- Mehrtages-Vorschau (alle Regeln über den ganzen Horizont auf einmal).
- Echter „Trockenlauf" von `padel_watch.py`: Diff gegen `state.json` und alle Tage in
  einem Durchgang. Die Nachrichten-Vorschau zeigt bisher nur einen Tag und ignoriert
  den Diff — bewusst, weil ein Volllauf ~15 Requests an den Club-Server bedeutet.
- Persistente `.env`-Verwaltung für Tokens statt Umgebungsvariablen.

### Nicht-Ziele
Die Web-UI **ersetzt nicht** den Cron-Job; sie dient nur Konfiguration und Test.
Der Produktivlauf bleibt `padel_watch.py` unter GitHub Actions.

---

## 7. Konventionen / Leitplanken
- **Keine Secrets im Code/Repo** — nur via GitHub Secrets bzw. lokale Umgebungsvariablen.
- **Ausnahmen nie ungefiltert loggen:** immer `scrub(e)` benutzen. Die Actions-Logs
  eines öffentlichen Repos sind für jeden lesbar, und Telegram-Fehler enthalten die
  komplette URL samt Bot-Token. GitHub maskiert Secrets zwar selbst, das ist aber nur
  die zweite Absicherung. Auch der Traceback in `__main__` läuft durch `scrub()`.
- **Zustand erst nach erfolgreichem Versand schreiben** (siehe §3). Auf allen
  Abbruchpfaden `keep_state(old_state, meta)` verwenden: Slots bleiben ungemeldet,
  eine gesendete Ablaufwarnung wiederholt sich trotzdem nicht.
- **Telegram nur über `padel_watch.send_telegram()`**, auch aus der Web-UI. Dort
  hängen Token-Filterung und die Behandlung migrierter Gruppen-Ids dran; eine zweite
  `requests.post`-Kopie würde beides umgehen.

### Gruppen-Ids und Supergruppen
Telegram stuft Gruppen automatisch zur Supergruppe hoch (mehr Mitglieder, öffentlich
schalten). Dabei **ändert sich die Id** von `-123456789` zu `-100123456789`, und
jeder Versand an die alte Id scheitert — inklusive der Fehlermeldung, die ja an
dieselbe Adresse ginge. `send_telegram()` fängt das ab: Telegram liefert die neue Id
als `parameters.migrate_to_chat_id`, die Nachricht wird sofort erneut geschickt
(dieser Lauf verliert also nichts), die Id landet in `state.json` unter
`meta.chat_id` und wird ab dann bevorzugt (`target_chat_id()`).

Ein Hinweis geht zusätzlich an den Nutzer, weil `state.json` ein Repo-Artefakt ist:
geht sie verloren, greift wieder das GitHub-Secret. Das sollte also nachgezogen werden.
Gegen simulierte Telegram-Antworten geprüft (Umleitung, Persistenz, Hinweis, und dass
ein Fehler *ohne* `migrate_to_chat_id` weiterhin durchschlägt).
- **Saubere Trennung:** Abruf (`fetch`), Logik (`core`), Orchestrierung
  (`padel_watch`), UI (`webapp`) bleiben entkoppelt.
- Poll-Intervall **nicht** unter ~10 Min drücken (Fairness ggü. Club-Server).
- `fecha` immer über `f"{d.day}/{d.month}/{d.year}"` bilden (Format ohne Nullen).
- **Uhrzeiten in `config.yaml` immer quoten** (`'18:00'`, `'24:00'`) — sonst liest PyYAML
  sie als Sexagesimalzahl.
- Überlappende Regeln sind unkritisch: `run()` dedupliziert pro Tag über `slot_id`,
  jeder Slot wird also nur einmal gemeldet (auch wenn zwei Regeln ihn treffen).
- Beim Debuggen keine Abruf-Schleifen — einzelne Requests, Antworten lokal
  zwischenspeichern und offline auswerten.

---

## 8. Lokale Dev-Umgebung (Windows, ohne Admin-Rechte)

Auf dem Rechner war **kein Python** installiert — `python.exe`/`python3.exe` unter
`AppData\Local\Microsoft\WindowsApps` sind nur Store-Alias-Stubs, `pip`/`py` fehlten.
Ohne Admin-Rechte geht nur User-Scope. Eingerichtet am 31.07.2026:

```powershell
winget install --id Python.Python.3.13 --source winget --scope user
```

→ Python 3.13.14 unter `%LOCALAPPDATA%\Programs\Python\Python313` (kein UAC-Prompt).

```powershell
& "$env:LOCALAPPDATA\Programs\Python\Python313\python.exe" -m venv .venv
& .\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
```

**`python` ohne Pfad benutzt weiterhin den Store-Stub** — immer
`.\.venv\Scripts\python.exe` aufrufen:

```powershell
& .\.venv\Scripts\python.exe test_key.py
& .\.venv\Scripts\python.exe webapp.py     # -> http://localhost:5000
```

Vorhanden ist außerdem `git 2.55`; **nicht** vorhanden: `gh`, `node`, `uv`, `pipx`.
`CONFIG_PATH` in `webapp.py` liegt bewusst neben der Datei (nicht relativ zum
Arbeitsverzeichnis), damit der Start über `.claude/launch.json` von außerhalb klappt.

**Komfort-Skripte** (Doppelklick, liegen im Repo):

| Datei | Zweck |
|---|---|
| `start-webapp.bat` | Testkonsole starten → http://localhost:5000 |
| `publish-config.bat` | `config.yaml` committen + pushen, falls geändert |
| `trigger-run.bat` | Workflow per API auslösen (braucht `GITHUB_TOKEN` in `.env`) |

---

## 9. Warum ein externer Cron-Dienst (Stand 01.08.2026)

**GitHubs eigener Scheduler hat für dieses Repo nie ausgelöst.** Befund nach dem
ersten manuellen Lauf (09:10 UTC): über 1,5 Stunden **null** geplante Läufe, obwohl
bei `*/10` rund neun fällig gewesen wären. Ausgeschlossen wurde:

- Workflow-Status ist `active` (API `/actions/workflows`), kein Deaktivierungsbanner in der UI
- Repo ist **öffentlich** → unbegrenzte Actions-Minuten, kein Kontingentproblem
- Default-Branch ist `main`, dort liegt die Workflow-Datei (Cron läuft nur dort)
- `workflow_dispatch` funktioniert → die `on:`-Sektion wird korrekt geparst
- Versatz auf krumme Minuten (`3,13,23,...`) brachte ebenfalls nichts

Geplante Läufe sind auf den kostenlosen Runnern laut GitHub ausdrücklich *best effort*:
sie werden verzögert und bei Last **verworfen**. Für „melde mir freie Plätze zeitnah"
ist das unbrauchbar.

**Lösung:** ein externer Cron-Dienst ruft alle 10 Minuten die
`workflow_dispatch`-API auf. Der GitHub-Zeitplan bleibt als **stündliches Notnetz**
(`cron: "7 * * * *"`) — so gibt es keine doppelten Läufe, falls GitHubs Scheduler
später doch anspringt, und der Ausfall des externen Dienstes bleibt nicht unbemerkt.

Aufruf (identisch in `trigger-run.ps1`):

```
POST https://api.github.com/repos/dolcevitalij/padel-watch/actions/workflows/padel-watch.yml/dispatches
Accept: application/vnd.github+json
Authorization: Bearer <PAT>
X-GitHub-Api-Version: 2022-11-28
Body: {"ref":"main"}
→ Erfolg = HTTP 204 (ohne Inhalt)
```

**cron-job.org läuft, aber unzuverlässig.** URL, Header, Body und Token sind korrekt
(Lese-Aufrufe mit demselben Token: HTTP 200). Am 01.08. schlug der Aufruf um 11:20 UTC
nach 30 s mit Timeout fehl — im Timing-Diagramm die volle Zeit in der Phase
„Empfangen", kein Lauf wurde erzeugt; der um 11:30 UTC lief dagegen durch und hat
Telegram-Nachrichten ausgelöst. Vermutung: deren HTTP-Client kommt mit GitHubs
inhaltsloser Antwort (**204 No Content**) nicht immer klar, oder die geteilten
IP-Adressen werden gebremst. Ein höheres Timeout hilft dagegen nicht.

**Ein ausgefallener Aufruf kostet keine Meldung**, nur Zeit: der `state.json`-Diff
vergleicht gegen den letzten *erfolgreichen* Lauf, ein übersprungener Slot wird also
beim nächsten Lauf gemeldet. Erst wenn Ausfälle gehäuft auftreten, wird es störend.

Vorbereiteter Ersatz, falls es so weit kommt: **Cloudflare Worker** mit Cron Trigger,
Code und Einrichtung in `cron-worker.js`. Der ist selbst der HTTP-Client, liest die
204 korrekt und protokolliert alles ≠ 204 als Fehler.

**Token:** Fine-grained PAT, *nur* dieses Repo, *nur* `Actions: Read and write`.
Der Schaden bei Verlust ist damit auf „kann Workflow-Läufe starten" begrenzt — kein
Push, kein Zugriff auf andere Repos. Der Token liegt beim Cron-Dienst (Drittanbieter)
und lokal in `.env`; **nicht** in den GitHub Secrets, die braucht nur der Lauf selbst.
Fine-grained Tokens laufen ab. Dagegen warnt der Lauf inzwischen selbst:
`check_token_expiry()` schickt ab `token_warn_days` (Standard 7) vor `token_expires`
**einmal täglich** eine Telegram-Warnung. Der Merker dafür steht in `state.json` unter
dem Sonderschlüssel `meta.token_warned` — ohne ihn käme die Warnung bei jedem der ~144
Läufe pro Tag. Er wird auch auf den Fehlerpfaden gesichert, damit ein gescheiterter
Abruf die Warnung nicht wiederholen lässt.

Beides ist in der Konsole einstellbar („Token läuft ab", „Vorwarnung (Tage)").
Leeres Datum = keine Warnung. Nach jeder Token-Erneuerung Datum aktualisieren.

Der Prüfaufruf steht **vor** dem Abruf, damit die Warnung auch rausgeht, wenn der
Club-Server gerade nicht erreichbar ist.
