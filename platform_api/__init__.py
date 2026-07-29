# platform_api/ — control-plane routers (PLATFORM_BLUEPRINT.md, Phase 0+).
#
# Named platform_api (not the blueprint's "platform/") because a top-level
# package called `platform` would shadow Python's stdlib `platform` module —
# imported by uvicorn, googleapiclient and friends — since the repo root is on
# sys.path. Matches platform_db.py.
#
# Everything in this package is control-plane: it reads/writes the platform
# tables and must never be imported by the agent runtime path (tools.py,
# agents.py, graph.py). api.py mounts the routers; that is the only coupling.

from platform_api.appointments import router as appointments_router
from platform_api.calls import router as calls_router
from platform_api.config import router as config_router
from platform_api.knowledge import router as knowledge_router
from platform_api.leads import router as leads_router
from platform_api.overview import router as overview_router
from platform_api.vapi_webhook import router as vapi_webhook_router

__all__ = [
    "appointments_router",
    "calls_router",
    "config_router",
    "knowledge_router",
    "leads_router",
    "overview_router",
    "vapi_webhook_router",
]
