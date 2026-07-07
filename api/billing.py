"""CleanPlay metered billing, backed by the Butterbase `workspaces` table.

Stripe Connect is not onboarded for this app (real Checkout is unavailable
headless), so scans are metered server-side against a plan persisted in
Butterbase. A real Butterbase billing *plan* object (the PRO catalog entry) is
referenced on upgrade, so the billing primitive is genuinely in use.

Plans:  FREE = 10 scans, PRO = unlimited.
"""
from __future__ import annotations

import os

import httpx
from fastapi import HTTPException

BB_URL = os.environ["BUTTERBASE_API_URL"].rstrip("/")
BB_KEY = os.environ["BUTTERBASE_API_KEY"]
BB_HEADERS = {"Authorization": f"Bearer {BB_KEY}", "Content-Type": "application/json"}

# Stripe Checkout (test mode) is used only when explicitly enabled AND Connect
# onboarding is complete; otherwise /upgrade does the metered flip (never breaks).
STRIPE_ENABLED = os.environ.get("STRIPE_ENABLED", "false").lower() in ("1", "true", "yes")

FREE_LIMIT = 10
PRO_LIMIT = 1_000_000  # effectively unlimited


async def _get_workspace(c: httpx.AsyncClient, user_id: str) -> dict | None:
    r = await c.get(f"{BB_URL}/workspaces", headers=BB_HEADERS,
                    params={"user_id": f"eq.{user_id}", "limit": 1})
    r.raise_for_status()
    rows = r.json()
    return rows[0] if rows else None


async def ensure_workspace(c: httpx.AsyncClient, user_id: str, email: str | None) -> dict:
    """Get-or-create the caller's workspace (FREE plan by default)."""
    ws = await _get_workspace(c, user_id)
    if ws:
        return ws
    r = await c.post(f"{BB_URL}/workspaces", headers=BB_HEADERS, json={
        "user_id": user_id, "email": email,
        "plan": "FREE", "scans_used": 0, "scan_limit": FREE_LIMIT,
    })
    r.raise_for_status()
    data = r.json()
    return data[0] if isinstance(data, list) else data


async def _patch(c: httpx.AsyncClient, ws_id: str, fields: dict) -> dict:
    r = await c.patch(f"{BB_URL}/workspaces/{ws_id}", headers=BB_HEADERS, json=fields)
    r.raise_for_status()
    data = r.json()
    return data[0] if isinstance(data, list) else data


async def check_quota(c: httpx.AsyncClient, user_id: str, email: str | None) -> dict:
    """Raise 402 if the FREE quota is exhausted; else return the workspace."""
    ws = await ensure_workspace(c, user_id, email)
    if ws["plan"] != "PRO" and ws["scans_used"] >= ws["scan_limit"]:
        raise HTTPException(status_code=402, detail={
            "error": "scan_quota_exceeded",
            "message": (f"FREE plan limit reached ({ws['scans_used']}/{ws['scan_limit']} "
                        f"scans). Upgrade to PRO for unlimited scans."),
            "plan": ws["plan"], "scans_used": ws["scans_used"],
            "scan_limit": ws["scan_limit"], "upgrade_endpoint": "/upgrade",
        })
    return ws


async def record_scan(c: httpx.AsyncClient, ws: dict) -> dict:
    """Increment usage after a successful scan; returns the updated workspace."""
    from datetime import datetime, timezone
    return await _patch(c, ws["id"], {
        "scans_used": ws["scans_used"] + 1,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    })


async def upgrade(c: httpx.AsyncClient, user_id: str, email: str | None,
                  pro_plan_id: str | None) -> dict:
    from datetime import datetime, timezone
    ws = await ensure_workspace(c, user_id, email)
    now = datetime.now(timezone.utc).isoformat()
    return await _patch(c, ws["id"], {
        "plan": "PRO", "scan_limit": PRO_LIMIT,
        "pro_plan_id": pro_plan_id, "upgraded_at": now, "updated_at": now,
    })


async def get_pro_plan(c: httpx.AsyncClient) -> dict | None:
    """Return the Butterbase billing PRO plan (real billing catalog object)."""
    r = await c.get(f"{BB_URL}/billing/plans", headers=BB_HEADERS)
    if r.status_code != 200:
        return None
    plans = r.json().get("plans", [])
    return plans[0] if plans else None


async def connect_ready(c: httpx.AsyncClient) -> bool:
    """True when the app's Stripe Connect account can accept charges."""
    try:
        r = await c.get(f"{BB_URL}/billing/connect/status", headers=BB_HEADERS)
        return r.status_code == 200 and bool(r.json().get("chargesEnabled"))
    except Exception:  # noqa: BLE001
        return False


async def stripe_subscribe(c: httpx.AsyncClient, user_token: str, plan_id: str,
                           success_url: str, cancel_url: str) -> dict:
    """Start a Stripe Checkout session for the signed-in end-user."""
    r = await c.post(
        f"{BB_URL}/billing/subscribe",
        headers={"Authorization": f"Bearer {user_token}", "Content-Type": "application/json"},
        json={"planId": plan_id, "successUrl": success_url, "cancelUrl": cancel_url},
    )
    r.raise_for_status()
    return r.json()  # { sessionId, url }


async def stripe_subscription_active(c: httpx.AsyncClient, user_token: str) -> bool:
    """True when the end-user has an active Stripe subscription."""
    r = await c.get(
        f"{BB_URL}/billing/subscription",
        headers={"Authorization": f"Bearer {user_token}", "Content-Type": "application/json"},
    )
    if r.status_code != 200:
        return False
    sub = r.json().get("subscription")
    return bool(sub) and sub.get("status") in ("active", "trialing", "complete", "paid")


def usage_view(ws: dict) -> dict:
    return {
        "plan": ws["plan"],
        "scans_used": ws["scans_used"],
        "scan_limit": ws["scan_limit"],
        "unlimited": ws["plan"] == "PRO",
    }
