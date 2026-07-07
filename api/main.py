"""CleanPlay FastAPI service (Phase 1 skeleton)."""
from fastapi import FastAPI

app = FastAPI(title="CleanPlay", version="0.1.0")


@app.get("/health")
async def health() -> dict:
    """Liveness probe."""
    return {"status": "ok", "service": "cleanplay"}
