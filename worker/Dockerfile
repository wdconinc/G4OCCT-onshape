# SPDX-License-Identifier: LGPL-2.1-or-later
# Copyright (C) 2026 G4OCCT Contributors
#
# Dockerfile for the G4OCCT simulation worker.
#
# Build:
#   docker build -t ghcr.io/wdconinc/g4occt-worker:latest .
#
# Run (local worker):
#   docker run --rm \
#     -e G4OCCT_SERVER=https://<app-server-host> \
#     -e G4OCCT_WORKER_TOKEN=<token-from-app-ui> \
#     ghcr.io/wdconinc/g4occt-worker:latest

# ── Base image: upstream G4OCCT image (includes Geant4 + OCCT) ────────────
# Replace with a published base image once one is available.
FROM ubuntu:24.04

# ── System dependencies ───────────────────────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-pip python3-venv \
        libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# ── Python dependencies ───────────────────────────────────────────────────
RUN python3 -m pip install --no-cache-dir httpx

# ── Worker script ─────────────────────────────────────────────────────────
WORKDIR /app
COPY run_worker.py /app/run_worker.py

# ── Environment defaults ──────────────────────────────────────────────────
ENV G4OCCT_SERVER=http://localhost:8000 \
    G4OCCT_WORKER_TOKEN="" \
    G4OCCT_POLL_INTERVAL=5 \
    GEANT4_VERSION="unknown" \
    OCCT_VERSION="unknown" \
    G4OCCT_VERSION="unknown"

# ── Entry point ───────────────────────────────────────────────────────────
CMD ["python3", "/app/run_worker.py"]
