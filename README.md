# Token Bucket Rate Limiter Service

A standalone, networked rate-limiting API. Other services call into it to check whether a request should be allowed or denied — not a library you import, a service you deploy.

**Live demo:** https://token-bucket-rate-limiter-7n47.onrender.com/dashboard  
**API docs:** https://token-bucket-rate-limiter-7n47.onrender.com/docs

> Free tier note: first request after 15 min idle takes ~30s to wake up.

---

## What it does

- Accepts `POST /check/{client_key}` → returns `200 ALLOW` or `429 DENY`
- Two algorithms per client: **token bucket** (burst-friendly) and **sliding window** (strict cap)
- Configures clients at runtime via admin API — no code changes or redeploys
- Persists state to SQLite (WAL mode) — survives restarts
- Race-condition safe under 500+ concurrent requests via per-key `asyncio.Lock`
- Returns standard rate-limit headers on every response

---

## Quick start

```bash
git clone https://github.com/likitha-shankar/token-bucket-rate-limiter
cd token-bucket-rate-limiter

python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

uvicorn src.main:app --host 0.0.0.0 --port 8000 --reload
```

Open http://localhost:8000/docs for interactive API docs, or http://localhost:8000/dashboard for the live dashboard.

---

## How to use it

### 1. Configure a client

```bash
curl -X POST http://localhost:8000/admin/clients/my-service \
  -H "Content-Type: application/json" \
  -d '{
    "rate": 10,
    "burst_size": 50,
    "algorithm": "token_bucket"
  }'
```

| Field | Type | Description |
|---|---|---|
| `rate` | float | Tokens refilled per second |
| `burst_size` | int | Max tokens / bucket capacity |
| `algorithm` | string | `"token_bucket"` or `"sliding_window"` |
| `window_size` | float | Window duration in seconds (sliding window only, default 1.0) |

### 2. Check a request

```bash
curl -X POST http://localhost:8000/check/my-service
```

**Allowed (200):**
```
HTTP/1.1 200 OK
X-RateLimit-Limit: 50
X-RateLimit-Remaining: 49
X-RateLimit-Reset: 1750000060

{"allowed": true, "client_key": "my-service", "algorithm": "token_bucket", "tokens_remaining": 49.0}
```

**Denied (429):**
```
HTTP/1.1 429 Too Many Requests
X-RateLimit-Limit: 50
X-RateLimit-Remaining: 0
X-RateLimit-Reset: 1750000065
Retry-After: 5

{"allowed": false, "client_key": "my-service", "algorithm": "token_bucket", "tokens_remaining": 0.0}
```

### 3. Use sliding window mode

```bash
curl -X POST http://localhost:8000/admin/clients/strict-client \
  -H "Content-Type: application/json" \
  -d '{
    "rate": 5,
    "burst_size": 5,
    "algorithm": "sliding_window",
    "window_size": 60
  }'
```

This allows exactly 5 requests per 60-second rolling window — no burst.

---

## All endpoints

| Method | Path | Description |
|---|---|---|
| `POST` | `/check/{client_key}` | Check if request is allowed |
| `POST` | `/admin/clients/{key}` | Create or update client config |
| `GET` | `/admin/clients` | List all configured clients |
| `GET` | `/admin/clients/{key}` | Get one client's config |
| `DELETE` | `/admin/clients/{key}` | Remove client and reset state |
| `GET` | `/stats?window=60` | Rolling stats per client |
| `GET` | `/dashboard` | Live visual dashboard |
| `GET` | `/health` | Health check |
| `GET` | `/docs` | Interactive Swagger UI |

---

## Algorithms

### Token Bucket
Each client has a bucket that holds up to `burst_size` tokens, refilling at `rate` per second. Each allowed request consumes one token.

```
tokens = min(burst_size, stored_tokens + elapsed × rate)
allowed = tokens >= 1
```

Best for: APIs that should allow short bursts while enforcing a long-run average.

### Sliding Window
Counts requests in the last `window_size` seconds. Hard cap at `rate × window_size`.

```
limit  = int(rate × window_size)
count  = requests in last window_size seconds
allowed = count < limit
```

Best for: strict per-period caps with zero burst tolerance.

---

## Dashboard

`GET /dashboard` — live single-page dashboard that polls `/stats` every second:

- **Summary cards** — req/s, allow/s, deny/s, active client count
- **Line chart** — rolling 60s time series of requests / allowed / denied
- **Bar chart** — stacked allowed vs denied per client
- **Table** — per-client req/s, deny/s, algorithm, and visual allow-rate bar

---

## Architecture

```
caller service
     │
     ▼
POST /check/{key}
     │
     ├── per-key asyncio.Lock (no double-spend)
     │
     ├── Token Bucket  ──► read/write bucket_state (SQLite WAL)
     │
     └── Sliding Window ──► count/append request_log (SQLite WAL)
                            ──► async write to event_log (dashboard)
```

**Concurrency:** all 500 concurrent requests for the same key serialize behind one lock. Different keys run fully in parallel.

**Persistence:** SQLite with WAL mode. All state written on every request. Schema auto-created on startup.

---

## Load test

```bash
# Start the server first
uvicorn src.main:app --port 8000

# In another terminal
PYTHONPATH=. python tests/load_test.py
```

Three scenarios:

| Test | Keys | Concurrent | Assert |
|---|---|---|---|
| Single-key burst | 1 | 500 | Exactly `burst_size` allowed, zero double-spend |
| Sliding window | 1 | 200 | Exactly `rate × window` allowed |
| Multi-key throughput | 100 | 500 | All 500 allowed (parallel, no contention) |

---

## Project structure

```
├── src/
│   ├── main.py          # FastAPI app, all endpoints
│   ├── limiter.py       # Token bucket + sliding window logic
│   ├── storage.py       # SQLite persistence layer
│   ├── models.py        # Pydantic request/response models
│   └── dashboard.html   # Live dashboard (Chart.js)
├── tests/
│   └── load_test.py     # 500-concurrent correctness + throughput test
├── .github/
│   └── workflows/
│       └── keep-warm.yml  # Pings Render every 14 min to prevent cold starts
├── Dockerfile
├── render.yaml
└── requirements.txt
```

---

## Deployment

### Render (one-click)

1. Fork this repo
2. Go to [render.com](https://render.com) → New → Web Service
3. Connect your fork — Render auto-detects `render.yaml`
4. Click **Deploy**

The `render.yaml` sets build command, start command, and health check path automatically.

### Docker

```bash
docker build -t rate-limiter .
docker run -p 8000:8000 rate-limiter
```

### Environment variables

| Variable | Default | Description |
|---|---|---|
| `DB_PATH` | `ratelimiter.db` | Path to SQLite database file |
| `PORT` | `8000` | Port (set automatically by Render) |

---

## Skills covered

- Concurrency control with `asyncio.Lock` and atomic read-modify-write
- Token bucket and sliding window algorithm implementation from scratch
- SQLite WAL mode for concurrent read/write persistence
- Race condition testing under 500+ simultaneous requests
- Standard HTTP API contracts (`X-RateLimit-*` headers)
- Networked service design (vs. embedded library)
- Containerization with Docker and cloud deployment
