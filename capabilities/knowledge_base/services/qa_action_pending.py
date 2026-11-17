#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""问答动作提案的**服务端待办单**（一次性、可认领、可回写）。

为什么不在前端保存卡片状态
    写工具大多不幂等：刷新页面、多开标签页或从历史再次点执行都会重复提交。
    卡片必须在服务端有一条带状态的待办记录，执行前原子认领，执行结果由服务端回写。

生命周期
    pending ──claim──> executing ──record──> executed | failed
    - 认领失败（已认领 / 已终态 / 已过期）返回 1003；
    - executing 期间进程崩溃不自动释放，TTL 到期后卡片显示「结果未知」，
      不允许重试（对方系统可能已经执行过）；
    - 认领放在执行器所有前置检查之后、真正调用工具之前：缺参 / 未确认 /
      判定 manual/refuse / 工具不可用 / 契约变化 / 权限被拒都不消耗卡片。

存储
    Redis `ontology:qa_action_pending:{tenant}:{pending_id}`，TTL 默认 30 分钟。
    记录里含租户、提问人、KB、动作、工具、模型入参、契约哈希、状态、history_id。
    入参只存在 Redis 的短时记录里；检索历史只存键名与截断后的执行结果。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from jonex_core.common.cache import get_redis_client

logger = logging.getLogger(__name__)

ENV_PROPOSAL_ENABLED = "ONTOLOGY_QA_ACTION_PROPOSAL_ENABLED"
ENV_PENDING_TTL = "ONTOLOGY_QA_ACTION_PENDING_TTL"
DEFAULT_PENDING_TTL = 1800
_KEY_PREFIX = "ontology:qa_action_pending"

STATUS_PENDING = "pending"
STATUS_EXECUTING = "executing"
STATUS_EXECUTED = "executed"
STATUS_FAILED = "failed"
TERMINAL_STATUSES = frozenset({STATUS_EXECUTED, STATUS_FAILED})

# 只允许 pending → executing；executed/failed/executing 都不能再次认领。
_CLAIM_LUA = """
local raw = redis.call('GET', KEYS[1])
if not raw then return 0 end
local ok, obj = pcall(cjson.decode, raw)
if not ok or type(obj) ~= 'table' then return 0 end
if obj['status'] ~= 'pending' then return 0 end
obj['status'] = 'executing'
obj['claimed_at'] = ARGV[1]
redis.call('SET', KEYS[1], cjson.encode(obj), 'KEEPTTL')
return 1
"""


def proposal_enabled() -> bool:
    """问答动作提案总闸（默认关）；关掉即回滚到旧行为。"""
    return os.getenv(ENV_PROPOSAL_ENABLED, "false").lower() in ("1", "true", "yes", "on")


def pending_ttl() -> int:
    try:
        return max(60, min(24 * 3600, int(os.getenv(ENV_PENDING_TTL, str(DEFAULT_PENDING_TTL)))))
    except ValueError:
        return DEFAULT_PENDING_TTL


def _key(tenant_id: str, pending_id: str) -> str:
    return f"{_KEY_PREFIX}:{tenant_id}:{pending_id}"


def new_pending_id() -> str:
    return uuid.uuid4().hex


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def contract_hash(
    contract: dict, checklist: list[dict], tool_name: str = "", tool_source: str = "",
) -> str:
    """契约哈希：契约字段 + checklist 文本 + 工具目标。

    文档重新解析、绑定换工具都会改变哈希；执行时不一致就要求重新确认。
    """
    payload = {
        "contract": contract or {},
        "checklist": checklist or [],
        "tool_name": tool_name or "",
        "tool_source": tool_source or "",
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


async def create_pending(
    *,
    tenant_id: str,
    user_id: str,
    kb_id: str,
    action_name: str,
    tool_name: str,
    tool_source: str,
    inputs: dict,
    contract_hash_value: str,
    readable_input_keys: Optional[list[str]] = None,
) -> Optional[str]:
    """创建待办；Redis 不可用返回 None（调用方必须 fail closed，不出卡片）。"""
    pending_id = new_pending_id()
    record = {
        "pending_id": pending_id,
        "tenant_id": tenant_id,
        "user_id": str(user_id),
        "kb_id": kb_id,
        "action_name": action_name,
        "tool_name": tool_name,
        "tool_source": tool_source,
        "inputs": dict(inputs or {}),
        "input_keys": sorted(readable_input_keys or list((inputs or {}).keys())),
        "contract_hash": contract_hash_value,
        "status": STATUS_PENDING,
        "history_id": None,
        "created_at": _now(),
    }
    try:
        await get_redis_client().set(
            _key(tenant_id, pending_id),
            json.dumps(record, ensure_ascii=False, default=str),
            ex=pending_ttl(),
        )
        return pending_id
    except Exception as exc:  # noqa: BLE001 — 待办是安全边界，失败必须不出卡片
        logger.warning("[qa-action] 待办写入失败，按不可发起处理: %s", exc)
        return None


async def get_pending(tenant_id: str, pending_id: str) -> Optional[dict]:
    if not tenant_id or not pending_id:
        return None
    try:
        raw = await get_redis_client().get(_key(tenant_id, pending_id))
    except Exception as exc:  # noqa: BLE001
        logger.warning("[qa-action] 待办读取失败: %s", exc)
        return None
    if raw is None:
        return None
    try:
        value = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


async def bind_history(tenant_id: str, pending_ids: list[str], history_id: str) -> None:
    """历史保存后把 pending_id 绑到 history_id（结果回写时据此找历史行）。"""
    if not pending_ids or not history_id:
        return
    client = get_redis_client()
    for pending_id in pending_ids:
        record = await get_pending(tenant_id, pending_id)
        if record is None or record.get("history_id"):
            continue
        record["history_id"] = history_id
        try:
            await client.set(
                _key(tenant_id, pending_id),
                json.dumps(record, ensure_ascii=False, default=str),
                ex=pending_ttl(),
            )
        except Exception as exc:  # noqa: BLE001 — 绑定失败只影响历史回显
            logger.warning("[qa-action] 绑定历史失败 pending=%s: %s", pending_id, exc)


async def update_contract_hash(tenant_id: str, pending_id: str, new_hash: str) -> None:
    """契约变化时更新待办哈希：用户确认新 checklist 后再次执行即可认领。"""
    record = await get_pending(tenant_id, pending_id)
    if record is None or record.get("status") != STATUS_PENDING:
        return
    record["contract_hash"] = new_hash
    record["contract_updated_at"] = _now()
    try:
        await get_redis_client().set(
            _key(tenant_id, pending_id),
            json.dumps(record, ensure_ascii=False, default=str),
            ex=pending_ttl(),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[qa-action] 更新契约哈希失败 pending=%s: %s", pending_id, exc)


async def claim(tenant_id: str, pending_id: str) -> bool:
    """原子认领 pending → executing；只有第一个请求成功。"""
    if not tenant_id or not pending_id:
        return False
    try:
        result = await get_redis_client().eval(
            _CLAIM_LUA, 1, _key(tenant_id, pending_id), _now(),
        )
    except Exception as exc:  # noqa: BLE001 — 认领失败 fail closed
        logger.warning("[qa-action] 认领失败 pending=%s: %s", pending_id, exc)
        return False
    return bool(result)


async def mark_result(tenant_id: str, pending_id: str, status: str) -> Optional[dict]:
    """把待办置为终态（executed / failed）；返回更新前的记录。"""
    record = await get_pending(tenant_id, pending_id)
    if record is None:
        return None
    if record.get("status") not in (STATUS_PENDING, STATUS_EXECUTING):
        return record
    record["status"] = status if status in TERMINAL_STATUSES else STATUS_FAILED
    record["finished_at"] = _now()
    try:
        await get_redis_client().set(
            _key(tenant_id, pending_id),
            json.dumps(record, ensure_ascii=False, default=str),
            ex=pending_ttl(),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[qa-action] 待办终态写入失败 pending=%s: %s", pending_id, exc)
    return record


def build_card_view(
    plan: dict,
    *,
    pending_id: Optional[str] = None,
    can_execute: bool = False,
    inputs: Optional[dict] = None,
    status: Optional[str] = None,
    include_input_values: bool = True,
) -> dict:
    """给问答响应 / 历史 / 前端的脱敏卡片视图。

    ⚠️ 刻意不返回 binding_id / param_mapping / input_schema / parameters.resolved：
    卡片只需要动作名、工具显示名、checklist、必填入参描述与判定原因。
    """
    decision = plan.get("decision") or {}
    binding = plan.get("binding") or {}
    schema = binding.get("input_schema") or {}
    props = schema.get("properties") or {}
    required = set(schema.get("required") or [])
    required_inputs = [
        {
            "name": str(name),
            "type": str((meta or {}).get("type") or "string"),
            "description": str((meta or {}).get("description") or ""),
            "required": name in required,
        }
        for name, meta in props.items()
        if isinstance(meta, dict)
    ]
    values = dict(inputs or {})
    action = plan.get("action") or {}
    view = {
        "pending_id": pending_id,
        "kb_id": plan.get("knowledge_base_id"),
        "action": action.get("name"),
        "tool_label": binding.get("tool_label") or binding.get("tool_name"),
        "tool_source": binding.get("tool_source"),
        "verdict": decision.get("verdict"),
        # 契约上是否可执行（与「有没有待办 / 提问人有没有写权限」分开）
        "contract_executable": bool(decision.get("executable")),
        "executable": bool(decision.get("executable")) and bool(pending_id),
        "can_execute": bool(can_execute),
        "reasons": [
            str(r.get("message")) for r in decision.get("reasons") or []
            if isinstance(r, dict) and r.get("message")
        ],
        "checklist": [
            {"kind": c.get("kind"), "items": [str(i) for i in (c.get("items") or [])]}
            for c in decision.get("checklist") or []
            if isinstance(c, dict)
        ],
        "required_inputs": required_inputs,
        "provided_input_keys": sorted(values.keys()),
        "status": status or ("pending" if pending_id else "explanation"),
    }
    if include_input_values:
        view["inputs"] = values
    return view


__all__ = [
    "ENV_PENDING_TTL",
    "ENV_PROPOSAL_ENABLED",
    "STATUS_EXECUTED",
    "STATUS_EXECUTING",
    "STATUS_FAILED",
    "STATUS_PENDING",
    "bind_history",
    "build_card_view",
    "claim",
    "contract_hash",
    "create_pending",
    "get_pending",
    "mark_result",
    "new_pending_id",
    "pending_ttl",
    "proposal_enabled",
    "update_contract_hash",
]
