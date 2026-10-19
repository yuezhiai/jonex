#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""Repository for Knowledge Base 以图问知识的查询图片。

设计：docs/knowledge-base/image-query-search-execution-plan.md §4-B10 / §4-B12

**事务边界全部由 service 控制**：这里只 `execute` / `flush`，从不 `commit`。
原因是落盘顺序被写死成「短事务 ① → 事务外对象存储 IO → 短事务 ②」
（对象存储调用不能长时间占着数据库连接），两个事务块由 service 用
`get_db_session()` 圈出来。
"""

from typing import Any, Optional, Sequence
from uuid import uuid4

from sqlalchemy import delete, exists, func, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from jonex_core.common.repository import BaseRepository

from ..models import KnowledgeQueryImage, KnowledgeSearchHistory, KnowledgeSearchHistoryImage


def _not_referenced_clause():
    """「没有任何未删除的历史引用这张图」的相关子查询。

    ⚠️ 判据落在 ``knowledge_search_history.is_deleted`` 上，**不是关联行** ——
    关联行不做软删，脱离历史没有语义（见模型注释）。
    """
    return ~exists(
        select(1)
        .select_from(KnowledgeSearchHistoryImage)
        .join(
            KnowledgeSearchHistory,
            KnowledgeSearchHistory.id == KnowledgeSearchHistoryImage.history_id,
        )
        .where(
            KnowledgeSearchHistoryImage.image_id == KnowledgeQueryImage.id,
            KnowledgeSearchHistory.is_deleted == 0,
        )
    )


class KnowledgeQueryImageRepository(BaseRepository[KnowledgeQueryImage]):
    model = KnowledgeQueryImage

    def __init__(self, session: AsyncSession):
        super().__init__(session)

    # ── 读：归属与去重 ────────────────────────────────────────

    async def get_owned(
        self, tenant_id: str, user_id: str, image_id: str
    ) -> Optional[KnowledgeQueryImage]:
        """按 tenant + user + 未删除取图；不是本人的一律当作不存在（不区分 404 与 403）。"""
        stmt = select(KnowledgeQueryImage).where(
            KnowledgeQueryImage.id == image_id,
            KnowledgeQueryImage.tenant_id == self._tenant_id(tenant_id),
            KnowledgeQueryImage.user_id == user_id,
            KnowledgeQueryImage.is_deleted == 0,
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def find_by_sha(
        self, tenant_id: str, user_id: str, sha256: str
    ) -> Optional[KnowledgeQueryImage]:
        """同用户同内容只存一份（不去重跨用户）。"""
        stmt = select(KnowledgeQueryImage).where(
            KnowledgeQueryImage.tenant_id == self._tenant_id(tenant_id),
            KnowledgeQueryImage.user_id == user_id,
            KnowledgeQueryImage.sha256 == sha256,
            KnowledgeQueryImage.is_deleted == 0,
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    # ── 写：落盘（事务 ①） ────────────────────────────────────

    async def insert_if_absent(
        self,
        *,
        tenant_id: str,
        user_id: str,
        image_id: str,
        storage_backend: str,
        storage_key: str,
        mime_type: str,
        file_size: int,
        sha256: str,
        recognized_query: Optional[str],
        description: Optional[str],
        ocr_text: Optional[str],
        entities: list,
        prompt_version: Optional[str],
        vlm_model: Optional[str],
    ) -> tuple[KnowledgeQueryImage, bool]:
        """插入图片行；撞唯一索引（并发同图）时返回已存在的那行。

        返回 ``(行, 是否新建)``。``是否新建=False`` 表示**不要落盘对象**
        —— 已有行指向的对象是别人写的，重复 put 只会覆盖同一份键。

        用 ``ON CONFLICT DO NOTHING`` 而不是捕获 IntegrityError：
        PG 里一次约束冲突会把整个事务打成 aborted 状态，后续查询全失败，
        除非额外套 SAVEPOINT；ON CONFLICT 没有这个问题。
        """
        values = {
            "id": image_id,
            "tenant_id": self._tenant_id(tenant_id),
            "user_id": user_id,
            "storage_backend": storage_backend,
            "storage_key": storage_key,
            "mime_type": mime_type,
            "file_size": file_size,
            "sha256": sha256,
            "recognized_query": recognized_query,
            "description": description,
            "ocr_text": ocr_text,
            "entities": entities or [],
            "prompt_version": prompt_version,
            "vlm_model": vlm_model,
            # 语义是「对象存储里那份文件**已经**删掉了」，所以插入时恒为 0
            # （对象此刻可能还没写、或正在写）。这样任何一步中断留下的行都满足
            # 「行在、对象待清理」，由兜底扫描补删——删不存在的对象视为成功。
            "object_deleted": 0,
            "is_deleted": 0,
        }
        stmt = (
            insert(KnowledgeQueryImage)
            .values(**values)
            .on_conflict_do_nothing(
                index_elements=["tenant_id", "user_id", "sha256"],
                index_where=text("is_deleted = 0"),
            )
            .returning(KnowledgeQueryImage.id)
        )
        inserted = (await self.session.execute(stmt)).scalar_one_or_none()
        await self.session.flush()
        if inserted is not None:
            row = await self.get_owned(tenant_id, user_id, inserted)
            if row is not None:
                return row, True
        # 冲突：改用已存在的那行
        existing = await self.find_by_sha(tenant_id, user_id, sha256)
        if existing is None:
            # 极端竞态：对方刚被清理掉。让调用方按「这张跳过」处理。
            raise LookupError(f"query image conflict without existing row: {sha256[:12]}")
        return existing, False

    async def mark_object_deleted(self, image_id: str, tenant_id: str | None = None) -> None:
        """标记「对象已删除」。

        `tenant_id` 可选但**建议传**：id 是主键、本可唯一定位，但带上租户条件让
        「跨租户误写」在 SQL 层就不可能发生（纵深防御，与 `BaseRepository` 的
        `_tenant_conditions` 同一个口径）。
        """
        conditions = [KnowledgeQueryImage.id == image_id]
        if tenant_id is not None:
            conditions.append(KnowledgeQueryImage.tenant_id == self._tenant_id(tenant_id))
        await self.session.execute(
            update(KnowledgeQueryImage).where(*conditions).values(object_deleted=1)
        )
        await self.session.flush()

    async def soft_delete_unlinked(self, image_id: str, tenant_id: str, user_id: str) -> None:
        """best-effort 软删一张还没关联出去、也还没写成功的图。

        用于两条失败路径（put_bytes 失败、或事务 ② 失败）：
        留下的「无关联图片行」由兜底扫描按宽限期收走，这里只是早点收。
        """
        await self.session.execute(
            update(KnowledgeQueryImage)
            .where(
                KnowledgeQueryImage.id == image_id,
                KnowledgeQueryImage.tenant_id == self._tenant_id(tenant_id),
                KnowledgeQueryImage.user_id == user_id,
                KnowledgeQueryImage.is_deleted == 0,
                _not_referenced_clause(),
            )
            .values(is_deleted=1, purged_at=func.now())
        )
        await self.session.flush()

    async def update_recognition(
        self,
        image_id: str,
        *,
        tenant_id: str | None = None,
        recognized_query: Optional[str],
        description: Optional[str],
        ocr_text: Optional[str],
        entities: list,
        prompt_version: Optional[str],
        vlm_model: Optional[str],
    ) -> None:
        """提示词版本过旧时刷新识别字段（沿用同一个 image_id）。"""
        conditions = [KnowledgeQueryImage.id == image_id]
        if tenant_id is not None:
            conditions.append(KnowledgeQueryImage.tenant_id == self._tenant_id(tenant_id))
        await self.session.execute(
            update(KnowledgeQueryImage)
            .where(*conditions)
            .values(
                recognized_query=recognized_query,
                description=description,
                ocr_text=ocr_text,
                entities=entities or [],
                prompt_version=prompt_version,
                vlm_model=vlm_model,
            )
        )
        await self.session.flush()

    # ── 写：关联（事务 ②） ────────────────────────────────────

    async def touch(self, image_id: str, tenant_id: str | None = None) -> int:
        """续期「最后一次被引用」；条件更新，返回影响行数。

        返回 0 表示这张图**恰好被并发清理收走了**（``is_deleted`` 已置 1）——
        调用方应把该张从待关联列表里去掉，而不是继续写关联行（那会留下
        「关联到一张已删图」的死引用，虽然列表与查看都会过滤掉它）。
        """
        conditions = [
            KnowledgeQueryImage.id == image_id,
            KnowledgeQueryImage.is_deleted == 0,
        ]
        if tenant_id is not None:
            conditions.append(KnowledgeQueryImage.tenant_id == self._tenant_id(tenant_id))
        result = await self.session.execute(
            update(KnowledgeQueryImage).where(*conditions).values(updated_at=func.now())
        )
        await self.session.flush()
        return result.rowcount or 0

    async def add_links(
        self,
        tenant_id: str,
        user_id: str,
        history_id: str,
        image_ids: Sequence[str],
    ) -> int:
        """写关联行，``sort_order`` 即入参顺序（用户附图顺序）。重复 id 只关联一次。"""
        ordered: list[str] = []
        seen: set[str] = set()
        for image_id in image_ids:
            if image_id and image_id not in seen:
                seen.add(image_id)
                ordered.append(image_id)
        if not ordered:
            return 0
        values = [
            {
                "history_id": history_id,
                "image_id": image_id,
                "tenant_id": self._tenant_id(tenant_id),
                "user_id": user_id,
                "sort_order": idx,
            }
            for idx, image_id in enumerate(ordered)
        ]
        stmt = (
            insert(KnowledgeSearchHistoryImage)
            .values(values)
            .on_conflict_do_nothing(index_elements=["history_id", "image_id"])
        )
        await self.session.execute(stmt)
        await self.session.flush()
        return len(values)

    # ── 读：历史带图 ──────────────────────────────────────────

    async def list_for_histories(
        self, tenant_id: str, user_id: str, history_ids: Sequence[str]
    ) -> dict[str, list[KnowledgeQueryImage]]:
        """按历史批量取图（列表页一次查完，避免 N+1），按 ``sort_order`` 排序。

        已软删的图不出现在结果里（调用方无需再过滤）。
        """
        if not history_ids:
            return {}
        stmt = (
            select(KnowledgeSearchHistoryImage.history_id, KnowledgeQueryImage)
            .join(
                KnowledgeQueryImage,
                KnowledgeQueryImage.id == KnowledgeSearchHistoryImage.image_id,
            )
            .where(
                KnowledgeSearchHistoryImage.history_id.in_(list(history_ids)),
                KnowledgeSearchHistoryImage.tenant_id == self._tenant_id(tenant_id),
                KnowledgeSearchHistoryImage.user_id == user_id,
                KnowledgeQueryImage.tenant_id == self._tenant_id(tenant_id),
                KnowledgeQueryImage.user_id == user_id,
                KnowledgeQueryImage.is_deleted == 0,
            )
            .order_by(
                KnowledgeSearchHistoryImage.history_id,
                KnowledgeSearchHistoryImage.sort_order,
            )
        )
        out: dict[str, list[KnowledgeQueryImage]] = {}
        for history_id, image in (await self.session.execute(stmt)).all():
            out.setdefault(history_id, []).append(image)
        return out

    async def image_ids_of_histories(
        self, tenant_id: str, user_id: str, history_ids: Sequence[str]
    ) -> list[str]:
        """这些历史当前关联的全部图片 id（**不看图片是否已软删**，用于找候选清理对象）。"""
        if not history_ids:
            return []
        stmt = select(KnowledgeSearchHistoryImage.image_id).where(
            KnowledgeSearchHistoryImage.history_id.in_(list(history_ids)),
            KnowledgeSearchHistoryImage.tenant_id == self._tenant_id(tenant_id),
            KnowledgeSearchHistoryImage.user_id == user_id,
        )
        return list((await self.session.execute(stmt)).scalars())

    async def is_referenced_by_user(self, tenant_id: str, user_id: str, image_id: str) -> bool:
        """这张图是否仍被该用户的至少一条**未删除**历史引用。

        查看端点用它兜住兜底清理的延迟期：历史一删，图立刻不可见。
        """
        stmt = (
            select(
                exists(
                    select(1)
                    .select_from(KnowledgeSearchHistoryImage)
                    .join(
                        KnowledgeSearchHistory,
                        KnowledgeSearchHistory.id == KnowledgeSearchHistoryImage.history_id,
                    )
                    .where(
                        KnowledgeSearchHistoryImage.image_id == image_id,
                        KnowledgeSearchHistoryImage.tenant_id == self._tenant_id(tenant_id),
                        KnowledgeSearchHistoryImage.user_id == user_id,
                        KnowledgeSearchHistory.is_deleted == 0,
                    )
                )
            )
        )
        return bool((await self.session.execute(stmt)).scalar())

    # ── 写：释放（同步清理） ───────────────────────────────

    async def release_if_unreferenced(
        self, tenant_id: str, user_id: str, image_id: str
    ) -> Optional[tuple[str, str]]:
        """无未删除引用 → 一条语句里软删并返回对象位置；仍有引用 → 不动、返回 None。

        把「判断无引用」与「标记删除」合成一条 UPDATE（``NOT EXISTS`` 子查询），
        把与 ``bind_for_history`` 的竞争窗口压到最小 —— 但仍不归零：
        关联行刚写入、这条 UPDATE 已判定无引用的交错仍可能发生，
        结果是**新历史少一张图**（列表与查看都要求 ``is_deleted=0``，不会显示坏图、不会越权）。
        一期接受这个降级，不加行锁。

        返回 ``(storage_backend, storage_key)`` 时调用方负责删对象；删对象失败只打 warning，
        该行已是软删状态，由兜底扫描按 ``object_deleted=0`` 补删。
        """
        stmt = (
            update(KnowledgeQueryImage)
            .where(
                KnowledgeQueryImage.id == image_id,
                KnowledgeQueryImage.tenant_id == self._tenant_id(tenant_id),
                KnowledgeQueryImage.user_id == user_id,
                KnowledgeQueryImage.is_deleted == 0,
                _not_referenced_clause(),
            )
            .values(is_deleted=1, purged_at=func.now())
            .returning(KnowledgeQueryImage.storage_backend, KnowledgeQueryImage.storage_key)
        )
        row = (await self.session.execute(stmt)).first()
        await self.session.flush()
        return (row[0], row[1]) if row else None

    async def purge_if_unreferenced(self, image_id: str) -> Optional[tuple[str, str]]:
        """**兜底扫描专用**：无未删除引用 → 一条语句里软删并返回对象位置；否则 None。

        与 `release_if_unreferenced` 是同一个口径（「判断无引用」与「标记删除」合成一条
        带 `NOT EXISTS` 的条件 UPDATE），区别只有两点：
        - **不按 tenant / user 过滤** —— 兜底扫描是跨租户维护，调用方本来就只有一个 id；
        - 调用方拿到位置后去删对象，删成功才置 `object_deleted=1`；删失败就留着，
          下一轮由 `list_objects_pending_delete` 兜住。

        ⚠️ 顺序是**先条件软删、再删对象**，不能反过来：反过来的话，「扫出来无引用」
        与「对象已删」之间新建立的引用会指向一张对象已消失的图（历史里那张图直接不见），
        而先软删则会把这次竞争挡在 `NOT EXISTS` 上、本轮不动它。
        """
        stmt = (
            update(KnowledgeQueryImage)
            .where(
                KnowledgeQueryImage.id == image_id,
                KnowledgeQueryImage.is_deleted == 0,
                _not_referenced_clause(),
            )
            .values(is_deleted=1, purged_at=func.now())
            .returning(KnowledgeQueryImage.storage_backend, KnowledgeQueryImage.storage_key)
        )
        row = (await self.session.execute(stmt)).first()
        await self.session.flush()
        return (row[0], row[1]) if row else None

    async def delete_links_for_images(self, image_ids: Sequence[str]) -> int:
        """图片被清掉后顺带删它的关联行（只为控制表体积，关联行不参与可见性判断）。"""
        if not image_ids:
            return 0
        result = await self.session.execute(
            delete(KnowledgeSearchHistoryImage).where(
                KnowledgeSearchHistoryImage.image_id.in_(list(image_ids))
            )
        )
        await self.session.flush()
        return result.rowcount or 0

    async def delete_all_links_for_history(self, history_id: str) -> int:
        result = await self.session.execute(
            delete(KnowledgeSearchHistoryImage).where(
                KnowledgeSearchHistoryImage.history_id == history_id
            )
        )
        await self.session.flush()
        return result.rowcount or 0

    # ── 兜底扫描（跨租户维护） ───────────────────────────

    async def list_orphans(self, grace_minutes: int, limit: int = 200) -> list[dict[str, Any]]:
        """无未删除引用、且最后一次被引用已早于宽限期的图。

        **跨租户**：这是内部兜底巡检，不是用户请求路径（先例见
        ``document_repository.list_by_ontology_status_for_reconciliation``）。

        宽限期只用来避开「刚落盘、历史事务还没提交」的那个窗口 ——
        正常路径下图片与历史在事务 ①② 里前后提交，不会长时间无引用。
        """
        # ⚠️ 宽限期的**截止时刻在数据库端算**（`now() - interval`），不用 Python 的
        # `datetime.now()`：`updated_at` 有两处写入口 —— 插入时的列默认值与
        # `touch()` 的 `func.now()` —— 它们都取数据库时钟。若这里改用容器时钟去比，
        # 容器与 PG 时区/时钟一旦不一致，宽限期就会整体偏移（可能刚落的图立刻被收走，
        # 也可能永远收不走）。同一条时间轴上的比较才成立。
        cutoff = func.now() - func.make_interval(0, 0, 0, 0, 0, max(0, int(grace_minutes)))
        stmt = (
            select(
                KnowledgeQueryImage.id,
                KnowledgeQueryImage.tenant_id,
                KnowledgeQueryImage.user_id,
                KnowledgeQueryImage.storage_backend,
                KnowledgeQueryImage.storage_key,
            )
            .where(
                KnowledgeQueryImage.is_deleted == 0,
                KnowledgeQueryImage.updated_at < cutoff,
                _not_referenced_clause(),
            )
            .order_by(KnowledgeQueryImage.updated_at.asc())
            .limit(limit)
        )
        return [dict(row._mapping) for row in (await self.session.execute(stmt)).all()]

    async def list_objects_pending_delete(self, limit: int = 200) -> list[dict[str, Any]]:
        """已软删、但对象还没删掉的行（两个来源：release 删对象失败、put_bytes 失败后软删）。"""
        stmt = (
            select(
                KnowledgeQueryImage.id,
                KnowledgeQueryImage.tenant_id,
                KnowledgeQueryImage.storage_backend,
                KnowledgeQueryImage.storage_key,
            )
            .where(
                KnowledgeQueryImage.is_deleted == 1,
                KnowledgeQueryImage.object_deleted == 0,
            )
            .order_by(KnowledgeQueryImage.purged_at.asc().nulls_first())
            .limit(limit)
        )
        return [dict(row._mapping) for row in (await self.session.execute(stmt)).all()]

    async def purge_row(self, image_id: str) -> None:
        """兜底扫描判定：对象已确保删除 → 行置 ``object_deleted=1``。

        ``purged_at`` 只在首次软删时写；这里**不覆盖**它（那是「何时被判定无引用」的记账，
        重复扫描不该把它往后推）。
        """
        await self.session.execute(
            update(KnowledgeQueryImage)
            .where(KnowledgeQueryImage.id == image_id)
            .values(is_deleted=1, object_deleted=1)
        )
        await self.session.flush()

    @staticmethod
    def new_image_id() -> str:
        return uuid4().hex


__all__ = ["KnowledgeQueryImageRepository"]
