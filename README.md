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

### Endpoints

| Method & path                        | Purpose                                                            |
| ------------------------------------ | ----------------------------------------------------------------- |
| `GET  /health`                       | liveness                                                          |
| `POST /scan/{device_id}`             | score via cloud pipeline, persist to Butterbase, Discord alert if not trusted |
| `POST /restrict/{device_id}`         | set `restricted=true` on every account that logged in from the device |
| `POST /simulate_new_account/{device}`| new account on the device — born restricted if latest scan ≠ trusted |
| `GET  /scans`                        | scan history from Butterbase                                      |
