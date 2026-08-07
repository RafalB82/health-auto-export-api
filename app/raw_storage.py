"""
Zapis surowych payloadów jako pliki .json.gz na dysku zamiast BLOB w SQLite.

Powód: przy dniach treningowych ~100MB surowego JSON-a (HR live + trasa GPS
próbkowana co sekundę), trzymanie tego w kolumnie TEXT w SQLite:
  - spowalnia inserty proporcjonalnie do rozmiaru pliku bazy (fragmentacja stron)
  - przyspiesza zużycie karty SD w RPi (ciągły zapis dużych bloków do jednego pliku)
  - utrudnia backup (cała baza rośnie, zamiast tylko katalog z plikami)

Na realnych danych testowych gzip daje ~84% redukcji (JSON kompresuje się
dobrze) - dzień 100MB spada do ~15MB na dysku.

Struktura katalogów: raw_payloads/<rok>/<miesiąc>/<dzień>/<timestamp>-<session>.json.gz
Podział po dacie ułatwia ręczne zarządzanie (np. przenoszenie starszych
miesięcy na inny dysk) bez dotykania kodu czy bazy.
"""
from __future__ import annotations

import gzip
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

# Katalog bazowy na surowe payloady - względem katalogu roboczego procesu.
# Zmień na bezwzględną ścieżkę (np. na osobny dysk USB), jeśli RPi ma taki podpięty.
RAW_STORAGE_DIR = Path("raw_payloads")

_SAFE_CHARS = re.compile(r"[^A-Za-z0-9._-]")


def _sanitize(value: Optional[str]) -> str:
    """Usuwa znaki niebezpieczne dla nazwy pliku (np. z session-id, gdyby zawierał '/')."""
    if not value:
        return "none"
    return _SAFE_CHARS.sub("_", value)[:64]


def save_raw_payload(raw_body: bytes, received_at: datetime,
                      session_id: Optional[str]) -> tuple[str, int, int]:
    """Zapisuje raw_body jako skompresowany plik. Zwraca (ścieżka_względna, rozmiar_raw, rozmiar_gz).

    Ścieżka jest względna do RAW_STORAGE_DIR, żeby przeniesienie całego katalogu
    (np. na inny dysk) nie wymagało migracji rekordów w bazie."""
    day_dir = RAW_STORAGE_DIR / f"{received_at:%Y}" / f"{received_at:%m}" / f"{received_at:%d}"
    day_dir.mkdir(parents=True, exist_ok=True)

    filename = f"{received_at:%Y%m%d-%H%M%S}-{_sanitize(session_id)}.json.gz"
    full_path = day_dir / filename

    # Jeśli w tej samej sekundzie przyjdzie drugi request (batch), dopisz licznik
    counter = 1
    while full_path.exists():
        filename = f"{received_at:%Y%m%d-%H%M%S}-{_sanitize(session_id)}-{counter}.json.gz"
        full_path = day_dir / filename
        counter += 1

    compressed = gzip.compress(raw_body, compresslevel=6)
    full_path.write_bytes(compressed)

    relative_path = str(full_path.relative_to(RAW_STORAGE_DIR.parent) if RAW_STORAGE_DIR.is_absolute()
                         else full_path)
    return relative_path, len(raw_body), len(compressed)


def load_raw_payload(file_path: str) -> bytes:
    """Odczytuje i dekompresuje payload z powrotem do surowego JSON-a (bytes).
    Do reprocessingu, jeśli kiedyś zmieni się logika agregacji/parsowania."""
    full_path = Path(file_path)
    with gzip.open(full_path, "rb") as f:
        return f.read()
