"""
SQLAlchemy engine, session i modele ORM dla danych z Health Auto Export.

Schemat:
- metric_points        -> pojedyncze punkty metryk typu qty (steps, HR pojedynczy, itd.)
- metric_points_agg     -> punkty agregowane Avg/Min/Max (np. heart_rate przy Summarize ON)
- sleep_sessions        -> sleep_analysis, osobna tabela bo inny kształt danych
- workouts              -> nagłówek treningu (skalary: dystans, HR avg/max, kalorie, itd.)
- workout_heart_rate    -> szereg czasowy HR w trakcie treningu
- workout_route         -> punkty GPS trasy (lat/lon/altitude/timestamp)
- raw_payloads          -> pełny surowy JSON każdego requestu (audyt / debug / reprocessing)
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    create_engine,
    Column,
    Integer,
    String,
    Float,
    DateTime,
    Text,
    ForeignKey,
    Boolean,
    UniqueConstraint,
    Index,
)
from sqlalchemy.orm import declarative_base, sessionmaker, relationship

import os
from pathlib import Path

# Baza SQLite. Domyślnie bezwzględna ścieżka względem katalogu projektu
# (parent katalogu zawierającego ten plik), żeby webhook i MCP server zawsze
# wskazywały na TEN SAM plik, niezależnie od katalogu roboczego procesu
# (OpenClaw startuje MCP z innego cwd). Można nadpisać przez DATABASE_URL.
_DB_DIR = Path(__file__).resolve().parent.parent  # health-auto-export-api/
DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    (
        "sqlite:///" + str(_DB_DIR / "health_data.db").replace("\\", "/")
        if os.name != "nt"
        else "sqlite:///" + str(_DB_DIR / "health_data.db").replace("\\", "/")
    ),
)

# check_same_thread=False bo FastAPI może odpalać handlery w różnych wątkach;
# przy SQLite pod jednym procesem to bezpieczne dopóki nie robimy współbieżnych write'ów
# z wielu procesów.
engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False},
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


class RawPayload(Base):
    """Metadane surowego payloadu każdego requestu - sam JSON leży na dysku
    jako plik .json.gz (patrz app/raw_storage.py), tutaj tylko wskaźnik + audyt.
    Rozdzielenie od SQLite: przy ~100MB/dzień surowych danych trzymanie tego
    jako BLOB w bazie degraduje wydajność insertów i przyspiesza zużycie karty SD."""
    __tablename__ = "raw_payloads"

    id = Column(Integer, primary_key=True)
    received_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    automation_name = Column(String, nullable=True)
    automation_id = Column(String, nullable=True)
    automation_period = Column(String, nullable=True)
    automation_aggregation = Column(String, nullable=True)
    session_id = Column(String, nullable=True, index=True)
    file_path = Column(String, nullable=False)  # ścieżka względna do pliku .json.gz na dysku
    size_bytes_raw = Column(Integer, nullable=True)      # rozmiar przed kompresją
    size_bytes_compressed = Column(Integer, nullable=True)  # rozmiar pliku .gz na dysku


class MetricPoint(Base):
    """Pojedynczy punkt metryki typu {qty, date, source} - większość metryk."""
    __tablename__ = "metric_points"
    __table_args__ = (
        UniqueConstraint("metric_name", "date", "source", name="uq_metric_point"),
        Index("ix_metric_points_name_date", "metric_name", "date"),
    )

    id = Column(Integer, primary_key=True)
    metric_name = Column(String, nullable=False)
    units = Column(String, nullable=True)
    date = Column(DateTime, nullable=False)
    source = Column(String, nullable=True)
    qty = Column(Float, nullable=False)
    session_id = Column(String, nullable=True)


class MetricPointAgg(Base):
    """Punkt metryki zagregowanej {Avg, Min, Max, date, source} - np. heart_rate."""
    __tablename__ = "metric_points_agg"
    __table_args__ = (
        UniqueConstraint("metric_name", "date", "source", name="uq_metric_point_agg"),
        Index("ix_metric_points_agg_name_date", "metric_name", "date"),
    )

    id = Column(Integer, primary_key=True)
    metric_name = Column(String, nullable=False)
    units = Column(String, nullable=True)
    date = Column(DateTime, nullable=False)
    source = Column(String, nullable=True)
    avg = Column(Float, nullable=True)
    min = Column(Float, nullable=True)
    max = Column(Float, nullable=True)
    sample_count = Column(Integer, nullable=True)  # ile surowych próbek zagregowano do tego wiersza
    session_id = Column(String, nullable=True)


class SleepSession(Base):
    """sleep_analysis - osobny kształt: fazy snu zamiast qty."""
    __tablename__ = "sleep_sessions"
    __table_args__ = (
        UniqueConstraint("date", "source", "sleep_start", name="uq_sleep_session"),
    )

    id = Column(Integer, primary_key=True)
    date = Column(DateTime, nullable=False)
    source = Column(String, nullable=True)
    sleep_start = Column(DateTime, nullable=True)
    sleep_end = Column(DateTime, nullable=True)
    in_bed_start = Column(DateTime, nullable=True)
    in_bed_end = Column(DateTime, nullable=True)
    in_bed = Column(Float, nullable=True)
    total_sleep = Column(Float, nullable=True)
    asleep = Column(Float, nullable=True)
    awake = Column(Float, nullable=True)
    core = Column(Float, nullable=True)
    deep = Column(Float, nullable=True)
    rem = Column(Float, nullable=True)
    session_id = Column(String, nullable=True)


class Workout(Base):
    """Nagłówek treningu - wszystkie skalarne pola {qty, units}."""
    __tablename__ = "workouts"

    id = Column(String, primary_key=True)  # UUID z Apple Health, naturalnie unikalny
    name = Column(String, nullable=True)
    location = Column(String, nullable=True)
    is_indoor = Column(Boolean, nullable=True)
    start = Column(DateTime, nullable=True)
    end = Column(DateTime, nullable=True)
    duration_s = Column(Float, nullable=True)

    active_energy_kj = Column(Float, nullable=True)
    total_energy_kj = Column(Float, nullable=True)
    distance_km = Column(Float, nullable=True)
    avg_speed_kmh = Column(Float, nullable=True)
    max_speed_kmh = Column(Float, nullable=True)
    avg_heart_rate_bpm = Column(Float, nullable=True)
    max_heart_rate_bpm = Column(Float, nullable=True)
    step_count = Column(Float, nullable=True)
    step_cadence = Column(Float, nullable=True)
    flights_climbed = Column(Float, nullable=True)
    intensity = Column(Float, nullable=True)
    humidity_pct = Column(Float, nullable=True)
    temperature_degc = Column(Float, nullable=True)
    elevation_ascent_m = Column(Float, nullable=True)
    elevation_descent_m = Column(Float, nullable=True)
    swim_cadence = Column(Float, nullable=True)
    total_swimming_stroke_count = Column(Float, nullable=True)

    session_id = Column(String, nullable=True)
    received_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    raw_payload_id = Column(Integer, ForeignKey("raw_payloads.id"), nullable=True)
    # ^ link do pliku źródłowego (patrz app/raw_storage.py) ustawiany w ingest.py
    # w momencie przetwarzania - unika zgadywania "który plik z tego dnia" przy
    # odczycie sekunda-po-sekundzie (get_workout_hr_trace w app/query.py).

    heart_rate_points = relationship(
        "WorkoutHeartRate", back_populates="workout", cascade="all, delete-orphan"
    )
    route_points = relationship(
        "WorkoutRoutePoint", back_populates="workout", cascade="all, delete-orphan"
    )
    energy_points = relationship(
        "WorkoutEnergy", back_populates="workout", cascade="all, delete-orphan"
    )
    step_points = relationship(
        "WorkoutStep", back_populates="workout", cascade="all, delete-orphan"
    )


class WorkoutHeartRate(Base):
    """Szereg czasowy HR w trakcie treningu (heartRateData / heartRateRecovery)."""
    __tablename__ = "workout_heart_rate"
    __table_args__ = (
        UniqueConstraint("workout_id", "date", "kind", name="uq_workout_hr_minute"),
        Index("ix_workout_hr_workout_date", "workout_id", "date"),
    )

    id = Column(Integer, primary_key=True)
    workout_id = Column(String, ForeignKey("workouts.id"), nullable=False)
    date = Column(DateTime, nullable=False)
    qty = Column(Float, nullable=False)
    min_qty = Column(Float, nullable=True)
    max_qty = Column(Float, nullable=True)
    sample_count = Column(Integer, nullable=True)  # ile surowych próbek zagregowano do tej minuty
    units = Column(String, nullable=True)
    kind = Column(String, nullable=False, default="active")  # "active" lub "recovery"

    workout = relationship("Workout", back_populates="heart_rate_points")


class WorkoutRoutePoint(Base):
    """Punkt trasy GPS treningu."""
    __tablename__ = "workout_route"
    __table_args__ = (
        Index("ix_workout_route_workout_ts", "workout_id", "timestamp"),
    )

    id = Column(Integer, primary_key=True)
    workout_id = Column(String, ForeignKey("workouts.id"), nullable=False)
    timestamp = Column(DateTime, nullable=False)
    lat = Column(Float, nullable=False)
    lon = Column(Float, nullable=False)
    altitude = Column(Float, nullable=True)

    workout = relationship("Workout", back_populates="route_points")


class WorkoutEnergy(Base):
    """Seria czasowa activeEnergy treningu - 1 wiersz/minutę (kJ).
    Rośnie naturalnie w trakcie dnia: kolejne sync'e dodają nowe minuty
    (upsert po (workout_id, date)), zamiast delete+insert całości."""
    __tablename__ = "workout_energy"
    __table_args__ = (
        UniqueConstraint("workout_id", "date", name="uq_workout_energy_minute"),
        Index("ix_workout_energy_workout_date", "workout_id", "date"),
    )

    id = Column(Integer, primary_key=True)
    workout_id = Column(String, ForeignKey("workouts.id"), nullable=False)
    date = Column(DateTime, nullable=False)
    qty = Column(Float, nullable=False)
    sample_count = Column(Integer, nullable=True)
    units = Column(String, nullable=True)

    workout = relationship("Workout", back_populates="energy_points")


class WorkoutStep(Base):
    """Seria czasowa stepCount treningu - 1 wiersz/minutę (steps).
    Analogicznie do WorkoutEnergy - rośnie naturalnie w ciągu dnia."""
    __tablename__ = "workout_steps"
    __table_args__ = (
        UniqueConstraint("workout_id", "date", name="uq_workout_step_minute"),
        Index("ix_workout_step_workout_date", "workout_id", "date"),
    )

    id = Column(Integer, primary_key=True)
    workout_id = Column(String, ForeignKey("workouts.id"), nullable=False)
    date = Column(DateTime, nullable=False)
    qty = Column(Float, nullable=False)
    sample_count = Column(Integer, nullable=True)
    units = Column(String, nullable=True)

    workout = relationship("Workout", back_populates="step_points")


def init_db() -> None:
    Base.metadata.create_all(bind=engine)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
