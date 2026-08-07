"""
Warstwa zapytań dla MCP/API. Dwie kategorie, świadomie rozdzielone:

1. Zapytania agregujące (get_workout_summary, get_hr_series_minutely, ...)
   -> zawsze SQLite (metric_points_agg / workout_heart_rate), szybkie,
      bo to małe zindeksowane tabele. To ma być domyślna ścieżka dla MCP.

2. Zapytania o surowe dane (get_workout_hr_trace)
   -> dekompresja konkretnego pliku .json.gz na żądanie. Wolniejsze
      (odczyt + gunzip + parse pliku, który dla długich treningów z GPS
      może mieć kilka-kilkanaście MB), więc to ma być świadomy, osobny
      tool call w MCP, nie coś wołane przy każdym pytaniu o trening.

get_workout_hr_trace() nie zgaduje pliku po dacie treningu - używa
Workout.raw_payload_id, ustawianego w ingest.py w momencie zapisu,
więc trafienie w plik jest gwarantowane (o ile trening był w ogóle
zapisany z działającym linkiem - patrz obsługa raw_payload_id=None
dla danych zaimportowanych przed wprowadzeniem tej kolumny).
"""
from __future__ import annotations

import json
from typing import Optional

from sqlalchemy.orm import Session

from app import db as models
from app.raw_storage import load_raw_payload


class WorkoutNotFoundError(Exception):
    pass


class RawPayloadUnavailableError(Exception):
    """Trening istnieje w bazie, ale nie ma linku do pliku źródłowego
    (np. zaimportowany przed dodaniem kolumny raw_payload_id) albo plik
    zniknął z dysku (ręcznie przeniesiony/skasowany)."""
    pass


def get_workouts_deduped(session: Session, limit: int = 20) -> list[dict]:
    """Lista ostatnich treningów Z deduplikacją po (data, nazwa, czas startu).

    Health Auto Export może wysyłać ten sam fizyczny wysiłek 2x (zegarek + Hevy /
    iPhone), z różnym UUID i lekko różnym duration/source. Funkcja grupuje wpisy
    po (data, nazwa, czas startu do minuty) i zwraca jednego reprezentanta
    (najdłuższy zapis), oznaczając liczbę zduplikowanych wpisów. Baza pozostaje
    wierna - to tylko warstwa odczytu.
    """
    import collections
    ws = session.query(models.Workout).order_by(models.Workout.start.desc()).all()
    groups = collections.defaultdict(list)
    for w in ws:
        key = (w.start.strftime('%Y-%m-%d %H:%M'), w.name) if w.start else (str(w.id), w.name)
        groups[key].append(w)

    result = []
    for key, items in groups.items():
        rep = max(items, key=lambda w: (w.duration_s or 0))
        result.append({
            "id": rep.id,
            "name": rep.name,
            "start": rep.start.isoformat() if rep.start else None,
            "end": rep.end.isoformat() if rep.end else None,
            "duration_s": rep.duration_s,
            "duration_min": round((rep.duration_s or 0) / 60, 1),
            "distance_km": rep.distance_km,
            "avg_heart_rate_bpm": round(rep.avg_heart_rate_bpm, 1) if rep.avg_heart_rate_bpm else None,
            "max_heart_rate_bpm": rep.max_heart_rate_bpm,
            "active_energy_kj": round(rep.active_energy_kj, 1) if rep.active_energy_kj else None,
            "step_count": rep.step_count,
            "deduped_copies": len(items),
        })

    result.sort(key=lambda r: (r["start"] or ""), reverse=True)
    return result[:limit]



def get_workout_summary(session: Session, workout_id: str) -> dict:
    """Zagregowane dane treningu z SQLite - szybkie, do typowych pytań
    ('jaki był średni/max HR', 'ile km', itd.). Preferowana ścieżka dla MCP."""
    w = session.query(models.Workout).filter_by(id=workout_id).one_or_none()
    if w is None:
        raise WorkoutNotFoundError(f"Brak treningu o id={workout_id}")

    return {
        "id": w.id,
        "name": w.name,
        "start": w.start.isoformat() if w.start else None,
        "end": w.end.isoformat() if w.end else None,
        "duration_s": w.duration_s,
        "distance_km": w.distance_km,
        "avg_speed_kmh": w.avg_speed_kmh,
        "max_speed_kmh": w.max_speed_kmh,
        "avg_heart_rate_bpm": w.avg_heart_rate_bpm,
        "max_heart_rate_bpm": w.max_heart_rate_bpm,
        "elevation_ascent_m": w.elevation_ascent_m,
        "elevation_descent_m": w.elevation_descent_m,
    }


def get_workout_hr_minutely(session: Session, workout_id: str) -> list[dict]:
    """HR treningu zagregowany do 1 punktu/minutę (avg/min/max/sample_count) -
    domyślna granulacja do wykresów, tania do odczytu (SQLite, zindeksowane)."""
    rows = (
        session.query(models.WorkoutHeartRate)
        .filter_by(workout_id=workout_id, kind="active")
        .order_by(models.WorkoutHeartRate.date)
        .all()
    )
    if not rows:
        # Rozróżniamy "trening nie istnieje" od "istnieje, ale brak punktów HR"
        if session.query(models.Workout).filter_by(id=workout_id).one_or_none() is None:
            raise WorkoutNotFoundError(f"Brak treningu o id={workout_id}")

    return [
        {
            "minute": r.date.isoformat(),
            "avg_bpm": r.qty,
            "min_bpm": r.min_qty,
            "max_bpm": r.max_qty,
            "sample_count": r.sample_count,
        }
        for r in rows
    ]


def get_workout_hr_trace(session: Session, workout_id: str) -> list[dict]:
    """Pełny szereg czasowy HR sekunda-po-sekundzie, wprost z surowego payloadu.

    Kroki:
      1. Znajdź Workout w bazie -> pobierz raw_payload_id
      2. Wczytaj RawPayload -> file_path na dysku
      3. load_raw_payload() -> dekompresja .json.gz -> surowy JSON (bytes)
      4. Sparsuj JSON, znajdź ten konkretny workout po id w data.workouts[]
      5. Zwróć jego heartRateData[] bez agregacji - dokładnie tak, jak
         wysłała to aplikacja (typowo próbkowanie co 1-5s)

    To jest świadomie wolniejsza ścieżka niż get_workout_hr_minutely - czyta
    i dekompresuje cały plik payloadu (może być kilka-kilkanaście MB przy
    treningu z pełną trasą GPS), więc używaj jej tylko gdy faktycznie
    potrzebna jest pełna rozdzielczość (np. analiza zmienności HR sekunda
    po sekundzie), nie do zwykłych podsumowań.
    """
    w = session.query(models.Workout).filter_by(id=workout_id).one_or_none()
    if w is None:
        raise WorkoutNotFoundError(f"Brak treningu o id={workout_id}")

    if w.raw_payload_id is None:
        raise RawPayloadUnavailableError(
            f"Trening {workout_id} nie ma linku do surowego payloadu "
            "(zaimportowany przed wprowadzeniem raw_payload_id?)"
        )

    raw_payload_row = (
        session.query(models.RawPayload).filter_by(id=w.raw_payload_id).one_or_none()
    )
    if raw_payload_row is None:
        raise RawPayloadUnavailableError(
            f"Trening {workout_id} wskazuje na raw_payload_id={w.raw_payload_id}, "
            "ale ten wpis nie istnieje w bazie."
        )

    try:
        raw_bytes = load_raw_payload(raw_payload_row.file_path)
    except FileNotFoundError:
        raise RawPayloadUnavailableError(
            f"Plik {raw_payload_row.file_path} nie istnieje na dysku "
            "(przeniesiony lub skasowany ręcznie po zapisie)."
        )

    raw_json = json.loads(raw_bytes)
    workouts_in_payload = raw_json.get("data", {}).get("workouts", [])

    matching = next((wo for wo in workouts_in_payload if wo.get("id") == workout_id), None)
    if matching is None:
        raise RawPayloadUnavailableError(
            f"Plik {raw_payload_row.file_path} nie zawiera treningu {workout_id} "
            "(nieoczekiwane - link w bazie wskazuje na zły plik?)"
        )

    return matching.get("heartRateData", [])
