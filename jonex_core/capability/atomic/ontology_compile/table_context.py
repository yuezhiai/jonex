# -*- coding: utf-8 -*-
"""[jonex] 表格上下文存档 + A18 表格片聚合。

自 raganything/service/task_manager.py L592-945 + L1012-1176 逐字搬迁（P1，行为不变；
L946-1011 的 sanitize 族已先搬入本包 sanitize.py）。

  · 存档族：content_list 的表格子集 gzip 落盘，供 ontology_only 重抽回放（P0-b）
  · A18 聚合：引擎表格片（ctype=table_row 锚）→ 存档条目（跨仓契约 1 的渲染常量在此）
  · _as_int / _parse_anchor_kv：锚串解析（_parse_anchor_kv 与平台 parse_file_source 同构但不复用）
"""
from __future__ import annotations

import gzip
import json
import logging
import os
import re
import time
from typing import Optional

from .table_grid import get_table_body as _get_table_body

logger = logging.getLogger(__name__)


# 问题：Row-as-Object 表格对象只从 content_list 的 table item 推导
# （_merge_table_objects 的唯一输入），而 content_list 只存在于 full 入库
# 链路。ontology_only 重抽按设计不本地化原文件、不解析（P0-A），
# ctx.content_list 恒为空 → _merge_table_objects 不跑 → 重抽结果**不含
# 表格对象**；而写图是「delete_by_document 全删 + merge」的全量替换
# （ontology_graph_repository.delete_by_document），首轮由表格对象建立的
# 节点会被整体换掉 = 静默丢数据。
#
# 为什么不让重抽「自己重新解析一份」：解析缓存键含 str(file_path
# .absolute()) 与 st_mtime（cache.ParseCacheManager.generate_cache_key），
# 而 _fetch_cos_object 每次下载都落在带随机 uuid 前缀的新路径上 → key 必
# 不同 → 必然 miss → 每次都要重解一遍原文件。代价与失败面都远大于
# 「首轮把表格上下文存一份」。
#
# 故：full 链路在进入本体阶段时把 content_list 的表格子集落盘（持久卷，
# 跨容器重启/重建存活），ontology_only 重抽读回放进 ctx.content_list——
# 让 table_handled / _merge_table_objects / P10 批 2.4 零产出兜底三条既有
# 逻辑**原样生效**，判据一个都不用改，重抽口径自然与首轮一致。
#
# 存档有三种状态，必须区分（否则「本来就没表格」会被误报成「存档丢失」）：
#   文件不存在      → 无存档（上线前入库的老文档）。**不降级**：由本体阶段
#                    按 chunk 判据决定——本文档确有表格 chunk 就拒绝重抽
#                    （见 _run_ontology_extraction 内的 P0-b 闸门）
#   文件存在、空列表 → 首轮确认无表格 → 重抽同样按无表格处理（正常跑）
#   文件存在、非空   → 回放，口径与首轮一致
#
# 为什么无存档时「拒绝重抽」而不是「降级跑」：降级把表格当普通文本抽，
# 产出一批口径不同的通用实体（无结构化 attributes、无复合主键区分），再经
# delete_by_document 全量替换掉首轮的表格对象——不是「补上缺失的」，是
# 「用差的换掉好的」；代价还是 24212 chunk 全量进 LLM（3027 批 × gleaning
# ≈45 分钟）+ backfill O(实体×chunk) 分钟级 CPU。拒绝则一行不动、首轮结果
# 原样保留，KB 侧最多空转 MAX_ONTOLOGY_RETRIES 次（秒级）后落 FAILED，并在
# error 里指明需 force_reparse 重建存档。
#
# 存档用 gzip（后缀 .json.gz）：表体是高重复结构化文本，实测 XBRL 财报
# 1015 张表 / 29431 行的表体原文 23.72MB → 压缩后 0.57MB（41.5x），磁盘占用
# 降到 1/40。
#
# 上限 _TABLE_CTX_MAX_BYTES_DEFAULT **按解压后（原始 JSON）字节**判定，不是
# 压缩后——真约束不是磁盘而是读回时的内存：_load_table_context 一次性
# gzip.decompress + json.loads，再经 _merge_table_objects 展开成 Python 对象
# （通常为 JSON 文本的 3~5 倍）。压缩比是文档属性、能差两个数量级（同一份
# 8MiB 压缩后存档，低压缩比文档解压后可能只有 24MB，高重复文档可达 348MB），
# 故「压缩后」无法约束内存。64MiB 解压后 ≈ 200~320MB Python 对象，考虑
# WORKER_COUNT=4 的并发读回量级可控；gzip 只负责省磁盘。
#
# 已知局限：
#   ① 存档不随文档删除即时回收——靠 _sweep_table_context 的 TTL 兜底
#      （RAG_TABLE_CTX_TTL_SEC，默认 90 天），不做删除链路钩子。
#   ② 存档是首轮 parse 的产物，而 chunk 可被 update_chunk 端点改写；改写后
#      回放的表格上下文与 chunk 实际内容可能漂移（概率低：改写表格 chunk
#      的场景罕见）。真发生则 Row-as-Object 产出的对象会引用已变更的
#      chunk。要根治需在 update_chunk 时失效存档，未做。
_TABLE_CTX_MAX_BYTES_DEFAULT = 64 * 1024 * 1024
_TABLE_CTX_TTL_SEC_DEFAULT = 90 * 24 * 3600
# 全树遍历随文档数增长，故按此间隔节流（模块级时间戳，进程内有效）
_TABLE_CTX_SWEEP_INTERVAL_SEC = 3600
_last_table_ctx_sweep = 0.0


def _table_context_dir() -> str:
    """表格上下文存档目录。

    与 _cos_cache_dir 同口径（KB_INPUT_DIR / WORKING_DIR 下的子目录），但
    独立成 ``_table_ctx``——``_cos_cache`` 有 TTL 清扫（_sweep_cos_cache，
    默认 24h），存档混进去会被清掉。
    """
    explicit = (os.getenv("RAG_TABLE_CTX_DIR") or "").strip()
    if explicit:
        return explicit
    base = (
        os.getenv("KB_INPUT_DIR")
        or os.getenv("WORKING_DIR")
        or "/app/rag_storage"
    )
    return os.path.join(base, "_table_ctx")


def _table_context_path(task) -> str:
    """存档路径 ``{dir}/{tenant}/{kb}/{document_id}.json.gz``（逐段净化防穿越）。"""

    def _seg(value: str) -> str:
        return re.sub(r"[^0-9A-Za-z_.\-]", "_", str(value or ""))[:128] or "_"

    return os.path.join(
        _table_context_dir(),
        _seg(getattr(task, "tenant_id", "")),
        _seg(getattr(task, "kb_id", "")),
        f"{_seg(getattr(task, 'document_id', ''))}.json.gz",
    )


def _table_context_items(content_list) -> list:
    """抽出 content_list 里的表格子集，规整成扁平条目列表。

    两类来源（与 _merge_table_objects 的消费口径一一对应）：
      ① ``type == "table"`` 的 item（MinerU/表格类 parser）；
      ② text item 上 push 阶段登记的 ``_mapped_tables``（md/内嵌表格路径，
         见 pipeline/stages.py §table-llm-bypass）。

    一律规整为 ``{type, table_body, table_caption, table_idx}``：表体统一
    落 ``table_body`` 键（get_table_body 的首选别名，故读回后取值口径不
    变），且**不保留 _mapped_tables 嵌套**——否则读回后同一个表会被
    _merge_table_objects 当作顶层 item 和内层条目各消费一次。
    """

    def _norm(item: dict) -> dict:
        return {
            "type": "table",
            "table_body": _get_table_body(item),
            "table_caption": item.get("table_caption"),
            "table_idx": item.get("table_idx"),
        }

    items: list = []
    for item in content_list or []:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "table":
            items.append(_norm(item))
        for mapped in (item.get("_mapped_tables") or []):
            if isinstance(mapped, dict):
                items.append(_norm(mapped))
    return items


# ── [jonex] A18：引擎表格片 → 存档条目聚合（spec §4.4）──────────────────
#
# 与引擎 `table_packer.py:19-22` 的渲染常量**同源**；引擎侧改这两个常量时
# 这里必须同步（跨仓契约，见 docs/codes-modified-a18-consume-integration.md）。
_A18_CELL_LEAD = "| "
_A18_CELL_TAIL = " |"
_A18_CELL_SEP = " | "
_A18_SELF_DESC_INFIX = ": "
_TABLE_CAPTION_PREFIX = "表格明细：【表】"
_CONTINUATION_SUFFIX_RE = re.compile(r"（续：[^）]*）\s*$")


def _as_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _parse_anchor_kv(file_source: str) -> dict:
    """引擎 file_source 锚串 → {键: 值}（`|` 分段、`=` 拆分）。

    与 `jonex_core.common.file_source_util.parse_file_source` 同构但**不复用**：
    那个实现读掉 `tenant=` 却不回传 tenant 键，而读回校验（T6 前置段）要用它。
    """
    out: dict = {}
    for seg in (file_source or "").split("|"):
        if "=" not in seg:
            continue
        key, _, value = seg.partition("=")
        out[key.strip()] = value
    return out


def _split_cells(line: str, *, strip_cells: bool = True) -> Optional[list]:
    """markdown 表行（`| a | b |`）→ 格列表；非 markdown 行 → None。

    ``strip_cells=False`` 返回**未 strip** 的原始切片——仅供
    `_merge_row_fragments` 拼接用：硬切是纯字符切，缝上的空白属格内容，
    strip 会静默吞掉（I2，缝两侧若是数字 token 会改语义）。表头格等
    既有调用点保持默认 strip 语义。
    """
    if not line.startswith(_A18_CELL_LEAD):
        return None
    body = line[len(_A18_CELL_LEAD):]
    if body.endswith(_A18_CELL_TAIL):
        body = body[: -len(_A18_CELL_TAIL)]
    cells = body.split(_A18_CELL_SEP)
    return [c.strip() for c in cells] if strip_cells else cells


def _strip_selfdesc_cells(line: str) -> list:
    """自描述行（`列名: 值 | 列名: 值`，无前导竖线）→ 逐格剥 `列名: ` 前缀。

    宽松剥：只剥**首个** `: ` 之前的部分；格内无 `: ` 时原样保留（防误剥）。
    """
    out = []
    for part in line.split(_A18_CELL_SEP):
        _, sep, value = part.partition(_A18_SELF_DESC_INFIX)
        out.append((value if sep else part).strip())
    return out


def _merge_row_fragments(frags: list, n_cols: int, sig: str) -> str:
    """一行的碎片 → 单行 markdown（spec N1：同格互斥切片按片序拼接）。

    frags: [(cell_start, cell_end, chunk_index, line_text)]。
    - `cell_start == cursor` ⇒ 连续整格组（软切）⇒ 追加格值；
    - `cell_start == cell_end - 1 == 已收格数 - 1` ⇒ 硬切续片（同一
      `[i, i+1)` 区间）⇒ 与最后一格已有文本拼接；
    - 其余（缺口/重叠）⇒ warning 后确定性降级追加（宁留冗余不丢字）。

    格值一律取**未 strip** 原始切片（I2）：硬切缝上的空白是格内容，
    strip 会把它吞掉、拼出与原文不同的文本。
    """
    cells: list = []
    cursor = 0
    for cell_start, cell_end, chunk_index, line in sorted(
        frags, key=lambda t: (t[0], t[2])
    ):
        values = _split_cells(line, strip_cells=False)
        if values is None:
            values = _strip_selfdesc_cells(line)
        last = len(cells) - 1
        if cell_start == cursor:
            cells.extend(values)
            cursor = cell_end
        elif cell_start == cell_end - 1 and cell_start == last:
            cells[last] = cells[last] + (values[0] if values else "")
        else:
            logger.warning(
                "[jonex][A18] 表格分组 %s 碎片不连续（期望 cell_start=%d，实到 %d，"
                "chunk=%d），确定性降级拼接",
                sig, cursor, cell_start, chunk_index,
            )
            cells.extend(values)
            cursor = cell_end
    if n_cols and len(cells) != n_cols:
        logger.warning(
            "[jonex][A18] 表格分组 %s 行格数不齐（%d != %d；已知有损场景：格值含 "
            "` | `），按现有文本输出",
            sig, len(cells), n_cols,
        )
    return _A18_CELL_LEAD + _A18_CELL_SEP.join(cells) + _A18_CELL_TAIL


def _assemble_table_item(sig: str, pieces: list) -> dict:
    """一组表格片 → 一条存档条目 {type, table_body, table_caption, table_idx}。

    表头锚点法（spec §4.4）：片内**首个 `| ` 行**即表头行（引擎每片恒重发表头），
    它上方是 caption（可多行；续段标记在首个 caption 行行尾，逐行剥除）。
    完整片（`cell_start is None`）数据行原样采用；碎片按 (row_start, row_end)
    键控、`_merge_row_fragments` 拼回。
    """
    def _key(item):
        _, kv = item
        return (_as_int(kv.get("row_start")), _as_int(kv.get("cell_start")),
                _as_int(kv.get("chunk")))

    pieces = sorted(pieces, key=_key)
    caption = ""
    header_cells: Optional[list] = None
    rows: dict = {}
    order: list = []

    for chunk, kv in pieces:
        lines = (chunk.get("content") or "").split("\n")
        header_idx = next(
            (i for i, ln in enumerate(lines) if ln.startswith(_A18_CELL_LEAD)), None
        )
        if header_idx is None:
            raise ValueError(
                f"[jonex][A18] 表格分组 {sig} 内 chunk={kv.get('chunk')} 无表头锚点行"
                "（引擎表格片恒含 `| ` 表头行）——产出形态漂移，拒绝静默聚合"
            )
        if header_cells is None:
            header_cells = _split_cells(lines[header_idx]) or []
        if not caption:
            cap_lines = []
            for ln in lines[:header_idx]:
                ln = _CONTINUATION_SUFFIX_RE.sub("", ln)
                if ln.startswith(_TABLE_CAPTION_PREFIX):
                    ln = ln[len(_TABLE_CAPTION_PREFIX):]
                cap_lines.append(ln)
            caption = "\n".join(ln for ln in cap_lines if ln.strip())

        key = (_as_int(kv.get("row_start")), _as_int(kv.get("row_end")))
        if key not in rows:
            rows[key] = {"full": None, "frags": []}
            order.append(key)
        slot = rows[key]
        data_lines = lines[header_idx + 1:]
        if kv.get("cell_start") is None:
            if slot["full"] is None:
                slot["full"] = data_lines
            else:
                logger.warning(
                    "[jonex][A18] 表格分组 %s 同键 %s 出现多个完整片，取序首", sig, key,
                )
        else:
            slot["frags"].append((
                _as_int(kv.get("cell_start")),
                _as_int(kv.get("cell_end")),
                _as_int(kv.get("chunk")),
                data_lines[0] if data_lines else "",
            ))

    n_cols = len(header_cells or [])
    out_rows: list = []
    for key in order:
        slot = rows[key]
        if slot["full"] is not None:
            if slot["frags"]:
                # 行为保持「完整片优先、碎片丢弃」，但绝不允许静默：引擎 240B
                # 降级链第 2 级会整段丢 cell 锚，降级后的碎片在消费侧看起来
                # 就是「完整片」——此共存态可达（I1），静默丢弃等于丢全量文本。
                logger.warning(
                    "[jonex][A18] 表格分组 %s 同键 %s 完整片与碎片共存，"
                    "碎片 %d 片被丢弃（引擎降级链可能把 cell 锚丢成"
                    "『完整片』，请查 file_source_degraded）",
                    sig, key, len(slot["frags"]),
                )
            for ln in slot["full"]:
                if ln.startswith(_A18_CELL_LEAD):
                    out_rows.append(ln)
                elif ln.strip():
                    out_rows.append(
                        _A18_CELL_LEAD + _A18_CELL_SEP.join(_strip_selfdesc_cells(ln))
                        + _A18_CELL_TAIL
                    )
            continue
        out_rows.append(_merge_row_fragments(slot["frags"], n_cols, sig))

    header_line = _A18_CELL_LEAD + _A18_CELL_SEP.join(header_cells or []) + _A18_CELL_TAIL
    return {
        "type": "table",
        "table_body": "\n".join([header_line, *out_rows]),
        "table_caption": caption,
        "table_idx": _as_int(pieces[0][1].get("table_idx")),
    }


def _table_context_items_from_chunks(chunks: list) -> list:
    """[jonex] A18：引擎全量 chunk → `_table_ctx` 存档条目（聚合，spec §4.4）。

    只消费 `ctype == "table_row"` 锚的片；无表格片 ⇒ 返回 []（合法值：
    「确认无表格」，前置段恒写档会把空列表也写下去）。
    """
    grouped: dict = {}
    for chunk in chunks or []:
        if not isinstance(chunk, dict):
            continue
        kv = _parse_anchor_kv(chunk.get("file_source") or "")
        if kv.get("ctype") != "table_row":
            continue
        sig = kv.get("table_sig")
        if not sig:
            raise ValueError(
                f"[jonex][A18] 表格 chunk（chunk_index={kv.get('chunk')}）缺 table_sig 锚"
                "——引擎产出形态漂移，拒绝静默聚合"
            )
        grouped.setdefault(sig, []).append((chunk, kv))
    items = [_assemble_table_item(sig, pieces) for sig, pieces in grouped.items()]
    items.sort(key=lambda item: item["table_idx"])
    return items




def _sweep_table_context() -> None:
    """按 TTL 回收存档（兜底磁盘泄漏：文档删除链路没有钩子）。

    仿 _sweep_cos_cache，但存档是 {tenant}/{kb}/{doc} 三层树、文件数随文档
    数增长，故用模块级时间戳节流（进程内有效），避免每次落盘都全树遍历。
    TTL 默认 90 天，远长于 _cos_cache 的 24h——存档是重抽的唯一凭据，删早了
    该文档就只能 force_reparse 重建。
    """
    global _last_table_ctx_sweep
    now = time.time()
    if now - _last_table_ctx_sweep < _TABLE_CTX_SWEEP_INTERVAL_SEC:
        return
    _last_table_ctx_sweep = now
    ttl = int(os.getenv("RAG_TABLE_CTX_TTL_SEC") or _TABLE_CTX_TTL_SEC_DEFAULT)
    if ttl <= 0:  # 显式关停清扫
        return
    removed = 0
    try:
        for root, _dirs, files in os.walk(_table_context_dir()):
            for name in files:
                if not name.endswith(".json.gz"):
                    continue
                fp = os.path.join(root, name)
                try:
                    if now - os.path.getmtime(fp) > ttl:
                        os.remove(fp)
                        removed += 1
                except OSError:  # 被并发删掉/权限问题：跳过
                    continue
    except OSError:
        return
    if removed:
        logger.info(
            "[jonex][P0-b] 表格上下文 TTL 回收 %d 个存档（TTL=%ds）", removed, ttl,
        )


def _write_table_context(task, items: list) -> Optional[int]:
    """表格上下文存档底层写口（[jonex] A18 从 `_persist_table_context` 提炼，共用）。

    **无条件可写**：空列表也是合法值（`_load_table_context` 的「首轮确认无表格」）
    ——「空则跳过」是三个上层调用点的语义（`write_when_empty`），不上移到本层；
    A18 前置段（`_ingest_engine_chunks`）必须能写空档（恒写档）。

    best-effort 语义保持：超限（RAG_TABLE_CTX_MAX_BYTES，按解压后原始 JSON
    字节判定）或任何异常 ⇒ warning + None；成功 ⇒ 返回写入条数。
    """
    try:
        _sweep_table_context()
        raw = json.dumps(items, ensure_ascii=False, default=str).encode("utf-8")
        payload = gzip.compress(raw, 6)
        max_bytes = int(
            os.getenv("RAG_TABLE_CTX_MAX_BYTES") or _TABLE_CTX_MAX_BYTES_DEFAULT
        )
        path = _table_context_path(task)
        if len(raw) > max_bytes:
            logger.warning(
                "[jonex][P0-b] 表格上下文超限不落盘（解压后 %d > %d 字节；"
                "压缩后 %d 字节）: doc=%s ——该文档日后重抽将被 P0-b 闸门拒绝",
                len(raw), max_bytes, len(payload), getattr(task, "document_id", ""),
            )
            return None
        os.makedirs(os.path.dirname(path), exist_ok=True)
        # 先写临时文件再 replace：避免重抽读到写了一半的 gzip
        tmp = f"{path}.tmp"
        with open(tmp, "wb") as fh:
            fh.write(payload)
        os.replace(tmp, path)
        logger.info(
            "[jonex][P0-b] 表格上下文已落盘 %d 条 %d→%d 字节（%.1fx）: doc=%s → %s",
            len(items), len(raw), len(payload),
            len(raw) / max(1, len(payload)),
            getattr(task, "document_id", ""), path,
        )
        return len(items)
    except Exception as exc:  # noqa: BLE001 — 存档失败不阻断本体链路
        # sweep 与写入同在此 try 内（结构不动）：此文案须同时覆盖「清扫抛错」
        # （如 RAG_TABLE_CTX_TTL_SEC 非法）与「写入失败」，否则归因误导。
        logger.warning(
            "[jonex][P0-b] 表格上下文落盘/清理失败（不影响本次本体抽取）: doc=%s: %s",
            getattr(task, "document_id", ""), exc,
        )
        return None


def _persist_table_context(
    task, content_list, write_when_empty: bool = True,
) -> Optional[int]:
    """把表格子集 gzip 落盘（按 document_id 幂等覆盖），供日后重抽回放。

    [jonex] A18：写入部分已提炼到 `_write_table_context`（前置段共用，需能
    无条件写空档）；本函数保留既有上层语义（含 write_when_empty 闸门与
    「为什么」的解释，见 _write_table_context 与模块注释）。

    Args:
        write_when_empty: 抽到 0 张表时是否仍写一份**空存档**。默认 True，
            即「0 表示首轮确认无表格」的语义（full 链路用，它一定跑到底，
            「无表格」是权威结论，必须与「无存档」区分开）。
            任务中心三步链路的**第一步**置 False —— 那一步的 content_list 里
            只有 ``type=="table"`` 的 item，md / 内嵌表格的表体要等 push 阶段
            登记成 ``_mapped_tables``（见 _table_context_items），所以第一步
            抽到 0 张表**不代表文档没有表格**。此时落空存档会把「未知」伪装成
            「确认无表格」：第三步的 P0-b 闸门是 ``ctx.content_list is None``
            才拒绝，空列表会被静默放过 → 表格知识凭空消失且不报错。
            宁可**不写**（第三步读回 None → 按 chunk 证据拒绝，会响）。

    Returns:
        int: 写入的表格条目数。0 表示首轮确认无表格——仍写空存档，用于
             把「无表格」与「无存档」区分开（write_when_empty=False 时不会
             返回 0）。
        None: 未写。content_list 为空（非 full 链路/解析无产出）、抽到 0 张表
              且 write_when_empty=False、超限或写入失败。best-effort，仅告警，
              绝不阻断本体链路。
    """
    if not content_list:
        return None
    try:
        items = _table_context_items(content_list)
    except Exception as exc:  # noqa: BLE001 — 与既有 best-effort 语义同源
        logger.warning(
            "[jonex][P0-b] 表格上下文抽取失败（不影响本次本体抽取）: doc=%s: %s",
            getattr(task, "document_id", ""), exc,
        )
        return None
    if not items and not write_when_empty:
        # 刻意不写：在「表格还没登记完」的阶段落空存档，会把「未知」写成
        # 「确认无表格」（详见原 docstring / P0-b spec）。
        logger.debug(
            "[jonex][P0-b] 表格上下文抽到 0 条且 write_when_empty=False，"
            "跳过落盘（保留磁盘上原有的存档，若有）: doc=%s",
            getattr(task, "document_id", ""),
        )
        return None
    return _write_table_context(task, items)


def _load_table_context(task) -> Optional[list]:
    """读回表格子集存档（gzip，兼容明文 JSON 便于运维手工排查）。

    Returns:
        list: 存档中的表格条目；**空列表是有效值**，代表首轮确认无表格。
        None: 无存档或存档损坏——调用方据此交本体阶段的 P0-b 闸门裁决。
    """
    try:
        path = _table_context_path(task)
        if not os.path.isfile(path):
            return None
        with open(path, "rb") as fh:
            blob = fh.read()
        # gzip 魔数 1f 8b 才解压；否则按明文 JSON 读（手工塞存档排查用，
        # 不是历史格式兼容——本功能上线前没有存档文件）
        if blob[:2] == b"\x1f\x8b":
            blob = gzip.decompress(blob)
        data = json.loads(blob.decode("utf-8"))
        if not isinstance(data, list):
            raise ValueError(f"存档格式非列表: {type(data).__name__}")
        return [it for it in data if isinstance(it, dict)]
    except Exception as exc:  # noqa: BLE001 — 读档失败按「无存档」处理
        logger.warning(
            "[jonex][P0-b] 表格上下文存档读取失败（按无存档处理）: doc=%s: %s",
            getattr(task, "document_id", ""), exc,
        )
        return None
