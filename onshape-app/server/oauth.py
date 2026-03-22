# SPDX-License-Identifier: LGPL-2.1-or-later
# Copyright (C) 2026 G4OCCT Contributors
"""Onshape OAuth 2.0 helpers."""

import os
import secrets
import urllib.parse

ONSHAPE_OAUTH_BASE = "https://oauth.onshape.com"
ONSHAPE_TOKEN_URL = f"{ONSHAPE_OAUTH_BASE}/oauth/token"
ONSHAPE_AUTHORIZE_URL = f"{ONSHAPE_OAUTH_BASE}/oauth/authorize"

CLIENT_ID = os.environ.get("ONSHAPE_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("ONSHAPE_CLIENT_SECRET", "")
APP_HOST = os.environ.get("APP_HOST", "http://localhost:8000")
REDIRECT_URI = f"{APP_HOST}/oauth/callback"

# Onshape OAuth scopes required by this application.
# OAuth2Read is sufficient for STEP export and metadata access.
SCOPES = "OAuth2Read"


def build_authorization_url(state: str) -> str:
    """Return the Onshape OAuth authorisation URL for the given CSRF *state* token."""
    params = {
        "client_id": CLIENT_ID,
        "response_type": "code",
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPES,
        "state": state,
    }
    return f"{ONSHAPE_AUTHORIZE_URL}?{urllib.parse.urlencode(params)}"


def generate_state() -> str:
    """Generate a cryptographically secure CSRF state token."""
    return secrets.token_urlsafe(32)
