# [jonex] 新增文件 — compile 步（本体编译）后端灰度开关（P2 §5.1 / Task 6）
"""compile 步（本体编译）后端灰度开关 —— **照 `ingest_backend.py` 的同构纪律**。

**提交时决定、写进文档行、查询时读回来**：只有提交那一处读 env，查询侧只读粘性。

为什么查询侧不读 env：一个在**旧后端**提交、还没跑完的文档，在开关翻转后必须继续从
**旧**后端取状态。查询侧各读一次环境变量的写法，会在翻转那一刻把在途文档**永远卡在
EXTRACTING** —— 一个不报错、只是永远不完成的失败形态。回滚就是切一个值；在途文档不受
影响（它们各自按行上的粘性值收尾）。这与 2026-10-09 那次事故是同一个形态。

⚠ **本模块的粘性键叫 `ontology_compile_backend`，与解析链的 `ingest_backend` 是两把钥匙**
（两者语义不同、值域也不同）—— 落点同构（`doc.extra_metadata`），但**不要**混用。

── 为什么 `reconciliation_service` 的 EXTRACTING 轮询那一支**不能**按解析链的粘性值派 ──

那一支（`reconciliation_service.py:2533` 一带）问的 `rag_task_id` 是**本体任务**，
不是解析任务。改动前它硬编码 `force_backend="rag"`（本体任务当时在 atomic-rag）。
若改用解析链的 `ingest_backend.backend_for_document(doc)`：解析链与本体链一旦不是
同一条，这一支会去问错后端要一个属于 atomic-rag 的本体任务号
⇒ 恒 404 ⇒ `not_found` 一条分支都不命中 ⇒ 真因读不回来、只剩空转重试
（2026-10-09 事故）。所以本能力自带一把钥匙：**写它的是 compile 提交那一处，
读它的是 compile 轮询这一处**，两处同源。

── 状态词汇归一（与 `ingest_backend._ENGINE_STATUS_TO_RAG` 同型）──

新服务的任务状态取自 raganything 的 `TaskStatus`（`created/queued/processing/completed/
failed/cancelled`），而对账循环 `/巡检` 认的是它自己那套小写词汇。**「待跑」这一档不归一
就是致命的**：新服务的 `queued`/`created` 不在对账的存活集合（`processing`/`pending`）里，
会落到「不认识的状态词」那一支、一路穿透到「重新提交」—— 于是同一条本体任务被**重投一次**
（或文档被永远吊在 EXTRACTING）。故 `normalize_status_info` 把两档都归到 `pending`。
"""

from __future__ import annotations

import logging
import os
from typing import Any

from jonex_core.common.exceptions import InternalError

# 平台侧是 stdlib `logging`（与同目录的 ingest_backend / reconciliation_service 同口径）。
logger = logging.getLogger(__name__)

# compile 步后端：`rag` = atomic-rag（现状）/ `ontology_compile` = 独立编译服务。
VALID_COMPILE_BACKENDS = ("rag", "ontology_compile")

#: **提交时**缺省后端。P4a（2026-10-17）起翻为 `ontology_compile`；
#: 回滚 = 设 `ONTOLOGY_COMPILE_BACKEND=rag` + 重启（无需改代码）。
DEFAULT_COMPILE_BACKEND = "ontology_compile"

#: **查询侧**兜底。老文档（没有粘性 `ontology_compile_backend` 键）确实跑在 atomic-rag。
#: ⚠ 与提交默认**必须分开**：跟着提交默认翻，会把在途/历史文档指到新服务 ——
#: 新服务没有它们的任务号，恒 404，对账一条分支都不命中，永远卡在 EXTRACTING
#: （见模块 docstring 与 `ingest_backend.backend_for_document` 的同型注释）。
LEGACY_COMPILE_BACKEND = "rag"

#: 新服务的任务生命周期状态词 → 对账循环消费的小写口径。
#:
#: · `created` / `queued` → `pending`：**本表最要紧的两行**。新服务的「待跑」是 `queued`
#:   （`worker.py` 的 `TaskStatus.QUEUED`，阶段 1 的模型里没有 `pending` 枚举值），而对账
#:   的存活集合是 `("processing", "pending")` —— 不归一的话，「排队中」的任务会被判成
#:   「不认识的状态词」，穿透到重新提交那一段（同一任务跑两遍 / 文档永远吊在 EXTRACTING）。
#: · `completed` / `failed` / `cancelled` 与对账词汇同形，列出只为把整张表读全。
#: · 表里**不该**出现新服务不产生的状态词；真收到就落 lowercase 原文，不在存活集合里
#:   （「不认识就别当活的」，与 ingest_backend 那条同口径）。
_COMPILE_STATUS_TO_RECONCILE: dict[str, str] = {
    "created": "pending",
    "queued": "pending",
    "processing": "processing",
    "completed": "completed",
    "failed": "failed",
    "cancelled": "cancelled",
}


class CompileBackendConfigError(InternalError, ValueError):
    """`ONTOLOGY_COMPILE_BACKEND`（或库级 `ontology_compile_backend`）取值非法。

    **服务端配置**错误，不是调用方参数错误 —— 形状照 `ingest_backend.IngestBackendConfigError`
    （本仓不许裸 `ValueError` 逃出异常体系，同时保留 `except ValueError` 这个运维/脚本的
    自然写法；两个契约都满足，不是二选一）。`InternalError.code` 是既有错误码，**不自造**。
    """


def _requested_raw() -> str:
    """开关的原始取值（去过空白）。**不校验** —— 只有 `resolve_compile_backend` 的回落
    判断与日志用它，用来回答「运维是不是打开了新链路」，而不是「这个值合法吗」。"""
    return os.environ.get("ONTOLOGY_COMPILE_BACKEND", "").strip()


def current_backend() -> str:
    """读开关并校验。**只允许在提交时调用**（见模块 docstring：查询侧读 env 会把在途
    文档卡死），且只允许在库值那条回落**之后**调用（见 `resolve_compile_backend`）。"""
    value = _requested_raw() or DEFAULT_COMPILE_BACKEND
    if value not in VALID_COMPILE_BACKENDS:
        raise CompileBackendConfigError(
            f"ONTOLOGY_COMPILE_BACKEND 取值非法：{value!r}（可选 {VALID_COMPILE_BACKENDS}）"
        )
    return value


def resolve_compile_backend(*, kb_backend: str | None) -> str:
    """**提交时**定 compile 步走哪个后端。判据两层，都在当场判（不做启动期校验 ——
    库值与 env 都是运行时可变的）：

    1. 库级派单值（`kb_backend`）：KB 行上的 `ontology_compile_backend`，库里就地表态
       「这个库的 compile 步走哪条」。非 NULL 时**覆盖**服务端开关 —— 形状照
       `ingest_backend.resolve_backend` 的第三层（2026-10-09 事故的成熟模式：
       全局单值改一头会静默打掉另一头）。域外值在这条路上照抛
       `CompileBackendConfigError`（响亮失败，不静默回落）。
    2. 以上不适用 ⇒ 读服务端开关（`current_backend()`，**缺省 `ontology_compile`**
       （P4a 2026-10-17 起；回滚 = 设 `ONTOLOGY_COMPILE_BACKEND=rag`））。

    ⚠ 读开关必须放在库值之后：开关只管真正管辖的那条路。放前面的话，一条躺着脏值的库
    会让它的每一次 compile 提交都硬失败（一次笔误从「新链路起不来」放大成「整个库编不了」）。
    """
    if kb_backend is not None:
        if kb_backend not in VALID_COMPILE_BACKENDS:
            raise CompileBackendConfigError(
                f"知识库行的 ontology_compile_backend 取值非法：{kb_backend!r}"
                f"（可选 {VALID_COMPILE_BACKENDS}）"
            )
        return kb_backend
    return current_backend()


def backend_for_document(doc: Any) -> str:
    """**查询时**定后端：读文档行上的**粘性**值；老文档（没有该键）按旧链路口径。

    域外值（人工改库 / 旧版本遗留）同样按旧链路 —— 行上的值只可能由
    `resolve_compile_backend` 写入，出现别的值是数据脏，不是「切到新链路」的信号。
    逐字照 `ingest_backend.backend_for_document` 的同构写法。
    """
    metadata = getattr(doc, "extra_metadata", None) or {}
    value = metadata.get("ontology_compile_backend")
    return value if value in VALID_COMPILE_BACKENDS else LEGACY_COMPILE_BACKEND


def normalize_status_info(status_info: dict | None) -> dict | None:
    """把新服务的任务状态翻成对账循环消费的词汇。形状照 `ingest_backend.normalize_status_info`。

    `None`（取不到状态）原样透传 —— 调用方的降级语义靠它，不许被翻成一个「看起来正常」
    的假状态。未知状态只做大小写归一（**绝不**落进 `completed`）：将来新服务加了新的状态
    时，最坏是一轮「不认识、继续等」，而不是把在跑的任务当成功收尾。

    **字段名也要归一，不只是状态词**：失败收尾读的是 `error`
    （`reconciliation_service._ontology_task_failure_detail` 取 `error` / `error_message`
    / `message`），而新服务的诊断字段是 `ontology_error`。少了这层别名，「抽取失败」
    的真因会**塌成一句「下游任务状态 failed（未带原因）」**，文档行上留不下任何线索。
    原字段 `ontology_error` **保留不动**：`_is_empty_ontology_output` 还要读它。
    """
    if status_info is None:
        return None
    normalized = dict(status_info)
    raw = str(status_info.get("status") or status_info.get("state") or "")
    if raw:
        normalized["status"] = _COMPILE_STATUS_TO_RECONCILE.get(raw, raw.lower())
    if not normalized.get("error"):
        detail = status_info.get("ontology_error") or status_info.get("error_message")
        if detail:
            normalized["error"] = detail
    return normalized
