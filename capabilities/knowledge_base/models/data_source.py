#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""KB 数据源实例模型 — KnowledgeDataSource。"""
import uuid

from sqlalchemy import Column, DateTime, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB

from jonex_core.common.database import Base
from jonex_core.common.entity import SoftDeleteMixin, TenantMixin, TimestampMixin

# 敏感字段集合：出库前一律脱敏成 ***。
#
# `password_ref` 是数据库数据源的连接密码密文（`db_config.encrypt_config` 里
# `password` → `password_ref`）。**漏掉它等于把密文原样吐给前端**——而
# DATA_SOURCE_SECRET_KEY 常常没配（crypto.py 回退到 JWT_SECRET 派生），
# 那种情况下这份密文接近明文。以后新增加密字段时，记得同步加到这里。
#
# 连接池（DataSourceConnection）与本模型共用同一份——凭证口径分叉过一次，
# 就是「改密码只改一处」这个前提失效的开始。
SECRET_KEYS = (
    "token_ref",
    "credential_ref",
    "ingest_key_hash",
    "password_ref",
    # ② NL→SQL 用：只读凭据与写凭据**分开登记**（见 db_config.resolve_readonly_database_config）。
    # 脱敏看的是键名，加字段必须同时加进这里。
    "readonly_password_ref",
)


def mask_secrets(cfg: dict | None) -> dict:
    """顶层敏感字段 + 嵌套 `auth.token_ref` 脱敏，**返回新 dict**。

    返回新对象是刻意的：调用方（如列表回显把连接配置合并进引用行）不该
    因为脱敏就把手里的原始 config 改脏。

    但只保证到这一层：拷贝是**浅的**——顶层键可以放心改写，被脱敏的嵌套
    `auth` 换成了副本；其余嵌套值（子 dict / list）仍与入参共享同一个对象，
    别拿返回值去就地改它们（要深改请自己 deepcopy，本函数不做）。
    """
    cfg = dict(cfg or {})
    for k in SECRET_KEYS:
        if k in cfg:
            cfg[k] = "***"
    auth = cfg.get("auth")
    if isinstance(auth, dict) and auth.get("token_ref"):
        auth = dict(auth)
        auth["token_ref"] = "***"
        cfg["auth"] = auth
    return cfg


class KnowledgeDataSource(TenantMixin, TimestampMixin, SoftDeleteMixin, Base):
    """知识库数据源实例（api / api_push / storage / file / database / etl）。"""

    __tablename__ = "knowledge_data_sources"
    __table_args__ = {"schema": "knowledge_base"}

    id = Column(String(64), primary_key=True, default=lambda: uuid.uuid4().hex)
    knowledge_base_id = Column(String(128), nullable=False, index=True)
    access_method_id = Column(String(64), nullable=True)
    access_type = Column(String(32), nullable=False)
    name = Column(String(255), nullable=False)
    config_json = Column(JSONB, nullable=False, default=dict)
    # 非空 = 引用平台连接池里的连接，配置（含凭证）从那边读；
    # NULL = 存量内联数据源，config_json 自带完整配置。两条路并存，见 039 迁移。
    connection_id = Column(String(64), nullable=True, index=True)
    sync_mode = Column(String(16), nullable=False, default="manual")
    cron_expr = Column(String(128), nullable=True)
    schedule_task_id = Column(Integer, nullable=True)
    status = Column(String(32), nullable=False, default="active")
    last_sync_at = Column(DateTime(timezone=True), nullable=True)
    last_sync_status = Column(String(32), nullable=True)
    last_sync_message = Column(Text, nullable=True)
    document_count = Column(Integer, nullable=False, default=0)

    _SECRET_KEYS = SECRET_KEYS

    def to_dict(self, *, reveal: bool = False) -> dict:
        cfg = dict(self.config_json or {}) if reveal else mask_secrets(self.config_json)
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "knowledge_base_id": self.knowledge_base_id,
            "access_method_id": self.access_method_id,
            "access_type": self.access_type,
            "name": self.name,
            "config_json": cfg,
            "connection_id": self.connection_id,
            "sync_mode": self.sync_mode,
            "cron_expr": self.cron_expr,
            "schedule_task_id": self.schedule_task_id,
            "status": self.status,
            "last_sync_at": self.last_sync_at.isoformat() if self.last_sync_at else None,
            "last_sync_status": self.last_sync_status,
            "last_sync_message": self.last_sync_message,
            "document_count": self.document_count or 0,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


__all__ = ["KnowledgeDataSource", "SECRET_KEYS", "mask_secrets"]
