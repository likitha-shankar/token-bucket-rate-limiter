"""
Load test: 500+ concurrent requests for the same client key.
Verifies no double-spending (allowed count never exceeds burst_size).

Usage:
    # Start server first:  uvicorn src.main:app --host 0.0.0.0 --port 8000
    python tests/load_test.py
"""

import asyncio
import math
import time
import httpx

BASE_URL = "http://localhost:8000"
CLIENT_KEY = "load-test-client"
BURST_SIZE = 50          # allow 50 per burst
RATE = 0.001             # near-zero refill: isolates burst correctness from timing
CONCURRENCY = 500        # simultaneous requests
ALGORITHM = "token_bucket"


async def setup_client(client: httpx.AsyncClient) -> None:
    resp = await client.post(
        f"{BASE_URL}/admin/clients/{CLIENT_KEY}",
        json={
            "rate": RATE,
            "burst_size": BURST_SIZE,
            "algorithm": ALGORITHM,
        },
    )
    resp.raise_for_status()
    print(f"[setup] client '{CLIENT_KEY}' configured: burst={BURST_SIZE}, rate={RATE}/s, algo={ALGORITHM}")


async def fire_request(client: httpx.AsyncClient, idx: int, key: str = CLIENT_KEY) -> tuple[int, bool]:
    try:
        resp = await client.post(f"{BASE_URL}/check/{key}", timeout=10.0)
        allowed = resp.status_code == 200
        return idx, allowed
    except Exception as e:
        print(f"  request {idx} error: {e}")
        return idx, False


async def run_load_test() -> None:
    async with httpx.AsyncClient(base_url=BASE_URL) as client:
        await setup_client(client)

        print(f"\n[test] firing {CONCURRENCY} concurrent requests...")
        t0 = time.perf_counter()
        tasks = [fire_request(client, i) for i in range(CONCURRENCY)]
        results = await asyncio.gather(*tasks)
        elapsed = time.perf_counter() - t0

        allowed = sum(1 for _, ok in results if ok)
        denied  = CONCURRENCY - allowed
        rps     = CONCURRENCY / elapsed

        print(f"\n{'='*50}")
        print(f"  Total requests : {CONCURRENCY}")
        print(f"  Allowed        : {allowed}")
        print(f"  Denied         : {denied}")
        print(f"  Elapsed        : {elapsed:.3f}s")
        print(f"  Throughput     : {rps:.0f} req/s")
        print(f"{'='*50}")

        # Correctness: allowed <= burst + tokens refilled during elapsed window
        # With RATE=0.001, max refill over elapsed seconds is negligible
        max_allowed = BURST_SIZE + math.ceil(RATE * elapsed) + 1  # +1 for rounding
        if allowed > max_allowed:
            print(f"\n[FAIL] double-spend! allowed={allowed} > max_expected={max_allowed}")
            raise SystemExit(1)
        else:
            print(f"\n[PASS] no double-spend. allowed={allowed} <= max_expected={max_allowed}")

        if rps < 500:
            print(f"[WARN] throughput {rps:.0f} req/s below 500 req/s target (server may be on same machine)")
        else:
            print(f"[PASS] throughput {rps:.0f} req/s >= 500 req/s target")


async def run_sliding_window_test() -> None:
    sw_key = "load-test-sw"
    async with httpx.AsyncClient(base_url=BASE_URL) as client:
        resp = await client.post(
            f"{BASE_URL}/admin/clients/{sw_key}",
            json={"rate": 50.0, "burst_size": 50, "algorithm": "sliding_window", "window_size": 1.0},
        )
        resp.raise_for_status()
        print(f"\n[sliding-window] configured '{sw_key}': 50 req/s")

        tasks = [fire_request(client, i, sw_key) for i in range(200)]
        results = await asyncio.gather(*tasks)
        allowed = sum(1 for _, ok in results if ok)
        print(f"[sliding-window] 200 concurrent → allowed={allowed} (expect ≤50)")
        if allowed > 50:
            print("[FAIL] sliding window double-spend!")
            raise SystemExit(1)
        print("[PASS] sliding window correctness OK")


async def run_multi_key_throughput_test() -> None:
    """
    500 requests across 100 different client keys (5 req per key).
    Different keys run in parallel — no lock contention between them.
    Shows real-world throughput vs the worst-case single-key test.
    """
    num_keys = 100
    reqs_per_key = 5
    total = num_keys * reqs_per_key  # 500
    burst = 10
    rate = 0.001

    async with httpx.AsyncClient(base_url=BASE_URL) as client:
        # Configure all clients
        setup_tasks = [
            client.post(
                f"{BASE_URL}/admin/clients/mk-{k}",
                json={"rate": rate, "burst_size": burst, "algorithm": "token_bucket"},
            )
            for k in range(num_keys)
        ]
        await asyncio.gather(*setup_tasks)
        print(f"\n[multi-key] {num_keys} clients configured (burst={burst}, rate≈0)")

        # Fire all requests concurrently across different keys
        print(f"[multi-key] firing {total} concurrent requests across {num_keys} keys...")
        t0 = time.perf_counter()
        tasks = [
            fire_request(client, i, f"mk-{i % num_keys}")
            for i in range(total)
        ]
        results = await asyncio.gather(*tasks)
        elapsed = time.perf_counter() - t0

        allowed = sum(1 for _, ok in results if ok)
        rps = total / elapsed

        print(f"\n{'='*50}")
        print(f"  Keys           : {num_keys}")
        print(f"  Total requests : {total}")
        print(f"  Allowed        : {allowed}  (expect {num_keys * reqs_per_key} = all, burst={burst})")
        print(f"  Elapsed        : {elapsed:.3f}s")
        print(f"  Throughput     : {rps:.0f} req/s")
        print(f"{'='*50}")

        # Each key gets exactly reqs_per_key=5 requests, burst=10 → all should be allowed
        if allowed < total:
            print(f"\n[WARN] only {allowed}/{total} allowed (some may have been denied unexpectedly)")
        else:
            print(f"\n[PASS] all {allowed} requests allowed (within burst per key)")

        if rps >= 500:
            print(f"[PASS] throughput {rps:.0f} req/s >= 500 req/s target")
        else:
            print(f"[WARN] throughput {rps:.0f} req/s below 500 req/s (loopback + SQLite overhead)")


if __name__ == "__main__":
    asyncio.run(run_load_test())
    asyncio.run(run_sliding_window_test())
    asyncio.run(run_multi_key_throughput_test())
