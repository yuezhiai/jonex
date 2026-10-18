# -*- coding: utf-8 -*-
"""[jonex] 命名空间 token 注入（跨仓契约 3）。

自 raganything/pipeline/stages.py L1228-1242 逐字搬迁（P1，行为不变）。
定义方迁到平台侧后，raganything 的 push 阶段（full 链路，P4 退役）从本模块 import。
"""
from __future__ import annotations

import hashlib


def _inject_ns_token(
    text: str, tenant_id: str, kb_id: str, document_id: str
) -> str:
    """[jonex] 注入命名空间 token，使 chunk 内容 hash 带上 (tenant, kb, doc) 维度。

    对齐 v1 LightRAGAdapter 的行为（原 lightrag_adapter.py 上传前注入）：
    LightRAG 按 chunk 内容 hash 全局去重，跨文档/跨 KB 的相同文本会被去重合并，
    导致只有首篇文档抽到实体、其余文档图为空（本体阶段"无候选实体"→失败）。
    在文本末尾追加短 hash 的 HTML 注释 marker，使每个 (tenant, kb, doc) 拥有独立
    抽取记录、保证 ABox 隔离；同文档幂等重传时 token 相同 → hash 不变 → 仍可去重，
    不浪费 LLM。消费侧（task_manager 本体抽取）按 `<!--yx:[a-f0-9]{8}-->` 过滤该 token。
    """
    ns_raw = f"{tenant_id}|{kb_id}|{document_id}"
    ns_hash = hashlib.md5(ns_raw.encode()).hexdigest()[:8]
    return text + f"\n<!--yx:{ns_hash}-->"


# [jonex] P1：本模块的对外名。函数体按 L1228-1242 逐字搬迁、保留原私有名
# `_inject_ns_token`；raganything 侧以 `inject_ns_token as _inject_ns_token`
# 别名回原名，stages.py 的 5 处调用点与 ingest.py 均不改。
inject_ns_token = _inject_ns_token
