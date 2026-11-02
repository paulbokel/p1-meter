"""Wipe the test database. TimescaleDB's background jobs (continuous aggregate
refresh policies start right after a migration) can hold locks on the objects
being dropped, which Postgres resolves by aborting one side with a deadlock
error; the drop is then simply retried."""
import asyncio

import asyncpg


async def reset_schema(dsn: str, attempts: int = 5) -> None:
    con = await asyncpg.connect(dsn)
    try:
        for i in range(attempts):
            try:
                await con.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
                return
            except asyncpg.DeadlockDetectedError:
                if i == attempts - 1:
                    raise
                await asyncio.sleep(0.2 * (i + 1))
    finally:
        await con.close()
