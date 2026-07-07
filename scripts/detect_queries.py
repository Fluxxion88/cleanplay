"""CleanPlay Phase 2 — detection queries + verification report.

Three device-centric Cypher queries (scoring is wired in Phase 3) plus a
ranking helper. Run AFTER scripts/seed_graph.py:

    python scripts/detect_queries.py

Notice: none of these queries look at IP. That is deliberate — the innocent
family shares one home IP, and a naive "shared IP" rule would flag them. Our
signals are structural (device co-location, funnel flow, smurf timing), so the
family trips nothing while the ring lights up.
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from neo4j import GraphDatabase

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")

HOUR = 3600
DAY = 86_400

# --------------------------------------------------------------------------- #
# The three named detection queries + helper.
# Each takes a neo4j session and returns a list of dict rows.
# --------------------------------------------------------------------------- #

Q1 = """
// Farm-like origins: accounts whose total gold outflow is large.
MATCH (farm:Account)-[o:TRANSFERRED_TO]->()
WITH farm, sum(o.amount) AS total_out
WHERE total_out > 4000
// Accounts that logged in from the target device.
MATCH (dev:Device {id:$device_id})<-[:LOGGED_IN_FROM]-(mid:Account)
// One-directional transfer chains (length 1..4) starting at a farm-like origin.
MATCH path = (farm)-[:TRANSFERRED_TO*1..4]->(dest:Account)
WHERE mid IN nodes(path)
WITH farm, total_out, path, relationships(path) AS rels, nodes(path) AS ns
// Every hop is a LARGE transfer (excludes organic <=500 traffic) ...
WHERE ALL(r IN rels WHERE r.amount > 1000)
// ... and the chain flows forward in time (money is forwarded, never returned).
  AND ALL(i IN range(0, size(rels) - 2) WHERE rels[i].ts <= rels[i + 1].ts)
RETURN DISTINCT
       farm.name           AS farm,
       total_out           AS farm_total_outflow,
       [n IN ns | n.name]  AS chain,
       [r IN rels | r.amount] AS amounts,
       size(rels)          AS hops
ORDER BY hops, farm
"""

Q2 = """
MATCH (dev:Device {id:$device_id})<-[:LOGGED_IN_FROM]-(a:Account)
WITH count(a)          AS n_accounts,
     min(a.created_at) AS earliest,
     max(a.created_at) AS latest,
     collect(a.name)   AS accounts
RETURN n_accounts,
       accounts,
       earliest,
       latest,
       (latest - earliest) AS creation_spread_seconds,
       (n_accounts >= 2 AND (latest - earliest) < 86400) AS clustered
"""

Q3 = """
MATCH (dev:Device {id:$device_id})<-[:LOGGED_IN_FROM]-(a:Account)
MATCH (a)<-[t:TRANSFERRED_TO]-(sender:Account)
WHERE t.amount > 2000
  AND t.ts >= a.created_at
  AND (t.ts - a.created_at) <= 172800   // 48h
RETURN a.name       AS account,
       a.created_at AS created_at,
       sender.name  AS sender,
       t.amount     AS amount,
       t.ts         AS ts,
       (t.ts - a.created_at) AS seconds_after_creation
ORDER BY amount DESC
"""

ALL_DEVICES = """
MATCH (d:Device)
CALL (d) {
    OPTIONAL MATCH (d)<-[:LOGGED_IN_FROM]-(a:Account)
    RETURN count(DISTINCT a) AS n_accounts, collect(DISTINCT a) AS accts
}
CALL (accts) {
    OPTIONAL MATCH (s)-[o:TRANSFERRED_TO]->() WHERE s IN accts
    RETURN coalesce(sum(o.amount), 0) AS outflow
}
CALL (accts) {
    OPTIONAL MATCH (r)<-[i:TRANSFERRED_TO]-() WHERE r IN accts
    RETURN coalesce(sum(i.amount), 0) AS inflow
}
RETURN d.id AS device, n_accounts, inflow, outflow
ORDER BY outflow DESC, inflow DESC, device
"""


def q1_funnel(session, device_id: str) -> list[dict]:
    return [r.data() for r in session.run(Q1, device_id=device_id)]


def q2_device_density(session, device_id: str) -> dict:
    rec = session.run(Q2, device_id=device_id).single()
    return rec.data() if rec else {"n_accounts": 0, "accounts": [], "clustered": False,
                                   "creation_spread_seconds": None}


def q3_smurf(session, device_id: str) -> list[dict]:
    return [r.data() for r in session.run(Q3, device_id=device_id)]


def all_devices_summary(session) -> list[dict]:
    return [r.data() for r in session.run(ALL_DEVICES)]


# --------------------------------------------------------------------------- #
# Verification report
# --------------------------------------------------------------------------- #
def _fmt_hours(secs) -> str:
    if secs is None:
        return "n/a"
    return f"{secs / HOUR:.1f}h"


def report(session) -> bool:
    ok = True

    def check(label: str, passed: bool) -> None:
        nonlocal ok
        ok = ok and passed
        print(f"    [{'PASS' if passed else 'FAIL'}] {label}")

    print("=" * 72)
    print("MULE DEVICE  D-42  (expected: trips q1 funnel + q2 density)")
    print("=" * 72)
    f = q1_funnel(session, "D-42")
    print(f"  q1_funnel: {len(f)} chain(s)")
    for row in f[:6]:
        print(f"    {' -> '.join(row['chain'])}   amounts={row['amounts']}  "
              f"(origin {row['farm']}, outflow {row['farm_total_outflow']})")
    d = q2_device_density(session, "D-42")
    print(f"  q2_density: {d['n_accounts']} accounts {d['accounts']}, "
          f"spread={_fmt_hours(d['creation_spread_seconds'])}, clustered={d['clustered']}")
    s = q3_smurf(session, "D-42")
    print(f"  q3_smurf: {len(s)} hit(s) (mules are old accounts -> expected 0)")
    check("D-42 trips q1 (funnel chain through device)", len(f) > 0)
    check("D-42 trips q2 (2 accounts, clustered creation)",
          d["n_accounts"] == 2 and d["clustered"])

    print()
    print("=" * 72)
    print("BUYER DEVICES  D-BUY1/2/3  (expected: trip q3 smurf)")
    print("=" * 72)
    for dev in ("D-BUY1", "D-BUY2", "D-BUY3"):
        s = q3_smurf(session, dev)
        print(f"  {dev}: q3_smurf {len(s)} hit(s)")
        for row in s[:3]:
            print(f"    {row['account']} received {row['amount']} from {row['sender']} "
                  f"{_fmt_hours(row['seconds_after_creation'])} after creation")
        check(f"{dev} trips q3 (large transfer soon after creation)", len(s) > 0)

    print()
    print("=" * 72)
    print("INNOCENT FAMILY  D-FAM1/2/3  (expected: trip NOTHING)")
    print("=" * 72)
    for dev in ("D-FAM1", "D-FAM2", "D-FAM3"):
        f = q1_funnel(session, dev)
        d = q2_device_density(session, dev)
        s = q3_smurf(session, dev)
        print(f"  {dev}: q1={len(f)}  q2={d['n_accounts']} acct "
              f"(clustered={d['clustered']})  q3={len(s)}")
        check(f"{dev} q1 empty", len(f) == 0)
        check(f"{dev} q2 normal density (1 account, not clustered)",
              d["n_accounts"] == 1 and not d["clustered"])
        check(f"{dev} q3 empty", len(s) == 0)

    print()
    print("=" * 72)
    print("SUSPECT RANKING — all_devices_summary (top 8 by outflow)")
    print("=" * 72)
    print(f"  {'device':<10} {'accts':>5} {'inflow':>10} {'outflow':>10}")
    for row in all_devices_summary(session)[:8]:
        print(f"  {row['device']:<10} {row['n_accounts']:>5} "
              f"{row['inflow']:>10} {row['outflow']:>10}")

    print()
    print("=" * 72)
    print("VERIFICATION:", "ALL CHECKS PASSED ✅" if ok else "SOME CHECKS FAILED ❌")
    print("=" * 72)
    return ok


# --------------------------------------------------------------------------- #
# Neo4j Browser visualization queries (printed + saved to file)
# --------------------------------------------------------------------------- #
BROWSER_FILE = ROOT / "scripts" / "browser_queries.cypher"

BROWSER_CYPHER = """\
// ==========================================================================
// CleanPlay — Neo4j Browser visualization queries
// Paste either block into the Neo4j Browser query bar and run.
// ==========================================================================

// --------------------------------------------------------------------------
// (a) THE WHOLE GRAPH — every Account / Device / IP and their relationships.
//     Good for the "here is all the telemetry" overview shot.
// --------------------------------------------------------------------------
MATCH (n)
WHERE n:Account OR n:Device OR n:IP
OPTIONAL MATCH p = (n)-[]->()
RETURN n, p;


// --------------------------------------------------------------------------
// (b) THE RING SUBGRAPH — just the farmer, the two mules (shared device D-42),
//     the three buyers, plus their devices, shared IPs and gold transfers.
//     Ring accounts are the ones whose id starts with 'A-'.
// --------------------------------------------------------------------------
MATCH (a:Account)
WHERE a.id STARTS WITH 'A-'
OPTIONAL MATCH (a)-[t:TRANSFERRED_TO]->(b:Account)
OPTIONAL MATCH (a)-[l:LOGGED_IN_FROM]->(d:Device)
OPTIONAL MATCH (d)-[u:USES_IP]->(ip:IP)
RETURN a, t, b, l, d, u, ip;
"""


def main() -> int:
    uri = os.environ["NEO4J_URI"]
    auth = (os.environ["NEO4J_USER"], os.environ["NEO4J_PASSWORD"])
    driver = GraphDatabase.driver(uri, auth=auth)
    try:
        driver.verify_connectivity()
        with driver.session() as session:
            ok = report(session)
    finally:
        driver.close()

    BROWSER_FILE.write_text(BROWSER_CYPHER)
    print("\nNeo4j Browser queries (also saved to scripts/browser_queries.cypher):\n")
    print(BROWSER_CYPHER)

    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
