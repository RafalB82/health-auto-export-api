"""
FastAPI serwer odbierający dane z Health Auto Export (REST API automation, JSON v2).

Uruchomienie:
    uvicorn app.main:app --host 0.0.0.0 --port 8000

Konfiguracja w apce (Automations -> REST API):
    URL: http://<twoj-host>:8000/health-data
    Header: X-API-Key: <wartość z API_KEY poniżej>
    Export Format: JSON
    Export Version: 2

UWAGA na limit body w uvicornie/reverse proxy: przy dużych trasach GPS payload
może mieć dziesiątki-setki MB. Jeśli stawiasz to za nginx, ustaw tam
client_max_body_size (np. 200m), inaczej nginx utnie request przed FastAPI.
"""
from __future__ import annotations

import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Optional

from fastapi import FastAPI, Header, HTTPException, Request, Depends
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.db import init_db, get_db, SessionLocal
from app.db import RawPayload
from app.schemas import HealthExportPayload
from app.ingest import ingest_payload
from app.raw_storage import save_raw_payload

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("health-api")

# Klucz API do walidacji nagłówka X-API-Key. Ustaw realną wartość jako
# zmienną środowiskową przed uruchomieniem: export HEALTH_API_KEY="..."
API_KEY = os.environ.get("HEALTH_API_KEY", "change-me")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    logger.info("DB zainicjalizowana (SQLite: health_data.db)")
    yield


app = FastAPI(title="Health Auto Export Receiver", lifespan=lifespan)


def verify_api_key(x_api_key: Optional[str] = Header(default=None)) -> None:
    if API_KEY == "change-me":
        # Świadomie nie blokujemy startu appki, ale krzyczymy w logach -
        # w produkcji ustaw HEALTH_API_KEY, inaczej endpoint jest otwarty.
        logger.warning("HEALTH_API_KEY nieustawiony - endpoint bez realnej autoryzacji!")
        return
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Nieprawidłowy lub brakujący X-API-Key")


@app.get("/")
def root() -> dict:
    """Szybki test żywotności + czy klucz API został ustawiony (bez ujawniania go)."""
    return {
        "status": "ok",
        "service": "health-auto-export-api",
        "api_key_set": API_KEY != "change-me",
        "raw_payloads": "files on disk (json.gz), not DB blob",
    }


@app.post("/health-data/test")
async def test_health_data(
    request: Request,
    _: None = Depends(verify_api_key),
) -> dict:
    """Walidacja przykładowego payloadu BEZ zapisu do bazy.

    Pozwala sprawdzić ręcznie (curl / skrypt), czy serwer przyjąłby dany JSON
    i co by z nim zrobił — bez dotykania raw_payloads ani pozostałych tabel.
    Używa tego samego schematu Pydantic co produkcyjny /health-data.
    """
    raw_body = await request.body()

    if not raw_body:
        return {"valid": False, "error": "Puste body", "metrics": 0, "workouts": 0}

    try:
        raw_json = json.loads(raw_body)
    except json.JSONDecodeError as e:
        return {"valid": False, "error": f"Niepoprawny JSON: {e}", "metrics": 0, "workouts": 0}

    try:
        payload = HealthExportPayload.model_validate(raw_json)
    except ValidationError as e:
        return {
            "valid": False,
            "error": "Błąd walidacji schematu",
            "details": e.errors(),
            "metrics": 0,
            "workouts": 0,
        }

    return {
        "valid": True,
        "metrics": len(payload.data.metrics),
        "workouts": len(payload.data.workouts),
        "metric_points": sum(len(m.data) for m in payload.data.metrics),
    }


@app.get("/health")
def healthcheck() -> dict:
    return {"status": "ok"}


@app.post("/health-data")
async def receive_health_data(
    request: Request,
    x_api_key: Optional[str] = Header(default=None),
    automation_name: Optional[str] = Header(default=None),
    automation_id: Optional[str] = Header(default=None),
    automation_period: Optional[str] = Header(default=None),
    automation_aggregation: Optional[str] = Header(default=None),
    session_id: Optional[str] = Header(default=None),
    _: None = Depends(verify_api_key),
) -> dict:
    raw_body = await request.body()

    try:
        raw_json = json.loads(raw_body)
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=400, detail=f"Niepoprawny JSON: {e}")

    try:
        payload = HealthExportPayload.model_validate(raw_json)
    except ValidationError as e:
        # Nie chowamy szczegółów błędu - łatwiej debugować z poziomu Activity Logs w apce
        raise HTTPException(status_code=422, detail=e.errors())

    db: Session = SessionLocal()
    try:
        # Surowy payload zapisujemy zawsze jako skompresowany plik na dysku
        # (patrz app/raw_storage.py) - to Twoja siatka bezpieczeństwa przy
        # zmianach schematu lub logiki agregacji, bez obciążania SQLite dużym BLOB-em.
        received_at = datetime.utcnow()
        file_path, size_raw, size_gz = save_raw_payload(raw_body, received_at, session_id)

        db.add(RawPayload(
            received_at=received_at,
            automation_name=automation_name,
            automation_id=automation_id,
            automation_period=automation_period,
            automation_aggregation=automation_aggregation,
            session_id=session_id,
            file_path=file_path,
            size_bytes_raw=size_raw,
            size_bytes_compressed=size_gz,
        ))
        db.commit()

        counts = ingest_payload(db, payload, session_id)
    except Exception:
        db.rollback()
        logger.exception("Błąd podczas zapisu payloadu")
        raise HTTPException(status_code=500, detail="Błąd zapisu do bazy")
    finally:
        db.close()

    logger.info(
        "Przyjęto payload: automation=%s session=%s metrics=%d points=%d workouts=%d "
        "raw=%dKB gz=%dKB (%.0f%% redukcji)",
        automation_name, session_id, counts["metrics"], counts["metric_points"], counts["workouts"],
        size_raw // 1024, size_gz // 1024,
        (1 - size_gz / size_raw) * 100 if size_raw else 0,
    )

    return {"status": "ok", **counts,
            "raw_size_kb": size_raw // 1024, "compressed_size_kb": size_gz // 1024}
