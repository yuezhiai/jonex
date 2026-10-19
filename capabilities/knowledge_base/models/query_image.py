#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""Knowledge Base — 以图问知识的查询图片模型。

对应表：
- ``knowledge_base.knowledge_query_images``          查询图片本体
- ``knowledge_base.knowledge_search_history_images`` 历史 ⇄ 图片 多对多关联

设计：docs/knowledge-base/image-query-search-execution-plan.md §4-B10 / §4-B11
"""

from uuid import uuid4

from sqlalchemy import Column, DateTime, Index, Integer, SmallInteger, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.sql import func

from jonex_core.common.database import Base
from jonex_core.common.entity import SoftDeleteMixin, TenantMixin


class KnowledgeQueryImage(Base, TenantMixin, SoftDeleteMixin):
    """以图问知识的查询图片（按 tenant + user 隔离）。

    生命周期与历史绑定：
    - **只在保存历史时落盘** —— 识别阶段仅暂存在 Redis（TTL 到期即消失）；
    - 存活判据是「至少还有一条未删除的历史引用它」，没有就删对象 + 软删该行；
    - 可见范围跟随历史（= 本人），故查询一律带 user_id。

    ``updated_at`` 不只是记账：它是「最后一次被引用」的时刻，兜底扫描
    （``sweep_orphans``）用它判断宽限期，避免收走刚落盘、历史事务还没提交的图。
    """

    __tablename__ = "knowledge_query_images"
    __table_args__ = (
        # 同用户同图只存一份；带 is_deleted=0 的部分索引让软删过的旧图不挡新图落盘
        # （与 update/045 + migrations/004 里的同名索引一致）
        Index(
            "uq_kb_qimg_tenant_user_sha",
            "tenant_id", "user_id", "sha256",
            unique=True,
            postgresql_where=text("is_deleted = 0"),
        ),
        Index(
            "idx_kb_qimg_sweep",
            "updated_at",
            postgresql_where=text("is_deleted = 0"),
        ),
        Index(
            "idx_kb_qimg_pending_object_delete",
            "purged_at",
            postgresql_where=text("is_deleted = 1 AND object_deleted = 0"),
        ),
        {"schema": "knowledge_base"},
    )

    id = Column(String(64), primary_key=True, default=lambda: str(uuid4().hex))
    # ⚠️ **有意不用 `TimestampMixin`**：它给的是 Python 侧默认值（容器时钟），
    # 而这一列还有另一个写入口 —— `touch()` / `purge_if_unreferenced()` 用的是
    # 数据库的 `func.now()`。同一列上两个时钟，兜底扫描的「多久算过期」就不可靠了
    # （容器与 PG 时区不一致时宽限期直接失效）。这里统一交给数据库，
    # 与 DDL 的 `DEFAULT CURRENT_TIMESTAMP` 也是同一口径。
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)
    # 索引不在这里开 `index=True`：user_id / sha256 的查询一律带 tenant，由上面的
    # uq_kb_qimg_tenant_user_sha 覆盖；另开会让 create_all 多建两条 DDL 没有的索引
    # （task_center 同款约定，见 capabilities/task_center/models/definition.py）。
    user_id = Column(String(128), nullable=False)
    storage_backend = Column(String(16), nullable=False)
    storage_key = Column(String(512), nullable=False)
    mime_type = Column(String(64), nullable=False)
    file_size = Column(Integer, nullable=False)
    sha256 = Column(String(64), nullable=False)
    recognized_query = Column(Text, nullable=True)
    description = Column(Text, nullable=True)
    ocr_text = Column(Text, nullable=True)
    entities = Column(JSONB, nullable=False, default=list)
    prompt_version = Column(String(16), nullable=True)
    vlm_model = Column(String(128), nullable=True)
    # 被判定无引用、软删的时刻（兜底扫描据此补删对象）
    purged_at = Column(DateTime, nullable=True)
    # 对象存储里的文件已删除为 1；软删后删对象失败时保持 0
    object_deleted = Column(SmallInteger, nullable=False, default=0)

    def to_ref(self) -> dict:
        """历史条目上的图片引用（``view_url`` 由 Gateway 补签，此处不给）。"""
        return {
            "id": self.id,
            "mime_type": self.mime_type,
            "file_size": self.file_size or 0,
            "view_url": None,
        }


class KnowledgeSearchHistoryImage(Base, TenantMixin):
    """检索历史 ⇄ 查询图片 多对多关联。

    一条历史可以附多张图，一张图也可以被多条历史引用（历史「重新检索」、同图再搜）。

    ⚠️ **关联行不做软删**：引用是否有效**一律以 ``knowledge_search_history.is_deleted``
    为准**（关联行脱离历史没有语义）。所以「这张图还有人引用吗」问的是
    join 之后的未删除历史行，而不是关联行本身。

    ⚠️ 复合主键 ``(history_id, image_id)``，**没有 id 列** ——
    ``BaseRepository`` 里依赖 ``_primary_key()`` 的方法（``get_by_id`` 等）对本模型不可用，
    仓储方法一律显式写查询。
    """

    __tablename__ = "knowledge_search_history_images"
    __table_args__ = (
        Index("idx_kb_hist_img_image", "image_id"),
        Index("idx_kb_hist_img_history", "history_id"),
        {"schema": "knowledge_base"},
    )

    history_id = Column(String(64), primary_key=True)
    image_id = Column(String(64), primary_key=True)
    user_id = Column(String(128), nullable=False)
    sort_order = Column(SmallInteger, nullable=False, default=0)
    created_at = Column(DateTime, nullable=False, default=func.now())


__all__ = ["KnowledgeQueryImage", "KnowledgeSearchHistoryImage"]
