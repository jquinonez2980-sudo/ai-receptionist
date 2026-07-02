"""Delete LangGraph checkpoint threads idle longer than a retention window.

Finding 9.3: nothing prunes the checkpoint tables (checkpoints, checkpoint_blobs,
checkpoint_writes) in Postgres — every anonymous web visitor and every voice call
creates permanent rows on every turn, forever. This is a standalone maintenance
script, not something api.py runs automatically (deleting conversation history is
a destructive, deliberate action that shouldn't run inline with request serving).

Usage:
    python scripts/cleanup_old_checkpoints.py                 # dry run, 90-day window
    python scripts/cleanup_old_checkpoints.py --days 30       # dry run, 30-day window
    python scripts/cleanup_old_checkpoints.py --days 90 --yes # actually delete

Wire into Railway as a scheduled Cron Job (Railway dashboard -> New -> Cron Job,
same repo/image, command `python scripts/cleanup_old_checkpoints.py --yes`,
schedule e.g. "0 6 * * 0" for weekly). Requires DATABASE_URL in that job's env
(same value as the main service).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from datetime import datetime, timedelta, timezone

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("cleanup_old_checkpoints")

# Threads with no thread_id (shouldn't happen) or an empty string are skipped —
# defensive, since delete_thread with a blank id would match nothing but it's
# not worth risking a malformed query.
_FIND_STALE_THREADS_SQL = """
    SELECT thread_id, MAX((checkpoint->>'ts')::timestamptz) AS last_seen
    FROM checkpoints
    WHERE thread_id IS NOT NULL AND thread_id <> ''
    GROUP BY thread_id
    HAVING MAX((checkpoint->>'ts')::timestamptz) < %(cutoff)s
    ORDER BY last_seen ASC
"""


async def _run(days: int, apply: bool) -> None:
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        log.error("DATABASE_URL is not set — nothing to clean up against.")
        sys.exit(1)

    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    from psycopg_pool import AsyncConnectionPool

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    pool = AsyncConnectionPool(conninfo=db_url, max_size=5, kwargs={"autocommit": True}, open=False)
    await pool.open()
    try:
        saver = AsyncPostgresSaver(pool)
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(_FIND_STALE_THREADS_SQL, {"cutoff": cutoff})
                rows = await cur.fetchall()

        if not rows:
            log.info("No threads idle since before %s — nothing to do.", cutoff.date())
            return

        log.info(
            "%d thread(s) idle since before %s (retention window: %d days).",
            len(rows), cutoff.date(), days,
        )
        if not apply:
            for thread_id, last_seen in rows:
                log.info("  [dry run] would delete thread_id=%s (last seen %s)", thread_id, last_seen)
            log.info("Dry run only — re-run with --yes to actually delete.")
            return

        deleted = 0
        for thread_id, last_seen in rows:
            try:
                await saver.adelete_thread(thread_id)
                deleted += 1
                log.info("  deleted thread_id=%s (last seen %s)", thread_id, last_seen)
            except Exception:
                log.exception("  failed to delete thread_id=%s", thread_id)
        log.info("Deleted %d/%d stale thread(s).", deleted, len(rows))
    finally:
        await pool.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--days", type=int, default=int(os.environ.get("CHECKPOINT_RETENTION_DAYS", "90")),
        help="Delete threads with no activity in this many days (default: 90, or CHECKPOINT_RETENTION_DAYS env var).",
    )
    parser.add_argument(
        "--yes", action="store_true",
        help="Actually delete. Without this flag, only reports what would be deleted.",
    )
    args = parser.parse_args()
    if sys.platform == "win32":
        # psycopg's async mode can't run on Windows' default ProactorEventLoop —
        # only matters if this is run manually from a Windows machine; Railway's
        # Linux container never hits this.
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(_run(args.days, args.yes))


if __name__ == "__main__":
    main()
