"""CleanPlay Phase 3 — RocketRide Cloud case-report generator.

Runs the `cleanplay_score` pipeline on RocketRide Cloud (api.rocketride.ai):
a `chat` source -> `llm_anthropic` (Claude Haiku 4.5) -> `response_answers`.
We feed the device scoring result in as chat context and the LLM writes a short
Trust & Safety case report. Shared by scripts/deploy_pipeline.py and the API.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")

PIPE = ROOT / "pipeline" / "cleanplay_score.pipe"


def _build_question(scoring: dict):
    from rocketride.schema import Question

    q = Question()
    q.addInstruction(
        "Role",
        "You are a Trust & Safety analyst at an online game studio writing a concise "
        "internal case report about a device suspected of involvement in a real-money-"
        "trading (RMT) gold-selling ring (farmer -> mules -> buyers).",
    )
    q.addInstruction(
        "Length & tone",
        "About 150 words, single prose block, professional and factual. No markdown "
        "headings, no bullet lists.",
    )
    q.addInstruction(
        "Must cover",
        "1) the verdict and trust score; 2) the implicated accounts by name; 3) the key "
        "evidence — funnel transfer chain with amounts, multiple accounts sharing the "
        "device with clustered creation, and any large transfers to freshly-created "
        "accounts (smurfing); 4) a recommended moderator action matching the verdict "
        "(trusted = no action; suspicious = watchlist/manual review; dirty = restrict "
        "accounts and freeze transfers).",
    )
    q.addContext(json.dumps(scoring))
    q.addQuestion(
        f"Write the case report for device {scoring['device_id']} "
        f"(verdict: {scoring['verdict']}, trust score {scoring['score']}/100)."
    )
    return q


def _extract_answer(resp: dict) -> str:
    answers = resp.get("answers") or []
    if not answers:
        # fall back to result_types discovery (custom lane names)
        for key, lane in (resp.get("result_types") or {}).items():
            if lane == "answers" and resp.get(key):
                answers = resp[key]
                break
    if not answers:
        return ""
    a = answers[0]
    if isinstance(a, dict):
        a = a.get("text") or a.get("answer") or json.dumps(a)
    return str(a).strip()


async def generate_case_report(scoring: dict) -> dict:
    """Invoke the deployed RocketRide Cloud pipeline; return {report_text, token}."""
    from rocketride import RocketRideClient

    uri = os.environ["ROCKETRIDE_URI"]
    auth = os.environ["ROCKETRIDE_AUTH"]

    client = RocketRideClient(uri=uri, auth=auth)
    await client.connect()
    try:
        # use_existing=True reuses the already-running cloud pipeline instance.
        result = await client.use(filepath=str(PIPE), use_existing=True)
        token = result["token"]
        question = _build_question(scoring)
        resp = await client.chat(token=token, question=question)
        return {"report_text": _extract_answer(resp), "token": token}
    finally:
        await client.disconnect()
