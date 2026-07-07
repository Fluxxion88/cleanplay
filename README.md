# CleanPlay

CleanPlay is a device-level trust-scoring service that detects real-money gold-selling
rings in online games. It ingests Account/Device/IP relationships into a Neo4j graph,
scores devices for ring-membership risk via a RocketRide AI pipeline, persists scan
verdicts in Butterbase (Postgres), and pushes alerts to Discord — exposed through a
FastAPI service.

## Phase 1 (skeleton)

- `api/` — FastAPI app (`GET /health`)
- `pipeline/` — RocketRide pipeline definitions (`.pipe`)
- `scripts/` — utilities, including `smoke_test.py` which verifies every integration

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then fill in credentials
python scripts/smoke_test.py
```

## Run the API

```bash
uvicorn api.main:app --reload
```
