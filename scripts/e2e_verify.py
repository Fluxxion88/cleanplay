"""CleanPlay Phase 3 — end-to-end verification (a-f).

Re-seeds the graph for a clean state, then drives the running FastAPI service
through the full demo flow and prints a PASS/FAIL summary.

Prereq: the API must be running (uvicorn api.main:app) at BASE (default
http://127.0.0.1:8000). Run:  python scripts/e2e_verify.py
"""
from __future__ import annotations

import os
import sys

import httpx

import seed_graph

BASE = os.environ.get("CLEANPLAY_BASE", "http://127.0.0.1:8000")
results: list[tuple[str, bool, str]] = []


def check(label: str, passed: bool, detail: str) -> None:
    results.append((label, passed, detail))
    print(f"[{'PASS' if passed else 'FAIL'}] {label}: {detail}")


def main() -> int:
    print("Re-seeding graph for a clean demo state ...")
    seed_graph.main()
    print()

    with httpx.Client(base_url=BASE, timeout=180.0) as c:
        # a) scan the mule device -> dirty + Discord alert + stored row
        r = c.post("/scan/D-42").json()
        check("a) POST /scan/D-42 dirty + alert + stored",
              r["verdict"] == "dirty" and r["alert_sent"] and bool(r.get("scan_id")),
              f"verdict={r['verdict']} score={r['score']} alert_sent={r['alert_sent']} "
              f"scan_id={r.get('scan_id')}")
        print(f"     report: {r['report_text'][:110]}...")

        # b) scan a family device -> trusted + NO alert
        r = c.post("/scan/D-FAM1").json()
        check("b) POST /scan/D-FAM1 trusted + NO alert",
              r["verdict"] == "trusted" and r["alert_sent"] is False,
              f"verdict={r['verdict']} score={r['score']} alert_sent={r['alert_sent']}")

        # c) restrict the mule device -> both mules restricted
        r = c.post("/restrict/D-42").json()
        names = sorted(a["name"] for a in r["restricted_accounts"])
        check("c) POST /restrict/D-42 both mules restricted",
              r["restricted_count"] == 2 and names == ["mulealt_01", "mulealt_02"],
              f"count={r['restricted_count']} accounts={names}")

        # d) new account on dirty device -> born restricted
        r = c.post("/simulate_new_account/D-42").json()
        check("d) POST /simulate_new_account/D-42 born restricted",
              r["born_restricted"] is True and r["account"]["restricted"] is True,
              f"latest_verdict={r['latest_scan_verdict']} "
              f"born_restricted={r['born_restricted']} acct={r['account']['name']}")

        # e) new account on trusted device -> born unrestricted
        r = c.post("/simulate_new_account/D-FAM1").json()
        check("e) POST /simulate_new_account/D-FAM1 born unrestricted",
              r["born_restricted"] is False and r["account"]["restricted"] is False,
              f"latest_verdict={r['latest_scan_verdict']} "
              f"born_restricted={r['born_restricted']} acct={r['account']['name']}")

        # f) scan history
        r = c.get("/scans").json()
        check("f) GET /scans shows history",
              r["count"] >= 2,
              f"count={r['count']} latest={[(s['device_id'], s['verdict']) for s in r['scans'][:4]]}")

    print("\n" + "=" * 60)
    print(f"{'STEP':<48}{'RESULT'}")
    print("-" * 60)
    for label, passed, _ in results:
        print(f"{label:<48}{'PASS ✅' if passed else 'FAIL ❌'}")
    print("=" * 60)
    allok = all(p for _, p, _ in results)
    print("END-TO-END:", "ALL PASSED ✅" if allok else "SOME FAILED ❌")
    return 0 if allok else 1


if __name__ == "__main__":
    sys.exit(main())
