"""CleanPlay Phase 3 — device trust scoring.

Pure-python trust score for a device, built on the Phase 2 detection queries.
Score is 0-100 where 100 = fully trusted and 0 = dirty. Verdict bands:

    dirty       score < 40
    suspicious  40 <= score < 70
    trusted     70 <= score

Design notes
------------
* q1 returns every length-1..4 sub-chain of the funnel, which is noisy. We
  collapse those into distinct MAXIMAL chains (a chain kept only if it is not a
  contiguous sub-path of a longer returned chain) before scoring and evidence.
* Shared IP contributes NOTHING to the score by design (weight 0). The innocent
  family shares one home IP; a naive shared-IP rule would flag them. We surface
  the shared-IP count as evidence but never penalise it.
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from neo4j import GraphDatabase

from detect_queries import q1_funnel, q2_device_density, q3_smurf

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")

# Feature weights (penalties subtracted from a perfect 100).
W_FUNNEL = 70   # dominant: device sits inside a high-volume one-directional chain
W_DENSITY_CLUSTERED = 25
W_DENSITY_LOOSE = 10
W_SMURF_BASE = 25
W_SMURF_EXTRA = 5   # +5 when 3+ smurf hits
W_SHARED_IP = 0     # fairness requirement: shared IP must not move the score

DIRTY_MAX = 40      # score < 40  -> dirty
TRUSTED_MIN = 70    # score >= 70 -> trusted


def _is_subchain(short: list, long: list) -> bool:
    """True if `short` is a contiguous sub-sequence of `long` (and shorter)."""
    if len(short) >= len(long):
        return False
    for i in range(len(long) - len(short) + 1):
        if long[i:i + len(short)] == short:
            return True
    return False


def _maximal_chains(rows: list[dict]) -> list[dict]:
    """Collapse overlapping funnel sub-chains into distinct maximal chains."""
    # Deduplicate identical chains first.
    uniq: dict[tuple, dict] = {}
    for r in rows:
        uniq[tuple(r["chain"])] = r
    chains = list(uniq.values())
    maximal = []
    for r in chains:
        if not any(_is_subchain(r["chain"], other["chain"])
                   for other in chains if other is not r):
            maximal.append(r)
    # Longest / largest first.
    maximal.sort(key=lambda r: (len(r["chain"]), sum(r["amounts"])), reverse=True)
    return maximal


def _accounts_on_device(session, device_id: str) -> list[str]:
    q = ("MATCH (d:Device {id:$id})<-[:LOGGED_IN_FROM]-(a:Account) "
         "RETURN a.name AS name, a.id AS id ORDER BY a.created_at")
    return [r["name"] for r in session.run(q, id=device_id)]


def _shared_ip_devices(session, device_id: str) -> int:
    q = ("MATCH (d:Device {id:$id})-[:USES_IP]->(:IP)<-[:USES_IP]-(other:Device) "
         "WHERE other <> d RETURN count(DISTINCT other) AS m")
    rec = session.run(q, id=device_id).single()
    return rec["m"] if rec else 0


def score_device(session, device_id: str) -> dict:
    # --- gather raw signals ---
    funnel_rows = q1_funnel(session, device_id)
    maximal = _maximal_chains(funnel_rows)
    density = q2_device_density(session, device_id)
    smurf_rows = q3_smurf(session, device_id)
    shared_ip = _shared_ip_devices(session, device_id)
    accounts = _accounts_on_device(session, device_id)

    # --- funnel feature (dominant) ---
    funnel_hit = len(maximal) > 0
    funnel_max_amount = max((max(r["amounts"]) for r in maximal), default=0)
    funnel_penalty = W_FUNNEL if funnel_hit else 0

    # --- density feature ---
    n_acc = density.get("n_accounts", 0)
    clustered = bool(density.get("clustered"))
    if n_acc >= 2 and clustered:
        density_penalty = W_DENSITY_CLUSTERED
    elif n_acc >= 2:
        density_penalty = W_DENSITY_LOOSE
    else:
        density_penalty = 0

    # --- smurf feature ---
    smurf_hits = len(smurf_rows)
    smurf_max_amount = max((r["amount"] for r in smurf_rows), default=0)
    if smurf_hits >= 3:
        smurf_penalty = W_SMURF_BASE + W_SMURF_EXTRA
    elif smurf_hits >= 1:
        smurf_penalty = W_SMURF_BASE
    else:
        smurf_penalty = 0

    # --- shared IP feature (fairness: penalty always 0) ---
    shared_ip_penalty = W_SHARED_IP  # 0

    penalties = funnel_penalty + density_penalty + smurf_penalty + shared_ip_penalty
    score = max(0, min(100, 100 - penalties))

    if score < DIRTY_MAX:
        verdict = "dirty"
    elif score < TRUSTED_MIN:
        verdict = "suspicious"
    else:
        verdict = "trusted"

    return {
        "device_id": device_id,
        "score": score,
        "verdict": verdict,
        "features": {
            "funnel": {"hit": funnel_hit, "maximal_chains": len(maximal),
                       "max_amount": funnel_max_amount, "penalty": funnel_penalty},
            "device_density": {"accounts": n_acc, "clustered": clustered,
                               "penalty": density_penalty},
            "smurf": {"hits": smurf_hits, "max_amount": smurf_max_amount,
                      "penalty": smurf_penalty},
            "shared_ip": {"shares_ip_with_devices": shared_ip,
                          "penalty": shared_ip_penalty},
        },
        "affected_accounts": accounts,
        "evidence": {
            "chains": [r["chain"] for r in maximal],
            "amounts": [r["amounts"] for r in maximal],
        },
    }


def score_device_standalone(device_id: str) -> dict:
    """Convenience wrapper that manages its own Neo4j connection."""
    uri = os.environ["NEO4J_URI"]
    auth = (os.environ["NEO4J_USER"], os.environ["NEO4J_PASSWORD"])
    driver = GraphDatabase.driver(uri, auth=auth)
    try:
        driver.verify_connectivity()
        with driver.session() as session:
            return score_device(session, device_id)
    finally:
        driver.close()


# --------------------------------------------------------------------------- #
# Verification
# --------------------------------------------------------------------------- #
def _verify() -> int:
    uri = os.environ["NEO4J_URI"]
    auth = (os.environ["NEO4J_USER"], os.environ["NEO4J_PASSWORD"])
    driver = GraphDatabase.driver(uri, auth=auth)

    # device -> expected verdicts (a set of acceptable outcomes)
    expectations = {
        "D-42":    {"dirty"},                 # mule device
        "D-FARM":  {"dirty", "suspicious"},   # farmer device
        "D-BUY1":  {"suspicious", "dirty"},
        "D-BUY2":  {"suspicious", "dirty"},
        "D-BUY3":  {"suspicious", "dirty"},
        "D-FAM1":  {"trusted"},
        "D-FAM2":  {"trusted"},
        "D-FAM3":  {"trusted"},
        "D-P000":  {"trusted"},               # background players
        "D-P004":  {"trusted"},
        "D-P021":  {"trusted"},
    }

    ok = True
    print(f"{'device':<9}{'score':>6}  {'verdict':<11}{'funnel':<8}{'density':<12}"
          f"{'smurf':<8}{'sharedIP':<9}{'expect':<20}result")
    print("-" * 100)
    try:
        driver.verify_connectivity()
        with driver.session() as session:
            for dev, allowed in expectations.items():
                r = score_device(session, dev)
                f = r["features"]
                passed = r["verdict"] in allowed
                ok = ok and passed
                density = f"{f['device_density']['accounts']}acct" + (
                    "/clust" if f["device_density"]["clustered"] else "")
                print(f"{dev:<9}{r['score']:>6}  {r['verdict']:<11}"
                      f"{('yes' if f['funnel']['hit'] else '-'):<8}"
                      f"{density:<12}"
                      f"{f['smurf']['hits']:<8}"
                      f"{f['shared_ip']['shares_ip_with_devices']:<9}"
                      f"{'|'.join(sorted(allowed)):<20}"
                      f"{'PASS' if passed else 'FAIL'}")
    finally:
        driver.close()

    print("-" * 100)
    print("SCORING VERIFICATION:", "ALL PASSED ✅" if ok else "SOME FAILED ❌")
    print("\nNote: D-FAM* devices share one home IP (sharedIP=2) yet stay TRUSTED — "
          "shared IP carries zero weight (fairness).")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(_verify())
