# SPDX-License-Identifier: LGPL-2.1-or-later
# Copyright (C) 2026 G4OCCT Contributors

# G4OCCT Onshape Application

An OAuth-authenticated Onshape iframe tab that lets physicists and engineers
trigger [Geant4](https://github.com/geant4/geant4) simulations driven by CAD
geometry from within the Onshape browser UI, without manually exporting STEP
files or running G4OCCT locally.

---

## Architecture

```
Onshape iframe  ──HTTPS──►  App Server  ──job dispatch──►  G4OCCT Worker
                                 │                              │
                                 └── Onshape REST API proxy ◄──┘
```

| Component | Technology |
|---|---|
| App Server | Python · FastAPI |
| Job queue | SQLite (development) |
| Frontend | Plain HTML + JS |
| Worker | Python polling loop + G4OCCT binary |
| Container | Docker / Apptainer |

---

## Quick Start (local development)

### 1. Register an Onshape OAuth application

1. Go to the [Onshape Developer Portal](https://dev-portal.onshape.com/).
2. Create a new OAuth application.
3. Set the **Redirect URI** to `http://localhost:8000/oauth/callback`.
4. Note the **Client ID** and **Client Secret**.

### 2. Configure environment

```bash
cp .env.example .env
# Edit .env and fill in ONSHAPE_CLIENT_ID, ONSHAPE_CLIENT_SECRET, etc.
```

### 3. Run with Docker Compose

```bash
docker compose up --build
```

The App Server is now available at <http://localhost:8000>.  Open
<http://localhost:8000/app> to see the iframe UI.

### 4. Register the iframe tab in Onshape

In the Onshape Developer Portal, register an **iframe extension** pointing at
`http://localhost:8000/app` (for local testing use an ngrok/Cloudflare Tunnel
URL so Onshape can reach your machine over HTTPS).

---

## Running without Docker

```bash
cd onshape-app/server
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
# Copy and edit .env at the repository root
uvicorn app:app --reload --port 8000
```

---

## Local Worker

Start a local simulation worker that polls the App Server for jobs:

```bash
docker run --rm \
  -e G4OCCT_SERVER=https://<app-server-host> \
  -e G4OCCT_WORKER_TOKEN=<token-from-app-ui> \
  ghcr.io/wdconinc/g4occt-worker:latest
```

Or with Apptainer on an HPC system:

```bash
apptainer build g4occt-worker.sif onshape-app/worker/apptainer.def
G4OCCT_SERVER=https://<app-server-host> \
G4OCCT_WORKER_TOKEN=<token> \
apptainer run g4occt-worker.sif
```

---

## Directory Structure

```
onshape-app/
├── server/          App Server (OAuth, API proxy, job queue)
│   ├── app.py       FastAPI application
│   ├── oauth.py     Onshape OAuth 2.0 helpers
│   ├── jobs.py      SQLite-backed job queue
│   ├── requirements.txt
│   └── Dockerfile
├── worker/          G4OCCT simulation worker
│   ├── run_worker.py    Polling loop + simulation runner
│   ├── Dockerfile
│   └── apptainer.def
└── frontend/        iframe UI (served as static files by the App Server)
    ├── index.html
    ├── app.js
    └── style.css
```

---

## API Reference

### OAuth

| Endpoint | Description |
|---|---|
| `GET /oauth/start` | Redirect to Onshape authorisation page |
| `GET /oauth/callback` | Handle authorisation code; store tokens server-side |
| `GET /oauth/logout` | Clear session |

### iframe

| Endpoint | Description |
|---|---|
| `GET /app` | Serve the iframe frontend (requires auth) |

### Onshape REST API Proxy

All calls use the server-side stored access token — tokens are **never**
sent to the browser.

| Endpoint | Description |
|---|---|
| `GET /api/element/metadata` | Element name and type |
| `POST /api/element/export-step` | Export STEP geometry from Part Studio or Assembly |

### Job Management

| Endpoint | Description |
|---|---|
| `GET /api/jobs` | List user's jobs |
| `POST /api/jobs` | Submit a new simulation job |
| `GET /api/jobs/{id}` | Get job status and results |

### Worker API

Workers authenticate with `X-Worker-Token` header.

| Endpoint | Description |
|---|---|
| `POST /workers/register` | Register or refresh a worker |
| `GET /workers` | List registered workers |
| `GET /jobs/next?worker_id=…` | Claim the next queued job |
| `POST /jobs/{id}/result` | Submit simulation results |

---

## Security Notes

* OAuth tokens are stored **server-side** in the session; they are never
  sent to the browser or the iframe JavaScript context.
* The `SESSION_SECRET` and `WORKER_TOKEN` environment variables must be
  set to strong random values in production.
* HTTPS is **required** in production — Onshape will not load non-HTTPS iframes.
* STEP file payloads are treated as ephemeral; they should be deleted after
  job completion in a production deployment.
* See [Phase 6 of the roadmap](https://github.com/wdconinc/G4OCCT-onshape)
  for planned security hardening.

---

## Development Roadmap

See the [planning document](https://github.com/wdconinc/G4OCCT-onshape/issues/1) for the full
six-phase roadmap.  Current status:

- [x] **Phase 1** — OAuth scaffold (server + iframe)
- [x] **Phase 2** — STEP export & metadata proxy
- [x] **Phase 3** — Job queue + remote worker HTTP interface
- [x] **Phase 4** — Job polling & results display in UI
- [x] **Phase 5** — Local worker outbound polling + Docker/Apptainer images
- [ ] **Phase 6** — Security hardening, performance, App Store submission

---

## License

LGPL-2.1-or-later · Copyright (C) 2026 G4OCCT Contributors
