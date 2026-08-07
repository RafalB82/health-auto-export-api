# Health Auto Export -> REST API -> SQLite

Serwer FastAPI odbierający dane z aplikacji Health Auto Export
(automatyzacja REST API, format JSON, Export Version 2).

Przetestowano na realnym eksporcie (27 metryk, 1 trening rowerowy z GPS,
5491 punktów trasy, 1121 punktów HR) — patrz sekcja Testy.

## Uruchomienie

```bash
pip install -r requirements.txt --break-system-packages   # lub w venv bez tej flagi
uvicorn app.main:app --host 0.0.0.0 --port 8001
```

Baza SQLite (`health_data.db`) tworzy się automatycznie przy starcie w bieżącym katalogu.

### Uruchomienie jako systemd service (zalecane w produkcji)

Zamiast zwykłego `nohup` (który nie przeżywa restartów ani awarii), webhook można
uruchomić jako **user service** — autostart przy boot, automatyczny restart przy
awarii lub zawieszeniu (np. cykliczny event loop z wysokim CPU).

```bash
cp health-api.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now health-api.service
# sprawdzenie
systemctl --user status health-api.service
```

Działa w tle bez zalogowania dzięki `Linger=yes` dla użytkownika (`loginctl enable-linger rafal`).
Plik `health-api.service`:

```ini
[Unit]
Description=Health Auto Export REST API webhook (FastAPI -> SQLite)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/home/rafal/health-auto-export-api
ExecStart=/home/rafal/health-auto-export-api/.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8001
Restart=always
RestartSec=5
TimeoutStopSec=20
NoNewPrivileges=true
PrivateTmp=true

# Logi do pliku (journald user bywa niepersystentny) - obserwujesz na żywo
# przyrost danych przez: tail -f server.log
StandardOutput=append:/home/rafal/health-auto-export-api/server.log
StandardError=append:/home/rafal/health-auto-export-api/server.log

[Install]
WantedBy=default.target
```

Klucz API czytany jest z `.env` (patrz niżej), więc nie trzeba przekazywać go w `ExecStart`.

### Klucz API

Klucz czytany jest w kolejności: zmienna env `HEALTH_API_KEY` → plik `.env` (obok projektu) → domyślny `change-me`. Wartość w `.env` może być podana jako base64 (żeby nie konfliktować z maskowaniem sekretów w shellu):

```
HEALTH_API_KEY=ZGV2LWxvY2FsLWtleQ==   # base64 z 'dev-local-key'
```

Bez ustawionego klucza endpoint loguje ostrzeżenie i przyjmuje dane bez autoryzacji (tryb dev) — w produkcji ZAWSZE ustaw klucz.

## Konfiguracja w Health Auto Export

Automations -> New Automation -> REST API:

- **URL**: `http://<adres-serwera>:8001/health-data`
- **HTTP Headers**: dodaj `X-API-Key` = wartość z `HEALTH_API_KEY` / `.env`
- **Export Format**: JSON
- **Export Version**: 2
- **Batch Requests**: włącz dla treningów z GPS/długich okresów — patrz „Duże payloady"

## Struktura bazy

| Tabela | Zawartość |
|---|---|
| `raw_payloads` | Pełny surowy JSON każdego requestu — audyt, reprocessing przy zmianie schematu |
| `metric_points` | Metryki standardowe `{qty, date, source}` — steps, HR spoczynkowe, dystanse itd. |
| `metric_points_agg` | Metryki zagregowane `{Avg, Min, Max}` — np. `heart_rate` przy Summarize ON |
| `sleep_sessions` | `sleep_analysis` — osobny kształt (fazy snu: core/deep/rem/awake) |
| `workouts` | Nagłówek treningu — dystans, prędkość, HR avg/max, kalorie, itd. |
| `workout_heart_rate` | Szereg czasowy HR w trakcie treningu (`kind`: active/recovery) |
| `workout_energy` | Seria `activeEnergy` treningu — 1 wiersz/minutę (kJ) |
| `workout_steps` | Seria `stepCount` treningu — 1 wiersz/minutę |
| `workout_route` | Punkty GPS trasy (lat/lon/altitude/timestamp) |

## Idempotencja i serie czasowe

Metryki i sesje snu mają `UniqueConstraint` po `(nazwa, data, źródło)` — powtórny sync robi UPDATE, nie duplikuje wierszy.

Treningi identyfikowane są po natywnym UUID z Apple Health (`workouts.id`). Serie czasowe treningu (HR, energia, kroki) zapisywane są **per-minutę z upsertem po `(workout_id, date)`** — dzięki temu **trening rośnie naturalnie w ciągu dnia**: kolejne sync'e dodają nowe minuty, a powtórki istniejących minut robią UPDATE (bez churnu delete+insert). To obsługuje scenariusz, gdzie dane przyrastają z każdą synchronizacją/aktywnością.

Trasa (`workout_route`) to pełna ścieżka — nadpisywana (delete+insert) przy powtórce.

## Deduplikacja treningów (warstwa odczytu)

Health Auto Export potrafi wysłać ten sam fizyczny wysiłek **dwa razy** (np. z zegarka Apple Watch i z iPhone'a/Hevy), pod różnym UUID i lekko innym `duration`/`source`. Idempotencja po `id` ich nie łączy.

Warstwa odczytu (`app/query.py::get_workouts_deduped`) grupuje treningi po **(data, nazwa, czas startu do minuty)** i zwraca jednego reprezentanta (najdłuższy zapis) z flagą `deduped_copies`. Baza pozostaje wierna oryginalnym rekordom — dedupe dotyczy tylko odpowiedzi.

## Agregacja minutowa (heart_rate)

`heart_rate` jest wpisany w `HIGH_GRANULARITY_METRICS` w `app/ingest.py` —
zamiast insertować każdy surowy punkt, dane grupowane są do 1 wiersza/minutę
(`avg`/`min`/`max`/`sample_count`) zarówno w `metric_points_agg`, jak i w
`workout_heart_rate` (osobno dla `kind="active"` i `kind="recovery"`).

Na testowym pliku: 1097 surowych punktów HR z treningu (próbkowanie co ~5s)
zredukowane do 92 wierszy (1 wiersz/minutę) — ok. 12x mniej rekordów, bez
utraty żadnej informacji przydatnej do analizy treningu/regeneracji (avg do
wykresu, min/max do wychwycenia skoków). Surowe punkty nadal są dostępne w
`raw_payloads`, gdyby kiedyś potrzebna była wyższa rozdzielczość.

Żeby dodać kolejną metrykę do agregacji (np. jeśli zaczniesz eksportować
`blood_oxygen_saturation` z podobną granularnością), dopisz jej nazwę do
zbioru `HIGH_GRANULARITY_METRICS` w `app/ingest.py` — bez zmian w reszcie kodu.

## Surowe payloady — pliki, nie BLOB w bazie

Przy dniach treningowych (~100 MB surowego JSON-a z HR live + trasą GPS
próbkowaną co sekundę) trzymanie tego w kolumnie SQLite `TEXT` degraduje
wydajność insertów i przyspiesza zużycie karty SD. Zamiast tego każdy
request zapisywany jest jako skompresowany plik:

```
raw_payloads/<rok>/<miesiац>/<dzien>/<timestamp>-<session-id>.json.gz
```

W SQLite (`raw_payloads`) zostaje tylko ścieżka + rozmiar przed/po kompresji.
Na realnym pliku testowym gzip dał **84.5% redukcji** — dzień 100 MB spada
do ~15 MB na dysku, czyli ~5.5 GB/rok przy codziennych treningach. Zero utraty
danych: `app/raw_storage.py::load_raw_payload()` odtwarza oryginalny JSON
bajt w bajt, do reprocessingu przy zmianie logiki agregacji.

Katalog `raw_payloads/` nie jest automatycznie czyszczony — to świadoma
decyzja: to tani, skompresowany log plikowy, a nie ciężki BLOB w bazie.
Zarządzaj nim ręcznie (np. `rsync` starszych miesięcy na inny dysk).

## Dostęp przez MCP (odpytywanie danych treningowych przez asystenta)

`mcp_server.py` wystawia dane treningowe jako narzędzia MCP **po stdio**. Prosty
lokalny transport: klient MCP (OpenClaw / trainer) uruchamia ten proces
bezpośrednio na tym samym hoście, bez sieci.

**Wymaga osobnego środowiska** (`requirements-mcp.txt`) — pakiet `mcp` podnosi
`starlette` do wersji niezgodnej z FastAPI używanym przez webhook, więc oba
serwery nie mogą współdzielić jednego venv. To i tak ma sens architektonicznie:
webhook (FastAPI, always-on HTTP) i MCP (stdio, uruchamiany na żądanie klienta)
to różne procesy o różnym cyklu życia — łączy je wspólny plik `health_data.db`
i katalog `raw_payloads/`.

```bash
python3 -m venv venv-mcp
venv-mcp/bin/pip install -r requirements-mcp.txt
```

Konfiguracja w OpenClaw (`openclaw.json` → `mcp.servers.health-data`):
```json
{
  "command": "/home/rafal/health-auto-export-api/venv-mcp/bin/python",
  "args": ["/home/rafal/health-auto-export-api/mcp_server.py"]
}
```

**Narzędzia MCP — 4 grupy (A/B/C/D):**

#### Grupa A — `workout_*` (konkretne sesje treningowe)

| Narzędzie | Źródło | Opis |
|---|---|---|
| `list_recent_workouts(limit)` | SQLite | Lista ostatnich treningów z dedupe (id, nazwa, czas, dystans, HR, kroki, kcal, kopie) |
| `get_workout_summary(workout_id)` | SQLite | Podsumowanie sesji: dystans, prędkość, HR avg/max, kroki, kcal, energia, elewacja |
| `get_workout_hr_minutely(workout_id)` | SQLite | Tętno treningu 1 punkt/minutę (avg/min/max/count) |
| `get_workout_series(workout_id)` | SQLite | Pełny profil: energia + kroki + HR na minutę + sumaryczne kcal/kroki |
| `get_workout_hr_trace(workout_id)` | Plik `.json.gz` | Pełne HR sekunda-po-sekundzie z surowego payloadu (wolniejsze) |

#### Grupa B — `daily_*` (aktywność dzienna)

| Narzędzie | Opis |
|---|---|
| `get_daily_activity(date)` | Kroki, dystans, kalorie (aktywne+basal), RHR, HRV, sen za dzień |
| `get_daily_activity_range(start, end)` | Serie dzienne (kroki/kalorie/RHR/HRV/sen) do trendów |
| `get_daily_series(date, metric, bucket)` | Profil metryki w ciągu dnia (godzinowo lub minutowo) |

#### Grupa C — `metrics_*` (regeneracja: sen, HRV, RHR)

| Narzędzie | Opis |
|---|---|
| `get_metric_trend(metric, start, end)` | Trend dzienny metryki: `sleep`, `resting_heart_rate`, `heart_rate_variability`, `step_count`, `active_energy`, ... |

#### Grupa D — `generic_*` (uniwersalny odczyt dowolnych danych) ⭐

| Narzędzie | Opis |
|---|---|
| `list_data()` | Lista WSZYSTKICH dostępnych danych (metryki, sen, treningi) |
| `get_data(name, aggregation, from, to, detail)` | Generyczny odczyt dowolnej metryki/obszaru (`metrics`, `sleep`, `workouts`, lub dowolna metryka np. `apple_sleeping_wrist_temperature`) |

Grupa D czyta **dowolną metrykę w bazie bez definiowania jej z góry** — każda nowa metryka, która wpadnie do bazy, jest automatycznie odpytywalna. Domyślnie zwraca zwarte podsumowanie (min/max/avg/latest + trend) zamiast surowych punktów, z **capem 500 punktów**, żeby nie generować zbędnych tokenów. Surowe punkty tylko przy `detail=true`.

**Agregacja dzienna:** `step_count` → suma, `heart_rate_variability` → mediana, `resting_heart_rate` → ostatni pomiar; sen z osobnej tabeli (total/core/deep/rem/awake).

`get_workout_hr_trace` **nie zgaduje** pliku po dacie — `Workout` ma kolumnę `raw_payload_id` ustawianą w ingestcie, więc trafienie w plik jest gwarantowane.

## Architektura i struktura plików

Projekt składa się z **dwóch niezależnych procesów** łączących się tym samym plikiem `health_data.db` i katalogiem `raw_payloads/`:

1. **Webhook** (`app/main.py`, FastAPI, always-on HTTP na :8001) — odbiera dane z Health Auto Export, zapisuje raw payloady jako pliki, ingeruje do tabel. Endpointy: `GET /` (liveness), `GET /health`, `POST /health-data` (główny), `POST /health-data/test` (walidacja bez zapisu).
2. **MCP server** (`mcp_server.py`, stdio) — uruchamiany na żądanie przez klienta (OpenClaw/trainer) do odpytywania danych treningowych przez asystenta.

```
health-auto-export-api/
├── app/
│   ├── __init__.py
│   ├── main.py          # FastAPI webhook
│   ├── db.py            # SQLAlchemy: schemat (11 tabel)
│   ├── schemas.py       # Pydantic: walidacja payloadu (JSON v2)
│   ├── ingest.py        # parsowanie + zapis + agregacja minutowa
│   ├── raw_storage.py   # save/load raw payloadów jako .json.gz
│   └── query.py         # warstwa odczytu (MCP): workout/daily/metrics/generic
├── mcp_server.py        # MCP (stdio) — 11 narzędzi w 4 grupach
├── requirements.txt     # webhook
├── requirements-mcp.txt # MCP (osobny venv-mcp)
├── .env                 # klucz API (nie w repo)
└── raw_payloads/        # surowe payloady (nie w repo)
```

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

- `HEALTH_API_KEY` musi być ustawiony (env lub `.env`) — bez niego endpoint
  loguje ostrzeżenie i **przyjmuje dane bez autoryzacji**.
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
