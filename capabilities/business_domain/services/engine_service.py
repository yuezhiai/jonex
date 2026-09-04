"""
业务领域 — 引擎管理服务（数据接入 + 解析器 + 模型）
"""
import uuid

from jonex_core.common import get_db_session
from jonex_core.common.i18n import translate

from capabilities.business_domain.models import DataAccessMethod, ParserConfig
from capabilities.business_domain.repository import (
    DataAccessMethodRepository,
    ParserConfigRepository,
    ModelProviderRepository,
)


class EngineService:
    """引擎管理：数据接入 + 解析器 + 模型"""

    # Data Access Methods（平台共享：平台支持的数据接入方式目录，全体租户可见）
    async def list_access_methods(self, offset: int = 0, limit: int = 20) -> dict:
        async with get_db_session() as session:
            repo = DataAccessMethodRepository(session)
            active_cond = [DataAccessMethod.status == "active"]
            items = await repo.list_all_shared(offset, limit, extra_conditions=active_cond)
            total = await repo.count_shared(extra_conditions=active_cond)
            return {"items": [o.to_dict() for o in items], "total": total, "offset": offset, "limit": limit}

    async def create_access_method(self, data: dict) -> dict:
        async with get_db_session() as session:
            repo = DataAccessMethodRepository(session)
            obj = await repo.create(
                id=uuid.uuid4().hex,
                name=data["name"], access_type=data["access_type"],
                config_json=data.get("config_json", {}),
            )
            await session.commit()
            return obj.to_dict()

    async def update_access_method(self, method_id: str, data: dict) -> dict:
        async with get_db_session() as session:
            repo = DataAccessMethodRepository(session)
            obj = await repo.update_shared(method_id, **{
                k: v for k, v in data.items()
                if k in ("name", "config_json", "status") and v is not None
            })
            if obj is None:
                obj = await repo.get_required_shared(method_id)
            await session.commit()
            return obj.to_dict()

    # Parser Configs（平台共享：全体租户可读，仅平台 admin 可写）
    async def list_parsers(self, offset: int = 0, limit: int = 20) -> dict:
        async with get_db_session() as session:
            repo = ParserConfigRepository(session)
            active_cond = [ParserConfig.status == "active"]
            items = await repo.list_all_shared(offset, limit, extra_conditions=active_cond)
            total = await repo.count_shared(extra_conditions=active_cond)
            return {"items": [o.to_dict() for o in items], "total": total, "offset": offset, "limit": limit}

    async def create_parser(self, data: dict) -> dict:
        async with get_db_session() as session:
            repo = ParserConfigRepository(session)
            obj = await repo.create(
                id=uuid.uuid4().hex,
                name=data["name"], parser_type=data["parser_type"],
                file_types=data.get("file_types", []),
                config_json=data.get("config_json", {}),
            )
            await session.commit()
            return obj.to_dict()

    async def update_parser(self, parser_id: str, data: dict) -> dict:
        async with get_db_session() as session:
            repo = ParserConfigRepository(session)
            obj = await repo.update_shared(parser_id, **{
                k: v for k, v in data.items()
                if k in ("name", "file_types", "config_json", "status") and v is not None
            })
            if obj is None:
                obj = await repo.get_required_shared(parser_id)
            await session.commit()
            return obj.to_dict()

    # Model Providers（平台共享：全体租户可读，仅平台 admin 可写）
    async def list_providers(self, offset: int = 0, limit: int = 20) -> dict:
        async with get_db_session() as session:
            repo = ModelProviderRepository(session)
            items = await repo.list_all_shared(offset, limit)
            total = await repo.count_shared()
            return {"items": [o.to_dict() for o in items], "total": total, "offset": offset, "limit": limit}

    async def create_provider(self, data: dict) -> dict:
        async with get_db_session() as session:
            repo = ModelProviderRepository(session)
            obj = await repo.create(
                id=uuid.uuid4().hex,
                name=data["name"], provider_type=data["provider_type"],
                model_type=data.get("model_type"), endpoint=data.get("endpoint"),
                api_key_encrypted=data.get("api_key"), model_name=data.get("model_name"),
                config_json=data.get("config_json", {}),
            )
            await session.commit()
            return obj.to_dict()

    async def update_provider(self, provider_id: str, data: dict) -> dict:
        async with get_db_session() as session:
            repo = ModelProviderRepository(session)
            updatable = {"name", "endpoint", "api_key_encrypted", "model_name", "config_json", "status"}
            obj = await repo.update_shared(provider_id, **{
                k: v for k, v in data.items() if k in updatable and v is not None
            })
            if obj is None:
                obj = await repo.get_required_shared(provider_id)
            await session.commit()
            return obj.to_dict()

    async def test_provider(self, provider_id: str) -> dict:
        async with get_db_session() as session:
            repo = ModelProviderRepository(session)
            await repo.get_required_shared(provider_id)
        return {
            "success": True,
            "message": translate(
                "success.model.connection_mock",
                fallback="连接测试通过（模拟）",
            ),
        }
