"""
Jonex 平台 - API 路由模块
"""

from .internal import router as internal_router
from .knowledge_base import router as knowledge_base_router
from .knowledge_base import ingest_router as knowledge_base_ingest_router
from .tcadp import router as tcadp_router
from .auth import router as auth_router
from .platform import router as platform_router
from .business_domain import ecosystem_router

__all__ = [
    "internal_router",
    "knowledge_base_router",
    "knowledge_base_ingest_router",
    "tcadp_router",
    "auth_router",
    "platform_router",
    "ecosystem_router",
]
