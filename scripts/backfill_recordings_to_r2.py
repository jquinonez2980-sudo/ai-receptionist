#!/usr/bin/env python
"""One-shot/retryable backfill: copy existing VAPI call recordings to R2.

For every calls row whose recording_key is still an http(s) URL, obtain a
WORKING download URL and store the audio in R2, then swap recording_key to
the object key. The stored URLs are mostly useless (VAPI's webhook payloads
carried unsigned R2 paths for a while, and presigned ones expire in ~1h), so
archive_call_recording() automatically falls back to minting a fresh
presigned URL via GET /call/{id} — which works for as long as VAPI retains
the audio. Rows that still fail are reported and left untouched; re-running
never makes anything worse.

Requires: DATABASE_URL, VAPI_API_KEY (for the fresh-URL fallback), and the
four R2 vars (R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY,
R2_BUCKET) — all present when run via `railway run` (ai-receptionist
service). `railway run`'s DATABASE_URL is the internal Railway network host
(postgres.railway.internal), which does not resolve from a laptop — export
DATABASE_PUBLIC_URL (from the Postgres service) too and this script will
swap to it automatically, same convention as scripts/monthly_report.py:

    DATABASE_PUBLIC_URL=$(railway run --service Postgres printenv DATABASE_PUBLIC_URL) \\
        railway run python scripts/backfill_recordings_to_r2.py --dry-run

Usage:
    python scripts/backfill_recordings_to_r2.py [--dry-run] [--limit N]
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # repo root

from sqlalchemy import text  # noqa: E402

from platform_api.recordings import archive_call_recording, r2_configured  # noqa: E402
from platform_db import get_engine  # noqa: E402


def _use_public_db_url_if_needed() -> None:
    db_url = os.environ.get("DATABASE_URL", "")
    public_url = os.environ.get("DATABASE_PUBLIC_URL")
    if ".railway.internal" in db_url and public_url:
        os.environ["DATABASE_URL"] = public_url


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="list candidates, copy nothing")
    ap.add_argument("--limit", type=int, default=500, help="max rows this run")
    args = ap.parse_args()

    _use_public_db_url_if_needed()
    engine = get_engine()
    if engine is None:
        print("ERROR: DATABASE_URL is not set.", file=sys.stderr)
        return 1
    if not args.dry_run and not r2_configured():
        print(
            "ERROR: R2 is not configured — set R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, "
            "R2_SECRET_ACCESS_KEY, R2_BUCKET.",
            file=sys.stderr,
        )
        return 1

    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT tenant_id, vapi_call_id, recording_key FROM calls "
                "WHERE recording_key LIKE 'http%' ORDER BY started_at DESC LIMIT :n"
            ),
            {"n": args.limit},
        ).all()

    print(f"{len(rows)} recording URL(s) still pointing at VAPI.")
    if args.dry_run:
        for tid, cid, url in rows:
            print(f"  would copy  {tid}  {cid}  {url[:70]}")
        return 0

    copied, failed = [], []
    for tid, cid, url in rows:
        key = archive_call_recording(tid, cid, url)
        if key:
            with engine.begin() as conn:
                conn.execute(
                    text("UPDATE calls SET recording_key = :k WHERE vapi_call_id = :c"),
                    {"k": key, "c": cid},
                )
            copied.append(cid)
            print(f"  copied  {tid}  {cid} -> {key}")
        else:
            failed.append(cid)
            print(f"  FAILED  {tid}  {cid} (see warning log above for cause)")

    print(f"\nDone: {len(copied)} copied, {len(failed)} failed.")
    if failed:
        print("Failed rows keep their URL — re-run any time. Check the warning "
              "log above: a failure on every row usually means R2 credentials "
              "or bucket config are wrong, not that recordings expired.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
