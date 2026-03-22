# G4OCCT-onshape

Onshape Application to interface CAD geometry with Geant4 through Open CASCADE
Technology (OCCT).

G4OCCT-onshape is an OAuth-authenticated iframe tab that embeds directly inside
an [Onshape](https://www.onshape.com/) document. Physicists and engineers can
trigger [Geant4](https://github.com/geant4/geant4) simulations of the active
Part Studio or Assembly geometry without ever leaving Onshape — no manual STEP
export, no local G4OCCT install required.

---

## How it works

```
┌─────────────────────────────────────────┐
│  Onshape (browser)                      │
│  ┌───────────────────────────────────┐  │
│  │  G4OCCT iframe tab                │  │
│  │  (served by App Server over HTTPS)│  │
│  └──────────────┬────────────────────┘  │
└─────────────────│───────────────────────┘
                  │ HTTPS + session cookie
                  ▼
┌─────────────────────────────────────────┐
│  G4OCCT App Server                      │
│  • OAuth 2.0 handler                    │
│  • Onshape REST API proxy (STEP export) │
│  • Job queue & dispatcher               │
└──────┬──────────────────────────────────┘
       │ job dispatch (STEP + config)
       ├──────────────────────────────────►  Remote worker (cloud / HPC)
       └──────────────────────────────────►  Local worker  (Docker / Apptainer)
```

Onshape OAuth tokens are kept **server-side** and are never sent to the browser.

---

## Repository layout

```
G4OCCT-onshape/
├── onshape-app/
│   ├── server/       FastAPI App Server (OAuth, API proxy, job queue)
│   ├── worker/       G4OCCT simulation worker (Docker + Apptainer)
│   └── frontend/     iframe UI (HTML + JS + CSS)
├── tests/            pytest test suite
├── docker-compose.yml
├── .env.example
└── README.md         ← you are here
```

Full documentation, quick-start guide, API reference, and deployment notes live
in **[onshape-app/README.md](onshape-app/README.md)**.

---

## Status & Roadmap

### ✅ Phases 1–5 — implemented in this repository

| Phase | Milestone | Status |
|---|---|---|
| 1 | OAuth scaffold — `/oauth/start`, `/oauth/callback`, iframe served | **Done** |
| 2 | STEP export & element metadata proxy | **Done** |
| 3 | Job queue + remote worker HTTP interface | **Done** |
| 4 | Job polling & results display in the iframe UI | **Done** |
| 5 | Local worker (outbound polling, Docker & Apptainer images) | **Done** |
| 6 | Security hardening, performance, App Store submission | Planned |

### ⬜ Immediate next steps — human action required

The following tasks require access to external accounts, infrastructure, or
decisions that cannot be automated:

1. **Register the Onshape OAuth application**
   — Create an OAuth app in the
   [Onshape Developer Portal](https://dev-portal.onshape.com/), set the redirect
   URI to `https://<app-host>/oauth/callback`, and record the `CLIENT_ID` and
   `CLIENT_SECRET`.

2. **Register the iframe tab extension**
   — In the Developer Portal, add an **iframe extension** pointing at
   `https://<app-host>/app`.  The extension appears as a tab inside Onshape
   documents once installed.

3. **Deploy the App Server to a public HTTPS host**
   — Onshape requires HTTPS for iframe content.  Deploy the server container
   (see `onshape-app/server/Dockerfile` and `docker-compose.yml`) to a cloud VM
   or container platform and configure TLS (e.g. Let's Encrypt / Caddy).

4. **Choose and provision compute infrastructure for the remote worker**
   — Decide where remote G4OCCT workers will run (public cloud, NERSC, or
   institutional HPC) and deploy the worker image
   (`onshape-app/worker/Dockerfile`).

5. **Build and publish the worker container image**
   — Build and push `ghcr.io/wdconinc/g4occt-worker:latest` to the GitHub
   Container Registry so users can pull it without building from source.

6. **Define the material mapping strategy**
   — Decide how Onshape material names map to Geant4 `G4Material` entries
   (see the [material bridging docs](https://wdconinc.github.io/G4OCCT/#/material_bridging)).

7. **Decide on simulation scope for the first public release**
   — Start with geantino navigation scans (geometry validation only) or include
   full physics runs from the outset?

8. **Security review before opening to external users**
   — Review token scoping, STEP file sanitisation, multi-tenancy isolation, and
   worker token rotation policies (Phase 6 of the roadmap).

---

## Getting started (developers)

```bash
# 1. Clone the repository
git clone https://github.com/wdconinc/G4OCCT-onshape.git
cd G4OCCT-onshape

# 2. Copy and fill in the environment file
cp .env.example .env   # set ONSHAPE_CLIENT_ID, ONSHAPE_CLIENT_SECRET, etc.

# 3. Start the full stack locally
docker compose up --build
```

See **[onshape-app/README.md](onshape-app/README.md)** for the full quick-start
guide, API reference, and deployment instructions.

---

## License

LGPL-2.1-or-later · Copyright (C) 2026 G4OCCT Contributors
