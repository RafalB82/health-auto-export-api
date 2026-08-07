"""
Parsowanie sparsowanego payloadu (schemas.HealthExportPayload) i zapis do SQLite.

Idempotencja: metryki i sesje snu mają UniqueConstraint w DB, więc powtórny
POST z tym samym (metric_name, date, source) robi UPDATE zamiast duplikować -
istotne przy Date Range "Since Last Sync", gdzie zdarzają się nakładające okna.
Workouty są kluczowane po natywnym UUID z Apple Health (id), więc powtórka
usuwa stare punkty HR/route i wstawia świeże (patrz upsert_workout).

Agregacja minutowa: metryki z HIGH_GRANULARITY_METRICS (domyślnie heart_rate)
są redukowane do 1 wiersza/minutę (avg/min/max/count) zamiast insertu per
surowy punkt. Przy próbkowaniu HR co 1-5s to redukcja rzędu 10-60x rozmiaru
tabeli/insertów, bez utraty danych źródłowych - pełny surowy JSON zawsze
ląduje w raw_payloads.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Optional

from dateutil import parser as dtparser
from sqlalchemy.orm import Session

from app.schemas import HealthExportPayload, WorkoutEntry
from app import db as models

# Metryki, dla których punkty grupujemy do 1 wiersza/minutę zamiast zapisywać
# każdy surowy punkt osobno. Dopisz kolejne nazwy (np. "blood_oxygen_saturation"),
# jeśli apka zacznie je eksportować z podobną granularnością.
HIGH_GRANULARITY_METRICS = {"heart_rate"}


def parse_dt(value: Optional[str]) -> Optional[datetime]:
    """Health Auto Export daje daty jako '2026-08-06 00:00:00 +0200'."""
    if not value:
        return None
    return dtparser.parse(value)


def floor_to_minute(dt: datetime) -> datetime:
    return dt.replace(second=0, microsecond=0)


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


def upsert_metric_minute_bucket(session: Session, metric_name: str, units: Optional[str],
                                 source: Optional[str], minute: datetime,
                                 values: list[float], session_id: Optional[str]) -> None:
    """Zapisuje jeden zagregowany wiersz (avg/min/max/count) dla minuty do MetricPointAgg."""
    existing = (
        session.query(models.MetricPointAgg)
        .filter_by(metric_name=metric_name, date=minute, source=source)
        .one_or_none()
    )
    if existing is None:
        existing = models.MetricPointAgg(
            metric_name=metric_name, date=minute, source=source
        )
        session.add(existing)
    existing.units = units
    existing.avg = sum(values) / len(values)
    existing.min = min(values)
    existing.max = max(values)
    existing.sample_count = len(values)
    existing.session_id = session_id


def ingest_metric_high_granularity(session: Session, metric_name: str, units: Optional[str],
                                    points: list[dict], session_id: Optional[str]) -> int:
    """Grupuje surowe punkty {qty|Avg, date, source} do 1 wiersza/minutę/source.

    Obsługuje oba warianty wejściowe (qty oraz Avg z ewentualnym Summarize ON w apce) -
    w obu przypadkach liczbą reprezentatywną do agregacji jest wartość Avg/qty.
    Zwraca liczbę zapisanych (zagregowanych) wierszy.
    """
    buckets: dict[tuple[Optional[str], datetime], list[float]] = defaultdict(list)

    for point in points:
        date = parse_dt(point.get("date"))
        if date is None:
            continue
        value = point.get("qty", point.get("Avg"))
        if value is None:
            continue
        source = point.get("source") or None
        minute = floor_to_minute(date)
        buckets[(source, minute)].append(float(value))

    for (source, minute), values in buckets.items():
        upsert_metric_minute_bucket(session, metric_name, units, source, minute, values, session_id)

    return len(buckets)


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


def ingest_workout_heart_rate_bucketed(session: Session, workout_id: str,
                                        points: list[dict], kind: str) -> int:
    """Agreguje HR treningowy (1097 punktów/88min w typowej jeździe) do 1 wiersza/minutę.
    kind="active" lub "recovery" - osobne kubełki, bo to różne fazy pomiaru."""
    buckets: dict[datetime, list[float]] = defaultdict(list)
    units_seen: Optional[str] = None

    for point in points:
        ts = parse_dt(point.get("date"))
        if ts is None or "qty" not in point:
            continue
        minute = floor_to_minute(ts)
        buckets[minute].append(float(point["qty"]))
        units_seen = point.get("units") or units_seen

    for minute, values in buckets.items():
        existing = (
            session.query(models.WorkoutHeartRate)
            .filter_by(workout_id=workout_id, date=minute, kind=kind)
            .one_or_none()
        )
        if existing is None:
            existing = models.WorkoutHeartRate(workout_id=workout_id, date=minute, kind=kind)
            session.add(existing)
        existing.qty = sum(values) / len(values)
        existing.min_qty = min(values)
        existing.max_qty = max(values)
        existing.sample_count = len(values)
        existing.units = units_seen

    return len(buckets)


def upsert_workout(session: Session, w: WorkoutEntry, session_id: Optional[str],
                    raw_payload_id: Optional[int]) -> None:
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
    existing.raw_payload_id = raw_payload_id

    session.flush()  # potrzebne żeby workout_id istniał przed insertami dzieci

    ingest_workout_heart_rate_bucketed(session, w.id, w.heartRateData, kind="active")
    ingest_workout_heart_rate_bucketed(session, w.id, w.heartRateRecovery, kind="recovery")

    for point in w.route:
        ts = parse_dt(point.get("timestamp"))
        if ts is None or "lat" not in point or "lon" not in point:
            continue
        session.add(models.WorkoutRoutePoint(
            workout_id=w.id, timestamp=ts,
            lat=point["lat"], lon=point["lon"], altitude=point.get("altitude"),
        ))


def ingest_payload(session: Session, payload: HealthExportPayload,
                    session_id: Optional[str], raw_payload_id: Optional[int] = None) -> dict:
    """Zapisuje cały payload w jednej transakcji. Zwraca licznik dla response.

    Commit zostawiany wywołującemu (main.py) - bo raw_payload_id musi być
    ustawione po zapisie RawPayload (db.flush()), zanim workouty dostaną
    link do pliku źródłowego.
    """
    counts = {"metrics": 0, "metric_points": 0, "workouts": 0}

    for metric in payload.data.metrics:
        counts["metrics"] += 1
        if metric.name in HIGH_GRANULARITY_METRICS:
            counts["metric_points"] += ingest_metric_high_granularity(
                session, metric.name, metric.units, metric.data, session_id
            )
        else:
            for point in metric.data:
                upsert_metric_point(session, metric.name, metric.units, point, session_id)
                counts["metric_points"] += 1

    for workout in payload.data.workouts:
        upsert_workout(session, workout, session_id, raw_payload_id)
        counts["workouts"] += 1

    return counts
