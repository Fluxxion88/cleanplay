"""CleanPlay — public deployment verification.

Runs the full moderator demo flow against the PUBLIC Railway URL, proving the
deployed container talks to Neo4j, RocketRide Cloud, Butterbase, and Discord.
Keeps the paid PRO state (payment requirement satisfied).

Run:  CLEANPLAY_BASE=https://cleanplay-production.up.railway.app python scripts/e2e_public.py
"""
from __future__ import annotations

import os
import sys

import httpx
from dotenv import load_dotenv

import seed_graph

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

BASE = os.environ.get("CLEANPLAY_BASE", "https://cleanplay-production.up.railway.app").rstrip("/")
BB = os.environ["BUTTERBASE_API_URL"].rstrip("/")
BBH = {"Authorization": f"Bearer {os.environ['BUTTERBASE_API_KEY']}", "Content-Type": "application/json"}
EMAIL, PASSWORD = "moderator@cleanplay.demo", "CleanPlay!2026"

results: list[tuple[str, bool, str]] = []


def check(label, passed, detail):
    results.append((label, passed, detail))
    print(f"[{'PASS' if passed else 'FAIL'}] {label}: {detail}")


def main() -> int:
    print(f"Target: {BASE}\nRe-seeding graph for a clean demo state ...")
    seed_graph.main()
    print()

    api = httpx.Client(base_url=BASE, timeout=180.0)
    sk = httpx.Client(timeout=30.0)

    # a) dashboard loads
    r = api.get("/")
    check("a) dashboard loads at / (public)",
          r.status_code == 200 and "CleanPlay" in r.text and "Upgrade to PRO" in r.text,
          f"status={r.status_code} bytes={len(r.text)}")

    # b) auth: unauth 401, then login
    check("b1) unauthenticated /scan rejected (401)",
          api.post("/scan/D-42").status_code == 401, "401 enforced")
    login = api.post("/api/login", json={"email": EMAIL, "password": PASSWORD})
    ok = login.status_code == 200
    check("b2) moderator login works", ok, f"status={login.status_code}")
    if not ok:
        print("cannot continue"); return 1
    tok = login.json()["access_token"]; uid = login.json()["user"]["id"]
    AUTH = {"Authorization": f"Bearer {tok}"}

    # keep the paid PRO state, clean the counter
    ws = sk.get(f"{BB}/workspaces", headers=BBH, params={"user_id": f"eq.{uid}", "limit": 1}).json()[0]
    sk.patch(f"{BB}/workspaces/{ws['id']}", headers=BBH,
             json={"plan": "PRO", "scans_used": 0, "scan_limit": 1000000})

    # c) scan D-42 -> dirty + Discord alert + counter increments (cloud pipeline from Railway)
    r = api.post("/scan/D-42", headers=AUTH).json()
    check("c) /scan D-42 dirty + Discord alert + counter++ (RocketRide Cloud from Railway)",
          r["verdict"] == "dirty" and r["alert_sent"] and r["usage"]["scans_used"] == 1,
          f"score={r['score']} verdict={r['verdict']} alert={r['alert_sent']} "
          f"counter={r['usage']['scans_used']} plan={r['usage']['plan']}")
    print(f"     report: {r['report_text'][:100]}...")

    # d) restrict
    r = api.post("/restrict/D-42", headers=AUTH).json()
    names = sorted(a["name"] for a in r["restricted_accounts"])
    check("d) restrict D-42 -> both mules", r["restricted_count"] == 2
          and names == ["mulealt_01", "mulealt_02"], f"restricted={names}")

    # e) simulate new account born restricted
    r = api.post("/simulate_new_account/D-42", headers=AUTH).json()
    check("e) new account on D-42 born RESTRICTED", r["born_restricted"] is True,
          f"latest={r['latest_scan_verdict']} restricted={r['account']['restricted']}")

    # f) trusted scan, no alert
    r = api.post("/scan/D-FAM1", headers=AUTH).json()
    check("f) /scan D-FAM1 trusted + NO alert",
          r["verdict"] == "trusted" and r["alert_sent"] is False,
          f"score={r['score']} verdict={r['verdict']} alert={r['alert_sent']}")

    # g) billing counter + upgrade action live
    me = api.get("/api/me", headers=AUTH).json()
    up = api.post("/upgrade", headers=AUTH, json={"origin": BASE}).json()
    check("g) billing counter + Stripe upgrade action live",
          "scans_used" in me["usage"] and up.get("mode") == "stripe"
          and str(up.get("checkout_url", "")).startswith("https://checkout.stripe.com"),
          f"plan={me['usage']['plan']} counter={me['usage']['scans_used']} "
          f"upgrade_mode={up.get('mode')} checkout={'yes' if up.get('checkout_url') else 'no'}")

    # show Butterbase state
    ws = sk.get(f"{BB}/workspaces", headers=BBH, params={"user_id": f"eq.{uid}", "limit": 1}).json()[0]
    plans = sk.get(f"{BB}/billing/plans", headers=BBH).json().get("plans", [])
    print("\n  Butterbase workspace:",
          f"plan={ws['plan']} scans_used={ws['scans_used']} pro_plan_id={ws['pro_plan_id']}")
    for p in plans:
        print(f"  Butterbase billing plan: {p['name']} ${p['price_cents']/100:.2f}/{p['interval']} (id={p['id']})")

    api.close(); sk.close()

    print("\n" + "=" * 70)
    print(f"{'STEP':<56}{'RESULT'}")
    print("-" * 70)
    for label, passed, _ in results:
        print(f"{label:<56}{'PASS ✅' if passed else 'FAIL ❌'}")
    print("=" * 70)
    allok = all(p for _, p, _ in results)
    print("PUBLIC DEPLOYMENT:", "ALL PASSED ✅" if allok else "SOME FAILED ❌")
    print(f"\nPUBLIC URL: {BASE}")
    print(f"DEMO LOGIN: {EMAIL} / {PASSWORD}")
    return 0 if allok else 1


if __name__ == "__main__":
    sys.exit(main())
