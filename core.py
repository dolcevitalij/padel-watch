"""
Kernlogik: aus der Matchpoint-Response (belegte Slots) die FREIEN
buchbaren Startzeiten berechnen und gegen die Regeln filtern.

Bewusst netzwerkfrei gehalten, damit die Logik isoliert testbar ist.
"""
from __future__ import annotations
from dataclasses import dataclass


def hhmm_to_min(s: str) -> int:
    """'18:30' -> 1110 (Minuten seit Mitternacht)."""
    h, m = s.split(":")
    return int(h) * 60 + int(m)


def min_to_hhmm(x: int) -> str:
    return f"{x // 60:02d}:{x % 60:02d}"


def merge_intervals(intervals: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Überlappende/angrenzende [start, end)-Intervalle zusammenfassen."""
    if not intervals:
        return []
    intervals = sorted(intervals)
    merged = [list(intervals[0])]
    for s, e in intervals[1:]:
        if s <= merged[-1][1]:            # überlappt oder grenzt an
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    return [(s, e) for s, e in merged]


@dataclass
class Court:
    court_id: str
    name: str
    occupied: list[tuple[int, int]]       # gemergte belegte Intervalle (Min)
    modalidad: int | None = None          # fuer den Buchungs-Link (fetch_slot_options)


def parse_courts(payload: dict) -> tuple[list[Court], dict]:
    """
    Matchpoint-Response -> Liste von Courts mit gemergten Belegungen.
    Gibt zusätzlich Meta-Infos zurück (Datum, aktuelle Uhrzeit, Raster).
    """
    d = payload.get("d", payload)
    meta = {
        "fecha": d.get("StrFecha"),                       # '28/07/2026'
        "now_min": hhmm_to_min(d.get("StrHoraActualDelCentro", "00:00")),
        "partes_por_hora": d.get("PartesPorHora", 2),     # 2 -> 30-Min-Raster
    }
    courts: list[Court] = []
    for col in d.get("Columnas", []):
        occ = []
        for o in col.get("Ocupaciones", []):
            try:
                occ.append((hhmm_to_min(o["StrHoraInicio"]),
                            hhmm_to_min(o["StrHoraFin"])))
            except (KeyError, ValueError):
                continue
        courts.append(Court(
            court_id=str(col.get("Id")),
            name=col.get("TextoPrincipal", "?"),
            occupied=merge_intervals(occ),
            modalidad=col.get("IdModalidadFijaParaReservas"),
        ))
    return courts, meta


def is_free_block(start: int, dur: int, occupied: list[tuple[int, int]]) -> bool:
    """Ist [start, start+dur) komplett frei (keine Überlappung)?"""
    end = start + dur
    for os_, oe in occupied:
        if start < oe and end > os_:      # Überlappung
            return False
    return True


def free_starts_for_court(
    court: Court,
    win_start: int, win_end: int,        # gewünschtes Zeitfenster (Min)
    duration: int,                        # gewünschte Spieldauer (Min)
    step: int,                            # Raster (z.B. 30 Min)
    not_before: int = 0,                  # frühestmögliche Startzeit (heute)
) -> list[int]:
    """
    Alle gerasterten Startzeiten t, bei denen ein Block der Länge `duration`
    KOMPLETT frei ist und vollständig im Wunschfenster [win_start, win_end] liegt.
    """
    result = []
    t = win_start
    # auf Raster ausrichten
    if t % step != 0:
        t += step - (t % step)
    while t + duration <= win_end:
        if t >= not_before and is_free_block(t, duration, court.occupied):
            result.append(t)
        t += step
    return result


def find_matches(
    payload: dict,
    *,
    court_ids: list[str] | None,          # None = alle Courts
    win_start_hhmm: str,
    win_end_hhmm: str,
    duration_min: int,
    day_open_hhmm: str = "07:00",
    day_close_hhmm: str = "23:00",
    is_today: bool = False,
) -> list[dict]:
    """
    Liefert alle Treffer für EINEN Tag (eine Response) als Liste von
    {court_id, court_name, start ('HH:MM'), end ('HH:MM')}.
    """
    courts, meta = parse_courts(payload)
    step = 60 // meta["partes_por_hora"]  # PartesPorHora=2 -> 30

    win_s = max(hhmm_to_min(win_start_hhmm), hhmm_to_min(day_open_hhmm))
    win_e = min(hhmm_to_min(win_end_hhmm), hhmm_to_min(day_close_hhmm))
    not_before = meta["now_min"] if is_today else 0

    matches = []
    for c in courts:
        if court_ids and c.court_id not in court_ids:
            continue
        for t in free_starts_for_court(c, win_s, win_e, duration_min, step, not_before):
            matches.append({
                "court_id": c.court_id,
                "court_name": c.name,
                "modalidad": c.modalidad,
                "start": min_to_hhmm(t),
                "end": min_to_hhmm(t + duration_min),
                "start_min": t,
                "dur": duration_min,
                "step": step,
            })
    return matches
