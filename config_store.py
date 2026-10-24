"""
Speicherort der Konfiguration fuer die Web-UI: lokale Datei oder direkt das Repo.

Datei-Modus (Standard): liest und schreibt config.yaml neben webapp.py, wie
bisher. Veroeffentlicht wird danach mit publish-config.bat.

GitHub-Modus (GITHUB_CONFIG_TOKEN + GITHUB_REPO gesetzt): liest und committet
config.yaml ueber die Contents-API. Noetig fuer den Online-Betrieb auf Render -
dort ist das Dateisystem fluechtig, und eine Datei auf dem Server erreicht den
GitHub-Lauf ohnehin nie.

Gegen gleichzeitige Aenderungen schuetzt der sha der Datei: der Browser bekommt
ihn beim Laden mit (Header X-Config-Version) und schickt ihn beim Speichern
zurueck. Bewusst nicht im Server gemerkt - Render startet den Prozess nach jedem
Einschlafen neu, der Merker waere zwischen Laden und Speichern weg. Der sha gilt
pro Datei, die state.json-Commits des Bots stoeren also nicht.
"""
from __future__ import annotations
import base64
import datetime as dt
import os
import threading
import time

import requests
import yaml

from padel_watch import load_yaml, scrub

API = "https://api.github.com"
# Vorschau-Aufrufe lesen die Konfiguration bei jedem Klick; der Seitenaufbau
# liest dagegen immer frisch
CACHE_SECONDS = 60


class ConfigConflict(Exception):
    """config.yaml wurde seit dem Laden anderweitig geaendert."""


def dump_yaml(cfg: dict) -> str:
    return yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False)


class FileStore:
    mode = "lokal"
    token_expires: str | None = None

    def __init__(self, path: str):
        self.path = path

    def load(self) -> dict:
        return load_yaml(self.path)      # UTF-8 mit Rueckfall auf cp1252

    def load_versioned(self) -> tuple[dict, str | None]:
        return self.load(), None

    def save(self, cfg: dict, version: str | None) -> dict:
        # encoding immer explizit: unter Windows waehlt open() sonst cp1252, und
        # Umlaute in Clubnamen ("HOP WOLFENBÜTTEL") machen die Datei fuer den
        # Produktivlauf unter Linux unlesbar (UnicodeDecodeError vor jeder Meldung).
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write(dump_yaml(cfg))
        return {}


class GitHubStore:
    mode = "github"

    def __init__(self, repo: str, token: str, path: str = "config.yaml",
                 branch: str = "main"):
        self.url = f"{API}/repos/{repo}/contents/{path}"
        self.token = token
        self.branch = branch
        self.token_expires: str | None = None    # ISO-Zeitpunkt, aus GitHubs Antwort
        self._lock = threading.Lock()
        self._cache: tuple[float, dict, str] | None = None

    def _headers(self) -> dict:
        return {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self.token}",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def _clean(self, value: object) -> str:
        """Token aus Fehlermeldungen entfernen - die landen in der UI."""
        return scrub(str(value).replace(self.token, "<GITHUB_CONFIG_TOKEN>"))

    def _note_expiry(self, res: requests.Response) -> None:
        # GitHub schickt das Ablaufdatum eines Fine-grained Tokens bei jeder
        # Antwort mit - so muss es niemand von Hand pflegen
        raw = res.headers.get("github-authentication-token-expiration")
        if not raw:
            return
        try:
            when = dt.datetime.strptime(raw, "%Y-%m-%d %H:%M:%S %Z")
            self.token_expires = when.replace(tzinfo=dt.timezone.utc).isoformat()
        except ValueError:
            pass

    def _fail(self, what: str, res: requests.Response) -> RuntimeError:
        try:
            detail = res.json().get("message", "")
        except ValueError:
            detail = res.text[:200]
        return RuntimeError(self._clean(f"GitHub {what}: HTTP {res.status_code} {detail}"))

    def load_versioned(self) -> tuple[dict, str]:
        try:
            res = requests.get(self.url, params={"ref": self.branch},
                               headers=self._headers(), timeout=20)
        except requests.RequestException as e:
            raise RuntimeError(self._clean(f"GitHub nicht erreichbar: {e}")) from None
        self._note_expiry(res)
        if res.status_code != 200:
            raise self._fail("lesen", res)
        data = res.json()
        cfg = yaml.safe_load(base64.b64decode(data["content"]).decode("utf-8-sig"))
        with self._lock:
            self._cache = (time.monotonic(), cfg, data["sha"])
        return cfg, data["sha"]

    def load(self) -> dict:
        with self._lock:
            if self._cache and time.monotonic() - self._cache[0] < CACHE_SECONDS:
                return self._cache[1]
        return self.load_versioned()[0]

    def save(self, cfg: dict, version: str | None) -> dict:
        if not version:
            # Ohne sha wuerde GitHub die Datei blind ersetzen
            raise ConfigConflict("Unbekannt, gegen welchen Stand gespeichert wird. "
                                 "Bitte die Seite neu laden.")
        body = {
            "message": "Konfiguration aktualisiert (Web)",
            "content": base64.b64encode(dump_yaml(cfg).encode("utf-8")).decode("ascii"),
            "sha": version,
            "branch": self.branch,
        }
        try:
            res = requests.put(self.url, json=body, headers=self._headers(), timeout=30)
        except requests.RequestException as e:
            raise RuntimeError(self._clean(f"GitHub nicht erreichbar: {e}")) from None
        self._note_expiry(res)
        if res.status_code == 409:
            raise ConfigConflict("config.yaml wurde inzwischen anderweitig geändert. "
                                 "Bitte die Seite neu laden - deine Änderungen hier "
                                 "gehen dabei verloren, also vorher notieren.")
        if res.status_code not in (200, 201):
            raise self._fail("speichern", res)
        data = res.json()
        sha = data["content"]["sha"]
        with self._lock:
            self._cache = (time.monotonic(), cfg, sha)
        return {"version": sha, "commit": data["commit"]["sha"][:7]}


def from_env(path: str) -> FileStore | GitHubStore:
    token = os.environ.get("GITHUB_CONFIG_TOKEN")
    repo = os.environ.get("GITHUB_REPO")
    if token and repo:
        return GitHubStore(repo, token)
    return FileStore(path)
