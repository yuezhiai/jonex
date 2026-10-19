"""Internal Knowledge Base reconciliation service.

Follows the same core logic as the previous flat-file architecture:
- Per-doc task status polling with completed/failed/not_found handling
- LightRAG storage fallback (`_verify_via_storage`) when task state is lost
- Ontology retry with max 3 attempts
- Neo4j ontology write before PG status update (consistency design)
- Compiled schema check before ontology retry (Phase 4.2)
"""

import asyncio
import json
import logging
import os
import re
import time
from datetime import datetime, timedelta
from typing import Optional

from jonex_core.capability.atomic.ontology_compile.client import (  # [jonex] P2 compile 步新后端
    get_ontology_compile_client,
)
from jonex_core.capability.atomic.rag.client import get_rag_client
from jonex_core.common.audit import schedule_emit
from jonex_core.common.audit_enums import ResourceType
from jonex_core.common.database import get_db_session
from jonex_core.common.exceptions import (
    ResourceNotFoundError,  # [jonex] jonex_wiki 编译任务查询：任务不存在 ≠ 查询失败
)
from jonex_core.common.neo4j_client import get_neo4j_driver

from ..models import DocStatus, OntologyStatus
from ..models.task_center import ACTIVE_STATES, MANAGED_STATES
from ..ontology_write_state import clear_ontology_write_state
from ..repository import (
    KnowledgeDocumentRepository,
    OntologyGraphRepository,
    record_compile_task,
)
from ..repository.ontology_graph_repository import (
    _is_transient_error,
    _write_retry_base_delay,
)
from . import compile_backend, ingest_backend  # [jonex] P2：compile 步后端 + 粘性值判据
from .document_service import DocumentService  # [jonex] O7 残留扫描复用
from .kb_type_service import LLM_WIKI_TYPES  # [jonex] llm-wiki 子集 {openkb, jonex_wiki}
from .rag_storage_lookup import find_rag_doc_ids

logger = logging.getLogger(__name__)


# [jonex] §11 v2 stage key 白名单：过滤未知阶段、避免噪音泄露到聚合字段
_V2_STAGE_KEYS = frozenset({
    "created", "queued", "parse", "text_insert", "multimodal",
    "push_chunks", "ontology_extract", "pipeline_done",
})

# [jonex] P1-1：created/queued 是排队等待，不算入 worker 端到端耗时
_QUEUE_KEYS = frozenset({"created", "queued"})

# [jonex] 引擎侧唯一索引：一个 (tenant_id, kb_id) 只允许一个**排队中**的 compile。
# 批量上传/并发重解析时，解析先后完成会让第 2+ 个文档的编译投递撞上它 —— 这是「KB 忙」
# 而不是真失败，必须回退为可重试状态（交给 stale 巡检重投），否则文档会永久停在 failed，
# 而编译巡检只捞 compiling、不会自愈。
#
# ⚠️ **索引名换过**：2026-09-24 的引擎迁移 0006 把它从 `uq_tasks_running_compile`
# （`status IN ('queued','compiling')`，一个 KB 只能有一个编译在跑或排队）放宽成
# `uq_tasks_queued_compile`（只看 `status = 'queued'`，允许「一个在跑 + 一个排队」）。
# 所以**两个名字都要认**：
#   · 新名字是现网引擎实际会报的（容器启动即 `alembic upgrade head`）；
#   · 旧名字留给「引擎库迁移没跟上的环境」—— 那正是当初加这条容错的场景。
# 只认旧名字的话，判据对现网引擎等于失效（报的是新索引名、一个 marker 都匹配不上）。
_JONEX_WIKI_COMPILE_BUSY_MARKERS = (
    "uq_tasks_queued_compile",
    "uq_tasks_running_compile",
    "running_compile",
)


def _is_jonex_wiki_compile_slot_busy(exc: BaseException) -> bool:
    """[jonex] 判断投递异常是否为「同一 KB 已有 running compile」。"""
    text = f"{type(exc).__name__}: {exc}"
    return any(marker in text for marker in _JONEX_WIKI_COMPILE_BUSY_MARKERS)


# [jonex] §10.2.3 引用关系：候选值长度上限（实体名不长，超长自由文本不可能命中）
_REF_CANDIDATE_MAX_LEN = 40
# 单文档引用候选值上限（防超大表把 UNWIND 参数打到离谱）
_REF_CANDIDATE_CAP = 3000

# [jonex] 改动 52（S2）：KB 级 stub 实体占比告警阈值（§14.3 建议 30%）；
# 超阈值 = 抽取管线异常信号（关系端点大量捏造空壳），修管线而不是静默保留。
_STUB_ENTITY_RATIO_WARN_THRESHOLD = 0.30
# 告警最小实体数：小 KB 样本不足，避免噪声
_STUB_ENTITY_RATIO_MIN_TOTAL = 10


def _stub_ratio_alert(total: int, stub: int,
                      threshold: float = _STUB_ENTITY_RATIO_WARN_THRESHOLD,
                      min_total: int = _STUB_ENTITY_RATIO_MIN_TOTAL) -> bool:
    """纯函数：KB 级 stub 占比是否超阈值（total 过小时不告警）。"""
    return total >= min_total and stub > 0 and (stub / total) > threshold


def _env_float(name: str, default: float) -> float:
    """读 float 型环境变量，缺失/非法时回落默认值。"""
    try:
        return float(os.environ.get(name) or default)
    except (TypeError, ValueError):
        return default


# ── [jonex] P0-d：任务 JSON 磁盘直读兜底 ───────────────────────────────────
# 背景（2026-09-13 线上事故）：46290 行的表格文档完成本体抽取后，任务 JSON 达
# 87MB（其中 ontology_data 82MB = 54237 实体 + 32206 关系）。KB 经 Sidecar 读该
# 状态时，Sidecar 的 uvicorn worker 在转发大响应体时退出（sidecar 日志
# "Child process died"，容器 RestartCount=0，既非超时——实测 10~20s 远小于
# SIDECAR_PROXY_TIMEOUT=150——也非宿主 OOM，dmesg 无记录），KB 侧只收到
# "Server disconnected without sending a response."。后果有二：
#   ① reconcile 拿不到 status_info → 已完成的任务收不了尾（44850 个表格对象
#      写不进 Neo4j）；
#   ② patrol 探活连续 3 次失败 → alive=False → 把**已成功**的文档判成
#      "知识入库超时（已 INGESTING 103 分钟）" FAILED。
# 任务 JSON 本就落在 atomic-rag 的 WORKING_DIR 下，KB 容器已只读挂载同一卷
# （deploy/docker-compose.yml），故 HTTP 不可用时改从磁盘取。
_RAG_STORAGE_SHARED_PATH = os.getenv("RAG_STORAGE_SHARED_PATH", "/app/rag_storage")
# 读取上限：超过视为异常文件，拒绝读入（防把 KB 进程撑爆）
_RAG_TASK_JSON_MAX_MB = int(os.getenv("RAG_TASK_JSON_MAX_MB", "1024"))
# 超过此大小就确定会打崩 Sidecar 的转发 worker（它的死因就是转发超大响应体），
# 此时直接读盘、不再白白牺牲一个 worker。阈值取 KB 级常规任务 JSON（几十 KB）
# 与事故文件（87MB）之间的安全值；置 0 = 只要磁盘上有该任务 JSON 就优先读盘。
_RAG_TASK_JSON_DISK_FIRST_MB = int(os.getenv("RAG_TASK_JSON_DISK_FIRST_MB", "8"))
# 路径段白名单：tenant/kb/task id 只允许安全字符，防路径穿越
_PATH_SEGMENT_RE = re.compile(r"^[A-Za-z0-9._\-]+$")


def _task_json_candidates(tenant_id: str, kb_id: str, task_id: str) -> list:
    """任务 JSON 的候选路径（按命中概率排序）。"""
    if not (_RAG_STORAGE_SHARED_PATH and tenant_id and kb_id and task_id):
        return []
    if not all(_PATH_SEGMENT_RE.match(str(x)) for x in (tenant_id, kb_id, task_id)):
        logger.warning(
            "P0-d disk fallback skipped: unsafe id tenant=%r kb=%r task=%r",
            tenant_id, kb_id, task_id,
        )
        return []
    names = [str(tenant_id)]
    alt = str(tenant_id).replace("-", "_")
    if alt != names[0]:
        names.append(alt)  # 实体图标签用 `-`→`_` 变体，任务目录沿用原值，两个都试
    return [
        os.path.join(_RAG_STORAGE_SHARED_PATH, n, str(kb_id), "tasks", f"{task_id}.json")
        for n in names
    ]


def _disk_task_json_size(tenant_id: str, kb_id: str, task_id: str) -> int:
    """任务 JSON 在共享卷上的字节数；不存在返回 -1。用于决定是否绕过 HTTP。"""
    for path in _task_json_candidates(tenant_id, kb_id, task_id):
        try:
            if os.path.isfile(path):
                return os.path.getsize(path)
        except OSError:
            continue
    return -1


def _read_task_json_sync(path: str) -> dict:
    """同步读任务 JSON（调用方丢线程池，避免阻塞事件循环）。"""
    size = os.path.getsize(path)
    limit = _RAG_TASK_JSON_MAX_MB * 1024 * 1024
    if size > limit:
        raise ValueError(f"task json {size} bytes exceeds limit {limit}")
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


async def _load_task_status_from_disk(tenant_id: str, kb_id: str, task_id: str,
                                      *, doc_id: str = "") -> dict | None:
    """HTTP 路径不可用时，从共享卷直读 atomic-rag 任务 JSON。

    返回任务 JSON 的 dict（与 get_task_status 的 status_info 同构，字段为其超集），
    找不到 / 读不动时返回 None，调用方保持原有降级语义。
    """
    for path in _task_json_candidates(tenant_id, kb_id, task_id):
        if not os.path.isfile(path):
            continue
        try:
            _t0 = time.perf_counter()
            data = await asyncio.to_thread(_read_task_json_sync, path)
            logger.warning(
                "P0-d disk fallback hit: doc_id=%s task=%s status=%s ontology_status=%s "
                "size=%.1fMB read=%.0fms",
                doc_id or "-", task_id, data.get("status"), data.get("ontology_status"),
                os.path.getsize(path) / 1048576.0, (time.perf_counter() - _t0) * 1000,
            )
            return data
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "P0-d disk fallback read failed: doc_id=%s path=%s err=%s",
                doc_id or "-", path, exc,
            )
            return None
    logger.debug(
        "P0-d disk fallback miss: task=%s (searched under %s)",
        task_id, _RAG_STORAGE_SHARED_PATH,
    )
    return None


async def _fetch_task_status(tenant_id: str, kb_id: str, task_id: str,
                             *, doc_id: str = "", doc,
                             force_backend: str | None = None) -> dict | None:
    """取任务状态的统一入口：能确定会打崩 Sidecar 的大 JSON 直读磁盘，其余走 HTTP。

    三条路径：
      ① JSON 超大 → 直读磁盘（HTTP 必崩，不走无谓的牺牲）；
      ② 常规大小 → HTTP；
      ③ HTTP 抛异常 → 回退磁盘。
    两条路都拿不到时返回 None，调用方保持原有降级语义。

    `doc`（文档行）与 `force_backend` 一起声明这条 `task_id` 属于哪条链：解析链由
    `ingest_backend.backend_for_document` 读粘性值（当前只有 `rag` 一条）；
    compile 步由 `compile_backend.backend_for_document` 读 `ontology_compile_backend`
    粘性值，决定问 atomic-rag 还是独立编译服务（P2）。

    ⚠ `doc` 是**必填关键字参数**（没有默认值）：它的默认值曾经是 `None`，于是
    「漏传」与「有意不传」在调用点长得一模一样 —— 新加的调用点漏传时不会报错，
    只会静默走错后端。必填逼每个调用点显式表态。

    ── `force_backend`：调用方**知道**这条 `task_id` 是谁建的时，替掉粘性值 ──

    `backend_for_document` 读的是**解析链**的粘性值（`insert` 发给了谁），可调用方手里
    的 `rag_task_id` 未必是解析任务：`_retry_ontology_one` 的 EXTRACTING 分支问的是
    **本体任务**。本体任务在哪由 compile 步的粘性值（`ontology_compile_backend`）
    决定 —— 该分支据此传 `force_backend`（见那里的注释），把**解析链**的粘性值让位。

    ⚠ **取值域**：本参数认 `ontology_compile`，以及「其余一切」（走 atomic-rag 那三条
    路径）。`rag` 不在新域里，但语义上等价于「atomic-rag 那一支」。认不出的值仍走
    「不是新链路」的那一支（与粘性值出脏值时的回落同口径），不会静默当成新链路。

    取不到状态时**区分两种**（对齐 `RAGClient.get_task_status` 的返回形态）：
      · 任务行不存在 ⇒ `{"status": "not_found"}`（下游 client 归一为状态、不抛异常），
        让调用方走 `_verify_chain`。那里的存储兜底是**后端无关**的，能救回一个
        「其实已经推成功、只是任务行丢了」的文档（DB 重置/恢复、手工清理、跨环境切库）。
        塌成 `None` 的话它只会被对账跳过、最终由 patrol 判死。
      · 其它取状态失败 ⇒ `None`，保持调用方的降级语义不变。
    """
    backend = force_backend or ingest_backend.backend_for_document(doc)
    if backend == "ontology_compile":
        # [jonex] P2：compile 步在独立服务里的那一支 —— 与解析链那一支同形。
        # `include_data=True` 是**承重**的：本函数的调用方之一是对账的 EXTRACTING
        # 轮询，它命中 `completed` 后要靠 `ontology_data` 收尾（`_handle_completed` 写
        # Neo4j）。默认的 `include_data=False` 只是「不带数据」，**键都不在** —— 少了它
        # 那份文档永远不落 READY（也不是报错，是静默不完成）。
        # （看门狗那条路只读 `ontology_status` / `ontology_error`，多带的 `ontology_data`
        # 对它是无用的负载；但 `compile` 步在 `task_watch_service` 里走 `_inspect_compile`，
        # **不问**本函数，所以这份负载只落在对账这一条需要它的路上。）
        try:
            info = await get_ontology_compile_client().get_compile_status(
                compile_id=task_id, tenant_id=tenant_id, include_data=True,
            )
        except ResourceNotFoundError:
            # 与旧链路/new 链路同口径：不是异常，是一个状态
            logger.warning(
                "P2 compile 任务不存在（进入查证链）: doc_id=%s task=%s",
                doc_id or "-", task_id,
            )
            return {"status": "not_found"}
        except Exception as exc:  # noqa: BLE001 —— 取不到就是取不到，交给调用方降级
            logger.warning(
                "P2 compile 取任务状态失败: doc_id=%s task=%s err=%s",
                doc_id or "-", task_id, exc,
            )
            return None
        # 状态词汇 / 字段名归一到对账循环消费的口径（`queued` → `pending` 是承重的一档，
        # `ontology_error` → `error` 让失败真因到得了文档行）。见 `compile_backend`。
        return compile_backend.normalize_status_info(info)

    size = await asyncio.to_thread(_disk_task_json_size, tenant_id, kb_id, task_id)
    if size >= _RAG_TASK_JSON_DISK_FIRST_MB * 1024 * 1024:
        logger.warning(
            "P0-d 任务 JSON 达 %.1fMB（≥%dMB），跳过 HTTP 直读磁盘: doc_id=%s task=%s",
            size / 1048576.0, _RAG_TASK_JSON_DISK_FIRST_MB, doc_id or "-", task_id,
        )
        return await _load_task_status_from_disk(tenant_id, kb_id, task_id, doc_id=doc_id)
    try:
        return await get_rag_client().get_task_status(task_id=task_id, tenant_id=tenant_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "P0-d HTTP 取任务状态失败，回退磁盘: doc_id=%s task=%s err=%s",
            doc_id or "-", task_id, exc,
        )
        return await _load_task_status_from_disk(tenant_id, kb_id, task_id, doc_id=doc_id)


# [jonex] 写图并发：实体/关系按条写，每条 = 1 次 embedding HTTP（~32ms）+ 1 次
# Cypher（~20ms），串行 54ms/条 → 6961 实体 + 3793 关系的文档写图阶段 578s
# （e2e 17min 的 57%），且同刻只有 ~1.3 个 embedding 请求在飞（上游 GPU ~20%）。
# 并发度复用 `.env.rag` 已实测标定的 EMBEDDING_FUNC_MAX_ASYNC（单请求甜点
# 24~32、峰值 ~85 req/s，超 32 延迟上升）——同一语义、同一上游，不再另造键。
WRITE_CONCURRENCY_DEFAULT = 24


def _write_concurrency() -> int:
    """写图并发度：EMBEDDING_FUNC_MAX_ASYNC（缺失/脏值回落 24，下限 1）。"""
    try:
        n = int(os.environ.get("EMBEDDING_FUNC_MAX_ASYNC") or WRITE_CONCURRENCY_DEFAULT)
    except (TypeError, ValueError):
        return WRITE_CONCURRENCY_DEFAULT
    return max(1, n)


# [jonex] P0-1：关系阶段独立并发度。merge_relation 全程无 embedding 调用
# （embedding 只在 merge_entity），复用 EMBEDDING_FUNC_MAX_ASYNC=24 是错配。
# 关系实际并发由「热点分桶」决定，本值只限制**桶间**并发——低度边大量
# 各自成桶时它就是吞吐上限。默认 8 是保守起点，按验收的 rel_write_ms 调。
REL_WRITE_CONCURRENCY_DEFAULT = 8

# [jonex] 热点判定阈值：本批内某端点出现次数 >= 此值即视为热点，它的边串行。
# 取 8 的依据：事故量级是 1992 个写者争同一节点；8 以下节点的并发争用远低于
# 此，交给 _with_transient_retry 的 6 次退避吸收即可，不值得为此让全图串行。
REL_HOT_DEGREE_DEFAULT = 8


def _rel_write_concurrency() -> int:
    """关系阶段桶间并发度（ONTOLOGY_REL_WRITE_CONCURRENCY，缺失/脏值回落 8，下限 1）。"""
    try:
        n = int(os.environ.get("ONTOLOGY_REL_WRITE_CONCURRENCY")
                or REL_WRITE_CONCURRENCY_DEFAULT)
    except (TypeError, ValueError):
        return REL_WRITE_CONCURRENCY_DEFAULT
    return max(1, n)


def _rel_hot_degree() -> int:
    """热点端点阈值（ONTOLOGY_REL_HOT_DEGREE，缺失/脏值回落 8，下限 1）。"""
    try:
        n = int(os.environ.get("ONTOLOGY_REL_HOT_DEGREE") or REL_HOT_DEGREE_DEFAULT)
    except (TypeError, ValueError):
        return REL_HOT_DEGREE_DEFAULT
    return max(1, n)


def _bucket_relations(
    resolved: list[tuple], hot_degree: int | None = None,
) -> list[list]:
    """度数感知分桶：热点端点串行，非热点边各自并发。

    为什么不是纯端点连通分量：知识图谱天然连通，一个枢纽实体就能把整批边
    并进同一分量 → 关系阶段从并发退回完全串行（32206 关系 × ~20ms ≈ 644s，
    叠加 transient 退避后逼近写图整段超时）。而事故成因是「同一节点被 24 个
    事务同时写」，只需让**热点**节点串行。表格文档的 1992 条分组边共享同一
    聚合节点（度数 1992 ≫ 阈值）→ 仍落 1 桶，事故照修不误。

    **这是权衡，不是零死锁的构造性保证**：共享低度端点（度数 < 阈值）的两条
    边可能落进不同桶并发写同一节点。量级（< 阈值 个写者）远低于事故的 1992，
    由 _with_transient_retry 兜底。别把它当证明来引用。

    resolved: ``[(src_lock_key, tgt_key, rel), ...]``，端点必须是**解析后**的
    ``_endpoint_lock_key`` 元组——用原始名会把同一实体的多个别名判成不同
    端点，热点会漏判、分桶直接失效。
    """
    if not resolved:
        return []
    threshold = hot_degree if hot_degree is not None else _rel_hot_degree()

    # 1) 本批内端点度数（自环只计一次）
    degree: dict[tuple, int] = {}
    for s, t, _ in resolved:
        degree[s] = degree.get(s, 0) + 1
        if t != s:
            degree[t] = degree.get(t, 0) + 1
    hot = {k for k, d in degree.items() if d >= threshold}

    # 2) 热点之间的并查集；两端都热点的边充当桥（否则两个热点分量会并发互撞）
    parent: dict[tuple, tuple] = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]      # 路径压缩
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for s, t, _ in resolved:
        if s in hot and t in hot:
            union(s, t)

    # 3) 归组：含热点 → 挂到该热点所在分量；不含热点 → 各自成桶（保持并发）
    groups: dict[tuple, list] = {}
    for i, (s, t, rel) in enumerate(resolved):
        if s in hot:
            anchor: tuple = find(s)
        elif t in hot:
            anchor = find(t)
        else:
            # 单例哨兵：不参与并查集，等价于「这条边自成一桶」。
            # 与真实端点键不可能碰撞——真实键是 (str, str)，这里是 (str, int)。
            anchor = ("__singleton__", i)
        groups.setdefault(anchor, []).append(rel)
    return list(groups.values())


async def _run_bounded(items: list, fn, limit: int) -> list:
    """有界并发执行 ``fn(item)``，返回结果按 ``items`` 原序。

    信号量压在**单次 await 全程**（embedding + Cypher 都在临界区内），
    保证同一时刻在飞的上游请求数 = limit。

    [jonex] 异常时必须**取消并回收全部兄弟任务**：``asyncio.gather`` 默认只把
    第一个异常抛给调用方，其余协程不会停——会继续在后台跑完。写图场景下这意味着
    「本轮已判失败，但另外 23 条 merge 仍在往图里写」：调用方随即重试
    （``_write_graph_once`` 开头就是 ``delete_by_document`` + 重写），孤儿写入与
    重试的删除**并发交错**，「先删后 merge、图终态=本轮产出」的一致性保证直接失效
    （线上实测：第 1/2 轮死锁失败后仍继续写入 4 分钟、39555 条边，撞进第 2 轮的
    delete 窗口）。这里显式 cancel + await 回收，保证本函数返回时已无在飞写入。
    取消 await 中的 Cypher 会连带关闭该 driver 连接 → 服务端事务一并终止。
    """
    sem = asyncio.Semaphore(max(1, limit))

    async def _one(item):
        async with sem:
            return await fn(item)

    tasks = [asyncio.create_task(_one(i)) for i in items]
    try:
        return await asyncio.gather(*tasks)
    except BaseException:
        # BaseException 而非 Exception：本函数自身被取消（外层超时）时同样要收尾。
        for t in tasks:
            t.cancel()
        # return_exceptions=True：回收被取消的任务，避免 "Task exception was never retrieved"
        await asyncio.gather(*tasks, return_exceptions=True)
        raise


# [jonex] 写图整段重试：单条 transient 已在仓储层（_with_transient_retry）兜住，
# 这里兜「整段中途失败」。抽取成果花了十几分钟 LLM，不该因写图阶段（~72s）的
# 一次抖动整轮报废；重试开头的 delete_by_document 天然清零上一轮半写。
GRAPH_REWRITE_ATTEMPTS_DEFAULT = 2


def _graph_rewrite_attempts() -> int:
    """写图整段尝试次数（含首跑）：ONTOLOGY_GRAPH_REWRITE_ATTEMPTS，下限 1。"""
    try:
        n = int(os.environ.get("ONTOLOGY_GRAPH_REWRITE_ATTEMPTS") or GRAPH_REWRITE_ATTEMPTS_DEFAULT)
    except (TypeError, ValueError):
        return GRAPH_REWRITE_ATTEMPTS_DEFAULT
    return max(1, n)


# [jonex] 引用命中查询超时：该查询的第 ② 段扫 aliases 无法索引化（Neo4j 常规索引
# 不支持列表成员查找），最坏情况（候选值全不命中）退化为「剩余值 × KB 节点数」的
# 逐节点过滤。线上实测 3000 值 × 5 万节点满核跑 11 分钟仍未完成——单条 Cypher 不
# 分批、无服务端超时，而调用方是**串行**的对账循环，于是一个文档的引用命中能把
# 整个对账（本体收尾 + PARSING 超时巡检 + OpenKB 编译巡检）一起卡死十几分钟。
# 该查询本就允许降级（内层 except 只丢引用关系、不阻断写图），套超时后语义变为
# 「超时 → 本轮无引用关系 → 照常 delete + 写图 → 文档正常 READY」，代价只是图谱
# 少一批增强边。<=0 关闭超时（回退旧行为，仅排障用）。
REF_HITS_TIMEOUT_DEFAULT = 30


def _ref_hits_timeout() -> float:
    """引用命中查询超时秒数：ONTOLOGY_REF_HITS_TIMEOUT_SEC，<=0 表示不限时。"""
    try:
        n = float(os.environ.get("ONTOLOGY_REF_HITS_TIMEOUT_SEC") or REF_HITS_TIMEOUT_DEFAULT)
    except (TypeError, ValueError):
        return float(REF_HITS_TIMEOUT_DEFAULT)
    return n


# [jonex] P1：写图整段超时。调用方是串行的对账循环（本体收尾 + PARSING 超时
# 巡检 + OpenKB 编译巡检共用一条线程），单个文档的写图不得无限占住它——
# 与 _ref_hits_timeout 同型风险。超时的语义是 transient（可退避重试）。
GRAPH_WRITE_TIMEOUT_DEFAULT = 1800


def _graph_write_timeout() -> float:
    """写图整段超时秒数（ONTOLOGY_GRAPH_WRITE_TIMEOUT_SEC，<=0 表示关闭）。"""
    try:
        n = float(os.environ.get("ONTOLOGY_GRAPH_WRITE_TIMEOUT_SEC")
                  or GRAPH_WRITE_TIMEOUT_DEFAULT)
    except (TypeError, ValueError):
        return float(GRAPH_WRITE_TIMEOUT_DEFAULT)
    return max(0.0, n)


async def _write_graph_once(gdao, doc, kb_id: str, ont_data: dict) -> list:
    """执行一轮完整的「先删后 merge」，返回本轮引用关系列表。

    外层套整段超时（见 _graph_write_timeout）：超时抛 asyncio.TimeoutError，
    由 _rewrite_ontology_graph 按 transient 处理（退避重试）。_run_bounded
    在取消时会回收全部在飞的兄弟协程，不会留孤儿写入。
    """
    _timeout = _graph_write_timeout()
    return await asyncio.wait_for(
        _write_graph_once_inner(gdao, doc, kb_id, ont_data),
        timeout=_timeout if _timeout > 0 else None,
    )


async def _write_graph_once_inner(gdao, doc, kb_id: str, ont_data: dict) -> list:
    """一轮「先删后 merge」的函数体。

    任一步抛异常 = 整轮失败（此刻图里可能是半写），由调用方
    ``_rewrite_ontology_graph`` 决定重试或清理。
    """
    # [jonex] §10.2.3 引用关系：表格对象列值精确命中 KB 内其他实体 → 建边
    # （源=行实体，关系类型=列标准属性名）。失败降级只丢关系。放在 delete
    # 之前算，命中口径是「本文档写入前」的 KB 图。
    ref_rels: list = []
    try:
        candidates = _table_reference_candidates(ont_data.get("entities", []))
        _timeout = _ref_hits_timeout()
        # [jonex] 超时兜底（见 _ref_hits_timeout 注释）：超时 = 放弃引用关系，
        # 继续往下 delete + 写图，文档照常收尾。wait_for 取消 await 会连带关闭
        # 该 driver 连接 → 服务端那条长跑 Cypher 一并终止，不会留在 Neo4j 里空转。
        hits = await asyncio.wait_for(
            gdao.find_reference_hits(doc.tenant_id, kb_id, candidates),
            timeout=_timeout if _timeout > 0 else None,
        )
        ref_rels = _build_table_reference_relations(ont_data.get("entities", []), hits)
    except asyncio.TimeoutError:
        logger.warning(
            "Table reference hits timed out (>%ss), skipped (degraded): "
            "doc_id=%s candidates=%d —— 写图继续，本轮仅缺引用关系",
            _ref_hits_timeout(), doc.id, len(candidates),
        )
    except Exception as exc:  # noqa: BLE001 — 引用关系失败不阻断写图
        logger.warning(
            "Table reference relations skipped (degraded): doc_id=%s err=%s", doc.id, exc,
        )
    # [jonex] 批 6「删光重来」：先删后 merge——重抽/重解析统一语义为
    # 「图终态 = 本轮产出」，实体不再跨轮累积（335 实体根因 L1）。
    # delete_by_document 已按 kb_id 收窄，不做全租户扫描。
    _delete_t0 = time.perf_counter()
    await gdao.delete_by_document(doc.tenant_id, kb_id, doc.id)
    _delete_ms = int((time.perf_counter() - _delete_t0) * 1000)
    # P1：本文档写入前清空端点解析缓存，避免命中上一文档/旧状态的解析结果
    gdao.reset_endpoint_cache()
    # 批量预取 embedding_hash，消除 merge_entity 循环内的 N+1 查询
    hash_cache = await gdao.get_embedding_hashes(doc.tenant_id, kb_id)
    # [jonex] 有界并发写图：串行 54ms/条 是写图阶段的唯一瓶颈
    # （见 _write_concurrency 注释）。实体先写完再写关系——关系
    # 端点解析（_resolve_endpoint）依赖实体的非 stub 状态。
    _conc = _write_concurrency()
    # [jonex] 实体也按 MERGE 键去重：MERGE 键与唯一约束
    # (tenant_id, kb_id, entity_type, canonical_name) 一致，同键重复项在并发下
    # 会撞 ConstraintValidationFailed，而它不是 transient（_is_transient_error
    # 只认 Neo.TransientError 前缀）→ 直接判死整轮。
    _ent_seen: set = set()
    _ent_todo = []
    for ent in list(ont_data.get("entities", [])):
        _ekey = (ent.get("entity_type"), ent.get("canonical_name"))
        if _ekey in _ent_seen:
            continue
        _ent_seen.add(_ekey)
        _ent_todo.append(ent)
    await _run_bounded(
        _ent_todo,
        lambda ent: gdao.merge_entity(doc.tenant_id, kb_id, doc.id, ent, hash_cache=hash_cache),
        _conc,
    )
    # 先删后 merge 已保证终态=本轮产出，关系全量 merge 即可
    # （并发前按端点+类型去重：既省写又消除同键 MERGE 竞争）
    _rel_seen: set = set()
    _rel_todo = []
    for rel in list(ont_data.get("relations", [])) + list(ref_rels):
        _key = (
            rel.get("source_name"), rel.get("relation_type"),
            rel.get("target_name"),
        )
        if _key in _rel_seen:
            continue
        _rel_seen.add(_key)
        _rel_todo.append(rel)
    _rel_t0 = time.perf_counter()
    # [jonex] P0-1：按热点端点分桶——热点节点的边串行写，其余各自并发。
    # 不用「按 min 端点分桶」：分组关系的 min 取决于表头列名与 LLM 类型名的
    # 字典序，group_type > object_type 时 min 恒为各不相同的行对象 → 全并发
    # 失效（50/50）。也不用「纯连通分量」：知识图谱天然连通，一个枢纽实体
    # 就能把整批边并进一个分量 → 全图串行。
    _endpoint_pairs = await gdao.resolve_relation_endpoint_keys(
        doc.tenant_id, kb_id, _rel_todo,
    )
    resolved = [
        (s, t, rel) for (s, t), rel in zip(_endpoint_pairs, _rel_todo)
    ]
    _hot_degree = _rel_hot_degree()
    buckets = _bucket_relations(resolved, _hot_degree)
    logger.info(
        "Ontology rel buckets: doc_id=%s rels=%d rel_buckets=%d max_bucket=%d "
        "hot_degree=%d rel_concurrency=%d",
        doc.id, len(_rel_todo), len(buckets),
        max((len(b) for b in buckets), default=0),
        _hot_degree, _rel_write_concurrency(),
    )

    async def _write_bucket(bucket_rels: list) -> None:
        for rel in bucket_rels:      # 桶内串行：热点不并发 → 无锁环
            await gdao.merge_relation(doc.tenant_id, kb_id, doc.id, rel)

    await _run_bounded(buckets, _write_bucket, _rel_write_concurrency())
    _rel_write_ms = int((time.perf_counter() - _rel_t0) * 1000)
    logger.info(
        "Ontology write: doc_id=%s entities=%d rels=%d delete_ms=%d rel_write_ms=%d",
        doc.id, len(_ent_todo), len(_rel_todo), _delete_ms, _rel_write_ms,
    )
    return ref_rels


async def _rewrite_ontology_graph(
    gdao, doc, kb_id: str, ont_data: dict,
) -> tuple[bool, list, bool]:
    """「先删后 merge」整段重试 + 耗尽清理。

    为什么要清理终态：本体检索只按 ``stub`` 过滤、**不看 ``ontology_status``**，
    残缺图会被正常召回并给出「看似有据、实则缺胳膊少腿」的答案——比「查不到、
    降级到普通 RAG」危险。故图终态只允许两种：完整的本轮产出，或零贡献。

    返回 ``(是否成功, 引用关系列表, 是否 transient 失败)``。第三个值供调用方
    分流：锁冲突走退避（不消耗计数），确定性失败才计入同任务重写上限。
    """
    attempts = _graph_rewrite_attempts()
    last_transient = False
    try:
        for attempt in range(attempts):
            try:
                ref_rels = await _write_graph_once(gdao, doc, kb_id, ont_data)
                return True, ref_rels, False
            except Exception as exc:  # noqa: BLE001 — 非 transient 视为确定性失败
                # [jonex] 整段超时（asyncio.TimeoutError）算 transient：写图卡住
                # 多为锁争用/长尾，退避重试有意义。判在调用点而非仓储层——
                # 仓储层的 _is_transient_error 判的是「单条 Cypher 的 Neo4j 错误
                # 类型」，那层没有 wait_for、不会产生 TimeoutError。
                transient = isinstance(exc, asyncio.TimeoutError) or _is_transient_error(exc)
                last_transient = transient
                logger.error(
                    "Neo4j ontology write failed for doc %s (第 %d/%d 轮, transient=%s): %s",
                    doc.id, attempt + 1, attempts, transient, exc,
                )
                if not transient or attempt >= attempts - 1:
                    break
                delay = _write_retry_base_delay() * (2 ** attempt)
                if delay > 0:
                    await asyncio.sleep(delay)
    except BaseException:
        # [jonex] 被取消（外层对账超时/关闭）同样要收回半写图——CancelledError
        # 是 BaseException 子类，except Exception 捕不到，半写图会留在图里被
        # 正常召回（比查不到更危险）。
        # 已知局限：若取消在清理期间被**再次**投递（连续两次 task.cancel()），
        # 清理会被打断。此处不加 shield——该文档仍停在 EXTRACTING，对账下一轮
        # 会重做「先删后 merge」，delete_by_document 会把残留清掉，可自愈。
        logger.warning("写图中途被取消，收回半写图: doc_id=%s", doc.id)
        await _reclaim_half_written(gdao, doc, kb_id)
        raise

    await _reclaim_half_written(gdao, doc, kb_id)
    return False, [], last_transient


async def _reclaim_half_written(gdao, doc, kb_id: str) -> None:
    """收回该文档在图里的全部贡献。成功与否都不抛（清理失败不阻断收尾）。"""
    try:
        await gdao.delete_by_document(doc.tenant_id, kb_id, doc.id)
        logger.warning(
            "半写本体图已收回（本文档在图内零贡献，检索降级到普通 RAG）: doc_id=%s",
            doc.id,
        )
    except Exception as cleanup_exc:  # noqa: BLE001 — 清理失败不阻断文档收尾
        logger.error(
            "半写本体图清理失败——图内可能残留不可信数据: doc_id=%s: %s",
            doc.id, cleanup_exc,
        )


def _degrade_guard_rejects(before: int, incoming: int, threshold: float) -> bool:
    """纯函数：本次重写实体数相对图中已有贡献的跌幅是否超阈值。

    [jonex] P5-var：上游采样方差使单次抽取实体数波动（实测 25~39），
    而 reconcile 是「删了重写」——方差下沿 = 数据损失；跌幅超阈值时
    拒绝覆盖、保留旧数据。threshold<=0 关闭闸门；before<=0（首次入库）
    不触发。
    """
    if threshold <= 0 or before <= 0:
        return False
    return incoming < before * (1 - threshold)


def _table_reference_candidates(entities: list[dict]) -> list[str]:
    """收集 row_as_object 表格实体的引用候选值（去重 + 上限保护）。

    attributes 含属性组（§10.2.3 层级关系）时取叶子字符串。
    """
    values: list[str] = []
    seen: set[str] = set()

    def push(v: str) -> None:
        v = v.strip()
        if not v or len(v) > _REF_CANDIDATE_MAX_LEN or v in seen:
            return
        seen.add(v)
        values.append(v)

    for ent in entities or []:
        if ent.get("extraction_method") != "row_as_object":
            continue
        attrs = ent.get("attributes") or {}
        if not isinstance(attrs, dict):
            continue
        for v in attrs.values():
            if isinstance(v, dict):
                for v2 in v.values():
                    if isinstance(v2, str):
                        push(v2)
            elif isinstance(v, str):
                push(v)
        if len(values) >= _REF_CANDIDATE_CAP:
            break
    return values


def _build_table_reference_relations(
    entities: list[dict], hits: dict[str, tuple[str, str]],
) -> list[dict]:
    """[jonex] §10.2.3 引用关系：列值精确命中 KB 内其他实体 → 建边。

    仅处理 row_as_object 表格实体；命中目标为实体自身（canonical_name
    相同，含命中自身别名）时跳过（自引用）。关系类型 = 该列名（
    column_mapping 已在 build_objects 落成标准键名）；源 = 行实体，
    目标 = 命中的正式实体，source_chunks 沿用行锚点。
    """
    rels: list[dict] = []
    seen: set[tuple] = set()
    for ent in entities or []:
        if ent.get("extraction_method") != "row_as_object":
            continue
        source_name = ent.get("canonical_name") or ""
        if not source_name:
            continue
        source_type = ent.get("entity_type") or "unknown"
        source_chunks = ent.get("source_chunks") or []

        def emit(col: str, value: str) -> None:
            value = value.strip()
            if not value or len(value) > _REF_CANDIDATE_MAX_LEN:
                return
            hit = hits.get(value)
            if not hit:
                return
            tgt_type, tgt_name = hit
            if tgt_name == source_name:
                return  # 自引用（含命中自身别名）
            sig = (source_name, tgt_name, col)
            if sig in seen:
                return
            seen.add(sig)
            rels.append({
                "source_name": source_name,
                "source_type": source_type,
                "target_name": tgt_name,
                "target_type": tgt_type,
                "relation_type": col or "引用",
                "confidence": 1.0,
                "source_chunks": source_chunks,
            })

        attrs = ent.get("attributes") or {}
        if not isinstance(attrs, dict):
            continue
        for col, v in attrs.items():
            if isinstance(v, dict):
                for leaf_col, leaf_v in v.items():
                    if isinstance(leaf_v, str):
                        emit(leaf_col, leaf_v)
            elif isinstance(v, str):
                emit(col, v)

    return rels


def _normalize_stage_timings(raw):
    """归一 worker 分阶段耗时，兼容两种形态：
    - v1：dict，含 `worker_total_ms` + 各阶段键
    - v2：list[{stage,label,started_at,ended_at,elapsed_seconds}]
    返回 (worker_total_ms: int|None, per_stage_ms: dict)。
    v2 list 形态仅取白名单内 stage key，其余静默跳过。
    """
    if isinstance(raw, dict):
        total = raw.get("worker_total_ms")
        per = {k: v for k, v in raw.items() if k != "worker_total_ms"}
        return total, per
    if isinstance(raw, list):
        per: dict = {}
        total_s = 0.0
        for s in raw:
            if not isinstance(s, dict):
                continue
            stage = s.get("stage") or "unknown"
            # [jonex] §11 白名单过滤
            if stage not in _V2_STAGE_KEYS:
                continue
            try:
                secs = float(s.get("elapsed_seconds") or 0)
            except (TypeError, ValueError):
                secs = 0.0
            per[f"{stage}_ms"] = int(secs * 1000)
            # [jonex] P1-1：created/queued 是排队，不算入 worker 端到端
            if stage not in _QUEUE_KEYS:
                total_s += secs
        return (int(total_s * 1000) if raw else None), per
    return None, {}


def _detect_pipeline_version(raw) -> str | None:
    """从 stage_timings 形态推断 pipeline 版本。
    - list → v2 (raganything pipeline)
    - dict → v1 (lightrag_adapter legacy)
    - None/empty → None
    """
    if isinstance(raw, list):
        return "v2"
    if isinstance(raw, dict):
        return "v1"
    return None

MAX_ONTOLOGY_RETRIES = 3

#: 重试上限文案的固定头。闸门要「不覆盖真因」，靠的就是认这个头判断
#: 「当前 `ontology_error` 是不是上一轮闸门自己写的那句」——
#: **写与认必须共用这一常量**：改文案时只改一边，幂等判定就失效，文案会一轮轮叠上去。
_ONTOLOGY_RETRY_LIMIT_HEAD = "本体重试次数已达上限"


def _ontology_retry_limit_message(
    retry_count: int, prior_error: str | None, *,
    max_retries: int = MAX_ONTOLOGY_RETRIES,
) -> str:
    """重试上限的 `ontology_error` 文案：**带上真因**，且可重复执行（幂等）。

    事故（2026-10-09，线上环境）：闸门只写「本体重试次数已达上限 (3/3)」，
    把上游刚读到的真因（`LightRAG chunk 存储中未找到该文档 chunk，无法抽取本体`）
    顶掉；而闸门对 FAILED 文档**每 30 秒重跑一次**，真因再也回不来。用户看到的是
    「失败了 3 次」，看不出为什么失败 —— 真因只留在 atomic-rag 的任务 JSON 里。

    真因就写在同一行的 `ontology_error` 上（`_retry_ontology_one` 的 EXTRACTING 分支
    读回来落的），所以 `prior_error` 传它即可，不必另存一份状态。

    **幂等**靠认 `_ONTOLOGY_RETRY_LIMIT_HEAD`：第二次进来发现 `prior_error` 已经是
    自己这一轮的产物（头 + 同一个计数）就原样返回 —— 既不重复拼接，也不把真因冲掉。

    ⚠ **判据必须带上本轮计数，不能只认头**。`ontology_error` 从来不会被清空
    （`set_ontology_status` 只在 `error is not None` 时才写），而运维重置的惯例只写
    `ontology_status='pending' + ontology_retry_count=0`、**不清这一列**（`docs/` 里
    多数重置 SQL 是这个短形式）。于是新一轮再撞上限时，`prior_error` 里躺着的是
    **上一轮**那句「…(2/3)：旧原因」：只认头就会原样返回它，把上一轮的结论连同旧计数
    一起当成本轮结论 —— 看的人没有理由怀疑。认到「头 + 旧计数」时把旧计数剥掉、
    只留下原因，再按本轮计数重新拼。

    没拿到真因就**维持原样的计数文案**（不补「原因未记录」）：存量失败文档本来就是这个
    字符串，凭空改文案会让它们在下一轮闸门里无意义地变更一次。
    """
    head = f"{_ONTOLOGY_RETRY_LIMIT_HEAD} ({retry_count}/{max_retries})"
    prior = (prior_error or "").strip()
    if prior.startswith(head):
        # 本轮闸门自己的产物 —— 原样返回才幂等
        return prior
    if prior.startswith(_ONTOLOGY_RETRY_LIMIT_HEAD):
        # 历史轮次的产物：剥掉旧的「头 (x/y)」段，只保留其后的原因
        _, sep, rest = prior.partition("：")
        prior = rest.strip() if sep else ""
    if not prior:
        return head
    return f"{head}：{prior}"


#: 下游任务**明确失败**的状态词。与 `task_watch_service._inspect_rag_task` 那一处
#: 用**同一个集合**：同一个 atomic-rag 任务状态在这两条链上被判成两种结果，只会让
#: 「看门狗说失败、对账说还在跑」这种事发生。
_ONTOLOGY_TASK_FAILED_STATUSES = frozenset({"failed", "cancelled", "canceled", "error"})


def _ontology_task_failure_detail(status_info: dict, rag_status: str) -> str:
    """从下游失败的任务状态里取真因；三个键都没有就按状态词造一句。

    `error` 排在最前：这一支问的下游后端由 **compile 步的粘性值**决定
    （`rag` → atomic-rag，`ontology_compile` → P2 新服务，见 `_fetch_task_status` 的
    `force_backend` 一节），而 `error` 是两边归一后**共同的**字段名
    （新服务侧的 `ontology_error` 由 `compile_backend.normalize_status_info` 别名过来）；
    `error_message` 是历史别名，留着只是兜底。取不到任何键时给出一句「状态是 X」——
    总比落一个空字符串强：空串会让「读不到真因」与「真因就是空」两件事长得一模一样。
    """
    for key in ("error", "error_message", "message"):
        detail = str(status_info.get(key) or "").strip()
        if detail:
            return detail
    return f"下游任务状态 {rag_status}（未带原因）"


def _is_empty_ontology_output(status_info: dict) -> bool:
    """抽取器明确报告「本文档无本体内容」——合法终态，不重试。

    判据必须同时满足：无实体无关系 **且** 无任何错误信息。只看
    ontology_data 会把「LLM 超时导致的空产出」也判成终态，掩盖真失败；
    实测 10s.mp4（2 chunk 短视频）为 ontology_status='failed' 但
    ontology_error=''、error_code=None，属前者。
    """
    data = status_info.get("ontology_data") or {}
    if data.get("entities") or data.get("relations"):
        return False
    return not (
        status_info.get("ontology_error")
        or status_info.get("error")
        or status_info.get("error_code")
    )


# [jonex] P0-3：锁冲突（transient）不该用固定计数判死——Neo4j 对
# DeadlockDetected 的官方语义就是「退避重试即可」，固定计数会让偶发竞争
# 直接毁掉一份完好的抽取成果（线上 542 次死锁/3min）。退避窗口内该文档
# 不再被对账重放。
WRITE_BACKOFF_CAP_SEC_DEFAULT = 1800.0
TRANSIENT_WRITE_FAIL_LIMIT_DEFAULT = 8


def _write_backoff_seconds(n: int) -> float:
    """第 n 次连续 transient 写图失败后的退避秒数：60 * 2^n，上限 30 分钟。

    实取值为 60 → 120 → 240 → 480 → 960 → 1800(封顶) → 1800 ...。
    ``n`` 先钳到 10 再移位：连续失败次数若被写坏，2**n 会炸内存。
    """
    return min(WRITE_BACKOFF_CAP_SEC_DEFAULT, 60.0 * (2 ** min(int(n), 10)))


def _transient_write_fail_limit() -> int:
    """连续 transient 写图失败的告警阈值（ONTOLOGY_TRANSIENT_WRITE_FAIL_LIMIT，
    缺失/脏值回落 8，下限 1）。达阈值落 FAILED 并停自动重抽，避免无限退避占资源。"""
    try:
        n = int(os.environ.get("ONTOLOGY_TRANSIENT_WRITE_FAIL_LIMIT")
                or TRANSIENT_WRITE_FAIL_LIMIT_DEFAULT)
    except (TypeError, ValueError):
        return TRANSIENT_WRITE_FAIL_LIMIT_DEFAULT
    return max(1, n)


def _retry_gate_reason(doc, now=None) -> str:
    """对账重放的门闩。返回空串 = 放行；否则返回拦截原因（供日志）。

    - ``ontology_auto_retry_disabled``：写图持续失败已判定需人工介入，
      永不再自动重抽——但**保留 FAILED 终态**供 UI 展示（永久 EXTRACTING
      会让文档一直留在对账扫描集里，正是本次事故的形态）；
    - ``ontology_write_backoff_until``：transient 写图失败的退避窗口内不重放。

    脏值/过期一律放行：不能让一个写坏的字段把文档永久卡死。
    """
    fm = dict(getattr(doc, "extra_metadata", None) or {})
    if fm.get("ontology_auto_retry_disabled"):
        return "auto_retry_disabled"
    raw = fm.get("ontology_write_backoff_until")
    if raw:
        try:
            if (now or datetime.now()) < datetime.fromisoformat(str(raw)):
                return "write_backoff"
        except (ValueError, TypeError):
            return ""          # 解析失败 = 脏值，放行
    return ""


# [jonex] R1：Neo4j 写图失败后的「同任务重写」上限。
#
# 写图失败 ≠ 抽取失败：抽取产出已完整持久化在 rag 任务记录里（实测首轮 29433 实体
# / 62MB；重抽要重新花十几分钟 LLM）。原实现对写图失败一律落 FAILED，而 FAILED
# 文档会被 reconcile_ontology 判为「需要新抽取」→ 走 retry_ontology_extract
# （方法名硬编码 ontology-only，无第二个实现可选）→ 重抽只走纯文本抽取、拿不到
# content_list，表格对象路径被整个绕过，且产出与首轮口径互不相认。等于用一份口径
# 不同的重抽去覆盖一份完好的抽取成果。
#
# 故写图失败时保持 EXTRACTING + rag_task_id 不变（set_ontology_status 不动
# rag_task_id），让下一轮对账的 EXTRACTING 分支从同一任务记录取回 ontology_data
# 重写图。写图本身幂等（_rewrite_ontology_graph 先 delete_by_document 再 merge），
# 重写无副作用；历史「旧 schema 结果回写」隐患已由入口的代次 fencing（P0-I）与
# schema 版本 fencing（P1-E）挡住。
# 本上限只防确定性失败（如唯一约束冲突）无限重写：超限才落 FAILED，交回重抽路径。
ONTOLOGY_WRITE_RETRIES_DEFAULT = 5


def _ontology_write_retries() -> int:
    """写图同任务重写上限（ONTOLOGY_WRITE_RETRIES，缺失/脏值回落 5，下限 1）。"""
    try:
        n = int(os.environ.get("ONTOLOGY_WRITE_RETRIES") or ONTOLOGY_WRITE_RETRIES_DEFAULT)
    except (TypeError, ValueError):
        return ONTOLOGY_WRITE_RETRIES_DEFAULT
    return max(1, n)


class ReconciliationService:
    """Background-only cross-tenant scans. Do not use from request handlers."""

    # ── public entry points (called by the 30s loop) ───────────

    async def reconcile_documents(self, limit: int = 50) -> dict:
        """Scan PARSING + INGESTING docs and reconcile each one."""
        async with get_db_session() as session:
            repo = KnowledgeDocumentRepository(session)
            docs = await repo.list_by_status_for_reconciliation(
                [DocStatus.PARSING, DocStatus.INGESTING], limit=limit,
                # [jonex] Ontology 库接入任务中心：正被接管的文档不归这两趟管 ——
                # 它的 PARSING 是任务中心链的第一步，按老形态去问会得到错误结论
                # （patrol 判超时后会重新 insert，与任务中心链分叉、产出重复向量）。
                #
                # ⚠️ **这里刻意不传 `task_center_exclude_states`**（2026-10-14）：
                # 本趟是**收尾**（`_reconcile_one` 只轮询引擎 → `_handle_completed`
                # / `_handle_failed`），必须用默认的 `ACTIVE_STATES` —— **交还
                # （`released`）的文档也得能进来**。交还时那条在途解析任务号是靠本趟
                # 兜住的（`hand_over_in_flight_task_id` 把任务号抄给文档行，本趟接着
                # 轮询收尾）；换成 `MANAGED_STATES` 会把它们一起挡在门外，文档永久卡
                # `parsing`。要拦住的是"替它发起工作"，那是 `patrol_parsing_timeout`，
                # 不是这里。
                exclude_task_center_managed=True,
            )

        updated, failed, skipped = 0, 0, 0
        for doc in docs:
            try:
                result = await self._reconcile_one(doc)
                if result == "updated":
                    updated += 1
                elif result == "skipped":
                    skipped += 1
            except Exception:
                failed += 1
                logger.warning("Failed to reconcile document %s", doc.id, exc_info=True)

        return {"checked": len(docs), "updated": updated, "skipped": skipped, "failed": failed}

    async def reconcile_ontology(self, limit: int = 50) -> dict:
        """Scan ontology pending/failed docs and retry extraction.

        [jonex] P1-F：最大在途 ontology task 数限流（ONTOLOGY_MAX_INFLIGHT，默认 100）——
        在途（EXTRACTING）过多时本轮少领/不领，避免打满上游 LLM。SKIP LOCKED 领取避免多实例重复。
        """
        max_inflight = int(os.getenv("ONTOLOGY_MAX_INFLIGHT", "100"))
        async with get_db_session() as session:
            repo = KnowledgeDocumentRepository(session)
            inflight = await repo.count_by_ontology_status([OntologyStatus.EXTRACTING])
            # 预留额度：本轮最多新领 (max_inflight - inflight) 个 PENDING/FAILED；
            # EXTRACTING 文档仍需扫描以轮询收尾，故 fetch limit 不缩减，仅在提交新任务时受限。
            effective_limit = limit
            docs = await repo.list_by_ontology_status_for_reconciliation(
                [OntologyStatus.PENDING, OntologyStatus.FAILED, OntologyStatus.EXTRACTING],
                limit=effective_limit,
                skip_locked=True,
                # [jonex] Ontology 库接入任务中心：被接管的文档不再由对账**发起**抽取
                # （那会和任务中心链的 compile 步撞车）。这是**唯一的例外**——
                # EXTRACTING 是收尾不是新提交：写 Neo4j 的唯一入口 _handle_completed
                # 就挂在这条路上，排除掉 compile 步永远收不了尾。详见仓储的同名参数。
                exclude_task_center_managed=True,
                task_center_kept_statuses=[OntologyStatus.EXTRACTING],
                # ⚠️ **必须传 `MANAGED_STATES`（含 `released`）**（2026-10-14 加）：
                # 默认的 `ACTIVE_STATES` 不含 `released`，于是运行方把卡一交还
                # （看门狗等满 `TC_NO_DISPATCH_SEC` / 链路判死），这些文档就落回本趟的
                # 扫描面、被**重新发起抽取**。
                #
                # 实测（2026-10-14，空间 9ce36c69…）：`compile` 步因全局并发名额占满
                # 从未被派发，16:39:27 看门狗交还，**16:40 就冒出 31 次
                # `ontology_extract`**、16:41 文档 `ontology_status=ready` —— 活被旧对账
                # 干完了，而任务中心那条 `compile` 永远停在「等待中」。交还只说明**看门狗
                # 放弃等待**，任务中心那条链还活着（任务仍是 `running`），授权链没断。
                task_center_exclude_states=MANAGED_STATES,
            )

        budget = max(0, max_inflight - inflight)  # 本轮可新提交的 ontology-only 任务数
        queued = 0
        for doc in docs:
            try:
                # EXTRACTING 文档只做轮询收尾（不占用新提交预算）；
                # PENDING/FAILED 需要新提交，受 budget 限制。
                is_new_submit = doc.ontology_status != OntologyStatus.EXTRACTING.value
                if is_new_submit and budget <= 0:
                    continue
                if await self._retry_ontology_one(doc):
                    if is_new_submit:
                        budget -= 1
                    queued += 1
            except Exception:
                logger.warning("Ontology retry failed for doc %s", doc.id, exc_info=True)

        return {"checked": len(docs), "queued": queued, "inflight": inflight}

    async def patrol_parsing_timeout(self, limit: int = 50) -> dict:
        """扫描 PARSING/INGESTING 超时的文档并处置。

        [jonex] R1 探活优化：
        - SOFT 超时（RAG_TASK_SOFT_TIMEOUT_SEC，默认 3600s）到点先探活，任务仍在处理/排队
          则继续等待，绝不无脑重推；
        - HARD 超时（RAG_TASK_HARD_TIMEOUT_SEC，默认 21600s=6h）兜底，超限即使 alive 也判死；
        - 探测失败不即判死（RAG_TASK_PROBE_FAIL_MAX，默认 3 次连续失败才判 dead）；
        - INGESTING 文档绝不 re-insert（已过 P1/P2，重推会丢弃进度并加重 LightRAG 负载）；
        - INGESTING 失败不动 parsing_retry_count（该字段语义仅代表 P1/P2 解析重试）。
        集成在 _reconcile_loop() 30 秒循环中，不额外引入 APScheduler。
        """
        SOFT = int(os.getenv("RAG_TASK_SOFT_TIMEOUT_SEC", "3600"))
        HARD = int(os.getenv("RAG_TASK_HARD_TIMEOUT_SEC", "21600"))
        PROBE_FAIL_MAX = int(os.getenv("RAG_TASK_PROBE_FAIL_MAX", "3"))

        async with get_db_session() as session:
            repo = KnowledgeDocumentRepository(session)
            docs = await repo.list_by_status_for_reconciliation(
                [DocStatus.PARSING, DocStatus.INGESTING], limit=limit,
                # [jonex] Ontology 库接入任务中心：正被接管的文档不归这两趟管 ——
                # 它的 PARSING 是任务中心链的第一步，按老形态去问会得到错误结论
                # （patrol 判超时后会重新 insert，与任务中心链分叉、产出重复向量）。
                #
                # ⚠️ **必须传 `MANAGED_STATES`（含 `released`）**（2026-10-14 加）：
                # 本趟是**发起**（判超时后 `get_rag_client().insert(...)` 重投），
                # 而 `released` 只是"看门狗不盯了"，文档仍归任务中心管。用默认的
                # `ACTIVE_STATES` 会让交还的文档落进本趟扫描面 → 被重投一份解析，
                # 正是仓储 docstring 点名的"与任务中心链彻底分叉、产出重复向量"。
                exclude_task_center_managed=True,
                task_center_exclude_states=MANAGED_STATES,
            )

        now = datetime.now()  # [jonex] P0-2: naive 本地时间，与 DB 写入口径一致
        timed_out = 0
        retried = 0
        exhausted = 0
        waiting = 0

        for doc in docs:
            # [jonex] P0-3: 计时基准优先用 parsing_started_at（进入 PARSING/INGESTING 时打点），
            # 兜底用 updated_at。解耦 TimestampMixin.updated_at onupdate 心跳污染：
            # _death_candidate 每 30s 写 DB → onupdate 刷新 updated_at → 旧实现永远到不了 SOFT。
            _pm = dict(doc.extra_metadata or {})
            started_at_str = _pm.get("parsing_started_at")
            if started_at_str:
                try:
                    started_at = datetime.fromisoformat(str(started_at_str))
                    if started_at.tzinfo is not None:
                        started_at = started_at.astimezone().replace(tzinfo=None)
                    elapsed_seconds = (now - started_at).total_seconds()
                except (ValueError, TypeError, OSError):
                    started_at = None
            else:
                started_at = None

            if started_at is None:
                # 兜底：旧文档没有 parsing_started_at，回退到 updated_at
                updated_at = doc.updated_at
                if updated_at is None:
                    continue
                if updated_at.tzinfo is not None:
                    updated_at = updated_at.astimezone().replace(tzinfo=None)
                elapsed_seconds = (now - updated_at).total_seconds()

            if elapsed_seconds < SOFT:
                continue

            timed_out += 1

            meta = dict(doc.extra_metadata or {})
            retry_count = meta.get("parsing_retry_count", 0)

            # ── R1-E 探活：查任务真实状态 ──
            alive = False
            rag_status = ""
            current_step = ""
            probe_failures = meta.get("consecutive_probe_failures", 0)

            if doc.rag_task_id:
                # [jonex] P0-d：统一取状态入口（超大任务 JSON 直读磁盘 / HTTP 失败回退
                # 磁盘）。探活失败 ≠ 任务死了：大任务 JSON 会让 Sidecar 的转发 worker
                # 退出，而那一刻任务通常刚好跑完（本次事故：04:28:55 完成 → 04:29:20 起
                # 每次探活都断连 → 04:31:51 被判「知识入库超时（已 INGESTING 103
                # 分钟）」）。
                st = await _fetch_task_status(
                    doc.tenant_id,
                    (doc.knowledge_base_id
                     or (doc.extra_metadata or {}).get("knowledge_base_id", "")),
                    doc.rag_task_id, doc_id=doc.id, doc=doc,
                )
                if st is None:
                    probe_failures += 1
                    meta["consecutive_probe_failures"] = probe_failures
                    if probe_failures < PROBE_FAIL_MAX:
                        # 单次探测失败 → 保守跳过本轮，不判死
                        waiting += 1
                        logger.info(
                            "Patrol→probe failed, skip this round: doc_id=%s waited=%.0fmin failures=%d/%d",
                            doc.id, elapsed_seconds / 60, probe_failures, PROBE_FAIL_MAX,
                        )
                        async with get_db_session() as session:
                            repo_s = KnowledgeDocumentRepository(session)
                            doc.extra_metadata = meta
                            session.add(doc)
                            await session.commit()
                        continue
                    # 连续失败达上限 → 判 dead
                    alive = False
                else:
                    rag_status = st.get("status") or st.get("state") or ""
                    current_step = st.get("current_step") or ""
                    # [jonex] 白名单在 `ingest_backend`（见 _reconcile_one 同处注释）
                    alive = rag_status in ingest_backend.RAG_ALIVE_STATUSES
                    # 探测成功 → 清零失败计数
                    if probe_failures > 0:
                        meta["consecutive_probe_failures"] = 0
            else:
                alive = False

            # ── [jonex] P0-d：任务已完成、文档尚未落位 → 只等不判死 ──
            # completed 不在上方 alive 白名单里，不拦的话下面的 INGESTING 分支会立刻
            # 落 FAILED（patrol 与 reconcile 是两条独立调度，patrol 抢先跑就会误杀一个
            # 已经跑完的任务）。收尾归 reconcile，超过 HARD*2 仍不放行，避免 reconcile
            # 侧持续失败时文档被永久挂住。
            # 只拦 INGESTING：PARSING 阶段的既有 re-insert 行为不动，避免连带改语义。
            if (
                rag_status == "completed"
                and doc.status == DocStatus.INGESTING.value
                and elapsed_seconds < HARD * 2
            ):
                waiting += 1
                logger.info(
                    "Patrol→任务已完成，等待对账收尾: doc_id=%s task_id=%s waited=%.0fmin",
                    doc.id, doc.rag_task_id, elapsed_seconds / 60,
                )
                if probe_failures > 0:
                    async with get_db_session() as session:
                        repo_s = KnowledgeDocumentRepository(session)
                        doc.extra_metadata = meta
                        session.add(doc)
                        await session.commit()
                continue

            # ── 未到硬上限且任务还活着 → 只是排队/慢，继续等 ──
            if alive and elapsed_seconds < HARD:
                # [jonex] R5-a：processing + cleanup 卡死兜底 —— 不再等 6h HARD，
                # 按删除量动态超时判死（与 _handle_failed 同口径），覆盖 patrol
                # 对 processing 态盲区。reparse 进入 cleanup 后任务 status 为
                # processing，旧 patrol 只看 alive 就继续等 → 永久卡 ingesting。
                if current_step == "cleanup":
                    stuck_timeout = self._cleanup_stuck_timeout(st)
                    if elapsed_seconds >= stuck_timeout:
                        logger.error(
                            "Patrol PARSING+cleanup stuck→FAILED: doc_id=%s task_id=%s "
                            "waited=%.0fmin current_step=%s",
                            doc.id, doc.rag_task_id, elapsed_seconds / 60, current_step,
                        )
                        async with get_db_session() as session:
                            repo_s = KnowledgeDocumentRepository(session)
                            await repo_s.set_status(
                                doc, DocStatus.FAILED,
                                error_message=f"文档清理超时（cleanup 卡死 {elapsed_seconds/60:.0f} 分钟），请重试",
                            )
                        continue
                waiting += 1
                logger.info(
                    "Patrol→仍在处理/排队，继续等待: doc_id=%s waited=%.0fmin rag_status=%s current_step=%s",
                    doc.id, elapsed_seconds / 60, rag_status, current_step,
                )
                # 持久化可能已清零的 probe 计数
                if meta.get("consecutive_probe_failures", 0) == 0 and probe_failures > 0:
                    async with get_db_session() as session:
                        repo_s = KnowledgeDocumentRepository(session)
                        doc.extra_metadata = meta
                        session.add(doc)
                        await session.commit()
                continue

            # ── R1-C：INGESTING 绝不 re-insert ──
            is_ingesting = doc.status == "ingesting"  # forward-compatible with Batch 2-B
            if is_ingesting:
                # [jonex] 问题2：判死前探活。INGESTING 阶段 LightRAG 抽取可能
                # 极慢（3~18s/chunk × N千 chunk），健康任务在 HARD 内未完成
                # 是正常的。alive 且未超绝对上限则继续等待，避免误杀（如本例
                # 1878 chunk 文档在 6h HARD 时实际仍在正常入库）。
                INGESTING_CEIL = HARD * 2
                if alive and elapsed_seconds < INGESTING_CEIL:
                    waiting += 1
                    logger.info(
                        "Patrol→INGESTING 仍在入库，继续等待: doc_id=%s waited=%.0fmin "
                        "rag_status=%s current_step=%s",
                        doc.id, elapsed_seconds / 60, rag_status, current_step,
                    )
                    continue

                error_msg = (
                    f"知识入库超时（已 INGESTING {elapsed_seconds/60:.0f} 分钟），"
                    f"任务状态={rag_status or 'unknown'}"
                )
                if elapsed_seconds >= HARD:
                    logger.error(
                        "Patrol HARD timeout INGESTING→FAILED: doc_id=%s task_id=%s "
                        "waited=%.0fmin rag_status=%s current_step=%s",
                        doc.id, doc.rag_task_id, elapsed_seconds / 60, rag_status, current_step,
                    )
                async with get_db_session() as session:
                    repo_s = KnowledgeDocumentRepository(session)
                    fresh = await repo_s.set_status(doc, DocStatus.FAILED, error_message=error_msg)
                    # #6 计数隔离：INGESTING 失败用独立标记，不动 parsing_retry_count
                    # [jonex] P0-新: N2 规范 — set_status 返回 fresh 实例（已装进 session），
                    # 在 fresh 上写 metadata；禁止 session.add(detached doc)。
                    fm = dict(fresh.extra_metadata or {})
                    fm["ingesting_failed"] = True
                    fresh.extra_metadata = fm
                logger.warning(
                    "Patrol INGESTING→FAILED (no re-insert): doc_id=%s waited=%.0fmin",
                    doc.id, elapsed_seconds / 60,
                )
                continue

            # ── PARSING：原有 FAILED + re-insert 路径 ──

            if retry_count >= 3:
                exhausted += 1
                if elapsed_seconds >= HARD:
                    logger.error(
                        "Patrol HARD timeout PARSING→FAILED(exhausted): doc_id=%s task_id=%s "
                        "waited=%.0fmin rag_status=%s current_step=%s retries=%d",
                        doc.id, doc.rag_task_id, elapsed_seconds / 60,
                        rag_status, current_step, retry_count,
                    )
                async with get_db_session() as session:
                    repo_s = KnowledgeDocumentRepository(session)
                    await repo_s.set_status(
                        doc, DocStatus.FAILED,
                        error_message=f"解析超时（已 PARSING {elapsed_seconds/60:.0f} 分钟），重试次数已用尽",
                    )
                logger.warning(
                    "Patrol timeout exhausted: doc_id=%s, waited=%.0fmin, retries=%d",
                    doc.id, elapsed_seconds / 60, retry_count,
                )
                continue

            # 决策 C：HARD 超限置 FAILED 时打 ERROR 告警日志
            if elapsed_seconds >= HARD:
                logger.error(
                    "Patrol HARD timeout PARSING→re-insert: doc_id=%s task_id=%s "
                    "waited=%.0fmin rag_status=%s current_step=%s retry=%d/3",
                    doc.id, doc.rag_task_id, elapsed_seconds / 60,
                    rag_status, current_step, retry_count + 1,
                )

            # Mark as FAILED first, then retry
            async with get_db_session() as session:
                repo_s = KnowledgeDocumentRepository(session)
                await repo_s.set_status(
                    doc, DocStatus.FAILED,
                    error_message=f"解析超时（已 PARSING {elapsed_seconds/60:.0f} 分钟），自动重试第 {retry_count + 1} 次",
                )

            # Re-submit to RAG
            # [jonex] kb_type 分流：llm-wiki 文档走 parse_only（不抽本体、不写 LightRAG），
            # 产物分支（openkb→content.md / jonex_wiki→content_list.json）随 kb_type 下发。
            from .kb_type_service import get_kb_type
            kb_type = await get_kb_type(doc.tenant_id, doc.knowledge_base_id)
            is_llm_wiki = kb_type in LLM_WIKI_TYPES
            schema = None
            if not is_llm_wiki:
                try:
                    from .ontology_compiler import OntologyCompiler
                    schema = await OntologyCompiler().get_compiled_schema(
                        doc.tenant_id, doc.knowledge_base_id, auto_compile=True,
                    )
                except Exception as exc:
                    logger.warning(
                        "Patrol schema check failed for doc %s, proceeding: %s", doc.id, exc,
                    )
                    schema = None

            try:
                rag_result = await get_rag_client().insert(
                    file_path=doc.file_path,
                    tenant_id=doc.tenant_id,
                    knowledge_base_id=doc.knowledge_base_id,
                    document_id=doc.id,
                    ontology_schema=schema,
                    storage_backend=doc.storage_backend or "local",
                    storage_key=doc.storage_key,
                    execution_mode="parse_only" if is_llm_wiki else "full",
                    kb_type=kb_type,
                )

                new_status = DocStatus.READY if not rag_result.get("task_id") else DocStatus.PARSING

                async with get_db_session() as session:
                    repo_s = KnowledgeDocumentRepository(session)
                    fresh = await repo_s.set_status(
                        doc, new_status,
                        rag_task_id=rag_result.get("task_id"),
                        rag_doc_ids=rag_result.get("doc_ids") or rag_result.get("document_ids") or [],
                    )
                    # [jonex] P0-新: N2 规范 — set_status 返回 fresh 实例（已装进 session），
                    # 在 fresh 上写 metadata；禁止 session.add(detached doc)。
                    fm = dict(fresh.extra_metadata or {})
                    fm["parsing_retry_count"] = retry_count + 1
                    # [jonex] 这条路重投发生在 atomic-rag（上面的
                    # `get_rag_client().insert`），粘性值必须与之一致 —— 查询侧
                    # 按粘性值派取状态，值指错链时那份 task_id 在对面不存在，
                    # 对账会把正在跑的文档判死。
                    # （与 `document_service.reparse_document` 同一条纪律）
                    fm["ingest_backend"] = "rag"
                    # [jonex] P0-b: 重推成功时重置计时锚点，每次 attempt 重新计时。
                    # 否则 elapsed 早已越过 SOFT/HARD，下一个 patrol 周期立即判超时，
                    # 3 次重试在 90 秒内烧完，健康大文档被误杀。
                    fm["parsing_started_at"] = datetime.now().isoformat()
                    fresh.extra_metadata = fm

                retried += 1
                logger.info(
                    "Patrol retry queued: doc_id=%s, retry=%d/%d, status=%s",
                    doc.id, retry_count + 1, 3, new_status.value,
                )

            except Exception as exc:
                logger.warning(
                    "Patrol re-insert failed for doc %s, left as FAILED: %s", doc.id, exc,
                )

        return {
            "checked": len(docs),
            "timed_out": timed_out,
            "retried": retried,
            "exhausted": exhausted,
            "waiting": waiting,
        }

    # ── per-doc reconciliation ─────────────────────────────────

    async def _reconcile_one(self, doc) -> str:
        """Reconcile a single PARSING document. Returns 'updated' | 'skipped'."""
        # [jonex] R2: task_id 为空 → 走查证链（不再直接 _handle_not_found 一拍即死）
        if not doc.rag_task_id:
            return await self._verify_chain(doc, entry="null_task_id")

        # [jonex] P0-d：统一取状态入口 —— 超大任务 JSON 直读磁盘（HTTP 必崩，
        # 见 _load_task_status_from_disk 注释），常规走 HTTP、失败回退磁盘。
        # 拿不到状态就跳过本轮，否则「已完成却收不了尾」，文档会一直卡
        # PARSING/INGESTING 直到被 patrol 误判超时。
        status_info = await _fetch_task_status(
            doc.tenant_id,
            (doc.knowledge_base_id
             or (doc.extra_metadata or {}).get("knowledge_base_id", "")),
            doc.rag_task_id, doc_id=doc.id, doc=doc,
        )
        if status_info is None:
            logger.warning("Reconcile query failed for doc %s: HTTP+disk 均无状态", doc.id)
            return "skipped"

        rag_status = status_info.get("status") or status_info.get("state", "")

        # [jonex] P0-2: 白名单清零 —— 只对「在途/健康」状态清零判死计数。
        # failed / not_found / 未知状态不清零，让 _death_candidate 连续多拍累积到 MAX。
        # 原实现 rag_status != "not_found" 会把 failed 也清零，导致 task_failed 判死
        # 计数每 30s 被清零→写1，永远到不了 MAX=3，文档永久卡 PARSING。
        # not_found 不走这里（进入 _verify_chain → _death_candidate），例外不参与计数。
        # [jonex] P1-c: TaskStatus 合法值: created / queued / processing / completed
        # / failed / cancelled
        # [jonex] 白名单在 `ingest_backend`（两份白名单共用一处定义，分开写迟早漂移）
        _ALIVE_STATES = ingest_backend.RAG_RECONCILE_ALIVE_STATUSES
        if rag_status in _ALIVE_STATES:
            await self._clear_death_verdict(doc)

        if rag_status == "completed":
            return await self._handle_completed(doc, status_info)
        elif rag_status == "failed":
            return await self._handle_failed(doc, status_info)
        elif rag_status == "not_found":
            # [jonex] R2: not_found → 也走查证链（不再直接 _handle_not_found）
            return await self._verify_chain(doc, entry="not_found")
        elif rag_status == "cancelled":
            # [jonex] P1-c: cancelled 任务直接落 FAILED。用户主动取消等同于确认放弃，
            # 不应无限期 keep PARSING 等 patrol 兜底。
            # 代次 fencing：旧代次 cancelled 不覆盖新代次 doc 状态。
            task_generation = int(status_info.get("content_generation", 0) or 0)
            if task_generation < int(getattr(doc, "content_generation", 0) or 0):
                logger.info(
                    "Reconcile→skip cancelled(stale generation): doc_id=%s task_gen=%s doc_gen=%s",
                    doc.id, task_generation, getattr(doc, "content_generation", 0),
                )
                return "skipped"
            logger.info("Reconcile→FAILED(task cancelled): doc_id=%s", doc.id)
            return await self._finalize_failure(
                doc, verdict="task_cancelled",
                error_msg="任务已被取消",
            )
        else:
            # [jonex] 细粒度落库（设计 §9.5）：task 进入本体抽取（ontology_status=extracting）
            # ⟹ parse+push 已完成 ⟹ 文档已可搜索。把仍处于 PARSING 的文档提前落成
            # READY+EXTRACTING，使线性状态「编译中」在首次上传时真实可见。
            # 只提前置 EXTRACTING，绝不置 PENDING——否则 reconcile_ontology 会对 pending
            # 文档直接 claim+提交新的 ontology-only 重抽，与 insert 任务内抽取重复触发。
            # 收尾交给 reconcile_ontology 的 EXTRACTING 分支轮询同一 rag_task_id（insert 任务）。
            if (
                os.getenv("RECONCILE_REFLECT_COMPILING", "true").lower() in ("1", "true", "yes", "on")
                and doc.status in (DocStatus.PARSING.value, DocStatus.INGESTING.value)
                and status_info.get("ontology_status") == "extracting"
            ):
                # 代次 fencing：与 _handle_completed 一致，旧代次任务不提前置位
                task_gen = int(status_info.get("content_generation", 0) or 0)
                doc_gen = int(getattr(doc, "content_generation", 0) or 0)
                if task_gen < doc_gen:
                    logger.debug(
                        "Reconcile→skip reflect(stale gen): doc_id=%s task_gen=%s doc_gen=%s",
                        doc.id, task_gen, doc_gen,
                    )
                    return "skipped"
                doc_ids = status_info.get("lightrag_doc_ids") or status_info.get("doc_ids") or []
                async with get_db_session() as session:
                    repo = KnowledgeDocumentRepository(session)
                    # 保留 rag_task_id（仍指向 insert 任务）；仅更新 status + rag_doc_ids
                    # [jonex] N3: 恢复 READY 时清零判死计数（与 set_status 同 session）
                    fresh = await repo.set_status(doc, DocStatus.READY, rag_doc_ids=doc_ids)
                    fm = dict(fresh.extra_metadata or {})
                    fm.pop("death_verdict_count", None)
                    fm.pop("death_verdict_reason", None)
                    fm.pop("death_verdict_error", None)
                    fresh.extra_metadata = fm
                    await repo.set_ontology_status(doc, OntologyStatus.EXTRACTING)
                    await session.commit()
                logger.info(
                    "Reconcile→READY+EXTRACTING (compiling visible): doc_id=%s", doc.id,
                )
                return "updated"
            # ── [jonex] R1-B：INGESTING 判定 ──
            # llm-wiki KB（parse_only）不推 LightRAG，跳过 INGESTING 判定
            kb_type = (doc.extra_metadata or {}).get("kb_type")
            if not kb_type:
                from .kb_type_service import get_kb_type
                kb_type = await get_kb_type(doc.tenant_id, doc.knowledge_base_id)

            if kb_type not in LLM_WIKI_TYPES and doc.status in (DocStatus.PARSING.value, DocStatus.INGESTING.value):
                current_step = status_info.get("current_step") or ""
                progress = status_info.get("progress", 0) or 0

                if current_step == "push_chunks":
                    async with get_db_session() as session:
                        repo = KnowledgeDocumentRepository(session)
                        await repo.set_status(doc, DocStatus.INGESTING)
                        await session.commit()
                    logger.info(
                        "Reconcile→INGESTING (push_chunks): doc_id=%s, progress=%s",
                        doc.id, progress,
                    )
                    return "updated"

                # 兜底：processing + progress>0 但 current_step 无法精确判定（已过解析）
                if rag_status == "processing" and progress > 0:
                    async with get_db_session() as session:
                        repo = KnowledgeDocumentRepository(session)
                        await repo.set_status(doc, DocStatus.INGESTING)
                        await session.commit()
                    logger.info(
                        "Reconcile→INGESTING (fallback, progress>0): doc_id=%s "
                        "rag_status=%s current_step=%s progress=%s",
                        doc.id, rag_status, current_step, progress,
                    )
                    return "updated"

            # processing / pending — keep current status
            logger.debug(
                "Reconcile→keep %s: doc_id=%s, rag_status=%s, progress=%s",
                doc.status, doc.id, rag_status, status_info.get("progress"),
            )
            return "skipped"

    async def _apply_synonyms(self, tenant_id: str, kb_id: str, ont_data: dict) -> dict:
        """[jonex] 写图前 KB 级同义词归一（效果 Y）。

        直查同义词表构建索引，对 ont_data 的 entities/relations 做归一 + 合并。
        受环境变量 ONTOLOGY_SYNONYM_MERGE_ENABLED（默认 true）总开关控制；
        任何异常降级返回原 ont_data，不阻塞写图。
        """
        if os.getenv("ONTOLOGY_SYNONYM_MERGE_ENABLED", "true").lower() not in ("1", "true", "yes", "on"):
            return ont_data
        try:
            from ..repository.ontology_synonym_repository import OntologySynonymRepository
            from .synonym_normalizer import build_synonym_index, normalize_ont_data

            async with get_db_session() as session:
                groups = await OntologySynonymRepository(session).list_all_by_kb(tenant_id, kb_id)
            index = build_synonym_index(groups)
            if not index:
                return ont_data
            before = len(ont_data.get("entities", []) or [])
            result = normalize_ont_data(ont_data, index)
            after = len(result.get("entities", []) or [])
            logger.info(
                "Synonym normalize: kb=%s groups=%d entities %d→%d",
                kb_id, len(groups), before, after,
            )
            return result
        except Exception as e:
            logger.warning("Synonym normalize skipped (degraded): kb=%s err=%s", kb_id, e)
            return ont_data

    async def _handle_completed(self, doc, status_info: dict, reconcile_source: str = "first") -> str:
        """Handle completed task: write Neo4j ontology (if any), then mark READY.

        reconcile_source: "first"（首次入库收尾）或 "ontology_retry"（本体重试收尾），
        用于区分 e2e_ready_ms 口径（重试路径含多轮等待，不可与首次比较）。
        """
        # [jonex] P0-I 代次 fencing：任务代次早于文档当前 content_generation → 整体作废，
        # 不改状态 / 不删数据 / 不覆盖 rag_doc_ids（交给最新代次任务收敛）。
        #
        # ⚠️ **「交给最新代次收敛」有个前提：最新代次的任务真的存在。**
        # 停止 / 删除来得比「提交」快时，那个任务**压根没被创建** —— 这时候还照旧 skip，
        # 就**没有任何人**来收尾了，文档永久卡在 `parsing`（三条出口全堵：看门狗只认
        # 「接管行在途」的文档、用户点重新解析/删除被中间态挡住、对账自己跳过）。
        # 2026-10-11 实测复现：`reparse_then_stop_immediately`（重新解析后立刻停止）——
        # 日志里那句 `Reconcile→skip(stale generation): task_gen=0 doc_gen=1` 每 30 秒
        # 重复一次，就是它的心跳。
        #
        # 所以先确认一次「有没有人在为最新代次干活」—— **看接管行**（运行方手里的凭据，
        # 与本文档同在 knowledge_base 库，**不产生任何跨服务调用**）：
        #   · 接管行仍在途（`submitting` / `managed`）→ 运行方攥着它 → 真有人管 → 跳过；
        #   · 已交还（`released`）→ 运行方早撒手了 → **没人管** → **必须自己收尾**。
        # 这个信号可靠的理由、以及「为什么不能改成让运行方上报」见
        # `_takeover_is_in_flight` 的 docstring。
        task_generation = int(status_info.get("content_generation", 0) or 0)
        doc_generation = int(getattr(doc, "content_generation", 0) or 0)
        if task_generation < doc_generation:
            if await self._takeover_is_in_flight(doc):
                logger.info(
                    "Reconcile→skip(stale generation, 最新代次在途): "
                    "doc_id=%s task_gen=%s doc_gen=%s",
                    doc.id, task_generation, doc_generation,
                )
                return "skipped"
            # 没人管了 —— **不能一直 skip**，拿手上这份结果老老实实收尾
            # （能置 READY 就置；真收不了自然走 FAILED，用户就能重新解析/删除了）。
            logger.warning(
                "Reconcile→按现有结果收尾（代次 %s<%s 但接管行已交还，最新代次没人做）: "
                "doc_id=%s",
                task_generation, doc_generation, doc.id,
            )

        ont_status = status_info.get("ontology_status", "pending")
        ont_data = status_info.get("ontology_data")
        ont_error = status_info.get("ontology_error")
        kb_id = (doc.knowledge_base_id or (doc.extra_metadata or {}).get("knowledge_base_id", ""))
        # [jonex] P0-d：任务宣告完成却没有本体数据时，先怀疑「响应被传输层截断」
        # 而非「真的没抽到东西」——补一次磁盘直读。否则 44850 个表格对象会因为
        # 前端拿不到而永远进不了图，且文档被静默当成「无候选实体」收尾。
        if not ont_data and ont_status == "completed":
            _disk = await _load_task_status_from_disk(
                doc.tenant_id, kb_id, doc.rag_task_id, doc_id=doc.id,
            )
            if _disk is not None:
                ont_data = _disk.get("ontology_data") or ont_data
        # worker 透传的分阶段耗时（见 docs/ingestion-timing-metrics-design.md §3.4 C）
        # 兼容 v1(dict) 与 v2(list) 两种 stage_timings 形态
        stage_timings_raw = status_info.get("stage_timings")
        worker_total_ms, worker_timings = _normalize_stage_timings(stage_timings_raw)
        pipeline_version = _detect_pipeline_version(stage_timings_raw)

        # [jonex] P1-E schema 版本 fencing：任务 schema 版本低于文档目标 → 旧结果，丢弃。
        # 任务版本 >= 目标版本 → 正常写入（版本升级时 task_ver 可大于 target_ver）。
        task_schema_version = int(status_info.get("schema_version", 0) or 0)
        task_schema_hash = status_info.get("schema_hash") or None
        target_schema_version = getattr(doc, "ontology_target_schema_version", None)
        schema_fenced = bool(
            target_schema_version is not None
            and task_schema_version
            and task_schema_version < target_schema_version  # only fence OLDER results
        )
        if schema_fenced and ont_status == "completed" and ont_data:
            logger.info(
                "Ontology result fenced (stale schema): doc_id=%s task_ver=%s target_ver=%s",
                doc.id, task_schema_version, target_schema_version,
            )

        # Step 1: Neo4j ontology write (before PG, consistency design)
        neo4j_ok = True
        neo4j_transient = False
        neo4j_write_ms = None
        if not schema_fenced and ont_status == "completed" and ont_data:
            _t_neo4j = time.perf_counter()
            try:
                gdao = OntologyGraphRepository(get_neo4j_driver())
                ref_rels: list = []
                # [jonex] P5-var 退化闸门：before = 图中本文档现有实体贡献。
                # [jonex] 批 6「删光重来」：先删后 merge 会让上游采样方差
                # （实测 25~39）重新变成图数据丢失——跌幅超阈值时不删不
                # merge、旧图保留、文档正常收尾。方差该在抽取侧治
                # （ONTOLOGY_NATIVE_SAMPLE_RUNS 多次采样并集），存储侧
                # 闸门是唯一兜底，不能省。
                before_count = await gdao.count_entities_by_document(
                    doc.tenant_id, kb_id, doc.id,
                )
                incoming_count = len(ont_data.get("entities", []) or [])
                guard_threshold = _env_float(
                    "ONTOLOGY_RECONCILE_DEGRADE_GUARD_THRESHOLD", 0.3,
                )
                guard_rejected = False
                if _degrade_guard_rejects(
                    before_count, incoming_count, guard_threshold,
                ):
                    guard_rejected = True
                    logger.warning(
                        "Ontology degrade guard rejected: doc_id=%s before=%d "
                        "incoming=%d threshold=%.0f%%（删光重来模式保留旧图，"
                        "文档正常收尾）",
                        doc.id, before_count, incoming_count,
                        100 * guard_threshold,
                    )
                if not guard_rejected:
                    # [jonex] KB 级同义词归一（效果 Y：实体归并）——写图前把同义词组内不同表述
                    # 归一到 canonical 并统一 entity_type，使 MERGE 主键相同的节点自然合并。
                    # 直查 DB（强一致，正确性锚点）；失败降级不阻塞写图。
                    ont_data = await self._apply_synonyms(doc.tenant_id, kb_id, ont_data)
                    # [jonex] 「先删后 merge」整段重试 + 耗尽清理——单条 transient
                    # 已在仓储层兜住，这里兜整段中途失败；终态只允许「完整的本轮
                    # 产出」或「零贡献」，不留会被检索召回的残缺图。
                    neo4j_ok, ref_rels, neo4j_transient = await _rewrite_ontology_graph(
                        gdao, doc, kb_id, ont_data,
                    )
                neo4j_write_ms = int((time.perf_counter() - _t_neo4j) * 1000)
                # [jonex] P5-var delta 观测：重写后按文档再统计（拒绝覆盖时
                # before==after）；查询失败只降级日志，不影响写图结果判定。
                try:
                    after_count = await gdao.count_entities_by_document(
                        doc.tenant_id, kb_id, doc.id,
                    )
                except Exception as delta_exc:  # noqa: BLE001 — delta 观测失败不阻断
                    logger.warning(
                        "Ontology delta count failed for doc %s: %s",
                        doc.id, delta_exc,
                    )
                    after_count = None
                logger.info(
                    "Ontology written: doc_id=%s, entities=%d, relations=%d, "
                    "ref_relations=%d, before=%d, after=%s, delta=%s",
                    doc.id,
                    len(ont_data.get("entities", [])),
                    len(ont_data.get("relations", [])),
                    len(ref_rels),
                    before_count,
                    after_count if after_count is not None else "?",
                    (after_count - before_count) if after_count is not None else "?",
                )
            except Exception as e:
                neo4j_write_ms = int((time.perf_counter() - _t_neo4j) * 1000)
                logger.error("Neo4j ontology write failed for doc %s: %s", doc.id, e)
                neo4j_ok = False

            # [jonex] 改动 52（S2）：写图成功后做 KB 级 stub 占比聚合，
            # 超阈值告警（抽取管线异常信号）。聚合失败只降级记日志，
            # 不影响文档 READY 流转。
            if neo4j_ok:
                try:
                    stub_stats = await gdao.stub_entity_stats(
                        doc.tenant_id, kb_id,
                    )
                    if _stub_ratio_alert(stub_stats["total"], stub_stats["stub"]):
                        logger.warning(
                            "Ontology stub entity ratio over threshold: kb_id=%s "
                            "doc_id=%s total=%d stub=%d ratio=%.1f%%",
                            kb_id, doc.id,
                            stub_stats["total"], stub_stats["stub"],
                            stub_stats["ratio"] * 100,
                        )
                except Exception as exc:  # noqa: BLE001 — 观测信号，失败不阻断
                    logger.warning(
                        "Stub entity ratio aggregation failed (degraded): "
                        "kb_id=%s err=%s", kb_id, exc,
                    )

        # [jonex] kb_type：openkb 管线只解析、不做本体抽取
        # 优先读 extra_metadata 快照（缓存），再查 knowledge_info 权威源兜底
        kb_type = (doc.extra_metadata or {}).get("kb_type")
        if not kb_type:
            from .kb_type_service import get_kb_type
            kb_type = await get_kb_type(doc.tenant_id, doc.knowledge_base_id)

        # Step 2: PG status update
        async with get_db_session() as session:
            repo = KnowledgeDocumentRepository(session)
            # [jonex] N3: 恢复 READY 时清零判死计数
            fresh = await repo.set_status(
                doc,
                DocStatus.READY,
                rag_doc_ids=status_info.get("lightrag_doc_ids") or status_info.get("doc_ids") or [],
            )
            fm = dict(fresh.extra_metadata or {})
            fm.pop("death_verdict_count", None)
            fm.pop("death_verdict_reason", None)
            fm.pop("death_verdict_error", None)
            # [jonex] 多模态转写警告（原子侧 _record_multimodal_warning → server-v2 状态
            # 接口白名单 → status_info）：**覆盖写**，reparse 后不累积重复条目（无警告时
            # 清掉旧 key，reparse 成功后旧失败警告不残留）。与编译侧 llm_wiki_compile_warnings
            # 并列两个来源——不写该列（编译侧 compiling/stale 会清空，对账 READY 写入
            # 会在编译开始时丢失）。
            mw = status_info.get("multimodal_warnings") or []
            if mw:
                fm["multimodal_warnings"] = mw
            else:
                fm.pop("multimodal_warnings", None)
            # [jonex] §table-grid-v2 O7: 新旧格式 chunk 残留扫描（读操作，失败不
            # 阻塞收尾）。残留 > 0 → extra_metadata 留警告（前端文档详情可见）；
            # 无残留 → 清掉旧 key（reparse 成功后旧警告不残留）。llm-wiki 跳过。
            if kb_type not in LLM_WIKI_TYPES:
                try:
                    scan = await DocumentService().scan_stale_chunks(
                        doc.tenant_id, doc.id,
                    )
                    stale_count = scan.get("stale_chunk_count") or 0
                    if stale_count:
                        fm["stale_chunks_detected"] = {
                            "count": stale_count,
                            "chunk_ids": [
                                s.get("chunk_id")
                                for s in scan.get("stale_chunks", [])
                                if s.get("chunk_id")
                            ][:20],
                            "scanned_at": datetime.now().isoformat(),
                        }
                    else:
                        fm.pop("stale_chunks_detected", None)
                except Exception as exc:  # noqa: BLE001 — 扫描失败不影响状态收尾
                    logger.warning(
                        "O7 stale-chunk scan failed for doc %s: %s", doc.id, exc,
                    )
            fresh.extra_metadata = fm

            if kb_type in LLM_WIKI_TYPES:
                # llm-wiki 无本体抽取，直接标 READY，避免 reconcile_ontology 无谓重试后置 FAILED
                await repo.set_ontology_status(doc, OntologyStatus.READY)
            elif schema_fenced and ont_status == "completed":
                # 本体结果按目标版本作废：保持 PENDING 等目标版本任务重抽，不写 applied
                await repo.set_ontology_status(doc, OntologyStatus.PENDING)
            elif ont_status == "completed" and neo4j_ok:
                # [jonex] P1-E：写图成功后记录已应用的 schema 版本/hash，
                # 同时将 target 版本同步到任务版本（编译 schema 升级后 task_ver > target_ver 为正常态）。
                await repo.set_ontology_status(
                    doc, OntologyStatus.READY,
                    applied_schema_version=(task_schema_version or None),
                    applied_schema_hash=task_schema_hash,
                )
                if task_schema_version and (
                    target_schema_version is None or task_schema_version > target_schema_version
                ):
                    doc.ontology_target_schema_version = task_schema_version
                # [jonex] 写图成功 → 清空全部写图重试状态（不跨轮累积）
                _cleared = clear_ontology_write_state(fm)
                if _cleared != fm:
                    fresh.extra_metadata = _cleared
            elif _is_empty_ontology_output(status_info):
                # [jonex] P0-4：抽取器明确报告无本体内容 = 合法终态，一次定案。
                # 必须在首次收尾就判掉：否则先落 FAILED，再被 reconcile 当
                # 「需新抽取」提交一轮真实 LLM 重抽，才在 _retry_ontology_one
                # 里判成终态（白烧一轮 LLM + UI 闪 FAILED）。
                await repo.set_ontology_status(doc, OntologyStatus.READY)
                logger.info(
                    "Ontology empty output, marked READY (no retry): doc_id=%s task=%s",
                    doc.id, doc.rag_task_id,
                )
            elif ont_status == "failed":
                await repo.set_ontology_status(doc, OntologyStatus.FAILED, error=ont_error or "本体抽取失败")
            elif not neo4j_ok:
                # [jonex] P0-3：transient 与确定性失败分流。
                # - 锁冲突（transient）：不计入同任务重写上限，改写退避窗口，
                #   窗口内对账不重放；连续失败到阈值仍落 FAILED 并停自动重抽
                #   （防无限退避占资源）。
                # - 确定性失败（唯一约束 / Cypher 错误）：退避无意义，走计数判死。
                #
                # 两种失败的共同点（R1 既有语义，不变）：写图失败 ≠ 抽取失败。
                # - 本分支只在「抽取成功 + 有 ontology_data + 未 schema fencing」时可达
                #   （neo4j_ok 仅在该分支内被置 False），即抽取成果完好、只有写图这一步失败。
                # - 未达上限时都不落 FAILED ⟹ 不会被判为「需要新抽取」⟹ 不触发
                #   ontology-only 重抽，表格对象路径与首轮抽取口径得以保留。
                # - set_ontology_status 不动 rag_task_id，下一轮 EXTRACTING 分支
                #   （_retry_ontology_one 的「EXTRACTING 且 rag_task_id」轮询）会重新轮询
                #   同一任务并再次调用本函数重写图。
                if neo4j_transient:
                    n = int(fm.get("ontology_write_transient_n") or 0) + 1
                    reached = n >= _transient_write_fail_limit()
                    new_fm = dict(fm)
                    new_fm["ontology_write_transient_n"] = n
                    if reached:
                        # 落 FAILED = 换任务：清全部同任务写图状态 + 停自动重抽
                        new_fm = clear_ontology_write_state(new_fm)
                        new_fm["ontology_auto_retry_disabled"] = True
                    else:
                        new_fm["ontology_write_backoff_until"] = (
                            datetime.now() + timedelta(seconds=_write_backoff_seconds(n))
                        ).isoformat()
                    # 单次整体赋值，且是**新对象**（JSONB 变更必被侦测）
                    fresh.extra_metadata = new_fm
                    if reached:
                        await repo.set_ontology_status(
                            doc, OntologyStatus.FAILED,
                            error=f"Neo4j 锁冲突持续 {n} 次未收敛（退避重试已停，需人工介入）",
                        )
                        logger.error(
                            "Neo4j 写图锁冲突持续未收敛，落 FAILED 并停自动重抽: doc_id=%s n=%d",
                            doc.id, n,
                        )
                    else:
                        await repo.set_ontology_status(doc, OntologyStatus.EXTRACTING)
                        logger.warning(
                            "Neo4j 写图撞锁冲突，退避 %.1fs 后重写（第 %d/%d 次）: doc_id=%s",
                            _write_backoff_seconds(n), n, _transient_write_fail_limit(), doc.id,
                        )
                else:
                    write_retries = int(fm.get("ontology_write_retries") or 0) + 1
                    write_limit = _ontology_write_retries()
                    if write_retries >= write_limit:
                        fresh.extra_metadata = clear_ontology_write_state(fm)
                        await repo.set_ontology_status(
                            doc, OntologyStatus.FAILED,
                            error=f"Neo4j 本体写入失败（同任务重写 {write_retries} 次达上限）",
                        )
                        logger.error(
                            "Neo4j 本体写入连续失败达上限，落 FAILED: doc_id=%s retries=%d/%d",
                            doc.id, write_retries, write_limit,
                        )
                    else:
                        # 保持 EXTRACTING：确定性失败的计数留着供同任务下轮重写。
                        # 两个分支统一「构造新 dict、末尾赋一次」——虽然按实测
                        # in-place 改后赋同一对象也能落库（set 事件无条件标记
                        # modified），但两种写法并存会让后来人改动时踩坑。
                        _keep = dict(fm)
                        _keep["ontology_write_retries"] = write_retries
                        fresh.extra_metadata = _keep
                        await repo.set_ontology_status(doc, OntologyStatus.EXTRACTING)
                        logger.warning(
                            "Neo4j 本体写入失败，保持 EXTRACTING 待下轮从同一任务重写（第 %d/%d 次）: doc_id=%s",
                            write_retries, write_limit, doc.id,
                        )

            await session.commit()

        # ── [jonex] llm-wiki 管线：解析完成 → 推送 Wiki 编译 ──
        #
        # ⚠️ **归任务中心管的文档不许在这里发起编译**（2026-10-14 加）：
        # 这一步是**发起**（提交编译任务 / 标 stale 让巡检去提交），而发起的权力归任务
        # 中心链（handler → `queue_task_compile` → stale → 巡检）。上面那段写 Neo4j +
        # 置 READY 是**收尾**，照做不受影响 —— 只有这一个 if 里的派发被挡。
        #
        # 挡它的理由与 A1/A2 同源：本方法是被 `_reconcile_one` / `_retry_ontology_one`
        # 轮询叫起来的，而交还（`released`）之后那两趟仍会走到这里。`released` 只说明
        # 看门狗不盯了，任务中心那条链还活着 —— 旧对账替它把 wiki 编译发起掉，就是
        # 「两套驱动器同跑」。
        #
        # 任务被删（接管行不存在）时本闸门放行，那正是把驱动权交回既有对账的时刻
        # （⚠️ 但该路径当前未对外开放，见 `TaskCenterDocState.RELEASED` 的注）。
        if kb_type in LLM_WIKI_TYPES:
            if await self._takeover_is_task_center_managed(doc):
                logger.info(
                    "Reconcile→跳过 Wiki 编译派发（文档归任务中心管，发起权不在旧对账）: "
                    "doc_id=%s kb_type=%s", doc.id, kb_type,
                )
            elif kb_type == "openkb":
                await self._dispatch_openkb_compile(doc, status_info)
            elif kb_type == "jonex_wiki":
                await self._dispatch_jonex_wiki_compile(doc, status_info)

        # 端到端墙钟：created_at 为 naive UTC（TimestampMixin: default=datetime.utcnow），
        # 必须用 datetime.utcnow() 作差（详见设计文档 §3.1 #9）。
        # 端到端墙钟：TimestampMixin 实为 datetime.now（naive 本地时间，见 jonex_core/common/entity.py:24），
        # 用 datetime.now() 作差保持同口径，避免 utcnow() 导致的 -8h 偏差（P0-2 同类 bug）。
        e2e_ready_ms = None
        if doc.created_at is not None:
            e2e_ready_ms = int((datetime.now() - doc.created_at).total_seconds() * 1000)

        logger.info("Reconcile→READY: doc_id=%s, chunks=%d", doc.id,
                     len(status_info.get("lightrag_doc_ids") or status_info.get("doc_ids") or []))
        logger.info(
            "reconcile_timing doc_id=%s source=%s pipeline_version=%s neo4j_write_ms=%s e2e_ready_ms=%s worker_total_ms=%s",
            doc.id,
            reconcile_source,
            pipeline_version or "unknown",
            neo4j_write_ms,
            e2e_ready_ms,
            worker_total_ms,
            extra={
                "event": "reconcile_timing",
                "tenant_id": doc.tenant_id,
                "knowledge_base_id": kb_id,
                "document_id": str(doc.id),
                "status": "ready",
                "reconcile_source": reconcile_source,
                "pipeline_version": pipeline_version,
                "neo4j_write_ms": neo4j_write_ms,
                "e2e_ready_ms": e2e_ready_ms,
                "worker_total_ms": worker_total_ms,
                **worker_timings,
            },
        )
        schedule_emit({
            "tenant_id": doc.tenant_id,
            "log_type": "TASK",
            "action": "document.parse_done",
            "outcome": "SUCCESS",
            "service_name": "knowledge_base",
            "resource": ResourceType.DOCUMENT.value,
            "resource_id": str(doc.id),
            "duration_ms": worker_total_ms,
            "request_params": {
                "reconcile_source": reconcile_source,
                "neo4j_write_ms": neo4j_write_ms,
                "e2e_ready_ms": e2e_ready_ms,
            },
        })
        return "updated"

    async def _takeover_is_in_flight(self, doc) -> bool:
        """这份文档的接管行**还在运行方手上**吗（= 还有人在为它干活）。

        ## 为什么要看它

        代次判据（`task_generation < doc_generation → skip`，见 `_handle_completed`）的本意是
        「旧结果别覆盖新结果，**交给最新代次收敛**」—— 而那句话**建立在「最新代次真的存在」
        这个假设上**。停止 / 删除来得比提交快时，新任务压根没被创建，**假设不成立**；
        这时候再跳过，文档就永久卡在 `parsing`（三条出口全堵：看门狗只认「接管行在途」的
        文档、用户点重新解析/删除被中间态挡住、对账自己跳过）。

        ## 为什么这个判据可靠

        接管行是**运行方手里的凭据**：它**动手之前**必先 `begin_step` 把接管行标成在途
        （那一步刻意排在调引擎**之前**）。所以「正在提交 / 正在跑」的**全程**它都是在途的
        —— 这里判「没人管」绝不会误伤正在跑的活儿。反过来，一旦交还，运行方那侧**确实
        不会再有人动它**了（看门狗只扫在途行），本文档也不该继续干等。

        ⚠️ **但「交还」不等于「任务中心那条链已经彻底结束」**（2026-10-14 更正，原文这么
        写是错的）：交还只说明**看门狗放弃等待**。实测：`compile` 子任务因全局并发名额
        占满从未被派发，看门狗等满 1800 秒交还，那一刻任务**还是 `running`**、子任务
        还在队列里 —— 只是派不出去。所以本判据回答的是「**还有人正在为它干活吗**」
        （用于**收尾**），**不是**「它归谁管」。后者见 `_takeover_is_task_center_managed`，
        两个问题别混。

        ## 为什么不用「让运行方上报」

        该上报的那个人**已经退场了**（任务中心那条链结束、运行方也没有理由再说话），
        **指望一个已经离场的人补一句话本身就是不可靠的** —— 漏报一次，文档又卡住，
        而卡住是没人兜底的。查接管行是**查事实**：事实就摆在自己库里，不会漏报、不用等谁开口。
        这也是它唯一的代价 —— 一次本地小查询，不碰任何跨服务通道。
        """
        # ⚠️ `text` 在本文件里是**函数内局部 import** 的（模块顶部没有它）——
        # 跟本文件其它几处同款，别删。
        from sqlalchemy import text

        async with get_db_session() as session:
            hit = (await session.execute(
                text(
                    "SELECT 1 FROM knowledge_base.task_center_documents "
                    "WHERE document_id = :doc_id AND state = ANY(:active_states) "
                    "LIMIT 1"
                ),
                {"doc_id": doc.id, "active_states": list(ACTIVE_STATES)},
            )).first()
        return hit is not None

    async def _takeover_is_task_center_managed(self, doc) -> bool:
        """这份文档**归任务中心管吗**（= 旧对账不许替它发起任何下游工作）。

        ## 和 `_takeover_is_in_flight` 是两个问题，别合并、别互相改坏

        | | 问的是什么 | 用哪个集合 | 用在哪 |
        |---|---|---|---|
        | `_takeover_is_in_flight` | 还有人在为它干活吗 | `ACTIVE_STATES`（不含 released） | **收尾**：没人管 → 对账自己收尾 |
        | 本方法 | 这份文档归谁管 | `MANAGED_STATES`（**含** released） | **发起**：归任务中心管 → 旧对账一步都不许发起 |

        差别只在 `released` 一格，而那一格就是整件事：交还之后**看门狗不盯了、但任务中心
        那条链还活着**（任务可能仍是 `running`，只是子任务排在队列里派不出去）。此时

          · 收尾该照做（在途任务号是交还时抄给文档行的，得有人接着轮询）→ 所以
            `_takeover_is_in_flight` 必须继续返回 False；
          · 发起**一步都不许**——发起的权力归任务中心，用户想重跑走「重新解析 /
            重新编译」两个按钮（那两条路会 `rearm_for_step` + `retry_subtask`，
            授权链完整）→ 所以本方法返回 True。

        ⚠️ **把本方法拿去当收尾判据**会让交还的文档被判成「有人在管」而永远 skip，
        文档永久卡 `parsing` —— 那正是 `_takeover_is_in_flight` 当初要治的病。
        ⚠️ **把 `_takeover_is_in_flight` 的判据改成 `MANAGED_STATES`** 是同一个错误，
        别顺手"统一口径"。

        ## 查库而不是查内存

        与 `_takeover_is_in_flight` 同构：一次本地小查询，不碰任何跨服务通道。调用方多半
        手上就有 `doc`，但接管行**不在文档表上**，只能现查。
        """
        # ⚠️ `text` 在本文件里是**函数内局部 import** 的（模块顶部没有它）——
        # 跟本文件其它几处同款，别删。
        from sqlalchemy import text

        async with get_db_session() as session:
            hit = (await session.execute(
                text(
                    "SELECT 1 FROM knowledge_base.task_center_documents "
                    "WHERE document_id = :doc_id AND state = ANY(:managed_states) "
                    "LIMIT 1"
                ),
                {"doc_id": doc.id, "managed_states": list(MANAGED_STATES)},
            )).first()
        return hit is not None

    @staticmethod
    def _cleanup_stuck_timeout(status_info: dict) -> float:
        """[jonex] R5-a：cleanup 动态超时公式 = BASE + PER_DOC × cleanup_total，套 CEIL 上限。

        供 patrol（processing+cleanup 盲区）与 _handle_failed（failed+cleanup 已覆盖）
        共享同一口径。
        """
        base = int(os.getenv("RECONCILE_CLEANUP_BASE_SEC", "600"))
        per_doc = int(os.getenv("RECONCILE_CLEANUP_PER_DOC_SEC", "15"))
        ceil = int(os.getenv("RECONCILE_CLEANUP_CEIL_SEC", "21600"))
        cleanup_total = int(status_info.get("cleanup_total", 0) or 0)
        return min(ceil, base + per_doc * cleanup_total)

    async def _handle_failed(self, doc, status_info: dict) -> str:
        """Handle failed task. Try storage fallback first before finalizing failure."""
        # [jonex] P0-J：cleanup 进行中的任务对 storage fallback 免疫——保持 PARSING，
        # 不因 LightRAG 里仍有（旧）数据就恢复 READY，避免绕过未完成的清理。
        if status_info.get("current_step") == "cleanup":
            # [jonex] 3-A + R5-a：cleanup 不再无限期 skip，按删除量动态超时判死。
            stuck_timeout = self._cleanup_stuck_timeout(status_info)
            cleanup_total = int(status_info.get("cleanup_total", 0) or 0)
            pending = int(status_info.get("cleanup_pending_count", 0) or 0)

            # 计时基准：doc.updated_at（进入 cleanup 后对账 skip 不再刷新，可作卡死时长代理）
            now = datetime.now()  # [jonex] P0-2: naive 本地时间，与 DB 写入口径一致
            updated_at = doc.updated_at
            elapsed = 0.0
            if updated_at is not None:
                if updated_at.tzinfo is not None:
                    updated_at = updated_at.astimezone().replace(tzinfo=None)
                elapsed = (now - updated_at).total_seconds()

            if elapsed >= stuck_timeout:
                err = (
                    f"补偿清理未收敛（cleanup 卡死 {elapsed/60:.0f} 分钟，"
                    f"待删 {pending}/{cleanup_total}），请删除文档后重新上传"
                )
                async with get_db_session() as session:
                    repo = KnowledgeDocumentRepository(session)
                    await repo.set_status(doc, DocStatus.FAILED, error_message=err)
                    await session.commit()
                logger.warning(
                    "Reconcile→FAILED(cleanup stuck): doc_id=%s elapsed=%.0fmin "
                    "pending=%d/%d timeout=%ds",
                    doc.id, elapsed / 60, pending, cleanup_total, stuck_timeout,
                )
                return "updated"

            logger.info(
                "Reconcile→keep %s (cleanup in progress): doc_id=%s elapsed=%.0fmin "
                "pending=%d/%d timeout=%ds",
                doc.status, doc.id, elapsed / 60, pending, cleanup_total, stuck_timeout,
            )
            return "skipped"
        # 代次 fencing：旧代次任务的失败结果不改文档状态（交给最新代次收敛）
        task_generation = int(status_info.get("content_generation", 0) or 0)
        if task_generation < int(getattr(doc, "content_generation", 0) or 0):
            logger.info(
                "Reconcile→skip failed(stale generation): doc_id=%s task_gen=%s doc_gen=%s",
                doc.id, task_generation, getattr(doc, "content_generation", 0),
            )
            return "skipped"

        # If LightRAG has the data, recover to READY instead of FAILED
        rag_doc_ids = await self._verify_via_storage(doc)
        if rag_doc_ids:
            async with get_db_session() as session:
                repo = KnowledgeDocumentRepository(session)
                # [jonex] N3: 恢复 READY 时清零判死计数（与 set_status 同 session）
                fresh = await repo.set_status(doc, DocStatus.READY, rag_doc_ids=rag_doc_ids)
                fm = dict(fresh.extra_metadata or {})
                fm.pop("death_verdict_count", None)
                fm.pop("death_verdict_reason", None)
                fm.pop("death_verdict_error", None)
                fresh.extra_metadata = fm
                await session.commit()
            logger.info("Reconcile→READY(via failed-task storage fallback): doc_id=%s", doc.id)
            return "updated"

        # [jonex] P1-5: 确定性终态跳过 3 拍确认，直接落 FAILED。
        # 连续确认是为对抗瞬时不可达（LightRAG 繁忙等），对确定性失败只是白拖 90s。
        # [jonex] P0-a: error_code 匹配 + error_msg 文本前缀兜底（兼容镜像未更新）。
        error_msg = status_info.get("error") or status_info.get("message") or "RAG 解析失败"
        error_code = status_info.get("error_code") or ""

        _DETERMINISTIC_TEXT_PREFIXES = (
            "File not found:", "OBJECT_FETCH_FAILED",
        )
        _DETERMINISTIC_TERMINAL_CODES = frozenset({
            "FILE_NOT_FOUND", "OBJECT_FETCH_FAILED",
            "PROFILE_NOT_FOUND", "PRESET_NOT_FOUND",
            "MODEL_NOT_FOUND", "CONFIG_INVALID",
        })
        if (error_code in _DETERMINISTIC_TERMINAL_CODES
                or error_msg.startswith(_DETERMINISTIC_TEXT_PREFIXES)):
            logger.info(
                "Reconcile→FAILED(deterministic terminal): doc_id=%s error_code=%s error=%s",
                doc.id, error_code or "(text match)", error_msg,
            )
            return await self._finalize_failure(doc, verdict="task_failed", error_msg=error_msg)

        return await self._death_candidate(doc, verdict="task_failed", error_msg=error_msg)

    # ── [jonex] R2 查证链 + R3 连续确认判死器 ──────────────────────

    @staticmethod
    def _build_idempotency_key(doc) -> str | None:
        """按 upload_document / reparse_document 同口径生成幂等键，供 R2-b 反查。"""
        kb_id = doc.knowledge_base_id or (doc.extra_metadata or {}).get("knowledge_base_id", "")
        gen = getattr(doc, "content_generation", 0) or 0
        if not kb_id:
            return None
        return f"insert:{doc.tenant_id}:{kb_id}:{doc.id}:{gen}"

    @staticmethod
    async def _clear_death_verdict(doc) -> None:
        """[jonex] P1-d: 恢复/成功路径清零判死计数+原因+错误。

        只在有残留时才写 DB。调用时机：
        - _reconcile_one 拿到健康 task 状态（created/queued/processing/completed）
        - _verify_chain R2-a/b/c 任一路命中
        - _handle_completed 成功收尾
        """
        meta = dict(doc.extra_metadata or {})
        if "death_verdict_count" not in meta and "death_verdict_reason" not in meta and "death_verdict_error" not in meta:
            return
        meta.pop("death_verdict_count", None)
        meta.pop("death_verdict_reason", None)
        meta.pop("death_verdict_error", None)
        async with get_db_session() as session:
            doc.extra_metadata = meta
            session.add(doc)
            await session.commit()

    async def _verify_chain(self, doc, *, entry: str) -> str:
        """R2 查证链：a（宽限期）→ b（幂等键反查）→ c（storage fallback）→ d（判死候选）。

        入口：entry="null_task_id"（task_id 为空，upload/reparse 空窗期）
              entry="not_found"（task_id 查询返回 not_found，容器重启丢状态）
        任一路命中即 skip/updated；全未命中进入 R3 _death_candidate。
        """
        doc_id = doc.id
        meta = dict(doc.extra_metadata or {})

        # ── R2-a: 提交宽限期（submit_started_at 在 GRACE 窗口内 → in-flight skip）──
        # [jonex] P1-1: 统一用 datetime.now()（naive 本地时间），与写入侧的 datetime.now().isoformat() 口径一致
        submit_at = meta.get("submit_started_at")
        if submit_at:
            try:
                submitted = datetime.fromisoformat(str(submit_at))
                grace = int(os.getenv("RECONCILE_SUBMIT_GRACE_SEC", "180"))
                if (datetime.now() - submitted).total_seconds() < grace:
                    # [jonex] N3: 任务 in-flight 正常态 → 清零判死计数
                    # [jonex] N4: elapsed 用 datetime.now()（与 submitted 同口径）
                    elapsed = (datetime.now() - submitted).total_seconds()
                    logger.info(
                        "Reconcile→skip(in-flight grace): doc_id=%s entry=%s elapsed=%.0fs",
                        doc_id, entry, elapsed,
                    )
                    await self._clear_death_verdict(doc)
                    return "skipped"
            except (ValueError, TypeError, OSError):
                pass  # 损坏的时间戳 → 穿透继续

        # ── R2-b: 幂等键反查 atomic-rag 在途任务 → 回填 rag_task_id ──
        if (
            os.getenv("RECONCILE_IDEMPOTENCY_LOOKUP_ENABLED", "true").lower() in ("1", "true", "yes", "on")
        ):
            ik = self._build_idempotency_key(doc)
            if ik:
                try:
                    result = await get_rag_client().get_task_by_idempotency_key(ik, doc.tenant_id)
                    if result.get("found"):
                        async with get_db_session() as session:
                            repo = KnowledgeDocumentRepository(session)
                            # [jonex] P1-2: 回填 task_id 后清除锚点
                            # [jonex] N2: 通过 fresh 实例持久化 metadata 变更
                            fresh = await repo.set_status(
                                doc, doc.status, rag_task_id=result["task_id"],
                            )
                            # [jonex] P1-2: 清除锚点 + [jonex] N3: 反查到在途任务 → 清零判死计数
                            fm = dict(fresh.extra_metadata or {})
                            had_anchor = fm.pop("submit_started_at", None) is not None
                            had_count = fm.pop("death_verdict_count", None) is not None
                            if had_anchor or had_count:
                                fm.pop("death_verdict_reason", None)
                                fm.pop("death_verdict_error", None)
                                fresh.extra_metadata = fm
                            await session.commit()
                        logger.info(
                            "Reconcile→backfill task_id(idempotency lookup hit): doc_id=%s "
                            "task_id=%s entry=%s",
                            doc_id, result["task_id"], entry,
                        )
                        return "updated"
                except Exception:
                    pass  # RAG 不可达 → 穿透继续

        # ── R2-c: storage fallback（现有 _verify_via_storage，原样保留）──
        _t0 = time.perf_counter()
        rag_doc_ids = await self._verify_via_storage(doc)
        storage_fallback_ms = int((time.perf_counter() - _t0) * 1000)
        if rag_doc_ids is not None:
            async with get_db_session() as session:
                repo = KnowledgeDocumentRepository(session)
                # [jonex] N3: 恢复 READY 时清零判死计数（与 set_status 同 session）
                fresh = await repo.set_status(doc, DocStatus.READY, rag_doc_ids=rag_doc_ids)
                fm = dict(fresh.extra_metadata or {})
                fm.pop("death_verdict_count", None)
                fm.pop("death_verdict_reason", None)
                fm.pop("death_verdict_error", None)
                fresh.extra_metadata = fm
                await session.commit()
            logger.info("Reconcile→READY(via storage fallback): doc_id=%s entry=%s", doc_id, entry)
            schedule_emit({
                "tenant_id": doc.tenant_id,
                "log_type": "TASK",
                "action": "document.parse_recover",
                "outcome": "SUCCESS",
                "service_name": "knowledge_base",
                "resource": ResourceType.DOCUMENT.value,
                "resource_id": str(doc_id),
                "duration_ms": storage_fallback_ms,
            })
            return "updated"

        # ── R2-d: 全未命中 → 进入判死候选（R3 连续确认）──
        return await self._death_candidate(doc, verdict=f"task_{entry}")

    async def _death_candidate(self, doc, *, verdict: str, error_msg: str | None = None) -> str:
        """R3: 连续 N 拍同一结论才判死。计数持久化在 extra_metadata。

        error_msg: 仅 task_failed 路径传入真实失败原因；null_task_id/not_found 路径
        不传 → _finalize_failure 回退 death_verdict_error（跨 verdict 保留）。
        """
        MAX = int(os.getenv("RAG_TASK_PROBE_FAIL_MAX", "3"))

        meta = dict(doc.extra_metadata or {})
        count = meta.get("death_verdict_count", 0)
        reason = meta.get("death_verdict_reason", "")

        if reason == verdict:
            count += 1
        else:
            # [jonex] P1-d: verdict 切换时清旧 death_verdict_error，
            # 防止上次 task_failed 的 FILE_NOT_FOUND 串台到这次 task_not_found 文案。
            count = 1
            reason = verdict
            meta.pop("death_verdict_error", None)

        meta["death_verdict_count"] = count
        meta["death_verdict_reason"] = reason

        # [jonex] P1-4: 保留首个真实错误跨 verdict 切换不丢失。
        if error_msg:
            meta["death_verdict_error"] = error_msg

        async with get_db_session() as session:
            doc.extra_metadata = meta
            session.add(doc)
            await session.commit()

        if count >= MAX:
            return await self._finalize_failure(doc, verdict, error_msg=error_msg)

        logger.info(
            "Reconcile→death candidate: doc_id=%s verdict=%s count=%d/%d",
            doc.id, verdict, count, MAX,
        )
        return "skipped"

    async def _finalize_failure(self, doc, verdict: str, *, error_msg: str | None = None) -> str:
        """落定 FAILED：清锚点/判死计数，写入 error_message。

        error_msg 优先级: 传入 > death_verdict_error（跨 verdict 保留）> verdict 分类。
        """
        meta = dict(doc.extra_metadata or {})

        # [jonex] P1-4: 优先取传入 error_msg，其次取 death_verdict_error（跨 verdict 保留），
        # 兜底生成通用文案。防止 verdict 从 task_failed 切到 task_not_found 时真实错误被抹掉。
        effective_error = error_msg or meta.get("death_verdict_error", "")
        if effective_error:
            err = f"{effective_error}（{verdict}）"
        else:
            err = f"RAG 任务确认丢失（{verdict}），原 task_id={doc.rag_task_id}，请重新上传"

        meta.pop("submit_started_at", None)
        meta.pop("death_verdict_count", None)
        meta.pop("death_verdict_reason", None)
        meta.pop("death_verdict_error", None)

        async with get_db_session() as session:
            repo = KnowledgeDocumentRepository(session)
            # [jonex] N2: set_status 返回 fresh 实例（get_by_id），需在 fresh 上写 metadata
            # 直接改 doc.extra_metadata 是 detached 操作，不会被持久化
            fresh = await repo.set_status(doc, DocStatus.FAILED, error_message=err)
            fresh.extra_metadata = meta
            await session.commit()

        logger.info("Reconcile→FAILED(death confirmed): doc_id=%s verdict=%s", doc.id, verdict)
        schedule_emit({
            "tenant_id": doc.tenant_id,
            "log_type": "TASK",
            "action": "document.parse_recover",
            "outcome": "FAILED",
            "service_name": "knowledge_base",
            "resource": ResourceType.DOCUMENT.value,
            "resource_id": str(doc.id),
            "error_message": err[:1000],
        })
        return "updated"

    # ── ontology retry ─────────────────────────────────────────

    async def _retry_ontology_one(self, doc) -> bool:
        """Retry ontology extraction for a single doc. Returns True if a new task was queued.
        """
        # [jonex] P0-3 门闩：已停自动重抽 / 退避窗口内的文档直接跳过。
        # 领取阶段（list_by_ontology_status_for_reconciliation）也会排除已停抽的
        # 文档，这里再拦一次覆盖退避窗口（窗口是时间条件，不适合进 SQL）。
        gate = _retry_gate_reason(doc)
        if gate:
            logger.debug("Ontology retry gated (%s), skipping: doc_id=%s", gate, doc.id)
            return False

        kb_id = (doc.knowledge_base_id or (doc.extra_metadata or {}).get("knowledge_base_id", ""))

        # Step 0: Ensure compiled schema exists before retry (Phase 4.2)
        schema = None
        schema_version = 0
        try:
            from .ontology_compiler import OntologyCompiler
            schema = await OntologyCompiler().get_compiled_schema(doc.tenant_id, kb_id, auto_compile=True)
            if schema is None:
                logger.warning(
                    "Ontology retry skipped - no compiled schema for doc %s (KB %s)",
                    doc.id, kb_id,
                )
                return False
            schema_version = int(schema.get("schema_version", 0) or 0)
        except Exception as e:
            logger.warning("Compiled schema check failed for doc %s, proceeding anyway: %s", doc.id, e)
            if schema is None:
                return False

        # [jonex] R1：retry_count 计的是「已提交的重抽次数」。上限闸门原先放在函数入口，
        # 会把仍处于 EXTRACTING 的文档直接判死 —— 既包括在途任务的轮询收尾，也包括 R1
        # 「写图失败保持 EXTRACTING 待重写」的路径（该文档 retry_count 可能已 ≥ 上限，
        # 于是设置完 EXTRACTING 的下一轮就被判 FAILED，R1 形同虚设）。闸门据此下移到
        # 提交点之前（见下方 "retry limit" 检查），轮询与写图重写不受它约束。
        retry_count = doc.ontology_retry_count or 0

        # 仅当文档已处于 EXTRACTING 时，才轮询既有任务并据其结果收尾。
        # EXTRACTING 有且仅有两种来源，两者都「已有任务在手」，都可安全收尾：
        #   ① 本对账循环此前发起过 ontology-only 重抽，彼时已把 rag_task_id 改写为
        #      该重抽任务（见本函数末尾的提交段）；
        #   ② [jonex] R1：首次入库任务已完成，但 Neo4j 写图失败 → _handle_completed
        #      保持 EXTRACTING 未落 FAILED，rag_task_id 仍指向「入库任务」。此时从
        #      入库任务取回 ontology_data 重写图是**期望行为**（该产出含表格对象路径，
        #      口径与首次一致）——不要因为「rag_task_id 是入库任务」而跳过它，否则又
        #      回到写图失败即重抽的老路。结果陈旧的风险由入口代次 fencing（P0-I）
        #      与 schema 版本 fencing（P1-E）兜住。
        #
        # 对 PENDING / FAILED 文档：rag_task_id 可能仍是最初的“入库任务”，其 Redis 缓存里
        # 带着旧的 ontology_data。若用它收尾，会把旧的（无描述 / 旧 schema）结果回写 Neo4j，
        # 且 retry_count 不增、永不触发新抽取（历史 bug）。故 PENDING/FAILED 一律跳过收尾，
        # 直接走下方“触发全新重抽”，不再依赖运维手动清空 rag_task_id。
        if doc.ontology_status == OntologyStatus.EXTRACTING.value and doc.rag_task_id:
            try:
                # [jonex] P0-d：统一取状态入口（超大 JSON 直读磁盘 / HTTP 失败回退磁盘）。
                # 取不到才放弃本轮，保持原有「查不到状态就跳过」的保守语义；不兜底的话，
                # 在途重抽即便已经跑完也永远收不了尾，只能等 patrol 误判超时。
                #
                # ⚠ `force_backend` 传的是 **compile 步自己的粘性值**
                # （`ontology_compile_backend`），不是解析链的 `ingest_backend`。
                # 这一支问的 `rag_task_id` 是**本体任务**，而本体任务在哪由 compile 步
                # 当时派给了谁决定：`rag`（atomic-rag，现状）/ `ontology_compile`（P2 新
                # 服务）。**不能按解析链的粘性值派** —— 按它派会去问错后端 ⇒ 恒 404
                # ⇒ `not_found` 一个分支都不命中 ⇒ 真因读不回来、只剩空转重试
                # （2026-10-09 事故）。
                # 老文档没有这个键 ⇒ 回落 `rag` ⇒ 与改动前**逐字等价**（见
                # `compile_backend.backend_for_document`）。
                # ⚠ 这个值必须与 `_fetch_task_status` 的分支**成对**改：那函数认不出
                # `ontology_compile` 就会静默落到 atomic-rag 那三条路径 —— 比原先硬编码
                # `"rag"` 更隐蔽（看起来改对了、实际没生效）。
                status_info = await _fetch_task_status(
                    doc.tenant_id,
                    (doc.knowledge_base_id
                     or (doc.extra_metadata or {}).get("knowledge_base_id", "")),
                    doc.rag_task_id, doc_id=doc.id, doc=doc,
                    force_backend=compile_backend.backend_for_document(doc),
                )
                if status_info is None:
                    # 取不到状态（下游后端连不上 / atomic-rag 的任务 JSON 读不出来）→
                    # 本轮跳过。
                    # ⚠ 这里**必须留声**：这一支原先静默返回，于是「按解析链的粘性值去问
                    # 了错的服」这件事和真因一起被吞掉，日志上什么也看不出来，只有重试
                    # 次数在涨（2026-10-09 事故的排查代价就花在这）。
                    # 文案里点明问的是哪个后端（P2 起是**compile 步的粘性值**，可能是
                    # atomic-rag，也可能是新服务），假设一旦不成立能一眼看见。
                    logger.warning(
                        "本体任务状态取不到（问的是 %s，未回退解析链后端）: "
                        "doc_id=%s task=%s",
                        compile_backend.backend_for_document(doc),
                        doc.id, doc.rag_task_id,
                    )
                    return False
                rag_status = status_info.get("status") or status_info.get("state", "")
                if rag_status in ("processing", "pending"):
                    logger.debug("Ontology retry task still running: doc_id=%s, task_id=%s", doc.id, doc.rag_task_id)
                    return False
                if rag_status == "completed":
                    ont_status = status_info.get("ontology_status")
                    if ont_status == "completed" and status_info.get("ontology_data"):
                        # 在途重抽任务已完成 — 收尾写入 Neo4j + 置 READY
                        await self._handle_completed(doc, status_info, reconcile_source="ontology_retry")
                        return True
                    if _is_empty_ontology_output(status_info):
                        # [jonex] P0-4：在途重抽确认「确无本体内容」= 合法终态。
                        # 不落 FAILED、不再触发下一轮重抽（重抽不会改变结果）。
                        async with get_db_session() as session:
                            repo = KnowledgeDocumentRepository(session)
                            await repo.set_ontology_status(doc, OntologyStatus.READY)
                            await session.commit()
                        logger.info(
                            "Ontology empty output, marked READY (no retry): doc_id=%s",
                            doc.id,
                        )
                        return False
                    # 已完成但无 ontology_data（例如无候选实体）
                    ont_error = status_info.get("ontology_error") or status_info.get("error") or "无候选实体"
                    if retry_count + 1 >= MAX_ONTOLOGY_RETRIES:
                        async with get_db_session() as session:
                            repo = KnowledgeDocumentRepository(session)
                            await repo.set_ontology_status(doc, OntologyStatus.FAILED, error=ont_error)
                            await session.commit()
                        logger.warning("Ontology retry exhausted for doc %s: %s", doc.id, ont_error)
                        return False
                    # 落空（还有额度）→ 跳出整条 if/elif，继续往下触发新一轮重抽
                elif rag_status in _ONTOLOGY_TASK_FAILED_STATUSES:
                    # 下游任务**明确失败** → 把真因落到文档行上。
                    #
                    # 两类文档的终态不同：
                    #
                    # · **归任务中心管**：落 `FAILED`。compile 步的看门狗
                    #   （`task_watch_service._inspect_compile`）只看
                    #   `ontology_status` 的 READY / FAILED —— 保持 EXTRACTING
                    #   会让界面「编译中」一直挂到 `TC_STEP_HARD_TIMEOUT_SEC`
                    #   （默认 6 小时），而真正失败的任务 30 秒前就结束了
                    #   （2026-10-19 事故）。旧对账本来也不能替它重抽（下面那道
                    #   归属闸门），落 FAILED 不会引出任何自动重试；用户点
                    #   「重新编译」时 `retry_extract` 会重新置回 EXTRACTING。
                    # · **不归任务中心管**（旧链路）：**状态不动**（仍是
                    #   EXTRACTING：这一轮还没收口，重抽与否交给下面的闸门）。
                    #
                    # 落库走 `set_ontology_status`：状态没变 ⇒ `record_processing_task`
                    # 那条埋点不会发（它按 `prev_status != new_status` 判）。
                    #
                    # 原来这一支是**静默**往下走的：下游任务（atomic-rag 或 P2 新服务）
                    # 里明明带着「为什么失败」，文档行却一个字都不写，最后只剩闸门那句
                    # 计数文案 —— 与 2026-10-09 那次同一个病（真因只活在下游任务里）。
                    detail = _ontology_task_failure_detail(status_info, rag_status)
                    managed = await self._takeover_is_task_center_managed(doc)
                    async with get_db_session() as session:
                        repo = KnowledgeDocumentRepository(session)
                        await repo.set_ontology_status(
                            doc,
                            OntologyStatus.FAILED if managed else OntologyStatus.EXTRACTING,
                            error=detail,
                        )
                        await session.commit()
                    logger.warning(
                        "本体任务失败，真因已落文档行（%s）: doc_id=%s task=%s "
                        "status=%s detail=%s",
                        "任务中心托管 → FAILED" if managed else "旧链路 → 保持 EXTRACTING",
                        doc.id, doc.rag_task_id, rag_status, detail,
                    )
                    if managed:
                        return False
                elif rag_status == "not_found":
                    # 下游那一侧找不到这条任务。**不判死**（既有查证链就是为
                    # 「任务行被清理了但产物还在」准备的），但必须留声：原先它与
                    # `failed` 一样静默，排查时看不出「问到了、但对方说没有」。
                    logger.warning(
                        "本体任务在 %s 里不存在（保持 EXTRACTING 等下一轮）: "
                        "doc_id=%s task=%s",
                        compile_backend.backend_for_document(doc),
                        doc.id, doc.rag_task_id,
                    )
                else:
                    # 没见过的状态词：不认识就别当已知的处理，留声后交给硬超时。
                    logger.warning(
                        "本体任务状态词不认识（保持 EXTRACTING 等下一轮）: "
                        "doc_id=%s task=%s status=%r",
                        doc.id, doc.rag_task_id, rag_status,
                    )
            except Exception as e:
                logger.warning("Ontology retry handling failed for doc %s, skipping: %s", doc.id, e)
                return False

        # retry limit（R1：闸门从函数入口下移到提交点 —— 只拦「新提交」，不拦轮询/写图重写）
        if retry_count >= MAX_ONTOLOGY_RETRIES:
            logger.warning(
                "Ontology retry limit reached (%d/%d) for doc %s, marking failed",
                retry_count, MAX_ONTOLOGY_RETRIES, doc.id,
            )
            # 文案带上行上已有的真因（见 `_ontology_retry_limit_message`）：闸门每 30 秒
            # 重跑一次，原先只写计数会把真因永久顶掉。
            limit_error = _ontology_retry_limit_message(retry_count, doc.ontology_error)
            async with get_db_session() as session:
                repo = KnowledgeDocumentRepository(session)
                await repo.set_ontology_status(
                    doc, OntologyStatus.FAILED, error=limit_error,
                )
                await session.commit()
            return False

        # ── ⚠️ 最后一道闸：归任务中心管的文档，**新提交**一步都不许（2026-10-14 加）──
        #
        # 位置是刻意的：排在**上面所有轮询 / 写图 / 判死收尾段之后**，只拦「重新发起抽取」
        # 这一个动作。上面那些是收尾（EXTRACTING 轮询下游任务（atomic-rag / P2 新服务，
        # 按 compile 步粘性值）、写 Neo4j、把终态落到文档行），照做；到了这一行才开始
        # "替文档发起一个新任务"，那就越权了。
        #
        # 为什么 `reconcile_ontology` 的领取查询拦不住、这里还要再拦一次：领取那层的
        # `task_center_kept_statuses=[EXTRACTING]` 是**收尾例外**，被接管的 EXTRACTING
        # 文档**故意**放进了扫描面。而本函数在「下游任务明确失败」那支（`rag_status in
        # _ONTOLOGY_TASK_FAILED_STATUSES`）**只落真因、不 return**，会一路穿透到这里 ——
        # 于是被接管的文档仍会被旧链重抽一次。两层判据缺一不可：领取层管"进不进扫描面"，
        # 这一层管"要不要真发"。
        #
        # 拦下来之后这类文档停在 `EXTRACTING`（文档本身 `READY`、可检索，缺的只是图），
        # 等用户点「重新编译」——那条路走任务中心、授权链完整。这是口径，不是退化。
        if await self._takeover_is_task_center_managed(doc):
            logger.info(
                "本体抽取新提交被拦（文档归任务中心管，发起权不在旧对账）: doc_id=%s "
                "ontology_status=%s", doc.id, doc.ontology_status,
            )
            return False

        # [jonex] P1-F 领取协议：CAS 领取（当前状态 → EXTRACTING 占位）→ 提交 atomic-rag →
        # 拿到 task_id 后写 rag_task_id + ++retry；提交失败回退 PENDING、不 ++retry。
        prev_status = doc.ontology_status
        async with get_db_session() as session:
            repo = KnowledgeDocumentRepository(session)
            claimed = await repo.claim_ontology_for_retry(doc)
            await session.commit()
        if not claimed:
            # 其它实例已领取该文档 → 跳过（多实例只领一次）
            logger.debug("Ontology retry skipped (claimed by another instance): doc_id=%s", doc.id)
            return False

        # Trigger new ontology retry (ontology-only, 携带 schema + 版本 + 代次)

        try:
            result = await get_rag_client().retry_ontology_extract(
                document_id=doc.id,
                knowledge_base_id=kb_id,
                tenant_id=doc.tenant_id,
                file_path=doc.file_path,
                ontology_schema=schema,
                schema_version=schema_version,
                content_generation=int(getattr(doc, "content_generation", 0) or 0),
                # [jonex] A18（N5）：无条件 force_retry —— 对账重抽 = 明确要求重跑；
                # 不传会被同一幂等键下的 completed 任务早退复用（空转告终根因）。
                force_retry=True,
            )
        except Exception as e:
            logger.warning("Retry ontology extract failed for doc %s: %s", doc.id, e)
            async with get_db_session() as session:
                repo = KnowledgeDocumentRepository(session)
                await repo.revert_ontology_status(doc, prev_status)
                await session.commit()
            return False

        retry_task_id = (result or {}).get("task_id", "")
        if not retry_task_id:
            logger.warning("Retry ontology did not return task_id for doc %s", doc.id)
            async with get_db_session() as session:
                repo = KnowledgeDocumentRepository(session)
                await repo.revert_ontology_status(doc, prev_status)
                await session.commit()
            return False

        # 提交成功：保持 EXTRACTING（已由 claim 置位），写 rag_task_id + ++retry
        # `error=""`：**新一轮开跑，上一轮的原因作废**。不清的话它会一直留在行上，
        # 被重试上限闸门读去当成本轮结论 —— 而运维重置的惯例（`ontology_status='pending'
        # + ontology_retry_count=0`）本来就**不清这一列**，所以这个字段的陈旧是常态、
        # 不是异常。`set_ontology_status` 用 `error is not None` 判断要不要写，空串正是
        # 「显式清空」的那个取值（`None` 是「别动」）。
        async with get_db_session() as session:
            repo = KnowledgeDocumentRepository(session)
            await repo.set_ontology_status(
                doc, OntologyStatus.EXTRACTING, increment_retry=True, error="",
            )
            fresh = await repo.set_status(doc, doc.status, rag_task_id=retry_task_id)
            await session.commit()

        logger.info("Ontology retry queued: doc_id=%s, new_task_id=%s", doc.id, retry_task_id)
        return True

    # ── storage fallback ───────────────────────────────────────

    async def _verify_via_storage(self, doc) -> list[str] | None:
        """Confirm document existence via LightRAG storage reader.

        Used when atomic-rag task state is lost (Redis expired / container restarted)
        but the document may have been successfully ingested into LightRAG.

        Matching strategy: extract doc=<id> from LightRAG's file_path field and
        compare with doc.id.  This is more reliable than file_name matching
        because LightRAG prepends a UUID prefix and may change underscores
        (e.g. "年报" → "___").

        分页与匹配统一在 `rag_storage_lookup` 内实现——此前本地副本把 page_size
        写成 500（LightRAG 约束 [10, 200]）、且读不存在的 `items` 键，两处都让
        兜底恒失败，对账拿不到 doc_ids 也就无法把文档恢复成 READY。

        Returns:
            rag_doc_ids list if confirmed, None otherwise.
        """
        kb_id = (doc.knowledge_base_id or (doc.extra_metadata or {}).get("knowledge_base_id", ""))

        doc_ids = await find_rag_doc_ids(
            get_rag_client,
            tenant_id=doc.tenant_id,
            knowledge_base_id=kb_id,
            file_name=doc.file_name,
            document_id=doc.id,
        )
        # 契约：本方法返回 None 表示「未能确认」（调用失败或未命中），调用方
        # 据此跳过状态改写；返回非空列表才表示确认存在。
        return doc_ids or None

    # ── [jonex] OpenKB compile dispatch ──

    async def _dispatch_openkb_compile(self, doc, status_info: dict):
        """解析完成后调用 OpenKB compile_parsed_document。

        parsed markdown/assets 由解析任务在共享卷 inputs 上产出，经 task status 透传
        （parsed_markdown_path / assets_dir，绝对路径）。这里换算为「相对 inputs 卷根」
        的路径交给 OpenKB，做到跨容器挂载点无关。

        重要：拿不到 parsed markdown 时如实标记失败，绝不把原始二进制文件（PDF/docx）
        当 markdown 送入编译；编译结果（compiled/failed）写回文档，避免静默成功/失败。
        """
        from .openkb_service import KnowledgeCompilerService

        kb_id = (doc.knowledge_base_id or (doc.extra_metadata or {}).get("knowledge_base_id", ""))

        # 解析任务透传的产物路径（绝对路径，位于共享卷 inputs 挂载点内）
        abs_md = status_info.get("parsed_markdown_path") or ""
        abs_assets = status_info.get("assets_dir") or ""

        rel_md = self._relativize_input_path(abs_md)
        rel_assets = self._relativize_input_path(abs_assets)

        if not rel_md:
            # 没有可用 parsed markdown：如实标记失败，等待解析器把 markdown 落到共享卷并暴露路径
            await self._record_openkb_status(
                doc, status="failed",
                error="OPENKB_NO_PARSED_MARKDOWN: 解析任务未提供 parsed_markdown_path"
                      "（需解析器把 markdown 落到共享卷 inputs 并经 task status 暴露）",
            )
            logger.error(
                "[jonex] OpenKB compile 跳过：doc=%s 无 parsed_markdown_path（task 未暴露产物路径）",
                doc.id,
            )
            return

        # [jonex] 任务化：claim（列）→ 提交编译任务（容器内后台执行）→ task_id 写列
        # → 立即返回（不等待编译）。终态（compiled/failed）由 patrol_openkb_compile
        # 轮询任务状态回写，sidecar 同步超时不再能打断编译。
        compiler = KnowledgeCompilerService()
        # [jonex] LLM-Wiki Schema fencing（方案 §6）：读 active 版本 → claim 绑定
        # target → task 文件带同一版本（patrol 回写 applied 闭环）
        target_ver = await compiler.get_active_schema_version(doc.tenant_id, kb_id)
        if not await compiler.claim_compile(doc.tenant_id, doc.id,
                                            target_schema_version=target_ver):
            logger.info("[jonex] OpenKB compile claim 未抢到（已 compiling 或并发双投）doc=%s", doc.id)
            return
        try:
            _artifact = {
                "document_id": doc.id,
                "source_file_path": doc.file_path or "",
                "parsed_markdown_path": rel_md,       # 相对 inputs 卷根
                "assets_dir": rel_assets,             # 相对 inputs 卷根（可空）
                "parser": status_info.get("parser", "mineru"),
                "metadata": {
                    "pages": status_info.get("pages", 0),
                    "title": doc.file_name or "",
                    "rag_task_id": doc.rag_task_id,
                },
            }
            if target_ver is not None:
                _artifact["wiki_schema_version"] = target_ver
            result = await compiler.submit_compile(
                kb_name=kb_id,
                tenant_id=doc.tenant_id,
                kb_id=kb_id,
                parsed_artifact=_artifact,
            )
            await compiler.set_task_id(doc.tenant_id, doc.id, result["task_id"])
            logger.info(
                "[jonex] OpenKB compile 已提交（任务化）: kb=%s doc=%s task=%s",
                kb_id, doc.id, result.get("task_id"),
            )
        except Exception as exc:
            # 投递失败必须回滚 claim，否则永久卡 compiling
            await self._record_openkb_status(doc, status="failed", error=str(exc)[:1000])
            logger.exception(
                "[jonex] OpenKB compile 提交失败: kb=%s doc=%s error=%s", kb_id, doc.id, exc,
            )

    async def _dispatch_jonex_wiki_compile(self, doc, status_info: dict):
        """解析完成后检查产物并标记等待；由巡检整库合并投递。

        与 _dispatch_openkb_compile 镜像，但 artifact 是 jonex_wiki 的 ingest 形状
        ``{source_path, source_name, document_id}``（引擎 POST /ingest 只认这三个字段，
        不收 openkb 的 {parsed_markdown_path, assets_dir, parser, metadata}，也不收
        wiki_schema_version —— schema fencing 只落在 claim_compile 的列绑定上）。
        """
        from .jonex_wiki_service import stage_jonex_wiki_ingest_source

        kb_id = (doc.knowledge_base_id or (doc.extra_metadata or {}).get("knowledge_base_id", ""))

        suffix = os.path.splitext(doc.file_name or "")[1].lower()
        if suffix in {".xlsx", ".xlsm"}:
            # 原生表格:转发原始二进制到共享卷 parsed/{doc}/source{ext},
            # 让引擎走 SPREADSHEET_NATIVE 确定性解析(工作簿级阅读材料)。
            try:
                source_path = await stage_jonex_wiki_ingest_source(
                    doc.id, file_name=doc.file_name or "",
                    storage_key=doc.storage_key or "",
                    storage_backend=doc.storage_backend or "",
                    file_path=doc.file_path or "",
                )
            except Exception as exc:
                await self._record_openkb_status(
                    doc, status="failed",
                    error=f"JONEX_WIKI_NO_ORIGINAL_SOURCE: {exc}"[:1000],
                )
                logger.error(
                    "[jonex] jonex_wiki compile 跳过：doc=%s 原始表格不可读", doc.id,
                    exc_info=True,
                )
                return
        else:
            abs_md = status_info.get("parsed_markdown_path") or ""
            source_path = self._relativize_input_path(abs_md)

            if not source_path:
                # 没有可用 parsed markdown：如实标记失败（与 openkb 同口径）
                await self._record_openkb_status(
                    doc, status="failed",
                    error="JONEX_WIKI_NO_PARSED_MARKDOWN: 解析任务未提供 parsed_markdown_path"
                          "（需解析器把 markdown 落到共享卷 inputs 并经 task status 暴露）",
                )
                logger.error(
                    "[jonex] jonex_wiki compile 跳过：doc=%s 无 parsed_markdown_path", doc.id,
                )
                return

        # Parsing success is not a KB-wide batch-complete signal. Keep this
        # source durable and waiting; the patrol drains all ready sources once
        # the platform has no pending parses and the upload window is quiet.
        await self._record_openkb_status(doc, status="stale", error="")

    @staticmethod
    def _relativize_input_path(path: str) -> str:
        """把共享卷 inputs 上的绝对路径换算为「相对卷根」的路径（跨容器挂载点无关）。

        knowledge_base/atomic-rag 的 inputs 挂载点由 KB_INPUT_DIR 指定（默认 /app/inputs），
        openkb 挂在 OPENKB_INPUT_ROOT（默认 /app/data/inputs）。传相对路径让两端各自解析。
        空值返回空串；已是相对路径原样返回。
        """
        if not path:
            return ""
        root = os.getenv("KB_INPUT_DIR", "/app/inputs").rstrip("/")
        p = str(path)
        if p.startswith(root + "/"):
            return p[len(root) + 1:]
        return p if not p.startswith("/") else p.lstrip("/")

    async def _record_openkb_status(self, doc, *, status: str, error: str = "", warnings=None):
        """把 LLM-Wiki 编译状态写回文档（列化），避免静默成功/失败。

        [jonex] 统一语义（与 openkb_service 版一致）：
        - compiling/compiled/stale → 清除 llm_wiki_compile_error；
        - compiling/stale → 清除 llm_wiki_compile_warnings；
        - failed → 写 error（截断 1000），**保留 warnings 不动**；
        - compiled → 写 warnings（或 None）+ compiled_at（UTC）。
        """
        try:
            async with get_db_session() as session:
                repo = KnowledgeDocumentRepository(session)
                fresh = await repo.get_required(doc.id, doc.tenant_id)
                fresh.llm_wiki_compile_status = status
                if status in ("compiling", "compiled", "stale"):
                    fresh.llm_wiki_compile_error = None
                if status in ("compiling", "stale"):
                    fresh.llm_wiki_compile_warnings = None
                if status == "failed" and error:
                    fresh.llm_wiki_compile_error = error[:1000]
                if warnings is not None:
                    fresh.llm_wiki_compile_warnings = warnings or None
                if status == "compiled":
                    fresh.llm_wiki_compiled_at = datetime.utcnow()
                session.add(fresh)
                # [jonex] 数据运营二期：知识编译任务埋点（compile 终态）。
                # 与状态回写同事务（commit 之前）；`stale` 由薄封装挡下不落事件。
                await record_compile_task(
                    session, doc.tenant_id, document_id=doc.id,
                    compile_status=status, kb_id=fresh.knowledge_base_id,
                    content_generation=fresh.content_generation, error=error or None,
                )
                await session.commit()
                # 保持内存态一致（doc.extra_metadata 仍兼容旧 JSONB 读点）
                meta = dict(fresh.extra_metadata or {})
                meta["openkb_compile_status"] = status
                doc.extra_metadata = meta
        except Exception:
            logger.warning("记录 LLM-Wiki 编译状态失败 doc=%s", doc.id, exc_info=True)

    async def patrol_openkb_compile(self, limit: int = 50) -> dict:
        """[jonex] 轮询 OpenKB 编译任务状态并回写 PG；stale 文档补提交；超时判死。

        任务化后编译终态只由这里回写（30s 巡检）：
        - completed → llm_wiki_compile_status='compiled'（含 warnings / compiled_at）
        - failed → 'failed'（error 取任务）
        - running → 按任务 started_at 起算超时（排队时间不算）
        - pending → 还在 mutation 锁队列排队，不判死（批量上传队尾任务）
        - task 未知（容器重启清理）→ 判死 failed（error='task lost'）
        - task_id IS NULL → 只按 requested_at 超时（claim 后 submit 未回写的窗口）
        env 兼容：OPENKB_COMPILE_TIMEOUT_SEC 优先，回退 OPENKB_RECOMPILE_TIMEOUT_SEC。
        """
        TIMEOUT = int(os.getenv("OPENKB_COMPILE_TIMEOUT_SEC",
                     os.getenv("OPENKB_RECOMPILE_TIMEOUT_SEC", "1800")))
        from sqlalchemy import text

        # ① compiling 的 openkb 文档（索引查询；带 knowledge_base_id 供分组 invoke；
        #    kb_type 过滤防把 jonex_wiki 的 compiling 文档误拿到 OpenKB 轮询）
        sql = text("""
            SELECT d.id, d.tenant_id, d.knowledge_base_id, d.llm_wiki_task_id,
                   d.llm_wiki_compile_requested_at
              FROM knowledge_base.knowledge_documents d
              JOIN knowledge_base.knowledge_info ki
                ON ki.id = d.knowledge_base_id
               AND ki.tenant_id = d.tenant_id
               AND ki.is_deleted = 0
             WHERE d.is_deleted = 0
               AND d.llm_wiki_compile_status = 'compiling'
               AND ki.kb_type = 'openkb'
             LIMIT :limit
        """)
        async with get_db_session() as session:
            rows = (await session.execute(sql, {"limit": limit})).all()

        now = datetime.utcnow()
        compiled = failed = timed_out = 0

        # ② 按 (tenant_id, kb_id) 分组，一组一次 list_compile_tasks
        by_kb: dict[tuple[str, str], list] = {}
        for r in rows:
            by_kb.setdefault((r.tenant_id, r.knowledge_base_id or ""), []).append(r)

        from .openkb_service import KnowledgeCompilerService
        compiler = KnowledgeCompilerService()
        for (tenant_id, kb_id), group in by_kb.items():
            task_map: dict[str, dict] = {}
            try:
                res = await compiler.list_compile_tasks(
                    kb_name=kb_id, tenant_id=tenant_id, kb_id=kb_id)
                for t in (res or {}).get("tasks", []):
                    task_map[t.get("task_id")] = t
            except Exception as exc:
                # 容器不可达：保持现状不判死（对账降级，不误杀）
                logger.warning("patrol_openkb_compile list_compile_tasks 失败 kb=%s: %s", kb_id, exc)
                continue

            for r in group:
                doc_id, task_id = r.id, r.llm_wiki_task_id
                req_at = r.llm_wiki_compile_requested_at
                if not task_id:
                    # claim 后 submit 尚未回写 task_id 的正常窗口：只按 requested_at 超时
                    if req_at and (now - req_at).total_seconds() > TIMEOUT:
                        await self._record_openkb_status_by_id(
                            doc_id, tenant_id, status="failed",
                            error="OPENKB_COMPILE_TIMEOUT: 编译超时（submit 未回写 task_id），请重试",
                        )
                        timed_out += 1
                    continue
                task = task_map.get(task_id)
                if task is None:
                    # 任务丢失（容器重启 initialize 判死）
                    await self._record_openkb_status_by_id(
                        doc_id, tenant_id, status="failed",
                        error="OPENKB_TASK_LOST: 编译任务丢失（容器重启），请重新编译",
                    )
                    failed += 1
                    continue
                tstatus = task.get("status")
                if tstatus == "completed":
                    await self._record_openkb_status_by_id(
                        doc_id, tenant_id, status="compiled",
                        warnings=task.get("warnings") or [],
                        # [jonex] §8.1：task 文件带出的版本 → applied 回写（fencing 闭环）
                        applied_schema_version=task.get("wiki_schema_version"),
                    )
                    compiled += 1
                elif tstatus == "failed":
                    await self._record_openkb_status_by_id(
                        doc_id, tenant_id, status="failed",
                        error=task.get("error") or "compile failed",
                    )
                    failed += 1
                elif tstatus == "running":
                    # ⚠️ 排队时间不算超时：running 才从 started_at 起算
                    started = task.get("started_at")
                    try:
                        started_dt = datetime.fromisoformat(str(started).replace("Z", "+00:00"))
                        if started_dt.tzinfo:
                            started_dt = started_dt.replace(tzinfo=None)
                        elapsed = (now - started_dt).total_seconds()
                    except (ValueError, TypeError):
                        elapsed = 0   # started_at 缺失不判死（保守）
                    if elapsed > TIMEOUT:
                        await self._record_openkb_status_by_id(
                            doc_id, tenant_id, status="failed",
                            error="OPENKB_COMPILE_TIMEOUT: 编译超时，请重试",
                        )
                        timed_out += 1
                # pending：排队中不判死（批量上传队尾任务，mutation 锁队列）

        # ③ stale 且 ready 的文档补提交（reparse 停在 stale 的兜底）
        stale_submitted = await self._patrol_stale_recompile(limit=limit)

        return {"checked": len(rows), "compiled": compiled, "failed": failed,
                "timed_out": timed_out, "stale_submitted": stale_submitted}

    async def patrol_jonex_wiki_compile(self, limit: int = 50) -> dict:
        """[jonex] 轮询 jonex_wiki 编译任务状态并回写 PG；超时判死（Phase 5）。

        状态机与 patrol_openkb_compile 一致（completed→compiled / failed→failed /
        running 按 started_at 超时判死 / task 丢失判死 / task_id 缺失只按 requested_at
        超时），仅替换 client 为 JonexWikiService + kb_type 过滤为 jonex_wiki。
        """
        TIMEOUT = int(os.getenv("JONEX_WIKI_COMPILE_TIMEOUT_SEC",
                     os.getenv("OPENKB_COMPILE_TIMEOUT_SEC",
                     os.getenv("OPENKB_RECOMPILE_TIMEOUT_SEC", "1800"))))
        from sqlalchemy import text

        # ① compiling 的 jonex_wiki 文档（索引查询；带 knowledge_base_id 供分组 invoke）
        sql = text("""
            SELECT d.id, d.tenant_id, d.knowledge_base_id, d.llm_wiki_task_id,
                   d.llm_wiki_compile_requested_at
              FROM knowledge_base.knowledge_documents d
              JOIN knowledge_base.knowledge_info ki
                ON ki.id = d.knowledge_base_id
               AND ki.tenant_id = d.tenant_id
               AND ki.is_deleted = 0
             WHERE d.is_deleted = 0
               AND d.llm_wiki_compile_status = 'compiling'
               AND ki.kb_type = 'jonex_wiki'
               AND d.id > :after_id
             ORDER BY d.id
             LIMIT :limit
        """)
        async with get_db_session() as session:
            rows = (await session.execute(sql, {
                "limit": limit, "after_id": getattr(self, "_jonex_compile_cursor", ""),
            })).all()
        # 排队/暂停任务可能长期不退出 compiling；轮转扫描避免它们一直占满窗口。
        self._jonex_compile_cursor = rows[-1].id if len(rows) == limit else ""

        now = datetime.utcnow()
        compiled = failed = timed_out = 0

        # ② 按 (tenant_id, kb_id) 分组，一组一次 list_compile_tasks
        by_kb: dict[tuple[str, str], list] = {}
        for r in rows:
            by_kb.setdefault((r.tenant_id, r.knowledge_base_id or ""), []).append(r)

        from .jonex_wiki_service import JonexWikiService
        compiler = JonexWikiService()
        for (tenant_id, kb_id), group in by_kb.items():
            task_map: dict[str, dict] = {}
            try:
                res = await compiler.list_compile_tasks(
                    kb_name=kb_id, tenant_id=tenant_id, kb_id=kb_id)
                for t in (res or {}).get("tasks", []):
                    task_map[t.get("task_id")] = t
            except Exception as exc:
                # 容器不可达：保持现状不判死（对账降级，不误杀）
                logger.warning("patrol_jonex_wiki_compile list_compile_tasks 失败 kb=%s: %s", kb_id, exc)
                continue

            for r in group:
                doc_id, task_id = r.id, r.llm_wiki_task_id
                req_at = r.llm_wiki_compile_requested_at
                if not task_id:
                    # claim 后 submit 尚未回写 task_id 的正常窗口：只按 requested_at 超时
                    if req_at and (now - req_at).total_seconds() > TIMEOUT:
                        await self._record_openkb_status_by_id(
                            doc_id, tenant_id, status="failed",
                            error="JONEX_WIKI_COMPILE_TIMEOUT: 编译超时（submit 未回写 task_id），请重试",
                        )
                        timed_out += 1
                    continue
                task = task_map.get(task_id)
                if task is None:
                    # list_compile_tasks 有分页上限；列表未命中不等于任务丢失。
                    try:
                        task = await compiler.get_compile_status(
                            kb_name=kb_id, tenant_id=tenant_id, kb_id=kb_id, task_id=task_id)
                    except ResourceNotFoundError:
                        pass
                    except Exception:
                        logger.warning("查询 jonex_wiki 编译任务失败 task=%s", task_id, exc_info=True)
                        continue
                if task is None:
                    # 单任务查询也确认任务丢失后才判死
                    await self._record_openkb_status_by_id(
                        doc_id, tenant_id, status="failed",
                        error="JONEX_WIKI_TASK_LOST: 编译任务丢失（容器重启），请重新编译",
                    )
                    failed += 1
                    continue
                tstatus = task.get("status")
                if tstatus == "completed":
                    await self._record_openkb_status_by_id(
                        doc_id, tenant_id, status="compiled",
                        # [jonex] 引擎的 compile task 文件不带 warnings 字段 → 这里恒为
                        # 空。传 [] 会被 _record_openkb_status_by_id 判成"清空"
                        # （compiled 分支 `warnings or None`），把派发阶段
                        # _dispatch_jonex_wiki_compile 写下的体检/隔离结论在编译完成的
                        # 瞬间抹掉（实测：compiling 时列里有值，compiled 后变 NULL）。
                        # 传 None = 「本次无新结论」→ 保留派发时写入的值；将来引擎若把
                        # warnings 带进 task 文件，这里会自动改用任务侧结论。
                        warnings=(task.get("warnings") or None),
                        # [jonex] §8.1：task 文件带出的版本 → applied 回写（fencing 闭环）
                        applied_schema_version=task.get("wiki_schema_version"),
                    )
                    compiled += 1
                elif tstatus == "failed":
                    await self._record_openkb_status_by_id(
                        doc_id, tenant_id, status="failed",
                        error=task.get("error") or "compile failed",
                    )
                    failed += 1
                elif tstatus == "running":
                    # ⚠️ 排队时间不算超时：running 才从 started_at 起算
                    started = task.get("started_at")
                    try:
                        started_dt = datetime.fromisoformat(str(started).replace("Z", "+00:00"))
                        if started_dt.tzinfo:
                            started_dt = started_dt.replace(tzinfo=None)
                        elapsed = (now - started_dt).total_seconds()
                    except (ValueError, TypeError):
                        elapsed = 0   # started_at 缺失不判死（保守）
                    if elapsed > TIMEOUT:
                        await self._record_openkb_status_by_id(
                            doc_id, tenant_id, status="failed",
                            error="JONEX_WIKI_COMPILE_TIMEOUT: 编译超时，请重试",
                        )
                        timed_out += 1
                # pending：排队中不判死（批量上传队尾任务，mutation 锁队列）

        # [jonex] ④ jonex_wiki 的 stale 补投：reparse 停在 stale、或编译撞「同 KB 已有
        # running compile」被回退成 stale 的文档都在这里自愈（与 openkb 同形但 artifact 不同）
        stale_submitted = await self._patrol_stale_recompile(
            limit=limit, kb_type="jonex_wiki")

        return {"checked": len(rows), "compiled": compiled, "failed": failed,
                "timed_out": timed_out, "stale_submitted": stale_submitted}

    async def _patrol_stale_recompile(self, limit: int = 50, kb_type: str = "openkb") -> int:
        """[jonex] ready 的 stale 文档补投；Jonex Wiki 还包含从未编译的 NULL。

        reparse 常规路径会推回 parsing→ready 再触发钩子，但 stale 若由其他路径
        写入、或 reparse 中途失败停在 ready，文档会永久停在 stale——此处兜底。
        [jonex] 追加「编译撞 KB 忙被回退为 stale」的场景（见 _dispatch_jonex_wiki_compile）：
        openkb 与 jonex_wiki 的 artifact 形状不同，按 kb_type 分派，各自巡检各自重投。

        OpenKB keeps task-center ownership exclusive. Jonex Wiki compile steps instead
        persist an execution-fenced request and share this KB-wide batch dispatcher;
        unfinished parses and compile handlers that have not queued yet still block it.
        Finished parse steps can join before compile concurrency allows their dispatch.
        Task-center parsing bypasses the legacy stale hook, so its initial NULL
        compile state must join both candidate selection and the full-KB drain.
        """
        from sqlalchemy import text

        # ── ⚠️ 两条接管行判据用的是**两个不同的状态集合**，别顺手统一（2026-10-14）──
        #
        #   · `owner.state = ANY(:active_states)` —— 问「**这个 KB 现在忙不忙**」。
        #     必须留在 `ACTIVE_STATES`：它是**整库**的闸门，把 `released` 也算进来的话，
        #     一份卡住的交还文档就能把这**整个 KB** 的批量编译永久堵死 —— 那些与它
        #     毫不相干的文档会一起饿死。
        #   · `tcd.state = ANY(:managed_states)` —— 问「**这份文档归谁管**」（本文档自身）。
        #     用 `MANAGED_STATES`（含 `released`）：交还之后文档仍归任务中心管，旧巡检
        #     不许替它补投编译。
        #
        # 下面那个 `NOT (...)` 的豁免也**只对在途行生效**（`tcd.state = ANY(:active_states)`）：
        # 它放行的是「parse 步已收口、正等 compile 派活」这一形状。`mark_released` **不清**
        # `handled_execution_id`，所以同样的形状在 `released` 行上**也能拼出来** —— 不补这
        # 道状态条件，刚交还的文档就会被这个豁免误放行。
        sql = text("""
            SELECT d.id, d.tenant_id, d.knowledge_base_id, d.file_name
              FROM knowledge_base.knowledge_documents d
              JOIN knowledge_base.knowledge_info ki
                ON ki.id = d.knowledge_base_id
               AND ki.tenant_id = d.tenant_id
               AND ki.is_deleted = 0
             WHERE d.is_deleted = 0
               AND (d.llm_wiki_compile_status = 'stale'
                    OR (:kb_type = 'jonex_wiki' AND d.llm_wiki_compile_status IS NULL))
               AND d.status = 'ready'
               AND ki.kb_type = :kb_type
               AND (:kb_type != 'jonex_wiki' OR NOT EXISTS (
                   SELECT 1 FROM knowledge_base.knowledge_documents pending
                    WHERE pending.tenant_id = d.tenant_id
                      AND pending.knowledge_base_id = d.knowledge_base_id AND pending.is_deleted = 0
                      AND (pending.status IN ('pending', 'parsing', 'ingesting')
                           OR (pending.status = 'ready'
                               AND (pending.llm_wiki_compile_status = 'stale'
                                    OR pending.llm_wiki_compile_status IS NULL)
                               AND pending.updated_at > :quiet_before)
                           OR (pending.status != 'failed' AND EXISTS (
                               SELECT 1 FROM knowledge_base.task_center_documents owner
                                WHERE owner.document_id = pending.id AND owner.tenant_id = pending.tenant_id
                                  AND owner.state = ANY(:active_states)
                                  AND (owner.current_step IS DISTINCT FROM 'compile'
                                       OR owner.execution_id IS NULL
                                       OR (pending.extra_metadata->>'jonex_wiki_compile_execution_id')
                                          IS DISTINCT FROM owner.execution_id)
                                  AND NOT (owner.current_step IS NOT DISTINCT FROM 'parse'
                                           AND owner.execution_id IS NULL
                                           AND owner.handled_execution_id IS NOT NULL))))
               ))
               AND NOT EXISTS (
                   SELECT 1 FROM knowledge_base.task_center_documents tcd
                    WHERE tcd.document_id = d.id
                      AND tcd.state = ANY(:managed_states)
                      AND (:kb_type != 'jonex_wiki'
                           OR tcd.current_step IS DISTINCT FROM 'compile'
                           OR tcd.execution_id IS NULL
                           OR (d.extra_metadata->>'jonex_wiki_compile_execution_id')
                              IS DISTINCT FROM tcd.execution_id)
                      AND NOT (:kb_type = 'jonex_wiki'
                               AND tcd.state = ANY(:active_states)
                               AND tcd.current_step IS NOT DISTINCT FROM 'parse'
                               AND tcd.execution_id IS NULL
                               AND tcd.handled_execution_id IS NOT NULL)
               )
             LIMIT :limit
        """)
        async with get_db_session() as session:
            rows = (await session.execute(
                sql,
                {
                    "limit": limit,
                    "kb_type": kb_type,
                    "active_states": list(ACTIVE_STATES),
                    "managed_states": list(MANAGED_STATES),
                    "quiet_before": datetime.now() - timedelta(seconds=5),
                },
            )).all()

        if kb_type == "jonex_wiki":
            return await self._resubmit_jonex_wiki_stale(rows)

        from .openkb_service import KnowledgeCompilerService, openkb_artifact_path
        compiler = KnowledgeCompilerService()
        submitted = 0
        for r in rows:
            md = openkb_artifact_path(r.id)
            if not md.is_file():
                continue
            # [jonex] LLM-Wiki Schema fencing（方案 §6）：补提交同样带版本
            _tver = await compiler.get_active_schema_version(r.tenant_id,
                                                             r.knowledge_base_id)
            if not await compiler.claim_compile(r.tenant_id, r.id,
                                                target_schema_version=_tver):
                continue
            try:
                _artifact = {
                    "document_id": r.id,
                    "source_file_path": "",
                    "parsed_markdown_path": f"parsed/{r.id}/content.md",
                    "assets_dir": f"parsed/{r.id}/assets"
                    if (md.parent / "assets").is_dir() else "",
                    "parser": "mineru",
                    "metadata": {"pages": 0, "title": "", "recompile": True},
                }
                if _tver is not None:
                    _artifact["wiki_schema_version"] = _tver
                result = await compiler.submit_compile(
                    kb_name=r.knowledge_base_id, tenant_id=r.tenant_id,
                    kb_id=r.knowledge_base_id,
                    parsed_artifact=_artifact,
                )
                await compiler.set_task_id(r.tenant_id, r.id, result["task_id"])
                submitted += 1
            except Exception as exc:
                await self._record_openkb_status_by_id(
                    r.id, r.tenant_id, status="failed",
                    error=f"OPENKB_STALE_SUBMIT_FAILED: {exc}"[:1000],
                )
        return submitted

    @staticmethod
    def _jonex_parsed_markdown_rel(document_id: str) -> str:
        """[jonex] 共享卷里该文档已落盘的 parsed 产物，返回相对卷根路径。

        引擎 ingest 只认 ``{source_path, source_name, document_id}``，且 source_path 相对
        inputs 卷根。jonex_wiki 首选 MinerU 结构化 ``content_list.json``，兼容旧的
        ``raw.md``/``content.md``（text 链路产物）。
        """
        root = os.getenv("KB_INPUT_DIR", "/app/inputs").rstrip("/")
        for name in ("content_list.json", "raw.md", "content.md"):
            rel = f"parsed/{document_id}/{name}"
            if os.path.isfile(os.path.join(root, rel)):
                return rel
        return ""

    async def _resubmit_jonex_wiki_stale(self, rows) -> int:
        """Drain each ready KB in one engine invocation, independently of patrol limit."""
        from sqlalchemy import text
        from .jonex_wiki_service import JonexWikiService, stage_jonex_wiki_ingest_source

        compiler = JonexWikiService()
        submitted = 0
        for tenant_id, kb_id in dict.fromkeys((r.tenant_id, r.knowledge_base_id) for r in rows):
            # The uploaded document rows exist before parser submission. Include
            # pending as well as parsing/ingesting; failures/deletions are terminal.
            # updated_at follows the platform's naive local timestamp convention.
            async with get_db_session() as session:
                locked = (await session.execute(text("SELECT pg_try_advisory_xact_lock(hashtext(:key))"),
                    {"key": f"jonex-upload-window:{tenant_id}:{kb_id}"})).scalar()
                if not locked:
                    continue
                waiting = (await session.execute(text("""
                    SELECT 1 FROM knowledge_base.knowledge_documents
                     WHERE tenant_id = :tenant AND knowledge_base_id = :kb AND is_deleted = 0
                       AND (status IN ('pending', 'parsing', 'ingesting')
                            OR (status = 'ready'
                                AND (llm_wiki_compile_status = 'stale' OR llm_wiki_compile_status IS NULL)
                                AND updated_at > :quiet_before)
                            OR (status != 'failed' AND EXISTS (
                                SELECT 1 FROM knowledge_base.task_center_documents owner
                                 WHERE owner.document_id = knowledge_documents.id AND owner.tenant_id = :tenant
                                   AND owner.state = ANY(:active_states)
                                   AND (owner.current_step IS DISTINCT FROM 'compile'
                                        OR owner.execution_id IS NULL
                                        OR (knowledge_documents.extra_metadata->>'jonex_wiki_compile_execution_id')
                                           IS DISTINCT FROM owner.execution_id)
                                  AND NOT (owner.current_step IS NOT DISTINCT FROM 'parse'
                                           AND owner.execution_id IS NULL
                                           AND owner.handled_execution_id IS NOT NULL))))
                     LIMIT 1
                """), {"tenant": tenant_id, "kb": kb_id,
                        "quiet_before": datetime.now() - timedelta(seconds=5),
                        "active_states": list(ACTIVE_STATES)})).first()
                if waiting:
                    continue
                # 同 `_patrol_stale_recompile`：上面 `waiting`（整库忙不忙）留在
                # `ACTIVE_STATES`，这里（本文档归谁管）用 `MANAGED_STATES`，豁免也只对
                # 在途行生效 —— 理由见那一处的注释，别把两处置成同一个集合。
                documents = (await session.execute(text("""
                    SELECT id, file_name, storage_key, storage_backend, file_path
                      FROM knowledge_base.knowledge_documents d
                     WHERE tenant_id = :tenant AND knowledge_base_id = :kb AND is_deleted = 0
                       AND status = 'ready'
                       AND (llm_wiki_compile_status = 'stale' OR llm_wiki_compile_status IS NULL)
                       AND NOT EXISTS (SELECT 1 FROM knowledge_base.task_center_documents tcd
                            WHERE tcd.document_id = d.id AND tcd.tenant_id = :tenant
                              AND tcd.state = ANY(:managed_states)
                              AND (tcd.current_step IS DISTINCT FROM 'compile'
                                   OR tcd.execution_id IS NULL
                                   OR (d.extra_metadata->>'jonex_wiki_compile_execution_id')
                                      IS DISTINCT FROM tcd.execution_id)
                              AND NOT (tcd.state = ANY(:active_states)
                                       AND tcd.current_step IS NOT DISTINCT FROM 'parse'
                                       AND tcd.execution_id IS NULL
                                       AND tcd.handled_execution_id IS NOT NULL))
                     ORDER BY id
                """), {"tenant": tenant_id, "kb": kb_id,
                        "active_states": list(ACTIVE_STATES),
                        "managed_states": list(MANAGED_STATES)})).all()
                artifacts, claimed = [], []
                target = await compiler.get_active_schema_version(tenant_id, kb_id)
                try:
                    for d in documents:
                        if not await compiler.claim_compile(tenant_id, d.id, target_schema_version=target, batch=True):
                            continue
                        try:
                            suffix = os.path.splitext(d.file_name or "")[1].lower()
                            rel = (await stage_jonex_wiki_ingest_source(
                                d.id, file_name=d.file_name or "", storage_key=d.storage_key or "",
                                storage_backend=d.storage_backend or "", file_path=d.file_path or "")
                                if suffix in {".xlsx", ".xlsm"} else self._jonex_parsed_markdown_rel(d.id))
                            if not rel:
                                raise ValueError(f"JONEX_WIKI_NO_PARSED_SOURCE: {d.id}")
                            artifacts.append({"source_path": rel, "source_name": d.file_name or d.id,
                                              "document_id": d.id})
                        except Exception as exc:
                            await self._record_openkb_status_by_id(d.id, tenant_id, status="failed", error=str(exc)[:1000])
                            continue
                        claimed.append(d.id)
                    if not artifacts:
                        continue
                    result = await compiler.compile_documents(kb_id, tenant_id, kb_id, artifacts)
                    warnings = {d["document_id"]: list(d.get("warnings") or [])
                                for d in result.get("documents", [])}
                    for doc_id in claimed:
                        await compiler.set_task_id(tenant_id, doc_id, result["task_id"])
                        if warnings.get(doc_id):
                            await self._record_openkb_status_by_id(doc_id, tenant_id, status="compiling",
                                                                   warnings=warnings[doc_id])
                    submitted += len(claimed)
                except Exception as exc:
                    for doc_id in claimed:
                        await self._record_openkb_status_by_id(doc_id, tenant_id,
                            status="stale" if _is_jonex_wiki_compile_slot_busy(exc) else "failed", error=str(exc)[:1000])
                    logger.exception("jonex_wiki upload-window dispatch failed kb=%s", kb_id)
        return submitted

    async def _record_openkb_status_by_id(self, document_id: str, tenant_id: str, *,
                                          status: str, error: str = "", warnings=None,
                                          applied_schema_version: int | None = None) -> None:
        """[jonex] 按 id+tenant 回写 LLM-Wiki 编译状态（列化；巡检无 ORM 对象场景）。

        applied_schema_version：编译 completed 时回写 llm_wiki_applied_schema_version
        （§8.1 版本 fencing 闭环）；值为 None 时不改动该列。

        warnings：``None`` = **不改动**该列（jonex_wiki 编译完成时用它保留派发阶段
        写入的体检/隔离结论）；``[]`` = 显式清空。只有 ``compiling``/``stale``
        无条件清空 warnings。
        """
        try:
            async with get_db_session() as session:
                repo = KnowledgeDocumentRepository(session)
                fresh = await repo.get_required(document_id, tenant_id)
                fresh.llm_wiki_compile_status = status
                if status in ("compiling", "compiled", "stale"):
                    fresh.llm_wiki_compile_error = None
                if status in ("compiling", "stale"):
                    fresh.llm_wiki_compile_warnings = None
                if status == "failed" and error:
                    fresh.llm_wiki_compile_error = error[:1000]
                if warnings is not None:
                    fresh.llm_wiki_compile_warnings = warnings or None
                if status == "compiled":
                    fresh.llm_wiki_compiled_at = datetime.utcnow()
                if applied_schema_version is not None:
                    fresh.llm_wiki_applied_schema_version = applied_schema_version
                session.add(fresh)
                # [jonex] 数据运营二期：知识编译任务埋点（compile 终态）。
                await record_compile_task(
                    session, tenant_id, document_id=document_id,
                    compile_status=status, kb_id=fresh.knowledge_base_id,
                    content_generation=fresh.content_generation, error=error or None,
                )
                await session.commit()
        except Exception:
            logger.warning("[jonex] patrol 回写 LLM-Wiki 状态失败 doc=%s", document_id, exc_info=True)


__all__ = ["ReconciliationService"]
