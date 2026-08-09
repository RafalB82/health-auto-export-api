"""
Serwer MCP dla Health Auto Export API - odpytywanie danych treningowych
przez asystenta (OpenClaw / trainer).

Transport wybierany flagą na starcie (wzorzec jak `MFP_TRANSPORT` w mfp-mcp):
- domyślnie (bez flag): stdio - klient uruchamia ten proces bezpośrednio,
  dane i tak leżą na tym samym dysku (health_data.db + raw_payloads/).
- `--http`: streamable-http - serwer nasłuchuje na host/port (domyślnie
  127.0.0.1:8766, ścieżka /mcp). Lokalnie, dla klientów MCP przez sieć.

Wymaga OSOBNEGO środowiska (venv-mcp, requirements-mcp.txt) - pakiet `mcp`
konfliktuje wersyjnie ze starlette używanym przez FastAPI webhooka. Dotyczy
to OBAJ transportów (stdio i HTTP) - to osobny proces od `health-api.service`.

Uruchomienie:
    stdio:   venv-mcp/bin/python mcp_server.py
    HTTP:    venv-mcp/bin/python mcp_server.py --http [--host 127.0.0.1] [--port 8766]
             (stopniowo zamiast stdio) 

Konfiguracja w OpenClaw (openclaw.json -> mcp.servers.health-data):
    stdio:
      { "command": "/home/rafal/health-auto-export-api/venv-mcp/bin/python",
        "args": ["/home/rafal/health-auto-export-api/mcp_server.py"] }
    HTTP:
      { "url": "http://127.0.0.1:8766/mcp",
        "transport": "streamable-http" }

Narzędzia:
- list_recent_workouts(limit, start_date, end_date) -> workout_id bez zgadywania UUID
- get_workout_summary(workout_id)  -> agregaty z SQLite (szybkie)
- get_workout_hr_minutely(id)      -> HR 1 punkt/minutę, do wykresów
- get_workout_series(id)           -> energia + kroki + HR minutowo
- get_workout_hr_trace(workout_id) -> HR sekunda-po-sekundzie z pliku surowego
- get_daily_activity / _range / _series / get_metric_trend / list_data / get_data
Uwaga: NIE używamy `from __future__ import annotations` - FastMCP (mcp==1.12.4)
robi `issubclass(param.annotation, Context)` na surowych adnotacjach, a stringowe
(lazy) adnotacje z future-import wywalaly TypeError. Typy są tu proste
(str/int/bool), więc nie ma forward references.
"""
import argparse
import json

from mcp.server.fastmcp import FastMCP

from app.db import SessionLocal
from app.query import (
    get_workout_summary,
    get_workout_hr_minutely,
    get_workout_hr_trace,
    get_workouts_deduped,
    get_workout_series,
    get_daily_activity,
    get_daily_activity_range,
    get_daily_series,
    get_metric_trend,
    list_data,
    get_data,
    WorkoutNotFoundError,
    RawPayloadUnavailableError,
)


def _json(obj) -> str:
    """Serializacja do JSON-a zgodna z poprzednią wersją (default=str dla dat)."""
    return json.dumps(obj, ensure_ascii=False, indent=2, default=str)


# Jedna instancja serwera; transport wybrany na starcie (stdio / HTTP).
mcp = FastMCP("health-data", debug=False)


@mcp.tool(
    name="list_recent_workouts",
    description=(
        "Lista ostatnich treningów (id, nazwa, data, dystans) - użyj tego "
        "najpierw, żeby znaleźć workout_id do pozostałych narzędzi. "
        "start_date/end_date (opcjonalne, YYYY-MM-DD) zawężają do zakresu "
        "dat startu; end_date włącznie."
    ),
)
def list_recent_workouts(
    limit: int = 10,
    start_date: str = None,
    end_date: str = None,
) -> str:
    db = SessionLocal()
    try:
        result = get_workouts_deduped(db, limit=limit, start_date=start_date, end_date=end_date)
        return _json(result)
    finally:
        db.close()


@mcp.tool(
    name="get_workout_summary",
    description=(
        "Zagregowane podsumowanie treningu (dystans, prędkość, HR avg/max, "
        "przewyższenie) - z SQLite, szybkie. Użyj do typowych pytań o trening."
    ),
)
def get_workout_summary_tool(workout_id: str) -> str:
    db = SessionLocal()
    try:
        result = get_workout_summary(db, workout_id)
        return _json(result)
    except WorkoutNotFoundError as e:
        return f"Błąd: {e}"
    finally:
        db.close()


@mcp.tool(
    name="get_workout_hr_minutely",
    description=(
        "Tętno treningu zagregowane do 1 punktu/minutę (avg/min/max/liczba próbek). "
        "Dobre do wykresu przebiegu HR w całym treningu bez nadmiaru danych."
    ),
)
def get_workout_hr_minutely_tool(workout_id: str) -> str:
    db = SessionLocal()
    try:
        result = get_workout_hr_minutely(db, workout_id)
        return _json(result)
    except WorkoutNotFoundError as e:
        return f"Błąd: {e}"
    finally:
        db.close()


@mcp.tool(
    name="get_workout_series",
    description=(
        "Serie czasowe treningu: energia (kJ/kcal na minutę) + kroki na minutę + "
        "tętno minutowe. Pełny profil wysiłku treningu - do wykresów."
    ),
)
def get_workout_series_tool(workout_id: str) -> str:
    db = SessionLocal()
    try:
        result = get_workout_series(db, workout_id)
        return _json(result)
    except WorkoutNotFoundError as e:
        return f"Błąd: {e}"
    finally:
        db.close()


@mcp.tool(
    name="get_workout_hr_trace",
    description=(
        "Pełny szereg czasowy tętna sekunda-po-sekundzie, wprost z surowego "
        "payloadu (dekompresja pliku na żądanie - wolniejsze niż wersja minutowa). "
        "Użyj tylko gdy faktycznie potrzebna jest pełna rozdzielczość, np. analiza "
        "zmienności HR sekunda po sekundzie, nie do zwykłych podsumowań."
    ),
)
def get_workout_hr_trace_tool(workout_id: str) -> str:
    db = SessionLocal()
    try:
        result = get_workout_hr_trace(db, workout_id)
        return _json(result)
    except WorkoutNotFoundError as e:
        return f"Błąd: {e}"
    except RawPayloadUnavailableError as e:
        return f"Surowe dane niedostępne: {e}"
    finally:
        db.close()


@mcp.tool(
    name="get_daily_activity",
    description=(
        "Aktywność dzienna: kroki, dystans, kalorie (aktywne + basal), RHR, HRV "
        "i sen (total/core/deep/rem) za podany dzień. Bazowy 'jak wyglądał mój dzień'."
    ),
)
def get_daily_activity_tool(date: str) -> str:
    return _json(get_daily_activity(date))


@mcp.tool(
    name="get_daily_activity_range",
    description=(
        "Serie dzienne aktywności (kroki, kalorie, dystans, RHR, HRV, sen) "
        "w zakresie dat - do trendów tygodniowych/miesięcznych."
    ),
)
def get_daily_activity_range_tool(start_date: str, end_date: str) -> str:
    return _json(get_daily_activity_range(start_date, end_date))


@mcp.tool(
    name="get_daily_series",
    description=(
        "Profil metryki w ciągu dnia: 1 punkt/godzinę (default) lub minutę. "
        "Np. active_energy -> kalorie na godzinę, step_count -> kroki na godzinę."
    ),
)
def get_daily_series_tool(
    date: str,
    metric: str,
    bucket: str = "hour",
) -> str:
    return _json(get_daily_series(date, metric, bucket))


@mcp.tool(
    name="get_metric_trend",
    description=(
        "Trend dzienny metryki w zakresie dat - regeneracja (sen, HRV, RHR, "
        "rest_hr) lub aktywność. Zwraca {date: wartość}."
    ),
)
def get_metric_trend_tool(metric: str, start_date: str, end_date: str) -> str:
    return _json(get_metric_trend(metric, start_date, end_date))


@mcp.tool(
    name="list_data",
    description=(
        "Lista WSZYSTKICH dostępnych danych w bazie - metryki dzienne, metryki "
        "zagregowane, daty snu, treningi. Użyj, żeby zobaczyć co można odpytać "
        "przez get_data (nie trzeba nic definiować z góry)."
    ),
)
def list_data_tool() -> str:
    return _json(list_data())


@mcp.tool(
    name="get_data",
    description=(
        "Generyczny odczyt dowolnej metryki/obszaru w bazie. name=metryka "
        "(np. step_count, heart_rate, resting_heart_rate, apple_sleeping_wrist_temperature) "
        "lub obszar (metrics, sleep, workouts). aggregation=daily|hourly|minute|raw. "
        "Domyślnie zwraca zwarte podsumowanie (min/max/avg/latest + trend) zamiast "
        "surowych punktów, żeby nie palkc tokenów. detail=true - surowe punkty. "
        "Zawsze capuje do 500 punktów."
    ),
)
def get_data_tool(
    name: str,
    aggregation: str = "daily",
    from_date: str = None,
    to_date: str = None,
    detail: bool = False,
) -> str:
    return _json(get_data(name, aggregation=aggregation, from_date=from_date, to_date=to_date, detail=detail))


def main() -> None:
    parser = argparse.ArgumentParser(description="Health Auto Export MCP server")
    parser.add_argument(
        "--http",
        action="store_true",
        help="Uruchom przez streamable-http (domyślnie stdio). Lokalnie, np. 127.0.0.1:8766/mcp",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Host dla --http (domyślnie 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8766, help="Port dla --http (domyślnie 8766)")
    args = parser.parse_args()

    if args.http:
        # nadpisz host/port (FastMCP domyślnie 127.0.0.1:8000, a port 8000 zajmuje mfp)
        mcp.settings.host = args.host
        mcp.settings.port = args.port
        # streamable-http; ścieżka domyślnie /mcp (patrz streamable_http_path w FastMCP)
        mcp.run(transport="streamable-http")
    else:
        # domyślny transport stdio - klient uruchamia ten proces bezpośrednio
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
