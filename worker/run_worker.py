# SPDX-License-Identifier: LGPL-2.1-or-later
# Copyright (C) 2026 G4OCCT Contributors
#
# G4OCCT simulation worker.
#
# This script runs inside the worker container and connects back to the
# App Server to claim and execute simulation jobs.  It uses an outbound
# polling loop so that no inbound network access to the worker is required;
# only the worker needs to be able to reach the App Server.
#
# Usage (local worker):
#
#   export G4OCCT_SERVER=https://<app-server-host>
#   export G4OCCT_WORKER_TOKEN=<token-from-app-ui>
#   python run_worker.py
#
# Or via Docker / Apptainer:
#
#   docker run --rm \
#     -e G4OCCT_SERVER=https://<app-server-host> \
#     -e G4OCCT_WORKER_TOKEN=<token> \
#     ghcr.io/wdconinc/g4occt-worker:latest

import base64
import json
import logging
import os
import subprocess
import sys
import tempfile
import time
import uuid

try:
    import httpx
except ImportError:
    sys.exit("httpx is required: pip install httpx")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("g4occt-worker")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SERVER_URL = os.environ.get("G4OCCT_SERVER", "http://localhost:8000").rstrip("/")
WORKER_TOKEN = os.environ.get("G4OCCT_WORKER_TOKEN", "")
WORKER_ID = os.environ.get("G4OCCT_WORKER_ID", f"worker-{uuid.uuid4().hex[:8]}")
POLL_INTERVAL = int(os.environ.get("G4OCCT_POLL_INTERVAL", "5"))  # seconds

GEANT4_VERSION = os.environ.get("GEANT4_VERSION", "unknown")
OCCT_VERSION = os.environ.get("OCCT_VERSION", "unknown")
G4OCCT_VERSION = os.environ.get("G4OCCT_VERSION", "unknown")

HEADERS = {"X-Worker-Token": WORKER_TOKEN, "Content-Type": "application/json"}


# ---------------------------------------------------------------------------
# Worker registration
# ---------------------------------------------------------------------------

def register_worker(client: httpx.Client) -> None:
    payload = {
        "worker_id": WORKER_ID,
        "capabilities": {
            "geant4_version": GEANT4_VERSION,
            "occt_version": OCCT_VERSION,
            "g4occt_version": G4OCCT_VERSION,
        },
    }
    resp = client.post(f"{SERVER_URL}/workers/register", json=payload, headers=HEADERS, timeout=10)
    resp.raise_for_status()
    log.info("Registered worker %s with server %s", WORKER_ID, SERVER_URL)


# ---------------------------------------------------------------------------
# Job polling
# ---------------------------------------------------------------------------

def poll_for_job(client: httpx.Client) -> dict | None:
    resp = client.get(
        f"{SERVER_URL}/jobs/next",
        params={"worker_id": WORKER_ID},
        headers=HEADERS,
        timeout=10,
    )
    if resp.status_code == 204:
        return None
    resp.raise_for_status()
    return resp.json()


# ---------------------------------------------------------------------------
# Simulation runner
# ---------------------------------------------------------------------------

def run_simulation(job: dict) -> dict:
    """Run the G4OCCT simulation and return a results dict.

    In a production deployment this would:
    1. Write the STEP data to a temp file.
    2. Invoke the compiled G4OCCT binary (e.g. ``g4occt_runner``).
    3. Parse the output JSON and return it.

    Here we provide a stub implementation that validates the STEP data exists
    and returns placeholder diagnostics.
    """
    sim_config = json.loads(job.get("sim_config", "{}"))
    step_data = job.get("step_data", "")

    if not step_data:
        raise RuntimeError("No STEP data in job payload")

    with tempfile.TemporaryDirectory() as tmpdir:
        step_path = os.path.join(tmpdir, "geometry.step")
        with open(step_path, "wb") as fh:
            fh.write(base64.b64decode(step_data))

        log.info(
            "Running %s with %d events, particle=%s",
            sim_config.get("type", "geantino_scan"),
            sim_config.get("nEvents", 1000),
            sim_config.get("particleType", "geantino"),
        )

        # Attempt to invoke the real G4OCCT runner; fall back to stub output.
        runner = os.environ.get("G4OCCT_RUNNER", "g4occt_runner")
        output_path = os.path.join(tmpdir, "results.json")

        # Write a JSON steering file so the runner can also be driven that way.
        steering = {
            "step": step_path,
            "type": sim_config.get("type", "geantino_scan"),
            "particle": sim_config.get("particleType", "geantino"),
            "nEvents": sim_config.get("nEvents", 1000),
            "output": output_path,
        }
        steering_path = os.path.join(tmpdir, "steering.json")
        with open(steering_path, "w") as fh:
            json.dump(steering, fh)

        try:
            result = subprocess.run(
                [
                    runner,
                    "--step", step_path,
                    "--type", sim_config.get("type", "geantino_scan"),
                    "--particle", sim_config.get("particleType", "geantino"),
                    "--events", str(sim_config.get("nEvents", 1000)),
                    "--output", output_path,
                ],
                capture_output=True,
                text=True,
                timeout=3600,
            )
            if result.returncode != 0:
                raise RuntimeError(result.stderr)
            with open(output_path) as fh:
                return json.load(fh)
        except FileNotFoundError:
            # Runner not installed – return a diagnostic stub.
            log.warning("G4OCCT runner not found; returning stub results")
            return {
                "status": "stub",
                "message": "G4OCCT runner not installed in this environment",
                "step_file_bytes": os.path.getsize(step_path),
                "simulation_config": sim_config,
            }


# ---------------------------------------------------------------------------
# Result submission
# ---------------------------------------------------------------------------

def submit_result(client: httpx.Client, job_id: str, results: dict) -> None:
    resp = client.post(
        f"{SERVER_URL}/jobs/{job_id}/result",
        json={"status": "complete", "worker_id": WORKER_ID, "results": results},
        headers=HEADERS,
        timeout=30,
    )
    resp.raise_for_status()
    log.info("Submitted result for job %s", job_id)


def submit_failure(client: httpx.Client, job_id: str, error: str) -> None:
    client.post(
        f"{SERVER_URL}/jobs/{job_id}/result",
        json={"status": "failed", "worker_id": WORKER_ID, "error": error},
        headers=HEADERS,
        timeout=30,
    )
    log.error("Reported failure for job %s: %s", job_id, error)


# ---------------------------------------------------------------------------
# Main polling loop
# ---------------------------------------------------------------------------

def main() -> None:
    with httpx.Client() as client:
        register_worker(client)

        log.info("Worker %s polling %s every %ds …", WORKER_ID, SERVER_URL, POLL_INTERVAL)
        while True:
            try:
                job = poll_for_job(client)
            except Exception as exc:
                log.warning("Poll error: %s", exc)
                time.sleep(POLL_INTERVAL)
                continue

            if job is None:
                time.sleep(POLL_INTERVAL)
                continue

            job_id = job["id"]
            log.info("Claimed job %s", job_id)
            try:
                results = run_simulation(job)
                submit_result(client, job_id, results)
            except Exception as exc:
                submit_failure(client, job_id, str(exc))


if __name__ == "__main__":
    main()
