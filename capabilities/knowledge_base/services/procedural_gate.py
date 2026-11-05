"""
库级 procedural（Skill/Action 规程）抽取开关的派单接线。

为什么需要它
    procedural 子管线原先的每文档触发条件是「compiled schema 含 Action」，而内置 TBox
    都已声明 Action ⇒ 打开进程级总闸 ONTOLOGY_PROCEDURAL_EXTRACT_ENABLED 就是全量打开。
    这里把库级开关 knowledge_info.procedural_extract 在**派单那一刻**读出来，写进
    下发给执行方的 schema 副本的 prompt_schema 顶层（键 PROCEDURAL_GATE_KEY）。
    执行方把 prompt_schema 原样作为 tbox 交给 NativeOntologyExtractor，抽取器见到值
    为 True 才跑 procedural（再与总闸、schema 含 Action AND）。

不改库里存的 compiled schema
    只改派单用的副本：开关不进 schema 就不触发 schema_version bump / 文档 fencing，
    切开关也不会连带让全库文档重抽。

fail-closed
    读列失败 / 查不到 / NULL 一律按「不参与」处理：这是增量 LLM 成本开关，
    读失败时宁可少抽也不能意外全量打开。
"""

from __future__ import annotations

import logging
import os
from typing import Optional

from jonex_core.capability.atomic.rag.ontology_extractor import PROCEDURAL_GATE_KEY
logger = logging.getLogger(__name__)

#: 进程级总闸。真正读它的是执行方（atomic-rag / ontology-compile 的抽取器）；
#: compose 下三个容器都从 deploy/.env 取值（kb-service 经 env_file，执行方经插值），
#: 所以 kb-service 读到的值可用来在界面上提示「总闸是否打开」。
MASTER_SWITCH_ENV = "ONTOLOGY_PROCEDURAL_EXTRACT_ENABLED"


def procedural_master_switch_on() -> bool:
    """总闸是否打开（与抽取器 `_env_flag` 同口径，缺省关）。仅供展示，不参与派单判定。"""
    return os.getenv(MASTER_SWITCH_ENV, "false").lower() in ("1", "true", "yes", "on")


def attach_procedural_gate(schema: Optional[dict], enabled: bool) -> Optional[dict]:
    """返回带库级开关的 schema 副本（不修改入参）；schema 为空原样返回。

    无论开关值如何都显式写入：库里存的 prompt_schema 若残留了这个键（理论上不会），
    也会被这一次派单的真实值覆盖。
    """
    if not schema:
        return schema
    out = dict(schema)
    prompt_schema = dict(out.get("prompt_schema") or {})
    prompt_schema[PROCEDURAL_GATE_KEY] = bool(enabled)
    out["prompt_schema"] = prompt_schema
    return out


async def with_procedural_gate(
    schema: Optional[dict], tenant_id: str, kb_id: str,
) -> Optional[dict]:
    """读库级开关并附到派单 schema 副本上（fail-closed）。"""
    if not schema:
        return schema
    enabled = False
    try:
        from .kb_type_service import get_kb_procedural_extract

        enabled = await get_kb_procedural_extract(tenant_id, kb_id)
    except Exception as exc:  # noqa: BLE001 — 增量成本开关，读失败按关处理
        logger.warning(
            "读取库级 procedural 开关失败，按关处理: kb=%s err=%s", kb_id, exc,
        )
    return attach_procedural_gate(schema, enabled)
