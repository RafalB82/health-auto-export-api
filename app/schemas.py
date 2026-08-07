"""
Modele Pydantic dla payloadu Health Auto Export (Export Version 2, JSON).

Trzy warianty elementu w metrics[].data[]:
  1. Standardowy punkt: {qty, date, source}          -> większość metryk
  2. Punkt agregowany:  {Avg, Min, Max, date, source} -> np. heart_rate przy Summarize ON
  3. Sesja snu:         {sleepStart, sleepEnd, core, deep, rem, ...} -> sleep_analysis

Nie da się tego opisać jednym sztywnym modelem bez utraty danych z dwóch pozostałych
wariantów, więc parsowanie idzie "po polach obecnych w dict" (patrz app/ingest.py),
a modele tutaj służą głównie do walidacji kształtu całego payloadu na wejściu.
"""
from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field, ConfigDict, field_validator


class MetricEntry(BaseModel):
    """Jeden element metrics[]. data zostaje jako list[dict] - rozgałęzienie
    na standard/agg/sleep dzieje się w ingest.py, bo klucze różnią się per metryka."""
    model_config = ConfigDict(extra="allow")

    name: str
    units: Optional[str] = None
    data: list[dict[str, Any]] = Field(default_factory=list)


class WorkoutQtyUnit(BaseModel):
    """Wspólny kształt skalarnych pól treningu: {qty, units}.

    Health Auto Export v2 wysyła czasem `qty` jako string (np. "11.17128")
    zamiast liczby - akceptujemy oba i normalizujemy do float."""
    model_config = ConfigDict(extra="allow")

    qty: Optional[float] = None
    units: Optional[str] = None

    @field_validator("qty", mode="before")
    @classmethod
    def _coerce_qty(cls, v):
        if v is None or isinstance(v, (int, float)):
            return v
        try:
            return float(str(v))
        except (TypeError, ValueError):
            return v  # zostaw - Pydantic i tak zgłosi błąd walidacji float

    @field_validator("units", mode="before")
    @classmethod
    def _coerce_units(cls, v):
        # Health Auto Export czasem wysyła units jako liczbę zamiast stringa
        if v is None or isinstance(v, str):
            return v
        return str(v)


class WorkoutElevation(BaseModel):
    model_config = ConfigDict(extra="allow")

    ascent: Optional[float] = None
    descent: Optional[float] = None
    units: Optional[str] = None

    @field_validator("ascent", "descent", mode="before")
    @classmethod
    def _coerce_elev(cls, v):
        if v is None or isinstance(v, (int, float)):
            return v
        try:
            return float(str(v))
        except (TypeError, ValueError):
            return v

    @field_validator("units", mode="before")
    @classmethod
    def _coerce_units(cls, v):
        if v is None or isinstance(v, str):
            return v
        return str(v)


class WorkoutEntry(BaseModel):
    """Jeden element workouts[]. extra='allow' bo Apple dodaje nowe pola
    między wersjami (np. swimCadence tylko przy pływaniu) - nie chcemy
    walić 422 na polu, którego jeszcze nie znamy."""
    model_config = ConfigDict(extra="allow")

    id: str
    name: Optional[str] = None
    location: Optional[str] = None
    isIndoor: Optional[bool] = None
    start: Optional[str] = None
    end: Optional[str] = None
    duration: Optional[float] = None

    activeEnergy: Optional[WorkoutQtyUnit] = None
    totalEnergy: Optional[WorkoutQtyUnit] = None
    distance: Optional[WorkoutQtyUnit] = None
    avgSpeed: Optional[WorkoutQtyUnit] = None
    maxSpeed: Optional[WorkoutQtyUnit] = None
    avgHeartRate: Optional[WorkoutQtyUnit] = None
    maxHeartRate: Optional[WorkoutQtyUnit] = None
    stepCount: Optional[WorkoutQtyUnit] = None
    stepCadence: Optional[WorkoutQtyUnit] = None
    flightsClimbed: Optional[WorkoutQtyUnit] = None
    intensity: Optional[WorkoutQtyUnit] = None
    humidity: Optional[WorkoutQtyUnit] = None
    temperature: Optional[WorkoutQtyUnit] = None
    swimCadence: Optional[WorkoutQtyUnit] = None
    totalSwimmingStrokeCount: Optional[WorkoutQtyUnit] = None

    elevation: Optional[WorkoutElevation] = None
    heartRateData: list[dict[str, Any]] = Field(default_factory=list)
    heartRateRecovery: list[dict[str, Any]] = Field(default_factory=list)
    route: list[dict[str, Any]] = Field(default_factory=list)


class HealthDataPayload(BaseModel):
    model_config = ConfigDict(extra="allow")

    metrics: list[MetricEntry] = Field(default_factory=list)
    workouts: list[WorkoutEntry] = Field(default_factory=list)


class HealthExportPayload(BaseModel):
    """Payload dokładnie tak, jak wysyła go Health Auto Export: {"data": {...}}."""
    model_config = ConfigDict(extra="allow")

    data: HealthDataPayload
