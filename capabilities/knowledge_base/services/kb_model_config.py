#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""KB 级模型配置的**唯一定义处**（SSOT）+ 解析判定。

为什么单独一个模块
------------------
scope 的展示名、适用 KB 类型、回落到哪个引擎变量，如果分散在
service / capability / 前端三处，必然出现"页面改名了、后端还在校验旧名"这种
互相矛盾的状态。这里集中定义，其他层只引用。

生效语义（设计 §2）
-------------------
1. 有 KB 覆盖 → 下发该模型名；
2. 无 KB 覆盖 → **不下发任何模型字段**，由引擎沿用自己现有的默认链
   （atomic-rag：模型注册表 → ``VLM_MODEL_NAME``；jonex-wiki：``.env.jonex_wiki`` 的 ``LLM_MODEL``）。
   平台不把 env 值显式下发 —— 两个容器的 env 不同源，下发就等于替引擎"决定"了默认值。

"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Iterable

#: KB 类型 → 走向量（LightRAG）还是 wiki 产物。与 kb_type_service.LLM_WIKI_TYPES 同源，
#: 这里不 import 那个模块以免循环依赖（它 import 的是 service 层）。
WIKI_KB_TYPES = frozenset({"openkb", "jonex_wiki"})


@dataclass(frozen=True)
class ScopeDef:
    """一个可配置的模型位。"""

    scope: str
    label_key: str
    label: str
    stage: str  # parse | compile
    #: 无覆盖时引擎侧真正生效的变量名（仅用于展示"部署默认来自哪里"，不下发）
    env_var: str
    #: 该 scope 对哪些 KB 类型可见（空 = 全部可见，再按 is_configurable 细化）
    kb_types: frozenset[str] = field(default_factory=frozenset)
    #: 即使可见也不可写（如只读展示 scope）
    reason_not_configurable: str = ""


#: 可写 scope（顺序即展示顺序）
SCOPE_DEFS: dict[str, ScopeDef] = {
    "parse_multimodal": ScopeDef(
        scope="parse_multimodal",
        label_key="kbModel.scope.parse_multimodal",
        label="多模态理解",
        stage="parse",
        env_var="VLM_MODEL_NAME",
    ),
    "compile_ontology": ScopeDef(
        scope="compile_ontology",
        label_key="kbModel.scope.compile_ontology",
        label="本体抽取",
        stage="compile",
        env_var="ONTOLOGY_LLM_MODEL",
        kb_types=frozenset({"lightrag"}),
    ),
    "compile_wiki": ScopeDef(
        scope="compile_wiki",
        label_key="kbModel.scope.compile_wiki",
        label="Wiki 编译",
        stage="compile",
        env_var="LLM_MODEL",
        kb_types=frozenset({"jonex_wiki"}),
    ),
}

#: 只读展示位（不属于可写 scope，也不落库）
EMBEDDING_SCOPE = "embedding"
EMBEDDING_LABEL_KEY = "kbModel.scope.embedding"
EMBEDDING_LABEL = "向量化"
EMBEDDING_ENV_VAR = "EMBEDDING_MODEL"

#: Rerank 只读展示位：同 embedding，属「部署侧统一配置」，不提供 KB 级覆盖。
#: 变量取 `RERANK_MODEL` 而不是网关侧的 `LLMGW_RERANK_MODEL` —— 检索时平台会把这个
#: 模型名**随请求下发**（`jonex_core/common/rerank.py` 模块级读它），所以它才是
#: 实际生效的那个；网关侧同名变量只在请求不带 model 时才兜底。
RERANK_SCOPE = "rerank"
RERANK_LABEL_KEY = "kbModel.scope.rerank"
RERANK_LABEL = "Rerank 重排"
RERANK_ENV_VAR = "RERANK_MODEL"
#: 与解析/编译分开成组：重排发生在检索期，不属于这两步任何一步
RERANK_STAGE = "retrieve"


def visible_scopes(kb_type: str) -> list[ScopeDef]:
    """按 KB 类型给出可见的**可写** scope（顺序即展示顺序）。"""
    visible: list[ScopeDef] = []
    for scope_def in SCOPE_DEFS.values():
        if scope_def.kb_types and kb_type not in scope_def.kb_types:
            continue
        visible.append(scope_def)
    return visible


def show_embedding_row(kb_type: str) -> bool:
    """向量化只读行：只有真的走向量检索的 KB 才显示（wiki 链路无向量）。"""
    return kb_type not in WIKI_KB_TYPES


def show_rerank_row(kb_type: str) -> bool:
    """Only vector-based KBs use the platform reranker; Wiki uses lexical search."""
    return kb_type not in WIKI_KB_TYPES


def is_configurable(scope_def: ScopeDef, kb_type: str, ingest_backend: str = "") -> bool:
    """该 scope 在这个 KB 上是否可写。"""
    return True


def not_configurable_reason(scope_def: ScopeDef, kb_type: str, ingest_backend: str = "") -> str:
    return ""


def configurable_scopes(kb_type: str, ingest_backend: str = "") -> frozenset[str]:
    """该 KB 上**允许写入**的 scope 集合（校验用）。"""
    return frozenset(
        scope_def.scope
        for scope_def in visible_scopes(kb_type)
        if is_configurable(scope_def, kb_type, ingest_backend)
    )


def env_default(scope: str) -> str | None:
    """部署侧默认值（只用于展示，不下发）。"""
    if scope == EMBEDDING_SCOPE:
        return os.getenv(EMBEDDING_ENV_VAR) or None
    if scope == RERANK_SCOPE:
        return os.getenv(RERANK_ENV_VAR) or None
    scope_def = SCOPE_DEFS.get(scope)
    if scope_def is None:
        return None
    return os.getenv(scope_def.env_var) or None


def resolve_for_dispatch(
    overrides: dict[str, str],
    *,
    kb_type: str = "",
    ingest_backend: str = "",
) -> dict[str, str]:
    """派活时决定"下发什么"。

    - 只返回**有 KB 覆盖**且该 scope 对该 KB 有意义的值；
    - 无覆盖的 scope 不出现在结果里 —— 调用方据此"不带模型字段"。

    为什么没有灰度开关：默认状态本来就是惰性的（没配置 = 一行都不下发），
    多一个开关只是多一种"页面看起来坏了"的故障态（2026-10 因此排查过两次）。
    """
    result: dict[str, str] = {}
    for scope, model_name in (overrides or {}).items():
        value = (model_name or "").strip()
        if not value:
            continue
        if scope not in configurable_scopes(kb_type, ingest_backend):
            continue
        result[scope] = value
    return result


def scopes_to_keys(overrides: Iterable[str]) -> dict[str, str]:
    """scope 集合 → 链路键名映射（P2/P3 落地时用；P1 只保证定义唯一）。"""
    mapping = {
        "parse_multimodal": "kb_vlm_override",
        "compile_ontology": "ontology_model",
        "compile_wiki": "compile_model",
    }
    return {scope: mapping[scope] for scope in overrides if scope in mapping}


__all__ = [
    "EMBEDDING_ENV_VAR",
    "EMBEDDING_LABEL",
    "EMBEDDING_LABEL_KEY",
    "EMBEDDING_SCOPE",
    "SCOPE_DEFS",
    "ScopeDef",
    "WIKI_KB_TYPES",
    "configurable_scopes",
    "env_default",
    "is_configurable",
    "not_configurable_reason",
    "resolve_for_dispatch",
    "scopes_to_keys",
    "show_embedding_row",
    "visible_scopes",
]
