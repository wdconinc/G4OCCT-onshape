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

# ── Base image: EIC XL nightly (includes Geant4 + OCCT + full HEP stack) ──
FROM ghcr.io/eic/eic_xl:nightly

# ── System dependencies for Python worker ────────────────────────────────
# The eic_xl image includes cmake, gcc, Geant4, and OpenCASCADE.
# We add git (to clone G4OCCT) and the Python pip installer.
RUN apt-get update && apt-get install -y --no-install-recommends \
        git \
        python3-pip \
    && rm -rf /var/lib/apt/lists/*

# ── Build and install G4OCCT ──────────────────────────────────────────────
# Clone the G4OCCT library from GitHub, build it against the Geant4 and
# OpenCASCADE installations provided by the base image, then install it
# to /usr/local so that downstream CMake projects can find it.
ARG G4OCCT_REF=main
RUN git clone --depth 1 --branch "${G4OCCT_REF}" \
        https://github.com/eic/G4OCCT.git /tmp/G4OCCT \
    && cmake -S /tmp/G4OCCT -B /tmp/G4OCCT/build \
        -DCMAKE_BUILD_TYPE=Release \
        -DBUILD_TESTING=OFF \
        -DBUILD_EXAMPLES=OFF \
        -DCMAKE_INSTALL_PREFIX=/usr/local \
    && cmake --build /tmp/G4OCCT/build -- -j"$(nproc)" \
    && cmake --install /tmp/G4OCCT/build \
    && rm -rf /tmp/G4OCCT

# ── Build and install the G4OCCT runner ───────────────────────────────────
# The runner is a Geant4 application that loads STEP geometry via G4OCCT,
# runs a simulation with the requested parameters, and writes JSON results.
COPY g4occt_runner/ /tmp/g4occt_runner/
RUN cmake -S /tmp/g4occt_runner -B /tmp/g4occt_runner/build \
        -DCMAKE_BUILD_TYPE=Release \
        -DCMAKE_INSTALL_PREFIX=/usr/local \
    && cmake --build /tmp/g4occt_runner/build -- -j"$(nproc)" \
    && cmake --install /tmp/g4occt_runner/build \
    && rm -rf /tmp/g4occt_runner

# ── Python dependencies ───────────────────────────────────────────────────
RUN python3 -m pip install --no-cache-dir --break-system-packages httpx

# ── Worker script ─────────────────────────────────────────────────────────
WORKDIR /app
COPY run_worker.py /app/run_worker.py

# ── Environment defaults ──────────────────────────────────────────────────
# Note: G4OCCT_WORKER_TOKEN must be supplied at runtime via -e or docker-compose.
# Geant4 and OCCT versions are optionally provided via environment variables
# (e.g. GEANT4_VERSION, OCCT_VERSION, G4OCCT_VERSION) and may be reported
# as "unknown" if not set by the base image or at runtime.
ENV G4OCCT_SERVER=http://localhost:8000 \
    G4OCCT_POLL_INTERVAL=5 \
    G4OCCT_RUNNER=g4occt_runner

# ── Entry point ───────────────────────────────────────────────────────────
CMD ["python3", "/app/run_worker.py"]
