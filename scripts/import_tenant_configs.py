#!/usr/bin/env python
"""One-shot importer: tenants/<id>/config.json -> tenants + tenant_configs.

Phase 0 of PLATFORM_BLUEPRINT.md (Ticket 1). For every tenants/<id>/ directory
that has a config.json, inserts:
  - a `tenants` row (status live, plan managed) if one doesn't exist, and
  - a `tenant_configs` row (version 1, published=true) with the file's JSON,
    byte-compatible with what tenants._config_from_file parses.

Also inserts a `tenants` row for the 'default' (Orchelix) tenant WITHOUT a
config row — its config stays code-canonical in tools.py, and future tables
(calls, chat_sessions) need the FK target.

Idempotent: a tenant that already has ANY tenant_configs row is skipped, so a
re-run can never clobber a newer version written by the dashboard later.
Every config.json is validated through tenants._config_from_file BEFORE any
insert, and all writes happen in one transaction — a bad file aborts the whole
run with nothing written.

Run AFTER `alembic upgrade head`.

Usage:
    python scripts/import_tenant_configs.py            # import
    python scripts/import_tenant_configs.py --dry-run  # validate + report only

Requires DATABASE_URL. From your machine, use the Railway Postgres *public*
connection string (the private postgres.railway.internal host only resolves
inside Railway).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # repo root

from sqlalchemy import text  # noqa: E402

from platform_db import get_engine  # noqa: E402
from tenants import _config_from_file, _REGISTRY_DIR  # noqa: E402

_DEFAULT_ROW = {
    "id": "default",
    "company_name": "Orchelix AI Consulting",
    "business_tz": "America/New_York",
}


def collect() -> list[tuple[str, dict]]:
    """Return [(tenant_id, config_dict), ...] for every valid config.json."""
    found: list[tuple[str, dict]] = []
    for d in sorted(_REGISTRY_DIR.iterdir()):
        if not d.is_dir() or d.name == "default":
            continue
        cfg_path = d / "config.json"
        if not cfg_path.exists():
            print(f"  skip  {d.name}: no config.json")
            continue
        data = json.loads(cfg_path.read_text(encoding="utf-8"))
        # Validate: must parse into the exact TenantConfig shape the runtime uses.
        parsed = _config_from_file(d.name, data)
        print(
            f"  ok    {d.name}: {parsed.company_name!r}, tz={parsed.business_tz}, "
            f"{len(parsed.locations) or 1} location(s), {len(parsed.services)} service(s)"
        )
        found.append((d.name, data))
    return found


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="validate and report; write nothing")
    args = ap.parse_args()

    print(f"Scanning {_REGISTRY_DIR} ...")
    rows = collect()
    print(f"{len(rows)} tenant config(s) parsed OK.")
    if args.dry_run:
        print("Dry run — nothing written.")
        return 0

    engine = get_engine()
    if engine is None:
        print("ERROR: DATABASE_URL is not set.", file=sys.stderr)
        return 1

    imported, skipped = [], []
    with engine.begin() as conn:  # single transaction: all-or-nothing
        conn.execute(
            text(
                "INSERT INTO tenants (id, company_name, business_tz) "
                "VALUES (:id, :company_name, :business_tz) "
                "ON CONFLICT (id) DO NOTHING"
            ),
            _DEFAULT_ROW,
        )
        for tid, data in rows:
            conn.execute(
                text(
                    "INSERT INTO tenants (id, company_name, business_tz) "
                    "VALUES (:id, :company_name, :business_tz) "
                    "ON CONFLICT (id) DO NOTHING"
                ),
                {
                    "id": tid,
                    "company_name": data.get("company_name"),
                    "business_tz": data.get("business_tz"),
                },
            )
            exists = conn.execute(
                text("SELECT 1 FROM tenant_configs WHERE tenant_id = :tid LIMIT 1"),
                {"tid": tid},
            ).first()
            if exists:
                skipped.append(tid)
                continue
            conn.execute(
                text(
                    "INSERT INTO tenant_configs "
                    "(tenant_id, version, config, published, created_by) "
                    "VALUES (:tid, 1, :config, true, 'import_tenant_configs')"
                ),
                {"tid": tid, "config": json.dumps(data)},
            )
            # Read-back verification: jsonb does NOT preserve key order, and
            # parts of _config_from_file are order-sensitive (e.g. the legacy
            # calendar_id falls back to the FIRST location). Prove the stored
            # row parses to the exact same TenantConfig as the file, or abort
            # the whole transaction.
            stored = conn.execute(
                text("SELECT config FROM tenant_configs WHERE tenant_id = :tid AND version = 1"),
                {"tid": tid},
            ).scalar_one()
            if isinstance(stored, str):
                stored = json.loads(stored)
            if _config_from_file(tid, stored) != _config_from_file(tid, data):
                raise RuntimeError(
                    f"Tenant '{tid}': DB round-trip changed the parsed config "
                    f"(jsonb key-order sensitivity?) — aborting, nothing committed. "
                    f"Make the ambiguous field explicit in config.json (e.g. top-level "
                    f"calendar_id for multi-location tenants) and re-run."
                )
            imported.append(tid)

    print(f"Imported v1 for : {', '.join(imported) or '(none)'}")
    print(f"Skipped (has configs already): {', '.join(skipped) or '(none)'}")
    print("Done. Verify with: SELECT tenant_id, version, published FROM tenant_configs;")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
