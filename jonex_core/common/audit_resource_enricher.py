"""审计日志资源名称富化器

根据 resource_type 和 resource_id 从对应 DB 表查询资源实例的显示名称，
为审计日志列表提供 resources 字段的富化（resource_name）。

使用方式:
    from jonex_core.common.audit_resource_enricher import ResourceNameEnricher

    enricher = ResourceNameEnricher(session)
    await enricher.enrich_batch(items)  # 原地设置 item.resource_name
"""

from typing import Any, Dict, List, Optional

from sqlalchemy import text as sa_text
from sqlalchemy.ext.asyncio import AsyncSession

from jonex_core.common.audit_enums import _RESOURCE_NAME_LOOKUP


class ResourceNameEnricher:
    """根据 resource_type 和 resource_id 从 DB 查询资源显示名称。

    使用 _RESOURCE_NAME_LOOKUP 字典定位 (schema, table, id_column, name_column)，
    执行 SELECT name_column FROM schema.table WHERE id_column = resource_id 查询。
    支持单条 (enrich) 和批量 (enrich_batch) 两种查询模式。
    """

    def __init__(self, session: AsyncSession):
        self.session = session

    async def enrich(
        self, resource_type: Optional[str], resource_id: Optional[str]
    ) -> Optional[str]:
        """查询单个资源的显示名称。

        Args:
            resource_type: 资源类型（如 "space", "document"）
            resource_id: 资源 ID

        Returns:
            显示名称，无映射或未找到时返回 None
        """
        if not resource_type or not resource_id:
            return None

        lookup = _RESOURCE_NAME_LOOKUP.get(resource_type)
        if not lookup:
            return None

        schema, table, id_col, name_col = lookup
        # ⚠️ resource_id 在 audit_logs 里是 VARCHAR，而部分资源表主键是 BIGINT
        # （platform.*）。直接把字符串绑到 BIGINT 列上，asyncpg 会拒绝
        # （invalid input for query argument: expected int, got str）并让整个
        # 审计列表 500。统一按文本比较，两种主键类型都成立；代价是放弃 id 索引，
        # 但富化只针对单页 ≤100 条记录，可接受。
        query = sa_text(
            f"SELECT {name_col} FROM {schema}.{table} "
            f"WHERE CAST({id_col} AS TEXT) = :rid"
        )
        result = await self.session.execute(query, {"rid": str(resource_id)})
        row = result.scalar_one_or_none()
        if row is not None:
            return str(row)
        return None

    async def enrich_batch(self, items: List[Any]) -> None:
        """批量查询资源显示名称并原地设置 item.resource_name。

        Args:
            items: 包含 resource / resource_id 属性的对象列表，
                   每个对象会被原地设置 resource_name 属性。
                   资源类型不匹配或查询不到时不设置（保持 None）。
        """
        if not items:
            return

        # 按 resource_type 分组批量查询
        groups: Dict[str, List[Any]] = {}
        for item in items:
            rt = getattr(item, "resource", None)
            rid = getattr(item, "resource_id", None)
            if rt and rid:
                groups.setdefault(rt, []).append(item)

        for resource_type, group in groups.items():
            lookup = _RESOURCE_NAME_LOOKUP.get(resource_type)
            if not lookup:
                continue

            schema, table, id_col, name_col = lookup

            # 收集唯一 resource_id 集合（统一转文本，见 enrich() 的类型说明）
            resource_ids = list(
                {str(getattr(it, "resource_id")) for it in group}
            )
            if not resource_ids:
                continue

            # 构建批量 IN 查询
            placeholders = ", ".join(f":rid_{i}" for i in range(len(resource_ids)))
            params = {f"rid_{i}": rid for i, rid in enumerate(resource_ids)}
            in_query = sa_text(
                f"SELECT {id_col}, {name_col} FROM {schema}.{table} "
                f"WHERE CAST({id_col} AS TEXT) IN ({placeholders})"
            )
            result = await self.session.execute(in_query, params)
            # 名称为 NULL（如用户未设置 display_name）时不要写成字符串 "None"
            name_map = {
                str(row[0]): str(row[1])
                for row in result
                if row[1] is not None
            }

            # 原地设置 resource_name
            for item in group:
                rid = getattr(item, "resource_id", None)
                if rid is not None and str(rid) in name_map:
                    item.resource_name = name_map[str(rid)]
