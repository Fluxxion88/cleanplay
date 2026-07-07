"""CleanPlay Phase 4 — auth + billing + dashboard end-to-end verification.

Re-seeds the graph, resets the demo moderator's workspace to FREE 0/10, then
drives the running API through auth, metering, upgrade, and the full moderator
demo flow. Prints a PASS/FAIL summary and the demo credentials.

Prereq: API running at BASE (default http://127.0.0.1:8077). Butterbase service
key is used only to inspect/reset billing state (simulating prior usage).

Run:  python scripts/e2e_phase4.py
"""
from __future__ import annotations

import os
import sys

import httpx
from dotenv import load_dotenv

import seed_graph

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

BASE = os.environ.get("CLEANPLAY_BASE", "http://127.0.0.1:8077")
BB_URL = os.environ["BUTTERBASE_API_URL"].rstrip("/")
BB_KEY = os.environ["BUTTERBASE_API_KEY"]
BBH = {"Authorization": f"Bearer {BB_KEY}", "Content-Type": "application/json"}

EMAIL = "moderator@cleanplay.demo"
PASSWORD = "CleanPlay!2026"

results: list[tuple[str, bool, str]] = []


def check(label: str, passed: bool, detail: str) -> None:
    results.append((label, passed, detail))
    print(f"[{'PASS' if passed else 'FAIL'}] {label}: {detail}")


def _bb_get_workspace(sk: httpx.Client, user_id: str) -> dict | None:
    r = sk.get(f"{BB_URL}/workspaces", headers=BBH,
               params={"user_id": f"eq.{user_id}", "limit": 1})
    r.raise_for_status()
    rows = r.json()
    return rows[0] if rows else None


def _bb_patch_workspace(sk: httpx.Client, ws_id: str, fields: dict) -> None:
    r = sk.patch(f"{BB_URL}/workspaces/{ws_id}", headers=BBH, json=fields)
    r.raise_for_status()


def main() -> int:
    print("Re-seeding graph for a clean demo state ...")
    seed_graph.main()
    print()

    api = httpx.Client(base_url=BASE, timeout=180.0)
    sk = httpx.Client(timeout=30.0)

    # ---- a) auth: no JWT -> 401, then login ----
    r = api.post("/scan/D-42")  # no Authorization header
    check("a1) unauthenticated /scan rejected (401)", r.status_code == 401,
          f"status={r.status_code}")
    r = api.get("/scans")
    check("a2) unauthenticated /scans rejected (401)", r.status_code == 401,
          f"status={r.status_code}")
    r = api.post("/scan/D-42", headers={"Authorization": "Bearer not.a.jwt"})
    check("a3) bogus token rejected (401)", r.status_code == 401, f"status={r.status_code}")

    login = api.post("/api/login", json={"email": EMAIL, "password": PASSWORD})
    ok = login.status_code == 200 and "access_token" in login.json()
    check("a4) moderator login works", ok, f"status={login.status_code}")
    if not ok:
        print("cannot continue without login"); return 1
    tok = login.json()["access_token"]
    user_id = login.json()["user"]["id"]
    AUTH = {"Authorization": f"Bearer {tok}"}

    # reset workspace to a clean FREE 0/10 for a deterministic run
    ws = _bb_get_workspace(sk, user_id)
    if ws:
        _bb_patch_workspace(sk, ws["id"], {"plan": "FREE", "scans_used": 0,
                                           "scan_limit": 10, "pro_plan_id": None})

    # ---- b) authorized scan increments usage in Butterbase ----
    r = api.post("/scan/D-42", headers=AUTH).json()
    ws_after = _bb_get_workspace(sk, user_id)
    check("b) /scan D-42 works + usage incremented in Butterbase",
          r["verdict"] == "dirty" and r["alert_sent"] and r["usage"]["scans_used"] == 1
          and ws_after["scans_used"] == 1,
          f"verdict={r['verdict']} alert={r['alert_sent']} "
          f"usage={r['usage']['scans_used']}/{r['usage']['scan_limit']} "
          f"(butterbase scans_used={ws_after['scans_used']})")

    # trusted scan (part of demo flow d) — no alert
    r_fam = api.post("/scan/D-FAM1", headers=AUTH).json()
    check("d1) /scan D-FAM1 trusted + NO alert",
          r_fam["verdict"] == "trusted" and r_fam["alert_sent"] is False,
          f"verdict={r_fam['verdict']} alert={r_fam['alert_sent']} "
          f"usage={r_fam['usage']['scans_used']}/{r_fam['usage']['scan_limit']}")

    # ---- c) exceed FREE limit -> 402, then upgrade -> PRO -> scans work ----
    ws = _bb_get_workspace(sk, user_id)
    _bb_patch_workspace(sk, ws["id"], {"scans_used": 10})  # simulate hitting the cap
    r = api.post("/scan/D-BUY1", headers=AUTH)
    over = r.status_code == 402
    msg = r.json().get("detail", {}).get("message", "") if over else r.text[:80]
    check("c1) FREE limit exceeded -> 402 upgrade message", over, f"status={r.status_code} :: {msg}")

    up = api.post("/upgrade", headers=AUTH).json()
    check("c2) /upgrade -> PRO", up["usage"]["plan"] == "PRO",
          f"plan={up['usage']['plan']} pro_plan_id={(up.get('pro_plan') or {}).get('id')}")

    r = api.post("/scan/D-BUY1", headers=AUTH)
    check("c3) scans work again after upgrade (no 402)",
          r.status_code == 200 and r.json()["usage"]["plan"] == "PRO",
          f"status={r.status_code} plan={r.json().get('usage',{}).get('plan')}")

    # ---- d) rest of demo flow: restrict + born-restricted / unrestricted ----
    r = api.post("/restrict/D-42", headers=AUTH).json()
    names = sorted(a["name"] for a in r["restricted_accounts"])
    check("d2) restrict D-42 -> both mules", r["restricted_count"] == 2
          and names == ["mulealt_01", "mulealt_02"], f"restricted={names}")

    r = api.post("/simulate_new_account/D-42", headers=AUTH).json()
    check("d3) new account on D-42 born RESTRICTED", r["born_restricted"] is True,
          f"latest={r['latest_scan_verdict']} restricted={r['account']['restricted']}")

    r = api.post("/simulate_new_account/D-FAM1", headers=AUTH).json()
    check("d4) new account on D-FAM1 born UNRESTRICTED", r["born_restricted"] is False,
          f"latest={r['latest_scan_verdict']} restricted={r['account']['restricted']}")

    # ---- dashboard served ----
    r = api.get("/")
    check("d5) dashboard served at / (single page)",
          r.status_code == 200 and "CleanPlay" in r.text and "Upgrade to PRO" in r.text,
          f"status={r.status_code} bytes={len(r.text)}")

    # ---- e) auth + billing state visibly stored in Butterbase ----
    ws = _bb_get_workspace(sk, user_id)
    plans = sk.get(f"{BB_URL}/billing/plans", headers=BBH).json().get("plans", [])
    scans = sk.get(f"{BB_URL}/scans", headers=BBH,
                   params={"order": "created_at.desc", "limit": 5}).json()
    check("e) billing + scan state persisted in Butterbase",
          ws is not None and ws["plan"] == "PRO" and len(scans) >= 2,
          f"workspace(plan={ws['plan']}, scans_used={ws['scans_used']}), "
          f"billing_plans={len(plans)}, scan_rows>=2")
    print("\n  Butterbase workspaces row:")
    print(f"    user={ws['email']} plan={ws['plan']} scans_used={ws['scans_used']} "
          f"limit={ws['scan_limit']} pro_plan_id={ws['pro_plan_id']}")
    print("  Butterbase billing plan catalog:")
    for p in plans:
        print(f"    {p['name']}  ${p['price_cents']/100:.0f}/{p['interval']}  (id={p['id']})")
    print("  Recent Butterbase scans:")
    for s in scans[:5]:
        print(f"    {s['device_id']:<8} {s['verdict']:<11} score={s['score']}")

    api.close(); sk.close()

    print("\n" + "=" * 66)
    print(f"{'STEP':<52}{'RESULT'}")
    print("-" * 66)
    for label, passed, _ in results:
        print(f"{label:<52}{'PASS ✅' if passed else 'FAIL ❌'}")
    print("=" * 66)
    allok = all(p for _, p, _ in results)
    print("PHASE 4 END-TO-END:", "ALL PASSED ✅" if allok else "SOME FAILED ❌")
    print("\n" + "#" * 66)
    print("#  DEMO MODERATOR CREDENTIALS (use these on stage):")
    print(f"#     email:    {EMAIL}")
    print(f"#     password: {PASSWORD}")
    print(f"#     dashboard: {BASE}/")
    print("#" * 66)
    return 0 if allok else 1


if __name__ == "__main__":
    sys.exit(main())
