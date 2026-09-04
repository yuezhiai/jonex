#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""
file_source 字符串解析/构造工具（纯函数）。

file_source 是 LightRAG 入库时写入 references[].file_path 的字符串，
包含 kb/doc/tenant/file/chunk 以及可选的位置锚点（字符偏移/页码/时间戳）。

D5：作为共享工具放在 jonex_core/common/，atomic 入库与 gateway 流式解析共用。
D2：位置锚点随 chunk 进入 LightRAG，查询时原样回传，无需额外位置表。
D10：区分 storage_key（COS 对象键）与 file_path（保留原值兼容）。
"""

from __future__ import annotations

import os
import re
from typing import Any

# LightRAG server 端对 workspace 的净化规则：仅保留 [A-Za-z0-9_]，其余替换为 _
# （见 Reference/LightRAG api/config.py 与 lightrag_server.get_workspace_from_request）。
# 客户端必须用同一规则预净化，确保「发送的 workspace 值」== 「服务端落库 workspace」，
# 否则入库与查询会落到不同 workspace，反而读不到数据。
_WORKSPACE_SAFE_RE = re.compile(r"[^A-Za-z0-9_]")


def _safe_ws_segment(value: str) -> str:
    return _WORKSPACE_SAFE_RE.sub("_", (value or "").strip())


def lightrag_workspace(tenant_id: str, knowledge_base_id: str) -> str:
    """计算 LightRAG 的 workspace 隔离命名空间（纯函数）。

    workspace = sanitize(tenant_id)__sanitize(knowledge_base_id)，把 KV / 向量 /
    图三套存储按「租户 + 知识库」整体物理隔离，从根上杜绝跨知识库检索串库。
    入库（upload_text）与查询（/query、/query/stream）必须用同一值，
    LightRAGGraphReader 也据此生成 LIGHTRAG-WORKSPACE 头调 lightrag-server 图端点。

    - 合入 tenant 维度避免跨租户撞名；
    - kb 为空时回落到仅 tenant（理论上业务链路 kb 必填，此处仅作健壮性兜底）。
    """
    t = _safe_ws_segment(tenant_id)
    k = _safe_ws_segment(knowledge_base_id)
    return f"{t}__{k}" if k else t


def build_file_source(
    task: dict[str, Any],
    idx: int,
    *,
    loc: dict[str, Any] | None = None,
) -> str:
    """构造 file_source 字符串（追加可选的位置锚点）。

    缺省字段省略以保持向后兼容；老数据无位置字段时查询端自动降级。
    """
    parts: list[str] = [
        f"kb={task.get('knowledge_base_id', '')}",
        f"doc={task.get('document_id') or ''}",
        f"tenant={task.get('tenant_id', '')}",
        f"file={task.get('file_path', '')}",            # COS 后端=storage_key
        f"chunk={idx}",
    ]
    loc = loc or {}
    if loc.get("char_start") is not None:
        parts.append(f"cstart={loc['char_start']}")
        parts.append(f"cend={loc['char_end']}")
    if loc.get("page_no") is not None:
        parts.append(f"page={loc['page_no']}")
    if loc.get("time_start") is not None:
        parts.append(f"tstart={loc['time_start']:.3f}")
        parts.append(f"tend={loc['time_end']:.3f}")
    parts.append(f"trace={task.get('trace_id') or task.get('task_id', '')}")
    return "|".join(parts)


def parse_file_source(raw: str) -> dict[str, Any]:
    """解析 file_source 字符串为结构化引用片段。

    兼容旧格式与缺省字段（旧格式 = 不带 | 的纯路径）。
    """
    if not raw:
        return {}
    if "|" not in raw or "=" not in raw:
        return {"file_path": raw, "storage_key": raw}

    kv: dict[str, str] = {}
    for seg in raw.split("|"):
        if "=" in seg:
            k, _, v = seg.partition("=")
            kv[k.strip()] = v.strip()

    def _num(v: str | None, cast: type) -> int | float | None:
        if v is None:
            return None
        try:
            return cast(v)
        except (TypeError, ValueError):
            return None

    file_seg = kv.get("file")  # COS 后端=storage_key；local 后端=本地路径
    return {
        "kb_id": kv.get("kb"),
        "doc_id": kv.get("doc") or None,
        "storage_key": file_seg,       # 新语义：对象键（D10）
        "file_path": file_seg,         # 兼容保留原值
        "chunk_index": _num(kv.get("chunk"), int),
        "char_start": _num(kv.get("cstart"), int),
        "char_end": _num(kv.get("cend"), int),
        "page_no": _num(kv.get("page"), int),
        # [jonex] §block-packing: 跨页打包 chunk 的末页（仅打包 chunk 写入；
        # 旧 chunk 无此键 → None）
        "page_end": _num(kv.get("page_end"), int),
        # [jonex] §block-packing: 页边界表（offset@page;…，仅跨页打包
        # chunk 写入）；检索侧按命中片段 offset 精算页码（见
        # resolve_page_by_offset）
        "pspans": kv.get("pspans") or None,
        "time_start": _num(kv.get("tstart"), float),
        "time_end": _num(kv.get("tend"), float),
        # [jonex] §table-chunking: row range for table-row-level references
        "row_start": _num(kv.get("row_start"), int),
        "row_end": _num(kv.get("row_end"), int),
        # [jonex] §C1-bis §19.5①: 行内切分子段的格区间（0-based 右开），
        # 仅当行内切分发生（split_row_by_cells）时写入；旧 chunk 无此键 → None
        "cell_start": _num(kv.get("cell_start"), int),
        "cell_end": _num(kv.get("cell_end"), int),
        "table_idx": _num(kv.get("table_idx"), int),
        # [jonex] §image-refs: 图片 chunk 的模态序号（image_idx=，写入侧见
        # raganything stages.py _build_file_source）与资产扩展名旁路（aext=，
        # 仅 P1 资产上传成功的图片写入）；旧 chunk 无此键 → None
        "image_idx": _num(kv.get("image_idx"), int),
        "asset_ext": kv.get("aext") or None,
        # [jonex] §table-ctypes: chunk 类型与表格元数据旁路
        # （table-parsing-retrieval-governance-plan.md 改动 36。
        #   ctype=table_row / table_summary / text / image / audio / video / …，
        #   供检索侧双路召回与低质过滤使用；旧 chunk 无此键 → None）
        "chunk_type": kv.get("ctype") or None,
        "table_sig": kv.get("table_sig") or None,
        # 列名清单以 \x1f（unit separator）分隔（写入侧 join 用 \x1f，
        # 与键值分隔符 | 和可见标点都无冲突）；展示时按需 split("\x1f")
        "table_cols": kv.get("table_cols") or None,
        # [jonex] §table-grid-v2 L4.1: 表前说明（notes）旁路，不进正文
        "notes": kv.get("notes") or None,
        # [jonex] 第五批 改动 20（方案 C 入库通道）：表格 chunk 的主体
        # 实体提示（= 表标题 heading，写入侧 ehint= 键），检索期主体
        # 一致性过滤的信号源；旧 chunk 无此键 → None
        "entity_hint": kv.get("ehint") or None,
        # [jonex] §image-refs E 方案：图片 chunk 版面主题锚点来源
        # （anchor_src=cap|foot|col|heading，写入侧见 raganything
        # stages.py _build_file_source），供锚点命中率统计使用；
        # 旧 chunk 无此键 → None
        "anchor_src": kv.get("anchor_src") or None,
    }


# ── 媒体类型分类 ─────────────────────────────────────────────

_MEDIA_BY_EXT: dict[str, set[str]] = {
    "text": {".txt", ".md", ".markdown"},
    "pdf": {".pdf"},
    "audio": {".mp3", ".wav", ".m4a", ".flac", ".aac", ".ogg", ".wma"},
    "video": {".mp4", ".avi", ".mov", ".mkv", ".webm", ".flv", ".wmv", ".m4v"},
    "image": {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"},
}


def classify_media(mime_type: str | None, file_name: str | None) -> str:
    """按 mime_type 优先、扩展名兜底分类，供前端选择查看器。"""
    mt = (mime_type or "").lower()
    if mt.startswith("audio/"):
        return "audio"
    if mt.startswith("video/"):
        return "video"
    if mt.startswith("image/"):
        return "image"
    if mt == "application/pdf":
        return "pdf"
    if mt.startswith("text/"):
        return "text"
    ext = os.path.splitext(file_name or "")[1].lower()
    for media, exts in _MEDIA_BY_EXT.items():
        if ext in exts:
            return media
    return "other"


# ── chunk 内容归一化 ─────────────────────────────────────────

# 入库时注入的命名空间隔离标记（写入侧见 raganything stages.py ns_token，
# 格式 `<!--yx:[a-f0-9]{8}-->`），LOCAL/REMOTE 查询链路统一在此清理。
_NS_TOKEN_RE = re.compile(r"\s*<!--yx:[0-9a-f]+-->\s*")


def normalize_chunk_content(content: Any) -> tuple[str | None, list[str]]:
    """[jonex] §10 L1：把 LightRAG references 的 content 归一化为
    (全文, 逐条 chunk 文本)。

    content 为 list[str]（同一 file_path 聚合的多 chunk 文本数组，与
    chunk_ids 对齐，见 vendored query_routes.py [jonex] 透出）或 str。
    返回：

    - ``text``：合并全文（逐条清理 ns token 后**过滤空条**再以
      ``\\n\\n`` 连接、整体 strip），供前端「关联原文」展示；无法
      提取时为 None。过滤空条是刻意的：text 要与包原文逐字符对齐
      （pspans 坐标基准），空条贡献的 ``\\n\\n`` 会让坐标整体偏移
      （review P2 修正，2026-08-20）
    - ``chunk_texts``：逐条清理 ns token 的文本列表，长度恒等于入参
      list（**不过滤**，空条保持位置），供页段精算等逐 chunk 对齐消费

    空列表 / 空串 / None / 清洗后全空 → ``(None, [])``（list 入参
    清洗后全空时 chunk_texts 仍保留原长，仅 text 为 None）。
    """
    if isinstance(content, list):
        cleaned = [
            _NS_TOKEN_RE.sub("", c).strip() if isinstance(c, str) else ""
            for c in content
        ]
        text = "\n\n".join(c for c in cleaned if c).strip() or None
        return text, cleaned
    if isinstance(content, str) and content:
        cleaned = _NS_TOKEN_RE.sub("", content).strip()
        if not cleaned:
            return None, []
        return cleaned, [cleaned]
    return None, []


def resolve_page_by_offset(pspans: str, offset: int) -> int | None:
    """[jonex] §block-packing: 按 chunk 内字符 offset 在 pspans 页边界表
    中定位页号。

    pspans 格式 ``offset@page;offset@page;…``（首 entry 恒为 ``0@{标题页}``，
    写入侧见 ``pack_text_blocks``）。空串 / 格式非法 / offset 为负时返回
    None，由调用方回落到 page 粗锚点；offset 落在末 entry 之后时返回末
    entry 页号（该页延伸到 chunk 尾）。
    """
    if not pspans or offset < 0:
        return None
    entries: list[tuple[int, int]] = []
    for seg in pspans.split(";"):
        if "@" not in seg:
            continue
        a, _, b = seg.partition("@")
        try:
            off = int(a)
            page = int(b)
        except ValueError:
            continue
        entries.append((off, page))
    if not entries:
        return None
    entries.sort(key=lambda e: e[0])
    hit: int | None = None
    for off, page in entries:
        if off <= offset:
            hit = page
        else:
            break
    return hit


def to_location(r: dict[str, Any]) -> dict[str, Any]:
    """按命中片段的可用位置字段决定 location 类型。

    Priority: timestamp > table_row > image > page > char > chunk.
    ``table_row`` is placed before ``page`` because MinerU-produces tables
    always carry ``page_no=0``, which would otherwise shadow the row-range.
    ``image`` is likewise placed before ``page`` (see image-reference-chain
    execution plan P0-2) — image chunks usually carry ``page_no`` too, which
    would shadow the image index.
    """
    text = r.get("text")
    if r.get("time_start") is not None:
        return {
            "type": "timestamp",
            "time_start": r["time_start"],
            "time_end": r.get("time_end"),
            "chunk_index": r.get("chunk_index"),
            "text": text,
        }
    # [jonex] §table-chunking: row-level positioning for table chunks
    if r.get("row_start") is not None and r.get("row_end") is not None:
        loc = {
            "type": "table_row",
            "row_start": r["row_start"],
            "row_end": r["row_end"],
            "table_idx": r.get("table_idx"),
            "chunk_index": r.get("chunk_index"),
            "text": text,
        }
        # [jonex] §C1-bis §19.5①: 行内格区间透出（行内切分子段才有），
        # 优先级不变——table_row 仍在 page 之前。
        if r.get("cell_start") is not None and r.get("cell_end") is not None:
            loc["cell_start"] = r["cell_start"]
            loc["cell_end"] = r["cell_end"]
        return loc
    # [jonex] §image-refs P0-2: 图片级定位（置于 page 之前——图片 chunk 通常
    # 带 page_no，会遮蔽 image_idx；与 table_row 的既有教训同型）。
    # asset_ext 不在此透出：它是检索侧推导对象键的中间量（见 search_service
    # _build_references 的 asset_url 富化）。
    if r.get("image_idx") is not None:
        return {
            "type": "image",
            "image_idx": r["image_idx"],
            "page_no": r.get("page_no"),
            "chunk_index": r.get("chunk_index"),
            "text": text,
        }
    if r.get("page_no") is not None:
        loc = {
            "type": "page",
            "page_no": r["page_no"],
            "chunk_index": r.get("chunk_index"),
            "text": text,
        }
        # [jonex] §block-packing: 跨页打包 chunk 的末页透出（前端可展示
        # 页范围；旧 chunk 无 page_end 不写该字段，前端零改动）
        if r.get("page_end") is not None:
            loc["page_end"] = r["page_end"]
        return loc
    if r.get("char_start") is not None:
        return {
            "type": "char",
            "char_start": r["char_start"],
            "char_end": r.get("char_end"),
            "chunk_index": r.get("chunk_index"),
            "text": text,
        }
    return {"type": "chunk", "chunk_index": r.get("chunk_index"), "text": text}


__all__ = [
    "build_file_source",
    "classify_media",
    "normalize_chunk_content",
    "parse_file_source",
    "resolve_page_by_offset",
    "to_location",
]
