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

import collections
import json
from typing import Optional

from sqlalchemy.orm import Session

from app import db as models
from app.raw_storage import load_raw_payload
from app.db import SessionLocal


class WorkoutNotFoundError(Exception):
    pass


class RawPayloadUnavailableError(Exception):
    """Trening istnieje w bazie, ale nie ma linku do pliku źródłowego
    (np. zaimportowany przed dodaniem kolumny raw_payload_id) albo plik
    zniknął z dysku (ręcznie przeniesiony/skasowany)."""
    pass


def get_workouts_deduped(session: Session, limit: int = 20,
                         start_date: Optional[str] = None,
                         end_date: Optional[str] = None) -> list[dict]:
    """Lista ostatnich treningów Z deduplikacją po (data, nazwa, czas startu).

    Health Auto Export może wysyłać ten sam fizyczny wysiłek 2x (zegarek + Hevy /
    iPhone), z różnym UUID i lekko różnym duration/source. Funkcja grupuje wpisy
    po (data, nazwa, czas startu do minuty) i zwraca jednego reprezentanta
    (najdłuższy zapis), oznaczając liczbę zduplikowanych wpisów. Baza pozostaje
    wierna - to tylko warstwa odczytu.

    start_date/end_date (YYYY-MM-DD, opcjonalne) ograniczają zakres po dacie
    startu treningu ZANIM deduplikacja - dzięki temu nie ładujemy całej bazy do
    pamięci dla zapytań zakresowych. end_date jest włącznie (cały dzień).
    """
    from datetime import datetime, timedelta

    q = session.query(models.Workout)
    if start_date:
        start_dt = datetime.strptime(start_date, "%Y-%m-%d")
        q = q.filter(models.Workout.start >= start_dt)
    if end_date:
        # end_date włącznie -> < koniec dnia następnego
        end_dt = datetime.strptime(end_date, "%Y-%m-%d") + timedelta(days=1)
        q = q.filter(models.Workout.start < end_dt)
    ws = q.order_by(models.Workout.start.desc()).all()
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
            "active_energy_kcal": round(rep.active_energy_kj / 4.184, 1) if rep.active_energy_kj else None,
            "total_steps": rep.step_count,
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

    # kroki/energia z nagłówka (suma serii zapisana w ingest)
    total_steps = w.step_count
    energy_kj = w.active_energy_kj
    kcal = energy_kj / 4.184 if energy_kj is not None else None

    return {
        "id": w.id,
        "name": w.name,
        "start": w.start.isoformat() if w.start else None,
        "end": w.end.isoformat() if w.end else None,
        "duration_s": w.duration_s,
        "duration_min": round((w.duration_s or 0) / 60, 1),
        "distance_km": w.distance_km,
        "avg_speed_kmh": w.avg_speed_kmh,
        "max_speed_kmh": w.max_speed_kmh,
        "avg_heart_rate_bpm": w.avg_heart_rate_bpm,
        "max_heart_rate_bpm": w.max_heart_rate_bpm,
        "total_steps": total_steps,
        "active_energy_kj": energy_kj,
        "active_energy_kcal": round(kcal, 1) if kcal is not None else None,
        "total_energy_kj": w.total_energy_kj,
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


def get_workout_series(session: Session, workout_id: str) -> dict:
    """Serie czasowe treningu: energia (kJ/min) + kroki (steps/min) + HR minutowy.

    Zwraca pełny profil minutowy treningu - do wykresów wysiłku. Zamiast trzech
    osobnych odczytów, jedno zapytanie zbiera energie/kroki/HR z osobnych tabel.
    """
    if session.query(models.Workout).filter_by(id=workout_id).one_or_none() is None:
        raise WorkoutNotFoundError(f"Brak treningu o id={workout_id}")

    energy = session.query(models.WorkoutEnergy).filter_by(workout_id=workout_id).order_by(models.WorkoutEnergy.date).all()
    steps = session.query(models.WorkoutStep).filter_by(workout_id=workout_id).order_by(models.WorkoutStep.date).all()
    hr = session.query(models.WorkoutHeartRate).filter_by(workout_id=workout_id, kind="active").order_by(models.WorkoutHeartRate.date).all()

    energy_min = [{"minute": e.date.isoformat(), "kJ": round(e.qty, 1), "kcal": round(e.qty / 4.184, 1)} for e in energy]
    steps_min = [{"minute": s.date.isoformat(), "steps": int(s.qty) if s.qty else 0} for s in steps]
    hr_min = [{"minute": r.date.isoformat(), "avg_bpm": r.qty, "min_bpm": r.min_qty, "max_bpm": r.max_qty, "samples": r.sample_count} for r in hr]

    return {
        "workout_id": workout_id,
        "total_kcal": round(sum(e.qty for e in energy) / 4.184, 1) if energy else None,
        "total_steps": sum(s.qty for s in steps) if steps else None,
        "energy_minutely": energy_min,
        "steps_minutely": steps_min,
        "hr_minutely": hr_min,
    }


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


# ---------------------------------------------------------------------------
# GRUPA D - DAILY (codzienna aktywność) oraz GRUPA C - METRICS (regeneracja)
# ---------------------------------------------------------------------------

# Metryki, których dzienna wartość to SUMA wierszy danego dnia (kursowe).
# active_energy / basal_energy_burned to kcal(kJ) narastająco w ciągu dnia -> suma.
# apple_exercise_time / apple_stand_time / apple_stand_hour to minuty/godziny -> suma.
# cycling_distance / walking_running_distance / flights_climbed to dystansy/liczniki -> suma.
SUM_DAILY_METRICS = {
    "step_count",
    "active_energy",
    "basal_energy_burned",
    "apple_exercise_time",
    "apple_stand_time",
    "apple_stand_hour",
    "cycling_distance",
    "walking_running_distance",
    "flights_climbed",
}
# Metryki, których dzienna wartość to MEDIANA próbek (np. wiele pomiarów HRV dziennie).
MEDIAN_DAILY_METRICS = {"heart_rate_variability"}
# Metryki, których dzienna wartość to OSTATNI/pojedynczy pomiar danego dnia.
LAST_DAILY_METRICS = {"resting_heart_rate", "respiratory_rate", "apple_sleeping_wrist_temperature",
                      "weight_body_mass", "body_fat_percentage", "lean_body_mass",
                      "body_mass_index", "height"}


def _agg_daily(values: list[float], metric: str) -> Optional[float]:
    if not values:
        return None
    if metric in SUM_DAILY_METRICS:
        return sum(values)
    if metric in MEDIAN_DAILY_METRICS:
        s = sorted(values)
        n = len(s)
        return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2
    return values[-1]  # last / single


def _dedup_rows(rows: list["MetricPoint"]) -> list[MetricPoint]:
    """Dedupuje wiersze po MINUCIE (usuwa powielone pomiary z różnych źródeł).

    Health Kit raportuje ten sam pomiar z zegarka pod wieloma identyfikatorami
    źródła (np. 'Apple Watch', 'Apple Watch|Connect', ...). Na wspólnych
    minutach wartości są IDENTYCZNE, więc `sum()` w _agg_daily liczy je
    wielokrotnie -> zawyżone dzienne sumy (nawet ~3x).

    Zwraca jedną wiersz per unikalna minuta (z preferencją dla wiersza z
    wartością, gdyby któreś źródło miało qty=None w tej minucie), zachowując
    chronologię. Do użycia PRZED sumowaniem metryk z SUM_DAILY_METRICS.
    """
    if not rows:
        return rows
    # pierwszy wiersz każdej minuty, nadpisany przez lepszy (z wartością) jeśli się pojawi
    best: dict[str, "MetricPoint"] = {}
    order: list[str] = []
    for r in rows:
        minute = r.date.strftime("%Y-%m-%d %H:%M")
        if minute not in best:
            best[minute] = r
            order.append(minute)
        else:
            cur = best[minute]
            # preferuj wiersz z qty (wartość) nad None w tej samej minucie
            if cur.qty is None and r.qty is not None:
                best[minute] = r
    return [best[m] for m in order]


def _metric_points_for_date(session: Session, metric: str, day) -> list[MetricPoint]:
    start = day.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start.replace(hour=23, minute=59, second=59, microsecond=999999)
    return (
        session.query(models.MetricPoint)
        .filter(models.MetricPoint.metric_name == metric,
                models.MetricPoint.date >= start,
                models.MetricPoint.date <= end)
        .order_by(models.MetricPoint.date)
        .all()
    )


def get_daily_activity(day) -> dict:
    """Aktywność dzienna: kroki, dystans, energia, RHR, HRV - agregowana z metric_points."""
    from datetime import datetime
    if isinstance(day, str):
        day = datetime.fromisoformat(day).replace(tzinfo=None) if "T" in day else datetime.strptime(day, "%Y-%m-%d")
    db = SessionLocal()
    try:
        out: dict = {"date": day.strftime("%Y-%m-%d")}
        order = ["step_count", "walking_running_distance", "active_energy",
                 "basal_energy_burned", "resting_heart_rate", "heart_rate_variability",
                 "apple_exercise_time", "apple_stand_time", "apple_stand_hour",
                 "cycling_distance", "flights_climbed", "physical_effort",
                 "weight_body_mass", "body_fat_percentage", "lean_body_mass",
                 "body_mass_index", "height"]
        for metric in order:
            rows = _metric_points_for_date(db, metric, day)
            # metryki sumujące dwadzieścia razy liczą te same pomiary z różnych
            # źródeł Health Kit (Apple Watch pojawia się pod kilkoma źródłami) ->
            # deduplikacja po minucie, zanim sumujemy (bug: zawyżone dzienne sumy)
            if metric in SUM_DAILY_METRICS:
                rows = _dedup_rows(rows)
            vals = [r.qty for r in rows if r.qty is not None]
            out[metric] = _agg_daily(vals, metric)
        # sen z osobu tabela (sleep_sessions)
        sleep = (
            db.query(models.SleepSession)
            .filter(models.SleepSession.date >= day.replace(hour=0, minute=0, second=0, microsecond=0),
                    models.SleepSession.date <= day.replace(hour=23, minute=59, second=59, microsecond=999999))
            .all()
        )
        if sleep:
            s = sleep[0]
            out["sleep"] = {
                "total_hours": round(s.total_sleep, 2) if s.total_sleep else None,
                "core_hours": round(s.core, 2) if s.core else None,
                "deep_hours": round(s.deep, 2) if s.deep else None,
                "rem_hours": round(s.rem, 2) if s.rem else None,
                "awake_hours": round(s.awake, 2) if s.awake else None,
            }
        else:
            out["sleep"] = None
        return out
    finally:
        db.close()


def get_daily_activity_range(start_day, end_day) -> list[dict]:
    """Serie dzienne aktywności w zakresie dat - do trendów tygodnia/miesiąca."""
    from datetime import datetime, timedelta
    if isinstance(start_day, str):
        start_day = datetime.strptime(start_day, "%Y-%m-%d")
    if isinstance(end_day, str):
        end_day = datetime.strptime(end_day, "%Y-%m-%d")
    out = []
    day = start_day
    while day <= end_day:
        out.append(get_daily_activity(day))
        day += timedelta(days=1)
    return out


def get_daily_series(day, metric: str, bucket: str = "hour") -> dict:
    """Profil metryki w ciągu dnia - zagregowany do godzin (default) lub minut.

    bucket='hour' -> 1 punkt/godzinę (24 punktów), bucket='minute' -> surowe minuty.
    """
    from datetime import datetime
    if isinstance(day, str):
        day = datetime.strptime(day, "%Y-%m-%d")
    db = SessionLocal()
    try:
        rows = _metric_points_for_date(db, metric, day)
        by_bucket: dict = collections.defaultdict(list)
        for r in rows:
            if r.qty is None:
                continue
            key = r.date.strftime("%Y-%m-%d %H:00") if bucket == "hour" else r.date.strftime("%Y-%m-%d %H:%M")
            by_bucket[key].append(r.qty)
        series = [
            {
                "bucket": k,
                "value": _agg_daily(v, metric),
                "samples": len(v),
            }
            for k, v in sorted(by_bucket.items())
        ]
        return {"date": day.strftime("%Y-%m-%d"), "metric": metric, "bucket": bucket, "series": series}
    finally:
        db.close()


def get_metric_trend(metric: str, start_day, end_day) -> dict:
    """Trend dzienny wybranej metryki w zakresie - np. sen, HRV, RHR (regeneracja).
    Zakres dat (string YYYY-MM-DD). Zwraca {date: wartość}."""
    from datetime import datetime, timedelta
    if isinstance(start_day, str):
        start_day = datetime.strptime(start_day, "%Y-%m-%d")
    if isinstance(end_day, str):
        end_day = datetime.strptime(end_day, "%Y-%m-%d")
    days = get_daily_activity_range(start_day, end_day)
    trend = []
    for d in days:
        if metric == "sleep":
            val = (d.get("sleep") or {}).get("total_hours")
        else:
            val = d.get(metric)
        trend.append({"date": d["date"], "value": val})
    return {"metric": metric, "trend": trend}


# ---------------------------------------------------------------------------
# GRUPA D - GENERIC (uniwetysalny odczyt dowolnych danych w bazie)
# ---------------------------------------------------------------------------

# Agregacja domyślna dla get_data (gdy aggregation nie podane).
DEFAULT_AGG = "daily"
CAP_POINTS = 500


def list_data() -> dict:
    """Wykrywa wszystkie obszary/metery w bazie, które można odpytac przez get_data.
    Zwraca listy: metryki z metric_points, metryki zagregowane (agg), sen, treningi + liczba rekordów."""
    db = SessionLocal()
    try:
        metrics = [r[0] for r in db.query(models.MetricPoint.metric_name).distinct().all()]
        metrics_agg = [r[0] for r in db.query(models.MetricPointAgg.metric_name).distinct().all()]
        workouts = [(w.id, w.name, w.start.strftime("%Y-%m-%d") if w.start else None) for w in db.query(models.Workout).order_by(models.Workout.start).all()]
        sleep_dates = [s.date.strftime("%Y-%m-%d") for s in db.query(models.SleepSession).order_by(models.SleepSession.date).all()]
        return {
            "metrics_daily": metrics,
            "metrics_agg": metrics_agg,
            "sleep_dates": sleep_dates,
            "workouts": [{"id": w[0], "name": w[1], "date": w[2]} for w in workouts],
        }
    finally:
        db.close()


def _coerce_date(v):
    from datetime import datetime
    if isinstance(v, str):
        v = v.replace("Z", "").replace("T", " ")
        try:
            return datetime.strptime(v[:19], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return datetime.strptime(v[:10], "%Y-%m-%d")
    return v


def get_data(name: str, aggregation: str = DEFAULT_AGG,
             from_date: Optional[str] = None, to_date: Optional[str] = None,
             detail: bool = False) -> dict:
    """Uniwersalny odczyt dowolnej metryki/obszaru w bazie.

    name         - metryka (np. 'step_count', 'heart_rate', 'resting_heart_rate') lub
                   obszar: 'metrics' (lista), 'sleep', 'workouts'.
    aggregation  - 'daily' (dzienna wartosc, domyslna) | 'hourly' | 'minute' | 'raw'
    from/to_date - zakres dat YYYY-MM-DD (opcjonalnie), bez = wszystko.
    detail       - True zwraca surowe punkty, False zwraca zwarte podsumowanie/trend (domyslnie).

    Zawsze capuje liczbę punktów (CAP_POINTS) i domyslnie zwraca summary (min/max/avg/liczba)
    + trend, żeby nie palkć tokenów na surowe serie.
    """
    from datetime import datetime, timedelta
    db = SessionLocal()
    try:
        # Obszar 'metrics' -> lista dostepnych
        if name == "metrics":
            return list_data()

        # Obszar 'sleep'
        if name == "sleep":
            q = db.query(models.SleepSession)
            if from_date or to_date:
                d0 = _coerce_date(from_date) if from_date else datetime(1970, 1, 1)
                d1 = _coerce_date(to_date) if to_date else datetime(2100, 1, 1)
                d1 = d1 + timedelta(days=1)
                q = q.filter(models.SleepSession.date >= d0, models.SleepSession.date <= d1)
            rows = q.order_by(models.SleepSession.date).all()
            pts = [{"date": s.date.strftime("%Y-%m-%d"),
                    "total_hours": round(s.total_sleep, 2) if s.total_sleep else None,
                    "core": round(s.core, 2) if s.core else None,
                    "deep": round(s.deep, 2) if s.deep else None,
                    "rem": round(s.rem, 2) if s.rem else None} for s in rows]
            return {"name": "sleep", "count": len(pts), "points": pts[:CAP_POINTS],
                    "truncated": len(pts) > CAP_POINTS}

        # Obszar 'workouts' -> lista treningów
        if name == "workouts":
            q = db.query(models.Workout)
            if from_date or to_date:
                d0 = _coerce_date(from_date) if from_date else datetime(1970, 1, 1)
                d1 = _coerce_date(to_date) if to_date else datetime(2100, 1, 1)
                d1 = d1 + timedelta(days=1)
                q = q.filter(models.Workout.start >= d0, models.Workout.start <= d1)
            rows = q.order_by(models.Workout.start).all()
            pts = [{"date": (w.start.strftime("%Y-%m-%d") if w.start else None),
                    "name": w.name, "id": w.id,
                    "duration_min": round((w.duration_s or 0) / 60, 1),
                    "distance_km": w.distance_km,
                    "kcal": round((w.active_energy_kj or 0) / 4.184, 1)} for w in rows]
            return {"name": "workouts", "count": len(pts), "points": pts[:CAP_POINTS],
                    "truncated": len(pts) > CAP_POINTS}

        # Metryka z metric_points
        q = db.query(models.MetricPoint).filter(models.MetricPoint.metric_name == name)
        if from_date or to_date:
            d0 = _coerce_date(from_date) if from_date else datetime(1970, 1, 1)
            d1 = _coerce_date(to_date) if to_date else datetime(2100, 1, 1)
            d1 = d1 + timedelta(days=1)
            q = q.filter(models.MetricPoint.date >= d0, models.MetricPoint.date <= d1)
        rows = q.order_by(models.MetricPoint.date).all()
        if rows:
            vals = [(r.date, r.qty) for r in rows if r.qty is not None]
        else:
            # spróbuj w metric_points_agg
            qa = db.query(models.MetricPointAgg).filter(models.MetricPointAgg.metric_name == name)
            if from_date or to_date:
                d0 = _coerce_date(from_date) if from_date else datetime(1970, 1, 1)
                d1 = _coerce_date(to_date) if to_date else datetime(2100, 1, 1)
                d1 = d1 + timedelta(days=1)
                qa = qa.filter(models.MetricPointAgg.date >= d0, models.MetricPointAgg.date <= d1)
            arow = qa.order_by(models.MetricPointAgg.date).all()
            vals = [(r.date, r.avg if r.avg is not None else r.max) for r in arow if r.avg is not None or r.max is not None]

        if not vals:
            return {"name": name, "error": "Brak danych w tym zakresie lub metryka nie istnieje", "count": 0}

        numbers = [v for _, v in vals if v is not None]
        summary = {
            "count": len(numbers),
            "min": round(min(numbers), 2) if numbers else None,
            "max": round(max(numbers), 2) if numbers else None,
            "avg": round(sum(numbers) / len(numbers), 2) if numbers else None,
            "latest": round(numbers[-1], 2) if numbers else None,
            "units": (rows[0].units if rows else None),
        }

        # Agregacja treści
        if aggregation == "minute":
            pts = [{"date": d.strftime("%Y-%m-%d %H:%M"), "value": round(v, 2)} for d, v in vals]
        elif aggregation == "hourly":
            by = collections.defaultdict(list)
            for d, v in vals:
                by[d.strftime("%Y-%m-%d %H:00")].append(v)
            pts = [{"bucket": k, "value": round(sum(vl) / len(vl), 2), "samples": len(vl)}
                   for k, vl in sorted(by.items())]
        elif aggregation == "raw":
            pts = [{"date": d.strftime("%Y-%m-%d %H:%M"), "value": round(v, 2)} for d, v in vals]
        else:  # daily
            by_day = collections.defaultdict(list)
            for d, v in vals:
                by_day[d.strftime("%Y-%m-%d")].append(v)
            pts = [{"date": k, "value": _agg_daily(vl, name)} for k, vl in sorted(by_day.items())]

        truncated = len(pts) > CAP_POINTS
        pts = pts[:CAP_POINTS]

        if detail or len(pts) <= 60:
            return {"name": name, "aggregation": aggregation, "summary": summary, "points": pts, "truncated": truncated}
        # domyślnie: podsumowanie + docinki trendu (żeby nie palic tokenów)
        return {"name": name, "aggregation": aggregation, "summary": summary,
                "trend_sample": pts, "truncated": truncated}
    finally:
        db.close()


