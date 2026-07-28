# platform_db.py — control-plane database access (platform tables).
#
# The agent runtime's conversation checkpoints use langgraph's AsyncPostgresSaver
# (graph.py). THIS module is a separate, synchronous SQLAlchemy engine for the
# platform tables introduced by PLATFORM_BLUEPRINT.md (tenants, tenant_configs,
# calls, chat_sessions, ...). Same DATABASE_URL, different concern — keep them
# decoupled so a platform-table problem can never take down checkpointing.
#
# Zero-risk contract: nothing here raises at import time, and every caller must
# treat the DB as optional — get_engine() returns None when DATABASE_URL is not
# set, and any operational error is the caller's cue to fall back to the
# file-based path (see tenants.load_tenant).

from __future__ import annotations

import os
import threading
from typing import Optional

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine

_engine: Optional[Engine] = None
_lock = threading.Lock()


def database_url() -> Optional[str]:
    """DATABASE_URL normalized for SQLAlchemy + psycopg (v3).

    Railway hands out postgres:// / postgresql:// URLs. Bare postgresql://
    makes SQLAlchemy pick the psycopg2 driver, which is not installed — force
    the psycopg (v3) driver that already ships for the langgraph checkpointer.
    """
    raw = os.getenv("DATABASE_URL")
    if not raw:
        return None
    for prefix in ("postgres://", "postgresql://"):
        if raw.startswith(prefix):
            return "postgresql+psycopg://" + raw[len(prefix):]
    return raw


def get_engine() -> Optional[Engine]:
    """Lazily-created process-wide engine, or None when DATABASE_URL is unset.

    Small pool: the runtime only reads tenant configs through this (one short
    query per tenant per 60s); the checkpointer has its own pool.
    """
    global _engine
    if _engine is not None:
        return _engine
    url = database_url()
    if not url:
        return None
    with _lock:
        if _engine is None:
            _engine = create_engine(
                url,
                pool_size=2,
                max_overflow=3,
                pool_pre_ping=True,
                pool_recycle=300,
                connect_args={"connect_timeout": 5},
            )
    return _engine
