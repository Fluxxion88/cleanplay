"""CleanPlay integration smoke test.

Verifies every external integration required for Phase 1 and prints a clear
PASS/FAIL line for each, followed by a summary table. Each check is isolated so
one failure never stops the others. Run with:

    python scripts/smoke_test.py

Exit code is 0 only if all checks pass.
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv

# Load .env from the project root regardless of where the script is run from.
ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")

# Ordered results: (name, passed, detail)
RESULTS: list[tuple[str, bool, str]] = []


def record(name: str, passed: bool, detail: str) -> None:
    status = "PASS" if passed else "FAIL"
    print(f"[{status}] {name}: {detail}")
    RESULTS.append((name, passed, detail))


# --------------------------------------------------------------------------- #
# a) Neo4j Aura
# --------------------------------------------------------------------------- #
def check_neo4j() -> None:
    name = "NEO4J"
    try:
        from neo4j import GraphDatabase

        uri = os.environ["NEO4J_URI"]
        user = os.environ["NEO4J_USER"]
        password = os.environ["NEO4J_PASSWORD"]

        ts = int(time.time() * 1000)
        driver = GraphDatabase.driver(uri, auth=(user, password))
        try:
            driver.verify_connectivity()
            with driver.session() as session:
                # RETURN 1
                one = session.run("RETURN 1 AS n").single()["n"]
                assert one == 1, f"RETURN 1 gave {one}"

                # MERGE a SmokeTest node
                created = session.run(
                    "MERGE (s:SmokeTest {ts: $ts}) RETURN s.ts AS ts", ts=ts
                ).single()["ts"]
                assert created == ts, "MERGE did not return the node"

                # Read it back
                read = session.run(
                    "MATCH (s:SmokeTest {ts: $ts}) RETURN s.ts AS ts", ts=ts
                ).single()
                assert read is not None and read["ts"] == ts, "node not found on read-back"

                # Delete it
                deleted = session.run(
                    "MATCH (s:SmokeTest {ts: $ts}) DELETE s RETURN count(*) AS c", ts=ts
                ).single()["c"]
                assert deleted == 1, f"expected to delete 1 node, deleted {deleted}"
        finally:
            driver.close()

        record(name, True, f"connect + RETURN 1 + MERGE/read/delete SmokeTest(ts={ts})")
    except Exception as e:  # noqa: BLE001
        record(name, False, f"{type(e).__name__}: {e}")


# --------------------------------------------------------------------------- #
# b) Butterbase
# --------------------------------------------------------------------------- #
def check_butterbase() -> None:
    name = "BUTTERBASE"
    try:
        api_url = os.environ["BUTTERBASE_API_URL"].rstrip("/")  # .../v1/{app_id}
        api_key = os.environ["BUTTERBASE_API_KEY"]
        app_id = os.environ.get("BUTTERBASE_APP_ID", "")
        headers = {"Authorization": f"Bearer {api_key}"}

        with httpx.Client(timeout=30.0, headers=headers) as client:
            # Verify auth works: read the app schema (service key required).
            r = client.get(f"{api_url}/schema")
            r.raise_for_status()
            schema = r.json()

            # Idempotently ensure the `scans` table exists.
            desired = {
                "schema": {
                    "tables": {
                        "scans": {
                            "columns": {
                                "id": {
                                    "type": "uuid",
                                    "primaryKey": True,
                                    "default": "gen_random_uuid()",
                                },
                                "device_id": {"type": "text", "nullable": False},
                                "score": {"type": "integer"},
                                "verdict": {"type": "text"},
                                "created_at": {"type": "timestamptz", "default": "now()"},
                            }
                        }
                    }
                }
            }
            r = client.post(f"{api_url}/schema/apply", json=desired)
            r.raise_for_status()

            # Insert one test row.
            row = {"device_id": f"smoke-{int(time.time())}", "score": 42, "verdict": "test"}
            r = client.post(f"{api_url}/scans", json=row)
            r.raise_for_status()
            inserted = r.json()
            if isinstance(inserted, list):
                inserted = inserted[0]
            row_id = inserted["id"]

            # Read it back.
            r = client.get(f"{api_url}/scans/{row_id}")
            r.raise_for_status()
            fetched = r.json()
            if isinstance(fetched, list):
                fetched = fetched[0]
            assert fetched["device_id"] == row["device_id"], "read-back mismatch"

        record(
            name,
            True,
            f"auth ok (app={app_id}, {len(schema.get('tables', {}))} tables); "
            f"ensured `scans`; inserted+read row {row_id}",
        )
    except Exception as e:  # noqa: BLE001
        detail = f"{type(e).__name__}: {e}"
        if isinstance(e, httpx.HTTPStatusError):
            detail += f" | body: {e.response.text[:300]}"
        record(name, False, detail)


# --------------------------------------------------------------------------- #
# c) RocketRide Cloud
# --------------------------------------------------------------------------- #
def check_rocketride() -> None:
    name = "ROCKETRIDE"

    async def _run() -> str:
        from rocketride import RocketRideClient

        uri = os.environ["ROCKETRIDE_URI"]
        auth = os.environ["ROCKETRIDE_AUTH"]

        # Constructor params take priority over .env; our key lives in
        # ROCKETRIDE_AUTH, not the SDK's default ROCKETRIDE_APIKEY.
        client = RocketRideClient(uri=uri, auth=auth)
        try:
            await client.connect()
            if not client.is_authenticated():
                raise RuntimeError("connected but not authenticated")
            info = client.get_account_info()
            return f"connected + authenticated to {uri} (account_info={info is not None})"
        finally:
            await client.disconnect()

    try:
        detail = asyncio.run(_run())
        record(name, True, detail)
    except Exception as e:  # noqa: BLE001
        record(name, False, f"{type(e).__name__}: {e}")


# --------------------------------------------------------------------------- #
# d) Discord webhook
# --------------------------------------------------------------------------- #
def check_discord() -> None:
    name = "DISCORD"
    try:
        url = os.environ["DISCORD_WEBHOOK_URL"]
        payload = {
            "embeds": [
                {
                    "title": "✅ CleanPlay smoke test",
                    "description": "All systems check — Phase 1 smoke test.",
                    "color": 0x2ECC71,
                }
            ]
        }
        # wait=true makes Discord return the created message so we can confirm.
        r = httpx.post(url, json=payload, params={"wait": "true"}, timeout=30.0)
        r.raise_for_status()
        record(name, True, f"posted embed (HTTP {r.status_code})")
    except Exception as e:  # noqa: BLE001
        detail = f"{type(e).__name__}: {e}"
        if isinstance(e, httpx.HTTPStatusError):
            detail += f" | body: {e.response.text[:200]}"
        record(name, False, detail)


def main() -> int:
    print("=== CleanPlay smoke test ===\n")
    check_neo4j()
    check_butterbase()
    check_rocketride()
    check_discord()

    print("\n=== Summary ===")
    width = max(len(n) for n, _, _ in RESULTS)
    for name, passed, _ in RESULTS:
        print(f"  {name.ljust(width)}  {'PASS ✅' if passed else 'FAIL ❌'}")

    all_pass = all(p for _, p, _ in RESULTS)
    print(f"\n{'ALL CHECKS PASSED' if all_pass else 'SOME CHECKS FAILED'}")
    return 0 if all_pass else 1


if __name__ == "__main__":
    sys.exit(main())
