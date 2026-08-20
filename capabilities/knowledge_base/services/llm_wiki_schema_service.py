"""LLM-Wiki Schema 服务（kb_type=openkb 编译设置权威源）。

方案：docs/llmwiki-schema-settings-execution-plan.md

职责：
- get_schema(auto_create=True)：读 active schema，缺则建默认
- save_schema：校验 → 渲染 → CAS 留档 → apply 投影 → 更新文档 target 版本
- render_agents_md：内置段（AGENTS_MD 快照原样）+ 注入段（§4.1 模板）
- apply_to_openkb：调 OpenKB apply_schema action（含 sync 回写与补偿）
- update_documents_target_schema_version：schema 变更后文档过期标记
"""
import logging
import re
from typing import Any, Optional

from jonex_core.common.database import get_db_session
from jonex_core.common.exceptions import InvalidParameterError, ResourceNotFoundError
from jonex_core.common.i18n import translate
from jonex_core.common.tenant import require_tenant

from ..dtos.llm_wiki_schema import (
    DEFAULT_AGENTS_MD_EXTRA,
    DEFAULT_ENTITY_TYPES,
    SaveLlmWikiSchemaRequest,
)
from ..models import LlmWikiSchema, STATUS_ACTIVE, SYNC_APPLY_FAILED, SYNC_SYNCED
from ..repository.llm_wiki_schema_repository import LlmWikiSchemaRepository

logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════════
# [jonex] AGENTS_MD 快照（同步自 Reference/OpenKB/openkb/schema.py 的
# AGENTS_MD 常量）。knowledge-base-service 容器无法 import vendored
# OpenKB 源码（跨容器），render 在 KB 侧执行需要本快照；
# vendored 升级时对照 schema.py 同步更新（JONEX_CHANGES 重放约定）。
# ══════════════════════════════════════════════════════════════════
AGENTS_MD_SNAPSHOT = """# Wiki Schema

## Directory Structure
- sources/ — Document content. Short docs as .md, long docs as .json (per-page). Do not modify directly.
- sources/images/ — Extracted images from documents, referenced by sources.
- summaries/ — One per source document. Summary of key content.
- concepts/ — Cross-document topic synthesis. Created when a theme spans multiple documents.
- entities/ — Specific named things: people, organizations, places, products, named works, events. One page per entity, accumulated across documents.
- explorations/ — Saved query results, analyses, and comparisons worth keeping.
- reports/ — Lint health check reports. Auto-generated.

## Special Files
- index.md — Content catalog: every page with link, one-line summary, organized by category.
- log.md — Chronological append-only record of operations (ingests, queries, lints).

## Page Types
- **Summary Page** (summaries/): Key content of a single source document.
- **Concept Page** (concepts/): Cross-document topic synthesis with [[wikilinks]].
- **Entity Page** (entities/): A specific named thing (proper noun) — e.g. a person, organization, place, product, named work, or event. Each page has a `type:` frontmatter field; the exact allowed type set is configurable (default: person, organization, place, product, work, event, other) and the authoritative set for this run is given in the compilation prompt. An entity differs from a concept: a concept is an abstract recurring idea; an entity is a specific named thing. Create an entity page only when the entity is central to a document or recurs across sources — do not page passing mentions.
- **Exploration Page** (explorations/): Saved query results — analyses, comparisons, syntheses.
- **Index Page** (index.md): One-liner summary of every page in the wiki. Auto-maintained.

## Index Page Format
index.md lists all documents, concepts, entities, and explorations with metadata:
- Documents: name, one-liner description, type (short|pageindex), detail access path
- Concepts: name, one-liner description
- Entities: name, type, one-liner description
- Explorations: name, one-liner description

## Log Format
Each log entry: `## [YYYY-MM-DD HH:MM:SS] operation | description`
Operations: ingest, query, lint

## Format
- Use [[wikilink]] to link other wiki pages (e.g., [[concepts/attention]])
- Standard Markdown heading hierarchy
- Keep each page focused on a single topic

## Frontmatter (managed by code — do NOT emit it in generated content)
- Every summary/concept/entity page carries a non-empty `type:` — `Summary`,
  `Concept`, or a capitalized entity subtype (e.g. `Organization`). This is the
  one field OKF requires; consumers use it for routing/filtering/presentation.
- `description:` — a single-sentence one-liner (the field formerly named `brief`).
- Do not include YAML frontmatter (---) in generated content; it is managed by code.

## Key Points of the Answer
- Do not list the basis and explanations in parentheses, just answer the question itself. For example, "according to", "see details", etc. I don't want to see reference source information. For a correct response like "1+1=?", just answer "2."
- Answer the question directly and accurately; do not restate or paraphrase the question.
- Only include information that directly answers the question - irrelevant background information, cautions, and generic filler content are prohibited.
- It is prohibited to describe the search process, tool usage, or reasoning steps in the answer.
- Use short and direct answers.
- Do not add closing remarks, proposals, or follow-up suggestions.
"""

_CODE_RE = re.compile(r"^[a-z0-9 _-]+$")


def render_agents_md(entity_types: list[dict],
                     concept_types: list[dict] | None = None,
                     agents_md_extra: str = "") -> str:
    """渲染 AGENTS.md（方案 §4.1 修订版）。

    内置段（AGENTS_MD 快照英文原样）+ 实体词表段 + 概念词表段（可选，无硬校验
    仅分类引导）+ 用户自定义追加块（多行 markdown 原样拼接——页面规则/编译
    规则等均由该块承载）。同一配置渲染结果 byte 级一致（确定性）。
    """
    parts: list[str] = [AGENTS_MD_SNAPSHOT.rstrip()]

    # ── Entity Types（受控词表）──
    lines = [
        "",
        "## Entity Types (受控词表)",
        "编译时实体页的 `type` 只能取下列值；超出词表的类型会静默回落为 `other`。",
    ]
    for et in entity_types:
        code = str(et.get("code", "")).strip()
        name = str(et.get("name", "")).strip()
        desc = str(et.get("description", "")).strip()
        line = f"- {code} — {name}"
        if desc:
            line += f"：{desc}"
        lines.append(line)
    parts.append("\n".join(lines))

    # ── Concept Types（概念类型词表，分类引导；OpenKB 无硬校验）──
    if concept_types:
        lines = [
            "",
            "## Concept Types (概念类型词表)",
            "概念页的主题分类可参考下列类型（无硬校验，供归类参考）：",
        ]
        for ct in concept_types:
            code = str(ct.get("code", "")).strip()
            name = str(ct.get("name", "")).strip()
            desc = str(ct.get("description", "")).strip()
            line = f"- {code} — {name}"
            if desc:
                line += f"：{desc}"
            lines.append(line)
        parts.append("\n".join(lines))

    # ── 用户自定义追加块（原样拼接，不格式化）──
    extra = (agents_md_extra or "").strip()
    if extra:
        parts.append("")
        parts.append(extra)

    return "\n".join(parts).strip() + "\n"


class LlmWikiSchemaService:
    def __init__(self) -> None:
        self._repo = LlmWikiSchemaRepository()

    async def _require_openkb_kb(self, tenant_id: str, kb_id: str) -> None:
        """校验 KB 存在且 kb_type=openkb（方案 §7 第 1 步）。"""
        from .kb_type_service import get_kb_type

        kb_type = await get_kb_type(tenant_id, kb_id)
        if kb_type is None:
            raise ResourceNotFoundError(
                message=translate("err.kb.not_found", fallback="知识库不存在"),
                details={"knowledge_base_id": kb_id},
            )
        if kb_type != "openkb":
            raise InvalidParameterError(
                message=translate(
                    "err.llm_wiki_schema.kb_type_mismatch",
                    fallback="LLM-Wiki Schema 仅适用于 llm-wiki（openkb）类型知识库",
                ),
                details={"error_code": "LLM_WIKI_SCHEMA_KB_TYPE_MISMATCH", "kb_type": kb_type},
            )

    async def get_schema(self, tenant_id: str, kb_id: str, *,
                         auto_create: bool = True) -> dict:
        """读 active schema；缺失且 auto_create → 建默认（方案 §15 兜底）。"""
        tenant_id = require_tenant(tenant_id)
        await self._require_openkb_kb(tenant_id, kb_id)
        row = await self._repo.get_active(tenant_id, kb_id)
        if row is None and auto_create:
            row = await self._create_default(tenant_id, kb_id)
        if row is None:
            raise ResourceNotFoundError(
                message=translate("err.llm_wiki_schema.not_found", fallback="LLM-Wiki Schema 不存在"),
                details={"error_code": "LLM_WIKI_SCHEMA_NOT_FOUND"},
            )
        return row.to_dict()

    async def _create_default(self, tenant_id: str, kb_id: str) -> LlmWikiSchema:
        """创建默认 schema（DB 行 + 同步 apply 失败不阻断）。"""
        entity_types = [dict(t) for t in DEFAULT_ENTITY_TYPES]
        concept_types: list = []
        agents_md_extra = DEFAULT_AGENTS_MD_EXTRA
        agents_md = render_agents_md(entity_types, concept_types, agents_md_extra)
        config = {"language": "zh-CN", "entity_types": [t["code"] for t in entity_types]}
        row = await self._repo.create_active(
            tenant_id, kb_id,
            schema_version=1, schema_name="default", language="zh-CN",
            model=None, entity_types=entity_types, concept_types=concept_types,
            agents_md_extra=agents_md_extra, agents_md=agents_md,
            config_snapshot=config,
        )
        await self.apply_to_openkb(tenant_id, kb_id, row.schema_version)
        return row

    async def save_schema(self, tenant_id: str, request: SaveLlmWikiSchemaRequest | dict,
                          *, user_id: Optional[str] = None) -> dict:
        """保存（CAS 留档）→ apply 投影 → 更新文档 target 版本（方案 §7）。"""
        tenant_id = require_tenant(tenant_id)
        req = SaveLlmWikiSchemaRequest(**_payload(request))
        kb_id = req.knowledge_base_id
        await self._require_openkb_kb(tenant_id, kb_id)

        # ① 校验 expected_schema_version（CAS 前置：active 版本必须一致）
        current = await self._repo.get_active(tenant_id, kb_id)
        if current is None:
            raise ResourceNotFoundError(
                message=translate("err.llm_wiki_schema.not_found", fallback="LLM-Wiki Schema 不存在"),
                details={"error_code": "LLM_WIKI_SCHEMA_NOT_FOUND"},
            )
        if current.schema_version != req.expected_schema_version:
            raise InvalidParameterError(
                message=translate(
                    "err.llm_wiki_schema.version_conflict",
                    fallback="LLM-Wiki Schema 已被更新，请刷新后重试",
                ),
                details={
                    "error_code": "LLM_WIKI_SCHEMA_VERSION_CONFLICT",
                    "current": current.schema_version,
                    "expected": req.expected_schema_version,
                },
            )

        # ② code 格式强校验（方案 §7 第 3 步，DTO 已做；此处防御性复查 other 存在）
        codes = [t.code for t in req.entity_types]
        if "other" not in codes:
            raise InvalidParameterError(
                message=translate(
                    "err.llm_wiki_schema.invalid_entity_type",
                    fallback="实体类型词表必须包含 other（兜底类型）",
                ),
                details={"error_code": "LLM_WIKI_SCHEMA_INVALID_ENTITY_TYPE"},
            )

        # ③ 渲染 + 构建投影
        entity_types = [t.dict() for t in req.entity_types]
        concept_types = [c.dict() for c in req.concept_types]
        agents_md_extra = req.agents_md_extra
        agents_md = render_agents_md(entity_types, concept_types, agents_md_extra)
        config = {
            "model": req.model,
            "language": req.language,
            "entity_types": codes,
        }

        # ④ CAS 留档（UPDATE 旧行钉 CAS → INSERT 新行，同事务）
        new_version = current.schema_version + 1
        row = await self._repo.save_with_cas(
            tenant_id, kb_id,
            expected_version=current.schema_version, new_version=new_version,
            schema_name=req.schema_name, language=req.language, model=req.model,
            entity_types=entity_types, concept_types=concept_types,
            agents_md_extra=agents_md_extra, agents_md=agents_md,
            config_snapshot=config, edited_by=user_id,
        )

        # ⑤ apply 投影（失败不阻断：sync_status=apply_failed，编译前补偿）
        await self.apply_to_openkb(tenant_id, kb_id, new_version)

        # ⑥ 更新文档 target 版本（不复用 stale，方案 §7 第 8 步）
        affected = await self.update_documents_target_schema_version(
            tenant_id, kb_id, new_version)

        result = row.to_dict()
        result["affected_documents"] = affected
        return result

    async def apply_to_openkb(self, tenant_id: str, kb_id: str,
                              schema_version: Optional[int] = None) -> dict:
        """调 OpenKB apply_schema；结果回写 sync_status（方案 §7 第 7 步）。"""
        row = await self._repo.get_active(tenant_id, kb_id)
        if row is None:
            raise ResourceNotFoundError(
                message=translate("err.llm_wiki_schema.not_found", fallback="LLM-Wiki Schema 不存在"),
                details={"error_code": "LLM_WIKI_SCHEMA_NOT_FOUND"},
            )
        version = schema_version if schema_version is not None else row.schema_version
        if version != row.schema_version:
            # apply 指定历史版本：不允许（只投影 active）
            raise InvalidParameterError(
                message=translate("err.llm_wiki_schema.not_applied", fallback="仅可同步当前 active Schema"),
                details={"error_code": "LLM_WIKI_SCHEMA_NOT_APPLIED"},
            )

        from jonex_core.capability.atomic.openkb.client import get_openkb_client

        try:
            result = await get_openkb_client().apply_schema(
                kb_name=kb_id, tenant_id=tenant_id, kb_id=kb_id,
                schema_version=version,
                config={
                    "model": row.model,
                    "language": row.language,
                    "entity_types": [t["code"] for t in (row.entity_types or [])],
                },
                agents_md=row.agents_md,
            )
            await self._repo.mark_sync(tenant_id, kb_id, version,
                                       sync_status=SYNC_SYNCED, applied=True)
            return result
        except Exception as exc:
            logger.warning("LLM-Wiki Schema apply 失败 kb=%s ver=%s: %s",
                           kb_id, version, exc)
            await self._repo.mark_sync(tenant_id, kb_id, version,
                                       sync_status=SYNC_APPLY_FAILED)
            return {"applied": False, "schema_version": version,
                    "error": str(exc)[:500]}

    async def update_documents_target_schema_version(
            self, tenant_id: str, kb_id: str, schema_version: int) -> int:
        """schema 变更后：该 KB ready 文档的 target 版本批量更新（不复用 stale）。"""
        from sqlalchemy import text

        async with get_db_session() as session:
            res = await session.execute(
                text(
                    "UPDATE knowledge_base.knowledge_documents"
                    "   SET llm_wiki_target_schema_version = :ver, updated_at = CURRENT_TIMESTAMP"
                    " WHERE tenant_id = :tid AND knowledge_base_id = :kb"
                    "   AND is_deleted = 0 AND status = 'ready'"
                ),
                {"ver": schema_version, "tid": tenant_id, "kb": kb_id},
            )
            await session.commit()
            return res.rowcount or 0

    # ── YAML 导入导出（方案 §11）──

    async def export_yaml(self, tenant_id: str, kb_id: str) -> dict:
        """导出 YAML（format_version: 1；schema_version 是业务版本，两者不同源）。"""
        import yaml as _yaml

        row = await self._repo.get_active(tenant_id, kb_id)
        if row is None:
            raise ResourceNotFoundError(
                message=translate("err.llm_wiki_schema.not_found", fallback="LLM-Wiki Schema 不存在"),
                details={"error_code": "LLM_WIKI_SCHEMA_NOT_FOUND"},
            )
        doc = {
            "format_version": 1,
            "schema_name": row.schema_name,
            "language": row.language,
            "model": row.model,
            "entity_types": row.entity_types or [],
            "concept_types": row.concept_types or [],
            "agents_md_extra": row.agents_md_extra or "",
        }
        return {"yaml_text": _yaml.safe_dump(
            doc, allow_unicode=True, sort_keys=False, default_flow_style=False)}

    async def import_yaml(self, tenant_id: str, request: dict,
                          *, user_id: Optional[str] = None) -> dict:
        """YAML 导入（dry-run 校验；非 dry-run 全量替换，走同一 CAS）。"""
        import yaml as _yaml

        from ..dtos.llm_wiki_schema import (
            LlmWikiConceptType, LlmWikiEntityType,
            SaveLlmWikiSchemaRequest,
        )

        yaml_text = str(request.get("yaml_text") or "")
        expected = int(request.get("expected_schema_version") or 0)
        dry_run = bool(request.get("dry_run", False))
        try:
            doc = _yaml.safe_load(yaml_text) or {}
            if not isinstance(doc, dict):
                raise ValueError("YAML 顶层必须是 mapping")
            if int(doc.get("format_version") or 0) != 1:
                raise ValueError("format_version 必须为 1")
        except Exception as exc:
            raise InvalidParameterError(
                message=translate("err.llm_wiki_schema.invalid_yaml", fallback="YAML 解析失败"),
                details={"error_code": "LLM_WIKI_SCHEMA_INVALID_YAML", "error": str(exc)[:200]},
            ) from exc

        try:
            entity_types = [LlmWikiEntityType(**t).dict() for t in (doc.get("entity_types") or [])]
            concept_types = [LlmWikiConceptType(**c).dict() for c in (doc.get("concept_types") or [])]
            agents_md_extra = str(doc.get("agents_md_extra") or "")
        except Exception as exc:
            raise InvalidParameterError(
                message=translate("err.llm_wiki_schema.invalid_yaml", fallback="YAML 字段校验失败"),
                details={"error_code": "LLM_WIKI_SCHEMA_INVALID_YAML", "error": str(exc)[:200]},
            ) from exc

        if "other" not in [t["code"] for t in entity_types]:
            raise InvalidParameterError(
                message=translate(
                    "err.llm_wiki_schema.invalid_entity_type",
                    fallback="实体类型词表必须包含 other（兜底类型）"),
                details={"error_code": "LLM_WIKI_SCHEMA_INVALID_ENTITY_TYPE"},
            )

        if dry_run:
            return {"valid": True, "entity_types": entity_types,
                    "concept_types": concept_types,
                    "agents_md_extra": agents_md_extra}

        # 非 dry-run：组装 SaveLlmWikiSchemaRequest 走统一保存（CAS + apply + target 更新）
        return await self.save_schema(
            tenant_id,
            SaveLlmWikiSchemaRequest(
                knowledge_base_id=str(request.get("knowledge_base_id") or ""),
                expected_schema_version=expected,
                schema_name=str(doc.get("schema_name") or "default"),
                language=str(doc.get("language") or "zh-CN"),
                model=doc.get("model"),
                entity_types=entity_types,
                concept_types=concept_types,
                agents_md_extra=agents_md_extra,
            ),
            user_id=user_id,
        )

    # ── 全库重编（方案 §9，一次性提交，与批量上传同体验）──

    async def recompile_outdated_documents(self, tenant_id: str,
                                           request: dict) -> dict:
        """扫 `applied < target` 的 openkb 文档 → 逐篇 claim + submit。

        执行模式（方案 §9 定稿）：一次性提交全部匹配文档，不分批不限流——
        与现有「批量上传全部进队、OpenKB 容器 mutation 锁串行消化」体验一致。
        """
        from sqlalchemy import text

        kb_id = str(request.get("knowledge_base_id") or "")
        await self._require_openkb_kb(tenant_id, kb_id)
        only_outdated = bool(request.get("only_outdated", True))

        # 读 active schema（submit 需要版本）
        row = await self._repo.get_active(tenant_id, kb_id)
        if row is None:
            raise ResourceNotFoundError(
                message=translate("err.llm_wiki_schema.not_found", fallback="LLM-Wiki Schema 不存在"),
                details={"error_code": "LLM_WIKI_SCHEMA_NOT_FOUND"},
            )
        target_version = row.schema_version

        # 匹配过期文档（only_outdated：applied IS DISTINCT FROM target；
        # 否则 failed/过期全部——failed 文档 status != ready 需另查，本期只扫 ready）
        async with get_db_session() as session:
            rows = (await session.execute(
                text(
                    "SELECT id FROM knowledge_base.knowledge_documents"
                    " WHERE tenant_id = :tid AND knowledge_base_id = :kb"
                    "   AND is_deleted = 0 AND status = 'ready'"
                    "   AND llm_wiki_applied_schema_version IS DISTINCT FROM"
                    "       llm_wiki_target_schema_version"
                ),
                {"tid": tenant_id, "kb": kb_id},
            )).all()

        from .openkb_service import KnowledgeCompilerService
        compiler = KnowledgeCompilerService()
        submitted = skipped = failed = 0
        for r in rows:
            try:
                result = await compiler.recompile_openkb(
                    tenant_id=tenant_id, document_id=r.id, kb_id=kb_id,
                    doc_status="ready",
                )
                submitted += 1
            except Exception as exc:
                logger.warning("全库重编跳过 doc=%s: %s", r.id, exc)
                if getattr(exc, "message", "") and ("编译中" in str(exc.message)):
                    skipped += 1  # 已在编译中
                else:
                    failed += 1

        return {
            "matched": len(rows),
            "submitted": submitted,
            "skipped": skipped,
            "failed": failed,
            "schema_version": target_version,
        }


def _payload(model_or_dict: Any) -> dict[str, Any]:
    if isinstance(model_or_dict, dict):
        return model_or_dict
    if hasattr(model_or_dict, "model_dump"):
        return model_or_dict.model_dump(exclude_none=True)
    return model_or_dict.dict(exclude_none=True)
