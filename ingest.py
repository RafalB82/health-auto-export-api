"""
Parsowanie sparsowanego payloadu (schemas.HealthExportPayload) i zapis do SQLite.

Idempotencja: metryki i sesje snu mają UniqueConstraint w DB, więc powtórny
POST z tym samym (metric_name, date, source) robi UPDATE zamiast duplikować -
istotne przy Date Range "Since Last Sync", gdzie zdarzają się nakładające okna.
Workouty są kluczowane po natywnym UUID z Apple Health (id), więc powtórka
usuwa stare punkty HR/route i wstawia świeże (patrz upsert_workout).
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from dateutil import parser as dtparser
from sqlalchemy.orm import Session

from app.schemas import HealthExportPayload, WorkoutEntry
from app import db as models


def parse_dt(value: Optional[str]) -> Optional[datetime]:
    """Health Auto Export daje daty jako '2026-08-06 00:00:00 +0200'."""
    if not value:
        return None
    return dtparser.parse(value)


def upsert_metric_point(session: Session, metric_name: str, units: Optional[str],
                         point: dict, session_id: Optional[str]) -> None:
    date = parse_dt(point.get("date"))
    if date is None:
        return
    source = point.get("source") or None

    if "Avg" in point or "Min" in point or "Max" in point:
        existing = (
            session.query(models.MetricPointAgg)
            .filter_by(metric_name=metric_name, date=date, source=source)
            .one_or_none()
        )
        if existing is None:
            existing = models.MetricPointAgg(
                metric_name=metric_name, date=date, source=source
            )
            session.add(existing)
        existing.units = units
        existing.avg = point.get("Avg")
        existing.min = point.get("Min")
        existing.max = point.get("Max")
        existing.session_id = session_id
        return

    if "qty" in point:
        existing = (
            session.query(models.MetricPoint)
            .filter_by(metric_name=metric_name, date=date, source=source)
            .one_or_none()
        )
        if existing is None:
            existing = models.MetricPoint(
                metric_name=metric_name, date=date, source=source
            )
            session.add(existing)
        existing.units = units
        existing.qty = point["qty"]
        existing.session_id = session_id
        return

    # sleep_analysis trafia tu jako trzeci wariant kształtu
    if "sleepStart" in point or "totalSleep" in point:
        upsert_sleep_point(session, point, session_id)
        return

    # Nieznany kształt punktu - pomijamy zamiast wywalać cały request.
    # W produkcji warto to logować do osobnej tabeli "unparsed_points".


def upsert_sleep_point(session: Session, point: dict, session_id: Optional[str]) -> None:
    date = parse_dt(point.get("date"))
    sleep_start = parse_dt(point.get("sleepStart"))
    if date is None:
        return
    source = point.get("source") or None

    existing = (
        session.query(models.SleepSession)
        .filter_by(date=date, source=source, sleep_start=sleep_start)
        .one_or_none()
    )
    if existing is None:
        existing = models.SleepSession(date=date, source=source, sleep_start=sleep_start)
        session.add(existing)

    existing.sleep_end = parse_dt(point.get("sleepEnd"))
    existing.in_bed_start = parse_dt(point.get("inBedStart"))
    existing.in_bed_end = parse_dt(point.get("inBedEnd"))
    existing.in_bed = point.get("inBed")
    existing.total_sleep = point.get("totalSleep")
    existing.asleep = point.get("asleep")
    existing.awake = point.get("awake")
    existing.core = point.get("core")
    existing.deep = point.get("deep")
    existing.rem = point.get("rem")
    existing.session_id = session_id


def upsert_workout(session: Session, w: WorkoutEntry, session_id: Optional[str]) -> None:
    existing = session.query(models.Workout).filter_by(id=w.id).one_or_none()
    if existing is None:
        existing = models.Workout(id=w.id)
        session.add(existing)
    else:
        # Powtórny sync tego samego treningu: czyścimy stare serie czasowe,
        # bo mogą się różnić (np. inny time grouping) - wstawiamy świeże.
        session.query(models.WorkoutHeartRate).filter_by(workout_id=w.id).delete()
        session.query(models.WorkoutRoutePoint).filter_by(workout_id=w.id).delete()

    existing.name = w.name
    existing.location = w.location
    existing.is_indoor = w.isIndoor
    existing.start = parse_dt(w.start)
    existing.end = parse_dt(w.end)
    existing.duration_s = w.duration

    existing.active_energy_kj = w.activeEnergy.qty if w.activeEnergy else None
    existing.total_energy_kj = w.totalEnergy.qty if w.totalEnergy else None
    existing.distance_km = w.distance.qty if w.distance else None
    existing.avg_speed_kmh = w.avgSpeed.qty if w.avgSpeed else None
    existing.max_speed_kmh = w.maxSpeed.qty if w.maxSpeed else None
    existing.avg_heart_rate_bpm = w.avgHeartRate.qty if w.avgHeartRate else None
    existing.max_heart_rate_bpm = w.maxHeartRate.qty if w.maxHeartRate else None
    existing.step_count = w.stepCount.qty if w.stepCount else None
    existing.step_cadence = w.stepCadence.qty if w.stepCadence else None
    existing.flights_climbed = w.flightsClimbed.qty if w.flightsClimbed else None
    existing.intensity = w.intensity.qty if w.intensity else None
    existing.humidity_pct = w.humidity.qty if w.humidity else None
    existing.temperature_degc = w.temperature.qty if w.temperature else None
    existing.swim_cadence = w.swimCadence.qty if w.swimCadence else None
    existing.total_swimming_stroke_count = (
        w.totalSwimmingStrokeCount.qty if w.totalSwimmingStrokeCount else None
    )
    existing.elevation_ascent_m = w.elevation.ascent if w.elevation else None
    existing.elevation_descent_m = w.elevation.descent if w.elevation else None
    existing.session_id = session_id

    session.flush()  # potrzebne żeby workout_id istniał przed insertami dzieci

    for point in w.heartRateData:
        ts = parse_dt(point.get("date"))
        if ts is None or "qty" not in point:
            continue
        session.add(models.WorkoutHeartRate(
            workout_id=w.id, date=ts, qty=point["qty"],
            units=point.get("units"), kind="active",
        ))

    for point in w.heartRateRecovery:
        ts = parse_dt(point.get("date"))
        if ts is None or "qty" not in point:
            continue
        session.add(models.WorkoutHeartRate(
            workout_id=w.id, date=ts, qty=point["qty"],
            units=point.get("units"), kind="recovery",
        ))

    for point in w.route:
        ts = parse_dt(point.get("timestamp"))
        if ts is None or "lat" not in point or "lon" not in point:
            continue
        session.add(models.WorkoutRoutePoint(
            workout_id=w.id, timestamp=ts,
            lat=point["lat"], lon=point["lon"], altitude=point.get("altitude"),
        ))


def ingest_payload(session: Session, payload: HealthExportPayload,
                    session_id: Optional[str]) -> dict:
    """Zapisuje cały payload w jednej transakcji. Zwraca licznik dla response."""
    counts = {"metrics": 0, "metric_points": 0, "workouts": 0}

    for metric in payload.data.metrics:
        counts["metrics"] += 1
        for point in metric.data:
            upsert_metric_point(session, metric.name, metric.units, point, session_id)
            counts["metric_points"] += 1

    for workout in payload.data.workouts:
        upsert_workout(session, workout, session_id)
        counts["workouts"] += 1

    session.commit()
    return counts
