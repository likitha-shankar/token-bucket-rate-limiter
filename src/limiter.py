import asyncio
import time
from dataclasses import dataclass
from typing import Optional

from src.models import Algorithm, ClientConfig
from src import storage


@dataclass
class CheckResult:
    allowed: bool
    tokens_remaining: float
    limit: int
    reset_at: float  # unix timestamp when bucket fully refills


class RateLimiter:
    def __init__(self, db_path: str = storage.DB_PATH):
        self._db_path = db_path
        self._locks: dict[str, asyncio.Lock] = {}
        self._meta_lock = asyncio.Lock()

    async def _get_lock(self, client_key: str) -> asyncio.Lock:
        async with self._meta_lock:
            if client_key not in self._locks:
                self._locks[client_key] = asyncio.Lock()
            return self._locks[client_key]

    async def check(self, client_key: str) -> Optional[CheckResult]:
        config = await storage.get_client(client_key, self._db_path)
        if config is None:
            return None

        lock = await self._get_lock(client_key)
        async with lock:
            if config.algorithm == Algorithm.TOKEN_BUCKET:
                return await self._check_token_bucket(client_key, config)
            else:
                return await self._check_sliding_window(client_key, config)

    async def _check_token_bucket(self, client_key: str, config: ClientConfig) -> CheckResult:
        now = time.time()
        state = await storage.get_bucket_state(client_key, self._db_path)

        if state is None:
            tokens = float(config.burst_size)
            last_refill = now
        else:
            tokens, last_refill = state
            elapsed = now - last_refill
            tokens = min(float(config.burst_size), tokens + elapsed * config.rate)
            last_refill = now

        allowed = tokens >= 1.0
        if allowed:
            tokens -= 1.0

        await storage.set_bucket_state(client_key, tokens, last_refill, self._db_path)

        tokens_until_full = config.burst_size - tokens
        seconds_to_full = tokens_until_full / config.rate if config.rate > 0 else 0
        reset_at = now + seconds_to_full

        return CheckResult(
            allowed=allowed,
            tokens_remaining=max(0.0, tokens),
            limit=config.burst_size,
            reset_at=reset_at,
        )

    async def _check_sliding_window(self, client_key: str, config: ClientConfig) -> CheckResult:
        now = time.time()
        window_start = now - config.window_size
        count = await storage.count_requests_in_window(client_key, window_start, self._db_path)

        limit = int(config.rate * config.window_size)
        allowed = count < limit

        if allowed:
            await storage.add_request_log(client_key, now, self._db_path)

        remaining = max(0, limit - count - (1 if allowed else 0))
        # Reset = when the oldest request in window falls out
        reset_at = now + config.window_size

        return CheckResult(
            allowed=allowed,
            tokens_remaining=float(remaining),
            limit=limit,
            reset_at=reset_at,
        )
