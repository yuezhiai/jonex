#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""跨数据源的抽象 schema（概念对齐层）ORM。

对应表：knowledge_base.ontology_abstract_schemas（迁移 040；042 列改名 concepts→schema）。

形态是**一库一份**：schema 里每个实体的每条 source 自带「这个实体在哪个数据源里
叫什么」，所以不需要额外一张映射表 —— 映射是实体的属性，不是独立实体。
"""
import copy

from sqlalchemy import Column, DateTime, Integer, String, text
from sqlalchemy.dialects.postgresql import JSONB

from jonex_core.common.database import Base
from jonex_core.common.entity import SoftDeleteMixin, TenantMixin, TimestampMixin


class OntologyAbstractSchema(TenantMixin, TimestampMixin, SoftDeleteMixin, Base):
    """一知识库一份的抽象 schema。

    生命周期：`proposed`（LLM 提的候选，**不参与检索**）→ `active`（人工确认）。
    源 schema 再生导致的「对齐指向不存在的实体」**不落库**：那是读时算的派生量
    （决策 7），在 `AbstractSchemaService.get()` 里现算现给。
    """

    __tablename__ = "ontology_abstract_schemas"
    __table_args__ = {"schema": "knowledge_base"}

    id = Column(String(64), primary_key=True)
    knowledge_base_id = Column(String(128), nullable=False, index=True)
    schema_version = Column(Integer, nullable=False, default=1)
    status = Column(String(32), nullable=False, default="proposed")
    # 列名显式传 `"schema"`：防 SQLAlchemy 关键字歧义（属性名与 DB 列名同形）。
    # `server_default` 与 040 的 `'[]'` 语义对齐 —— v2 空形状是对象（042 已同步
    # 改库端默认值）；`default=dict` 是 Python 侧默认（与 040 时代 `default=list` 对称）。
    schema = Column("schema", JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb"))
    source_hash = Column(String(64), nullable=True)
    edited_at = Column(DateTime(timezone=True), nullable=True)
    edited_by = Column(String(128), nullable=True)

    def to_dict(self) -> dict:
        # 对外不泄漏 tenant_id（与 OntologySynonym.to_dict 同口径：KB 级配置对象
        # 的调用方已经知道自己是谁，带上租户 ID 只是多一个可以被误用的字段）。
        # `or` 兜底不是防御性编程：`default=` 只在 flush 时生效，未落库的实例
        # 这几个字段是 None，而 to_dict 在 INSERT 之后就立刻被调（回给前端）。
        # schema 走 `copy.deepcopy`（与旧版 `list(...)` 的拷贝同意图）：直接把 ORM
        # 持有的那个 dict 交出去的话，调用方改一下嵌套的 entities/sources 就是**原地
        # 改 ORM 实例**，而 SQLAlchemy 的 JSON 跟踪不会把它标脏（改得进不了库、也没
        # 人知道改过）。浅拷贝挡不住嵌套层，所以必须是深拷贝。
        # ⚠ `list` 要**原样放行**：迁移窗口里列上还可能是 v1 的概念数组，`get()` 的
        # 过渡兼容分支（`isinstance(raw, list)` → 按迁移结果返回）靠它才拿得到原值；
        # 这里若把 list 一并吞成 `{}`，那条分支就是不可达死代码，API 会把一条有
        # 236 个概念的 v1 行显示成空 schema。其余类型（None / 标量）才回落 `{}`。
        return {
            "id": self.id,
            "knowledge_base_id": self.knowledge_base_id,
            "schema_version": self.schema_version or 1,
            "status": self.status or "proposed",
            "schema": (
                copy.deepcopy(self.schema)
                if isinstance(self.schema, (dict, list))
                else {}
            ),
            "source_hash": self.source_hash,
            "edited_at": self.edited_at.isoformat() if self.edited_at else None,
            "edited_by": self.edited_by,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class AbstractSchemaSaveHistory(Base):
    """抽象 schema 每次**公开保存**的内容快照（迁移 033 / update 085）。

    为什么需要它：`semantic_versions` 只记演化采纳版本，界面上的手动保存 / 确认 /
    导入 / 还原不留历史 —— 用户说「还原上一个版本」时无从恢复（2026-10-27 真实事故）。
    本表是**旁路**：写入失败只 warning，不影响保存本身。

    只在公开保存（`_carry_landing_slots=True`）时写；演化 apply / 版本恢复是权威
    内部写（False），它们已有 `semantic_versions`，这里不重复记。
    """

    __tablename__ = "abstract_schema_save_history"
    __table_args__ = {"schema": "knowledge_base"}

    id = Column(String(64), primary_key=True)
    tenant_id = Column(String(64), nullable=False)
    knowledge_base_id = Column(String(128), nullable=False)
    schema_version = Column(Integer, nullable=False)
    status = Column(String(32), nullable=False)
    # 与主表同形：显式列名 `"schema"` 防 SQLAlchemy 关键字歧义。
    schema = Column("schema", JSONB, nullable=False)
    saved_by = Column(String(128), nullable=True)
    # manual | restore；默认值同时由迁移的 server_default 兜底。
    source = Column(
        String(16), nullable=False, default="manual", server_default=text("'manual'"),
    )
    created_at = Column(
        DateTime(timezone=True), nullable=False, server_default=text("now()"),
    )


__all__ = ["OntologyAbstractSchema", "AbstractSchemaSaveHistory"]
