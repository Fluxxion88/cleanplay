# CleanPlay 🛡️

**Device-level trust scoring that busts real-money gold-selling rings in online games.**

> **🌐 Live demo:** https://cleanplay-production.up.railway.app
> **Moderator login:** `moderator@cleanplay.demo` / `CleanPlay!2026`

Gold farmers don't get caught by looking at accounts one at a time — bans just spawn
new alts. They get caught at the **device and money-flow** level: the same phone
running two "mule" accounts, the one-directional funnel of gold from a farmer through
those mules to paying buyers, and fresh accounts that receive thousands of gold hours
after signup. CleanPlay ingests game telemetry into a graph, scores each **device**
0–100 for ring involvement, writes a human case report for a moderator, and enforces
action — while provably **not** punishing an innocent family that happens to share one
home IP.

---

## The problem, concretely

A gold-selling ring looks like this:

```
GoldFarm_x7 ──5k–20k──▶ mule_01 ──95%──▶ mule_02 ──split──▶ buyer_1 / buyer_2 / buyer_3
(farmer)                └──────── SAME DEVICE D-42 ────────┘        (fresh accounts)
```

Two structural tells a naive rule misses:
- **Device co-location:** `mule_01` and `mule_02` are different accounts on the *same
  device*, created minutes apart.
- **Funnel flow:** large gold moves strictly one-directional, farmer → mules → buyers,
  never back.

And the fairness trap: a **family of 3** shares one home IP with organic, small,
two-way transfers. An IP-based rule bans them. CleanPlay's signals are structural
(device, flow, timing) and **weight shared IP at zero**, so the family stays clean.

---

## Architecture

```
                         ┌──────────────────────── CleanPlay API (FastAPI) ────────────────────────┐
   Browser dashboard     │                                                                          │
   (single page, dark) ──┤  Butterbase JWT auth ─▶ metered billing (FREE 10 / PRO ∞)                │
        │                │                                                                          │
        │  POST /scan/{device}                                                                       │
        ▼                │        ┌─────────────┐   score      ┌──────────────────────┐  report     │
   login ▶ device list ──┼──────▶ │  scoring.py │ ───────────▶ │  RocketRide Cloud     │ ──────────▶ │
   ▶ scan ▶ result ▶     │        │ funnel/     │  (features)  │  chat → llm_anthropic │  (Claude    │
   restrict / simulate   │        │ density/    │              │  → response_answers   │   Haiku 4.5)│
                         │        │ smurf       │              └──────────────────────┘             │
                         │        └──────┬──────┘                                                    │
                         │               │ Cypher                                                    │
                         │        ┌──────▼──────┐   persist scan   ┌──────────────┐  alert if !trusted │
                         │        │  Neo4j Aura │ ───────────────▶ │  Butterbase  │ ───▶ Discord embed  │
                         │        │  (graph)    │                  │  (Postgres)  │                    │
                         └────────┴─────────────┴──────────────────┴──────────────┴────────────────────┘
```

### The four integrations — and why each is load-bearing

| Integration | Role | Why it's load-bearing (not swappable) |
|---|---|---|
| **Neo4j Aura** (graph) | Stores Account/Device/IP nodes and `LOGGED_IN_FROM`, `USES_IP`, `TRANSFERRED_TO` edges. | The entire detection is **graph-shaped**: the funnel is a variable-length directed path, device co-location is a shared node, smurfing is a time-windowed edge. These are one-line Cypher traversals and painful-to-impossible in relational SQL. The graph *is* the product. |
| **RocketRide Cloud** (AI pipeline) | Hosts the `chat → llm_anthropic → response_answers` pipeline on `api.rocketride.ai` that turns a raw score + evidence into a ~150-word moderator case report. | Moderators act on **narratives**, not JSON. RocketRide runs the LLM step as managed cloud infrastructure (deploy once, invoke remotely) so the report generation isn't glued into the API process — it's a real pipeline you can evolve, trace, and swap models in. |
| **Butterbase** (BaaS) | Postgres (`scans`, `workspaces`), end-user **auth** (email/password → RS256 JWT), and **metered billing** (FREE 10 scans / PRO unlimited) with a real Stripe plan catalog. | Trust & Safety tooling is multi-tenant and gated: you need login, per-workspace usage limits, and payment. Butterbase provides auth + DB + billing as one backend, so CleanPlay is a real product with a paywall, not a script. |
| **Discord webhook** | Pushes a rich alert embed (score, verdict, top evidence, LLM report) the instant a device scores non-trusted. | Detection is worthless if nobody sees it. The webhook is the **operational surface** — the moderation team lives in Discord, so an alert there is the difference between a dashboard nobody checks and a caught ring. |

---

## How the score works

Each device starts at 100 (fully trusted). Penalties:

- **Funnel** (dominant, −70): the device's accounts sit inside a one-directional,
  high-value transfer chain from a farm-like origin. Overlapping sub-chains are
  collapsed to distinct **maximal** chains before scoring.
- **Device density** (−25 clustered / −10 loose): multiple accounts on the device with
  clustered creation times.
- **Smurf** (−25/−30): young accounts receiving >2000 gold within 48h of creation.
- **Shared IP** (**weight 0**): surfaced as evidence, never penalized — the fairness proof.

Bands: `dirty < 40 ≤ suspicious < 70 ≤ trusted`. On the seeded data: mule device **D-42
→ 5 (dirty)**, buyers → dirty, innocent family **D-FAM* → 100 (trusted)** despite a
shared home IP.

---

## Run it locally

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # fill in credentials (see env vars below)

python scripts/smoke_test.py          # verify all integrations
python scripts/seed_graph.py          # graph + planted ring + innocent family
python scripts/detect_queries.py      # detection queries + verification report
python scripts/scoring.py             # per-device trust score table
python scripts/deploy_pipeline.py     # deploy + live-invoke the RocketRide Cloud pipeline

uvicorn api.main:app --port 8077      # then open http://127.0.0.1:8077/
python scripts/e2e_phase4.py          # full auth + billing + demo flow (API must be running)
```

**Demo moderator:** `moderator@cleanplay.demo` / `CleanPlay!2026`

### Dashboard flow (projector demo)

Login → devices ranked by suspicion → **Scan D-42** (red 5/100 DIRTY gauge, Discord
alert fires, Claude case report) → **Restrict device** (both mules) → **Simulate new
account** (born restricted) → **Scan D-FAM1** (green 100 TRUSTED, no alert). The plan
widget shows the live scan counter and an **Upgrade to PRO** button (Stripe Checkout,
test mode).

---

## Endpoints

| Method & path | Auth | Purpose |
|---|---|---|
| `GET  /` | — | T&S dashboard (single page) |
| `GET  /health` | — | liveness |
| `POST /api/login` | — | proxy to Butterbase auth → JWT |
| `GET  /api/me` | JWT | user + plan/usage |
| `GET  /devices` | JWT | device suspicion ranking |
| `POST /scan/{device_id}` | JWT | metered: cloud score + persist + Discord alert |
| `POST /restrict/{device_id}` | JWT | restrict every account on the device |
| `POST /simulate_new_account/{device}` | JWT | new account, born restricted if latest scan ≠ trusted |
| `POST /upgrade` | JWT | Stripe Checkout (test) → PRO, or metered fallback |
| `POST /upgrade/confirm` | JWT | confirm Stripe subscription on return, flip to PRO |
| `GET  /scans` | JWT | scan history from Butterbase |

---

## Deploy (Docker / Railway)

`Dockerfile` runs `uvicorn api.main:app --host 0.0.0.0 --port $PORT` on `python:3.11-slim`.

Environment variables to set (all values from your `.env`):

```
NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD
ROCKETRIDE_URI, ROCKETRIDE_AUTH, ROCKETRIDE_ANTHROPIC_KEY
BUTTERBASE_API_KEY, BUTTERBASE_APP_ID, BUTTERBASE_API_URL
DISCORD_WEBHOOK_URL
ANTHROPIC_API_KEY
STRIPE_ENABLED           # "true" once Stripe Connect onboarding is complete
PUBLIC_BASE_URL          # e.g. https://cleanplay.up.railway.app (Stripe redirects)
```

```bash
railway login
railway init
railway up
# set the vars above in the Railway dashboard or via `railway variables --set K=V`
```

---

## Repo layout

```
api/         FastAPI service, auth, billing, dashboard.html
pipeline/    RocketRide Cloud pipeline (cleanplay_score.pipe)
scripts/     smoke_test, seed_graph, detect_queries, scoring, deploy_pipeline, e2e_*
Dockerfile   Railway/Docker deploy
```

Secrets live only in `.env` (git-ignored). `.env.example` documents every key.
