# Health Auto Export -> REST API -> SQLite

Serwer FastAPI odbierający dane z aplikacji Health Auto Export
(automatyzacja REST API, format JSON, Export Version 2).

Przetestowano na realnym eksporcie (27 metryk, 1 trening rowerowy z GPS,
5491 punktów trasy, 1121 punktów HR) — patrz sekcja Testy.

## Uruchomienie

```bash
pip install -r requirements.txt --break-system-packages   # lub w venv bez tej flagi
export HEALTH_API_KEY="wybierz-mocny-losowy-klucz"
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Baza SQLite (`health_data.db`) tworzy się automatycznie przy starcie w bieżącym katalogu.

## Konfiguracja w Health Auto Export

Automations -> New Automation -> REST API:

- **URL**: `http://<adres-twojego-serwera>:8000/health-data`
- **HTTP Headers**: dodaj `X-API-Key` = wartość, którą ustawiłeś w `HEALTH_API_KEY`
- **Export Format**: JSON
- **Export Version**: 2
- **Batch Requests**: włącz, jeśli eksportujesz treningi z GPS lub długie okresy —
  patrz sekcja "Duże payloady" niżej

## Struktura bazy

| Tabela | Zawartość |
|---|---|
| `raw_payloads` | Pełny surowy JSON każdego requestu — audyt, reprocessing przy zmianie schematu |
| `metric_points` | Metryki standardowe `{qty, date, source}` — steps, HR spoczynkowe, dystanse itd. |
| `metric_points_agg` | Metryki zagregowane `{Avg, Min, Max}` — np. `heart_rate` przy Summarize ON |
| `sleep_sessions` | `sleep_analysis` — osobny kształt (fazy snu: core/deep/rem/awake) |
| `workouts` | Nagłówek treningu — dystans, prędkość, HR avg/max, kalorie, itd. |
| `workout_heart_rate` | Szereg czasowy HR w trakcie treningu (`kind`: active/recovery) |
| `workout_route` | Punkty GPS trasy (lat/lon/altitude/timestamp) |

## Idempotencja

Metryki i sesje snu mają `UniqueConstraint` po `(nazwa, data, źródło)` — powtórny
sync tego samego okna (np. przy Date Range "Since Last Sync") robi UPDATE,
nie duplikuje wierszy. Treningi kluczowane są po natywnym UUID z Apple Health —
powtórny sync usuwa stare punkty HR/trasy i wstawia świeże.

Sprawdzone empirycznie: dwukrotny import tego samego pliku daje identyczną
liczbę wierszy w każdej tabeli.

## Duże payloady (GPS, długie okresy)

Testowy plik (jeden dzień + jeden 88-minutowy trening rowerowy) miał
~1.3 MB i 39 tys. linii JSON. Dokumentacja Health Auto Export ostrzega,
że payloady z pełną trasą GPS mogą sięgać setek MB. Jeśli stawiasz to za
nginx/reverse proxy, ustaw tam `client_max_body_size` (np. `200m`) —
inaczej proxy utnie request zanim dotrze do FastAPI, zanim jeszcze zadziała
walidacja Pydantic.

Dla realnie dużych wolumenów (wiele automatyzacji dziennie, długie trasy)
włącz w apce **Batch Requests** — endpoint jest idempotentny, więc odbiór
tych samych danych w kilku mniejszych requestach jest bezpieczny.

## Bezpieczeństwo — do zrobienia przed produkcją

- `HEALTH_API_KEY` musi być ustawiony jako zmienna środowiskowa — bez niego
  endpoint loguje ostrzeżenie i **przyjmuje dane bez autoryzacji**.
- Serwer nie ma wbudowanego HTTPS — do wystawienia w sieci publicznej
  postaw za nginx/caddy z TLS, albo używaj Tailscale/VPN zamiast
  otwierania portu na świat.
- SQLite jest ok dla jednego urządzenia i umiarkowanego wolumenu; przy
  współbieżnych zapisach z wielu automatyzacji rozważ migrację na
  PostgreSQL (schemat SQLAlchemy przenosi się bez zmian, zmienia się
  tylko `DATABASE_URL` w `app/db.py`).

## Testy

Model danych zweryfikowany na realnym pliku eksportu:
- Pydantic waliduje wszystkie 27 typów metryk + workout bez błędów
- Trzy warianty kształtu punktu metryki (`qty` / `Avg-Min-Max` / sesja snu)
  poprawnie rozróżniane i zapisywane w odpowiednich tabelach
- Pełny łańcuch HTTP (FastAPI TestClient, prawdziwe nagłówki automation-*)
  przetestowany end-to-end, łącznie z autoryzacją i replay/idempotencją
