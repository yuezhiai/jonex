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
        "time_start": _num(kv.get("tstart"), float),
        "time_end": _num(kv.get("tend"), float),
        # [jonex] §table-chunking: row range for table-row-level references
        "row_start": _num(kv.get("row_start"), int),
        "row_end": _num(kv.get("row_end"), int),
        "table_idx": _num(kv.get("table_idx"), int),
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


def to_location(r: dict[str, Any]) -> dict[str, Any]:
    """按命中片段的可用位置字段决定 location 类型。

    Priority: timestamp > table_row > page > char > chunk.
    ``table_row`` is placed before ``page`` because MinerU-produces tables
    always carry ``page_no=0``, which would otherwise shadow the row-range.
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
        return {
            "type": "table_row",
            "row_start": r["row_start"],
            "row_end": r["row_end"],
            "table_idx": r.get("table_idx"),
            "chunk_index": r.get("chunk_index"),
            "text": text,
        }
    if r.get("page_no") is not None:
        return {
            "type": "page",
            "page_no": r["page_no"],
            "chunk_index": r.get("chunk_index"),
            "text": text,
        }
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
    "parse_file_source",
    "to_location",
]
