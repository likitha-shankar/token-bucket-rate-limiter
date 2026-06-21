import aiosqlite
import time
from typing import Optional
from src.models import Algorithm, ClientConfig

DB_PATH = "ratelimiter.db"


async def init_db(db_path: str = DB_PATH) -> None:
    async with aiosqlite.connect(db_path) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA synchronous=NORMAL")
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS client_configs (
                client_key TEXT PRIMARY KEY,
                rate        REAL NOT NULL,
                burst_size  INTEGER NOT NULL,
                algorithm   TEXT NOT NULL DEFAULT 'token_bucket',
                window_size REAL NOT NULL DEFAULT 1.0
            );

            CREATE TABLE IF NOT EXISTS bucket_state (
                client_key  TEXT PRIMARY KEY,
                tokens      REAL NOT NULL,
                last_refill REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS request_log (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                client_key TEXT NOT NULL,
                ts         REAL NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_request_log_key_ts
                ON request_log (client_key, ts);
        """)
        await db.commit()


async def upsert_client(client_key: str, config: ClientConfig, db_path: str = DB_PATH) -> None:
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """
            INSERT INTO client_configs (client_key, rate, burst_size, algorithm, window_size)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(client_key) DO UPDATE SET
                rate        = excluded.rate,
                burst_size  = excluded.burst_size,
                algorithm   = excluded.algorithm,
                window_size = excluded.window_size
            """,
            (client_key, config.rate, config.burst_size, config.algorithm.value, config.window_size),
        )
        # Reset bucket state on config change
        await db.execute("DELETE FROM bucket_state WHERE client_key = ?", (client_key,))
        await db.execute("DELETE FROM request_log WHERE client_key = ?", (client_key,))
        await db.commit()


async def get_client(client_key: str, db_path: str = DB_PATH) -> Optional[ClientConfig]:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM client_configs WHERE client_key = ?", (client_key,)
        ) as cursor:
            row = await cursor.fetchone()
    if row is None:
        return None
    return ClientConfig(
        rate=row["rate"],
        burst_size=row["burst_size"],
        algorithm=Algorithm(row["algorithm"]),
        window_size=row["window_size"],
    )


async def list_clients(db_path: str = DB_PATH) -> list[dict]:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM client_configs ORDER BY client_key") as cursor:
            rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def delete_client(client_key: str, db_path: str = DB_PATH) -> bool:
    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute(
            "DELETE FROM client_configs WHERE client_key = ?", (client_key,)
        )
        await db.execute("DELETE FROM bucket_state WHERE client_key = ?", (client_key,))
        await db.execute("DELETE FROM request_log WHERE client_key = ?", (client_key,))
        await db.commit()
        return cursor.rowcount > 0


async def get_bucket_state(client_key: str, db_path: str = DB_PATH) -> Optional[tuple[float, float]]:
    """Returns (tokens, last_refill) or None if no state saved yet."""
    async with aiosqlite.connect(db_path) as db:
        async with db.execute(
            "SELECT tokens, last_refill FROM bucket_state WHERE client_key = ?", (client_key,)
        ) as cursor:
            row = await cursor.fetchone()
    return (row[0], row[1]) if row else None


async def set_bucket_state(client_key: str, tokens: float, last_refill: float, db_path: str = DB_PATH) -> None:
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """
            INSERT INTO bucket_state (client_key, tokens, last_refill)
            VALUES (?, ?, ?)
            ON CONFLICT(client_key) DO UPDATE SET
                tokens      = excluded.tokens,
                last_refill = excluded.last_refill
            """,
            (client_key, tokens, last_refill),
        )
        await db.commit()


async def count_requests_in_window(client_key: str, window_start: float, db_path: str = DB_PATH) -> int:
    async with aiosqlite.connect(db_path) as db:
        async with db.execute(
            "SELECT COUNT(*) FROM request_log WHERE client_key = ? AND ts >= ?",
            (client_key, window_start),
        ) as cursor:
            row = await cursor.fetchone()
    return row[0] if row else 0


async def add_request_log(client_key: str, ts: float, db_path: str = DB_PATH) -> None:
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "INSERT INTO request_log (client_key, ts) VALUES (?, ?)", (client_key, ts)
        )
        # Prune old entries (keep only last 10x window_size worth)
        await db.execute(
            "DELETE FROM request_log WHERE client_key = ? AND ts < ?",
            (client_key, ts - 600),  # 10-minute max retention
        )
        await db.commit()
