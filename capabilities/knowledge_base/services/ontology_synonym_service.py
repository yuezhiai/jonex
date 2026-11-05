#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""KB-level ontology synonym CRUD + batch import service.

本期范围：仅存储 + 管理（CRUD + 批量导入/导出）。抽取/查询归一化生效为后续。
规则见 docs/kb-compile-tab-and-synonym-plan.md §3.7。
"""
import logging
import uuid

from jonex_core.common import get_db_session
from jonex_core.common.exceptions import InvalidParameterError, ResourceConflictError
from jonex_core.common.i18n import translate
from jonex_core.common.tenant import require_tenant

logger = logging.getLogger(__name__)

from ..dtos.ontology_synonym import (
    MAX_IMPORT_GROUPS,
    MAX_TERM_LENGTH,
    MAX_TERMS_PER_GROUP,
)
from ..repository.knowledge_info_repository import KnowledgeInfoRepository
from ..repository.ontology_synonym_repository import OntologySynonymRepository


def _to_halfwidth(text: str) -> str:
    """全角转半角。"""
    out = []
    for ch in text:
        code = ord(ch)
        if code == 0x3000:  # 全角空格
            code = 0x20
        elif 0xFF01 <= code <= 0xFF5E:  # 全角 ASCII
            code -= 0xFEE0
        out.append(chr(code))
    return "".join(out)


def _normalize_display(term: str) -> str:
    """用于存储/展示的归一：trim + 全角转半角（保留大小写）。"""
    return _to_halfwidth((term or "").strip())


def _normalize_key(term: str) -> str:
    """用于去重/跨组冲突判定的归一键：display 归一后再大小写不敏感。"""
    return _normalize_display(term).casefold()


def _normalize_group(terms: list) -> tuple[list[str], list[str]]:
    """归一化一组词。

    返回 (display_terms, keys)：display_terms 去重保序用于存储，keys 为对应归一键。
    """
    display_terms: list[str] = []
    keys: list[str] = []
    seen: set[str] = set()
    for raw in terms or []:
        disp = _normalize_display(str(raw))
        if not disp:
            continue
        if len(disp) > MAX_TERM_LENGTH:
            raise InvalidParameterError(message=translate("err.synonym.term_too_long", params={"max": str(MAX_TERM_LENGTH), "term": disp[:20]}, fallback=f"同义词超过长度上限({MAX_TERM_LENGTH}): {disp[:20]}...")  )  # 原消息)
        key = disp.casefold()
        if key in seen:
            continue
        seen.add(key)
        display_terms.append(disp)
        keys.append(key)
    return display_terms, keys


def _resolve_canonical(canonical, display_terms: list[str], keys: list[str]) -> str:
    """校验并返回 canonical（缺省取 terms[0]，必须属于 terms）。"""
    if canonical is None or not str(canonical).strip():
        return display_terms[0]
    c_key = _normalize_key(str(canonical))
    if c_key not in keys:
        raise InvalidParameterError(message=translate("err.synonym.canonical_not_in_terms", fallback="canonical 必须属于 terms")  )  # 原消息)
    return display_terms[keys.index(c_key)]


class OntologySynonymService:
    """知识库级同义词组 CRUD + 批量导入。"""

    async def _ensure_kb(self, session, kb_id: str, tenant_id: str) -> None:
        """校验 KB 归属存在（不存在抛 ResourceNotFoundError）。"""
        await KnowledgeInfoRepository(session).get_required(kb_id, tenant_id)

    def _bump_value_alias_generation(self, tenant_id: str, kb_id: str) -> None:
        """同义词写入成功后推进 ② 值同义词缓存写代号（同进程写后读立即生效）。

        导入放在函数内：`nl2sql.value_aliases` 依赖本 service 所在包，延迟导入避免
        模块加载期耦合，也让单测可以 patch 源模块的 `bump_write_generation`。
        """
        from .nl2sql import value_aliases

        value_aliases.bump_write_generation(tenant_id, kb_id)

    async def _invalidate_schema_cache(self, tenant_id: str, kb_id: str) -> None:
        """[jonex] 同义词变更后清 compiled schema 缓存（key 与 atomic-rag 侧共用），
        使抽取端 prompt 的同义词最终一致。异常忽略（软提示层，不影响写图强一致归并）。"""
        try:
            from .ontology_compiler import OntologyCompiler

            await OntologyCompiler().invalidate_cache(tenant_id, kb_id)
        except Exception as e:
            logger.warning("Synonym cache invalidate failed: kb=%s err=%s", kb_id, e)

    async def list(self, tenant_id: str, kb_id: str, page: int = 1, page_size: int = 20) -> dict:
        tenant_id = require_tenant(tenant_id)
        page = max(1, int(page or 1))
        page_size = max(1, min(200, int(page_size or 20)))
        async with get_db_session() as session:
            await self._ensure_kb(session, kb_id, tenant_id)
            repo = OntologySynonymRepository(session)
            items, total = await repo.list_by_kb(
                tenant_id, kb_id, offset=(page - 1) * page_size, limit=page_size
            )
            return {
                "items": [o.to_dict() for o in items],
                "total": total,
                "page": page,
                "page_size": page_size,
            }

    async def create(self, tenant_id: str, data: dict) -> dict:
        tenant_id = require_tenant(tenant_id)
        kb_id = data["knowledge_base_id"]
        async with get_db_session() as session:
            await self._ensure_kb(session, kb_id, tenant_id)
            repo = OntologySynonymRepository(session)

            display_terms, keys = _normalize_group(data.get("terms") or [])
            if len(display_terms) < 2:
                raise InvalidParameterError(message=translate("err.synonym.min_terms", fallback="每组同义词去重后至少需要 2 个词")  )  # 原消息)
            if len(display_terms) > MAX_TERMS_PER_GROUP:
                raise InvalidParameterError(message=translate("err.synonym.max_terms", params={"max": str(MAX_TERMS_PER_GROUP)}, fallback=f"每组同义词数量超过上限({MAX_TERMS_PER_GROUP})")  )  # 原消息)
            canonical = _resolve_canonical(data.get("canonical"), display_terms, keys)

            # 跨组唯一（一期禁止一词多组）
            used = await self._used_term_keys(repo, tenant_id, kb_id)
            clash = [k for k in keys if k in used]
            if clash:
                raise ResourceConflictError(message=translate("err.synonym.cross_group_duplicate", params={"word": clash[0]}, fallback=f"存在跨组重复词: {clash[0]}")  )  # 原消息)

            obj = await repo.create(
                id=uuid.uuid4().hex,
                tenant_id=tenant_id,
                knowledge_base_id=kb_id,
                terms=display_terms,
                canonical=canonical,
            )
            await session.commit()
            await self._invalidate_schema_cache(tenant_id, kb_id)
            self._bump_value_alias_generation(tenant_id, kb_id)
            return obj.to_dict()

    async def update(self, tenant_id: str, synonym_id: str, data: dict) -> dict:
        tenant_id = require_tenant(tenant_id)
        async with get_db_session() as session:
            repo = OntologySynonymRepository(session)
            obj = await repo.get_required(synonym_id, tenant_id)

            new_terms = data.get("terms")
            if new_terms is not None:
                display_terms, keys = _normalize_group(new_terms)
                if len(display_terms) < 2:
                    raise InvalidParameterError(message=translate("err.synonym.min_terms", fallback="每组同义词去重后至少需要 2 个词")  )  # 原消息)
                if len(display_terms) > MAX_TERMS_PER_GROUP:
                    raise InvalidParameterError(message=translate("err.synonym.max_terms", params={"max": str(MAX_TERMS_PER_GROUP)}, fallback=f"每组同义词数量超过上限({MAX_TERMS_PER_GROUP})")  )  # 原消息)
                used = await self._used_term_keys(
                    repo, tenant_id, obj.knowledge_base_id, exclude_id=obj.id
                )
                clash = [k for k in keys if k in used]
                if clash:
                    raise ResourceConflictError(message=translate("err.synonym.cross_group_duplicate", params={"word": clash[0]}, fallback=f"存在跨组重复词: {clash[0]}")  )  # 原消息)
                obj.terms = display_terms
                # canonical 随 terms 一起校验（用新 canonical 或旧值或 terms[0]）
                canonical_src = data.get("canonical", obj.canonical)
                obj.canonical = _resolve_canonical(canonical_src, display_terms, keys)
            elif "canonical" in data:
                display_terms, keys = _normalize_group(obj.terms or [])
                obj.canonical = _resolve_canonical(data.get("canonical"), display_terms, keys)

            await session.commit()
            await self._invalidate_schema_cache(tenant_id, obj.knowledge_base_id)
            self._bump_value_alias_generation(tenant_id, obj.knowledge_base_id)
            return obj.to_dict()

    async def delete(self, tenant_id: str, synonym_id: str) -> bool:
        tenant_id = require_tenant(tenant_id)
        async with get_db_session() as session:
            repo = OntologySynonymRepository(session)
            obj = await repo.get_required(synonym_id, tenant_id)
            kb_id = obj.knowledge_base_id
            await repo.delete_soft(obj)
            await session.commit()
            await self._invalidate_schema_cache(tenant_id, kb_id)
            self._bump_value_alias_generation(tenant_id, kb_id)
            return True

    async def batch_import(self, tenant_id: str, kb_id: str, groups: list) -> dict:
        """先全量校验后写入：出现任一 failed 则整批不写库，正常返回 {created, skipped, failed}。"""
        tenant_id = require_tenant(tenant_id)
        groups = groups or []
        if len(groups) > MAX_IMPORT_GROUPS:
            raise InvalidParameterError(message=translate("err.synonym.import_max_groups", params={"max": str(MAX_IMPORT_GROUPS)}, fallback=f"单次导入组数超过上限({MAX_IMPORT_GROUPS})")  )  # 原消息)

        async with get_db_session() as session:
            await self._ensure_kb(session, kb_id, tenant_id)
            repo = OntologySynonymRepository(session)

            existing = await repo.list_all_by_kb(tenant_id, kb_id)
            existing_signatures: set[frozenset] = set()
            existing_keys: set[str] = set()
            for g in existing:
                _, ekeys = _normalize_group(g.terms or [])
                existing_signatures.add(frozenset(ekeys))
                existing_keys.update(ekeys)

            skipped = 0
            failed: list[dict] = []
            to_create: list[tuple[list[str], list[str]]] = []
            batch_keys: set[str] = set()
            batch_signatures: set[frozenset] = set()

            for index, group in enumerate(groups):
                try:
                    display_terms, keys = _normalize_group(group)
                except InvalidParameterError as e:
                    failed.append({"index": index, "reason": e.message})
                    continue
                if len(display_terms) < 2:
                    failed.append({"index": index, "reason": "去重后少于 2 个词"})
                    continue
                if len(display_terms) > MAX_TERMS_PER_GROUP:
                    failed.append({"index": index, "reason": f"词数超过上限({MAX_TERMS_PER_GROUP})"})
                    continue

                signature = frozenset(keys)
                # 完全重复组 → skipped（不算失败、不写入）
                if signature in existing_signatures or signature in batch_signatures:
                    skipped += 1
                    continue
                # 跨组词冲突 → failed
                clash = [k for k in keys if k in existing_keys or k in batch_keys]
                if clash:
                    failed.append({"index": index, "reason": f"跨组重复词: {clash[0]}"})
                    continue

                batch_signatures.add(signature)
                batch_keys.update(keys)
                to_create.append((display_terms, keys))

            # 有失败 → 整批不写库，正常返回
            if failed:
                return {"created": 0, "skipped": skipped, "failed": failed}

            for display_terms, keys in to_create:
                await repo.create(
                    id=uuid.uuid4().hex,
                    tenant_id=tenant_id,
                    knowledge_base_id=kb_id,
                    terms=display_terms,
                    canonical=display_terms[0],
                )
            if to_create:
                await session.commit()
                await self._invalidate_schema_cache(tenant_id, kb_id)
                self._bump_value_alias_generation(tenant_id, kb_id)

            return {"created": len(to_create), "skipped": skipped, "failed": []}

    async def _used_term_keys(
        self, repo: OntologySynonymRepository, tenant_id: str, kb_id: str, exclude_id: str = None
    ) -> set:
        used: set[str] = set()
        for g in await repo.list_all_by_kb(tenant_id, kb_id):
            if exclude_id and g.id == exclude_id:
                continue
            _, keys = _normalize_group(g.terms or [])
            used.update(keys)
        return used


__all__ = ["OntologySynonymService"]
