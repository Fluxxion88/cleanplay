"""Butterbase end-user auth for the CleanPlay API.

Verifies Butterbase-issued access tokens (RS256, per-app JWKS) and exposes a
FastAPI dependency that 401s any request without a valid token. Also proxies
login so the browser talks only to our single origin.
"""
from __future__ import annotations

import os

import httpx
import jwt
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

APP_ID = os.environ["BUTTERBASE_APP_ID"]
_API_ROOT = os.environ["BUTTERBASE_API_URL"].split("/v1/")[0]  # https://api.butterbase.ai
AUTH_BASE = f"{_API_ROOT}/auth/{APP_ID}"
JWKS_URL = f"{AUTH_BASE}/.well-known/jwks.json"
ISSUER = f"butterbase:app:{APP_ID}"

# PyJWKClient caches keys (JWKS is cache-friendly, rotates rarely).
_jwks_client = jwt.PyJWKClient(JWKS_URL)
_bearer = HTTPBearer(auto_error=False)


def verify_token(token: str) -> dict:
    """Return validated claims or raise ValueError."""
    try:
        signing_key = _jwks_client.get_signing_key_from_jwt(token)
        return jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            issuer=ISSUER,
            options={"verify_aud": False},
        )
    except Exception as e:  # noqa: BLE001
        raise ValueError(str(e))


async def require_user(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> dict:
    """FastAPI dependency: 401 unless a valid Butterbase JWT is present."""
    if creds is None or not creds.credentials:
        raise HTTPException(status_code=401, detail="Missing bearer token")
    try:
        claims = verify_token(creds.credentials)
    except ValueError as e:
        raise HTTPException(status_code=401, detail=f"Invalid token: {e}")
    return {"user_id": claims["sub"], "email": claims.get("email")}


async def login(email: str, password: str) -> dict:
    """Proxy to Butterbase login; returns the token payload."""
    async with httpx.AsyncClient(timeout=30.0) as c:
        r = await c.post(f"{AUTH_BASE}/login", json={"email": email, "password": password})
    if r.status_code != 200:
        raise HTTPException(status_code=401, detail="Invalid credentials")
    return r.json()
