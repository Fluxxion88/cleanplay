"""CleanPlay Phase 2 — Neo4j graph model + synthetic RMT (gold-selling) data.

Idempotent: every run WIPES all CleanPlay data (Account/Device/IP nodes and their
relationships) and recreates it from a fixed random seed, so it is safe to run
right before a live demo.

Graph model
-----------
Nodes:
  (:Account {id, name, created_at, restricted})   created_at = epoch seconds (int)
  (:Device  {id, fingerprint})
  (:IP      {addr})
Relationships:
  (Account)-[:LOGGED_IN_FROM {first_seen, last_seen, count}]->(Device)
  (Device)-[:USES_IP]->(IP)
  (Account)-[:TRANSFERRED_TO {amount, ts}]->(Account)

Populations:
  - ~40 background legit players (organic, small bidirectional transfers)
  - the RING: farmer -> 2 mules (SAME device D-42) -> 3 buyers (one-directional)
  - the innocent FAMILY: 3 accounts, 3 devices, ONE shared home IP, organic

Run:  python scripts/seed_graph.py
"""
from __future__ import annotations

import os
import random
from pathlib import Path

from dotenv import load_dotenv
from neo4j import GraphDatabase

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")

RNG = random.Random(42)  # fixed seed => reproducible

# Fixed clock so the data is identical every run (epoch seconds).
NOW = 1_720_000_000
HOUR = 3600
DAY = 86_400

# --------------------------------------------------------------------------- #
# In-memory row buffers, flushed to Neo4j at the end.
# --------------------------------------------------------------------------- #
accounts: list[dict] = []
devices: list[dict] = []
ips: list[dict] = []
device_ips: list[dict] = []
logins: list[dict] = []
transfers: list[dict] = []

_seen_ips: set[str] = set()


def add_account(aid: str, name: str, created_at: int, restricted: bool = False) -> str:
    accounts.append({"id": aid, "name": name, "created_at": created_at, "restricted": restricted})
    return aid


def add_device(did: str) -> str:
    devices.append({"id": did, "fingerprint": f"fp_{RNG.getrandbits(48):012x}"})
    return did


def add_ip(addr: str) -> str:
    if addr not in _seen_ips:
        ips.append({"addr": addr})
        _seen_ips.add(addr)
    return addr


def add_device_ip(did: str, addr: str) -> None:
    add_ip(addr)
    device_ips.append({"device_id": did, "addr": addr})


def add_login(aid: str, did: str, first_seen: int, last_seen: int, count: int) -> None:
    logins.append(
        {"account_id": aid, "device_id": did, "first_seen": first_seen,
         "last_seen": last_seen, "count": count}
    )


def add_transfer(src: str, dst: str, amount: int, ts: int) -> None:
    transfers.append({"from_id": src, "to_id": dst, "amount": amount, "ts": ts})


# --------------------------------------------------------------------------- #
# a) BACKGROUND — ~40 legit players
# --------------------------------------------------------------------------- #
FIRST = ["Aria", "Bram", "Cira", "Dax", "Enna", "Finn", "Gwen", "Hale", "Iris", "Jax",
         "Kira", "Lyle", "Mira", "Nolan", "Opal", "Pax", "Quinn", "Rhea", "Soren", "Tia",
         "Uma", "Vale", "Wren", "Xander", "Yara", "Zane"]


def build_background() -> list[str]:
    player_ids: list[str] = []
    shared_pool: list[str] = []  # a few IPs that get reused naturally
    for i in range(40):
        aid = f"P{i:03d}"
        name = f"{RNG.choice(FIRST)}{RNG.randint(1, 999)}"
        created = NOW - RNG.randint(30, 365) * DAY
        add_account(aid, name, created)
        did = add_device(f"D-P{i:03d}")

        # Most players get a unique IP; ~1 in 6 reuses one (roommates / cafe / ISP).
        if shared_pool and RNG.random() < 0.16:
            addr = RNG.choice(shared_pool)
        else:
            addr = f"10.{RNG.randint(0, 255)}.{RNG.randint(0, 255)}.{RNG.randint(1, 254)}"
            if RNG.random() < 0.25:
                shared_pool.append(addr)
        add_device_ip(did, addr)

        last = NOW - RNG.randint(0, 5) * DAY
        add_login(aid, did, first_seen=created, last_seen=last, count=RNG.randint(20, 600))
        player_ids.append(aid)

    # Organic transfers: small (10-500), bidirectional between friends, irregular timing.
    for aid in player_ids:
        for friend in RNG.sample(player_ids, RNG.randint(1, 3)):
            if friend == aid:
                continue
            for _ in range(RNG.randint(1, 4)):
                ts = NOW - RNG.randint(0, 30) * DAY - RNG.randint(0, DAY)
                add_transfer(aid, friend, RNG.randint(10, 500), ts)
            # occasional pay-back the other direction
            if RNG.random() < 0.6:
                for _ in range(RNG.randint(1, 3)):
                    ts = NOW - RNG.randint(0, 30) * DAY - RNG.randint(0, DAY)
                    add_transfer(friend, aid, RNG.randint(10, 500), ts)
    return player_ids


# --------------------------------------------------------------------------- #
# b) THE RING — the demo villain
# --------------------------------------------------------------------------- #
def build_ring() -> dict:
    # Accounts. Mules are OLD (created ~25d ago) so the big inbound transfers do
    # NOT fall within 48h of their creation -> they trip funnel/density, not smurf.
    farmer = add_account("A-FARM", "GoldFarm_x7", NOW - 40 * DAY)
    mule1 = add_account("A-MULE1", "mulealt_01", NOW - 25 * DAY)
    mule2 = add_account("A-MULE2", "mulealt_02", NOW - 25 * DAY + 600)  # ~10 min apart => clustered
    # Buyers look like normal players but were created RIGHT BEFORE receiving gold.
    buyer_names = ["DragonSlayer", "Nighthawk_", "PixelKnight"]
    buyers = [add_account(f"A-BUY{i+1}", buyer_names[i], NOW - 2 * DAY) for i in range(3)]

    # Devices. The KEY SIGNAL: both mules share ONE device, D-42.
    d_farm = add_device("D-FARM")
    d_42 = add_device("D-42")
    buyer_devices = [add_device(f"D-BUY{i+1}") for i in range(3)]

    # IPs. The mules share an IP with the farmer occasionally.
    ip_ring = "45.77.13.201"       # shared ring IP
    add_device_ip(d_farm, "45.77.13.200")
    add_device_ip(d_farm, ip_ring)
    add_device_ip(d_42, "45.77.13.202")
    add_device_ip(d_42, ip_ring)   # <- occasional overlap with farmer
    for i, bd in enumerate(buyer_devices):
        add_device_ip(bd, f"98.12.{i+40}.{RNG.randint(2, 250)}")

    # Logins.
    add_login(farmer, d_farm, NOW - 40 * DAY, NOW - DAY, 210)
    add_login(mule1, d_42, NOW - 25 * DAY, NOW - DAY, 55)
    add_login(mule2, d_42, NOW - 25 * DAY + 600, NOW - DAY, 48)
    for b, bd in zip(buyers, buyer_devices):
        add_login(b, bd, NOW - 2 * DAY, NOW - DAY, RNG.randint(3, 12))

    # ---- Flow (strictly one-directional, no back-flow) ----
    t0 = NOW - 3 * DAY

    # 1) farmer -> mule1 : large chunks (5k-20k)
    farmer_chunks = [15000, 8000, 20000, 12000]
    for k, amt in enumerate(farmer_chunks):
        add_transfer(farmer, mule1, amt, t0 + k * 3 * HOUR)

    # 2) mule1 -> mule2 : forwards ~95% within hours (all AFTER farmer transfers)
    for k, amt in enumerate(farmer_chunks):
        add_transfer(mule1, mule2, round(amt * 0.95), t0 + 12 * HOUR + k * HOUR)

    # 3) mule2 -> buyers : splits, each >2000, within 48h of buyer creation
    split_start = NOW - int(1.5 * DAY)
    for j in range(7):
        add_transfer(mule2, buyers[j % 3], RNG.randint(3000, 8000), split_start + j * 2 * HOUR)

    return {"farmer": farmer, "mules": [mule1, mule2], "buyers": buyers,
            "d_farm": d_farm, "d_42": d_42, "buyer_devices": buyer_devices}


# --------------------------------------------------------------------------- #
# c) THE INNOCENT FAMILY — fairness proof (shared home IP, organic behaviour)
# --------------------------------------------------------------------------- #
def build_family() -> dict:
    names = ["mom_ranger", "dad_tank", "kid_mage"]
    ages = [300, 290, 120]  # all well older than 48h
    fam_accounts = [add_account(f"A-FAM{i+1}", names[i], NOW - ages[i] * DAY) for i in range(3)]
    fam_devices = [add_device(f"D-FAM{i+1}") for i in range(3)]

    home_ip = "73.60.44.19"  # ONE shared household IP across all 3 devices
    for a, d in zip(fam_accounts, fam_devices):
        add_device_ip(d, home_ip)
        add_login(a, d, NOW - 300 * DAY, NOW - DAY, RNG.randint(100, 400))

    # Small, bidirectional, organic transfers among the three (10-500).
    for a in fam_accounts:
        for b in fam_accounts:
            if a == b:
                continue
            if RNG.random() < 0.7:
                ts = NOW - RNG.randint(0, 30) * DAY - RNG.randint(0, DAY)
                add_transfer(a, b, RNG.randint(10, 500), ts)
    return {"accounts": fam_accounts, "devices": fam_devices, "home_ip": home_ip}


# --------------------------------------------------------------------------- #
# Neo4j write
# --------------------------------------------------------------------------- #
def wipe(session) -> None:
    session.run("MATCH (n) WHERE n:Account OR n:Device OR n:IP DETACH DELETE n")


def constraints(session) -> None:
    session.run("CREATE CONSTRAINT account_id IF NOT EXISTS "
                "FOR (a:Account) REQUIRE a.id IS UNIQUE")
    session.run("CREATE CONSTRAINT device_id IF NOT EXISTS "
                "FOR (d:Device) REQUIRE d.id IS UNIQUE")
    session.run("CREATE CONSTRAINT ip_addr IF NOT EXISTS "
                "FOR (i:IP) REQUIRE i.addr IS UNIQUE")


def write_all(session) -> None:
    session.run(
        "UNWIND $rows AS r MERGE (a:Account {id:r.id}) "
        "SET a.name=r.name, a.created_at=r.created_at, a.restricted=r.restricted",
        rows=accounts,
    )
    session.run("UNWIND $rows AS r MERGE (d:Device {id:r.id}) SET d.fingerprint=r.fingerprint",
                rows=devices)
    session.run("UNWIND $rows AS r MERGE (i:IP {addr:r.addr})", rows=ips)
    session.run(
        "UNWIND $rows AS r MATCH (d:Device {id:r.device_id}), (i:IP {addr:r.addr}) "
        "MERGE (d)-[:USES_IP]->(i)",
        rows=device_ips,
    )
    session.run(
        "UNWIND $rows AS r MATCH (a:Account {id:r.account_id}), (d:Device {id:r.device_id}) "
        "MERGE (a)-[l:LOGGED_IN_FROM]->(d) "
        "SET l.first_seen=r.first_seen, l.last_seen=r.last_seen, l.count=r.count",
        rows=logins,
    )
    session.run(
        "UNWIND $rows AS r MATCH (a:Account {id:r.from_id}), (b:Account {id:r.to_id}) "
        "CREATE (a)-[:TRANSFERRED_TO {amount:r.amount, ts:r.ts}]->(b)",
        rows=transfers,
    )


def main() -> None:
    build_background()
    ring = build_ring()
    family = build_family()

    uri = os.environ["NEO4J_URI"]
    auth = (os.environ["NEO4J_USER"], os.environ["NEO4J_PASSWORD"])
    driver = GraphDatabase.driver(uri, auth=auth)
    try:
        driver.verify_connectivity()
        with driver.session() as session:
            print("Wiping existing CleanPlay data ...")
            wipe(session)
            print("Ensuring constraints ...")
            constraints(session)
            print("Writing nodes and relationships ...")
            write_all(session)
    finally:
        driver.close()

    print("\nSeed complete:")
    print(f"  accounts   : {len(accounts)}")
    print(f"  devices    : {len(devices)}")
    print(f"  ips        : {len(ips)}")
    print(f"  logins     : {len(logins)}")
    print(f"  transfers  : {len(transfers)}")
    print(f"  ring farmer: {ring['farmer']}  mules: {ring['mules']} (device D-42)  "
          f"buyers: {ring['buyers']}")
    print(f"  family     : {family['accounts']} share IP {family['home_ip']}")


if __name__ == "__main__":
    main()
