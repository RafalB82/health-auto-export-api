"""Testy deduplikacji pomiarów po minucie (_dedup_rows).

Problem: Health Kit raportuje ten sam pomiar zegarka pod wieloma źródłami
(np. 'Apple Watch', 'Apple Watch|Connect'), przez co jest zapisany wielokrotnie
za tę samą minutę. `_dedup_rows` usuwa powielenia przed sumowaniem dziennym.

Testy używają prostych obiektów zastępczych (attrybuty .date/.qty) — bez
dotykania bazy, żeby były czyste i deterministyczne.
"""
from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pytest

from app.query import _dedup_rows


def _p(dt, qty):
    return SimpleNamespace(date=datetime.strptime(dt, "%Y-%m-%d %H:%M"), qty=qty)


class TestDedupRows:
    def test_no_duplicates_unchanged(self):
        rows = [_p("2026-08-05 00:00", 1.0), _p("2026-08-05 00:01", 2.0), _p("2026-08-05 00:02", 3.0)]
        out = _dedup_rows(rows)
        assert len(out) == 3
        assert [r.qty for r in out] == [1.0, 2.0, 3.0]

    def test_removes_duplicate_same_minute_from_other_source(self):
        # dwa źródła za tę samą minutę (watch + watch|Connect) — zostaje pierwsza
        rows = [
            _p("2026-08-05 00:00", 1.0),
            _p("2026-08-05 00:00", 1.0),  # duplikat (inny source)
            _p("2026-08-05 00:01", 2.0),
            _p("2026-08-05 00:01", 2.0),
        ]
        out = _dedup_rows(rows)
        assert len(out) == 2
        assert [r.qty for r in out] == [1.0, 2.0]

    def test_preserves_first_value_when_dupes_differ(self):
        # gdyby wartości się różniły, zostaje pierwsza (determinizm, zachowanie kolejności)
        rows = [
            _p("2026-08-05 00:00", 1.0),
            _p("2026-08-05 00:00", 9.9),  # różna, ale ta sama minuta
        ]
        out = _dedup_rows(rows)
        assert len(out) == 1
        assert out[0].qty == 1.0

    def test_keeps_chronological_order(self):
        rows = [
            _p("2026-08-05 00:05", 5.0),
            _p("2026-08-05 00:03", 3.0),
            _p("2026-08-05 00:05", 5.0),
            _p("2026-08-05 00:04", 4.0),
        ]
        out = _dedup_rows(rows)
        # kolejność: pierwszy napotkany (00:05, 00:03, 00:04) — nie sortujemy
        assert [r.qty for r in out] == [5.0, 3.0, 4.0]

    def test_prefers_value_over_none_in_same_minute(self):
        # w tej samej minucie jedno źródło ma None, drugie wartość -> bierzemy wartość
        rows = [
            SimpleNamespace(date=datetime(2026, 8, 5, 0, 0), qty=None),
            _p("2026-08-05 00:00", 1.0),
        ]
        out = _dedup_rows(rows)
        assert len(out) == 1
        assert out[0].qty == 1.0

    def test_empty_input(self):
        assert _dedup_rows([]) == []
