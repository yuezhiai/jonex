# [jonex] 知识库类型查询 — 公共 helper
"""统一查询 KnowledgeInfo.kb_type（单 KB + 批量）+ 本部署「允许创建哪些类型」。

替代此前分散在 4 个 service（document / search / ontology / parse_result）
中各写一份的 _get_pipeline_type。原名 pipeline_type，迁移后重命名为 kb_type。

⚠ **这里有三个集合，别混**（混了会出两类错：把 lightrag 放进 llm-wiki 语义、
或把「运维配置禁用」误报成「调用方拼错」）::

    VALID_KB_TYPES      代码**认识**哪些类型       静态，随代码走
    LLM_WIKI_TYPES      其中哪些是 **wiki 语义**    静态，随代码走（跳过本体、走 LLM 编译）
    ENABLED_KB_TYPES    本部署**允许创建**哪些      动态，随 env KB_ENABLED_TYPES 走

不变式：``ENABLED_KB_TYPES ⊆ VALID_KB_TYPES``。

**第三个只作用于「创建」一个动作** —— 它不参与任何链路分流判定，
也**不隐藏存量库**：被禁类型的老库照常出现在列表里、照常可读可检索可上传，
只是不能再造新的。所以它绝不能拿去替换 `LLM_WIKI_TYPES` 参与分流。
"""
import os
from typing import Any, Optional

from sqlalchemy import select

from jonex_core.common.database import get_db_session
from jonex_core.common.exceptions import InvalidParameterError
from jonex_core.common.i18n import translate

from ..models.knowledge_info import KnowledgeInfo

DEFAULT_KB_TYPE = "lightrag"

#: 代码**认识**的类型。**声明顺序 = 前端选项顺序**（canonical order）——
#: 故意不用 env 里手写的顺序，否则运维换个写法前端选项就重排。
KB_TYPE_ORDER: tuple[str, ...] = ("lightrag", "openkb", "jonex_wiki")

VALID_KB_TYPES = frozenset(KB_TYPE_ORDER)  # [jonex] 创建白名单（三引擎，代码级）
# [jonex] llm-wiki 子集（两引擎：openkb/jonex_wiki）。区别于 VALID_KB_TYPES——
# 后者含 lightrag，会把 lightrag 误放进 llm-wiki schema / parse result 覆盖层 / 跳过 Neo4j 清理。
LLM_WIKI_TYPES = frozenset({"openkb", "jonex_wiki"})

#: 部署级「允许创建哪些类型」的白名单 env（逗号分隔）。
#: 落点：`deploy/.env`（知识库容器 `env_file: - .env`，**不需要改 docker-compose.yml**）。
KB_ENABLED_TYPES_ENV = "KB_ENABLED_TYPES"


def parse_enabled_kb_types(raw: str | None) -> frozenset[str]:
    """解析 ``KB_ENABLED_TYPES``；**纯函数**（不读 env，便于单测）。

    规则：

    * 未设置 / 留空 / 只有逗号 → 返回 ``VALID_KB_TYPES``（**全开**）。
      这是「合入零影响」的根据：不配这个变量的部署行为与从前完全一致。
    * 逗号分隔，去空格、转小写、去重。
    * **出现认识之外的值就整条配置无效**（抛 ``ValueError``）—— 不是「合法的那半照用」。
      静默丢一半的失败方式最难查：客户现场少显示一个选项，没人知道为什么。
      ``jonex-wiki``（连字符）是最可能的那一类笔误，必须在启动时就炸出来。

    注意：**无法表达「一个都不给」** —— 空值一律读作「没限制」。真要全关需要另立开关，
    不靠这个变量（把「空」同时读成「全开」和「全关」是个必然踩的坑）。
    """
    text = (raw or "").strip()
    if not text:
        return VALID_KB_TYPES
    values = {token.strip().lower() for token in text.split(",") if token.strip()}
    if not values:  # 只有逗号 / 全是空白：同样读作「没限制」，避免出现空集
        return VALID_KB_TYPES
    unknown = sorted(values - VALID_KB_TYPES)
    if unknown:
        raise ValueError(
            f"{KB_ENABLED_TYPES_ENV} 取值非法：{'、'.join(unknown)}"
            f"（可选值：{'、'.join(KB_TYPE_ORDER)}；多个用逗号分隔）"
        )
    return frozenset(values)


def enabled_kb_types() -> frozenset[str]:
    """本部署允许创建的类型集合（每次读 env，与 ``ingest_backend.current_backend`` 同款）。

    读时求值而不是模块级缓存：env 是启动时定形的，两者等价；但读时求值让
    `monkeypatch.setenv` 能直接生效、不必重载模块。
    """
    return parse_enabled_kb_types(os.environ.get(KB_ENABLED_TYPES_ENV))


def kb_type_options() -> dict[str, Any]:
    """下发给前端的「可创建类型」选项 —— **只给 code，不给文案**。

    i18n 的 label/description 留在前端（`locales/{zh,en}.json` 本来就有一份），
    后端掺 UI 文本只会变成第二个副本。

    ``default`` = 按 canonical order 的第一个可用类型：前端拿它当弹窗初始值。
    写死 ``lightrag`` 会做出「一打开弹窗就是个必被拒的默认值」。
    """
    ordered = [t for t in KB_TYPE_ORDER if t in enabled_kb_types()]
    return {"kb_types": ordered, "default": ordered[0]}


def normalize_kb_type(raw: str | None) -> str:
    """把 ``kb_type`` 收口成 canonical 形态：去首尾空白 + 小写。

    **只做大小写/空白归一，不做别名猜测**（``jonex-wiki`` 不会被悄悄改成
    ``jonex_wiki``）：``kb_type`` 创建后不可变，猜错代价高；未知值仍由
    ``resolve_creatable_kb_type`` 明确拒绝。读取侧同样走这里，避免历史库里的
    `` OpenKB `` 被当成未知类型、落进 lightrag 分支。
    """
    return (raw or "").strip().lower()


def resolve_creatable_kb_type(raw: str | None) -> str:
    """把请求里的 ``kb_type`` 解析成本次**允许创建**的类型；不允许则抛业务异常。

    **两级校验，两个错误码**（成因不同就要能分开，否则排查时又回到「一句话盖住所有原因」）：

    * 值**不认识**（调用方拼错）→ ``err.kb.invalid_kb_type``
    * 值认识但**本部署禁用**（运维配置）→ ``err.kb.kb_type_disabled``

    缺省值也走同一道闸门：``kb_type`` 创建后不可变，**不能替用户改选**成别的类型。
    大小写与首尾空白先归一（`` OpenKB `` → ``openkb``），但不接受连字符等别名。
    """
    value = normalize_kb_type(raw) or DEFAULT_KB_TYPE
    if value not in VALID_KB_TYPES:
        raise InvalidParameterError(
            message=translate(
                "err.kb.invalid_kb_type",
                params={"value": value, "valid": ", ".join(KB_TYPE_ORDER)},
                fallback=f"知识库类型不合法：{value}，可选值：{', '.join(KB_TYPE_ORDER)}",
            ),
        )
    enabled = enabled_kb_types()
    if value not in enabled:
        allowed = [t for t in KB_TYPE_ORDER if t in enabled]
        raise InvalidParameterError(
            message=translate(
                "err.kb.kb_type_disabled",
                params={"value": value, "enabled": ", ".join(allowed)},
                fallback=f"知识库类型 {value} 在本环境已被禁用，可用值：{', '.join(allowed)}",
            ),
        )
    return value


async def get_kb_type(tenant_id: str, kb_id: str) -> str:
    """查询单个 KB 的 kb_type，未命中/空 kb_id → 'lightrag'。"""
    if not kb_id:
        return DEFAULT_KB_TYPE
    async with get_db_session() as session:
        result = await session.execute(
            select(KnowledgeInfo.kb_type).where(
                KnowledgeInfo.tenant_id == tenant_id,
                KnowledgeInfo.id == kb_id,
                KnowledgeInfo.is_deleted == 0,
            )
        )
        row = result.first()
    return normalize_kb_type(row[0]) if row else DEFAULT_KB_TYPE


async def get_kb_type_or_none(tenant_id: str, kb_id: str) -> Optional[str]:
    """查单个 KB 的 `kb_type`；**查不到就返回 `None`，不回落 `lightrag`**（2026-10-09 加）。

    ⚠️ **和 `get_kb_type` 的唯一差别就是这个「查不到怎么办」**，但那一处差别在某些调用点
    上很要命：`get_kb_type` 查不到会给 `lightrag`，而 **wiki 类文档的 `ontology_status`
    被上传路径写成 `READY`** —— 拿回落值去选「这一步的终态从哪一列看」时，一份 wiki 文档
    会落进 lightrag 那条分支、被当场判成「编译成功」，而 Wiki 根本没生成。
    所以**凡是要用 kb_type 判分流、判终态来源**的地方用这个严格版；只是「按哪条管线读」
    这类宽松场景继续用 `get_kb_type`（它的回落是刻意的）。
    """
    if not kb_id:
        return None
    async with get_db_session() as session:
        result = await session.execute(
            select(KnowledgeInfo.kb_type).where(
                KnowledgeInfo.tenant_id == tenant_id,
                KnowledgeInfo.id == kb_id,
                KnowledgeInfo.is_deleted == 0,
            )
        )
        row = result.first()
    return (normalize_kb_type(row[0]) or None) if row else None


async def get_kb_ingest_backend(tenant_id: str, kb_id: str) -> Optional[str]:
    """查单个 KB 的**库级派单值**（`knowledge_info.ingest_backend`，2026-10-09 加）。

    **查不到 / 列是 NULL 都返回 `None`，绝不回落 `rag`** —— 与 `get_kb_type` 的
    回落语义正好相反：`None` 在这个字段上是一个**合法值**，表示「这个库没表态、
    跟随服务端开关 `KB_INGEST_BACKEND`」。回落 `rag` 会把「没表态」读成「明确要
    rag」—— 判据就少了一层，库级设置从此只是「碰巧与回落值相同」时才成立。
    所以**不要**好心给它加 `or "rag"` 之类的默认值。

    消费方是 `ingest_backend.resolve_backend(kb_type, storage_backend, kb_backend)`
    （库值层：仅剩的存储硬回落之下、env 之上；P4b 2026-10-17 起 openkb 边界已移除）。
    """
    if not kb_id:
        return None
    async with get_db_session() as session:
        result = await session.execute(
            select(KnowledgeInfo.ingest_backend).where(
                KnowledgeInfo.tenant_id == tenant_id,
                KnowledgeInfo.id == kb_id,
                KnowledgeInfo.is_deleted == 0,
            )
        )
        row = result.first()
    return (row[0] or None) if row else None


async def get_kb_ontology_compile_backend(tenant_id: str, kb_id: str) -> Optional[str]:
    """查单个 KB 的 **compile 步**库级派单值（`knowledge_info.ontology_compile_backend`，
    P2 / 2026-10-17 加）。**形状逐字照 `get_kb_ingest_backend`**（同一张表、同一套回落语义）。

    **查不到 / 列是 NULL 都返回 `None`，绝不回落 `rag`** —— `None` 在这个字段上是一个
    **合法值**，表示「这个库没表态、跟随服务端开关 `ONTOLOGY_COMPILE_BACKEND`」。
    回落 `rag` 会把「没表态」读成「明确要 rag」：开关开着时这个库会被静默改走另一条链。

    ⚠ 与 `get_kb_ingest_backend` 是**两把钥匙**（解析链 vs compile 步），不要互相复用：
    `ingest_backend.resolve_backend` 与 `compile_backend.resolve_compile_backend` 各读各的列。

    消费方是 `compile_backend.resolve_compile_backend(kb_backend=...)`。
    """
    if not kb_id:
        return None
    async with get_db_session() as session:
        result = await session.execute(
            select(KnowledgeInfo.ontology_compile_backend).where(
                KnowledgeInfo.tenant_id == tenant_id,
                KnowledgeInfo.id == kb_id,
                KnowledgeInfo.is_deleted == 0,
            )
        )
        row = result.first()
    return (row[0] or None) if row else None


async def get_kb_types(tenant_id: str, kb_ids: list[str]) -> dict[str, str]:
    """批量查询 KB 的 kb_type。未命中默认 'lightrag'。

    WHERE 带 tenant_id（安全性：跨租户 kb_id 查不到行 → 默认 lightrag）。
    """
    if not kb_ids:
        return {}
    async with get_db_session() as session:
        result = await session.execute(
            select(KnowledgeInfo.id, KnowledgeInfo.kb_type).where(
                KnowledgeInfo.tenant_id == tenant_id,
                KnowledgeInfo.id.in_(kb_ids),
                KnowledgeInfo.is_deleted == 0,
            )
        )
        rows = {row[0]: row[1] for row in result.all()}
    return {
        k: normalize_kb_type(rows.get(k)) or DEFAULT_KB_TYPE
        for k in kb_ids
    }
