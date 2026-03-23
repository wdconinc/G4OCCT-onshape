# SPDX-License-Identifier: LGPL-2.1-or-later
# Copyright (C) 2026 G4OCCT Contributors
"""Tests for the G4OCCT Onshape App Server.

These tests exercise the server endpoints using FastAPI's test client,
without requiring real Onshape credentials or a running database.
"""

import base64
import httpx
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Add the server directory to the import path.
sys.path.insert(0, str(Path(__file__).parent.parent / "server"))

from fastapi.testclient import TestClient

# Fake user dict injected by the authed_client fixture.
_FAKE_USER = {
    "id": "user-123",
    "name": "Test User",
    "email": "test@example.com",
    "access_token": "fake-access-token",
    "refresh_token": "fake-refresh-token",
}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _set_env(tmp_path, monkeypatch):
    """Set environment variables before importing the app module."""
    monkeypatch.setenv("JOB_DB_PATH", str(tmp_path / "test_jobs.db"))
    monkeypatch.setenv("SESSION_SECRET", "test-secret-key-not-for-production")
    monkeypatch.setenv("SESSION_HTTPS_ONLY", "false")
    monkeypatch.setenv("WORKER_TOKEN", "")  # disable token enforcement
    monkeypatch.setenv("DEV_MODE", "true")  # allow empty WORKER_TOKEN
    monkeypatch.setenv("ONSHAPE_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("ONSHAPE_CLIENT_SECRET", "test-client-secret")
    monkeypatch.setenv("APP_HOST", "http://testserver")
    # Point at the real frontend directory so StaticFiles mount succeeds.
    monkeypatch.setenv(
        "FRONTEND_DIR", str(Path(__file__).parent.parent / "frontend")
    )


@pytest.fixture()
def client():
    """Return a TestClient with a temporary SQLite database."""
    import importlib
    import app as server_app
    import jobs as job_store
    importlib.reload(job_store)
    importlib.reload(server_app)

    with TestClient(server_app.app, raise_server_exceptions=True) as c:
        yield c


@pytest.fixture()
def authed_client(client):
    """Return a TestClient whose requests appear authenticated.

    We patch ``_require_user`` and ``_get_session_user`` in the server module
    so that no real session cookie infrastructure is needed in tests.
    We also patch ``_onshape_post`` to avoid real Onshape API calls during
    job submission (STEP export).
    """
    import app as server_app

    with (
        patch.object(server_app, "_get_session_user", return_value=_FAKE_USER),
        patch.object(server_app, "_require_user", return_value=_FAKE_USER),
        patch.object(server_app, "_onshape_post", return_value=b"STEP-stub"),
    ):
        yield client


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

def test_health(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


# ---------------------------------------------------------------------------
# OAuth flow
# ---------------------------------------------------------------------------

def test_oauth_start_redirects(client):
    resp = client.get("/oauth/start", follow_redirects=False)
    assert resp.status_code in (302, 307)
    location = resp.headers["location"]
    from urllib.parse import urlparse, parse_qs
    parsed = urlparse(location)
    assert parsed.netloc == "oauth.onshape.com"
    assert parsed.path == "/oauth/authorize"
    qs = parse_qs(parsed.query)
    assert qs.get("client_id") == ["test-client-id"]
    assert qs.get("response_type") == ["code"]


def test_oauth_callback_bad_state(client):
    # No session state at all → should fail with 400.
    resp = client.get("/oauth/callback", params={"code": "abc", "state": "wrong-state"})
    assert resp.status_code == 400


def test_oauth_callback_no_state(client):
    # No session state at all.
    resp = client.get("/oauth/callback", params={"code": "abc", "state": "any"})
    assert resp.status_code == 400


def test_oauth_callback_uses_basic_auth(client):
    """Token exchange must use HTTP Basic Auth, not body params, for Onshape."""
    import app as server_app

    # Seed a valid state in the session via the /oauth/start redirect.
    start_resp = client.get("/oauth/start", follow_redirects=False)
    assert start_resp.status_code in (302, 307)

    # Extract the state value that was stored in the session cookie.
    from urllib.parse import urlparse, parse_qs
    location = start_resp.headers["location"]
    qs = parse_qs(urlparse(location).query)
    state = qs["state"][0]

    # Build the expected Basic-auth credentials.
    expected_creds = base64.b64encode(b"test-client-id:test-client-secret").decode()

    # Capture the request made to the Onshape token endpoint.
    captured = {}

    async def fake_post_token(url, **kwargs):
        captured["url"] = url
        captured["auth"] = kwargs.get("auth")
        captured["data"] = kwargs.get("data", {})
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = ""
        mock_resp.json.return_value = {
            "access_token": "fake-token",
            "refresh_token": "fake-refresh",
        }
        return mock_resp

    # Patch the user info call so the callback can complete.
    async def fake_onshape_get(*args, **kwargs):
        return {"id": "u1", "name": "Test", "email": "t@t.com"}

    with (
        patch.object(server_app, "_onshape_get", side_effect=fake_onshape_get),
    ):
        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_ctx = MagicMock()
            mock_ctx.__aenter__ = AsyncMock(return_value=mock_ctx)
            mock_ctx.__aexit__ = AsyncMock(return_value=False)
            mock_ctx.post = AsyncMock(side_effect=fake_post_token)
            mock_client_cls.return_value = mock_ctx

            resp = client.get(
                "/oauth/callback",
                params={"code": "authcode123", "state": state},
                follow_redirects=False,
            )

    # The callback should redirect on success (not return 400/502).
    assert resp.status_code in (302, 307), f"Unexpected status: {resp.status_code} {resp.text}"

    # Verify httpx.BasicAuth was used for client authentication.
    auth = captured.get("auth")
    assert isinstance(auth, httpx.BasicAuth), (
        f"Expected httpx.BasicAuth, got: {type(auth)!r}"
    )
    assert auth._auth_header == f"Basic {expected_creds}", (
        f"BasicAuth encodes wrong credentials: {auth._auth_header!r}"
    )

    # Verify client credentials were NOT in the body.
    body = captured.get("data", {})
    assert "client_id" not in body, "client_id must not appear in the token request body"
    assert "client_secret" not in body, "client_secret must not appear in the token request body"


# ---------------------------------------------------------------------------
# App endpoint (iframe)
# ---------------------------------------------------------------------------

def test_app_unauthenticated_redirects(client):
    resp = client.get("/app", follow_redirects=False)
    assert resp.status_code in (302, 307)
    assert "/oauth/start" in resp.headers["location"]


def test_app_authenticated_returns_html(authed_client):
    resp = authed_client.get(
        "/app",
        params={
            "documentId": "doc123",
            "workspaceId": "ws456",
            "elementId": "el789",
        },
    )
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "G4OCCT_CONTEXT" in resp.text
    assert "doc123" in resp.text
    assert "ws456" in resp.text
    assert "el789" in resp.text


# ---------------------------------------------------------------------------
# Job management
# ---------------------------------------------------------------------------

def test_submit_job_unauthenticated(client):
    resp = client.post("/api/jobs", json={
        "documentId": "d1", "workspaceId": "w1", "elementId": "e1",
    })
    assert resp.status_code == 401


def test_submit_and_get_job(authed_client):
    payload = {
        "documentId": "doc1",
        "workspaceId": "ws1",
        "elementId": "el1",
        "simulationConfig": {"type": "geantino_scan", "nEvents": 100},
    }
    resp = authed_client.post("/api/jobs", json=payload)
    assert resp.status_code == 201
    job = resp.json()
    assert job["status"] == "queued"
    assert job["document_id"] == "doc1"

    job_id = job["id"]
    resp2 = authed_client.get(f"/api/jobs/{job_id}")
    assert resp2.status_code == 200
    assert resp2.json()["id"] == job_id


def test_list_jobs(authed_client):
    # Submit two jobs.
    for i in range(2):
        authed_client.post("/api/jobs", json={
            "documentId": f"doc{i}",
            "workspaceId": "ws1",
            "elementId": "el1",
        })
    resp = authed_client.get("/api/jobs")
    assert resp.status_code == 200
    assert len(resp.json()) == 2


def test_get_job_not_found(authed_client):
    resp = authed_client.get("/api/jobs/nonexistent-id")
    assert resp.status_code == 404


def test_submit_job_missing_fields(authed_client):
    resp = authed_client.post("/api/jobs", json={"documentId": "d1"})
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Worker API
# ---------------------------------------------------------------------------

def test_worker_register(client):
    resp = client.post("/workers/register", json={
        "worker_id": "w1",
        "capabilities": {"geant4_version": "11.2"},
    })
    assert resp.status_code == 200
    assert resp.json()["worker_id"] == "w1"


def test_next_job_no_jobs(client):
    resp = client.get("/jobs/next", params={"worker_id": "w1"})
    assert resp.status_code == 204


def test_worker_claims_job(authed_client):
    # Submit a job.
    authed_client.post("/api/jobs", json={
        "documentId": "d1", "workspaceId": "w1", "elementId": "e1",
    })
    # Worker claims it.
    resp = authed_client.get("/jobs/next", params={"worker_id": "worker-abc"})
    assert resp.status_code == 200
    job = resp.json()
    assert job["status"] == "running"
    assert job["worker_id"] == "worker-abc"


def test_worker_submits_result(authed_client):
    authed_client.post("/api/jobs", json={
        "documentId": "d1", "workspaceId": "w1", "elementId": "e1",
    })
    claimed = authed_client.get("/jobs/next", params={"worker_id": "w1"})
    job_id = claimed.json()["id"]

    resp = authed_client.post(f"/jobs/{job_id}/result", json={
        "status": "complete",
        "worker_id": "w1",
        "results": {"volumes": 5, "navigation": "ok"},
    })
    assert resp.status_code == 200
    assert resp.json()["status"] == "complete"


def test_worker_reports_failure(authed_client):
    authed_client.post("/api/jobs", json={
        "documentId": "d1", "workspaceId": "w1", "elementId": "e1",
    })
    claimed = authed_client.get("/jobs/next", params={"worker_id": "w1"})
    job_id = claimed.json()["id"]

    resp = authed_client.post(f"/jobs/{job_id}/result", json={
        "status": "failed",
        "worker_id": "w1",
        "error": "Segmentation fault",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "failed"
    results = json.loads(data["results"])
    assert "error" in results


# ---------------------------------------------------------------------------
# API workers endpoint
# ---------------------------------------------------------------------------

def test_list_workers_api_unauthenticated(client):
    resp = client.get("/api/workers")
    assert resp.status_code == 401


def test_list_workers_api_authenticated(authed_client, client):
    # Register a worker first.
    client.post("/workers/register", json={
        "worker_id": "w-api-1",
        "capabilities": {"geant4_version": "11.2", "occt_version": "7.8"},
    })
    resp = authed_client.get("/api/workers")
    assert resp.status_code == 200
    workers = resp.json()
    assert isinstance(workers, list)
    assert any(w["id"] == "w-api-1" for w in workers)


# ---------------------------------------------------------------------------
# OAuth helpers (unit tests)
# ---------------------------------------------------------------------------

def test_oauth_generate_state():
    import oauth as oauth_mod
    s1 = oauth_mod.generate_state()
    s2 = oauth_mod.generate_state()
    assert len(s1) > 20
    assert s1 != s2  # Each call should produce a unique state.


def test_oauth_build_authorization_url():
    import oauth as oauth_mod
    from urllib.parse import urlparse, parse_qs
    url = oauth_mod.build_authorization_url("test-state-xyz")
    parsed = urlparse(url)
    assert parsed.netloc == "oauth.onshape.com"
    assert parsed.scheme == "https"
    qs = parse_qs(parsed.query)
    assert qs.get("state") == ["test-state-xyz"]
    assert qs.get("response_type") == ["code"]

