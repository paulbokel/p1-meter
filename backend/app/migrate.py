"""Apply versioned SQL migrations from MIGRATIONS_DIR in filename order.

Each file runs once, in its own transaction, and is recorded in
schema_migrations. Run as `python -m app.migrate` (the compose stack does this
in a one-shot service before the collector and backend start).
"""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

import asyncpg

log = logging.getLogger("migrate")


async def migrate(dsn: str, directory: Path) -> list[str]:
    con = None
    for attempt in range(30):
        try:
            con = await asyncpg.connect(dsn)
            break
        except (OSError, asyncpg.PostgresError) as e:
            log.info("waiting for database (%s)", e)
            await asyncio.sleep(2)
    if con is None:
        raise RuntimeError("database not reachable")
    applied: list[str] = []
    try:
        await con.execute("CREATE TABLE IF NOT EXISTS schema_migrations ("
                          "version text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())")
        done = {r["version"] for r in await con.fetch("SELECT version FROM schema_migrations")}
        for path in sorted(directory.glob("*.sql")):
            if path.name in done:
                continue
            log.info("applying %s", path.name)
            # Each file is atomic. Continuous aggregates must be created
            # WITH NO DATA to be allowed inside a transaction.
            async with con.transaction():
                await con.execute(path.read_text())
                await con.execute("INSERT INTO schema_migrations (version) VALUES ($1)", path.name)
            applied.append(path.name)
    finally:
        await con.close()
    return applied


def main() -> None:
    logging.basicConfig(level="INFO", format="%(asctime)s %(levelname)s %(name)s %(message)s")
    applied = asyncio.run(migrate(os.environ["DATABASE_URL"],
                                  Path(os.environ.get("MIGRATIONS_DIR", "/app/migrations"))))
    log.info("migrations applied: %s", ", ".join(applied) or "none (up to date)")


if __name__ == "__main__":
    main()
