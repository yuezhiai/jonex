"""
CompiledSchemaClient — atomic-rag 侧读取 compiled schema 的 HTTP+Redis 客户端。

职责（只读，不直连业务模板表 / PG）：
1. Redis 优先读取 compiled schema
2. Miss 时调用 knowledge-base 的 `/invoke`（`get_compiled_schema_for_extraction`）
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
        # 地址解析链（宿主态 / 容器态都正确，且无需两处各配一个变量名）：
        #
        #   1. `KNOWLEDGE_BASE_API_URL` —— 显式覆盖（可选，历史上存在此名）
        #   2. `get_config().KNOWLEDGE_BASE_URL` —— 与 Sidecar 代理 kb `/invoke`
        #      用的是**同一个配置**（`jonex_core/sidecar/proxy.py` 的静态端点表），
        #      语义完全一致：都是「kb 的 `/invoke` 基址」。
        #
        # 为什么必须复用配置而不是写死字面量：
        #   - 容器态：`docker-compose.yml` 给 atomic-rag / sidecar 都注入
        #     `KNOWLEDGE_BASE_URL=http://knowledge-base-service:8000`（服务名+容器端口）
        #   - 宿主态（VSCode Debug，本机跑 atomic-rag）：Docker 服务名**在宿主上无法解析**
        #     （实测 `knowledge-base-service` / `knowledge-base` 均 gaierror），
        #     必须走 `http://localhost:8003` —— 即 config 默认值 / `.env.local`
        #
        # ⚠️ 不要写 `:8003` 作为容器内地址：`8003` 只是**宿主映射**端口，
        #    容器内监听的是 `8000`；且该映射在生产已随 P0 端口收敛移除。
        #    写错会 ConnectionRefused → 静默回落 default.yaml（抽取质量下降、不报错）。
        from jonex_core.common.config import get_config

        self._knowledge_base_api_url = (
            os.getenv("KNOWLEDGE_BASE_API_URL") or get_config().KNOWLEDGE_BASE_URL
        )
        # 注：原先此处还持有 `KNOWLEDGE_BASE_API_KEY or GATEWAY_API_KEY` 作为
        # 容器 REST 层的 Bearer 凭据。改走 `/invoke` 后凭据统一为内部服务 JWT
        # （见 `_get_from_api`），该字段已无用途，一并移除。
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
        """经 knowledge-base 的 `/invoke` 取 compiled schema（含 KB 级同义词注入）。

        为什么走 `/invoke` 而不是容器 REST 层（原实现打的是
        `GET /api/v1/knowledge-base/ontology/compiled-schema`）：

        - 容器 REST 层是**并行调试面**，历史上**无鉴权**且按客户端 `X-Tenant-ID`
          决定租户 —— 容器端口一旦可达即为跨租户读取面（2026-10 渗透测试 3.2）。
          该层**实现已物理删除**（2026-10 整改，见
          `capabilities/knowledge_base/capability.py` 的 `register_routes`）。
        - `/invoke` 强制内部服务 JWT，且租户取自请求体（本客户端按调用方身份声明），
          是生产链路既有通道。

        语义上与旧 REST 路径**一致**：`get_compiled_schema_for_extraction` 会注入
        KB 级同义词（旧 REST 路径的专属行为，`get_compiled_schema` 有意不注入）。
        """
        import httpx

        from jonex_core.security import get_internal_auth

        url = f"{self._knowledge_base_api_url.rstrip('/')}/invoke"
        headers = {
            # 内部服务 JWT（/invoke 的 verify_internal_service 依赖此头）
            "Authorization": f"Bearer {get_internal_auth().generate_token('atomic-rag')}",
        }
        body = {
            "capability_id": "business.knowledge_base.v1",
            "tenant_id": tenant_id,
            "payload": {
                "action": "get_compiled_schema_for_extraction",
                "data": {"knowledge_base_id": knowledge_base_id},
            },
        }
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(url, json=body, headers=headers)
                if resp.status_code != 200:
                    logger.warning(
                        "Compiled schema invoke returned %s for KB %s",
                        resp.status_code, knowledge_base_id,
                    )
                    return None
                data = resp.json()
                if not data.get("success"):
                    # KB 无 compiled schema（或本体门禁拒绝）→ 交由上层走 default.yaml
                    logger.info(
                        "Compiled schema unavailable for KB %s: code=%s",
                        knowledge_base_id, data.get("code"),
                    )
                    return None
                return data.get("data") or None
        except Exception as e:
            logger.warning("Compiled schema invoke call failed: %s", e)
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