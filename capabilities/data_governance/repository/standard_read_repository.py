"""标准目录只读 SQL：business_domain 模板 + knowledge_base 应用。

跨 schema 直读现有表，**不 import `capabilities.business_domain` 的模型**，
避免 data_governance 反向依赖业务域能力；查询结果统一按 dict 返回。
所有用户值只进绑定参数（`keyword` 的 `%` 包裹在 Python 侧完成）。
"""
from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


def escape_like(value: str) -> str:
    """转义 ILIKE 字面量里的通配符（反斜杠必须先转，否则会二次转义）。"""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


#: 影响分析单次最多返回的 KB 数；SQL LIMIT 与服务截断共用这一个来源，
#: 避免两侧漂移（count 仍返回真实总数，truncated 由服务判定）。
REFERENCE_LIMIT = 200


def _visibility_clause(column: str, visible_kb_ids: list[str] | None) -> str:
    """`None` 表示不过滤（租户管理员）；`[]` 仍生成 ANY（service 负责短路）。"""
    return f"AND {column} = ANY(:visible_kb_ids)" if visible_kb_ids is not None else ""


# ---------------------------------------------------------------------------
# 模板标准：领域 / 场景 / 对象 / 属性 / 关系 / 约束
# ---------------------------------------------------------------------------


async def list_domains(session: AsyncSession, tenant_id: str) -> list[dict]:
    sql = text("""
        SELECT d.id, d.name, d.name_en, d.description, d.status, d.version,
               d.published_at,
               COUNT(DISTINCT s.id) AS scenario_count,
               COUNT(DISTINCT o.id) AS object_count,
               MAX(s.published_at) AS latest_published_at
          FROM business_domain.template_domains d
          LEFT JOIN business_domain.template_scenarios s
            ON s.tenant_id = d.tenant_id AND s.domain_id = d.id AND s.is_deleted = 0
          LEFT JOIN business_domain.template_objects o
            ON o.tenant_id = d.tenant_id AND o.scenario_id = s.id AND o.is_deleted = 0
         WHERE d.tenant_id = :tenant_id AND d.is_deleted = 0
         GROUP BY d.id, d.name, d.name_en, d.description, d.status, d.version, d.published_at
         ORDER BY d.name ASC, d.id ASC
    """)
    rows = (await session.execute(sql, {"tenant_id": tenant_id})).mappings().all()
    return [dict(row) for row in rows]


async def get_domain(
    session: AsyncSession, tenant_id: str, domain_id: str,
) -> dict | None:
    sql = text("""
        SELECT id, name, name_en, description, status, version, published_at, structure_hash
          FROM business_domain.template_domains
         WHERE tenant_id = :tenant_id AND id = :domain_id AND is_deleted = 0
    """)
    row = (
        await session.execute(
            sql, {"tenant_id": tenant_id, "domain_id": domain_id},
        )
    ).mappings().first()
    return dict(row) if row else None


async def list_scenarios(
    session: AsyncSession, tenant_id: str, domain_id: str,
) -> list[dict]:
    # 四个子表先各自按场景预聚合再 join：避免 objects × attributes ×
    # relations × constraints 的多表笛卡尔放大。
    sql = text("""
        SELECT s.id, s.name, s.description, s.version, s.published_at, s.structure_hash,
               COALESCE(o.object_count, 0) AS object_count,
               COALESCE(a.attribute_count, 0) AS attribute_count,
               COALESCE(r.relation_count, 0) AS relation_count,
               COALESCE(c.constraint_count, 0) AS constraint_count
          FROM business_domain.template_scenarios s
          LEFT JOIN (
              SELECT scenario_id, COUNT(*) AS object_count
                FROM business_domain.template_objects
               WHERE tenant_id = :tenant_id AND is_deleted = 0
               GROUP BY scenario_id
          ) o ON o.scenario_id = s.id
          LEFT JOIN (
              SELECT o.scenario_id, COUNT(*) AS attribute_count
                FROM business_domain.template_attributes a
                JOIN business_domain.template_objects o
                  ON o.tenant_id = a.tenant_id AND o.id = a.template_object_id
                 AND o.is_deleted = 0
               WHERE a.tenant_id = :tenant_id AND a.is_deleted = 0
               GROUP BY o.scenario_id
          ) a ON a.scenario_id = s.id
          LEFT JOIN (
              SELECT scenario_id, COUNT(*) AS relation_count
                FROM business_domain.template_relations
               WHERE tenant_id = :tenant_id AND is_deleted = 0
               GROUP BY scenario_id
          ) r ON r.scenario_id = s.id
          LEFT JOIN (
              SELECT scenario_id, COUNT(*) AS constraint_count
                FROM business_domain.template_constraints
               WHERE tenant_id = :tenant_id AND is_deleted = 0
               GROUP BY scenario_id
          ) c ON c.scenario_id = s.id
         WHERE s.tenant_id = :tenant_id AND s.domain_id = :domain_id AND s.is_deleted = 0
         ORDER BY s.published_at DESC NULLS LAST, s.name ASC, s.id ASC
    """)
    rows = (
        await session.execute(
            sql, {"tenant_id": tenant_id, "domain_id": domain_id},
        )
    ).mappings().all()
    return [dict(row) for row in rows]


async def get_scenario(
    session: AsyncSession, tenant_id: str, scenario_id: str,
) -> dict | None:
    sql = text("""
        SELECT s.id, s.domain_id, s.name, s.description, s.version,
               s.published_at, s.structure_hash
          FROM business_domain.template_scenarios s
          JOIN business_domain.template_domains d
            ON d.tenant_id = s.tenant_id AND d.id = s.domain_id AND d.is_deleted = 0
         WHERE s.tenant_id = :tenant_id AND s.id = :scenario_id AND s.is_deleted = 0
    """)
    row = (
        await session.execute(
            sql, {"tenant_id": tenant_id, "scenario_id": scenario_id},
        )
    ).mappings().first()
    return dict(row) if row else None


async def list_objects(
    session: AsyncSession, tenant_id: str, scenario_id: str,
) -> list[dict]:
    sql = text("""
        SELECT id, name, description, status, ontology_code, aliases
          FROM business_domain.template_objects
         WHERE tenant_id = :tenant_id AND scenario_id = :scenario_id AND is_deleted = 0
         ORDER BY name ASC, id ASC
    """)
    rows = (
        await session.execute(
            sql, {"tenant_id": tenant_id, "scenario_id": scenario_id},
        )
    ).mappings().all()
    return [dict(row) for row in rows]


async def list_attributes(
    session: AsyncSession, tenant_id: str, scenario_id: str,
) -> list[dict]:
    sql = text("""
        SELECT a.id, a.template_object_id, a.attr_name, a.description, a.attr_type,
               a.is_primary_key, a.is_required, a.ontology_code, a.constraints_json,
               a.sort_order
          FROM business_domain.template_attributes a
          JOIN business_domain.template_objects o
            ON o.tenant_id = a.tenant_id AND o.id = a.template_object_id AND o.is_deleted = 0
         WHERE a.tenant_id = :tenant_id AND o.scenario_id = :scenario_id
           AND a.is_deleted = 0
         ORDER BY a.sort_order ASC, a.attr_name ASC, a.id ASC
    """)
    rows = (
        await session.execute(
            sql, {"tenant_id": tenant_id, "scenario_id": scenario_id},
        )
    ).mappings().all()
    return [dict(row) for row in rows]


async def list_relations(
    session: AsyncSession, tenant_id: str, scenario_id: str,
) -> list[dict]:
    sql = text("""
        SELECT id, name, description, relation_type, status,
               source_object_id, target_object_id, ontology_code
          FROM business_domain.template_relations
         WHERE tenant_id = :tenant_id AND scenario_id = :scenario_id AND is_deleted = 0
         ORDER BY name ASC, id ASC
    """)
    rows = (
        await session.execute(
            sql, {"tenant_id": tenant_id, "scenario_id": scenario_id},
        )
    ).mappings().all()
    return [dict(row) for row in rows]


async def list_constraints(
    session: AsyncSession, tenant_id: str, scenario_id: str,
) -> list[dict]:
    sql = text("""
        SELECT id, name, target_type, target_id, target_label,
               constraint_type, expression, suggestion
          FROM business_domain.template_constraints
         WHERE tenant_id = :tenant_id AND scenario_id = :scenario_id AND is_deleted = 0
         ORDER BY name ASC, id ASC
    """)
    rows = (
        await session.execute(
            sql, {"tenant_id": tenant_id, "scenario_id": scenario_id},
        )
    ).mappings().all()
    return [dict(row) for row in rows]


# ---------------------------------------------------------------------------
# 知识库应用：compiled schema 列表与计数
# ---------------------------------------------------------------------------


def _kb_schema_base(visible_kb_ids: list[str] | None) -> str:
    """列表与计数共用的 FROM/WHERE（保证两条路径的过滤条件一致）。"""
    return f"""
        FROM knowledge_base.ontology_compiled_schemas cs
        -- INNER JOIN：孤儿 compiled schema（KB 已删）不进入知识库应用，
        -- 与资产目录「已删 KB 不可见」同口径。
        JOIN knowledge_base.knowledge_info ki
          ON ki.id = cs.knowledge_base_id AND ki.tenant_id = cs.tenant_id
         AND ki.is_deleted = 0
        LEFT JOIN knowledge_base.ontology_template_bindings b
          ON b.tenant_id = cs.tenant_id AND b.knowledge_base_id = cs.knowledge_base_id
        LEFT JOIN business_domain.template_scenarios s
          ON s.tenant_id = cs.tenant_id
         AND s.id = COALESCE(cs.template_scenario_id, b.template_scenario_id)
         AND s.is_deleted = 0
        LEFT JOIN business_domain.template_domains d
          ON d.tenant_id = cs.tenant_id
         AND d.id = COALESCE(s.domain_id, cs.template_domain_id, b.template_domain_id)
         AND d.is_deleted = 0
        WHERE cs.tenant_id = :tenant_id
          {_visibility_clause('cs.knowledge_base_id', visible_kb_ids)}
    """


def _kb_schema_select() -> str:
    return """
        SELECT cs.knowledge_base_id AS kb_id,
               ki.name AS kb_name, ki.space_id,
               COALESCE(cs.template_scenario_id, b.template_scenario_id) AS scenario_id,
               s.name AS scenario_name,
               COALESCE(s.domain_id, cs.template_domain_id, b.template_domain_id) AS domain_id,
               d.name AS domain_name,
               b.template_scenario_id AS binding_scenario_id,
               cs.template_scenario_id AS compiled_scenario_id,
               b.status AS binding_status,
               COALESCE(b.source_type, cs.source_type) AS source_type,
               cs.source_version, cs.source_hash, cs.schema_version,
               cs.schema_mode, cs.sync_status, cs.compiled_at, cs.updated_at
    """


def _kb_schema_filters(
    *,
    kb_id: str | None,
    keyword: str | None,
    params: dict,
) -> list[str]:
    filters: list[str] = []
    if kb_id:
        filters.append("AND cs.knowledge_base_id = :kb_id")
        params["kb_id"] = kb_id
    if keyword:
        # ESCAPE '\' 让用户输入的 % / _ 按字面量匹配，而不是通配符。
        filters.append("AND ki.name ILIKE :keyword ESCAPE '\\'")
        params["keyword"] = f"%{escape_like(keyword)}%"
    return filters


async def list_kb_schemas(
    session: AsyncSession, tenant_id: str, *,
    visible_kb_ids: list[str] | None,
    kb_id: str | None = None,
    keyword: str | None = None,
    limit: int = 20, offset: int = 0,
) -> list[dict]:
    params: dict = {"tenant_id": tenant_id, "limit": limit, "offset": offset}
    if visible_kb_ids is not None:
        params["visible_kb_ids"] = list(visible_kb_ids)
    filters = _kb_schema_filters(kb_id=kb_id, keyword=keyword, params=params)
    sql = text(
        _kb_schema_select()
        + _kb_schema_base(visible_kb_ids)
        + "\n" + "\n".join(filters)
        + "\nORDER BY cs.updated_at DESC NULLS LAST, ki.name ASC, cs.knowledge_base_id ASC"
        + "\nLIMIT :limit OFFSET :offset"
    )
    rows = (await session.execute(sql, params)).mappings().all()
    return [dict(row) for row in rows]


async def count_kb_schemas(
    session: AsyncSession, tenant_id: str, *,
    visible_kb_ids: list[str] | None,
    kb_id: str | None = None,
    keyword: str | None = None,
) -> int:
    """与列表**同一段 WHERE** 的计数（共用 `_kb_schema_base` 与过滤器）。"""
    params: dict = {"tenant_id": tenant_id}
    if visible_kb_ids is not None:
        params["visible_kb_ids"] = list(visible_kb_ids)
    filters = _kb_schema_filters(kb_id=kb_id, keyword=keyword, params=params)
    sql = text(
        "SELECT COUNT(*) AS total"
        + _kb_schema_base(visible_kb_ids)
        + "\n" + "\n".join(filters)
    )
    row = (await session.execute(sql, params)).mappings().first()
    return int((row or {}).get("total") or 0)


# ---------------------------------------------------------------------------
# 影响分析：场景绑定 / compiled schema JSONB 反查
# ---------------------------------------------------------------------------


def _reference_select() -> str:
    return """
        SELECT cs.knowledge_base_id AS kb_id,
               ki.name AS kb_name, ki.space_id,
               b.status AS binding_status,
               COALESCE(b.source_type, cs.source_type) AS source_type,
               b.template_scenario_id AS binding_scenario_id,
               cs.template_scenario_id AS compiled_scenario_id,
               cs.source_version, cs.schema_version,
               cs.sync_status, cs.schema_mode
    """


def _reference_from(visible_kb_ids: list[str] | None) -> str:
    return f"""
        FROM knowledge_base.ontology_compiled_schemas cs
        JOIN knowledge_base.knowledge_info ki
          ON ki.id = cs.knowledge_base_id AND ki.tenant_id = cs.tenant_id
         AND ki.is_deleted = 0
        -- inactive 绑定不作为生效绑定：只 join active，否则对象/属性反查
        -- 会把已停用绑定误当生效绑定并透出它的 source_type。
        LEFT JOIN knowledge_base.ontology_template_bindings b
          ON b.tenant_id = cs.tenant_id AND b.knowledge_base_id = cs.knowledge_base_id
         AND b.status = 'active'
        WHERE cs.tenant_id = :tenant_id
          {_visibility_clause('cs.knowledge_base_id', visible_kb_ids)}
    """


def _scope_exists_clause(*, nested: bool) -> str:
    """对象/属性 JSONB 反查片段：列表与计数共用，避免两侧漂移。"""
    if nested:
        return """
          AND EXISTS (
              SELECT 1
                FROM jsonb_array_elements(COALESCE(cs.entity_types, '[]'::jsonb)) AS e,
                     jsonb_array_elements(COALESCE(e->'attributes', '[]'::jsonb)) AS a
               WHERE a->>'source_attribute_id' = :scope_id
          )
        """
    return """
          AND EXISTS (
              SELECT 1
                FROM jsonb_array_elements(COALESCE(cs.entity_types, '[]'::jsonb)) AS e
               WHERE e->>'source_object_id' = :scope_id
          )
    """


async def list_reference_compiled_by_object(
    session: AsyncSession, tenant_id: str, object_id: str,
    *, visible_kb_ids: list[str] | None,
) -> list[dict]:
    params: dict = {"tenant_id": tenant_id, "scope_id": object_id}
    if visible_kb_ids is not None:
        params["visible_kb_ids"] = list(visible_kb_ids)
    sql = text(
        _reference_select() + _reference_from(visible_kb_ids)
        + _scope_exists_clause(nested=False)
        + f"""
          ORDER BY ki.name ASC, cs.knowledge_base_id ASC
          LIMIT {REFERENCE_LIMIT}
        """
    )
    rows = (await session.execute(sql, params)).mappings().all()
    return [dict(row) for row in rows]


async def list_reference_compiled_by_attribute(
    session: AsyncSession, tenant_id: str, attribute_id: str,
    *, visible_kb_ids: list[str] | None,
) -> list[dict]:
    params: dict = {"tenant_id": tenant_id, "scope_id": attribute_id}
    if visible_kb_ids is not None:
        params["visible_kb_ids"] = list(visible_kb_ids)
    sql = text(
        _reference_select() + _reference_from(visible_kb_ids)
        + _scope_exists_clause(nested=True)
        + f"""
          ORDER BY ki.name ASC, cs.knowledge_base_id ASC
          LIMIT {REFERENCE_LIMIT}
        """
    )
    rows = (await session.execute(sql, params)).mappings().all()
    return [dict(row) for row in rows]


async def count_reference_compiled_by_object(
    session: AsyncSession, tenant_id: str, object_id: str,
    *, visible_kb_ids: list[str] | None,
) -> int:
    params: dict = {"tenant_id": tenant_id, "scope_id": object_id}
    if visible_kb_ids is not None:
        params["visible_kb_ids"] = list(visible_kb_ids)
    sql = text(
        "SELECT COUNT(*) AS total" + _reference_from(visible_kb_ids)
        + _scope_exists_clause(nested=False)
    )
    row = (await session.execute(sql, params)).mappings().first()
    return int((row or {}).get("total") or 0)


async def count_reference_compiled_by_attribute(
    session: AsyncSession, tenant_id: str, attribute_id: str,
    *, visible_kb_ids: list[str] | None,
) -> int:
    params: dict = {"tenant_id": tenant_id, "scope_id": attribute_id}
    if visible_kb_ids is not None:
        params["visible_kb_ids"] = list(visible_kb_ids)
    sql = text(
        "SELECT COUNT(*) AS total" + _reference_from(visible_kb_ids)
        + _scope_exists_clause(nested=True)
    )
    row = (await session.execute(sql, params)).mappings().first()
    return int((row or {}).get("total") or 0)


async def list_reference_bindings(
    session: AsyncSession, tenant_id: str, scenario_id: str,
    *, visible_kb_ids: list[str] | None,
) -> list[dict]:
    """场景影响：生效绑定（`status='active'`），left join compiled 取版本信息。"""
    params: dict = {"tenant_id": tenant_id, "scenario_id": scenario_id}
    if visible_kb_ids is not None:
        params["visible_kb_ids"] = list(visible_kb_ids)
    sql = text(f"""
        SELECT b.knowledge_base_id AS kb_id, ki.name AS kb_name, ki.space_id,
               b.status AS binding_status, b.source_type,
               b.template_scenario_id AS binding_scenario_id,
               cs.template_scenario_id AS compiled_scenario_id,
               cs.source_version, cs.schema_version,
               cs.sync_status, cs.schema_mode
          FROM knowledge_base.ontology_template_bindings b
          JOIN knowledge_base.knowledge_info ki
            ON ki.id = b.knowledge_base_id AND ki.tenant_id = b.tenant_id
           AND ki.is_deleted = 0
          LEFT JOIN knowledge_base.ontology_compiled_schemas cs
            ON cs.tenant_id = b.tenant_id AND cs.knowledge_base_id = b.knowledge_base_id
         WHERE b.tenant_id = :tenant_id AND b.template_scenario_id = :scenario_id
           AND b.status = 'active'
           {_visibility_clause('b.knowledge_base_id', visible_kb_ids)}
         ORDER BY ki.name ASC, b.knowledge_base_id ASC
    """)
    rows = (await session.execute(sql, params)).mappings().all()
    return [dict(row) for row in rows]


async def list_reference_compiled_by_scenario(
    session: AsyncSession, tenant_id: str, scenario_id: str,
    *, visible_kb_ids: list[str] | None,
) -> list[dict]:
    """场景影响：compiled 侧（即使没有绑定行也要返回）。"""
    params: dict = {"tenant_id": tenant_id, "scenario_id": scenario_id}
    if visible_kb_ids is not None:
        params["visible_kb_ids"] = list(visible_kb_ids)
    sql = text(f"""
        SELECT cs.knowledge_base_id AS kb_id, ki.name AS kb_name, ki.space_id,
               NULL AS binding_status,
               cs.source_type,
               NULL AS binding_scenario_id,
               cs.template_scenario_id AS compiled_scenario_id,
               cs.source_version, cs.schema_version,
               cs.sync_status, cs.schema_mode
          FROM knowledge_base.ontology_compiled_schemas cs
          JOIN knowledge_base.knowledge_info ki
            ON ki.id = cs.knowledge_base_id AND ki.tenant_id = cs.tenant_id
           AND ki.is_deleted = 0
         WHERE cs.tenant_id = :tenant_id
           AND cs.template_scenario_id = :scenario_id
           {_visibility_clause('cs.knowledge_base_id', visible_kb_ids)}
         ORDER BY ki.name ASC, cs.knowledge_base_id ASC
    """)
    rows = (await session.execute(sql, params)).mappings().all()
    return [dict(row) for row in rows]


# ---------------------------------------------------------------------------
# 文档统计（一次 GROUP BY，避免逐 KB N+1）
# ---------------------------------------------------------------------------


async def doc_stats(
    session: AsyncSession, tenant_id: str, kb_ids: list[str],
) -> dict[str, dict]:
    if not kb_ids:
        return {}
    sql = text("""
        SELECT d.knowledge_base_id,
               COUNT(*) AS doc_total,
               COUNT(*) FILTER (
                   WHERE d.ontology_target_schema_version IS NULL
               ) AS no_target,
               COUNT(*) FILTER (
                   WHERE d.ontology_target_schema_version IS NOT NULL
                     AND d.ontology_target_schema_version
                         IS DISTINCT FROM d.ontology_applied_schema_version
               ) AS not_applied,
               COUNT(*) FILTER (
                   WHERE d.ontology_applied_schema_version = cs.schema_version
               ) AS applied_current,
               COUNT(*) FILTER (
                   WHERE d.ontology_status = 'ready'
               ) AS ontology_ready
          FROM knowledge_base.knowledge_documents d
          LEFT JOIN knowledge_base.ontology_compiled_schemas cs
            ON cs.tenant_id = d.tenant_id AND cs.knowledge_base_id = d.knowledge_base_id
         WHERE d.tenant_id = :tenant_id AND d.is_deleted = 0
           AND d.status <> 'archived'
           AND d.knowledge_base_id = ANY(:kb_ids)
         GROUP BY d.knowledge_base_id
    """)
    rows = (
        await session.execute(
            sql, {"tenant_id": tenant_id, "kb_ids": list(kb_ids)},
        )
    ).mappings().all()
    return {row["knowledge_base_id"]: dict(row) for row in rows}


# ---------------------------------------------------------------------------
# scope 存在性：对象/属性必须经场景/领域且都未软删
# ---------------------------------------------------------------------------


async def get_object_scope(
    session: AsyncSession, tenant_id: str, object_id: str,
) -> dict | None:
    sql = text("""
        SELECT o.id, o.scenario_id
          FROM business_domain.template_objects o
          JOIN business_domain.template_scenarios s
            ON s.tenant_id = o.tenant_id AND s.id = o.scenario_id AND s.is_deleted = 0
          JOIN business_domain.template_domains d
            ON d.tenant_id = s.tenant_id AND d.id = s.domain_id AND d.is_deleted = 0
         WHERE o.tenant_id = :tenant_id AND o.id = :scope_id AND o.is_deleted = 0
    """)
    row = (
        await session.execute(
            sql, {"tenant_id": tenant_id, "scope_id": object_id},
        )
    ).mappings().first()
    return dict(row) if row else None


async def get_attribute_scope(
    session: AsyncSession, tenant_id: str, attribute_id: str,
) -> dict | None:
    sql = text("""
        SELECT a.id, o.id AS object_id, o.scenario_id
          FROM business_domain.template_attributes a
          JOIN business_domain.template_objects o
            ON o.tenant_id = a.tenant_id AND o.id = a.template_object_id
           AND o.is_deleted = 0
          JOIN business_domain.template_scenarios s
            ON s.tenant_id = o.tenant_id AND s.id = o.scenario_id AND s.is_deleted = 0
          JOIN business_domain.template_domains d
            ON d.tenant_id = s.tenant_id AND d.id = s.domain_id AND d.is_deleted = 0
         WHERE a.tenant_id = :tenant_id AND a.id = :scope_id AND a.is_deleted = 0
    """)
    row = (
        await session.execute(
            sql, {"tenant_id": tenant_id, "scope_id": attribute_id},
        )
    ).mappings().first()
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# 发布版本历史（第 6 项第二增量）
# ---------------------------------------------------------------------------


async def list_scenario_versions(
    session: AsyncSession, tenant_id: str, scenario_id: str,
) -> list[dict]:
    """场景发布版本列表（不含 snapshot，保持列表轻量）。"""
    sql = text("""
        SELECT id, version, structure_hash, published_by, published_at
          FROM business_domain.template_scenario_versions
         WHERE tenant_id = :tenant_id
           AND scenario_id = :scenario_id
         ORDER BY version DESC
    """)
    rows = (
        await session.execute(
            sql, {"tenant_id": tenant_id, "scenario_id": scenario_id},
        )
    ).mappings().all()
    return [dict(row) for row in rows]


async def get_scenario_version_row(
    session: AsyncSession, tenant_id: str, scenario_id: str, version: int,
) -> dict | None:
    """单个发布版本（含完整 snapshot）。"""
    sql = text("""
        SELECT id, version, structure_hash, snapshot, published_by, published_at
          FROM business_domain.template_scenario_versions
         WHERE tenant_id = :tenant_id
           AND scenario_id = :scenario_id
           AND version = :version
         LIMIT 1
    """)
    row = (
        await session.execute(
            sql,
            {"tenant_id": tenant_id, "scenario_id": scenario_id, "version": version},
        )
    ).mappings().first()
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# 落标校验：最新发布快照 + 各 KB 编译产物
# ---------------------------------------------------------------------------


async def get_latest_scenario_version(
    session: AsyncSession, tenant_id: str, scenario_id: str,
) -> dict | None:
    """场景的最新发布版本（含完整 snapshot）。"""
    sql = text("""
        SELECT id, version, structure_hash, snapshot, published_by, published_at
          FROM business_domain.template_scenario_versions
         WHERE tenant_id = :tenant_id
           AND scenario_id = :scenario_id
         ORDER BY version DESC
         LIMIT 1
    """)
    row = (
        await session.execute(
            sql, {"tenant_id": tenant_id, "scenario_id": scenario_id},
        )
    ).mappings().first()
    return dict(row) if row else None


async def list_compiled_for_scenario(
    session: AsyncSession,
    tenant_id: str,
    scenario_id: str,
    visible_kb_ids: list[str] | None,
) -> list[dict]:
    """场景下有 compiled schema 的 KB（含 entity_types / relation_types）。"""
    if visible_kb_ids == []:
        return []
    sql = text(f"""
        SELECT s.knowledge_base_id AS kb_id,
               k.name AS kb_name,
               s.template_scenario_id AS compiled_scenario_id,
               s.source_type,
               s.source_version,
               s.schema_version,
               s.sync_status,
               s.schema_mode,
               s.compiled_at,
               s.entity_types,
               s.relation_types
          FROM knowledge_base.ontology_compiled_schemas s
          JOIN knowledge_base.knowledge_info k
            ON k.id = s.knowledge_base_id
           AND k.tenant_id = s.tenant_id
           AND k.is_deleted = 0
         WHERE s.tenant_id = :tenant_id
           AND s.template_scenario_id = :scenario_id
           {_visibility_clause("s.knowledge_base_id", visible_kb_ids)}
         ORDER BY k.name ASC, s.knowledge_base_id ASC
    """)
    rows = (
        await session.execute(
            sql,
            {
                "tenant_id": tenant_id,
                "scenario_id": scenario_id,
                **(
                    {"visible_kb_ids": list(visible_kb_ids or [])}
                    if visible_kb_ids is not None else {}
                ),
            },
        )
    ).mappings().all()
    return [dict(row) for row in rows]
