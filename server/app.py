# SPDX-License-Identifier: LGPL-2.1-or-later
# Copyright (C) 2026 G4OCCT Contributors
"""G4OCCT Onshape App Server.

Responsibilities
----------------
* Serve the iframe frontend (``/app``).
* Manage Onshape OAuth 2.0 flow (``/oauth/start``, ``/oauth/callback``).
* Proxy Onshape REST API calls (STEP export, metadata) on behalf of the
  authenticated user; OAuth tokens are stored in a signed session cookie and
  are not accessible to iframe JavaScript.
* Manage a job queue and dispatch jobs to remote or local G4OCCT workers.

Run locally
-----------
::

    cd server
    pip install -r requirements.txt
    cp ../.env.example ../.env   # then fill in credentials
    uvicorn app:app --reload

"""

import asyncio
import base64
import json
import os
import secrets
import urllib.parse
from pathlib import Path
import html as html_lib

# Load .env before importing local modules that read env vars at module level.
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

import httpx  # noqa: E402
from fastapi import FastAPI, HTTPException, Query, Request, Response  # noqa: E402
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from starlette.middleware.sessions import SessionMiddleware  # noqa: E402

import jobs as job_store  # noqa: E402
import oauth as oauth_helper  # noqa: E402

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SESSION_SECRET = os.environ.get("SESSION_SECRET", secrets.token_hex(32))

# https_only=True sets the Secure flag on the session cookie (required in
# production over HTTPS).  Set SESSION_HTTPS_ONLY=false for local HTTP dev.
SESSION_HTTPS_ONLY = os.environ.get("SESSION_HTTPS_ONLY", "true").lower() in (
    "1",
    "true",
    "yes",
)

# "none" is required for cross-site iframe cookies (Onshape embedding context).
# Use "lax" or "strict" for same-site-only deployments.
SESSION_COOKIE_SAMESITE = os.environ.get("SESSION_COOKIE_SAMESITE", "none")

ONSHAPE_API_BASE = "https://cad.onshape.com/api"

# Frontend directory: configurable via env var.
# In Docker, docker-compose mounts ./frontend at /app/frontend, so the default
# Path(__file__).parent / "frontend" resolves correctly inside the container.
# For local development (cd server && uvicorn app:app), set FRONTEND_DIR=../frontend.
FRONTEND_DIR = Path(
    os.environ.get("FRONTEND_DIR", Path(__file__).parent / "frontend")
).resolve()

# ---------------------------------------------------------------------------
# Worker token / dev-mode configuration
# ---------------------------------------------------------------------------

_DEV_MODE = os.environ.get("DEV_MODE", "").lower() in {"1", "true", "yes", "on"}
_WORKER_TOKEN = os.environ.get("WORKER_TOKEN", "")

if not _WORKER_TOKEN and not _DEV_MODE:
    raise RuntimeError(
        "WORKER_TOKEN environment variable must be set. "
        "Set DEV_MODE=true to disable token enforcement during local development."
    )

# Poll settings for the glTF translation status endpoint.
GLTF_POLL_INTERVAL = 2   # seconds between polls
GLTF_MAX_POLLS = 60      # 60 × 2 s = 120 s timeout

# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------

app = FastAPI(title="G4OCCT Onshape App", version="0.1.0")
app.add_middleware(
    SessionMiddleware,
    secret_key=SESSION_SECRET,
    https_only=SESSION_HTTPS_ONLY,
    same_site=SESSION_COOKIE_SAMESITE,
)

# Serve static frontend files from /static
app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_session_user(request: Request) -> dict | None:
    """Return the authenticated user dict from the session, or None."""
    return request.session.get("user")


def _require_user(request: Request) -> dict:
    """Return the authenticated user dict or raise 401."""
    user = _get_session_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user


async def _onshape_get(access_token: str, path: str, **params) -> dict:
    """Issue an authenticated GET to the Onshape REST API."""
    async with httpx.AsyncClient() as client:
        r = await client.get(
            f"{ONSHAPE_API_BASE}{path}",
            headers={"Authorization": f"Bearer {access_token}"},
            params=params,
            timeout=30,
        )
    r.raise_for_status()
    return r.json()


async def _onshape_post(access_token: str, path: str, body: dict) -> bytes:
    """Issue an authenticated POST to the Onshape REST API and return raw bytes."""
    async with httpx.AsyncClient() as client:
        r = await client.post(
            f"{ONSHAPE_API_BASE}{path}",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
                "Accept": "application/octet-stream",
            },
            json=body,
            timeout=120,
            follow_redirects=True,
        )
    r.raise_for_status()
    return r.content


async def _onshape_post_json(access_token: str, path: str, body: dict) -> dict:
    """Issue an authenticated POST to the Onshape REST API and return JSON."""
    async with httpx.AsyncClient() as client:
        r = await client.post(
            f"{ONSHAPE_API_BASE}{path}",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            json=body,
            timeout=30,
        )
    r.raise_for_status()
    return r.json()


async def _onshape_get_bytes(access_token: str, path: str) -> bytes:
    """Issue an authenticated GET to the Onshape REST API and return raw bytes."""
    async with httpx.AsyncClient() as client:
        r = await client.get(
            f"{ONSHAPE_API_BASE}{path}",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/octet-stream",
            },
            timeout=120,
            follow_redirects=True,
        )
    r.raise_for_status()
    return r.content


# ---------------------------------------------------------------------------
# OAuth endpoints
# ---------------------------------------------------------------------------


@app.get("/oauth/start")
async def oauth_start(request: Request, next: str = "/app"):
    """Redirect the browser to Onshape's OAuth authorisation page."""
    # Restrict 'next' to same-origin relative paths to prevent open-redirect
    # attacks (e.g. ?next=https://evil.example).
    if not next.startswith("/") or next.startswith("//"):
        next = "/app"
    state = oauth_helper.generate_state()
    request.session["oauth_state"] = state
    request.session["oauth_next"] = next
    auth_url = oauth_helper.build_authorization_url(state)
    return RedirectResponse(auth_url)


@app.get("/oauth/callback")
async def oauth_callback(
    request: Request,
    code: str = Query(...),
    state: str = Query(...),
):
    """Exchange the authorisation code for access + refresh tokens."""
    stored_state = request.session.pop("oauth_state", None)
    if stored_state is None or stored_state != state:
        raise HTTPException(status_code=400, detail="Invalid OAuth state parameter")

    # Onshape requires client credentials as HTTP Basic Auth, not in the body.
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            oauth_helper.ONSHAPE_TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": oauth_helper.REDIRECT_URI,
            },
            headers={"Accept": "application/json"},
            auth=httpx.BasicAuth(oauth_helper.CLIENT_ID, oauth_helper.CLIENT_SECRET),
            timeout=30,
        )
    if resp.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail=f"Token exchange failed: {resp.text}",
        )

    token_data = resp.json()
    access_token = token_data["access_token"]

    # Fetch the authenticated user's profile from Onshape.
    user_info = await _onshape_get(access_token, "/users/sessioninfo")

    request.session["user"] = {
        "id": user_info.get("id"),
        "name": user_info.get("name"),
        "email": user_info.get("email"),
        "access_token": access_token,
        "refresh_token": token_data.get("refresh_token"),
    }

    next_url = request.session.pop("oauth_next", "/app")
    return RedirectResponse(next_url)


@app.get("/oauth/logout")
async def oauth_logout(request: Request):
    """Clear the server-side session and redirect to the app root."""
    request.session.clear()
    return RedirectResponse("/app")


# ---------------------------------------------------------------------------
# iframe / frontend endpoint
# ---------------------------------------------------------------------------


@app.get("/app", response_class=HTMLResponse)
async def serve_app(
    request: Request,
    documentId: str = Query(None),
    workspaceId: str = Query(None),
    elementId: str = Query(None),
):
    """Serve the iframe frontend, triggering OAuth if the user is not logged in."""
    user = _get_session_user(request)
    if user is None:
        # Build the /app URL with context parameters so we can redirect back
        # after the OAuth dance completes.
        params = {}
        if documentId:
            params["documentId"] = documentId
        if workspaceId:
            params["workspaceId"] = workspaceId
        if elementId:
            params["elementId"] = elementId
        next_url = "/app"
        if params:
            next_url += "?" + urllib.parse.urlencode(params)
        return RedirectResponse("/oauth/start?next=" + urllib.parse.quote(next_url, safe=""))

    # Serve the static index.html – inject context via a <script> block so
    # that the frontend JS can read it without embedding OAuth tokens.
    html = (FRONTEND_DIR / "index.html").read_text()
    context = {
        "documentId": documentId,
        "workspaceId": workspaceId,
        "elementId": elementId,
        "userName": user.get("name", ""),
        "userEmail": user.get("email", ""),
    }
    # Escape </script to prevent untrusted values from breaking out of the
    # inline script block and enabling reflected XSS.
    context_json = json.dumps(context).replace("</script", "<\\/script")
    context_script = f"""
<script>
  window.G4OCCT_CONTEXT = {context_json};
</script>"""
    escaped_context_script = html_lib.escape(context_script, quote=False)
    html = html.replace("</head>", f"{escaped_context_script}\n</head>", 1)
    return HTMLResponse(html)


# ---------------------------------------------------------------------------
# Onshape REST API proxy
# ---------------------------------------------------------------------------


@app.get("/api/element/metadata")
async def element_metadata(
    request: Request,
    documentId: str = Query(...),
    workspaceId: str = Query(...),
    elementId: str = Query(...),
):
    """Return metadata for the active Onshape element."""
    user = _require_user(request)
    try:
        data = await _onshape_get(
            user["access_token"],
            f"/documents/d/{documentId}/w/{workspaceId}/elements",
        )
    except httpx.HTTPStatusError as exc:
        raise HTTPException(status_code=exc.response.status_code, detail=str(exc))

    # Find the element that matches elementId
    elements = data if isinstance(data, list) else data.get("items", [])
    element = next((e for e in elements if e.get("id") == elementId), None)
    if element is None:
        raise HTTPException(status_code=404, detail="Element not found")
    return element


@app.post("/api/element/export-step")
async def export_step(
    request: Request,
    documentId: str = Query(...),
    workspaceId: str = Query(...),
    elementId: str = Query(...),
    elementType: str = Query("partstudio"),
):
    """Export the STEP file for the active element.

    *elementType* must be ``"partstudio"`` or ``"assembly"``.
    Returns the raw STEP bytes with ``Content-Type: application/octet-stream``.
    """
    user = _require_user(request)
    if elementType not in ("partstudio", "assembly"):
        raise HTTPException(status_code=400, detail="elementType must be 'partstudio' or 'assembly'")

    api_path = f"/{'partstudios' if elementType == 'partstudio' else 'assemblies'}/d/{documentId}/w/{workspaceId}/e/{elementId}/export"

    body: dict
    if elementType == "partstudio":
        body = {
            "formatName": "STEP",
            "storeInDocument": False,
            "yAxisIsUp": False,
        }
    else:
        body = {
            "formatName": "STEP",
            "flattenAssemblies": False,
            "storeInDocument": False,
        }

    try:
        step_bytes = await _onshape_post(user["access_token"], api_path, body)
    except httpx.HTTPStatusError as exc:
        raise HTTPException(status_code=exc.response.status_code, detail=str(exc))

    return Response(
        content=step_bytes,
        media_type="application/octet-stream",
        headers={"Content-Disposition": 'attachment; filename="geometry.step"'},
    )


@app.post("/api/element/export-gltf")
async def export_gltf(
    request: Request,
    documentId: str = Query(...),
    workspaceId: str = Query(...),
    elementId: str = Query(...),
    elementType: str = Query("partstudio"),
):
    """Export the glTF file for the active element via the Onshape Translations API.

    *elementType* must be ``"partstudio"`` or ``"assembly"``.

    The translation is an asynchronous three-step process:

    1. Initiate the translation (POST → returns a translation ID).
    2. Poll the translation status until ``requestState`` is ``"DONE"`` or
       ``"FAILED"`` (every 2 s, up to 120 s).
    3. Download the resulting GLB file.

    Returns the raw GLB bytes with ``Content-Type: model/gltf-binary``.
    """
    user = _require_user(request)
    if elementType not in ("partstudio", "assembly"):
        raise HTTPException(status_code=400, detail="elementType must be 'partstudio' or 'assembly'")

    # Step 1: Initiate translation.
    api_prefix = "partstudios" if elementType == "partstudio" else "assemblies"
    translate_path = f"/{api_prefix}/d/{documentId}/w/{workspaceId}/e/{elementId}/translations"

    # Reuse one client for the whole initiate → poll → download lifecycle to
    # avoid repeated TCP/TLS handshakes during the polling loop.
    async with httpx.AsyncClient() as client:
        auth_header = {"Authorization": f"Bearer {user['access_token']}"}
        try:
            r = await client.post(
                f"{ONSHAPE_API_BASE}{translate_path}",
                headers={**auth_header, "Content-Type": "application/json", "Accept": "application/json"},
                json={"formatName": "GLTF", "storeInDocument": False},
                timeout=30,
            )
            r.raise_for_status()
            translation = r.json()
        except httpx.HTTPStatusError as exc:
            raise HTTPException(status_code=exc.response.status_code, detail=str(exc))
        except httpx.RequestError as exc:
            raise HTTPException(status_code=502, detail=f"Network error contacting Onshape: {exc}")

        translation_id = translation.get("id")
        if not translation_id:
            raise HTTPException(status_code=502, detail="Translation initiation did not return an ID")

        # Step 2: Poll for completion (up to 120 s, polling every 2 s).
        status: dict
        for _ in range(GLTF_MAX_POLLS):
            await asyncio.sleep(GLTF_POLL_INTERVAL)
            try:
                r = await client.get(
                    f"{ONSHAPE_API_BASE}/translations/{translation_id}",
                    headers={**auth_header, "Accept": "application/json"},
                    timeout=30,
                )
                r.raise_for_status()
                status = r.json()
            except httpx.HTTPStatusError as exc:
                raise HTTPException(status_code=exc.response.status_code, detail=str(exc))
            except httpx.RequestError as exc:
                raise HTTPException(status_code=502, detail=f"Network error contacting Onshape: {exc}")
            state = status.get("requestState")
            if state == "DONE":
                break
            if state == "FAILED":
                raise HTTPException(status_code=502, detail="Onshape translation failed")
        else:
            raise HTTPException(status_code=504, detail="glTF translation timed out")

        # Step 3: Download the result.
        result_ids = status.get("resultExternalDataIds", [])
        if not result_ids:
            raise HTTPException(status_code=502, detail="Translation completed but returned no data")

        try:
            r = await client.get(
                f"{ONSHAPE_API_BASE}/documents/d/{documentId}/externaldata/{result_ids[0]}",
                headers={**auth_header, "Accept": "application/octet-stream"},
                timeout=120,
                follow_redirects=True,
            )
            r.raise_for_status()
            gltf_bytes = r.content
        except httpx.HTTPStatusError as exc:
            raise HTTPException(status_code=exc.response.status_code, detail=str(exc))
        except httpx.RequestError as exc:
            raise HTTPException(status_code=502, detail=f"Network error contacting Onshape: {exc}")

    return Response(
        content=gltf_bytes,
        media_type="model/gltf-binary",
        headers={"Content-Disposition": 'attachment; filename="geometry.glb"'},
    )


# ---------------------------------------------------------------------------
# Job management
# ---------------------------------------------------------------------------


@app.get("/api/jobs")
async def list_jobs(request: Request):
    """List all jobs belonging to the authenticated user."""
    user = _require_user(request)
    job_list = await job_store.list_jobs(user["id"])
    return job_list


@app.post("/api/jobs")
async def submit_job(request: Request):
    """Submit a new simulation job.

    Expected JSON body::

        {
          "documentId": "...",
          "workspaceId": "...",
          "elementId": "...",
          "elementType": "partstudio",
          "simulationConfig": {
            "type": "geantino_scan",
            "nEvents": 1000
          }
        }
    """
    user = _require_user(request)
    body = await request.json()

    required = ("documentId", "workspaceId", "elementId")
    missing = [k for k in required if not body.get(k)]
    if missing:
        raise HTTPException(status_code=422, detail=f"Missing fields: {missing}")

    element_type = body.get("elementType", "partstudio")
    if element_type not in ("partstudio", "assembly"):
        raise HTTPException(
            status_code=400, detail="elementType must be 'partstudio' or 'assembly'"
        )

    # Export STEP geometry at submission time so the worker receives it in the
    # job payload and can start the simulation immediately after claiming.
    api_path = (
        f"/{'partstudios' if element_type == 'partstudio' else 'assemblies'}"
        f"/d/{body['documentId']}/w/{body['workspaceId']}/e/{body['elementId']}/export"
    )
    export_body: dict
    if element_type == "partstudio":
        export_body = {"formatName": "STEP", "storeInDocument": False, "yAxisIsUp": False}
    else:
        export_body = {
            "formatName": "STEP",
            "flattenAssemblies": False,
            "storeInDocument": False,
        }
    try:
        step_bytes = await _onshape_post(user["access_token"], api_path, export_body)
    except httpx.HTTPStatusError as exc:
        raise HTTPException(
            status_code=exc.response.status_code,
            detail=f"STEP export failed: {exc}",
        )

    step_data = base64.b64encode(step_bytes).decode()

    job = await job_store.create_job(
        user_id=user["id"],
        document_id=body["documentId"],
        workspace_id=body["workspaceId"],
        element_id=body["elementId"],
        sim_config=body.get("simulationConfig", {}),
        step_data=step_data,
    )
    return JSONResponse(job, status_code=201)


@app.get("/api/jobs/{job_id}")
async def get_job(request: Request, job_id: str):
    """Return the current state of a job."""
    user = _require_user(request)
    job = await job_store.get_job(job_id)
    if job is None or job["user_id"] != user["id"]:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@app.get("/api/workers")
async def list_workers_api(request: Request):
    """Return registered workers for display in the frontend."""
    _require_user(request)
    workers = await job_store.list_workers()
    # Strip internal/sensitive fields such as user_id before returning to clients.
    return [{k: v for k, v in worker.items() if k != "user_id"} for worker in workers]


# ---------------------------------------------------------------------------
# Worker API (used by both remote and local workers)
# ---------------------------------------------------------------------------


@app.post("/workers/register")
async def worker_register(request: Request):
    """Register or refresh a worker.

    Expected JSON body::

        {
          "worker_id": "unique-worker-id",
          "capabilities": { "geant4_version": "11.2", "occt_version": "7.8" }
        }

    Workers must supply a valid ``X-Worker-Token`` header that matches
    ``WORKER_TOKEN`` environment variable.
    """
    _verify_worker_token(request)
    body = await request.json()
    worker_id = body.get("worker_id")
    if not worker_id:
        raise HTTPException(status_code=422, detail="worker_id required")
    await job_store.register_worker(
        worker_id=worker_id,
        capabilities=body.get("capabilities", {}),
    )
    return {"status": "registered", "worker_id": worker_id}


@app.get("/workers")
async def list_workers(request: Request):
    """Return registered workers (admin / debug endpoint)."""
    _verify_worker_token(request)
    return await job_store.list_workers()


@app.get("/jobs/next")
async def next_job(request: Request, worker_id: str = Query(...)):
    """Claim and return the next queued job.

    Returns ``204 No Content`` when there are no queued jobs.
    """
    _verify_worker_token(request)
    job = await job_store.claim_next_job(worker_id)
    if job is None:
        return Response(status_code=204)
    return job


@app.post("/jobs/{job_id}/result")
async def submit_result(request: Request, job_id: str):
    """Accept simulation results from a worker.

    Expected JSON body::

        {
          "status": "complete" | "failed",
          "worker_id": "unique-worker-id",
          "results": { ... }   // or "error": "..." on failure
        }
    """
    _verify_worker_token(request)
    body = await request.json()

    # Verify the job exists, is in running state, and belongs to the
    # submitting worker (prevents one worker from overwriting another's results).
    existing_job = await job_store.get_job(job_id)
    if existing_job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    if existing_job["status"] != "running":
        raise HTTPException(
            status_code=409,
            detail=f"Job is not running (current state: {existing_job['status']})",
        )
    submitting_worker = body.get("worker_id")
    if (
        submitting_worker
        and existing_job.get("worker_id")
        and existing_job["worker_id"] != submitting_worker
    ):
        raise HTTPException(status_code=403, detail="Worker ID mismatch")

    status = body.get("status", "complete")
    if status == "failed":
        job = await job_store.fail_job(job_id, body.get("error", "unknown error"))
    else:
        job = await job_store.complete_job(job_id, body.get("results", {}))
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------


@app.get("/health")
async def health():
    """Simple liveness probe."""
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Internal utilities
# ---------------------------------------------------------------------------


def _verify_worker_token(request: Request) -> None:
    """Raise 401 if the request does not carry a valid worker token.

    In explicit development mode (DEV_MODE=true) without a configured
    WORKER_TOKEN, token enforcement is disabled to simplify local testing.
    """
    if not _WORKER_TOKEN and _DEV_MODE:
        return
    if not _WORKER_TOKEN:
        raise HTTPException(
            status_code=500,
            detail="Worker authentication is misconfigured on the server.",
        )
    token = request.headers.get("X-Worker-Token", "")
    if not secrets.compare_digest(token, _WORKER_TOKEN):
        raise HTTPException(status_code=401, detail="Invalid worker token")
