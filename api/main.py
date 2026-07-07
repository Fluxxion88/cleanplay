"""CleanPlay FastAPI service.

Public:
  GET  /                               T&S dashboard (single page)
  GET  /health                         liveness
  POST /api/login                      proxy to Butterbase auth -> JWT

Protected (Butterbase JWT required):
  GET  /api/me                         user + plan/usage
  GET  /devices                        device suspicion ranking
  POST /scan/{device_id}               metered: score via RocketRide Cloud, persist, alert
  POST /restrict/{device_id}           restrict all accounts on the device
  POST /simulate_new_account/{device}  new account, born restricted if latest scan != trusted
  POST /upgrade                        FREE -> PRO
  GET  /scans                          scan history from Butterbase
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
import uuid
from pathlib import Path

import httpx
from dotenv import load_dotenv
from fastapi import Body, Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse
from neo4j import GraphDatabase

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
load_dotenv(ROOT / ".env")

from scoring import score_device_standalone          # noqa: E402
from detect_queries import all_devices_summary        # noqa: E402
from rocketride_report import generate_case_report    # noqa: E402

from . import billing                                  # noqa: E402
from .auth import require_user, login as bb_login       # noqa: E402

app = FastAPI(title="CleanPlay", version="0.4.0")

BB_URL = os.environ["BUTTERBASE_API_URL"].rstrip("/")
BB_KEY = os.environ["BUTTERBASE_API_KEY"]
BB_HEADERS = {"Authorization": f"Bearer {BB_KEY}", "Content-Type": "application/json"}
DISCORD_URL = os.environ["DISCORD_WEBHOOK_URL"]
DASHBOARD = ROOT / "api" / "dashboard.html"

VERDICT_COLOR = {"dirty": 0xE74C3C, "suspicious": 0xE67E22, "trusted": 0x2ECC71}

# --------------------------------------------------------------------------- #
# Neo4j (sync driver; graph ops run in a worker thread)
# --------------------------------------------------------------------------- #
_driver = None


def _neo4j():
    global _driver
    if _driver is None:
        _driver = GraphDatabase.driver(
            os.environ["NEO4J_URI"],
            auth=(os.environ["NEO4J_USER"], os.environ["NEO4J_PASSWORD"]),
        )
    return _driver


def _device_summary() -> list[dict]:
    with _neo4j().session() as s:
        return all_devices_summary(s)


def _restrict_device(device_id: str) -> list[dict]:
    q = ("MATCH (d:Device {id:$id})<-[:LOGGED_IN_FROM]-(a:Account) "
         "SET a.restricted = true "
         "RETURN a.id AS id, a.name AS name ORDER BY a.name")
    with _neo4j().session() as s:
        return [r.data() for r in s.run(q, id=device_id)]


def _create_sim_account(device_id: str, restricted: bool) -> dict | None:
    aid = f"SIM-{device_id}-{uuid.uuid4().hex[:6]}"
    name = f"sim_{uuid.uuid4().hex[:8]}"
    ts = int(time.time())
    q = ("MATCH (d:Device {id:$id}) "
         "CREATE (a:Account {id:$aid, name:$name, created_at:$ts, restricted:$restricted}) "
         "CREATE (a)-[:LOGGED_IN_FROM {first_seen:$ts, last_seen:$ts, count:1}]->(d) "
         "RETURN a.id AS id, a.name AS name, a.restricted AS restricted, "
         "a.created_at AS created_at")
    with _neo4j().session() as s:
        rec = s.run(q, id=device_id, aid=aid, name=name, ts=ts, restricted=restricted).single()
        return rec.data() if rec else None


# --------------------------------------------------------------------------- #
# Butterbase / Discord helpers
# --------------------------------------------------------------------------- #
async def _bb_insert_scan(c: httpx.AsyncClient, row: dict) -> dict:
    r = await c.post(f"{BB_URL}/scans", headers=BB_HEADERS, json=row)
    r.raise_for_status()
    data = r.json()
    return data[0] if isinstance(data, list) else data


async def _bb_latest_verdict(c: httpx.AsyncClient, device_id: str) -> str | None:
    r = await c.get(f"{BB_URL}/scans", headers=BB_HEADERS,
                    params={"device_id": f"eq.{device_id}", "order": "created_at.desc",
                            "limit": 1})
    r.raise_for_status()
    rows = r.json()
    return rows[0]["verdict"] if rows else None


def _top_evidence(scoring: dict) -> str:
    chains = scoring["evidence"]["chains"]
    amounts = scoring["evidence"]["amounts"]
    if not chains:
        f = scoring["features"]
        return (f"device_density={f['device_density']['accounts']} accounts, "
                f"smurf_hits={f['smurf']['hits']}")
    return " → ".join(chains[0]) + f"  (amounts {amounts[0]})"


async def _send_discord(c: httpx.AsyncClient, scoring: dict, report_text: str) -> None:
    verdict = scoring["verdict"]
    embed = {
        "title": f"⚠️ CleanPlay Alert — device {scoring['device_id']}",
        "description": report_text[:4000] if report_text else "(no report text)",
        "color": VERDICT_COLOR.get(verdict, 0x95A5A6),
        "fields": [
            {"name": "Trust score", "value": f"{scoring['score']}/100", "inline": True},
            {"name": "Verdict", "value": verdict.upper(), "inline": True},
            {"name": "Accounts on device", "value": str(len(scoring["affected_accounts"])),
             "inline": True},
            {"name": "Top evidence", "value": _top_evidence(scoring)[:1024], "inline": False},
        ],
    }
    r = await c.post(DISCORD_URL, params={"wait": "true"}, json={"embeds": [embed]})
    r.raise_for_status()


# --------------------------------------------------------------------------- #
# Public endpoints
# --------------------------------------------------------------------------- #
@app.get("/")
async def dashboard() -> FileResponse:
    return FileResponse(DASHBOARD)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "service": "cleanplay"}


@app.post("/api/login")
async def api_login(body: dict = Body(...)) -> dict:
    email = body.get("email", "")
    password = body.get("password", "")
    tokens = await bb_login(email, password)
    # ensure a workspace exists on first login
    async with httpx.AsyncClient(timeout=30.0) as c:
        await billing.ensure_workspace(c, tokens["user"]["id"], tokens["user"]["email"])
    return {"access_token": tokens["access_token"], "user": tokens["user"]}


# --------------------------------------------------------------------------- #
# Protected endpoints
# --------------------------------------------------------------------------- #
@app.get("/api/me")
async def api_me(user: dict = Depends(require_user)) -> dict:
    async with httpx.AsyncClient(timeout=30.0) as c:
        ws = await billing.ensure_workspace(c, user["user_id"], user["email"])
        pro = await billing.get_pro_plan(c)
    return {"email": user["email"], "usage": billing.usage_view(ws),
            "pro_plan": pro}


@app.get("/devices")
async def devices(user: dict = Depends(require_user)) -> dict:
    rows = await asyncio.to_thread(_device_summary)
    return {"count": len(rows), "devices": rows}


@app.post("/scan/{device_id}")
async def scan(device_id: str, user: dict = Depends(require_user)) -> dict:
    async with httpx.AsyncClient(timeout=90.0) as c:
        # 0) metering gate (402 if FREE quota exhausted)
        ws = await billing.check_quota(c, user["user_id"], user["email"])

        # 1) score against Neo4j
        scoring = await asyncio.to_thread(score_device_standalone, device_id)

        # 2) case report from the deployed RocketRide Cloud pipeline
        try:
            rr = await generate_case_report(scoring)
        except Exception as e:  # surface, never fake a report
            raise HTTPException(status_code=502,
                                detail=f"RocketRide Cloud invocation failed: {e}")
        report_text, pipeline_token = rr["report_text"], rr["token"]

        # 3) persist scan
        stored = await _bb_insert_scan(c, {
            "device_id": device_id, "score": scoring["score"],
            "verdict": scoring["verdict"],
            "account_count": len(scoring["affected_accounts"]),
            "report_text": report_text,
        })

        # 4) record metered usage
        ws = await billing.record_scan(c, ws)

        # 5) alert Discord when not trusted
        alert_sent = False
        if scoring["verdict"] != "trusted":
            await _send_discord(c, scoring, report_text)
            alert_sent = True

    return {
        "device_id": device_id, "score": scoring["score"], "verdict": scoring["verdict"],
        "affected_accounts": scoring["affected_accounts"], "report_text": report_text,
        "evidence": scoring["evidence"], "features": scoring["features"],
        "alert_sent": alert_sent, "scan_id": stored.get("id"),
        "pipeline_token": pipeline_token, "usage": billing.usage_view(ws),
    }


@app.post("/restrict/{device_id}")
async def restrict(device_id: str, user: dict = Depends(require_user)) -> dict:
    restricted = await asyncio.to_thread(_restrict_device, device_id)
    return {"device_id": device_id, "restricted_count": len(restricted),
            "restricted_accounts": restricted}


@app.post("/simulate_new_account/{device_id}")
async def simulate_new_account(device_id: str, user: dict = Depends(require_user)) -> dict:
    async with httpx.AsyncClient(timeout=30.0) as c:
        latest = await _bb_latest_verdict(c, device_id)
    born_restricted = latest is not None and latest != "trusted"
    account = await asyncio.to_thread(_create_sim_account, device_id, born_restricted)
    if account is None:
        raise HTTPException(status_code=404, detail=f"device {device_id} not found")
    return {
        "device_id": device_id, "latest_scan_verdict": latest, "account": account,
        "born_restricted": born_restricted,
        "reason": (f"device's latest scan verdict is '{latest}'" if born_restricted
                   else f"device is {latest or 'unscanned'} — new account unrestricted"),
    }


@app.post("/upgrade")
async def upgrade(user: dict = Depends(require_user)) -> dict:
    async with httpx.AsyncClient(timeout=30.0) as c:
        pro = await billing.get_pro_plan(c)
        ws = await billing.upgrade(c, user["user_id"], user["email"],
                                   pro.get("id") if pro else None)
    return {"upgraded": True, "usage": billing.usage_view(ws),
            "pro_plan": pro,
            "note": ("Metered upgrade recorded in Butterbase. Stripe Connect is not "
                     "onboarded for this app, so no live Checkout was invoked.")}


@app.get("/scans")
async def list_scans(limit: int = 50, user: dict = Depends(require_user)) -> dict:
    async with httpx.AsyncClient(timeout=30.0) as c:
        r = await c.get(f"{BB_URL}/scans", headers=BB_HEADERS,
                        params={"order": "created_at.desc", "limit": limit,
                                "select": "id,device_id,score,verdict,account_count,created_at"})
        r.raise_for_status()
        rows = r.json()
    return {"count": len(rows), "scans": rows}
