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
from typing import Optional

from fastapi import FastAPI, Header, HTTPException, Request, Depends
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.db import init_db, get_db, SessionLocal
from app.db import RawPayload
from app.schemas import HealthExportPayload
from app.ingest import ingest_payload

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
        # Surowy payload zapisujemy zawsze, niezależnie od dalszego parsowania -
        # to Twoja siatka bezpieczeństwa przy zmianach schematu po stronie Apple/appki.
        db.add(RawPayload(
            automation_name=automation_name,
            automation_id=automation_id,
            automation_period=automation_period,
            automation_aggregation=automation_aggregation,
            session_id=session_id,
            payload=raw_body.decode("utf-8", errors="replace"),
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
        "Przyjęto payload: automation=%s session=%s metrics=%d points=%d workouts=%d",
        automation_name, session_id, counts["metrics"], counts["metric_points"], counts["workouts"],
    )

    return {"status": "ok", **counts}
