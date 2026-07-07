# CleanPlay

CleanPlay is a device-level trust-scoring service that detects real-money gold-selling
rings in online games. It ingests Account/Device/IP relationships into a Neo4j graph,
scores devices for ring-membership risk via a RocketRide AI pipeline, persists scan
verdicts in Butterbase (Postgres), and pushes alerts to Discord — exposed through a
FastAPI service.

## Layout

- `api/` — FastAPI service (`main.py`)
- `pipeline/` — RocketRide Cloud pipeline (`cleanplay_score.pipe`: `chat → llm_anthropic → response_answers`)
- `scripts/` — seeding, detection, scoring, pipeline + end-to-end verification

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then fill in credentials
python scripts/smoke_test.py       # Phase 1: verify all integrations
```

## Data & detection (Phase 2)

```bash
python scripts/seed_graph.py       # idempotent: graph + planted RMT ring + innocent family
python scripts/detect_queries.py   # detection queries + verification report + browser Cypher
python scripts/scoring.py          # per-device trust score verification table
```

## Brain: scoring + cloud pipeline + alerts (Phase 3)

Devices are scored 0–100 (100 = trusted). Signals: **funnel** (device sits inside a
one-directional high-volume transfer chain, dominant weight), **device density**
(multiple accounts with clustered creation), **smurf** (young accounts receiving large
transfers). Shared IP carries **zero** weight (fairness). Bands: `dirty < 40 ≤
suspicious < 70 ≤ trusted`.

The moderator **case report** is written by Claude Haiku 4.5 running in a RocketRide
Cloud pipeline (`api.rocketride.ai`), keyed via `ROCKETRIDE_ANTHROPIC_KEY`.

```bash
python scripts/deploy_pipeline.py D-42   # deploy + prove live remote invocation
uvicorn api.main:app --port 8077         # run the API
python scripts/e2e_verify.py             # drive endpoints a–f (needs the API running)
```

## Console: auth, billing, dashboard (Phase 4)

The product sits behind **Butterbase end-user auth** (email/password → RS256 JWT,
verified in FastAPI via the app's JWKS). Scans are a **metered, billable action**:
FREE = 10 scans, PRO = unlimited. Usage + plan live in a Butterbase `workspaces`
table; a real Butterbase billing plan (`CleanPlay PRO`, $25/mo) is the catalog
object referenced on upgrade. (Stripe Connect isn't onboarded for this app, so
`/upgrade` records a metered upgrade rather than invoking live Checkout.)

The single-page **dashboard** (dark theme, no build step) is served at `/`: login,
device suspicion ranking with per-device Scan, a scan result view (score gauge,
verdict badge, accounts, evidence chain, Claude case report, Restrict / Simulate
buttons), scan history, and a plan/usage widget with an Upgrade button.

```bash
uvicorn api.main:app --port 8077        # then open http://127.0.0.1:8077/
python scripts/e2e_phase4.py            # verify auth + billing + demo flow (needs API running)
```

**Demo moderator:** `moderator@cleanplay.demo` / `CleanPlay!2026`

### Endpoints

| Method & path                        | Auth | Purpose                                                     |
| ------------------------------------ | ---- | ---------------------------------------------------------- |
| `GET  /`                             | —    | T&S dashboard (single page)                                |
| `GET  /health`                       | —    | liveness                                                   |
| `POST /api/login`                    | —    | proxy to Butterbase auth → JWT                             |
| `GET  /api/me`                       | JWT  | user + plan/usage                                          |
| `GET  /devices`                      | JWT  | device suspicion ranking                                   |
| `POST /scan/{device_id}`             | JWT  | metered: cloud score + Butterbase persist + Discord alert  |
| `POST /restrict/{device_id}`         | JWT  | restrict every account that logged in from the device      |
| `POST /simulate_new_account/{device}`| JWT  | new account — born restricted if latest scan ≠ trusted     |
| `POST /upgrade`                      | JWT  | FREE → PRO                                                 |
| `GET  /scans`                        | JWT  | scan history from Butterbase                               |
