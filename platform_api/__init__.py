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

from platform_api.admin import router as admin_router
from platform_api.analytics import router as analytics_router
from platform_api.appointments import router as appointments_router
from platform_api.billing import router as billing_router
from platform_api.calls import router as calls_router
from platform_api.chats import router as chats_router
from platform_api.config import router as config_router
from platform_api.knowledge import router as knowledge_router
from platform_api.leads import router as leads_router
from platform_api.onboarding import router as onboarding_router
from platform_api.overview import router as overview_router
from platform_api.public_voice_preview import router as public_voice_preview_router
from platform_api.quality_studio import router as quality_studio_router
from platform_api.scheduling import router as scheduling_router
from platform_api.signup import router as signup_router
from platform_api.tenant_status import router as tenant_status_router
from platform_api.usage import router as usage_router
from platform_api.vapi_webhook import router as vapi_webhook_router
from platform_api.voice_preview import router as voice_preview_router
from platform_api.voice_sync import router as voice_sync_router

__all__ = [
    "admin_router",
    "analytics_router",
    "appointments_router",
    "billing_router",
    "calls_router",
    "chats_router",
    "config_router",
    "knowledge_router",
    "leads_router",
    "onboarding_router",
    "overview_router",
    "public_voice_preview_router",
    "quality_studio_router",
    "scheduling_router",
    "signup_router",
    "tenant_status_router",
    "usage_router",
    "vapi_webhook_router",
    "voice_preview_router",
    "voice_sync_router",
]
