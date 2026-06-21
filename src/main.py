import asyncio
import math
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path as FilePath
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Path, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse

from src import storage
from src.limiter import RateLimiter
from src.models import CheckResponse, ClientConfig, ClientConfigResponse

DB_PATH = os.getenv("DB_PATH", storage.DB_PATH)

_limiter: RateLimiter | None = None


def get_limiter() -> RateLimiter:
    assert _limiter is not None
    return _limiter


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _limiter
    await storage.init_db(DB_PATH)
    _limiter = RateLimiter(DB_PATH)
    yield


app = FastAPI(
    title="Rate Limiter Service",
    description="Standalone networked rate-limiting API. Token bucket + sliding window, per-client config, persistent state.",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def _apply_headers(response: Response, limit: int, remaining: float, reset_at: float) -> None:
    response.headers["X-RateLimit-Limit"] = str(limit)
    response.headers["X-RateLimit-Remaining"] = str(math.floor(remaining))
    response.headers["X-RateLimit-Reset"] = str(math.ceil(reset_at))


# ── Check endpoint ─────────────────────────────────────────────────────────────

@app.post("/check/{client_key}", response_model=CheckResponse)
async def check_rate_limit(
    client_key: Annotated[str, Path(min_length=1, max_length=128)],
    response: Response,
    limiter: RateLimiter = Depends(get_limiter),
):
    result = await limiter.check(client_key)
    if result is None:
        raise HTTPException(status_code=404, detail=f"Client '{client_key}' not configured")

    _apply_headers(response, result.limit, result.tokens_remaining, result.reset_at)

    if not result.allowed:
        retry_after = math.ceil(result.reset_at - time.time())
        response.headers["Retry-After"] = str(max(1, retry_after))
        response.status_code = 429

    asyncio.ensure_future(storage.log_event(client_key, result.allowed, time.time(), DB_PATH))

    config = await storage.get_client(client_key, DB_PATH)
    return CheckResponse(
        allowed=result.allowed,
        client_key=client_key,
        algorithm=config.algorithm.value if config else "unknown",
        tokens_remaining=result.tokens_remaining,
    )


# ── Admin endpoints ────────────────────────────────────────────────────────────

@app.post("/admin/clients/{client_key}", response_model=ClientConfigResponse, status_code=201)
async def create_or_update_client(
    client_key: Annotated[str, Path(min_length=1, max_length=128)],
    config: ClientConfig,
):
    await storage.upsert_client(client_key, config, DB_PATH)
    return ClientConfigResponse(client_key=client_key, **config.model_dump())


@app.get("/admin/clients", response_model=list[ClientConfigResponse])
async def list_clients():
    rows = await storage.list_clients(DB_PATH)
    return [ClientConfigResponse(**r) for r in rows]


@app.get("/admin/clients/{client_key}", response_model=ClientConfigResponse)
async def get_client(client_key: str):
    config = await storage.get_client(client_key, DB_PATH)
    if config is None:
        raise HTTPException(status_code=404, detail=f"Client '{client_key}' not found")
    return ClientConfigResponse(client_key=client_key, **config.model_dump())


@app.delete("/admin/clients/{client_key}", status_code=204)
async def delete_client(client_key: str):
    deleted = await storage.delete_client(client_key, DB_PATH)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Client '{client_key}' not found")


# ── Stats & Dashboard ──────────────────────────────────────────────────────────

@app.get("/stats")
async def get_stats(window: float = 60.0):
    return await storage.get_stats(window_seconds=window, db_path=DB_PATH)


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard():
    html = (FilePath(__file__).parent / "dashboard.html").read_text()
    return HTMLResponse(content=html)


# ── Health ─────────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok", "ts": time.time()}
