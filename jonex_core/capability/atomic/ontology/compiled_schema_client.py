"""
CompiledSchemaClient — atomic-rag 侧读取 compiled schema 的 HTTP+Redis 客户端。

职责（只读，不直连业务模板表 / PG）：
1. Redis 优先读取 compiled schema
2. Miss 时调用 knowledge-base HTTP API
3. 失败时 fallback 到 default.yaml
"""
import json
import logging
import os
from typing import Any, Optional

from jonex_core.common.cache import CacheUtil

logger = logging.getLogger(__name__)

ONTOLOGY_SCHEMA_FALLBACK_PATH = os.getenv(
    "ONTOLOGY_SCHEMA_FALLBACK_PATH",
    "deploy/config/ontology/default.yaml",
)

ONTOLOGY_COMPILED_SCHEMA_TTL = int(os.getenv("ONTOLOGY_COMPILED_SCHEMA_TTL", "3600"))


def _compiled_key(tenant_id: str, knowledge_base_id: str) -> str:
    return f"ontology:compiled:{tenant_id}:{knowledge_base_id}"


class CompiledSchemaClient:
    """atomic-rag 侧 compiled schema 客户端（只读）。"""

    def __init__(self):
        self._knowledge_base_api_url = os.getenv(
            "KNOWLEDGE_BASE_API_URL",
            "http://knowledge-base:8003",
        )
        from jonex_core.common.config import get_config

        self._api_key = os.getenv("KNOWLEDGE_BASE_API_KEY") or get_config().GATEWAY_API_KEY
        self._fallback_schema: Optional[dict] = None

    async def get_schema(
        self,
        tenant_id: str,
        knowledge_base_id: str,
    ) -> Optional[dict]:
        """按 tenant_id + knowledge_base_id 获取 compiled schema。

        策略：Redis -> HTTP API -> default.yaml fallback
        """
        # 1. Redis
        schema = await self._get_from_redis(tenant_id, knowledge_base_id)
        if schema is not None:
            return schema

        # 2. HTTP API
        schema = await self._get_from_api(tenant_id, knowledge_base_id)
        if schema is not None:
            await self._set_redis(tenant_id, knowledge_base_id, schema)
            return schema

        # 3. default.yaml fallback
        return await self._get_fallback()

    async def _get_from_redis(self, tenant_id: str, knowledge_base_id: str) -> Optional[dict]:
        """Redis 读取。"""
        try:
            key = _compiled_key(tenant_id, knowledge_base_id)
            raw = await CacheUtil.get(key)
            if raw:
                return json.loads(raw)
        except Exception as e:
            logger.warning("Compiled schema Redis read failed: %s", e)
        return None

    async def _set_redis(self, tenant_id: str, knowledge_base_id: str, data: dict) -> None:
        """Redis 写入。"""
        try:
            key = _compiled_key(tenant_id, knowledge_base_id)
            raw = json.dumps(data, ensure_ascii=False, default=str)
            await CacheUtil.set(key, raw, expire=ONTOLOGY_COMPILED_SCHEMA_TTL)
        except Exception as e:
            logger.warning("Compiled schema Redis write failed: %s", e)

    async def _get_from_api(self, tenant_id: str, knowledge_base_id: str) -> Optional[dict]:
        """HTTP 调用 knowledge-base API。"""
        import httpx

        url = (
            f"{self._knowledge_base_api_url.rstrip('/')}"
            f"/api/v1/knowledge-base/ontology/compiled-schema"
            f"?knowledge_base_id={knowledge_base_id}"
        )
        headers = {
            "X-Tenant-ID": tenant_id,
            "Authorization": f"Bearer {self._api_key}",
        }
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(url, headers=headers)
                if resp.status_code == 200:
                    data = resp.json()
                    payload = data.get("data")
                    if payload:
                        return payload
                elif resp.status_code == 404:
                    logger.info("Compiled schema not found for KB %s", knowledge_base_id)
                    return None
                else:
                    logger.warning(
                        "Compiled schema API returned %s for KB %s",
                        resp.status_code, knowledge_base_id,
                    )
        except Exception as e:
            logger.warning("Compiled schema API call failed: %s", e)
        return None

    async def _get_fallback(self) -> Optional[dict]:
        """default.yaml fallback。"""
        if self._fallback_schema:
            return self._fallback_schema

        try:
            from jonex_core.capability.atomic.ontology.registry import OntologyRegistry

            registry = OntologyRegistry()
            schema = registry.load(ONTOLOGY_SCHEMA_FALLBACK_PATH)
            prompt = json.loads(registry.to_prompt_json(schema.domain))
            self._fallback_schema = {
                "source_type": "yaml_default",
                "entity_types": [
                    {
                        "name": et.name,
                        "display_name": et.name,
                        "aliases": et.aliases,
                        "attributes": [
                            {
                                "name": a.name,
                                "display_name": a.name,
                                "type": a.type,
                                "required": a.required,
                            }
                            for a in et.attributes
                        ],
                    }
                    for et in schema.entity_types
                ],
                "relation_types": [
                    {
                        "name": rt.name,
                        "display_name": rt.name,
                        "source": rt.source,
                        "target": rt.target,
                    }
                    for rt in schema.relation_types
                ],
                "disambiguation": {"case_insensitive": True, "alias_merge": True},
                "prompt_schema": prompt,
            }
            logger.info("Loaded fallback schema from %s", ONTOLOGY_SCHEMA_FALLBACK_PATH)
        except Exception as e:
            logger.warning("Fallback schema load failed: %s", e)
            self._fallback_schema = {
                "source_type": "yaml_default",
                "entity_types": [],
                "relation_types": [],
                "disambiguation": {"case_insensitive": True, "alias_merge": True},
                "prompt_schema": {"entity_types": [], "relation_types": []},
            }

        return self._fallback_schema


__all__ = ["CompiledSchemaClient"]