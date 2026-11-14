# Stream clock

A web app for clocking in and out of scheduled work sessions, reviewing session history, and
attributing imported order exports to sessions. The app is the source of truth; Google Sheets
integration is optional.

## Features

- `/clock` and `/t/<token>` provide kiosk and NFC-tag clock flows.
- `/day` shows current and historical sessions with CSV export.
- `/sales` imports order exports and summarizes sales by session.
- `/admin` manages people, shops, tags, schedules, history, and settings.
- Missed clock-outs can close automatically; session changes are recorded in history.
- Optional Google Sheets mirroring and scheduled PostgreSQL backups.

## Local setup

Requires Python and the packages in `requirements.txt`.

```sh
python -m pip install -r requirements.txt
cp .env.example .env
# Set unique ADMIN_PASSWORD and SECRET_KEY values in .env.
python seed.py
uvicorn app.main:app --reload --env-file .env
```

Open <http://localhost:8000/clock>. Local default storage is SQLite at
`data/streamclock.db`. The seed command creates generic sample people, shops, and a persistent tag
link without adding session records. To add example sessions to an empty database, run
`python seed.py --samples`.

## Docker

The Compose setup runs the app with PostgreSQL and a backup service. Copy `.env.example` to `.env`,
set unique passwords and a `SECRET_KEY`, then configure `PUBLIC_URL` to the URL where the app will
be reached.

```sh
docker compose up -d --build
docker compose exec app python seed.py
```

The app port is bound to `127.0.0.1:8000` by default. Configure a trusted reverse proxy or the
optional Cloudflare Tunnel profile before exposing it publicly. Set `FORWARDED_ALLOW_IPS` to the
proxy's actual address; do not trust arbitrary forwarded headers or use `*`.

Keep `.env`, tunnel tokens, service-account keys, and database backups private. They are excluded
from Git. Set `BACKUP_DIR` to a mounted backup location for a separate backup copy, and periodically
verify that an archive can be restored.

## Google Sheets (optional)

Create a Google service account, enable the Sheets API, and share the spreadsheet with that account.
Set `GOOGLE_SERVICE_ACCOUNT_JSON` and `GOOGLE_SHEET_ID` in `.env`, then use **Send now** in the
admin interface. Each send replaces the configured tabs rather than appending duplicate rows.

## Tests

```sh
pytest -q
```

Tests requiring PostgreSQL use `POSTGRES_TEST_URL` to connect to a disposable database. Without
that variable, those tests are skipped.
