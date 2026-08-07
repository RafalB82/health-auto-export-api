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
                "najpierw, żeby znaleźć workout_id do pozostałych narzędzi."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "description": "Ile ostatnich treningów zwrócić (domyślnie 10)"},
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
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    db = SessionLocal()
    try:
        if name == "list_recent_workouts":
            limit = arguments.get("limit", 10)
            result = get_workouts_deduped(db, limit=limit)
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
