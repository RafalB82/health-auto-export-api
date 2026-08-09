"""
Serwer MCP (stdio) dla Health Auto Export API - odpytywanie danych treningowych
przez asystenta (OpenClaw / trainer) po stdio.

Lokalnie (ten sam host co baza) - klient MCP (OpenClaw) uruchamia ten proces
bezpośrednio, bez sieci. To najprostszy i najtańszy transport dla danych,
które i tak leżą na tym samym dysku (health_data.db + raw_payloads/).

Wymaga OSOBNEGO środowiska (venv-mcp, requirements-mcp.txt) - pakiet `mcp`
konfliktuje wersyjnie ze starlette używanym przez FastAPI webhooka.

Konfiguracja w OpenClaw (openclaw.json -> mcp.servers.health-data):
    {
      "command": "/home/rafal/health-auto-export-api/venv-mcp/bin/python",
      "args": ["/home/rafal/health-auto-export-api/mcp_server.py"]
    }

Narzędzia:
- list_recent_workouts(limit)     -> znajdź workout_id bez zgadywania UUID
- get_workout_summary(workout_id) -> agregaty z SQLite (szybkie)
- get_workout_hr_minutely(id)     -> HR 1 punkt/minutę, do wykresów
- get_workout_hr_trace(workout_id)-> HR sekunda-po-sekundzie z pliku surowego
                                     (wolniejsze - świadomy osobny tool)
"""
from __future__ import annotations

import asyncio
import json

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

from app.db import SessionLocal, Workout
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

server = Server("health-data")


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="list_recent_workouts",
            description=(
                "Lista ostatnich treningów (id, nazwa, data, dystans) - użyj tego "
                "najpierw, żeby znaleźć workout_id do pozostałych narzędzi. "
                "start_date/end_date (opcjonalne, YYYY-MM-DD) zawężają do zakresu "
                "dat startu; end_date włącznie."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "description": "Ile ostatnich treningów zwrócić (domyślnie 10)"},
                    "start_date": {"type": "string", "description": "Start YYYY-MM-DD (opcjonalnie)"},
                    "end_date": {"type": "string", "description": "Koniec YYYY-MM-DD włącznie (opcjonalnie)"},
                },
            },
        ),
        Tool(
            name="get_workout_summary",
            description=(
                "Zagregowane podsumowanie treningu (dystans, prędkość, HR avg/max, "
                "przewyższenie) - z SQLite, szybkie. Użyj do typowych pytań o trening."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "workout_id": {"type": "string", "description": "UUID treningu"},
                },
                "required": ["workout_id"],
            },
        ),
        Tool(
            name="get_workout_hr_minutely",
            description=(
                "Tętno treningu zagregowane do 1 punktu/minutę (avg/min/max/liczba próbek). "
                "Dobre do wykresu przebiegu HR w całym treningu bez nadmiaru danych."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "workout_id": {"type": "string", "description": "UUID treningu"},
                },
                "required": ["workout_id"],
            },
        ),
        Tool(
            name="get_workout_series",
            description=(
                "Serie czasowe treningu: energia (kJ/kcal na minutę) + kroki na minutę + "
                "tętno minutowe. Pełny profil wysiłku treningu - do wykresów."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "workout_id": {"type": "string", "description": "UUID treningu"},
                },
                "required": ["workout_id"],
            },
        ),
        Tool(
            name="get_workout_hr_trace",
            description=(
                "Pełny szereg czasowy tętna sekunda-po-sekundzie, wprost z surowego "
                "payloadu (dekompresja pliku na żądanie - wolniejsze niż wersja minutowa). "
                "Użyj tylko gdy faktycznie potrzebna jest pełna rozdzielczość, np. analiza "
                "zmienności HR sekunda po sekundzie, nie do zwykłych podsumowań."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "workout_id": {"type": "string", "description": "UUID treningu"},
                },
                "required": ["workout_id"],
            },
        ),
        Tool(
            name="get_daily_activity",
            description=(
                "Aktywność dzienna: kroki, dystans, kalorie (aktywne + basal), RHR, HRV "
                "i sen (total/core/deep/rem) za podany dzień. Bazowy 'jak wyglądał mój dzień'."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "date": {"type": "string", "description": "Dzień YYYY-MM-DD"},
                },
                "required": ["date"],
            },
        ),
        Tool(
            name="get_daily_activity_range",
            description=(
                "Serie dzienne aktywności (kroki, kalorie, dystans, RHR, HRV, sen) "
                "w zakresie dat - do trendów tygodniowych/miesięcznych."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "start_date": {"type": "string", "description": "Start YYYY-MM-DD"},
                    "end_date": {"type": "string", "description": "Koniec YYYY-MM-DD (włącznie)"},
                },
                "required": ["start_date", "end_date"],
            },
        ),
        Tool(
            name="get_daily_series",
            description=(
                "Profil metryki w ciągu dnia: 1 punkt/godzinę (default) lub minutę. "
                "Np. active_energy -> kalorie na godzinę, step_count -> kroki na godzinę."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "date": {"type": "string", "description": "Dzień YYYY-MM-DD"},
                    "metric": {"type": "string", "description": "Nazwa metryki (np. active_energy, step_count, heart_rate)"},
                    "bucket": {"type": "string", "enum": ["hour", "minute"], "description": "Agregacja godzinowa (default) lub minutowa"},
                },
                "required": ["date", "metric"],
            },
        ),
        Tool(
            name="get_metric_trend",
            description=(
                "Trend dzienny metryki w zakresie dat - regeneracja (sen, HRV, RHR, "
                "rest_hr) lub aktywność. Zwraca {date: wartość}."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "metric": {"type": "string", "description": "Metryka: sleep, resting_heart_rate, heart_rate_variability, step_count, active_energy..."},
                    "start_date": {"type": "string", "description": "Start YYYY-MM-DD"},
                    "end_date": {"type": "string", "description": "Koniec YYYY-MM-DD"},
                },
                "required": ["metric", "start_date", "end_date"],
            },
        ),
        Tool(
            name="list_data",
            description=(
                "Lista WSZYSTKICH dostępnych danych w bazie - metryki dzienne, metryki "
                "zagregowane, daty snu, treningi. Użyj, żeby zobaczyć co można odpytać "
                "przez get_data (nie trzeba nic definiować z góry)."
            ),
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="get_data",
            description=(
                "Generyczny odczyt dowolnej metryki/obszaru w bazie. name=metryka "
                "(np. step_count, heart_rate, resting_heart_rate, apple_sleeping_wrist_temperature) "
                "lub obszar (metrics, sleep, workouts). aggregation=daily|hourly|minute|raw. "
                "Domyślnie zwraca zwarte podsumowanie (min/max/avg/latest + trend) zamiast "
                "surowych punktów, żeby nie palkc tokenów. detail=true - surowe punkty. "
                "Zawsze capuje do 500 punktów."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Metryka lub obszar (metrics/sleep/workouts)"},
                    "aggregation": {"type": "string", "enum": ["daily", "hourly", "minute", "raw"], "description": "Agregacja, domyślnie daily"},
                    "from_date": {"type": "string", "description": "Start YYYY-MM-DD (opcjonalnie)"},
                    "to_date": {"type": "string", "description": "Koniec YYYY-MM-DD (opcjonalnie)"},
                    "detail": {"type": "boolean", "description": "True = surowe punkty, False = summary+trend (default)"},
                },
                "required": ["name"],
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    db = SessionLocal()
    try:
        if name == "list_recent_workouts":
            limit = arguments.get("limit", 10)
            result = get_workouts_deduped(
                db,
                limit=limit,
                start_date=arguments.get("start_date"),
                end_date=arguments.get("end_date"),
            )
            return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False, indent=2))]

        if name == "get_workout_summary":
            result = get_workout_summary(db, arguments["workout_id"])
            return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False, indent=2))]

        if name == "get_workout_hr_minutely":
            result = get_workout_hr_minutely(db, arguments["workout_id"])
            return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False, indent=2))]

        if name == "get_workout_series":
            result = get_workout_series(db, arguments["workout_id"])
            return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False, indent=2))]

        if name == "get_workout_hr_trace":
            result = get_workout_hr_trace(db, arguments["workout_id"])
            return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False, indent=2))]

        if name == "get_daily_activity":
            result = get_daily_activity(arguments["date"])
            return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False, indent=2, default=str))]

        if name == "get_daily_activity_range":
            result = get_daily_activity_range(arguments["start_date"], arguments["end_date"])
            return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False, indent=2, default=str))]

        if name == "get_daily_series":
            result = get_daily_series(arguments["date"], arguments["metric"], arguments.get("bucket", "hour"))
            return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False, indent=2, default=str))]

        if name == "get_metric_trend":
            result = get_metric_trend(arguments["metric"], arguments["start_date"], arguments["end_date"])
            return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False, indent=2, default=str))]

        if name == "list_data":
            result = list_data()
            return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False, indent=2, default=str))]

        if name == "get_data":
            result = get_data(
                arguments["name"],
                aggregation=arguments.get("aggregation", "daily"),
                from_date=arguments.get("from_date"),
                to_date=arguments.get("to_date"),
                detail=arguments.get("detail", False),
            )
            return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False, indent=2, default=str))]

        return [TextContent(type="text", text=f"Nieznane narzędzie: {name}")]

    except WorkoutNotFoundError as e:
        return [TextContent(type="text", text=f"Błąd: {e}")]
    except RawPayloadUnavailableError as e:
        return [TextContent(type="text", text=f"Surowe dane niedostępne: {e}")]
    finally:
        db.close()


async def main() -> None:
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
