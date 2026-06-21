# Rate Limiter Service

A standalone, networked rate-limiting API that other services call into. Implements **token bucket** and **sliding window** algorithms with persistent state, race-condition safety, and standard HTTP rate-limit headers.

---

## Why a Standalone Service?

Most apps import a rate-limiting library. This is different — it's a network service your backends call before processing a request. That means:

- **Shared state** across multiple backend instances
- **Per-client configurability** without deploying new code
- **Algorithm choice** (token bucket vs sliding window) per client at runtime

---

## Features

- `POST /check/{client_key}` → `200 ALLOW` or `429 DENY`
- Per-client rate config via admin REST endpoints
- Two algorithms selectable per client:
  - **Token Bucket** — smooth refill, supports burst
  - **Sliding Window** — strict count over rolling time window
- State persists across restarts (SQLite WAL)
- Race-condition safe under 500+ concurrent requests (per-key `asyncio.Lock`)
- Standard headers on every response: `X-RateLimit-Limit`, `X-RateLimit-Remaining`, `X-RateLimit-Reset`, `Retry-After`
- Load test proving correctness under concurrency

---

## Project Structure

```
p1/
├── src/
│   ├── main.py       # FastAPI app, all endpoints
│   ├── limiter.py    # Token bucket + sliding window logic
│   ├── storage.py    # SQLite persistence layer
│   └── models.py     # Pydantic request/response models
├── tests/
│   └── load_test.py  # Concurrent correctness + throughput test
├── requirements.txt
└── README.md
```

---

## Quick Start

```bash
# 1. Create virtualenv and install deps
python3 -m venv .venv
source .venv/bin/activate
pip install fastapi uvicorn aiosqlite httpx

# 2. Start the server
uvicorn src.main:app --host 0.0.0.0 --port 8000 --reload

# 3. Open interactive docs
open http://localhost:8000/docs
```

---

## API Reference

### Check Rate Limit

```
POST /check/{client_key}
```

Returns `200` if the request is allowed, `429` if denied.

**Response headers (always present):**

| Header | Description |
|---|---|
| `X-RateLimit-Limit` | Max tokens / window limit for this client |
| `X-RateLimit-Remaining` | Tokens left (token bucket) or requests left in window |
| `X-RateLimit-Reset` | Unix timestamp when limit resets / bucket fully refills |
| `Retry-After` | Seconds until next allowed request (429 only) |

**Example:**
```bash
curl -i -X POST http://localhost:8000/check/my-service
```

```
HTTP/1.1 200 OK
X-RateLimit-Limit: 100
X-RateLimit-Remaining: 99
X-RateLimit-Reset: 1750000060

{"allowed":true,"client_key":"my-service","algorithm":"token_bucket","tokens_remaining":99.0}
```

---

### Configure a Client

```
POST /admin/clients/{client_key}
Content-Type: application/json
```

**Body:**

| Field | Type | Required | Description |
|---|---|---|---|
| `rate` | float | yes | Tokens refilled per second (token bucket) or requests per second (sliding window) |
| `burst_size` | int | yes | Max tokens / bucket capacity |
| `algorithm` | string | no | `"token_bucket"` (default) or `"sliding_window"` |
| `window_size` | float | no | Sliding window duration in seconds (default: `1.0`) |

**Token bucket example:**
```bash
curl -X POST http://localhost:8000/admin/clients/my-service \
  -H "Content-Type: application/json" \
  -d '{"rate": 10, "burst_size": 50}'
```

**Sliding window example:**
```bash
curl -X POST http://localhost:8000/admin/clients/strict-client \
  -H "Content-Type: application/json" \
  -d '{"rate": 5, "burst_size": 5, "algorithm": "sliding_window", "window_size": 60}'
```

---

### Other Admin Endpoints

```
GET    /admin/clients              # List all configured clients
GET    /admin/clients/{key}        # Get one client's config
DELETE /admin/clients/{key}        # Remove client (resets all state)
GET    /health                     # Health check
```

---

## Algorithms

### Token Bucket

Each client has a bucket that holds up to `burst_size` tokens. Tokens refill at `rate` per second. Each allowed request consumes one token.

```
tokens = min(burst_size, stored_tokens + elapsed_seconds × rate)
if tokens ≥ 1 → ALLOW, tokens -= 1
else          → DENY
```

**Best for:** APIs that want to allow short bursts while enforcing a long-run average.

### Sliding Window

Counts requests in the rolling window `[now - window_size, now]`. Limit = `rate × window_size`.

```
limit = int(rate × window_size)
count = requests in last window_size seconds
if count < limit → ALLOW
else             → DENY
```

**Best for:** Strict per-period caps with no burst tolerance.

---

## Concurrency & Persistence

**Race safety:** Each client key has its own `asyncio.Lock`. The read-modify-write of bucket state is atomic per key. Different keys run fully in parallel.

**Persistence:** SQLite with WAL mode. Bucket state (`tokens`, `last_refill`) and sliding window request logs are written on every request. State survives process restarts.

---

## Load Test

```bash
# Start the server first
uvicorn src.main:app --port 8000

# In another terminal
PYTHONPATH=. python tests/load_test.py
```

Three scenarios:

| Test | Description | Assert |
|---|---|---|
| Single-key burst | 500 concurrent → 1 key (burst=50) | `allowed == 50`, no double-spend |
| Sliding window | 200 concurrent → 1 key (50 req/window) | `allowed == 50` |
| Multi-key throughput | 500 concurrent → 100 keys (5 req each) | `allowed == 500`, all within burst |

Expected output (loopback SQLite, single process):
```
[PASS] no double-spend. allowed=50 <= max_expected=52
[PASS] sliding window correctness OK
[PASS] all 500 requests allowed (within burst per key)
```

---

## Throughput Notes

On a single machine with SQLite (~300 req/s ceiling):
- **Same key:** all requests serialize behind one lock → worst case
- **Many keys:** parallel execution, throughput scales with key diversity

To push past 500 req/s in production:
- Run `uvicorn --workers N` with a shared Postgres/Redis backend
- Or replace SQLite with an in-memory store + async write-behind

---

## Configuration

No config files needed. All client limits are set via the admin API and persisted to `ratelimiter.db` (auto-created on first run).

To use a custom DB path, set `DB_PATH` in `src/storage.py` or pass it to `RateLimiter(db_path=...)`.

---

## License

MIT
