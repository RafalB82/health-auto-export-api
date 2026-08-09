"""Testy get_workouts_deduped: deduplikacja po (data, nazwa, czas startu)
oraz nowe opcjonalne filtry zakresu dat (start_date/end_date).

Zgodnie z lokalnym wzorcem (test_dedup.py) nie dotykamy bazy - mockujemy
łańcuch zapytań SQLAlchemy i weryfikujemy, że filtr dat trafia do zapytania
zanim załadujemy wiersze (to jest sedno optymalizacji: nie ładujemy całej
bazy do pamięci dla zapytań zakresowych).
"""
from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pytest

from app.query import get_workouts_deduped


def _w(id_, start_str, name="Run", duration_s=600):
    """Workout-like obiekt (tylko atrybuty, których używa funkcja)."""
    return SimpleNamespace(
        id=id_,
        name=name,
        start=datetime.strptime(start_str, "%Y-%m-%d %H:%M"),
        end=None,
        duration_s=duration_s,
        distance_km=5.0,
        avg_heart_rate_bpm=140.0,
        max_heart_rate_bpm=170,
        active_energy_kj=500.0,
        step_count=6000,
    )


class FakeQuery:
    """Symuluje session.query().filter(...).order_by(...).all().

    Zapamiętuje przekazane filtry, żeby test mógł zweryfikować, że zakres
    dat trafił do zapytania SQL (a nie tylko do obcinania w Pythonie).
    """

    def __init__(self, rows):
        self._rows = list(rows)
        self.filters = []

    def filter(self, *args):
        self.filters.extend(args)
        return self

    def order_by(self, *args):
        return self

    def all(self):
        return self._rows


class FakeSession:
    def __init__(self, rows):
        self._rows = rows
        self.last_query = None

    def query(self, *args, **kwargs):
        self.last_query = FakeQuery(self._rows)
        return self.last_query


def _rows_workouts():
    # trzy fizyczne treningi, jeden z nich (Run A 08-05) ma duplikat (inny UUID)
    return [
        _w("A1", "2026-08-05 07:00", "Run A", duration_s=1800),
        _w("A2", "2026-08-05 07:00", "Run A", duration_s=1800),  # duplikat Run A
        _w("B1", "2026-08-04 18:00", "Gym", duration_s=2700),
        _w("C1", "2026-08-03 06:30", "Run A", duration_s=1200),
    ]


# --- dedup / reprezentant / sort / limit (bez dat) ---


def test_dedup_groups_by_name_and_start_minute():
    out = get_workouts_deduped(FakeSession(_rows_workouts()), limit=20)
    run_a = [r for r in out if r["id"] == "A1"]
    assert len(run_a) == 1
    assert run_a[0]["deduped_copies"] == 2
    assert {r["id"] for r in out} == {"A1", "B1", "C1"}


def test_rep_is_longest_recording():
    rows = [
        _w("short", "2026-08-05 07:00", "Run", duration_s=600),
        _w("long", "2026-08-05 07:00", "Run", duration_s=3600),
    ]
    out = get_workouts_deduped(FakeSession(rows), limit=20)
    assert len(out) == 1
    assert out[0]["id"] == "long"
    assert out[0]["duration_s"] == 3600


def test_sorted_desc_by_start():
    out = get_workouts_deduped(FakeSession(_rows_workouts()), limit=20)
    starts = [r["start"] for r in out]
    assert starts == sorted(starts, reverse=True)


def test_limit_applied_after_dedup():
    out = get_workouts_deduped(FakeSession(_rows_workouts()), limit=1)
    assert len(out) == 1
    assert out[0]["id"] == "A1"  # najnowszy po dedup


def test_full_range_no_filters_returns_all_deduped():
    out = get_workouts_deduped(FakeSession(_rows_workouts()), limit=20)
    assert len(out) == 3


def test_derived_fields():
    out = get_workouts_deduped(FakeSession([_w("A1", "2026-08-05 07:00", duration_s=600)]), limit=20)
    assert out[0]["duration_min"] == 10.0
    assert round(out[0]["active_energy_kcal"], 1) == round(500.0 / 4.184, 1)


# --- filtry zakresu dat trafiają do zapytania SQL przed załadowaniem ---


def test_start_date_adds_sql_filter():
    sess = FakeSession(_rows_workouts())
    get_workouts_deduped(sess, limit=20, start_date="2026-08-04")
    # filtr daty poszedł do łańcucha zapytania (nie tylko Python-slice)
    assert len(sess.last_query.filters) == 1


def test_end_date_adds_sql_filter():
    sess = FakeSession(_rows_workouts())
    get_workouts_deduped(sess, limit=20, end_date="2026-08-04")
    assert len(sess.last_query.filters) == 1


def test_both_dates_add_two_sql_filters():
    sess = FakeSession(_rows_workouts())
    get_workouts_deduped(sess, limit=20, start_date="2026-08-01", end_date="2026-08-31")
    assert len(sess.last_query.filters) == 2


def test_no_dates_adds_no_sql_filter():
    sess = FakeSession(_rows_workouts())
    get_workouts_deduped(sess, limit=20)
    assert sess.last_query.filters == []
