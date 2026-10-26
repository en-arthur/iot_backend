# Relayboard backend

FastAPI service that bridges ESP32 boards on MQTT to Supabase Postgres. It is
the only component that talks to the broker: it ingests telemetry, keeps the
database current, validates Supabase access tokens, and publishes relay
commands requested by the web app.

## What it does

- Subscribes to `boards/+/status`, `boards/+/sensors/+`, and
  `boards/+/relays/+/state`
- Batches sensor readings before inserting them
- Auto-creates unknown boards with `owner_id = null`
- Validates Supabase JWTs (HS256 secret or JWKS)
- Enforces board ownership on write endpoints
- Exposes `POST /boards/claim`, `POST /boards/{id}/relays/{n}`, and
  `GET /health`

## Requirements

- Python 3.11
- A Supabase project with the migrations applied
- An MQTT broker reachable from the backend host

## Local run

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# edit .env
uvicorn app.main:app --reload --port 8000
```

Check it:

```bash
curl -s localhost:8000/health
```

## Deploy on Render

1. Push this repository to GitHub.
2. In Render: **New → Blueprint**, select the repo, and apply `render.yaml`.
   Or create a **Web Service** manually with:
   - **Build command:** `pip install -r requirements.txt`
   - **Start command:** `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
   - **Health check path:** `/health`
3. Add the environment variables from `.env.example` in the Render dashboard.
4. Use an **always-on instance**. Free web services spin down after inactivity,
   which stops the MQTT listener and the board status updates.
5. Set `CORS_ORIGINS` to your Vercel domain, e.g.
   `https://your-app.vercel.app`.

`Procfile` and `runtime.txt` are included for Railway/Heroku-style hosts.

## Critical: broker reachability

The backend must be able to open a TCP connection to Mosquitto. A broker on
your laptop (`localhost` or a private LAN IP) will **not** be reachable from a
cloud host. Run Mosquitto on the same VPS/private network as the backend, or
expose it through a secure tunnel/VPN. Keep port 1883 private and use
username/password authentication.

The ESP32 boards must reach the same broker. If the backend is in the cloud,
the broker must also be reachable from the boards' network.

## Environment variables

| Variable | Required | Notes |
| --- | --- | --- |
| `DATABASE_URL` | yes | Supabase direct connection or session pooler |
| `SUPABASE_URL` | yes | `https://<project-ref>.supabase.co` |
| `SUPABASE_JWT_SECRET` | one of | Legacy HS256 projects |
| `SUPABASE_JWKS_URL` | one of | Defaults to `${SUPABASE_URL}/auth/v1/.well-known/jwks.json` |
| `MQTT_HOST` | yes | Broker hostname/IP reachable from this service |
| `MQTT_PORT` | no | Default `1883` |
| `MQTT_USERNAME` | yes | Must match `mosquitto/passwd` |
| `MQTT_PASSWORD` | yes | Must match `mosquitto/passwd` |
| `MQTT_TOPIC_PREFIX` | no | Default `boards` |
| `MQTT_QOS` | no | Default `1` |
| `CORS_ORIGINS` | yes | Comma-separated frontend origins |
| `LOG_LEVEL` | no | Default `INFO` |
| `LOG_JSON` | no | Default `true` |

## API

All board endpoints require `Authorization: Bearer <Supabase access token>`.

| Method | Path | Result |
| --- | --- | --- |
| `GET` | `/health` | Database + MQTT status |
| `POST` | `/boards/claim` | Claim an unowned board |
| `POST` | `/boards/{id}/relays/{n}` | Publish `on`/`off` to the board |

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```
