# -*- coding: utf-8 -*-
"""[jonex] 读回比对用的文本归一与分片容忍。

自 raganything/service/task_manager.py L946-1011 逐字搬迁（P1，行为不变）。
  _DEDUP_*          : LightRAG sanitize_text_for_encoding 的同源复刻（跨仓契约 2）
  _FRAGMENT_*       : ns marker 半截形态的剥离正则
"""
from __future__ import annotations

import html
import re


# [jonex] A18：与 LightRAG `sanitize_text_for_encoding`（lightrag/utils.py 的
# `sanitize_text_for_encoding`，其 regex 在两处模块常量）**同源复刻**——灌库
# 去重门②（sanitized 内容 hash）的键形态。T1 实测（2026-10-10）：读回 =
# `html.unescape(raw).strip()` 形态；实测管线 strip → unescape → 去 surrogate
# → 去控制符（\t\n\r 保留）→ strip。LightRAG 侧改口径时这里必须同步。
_DEDUP_SURROGATE_PATTERN = re.compile(r"[\uD800-\uDFFF\uFFFE\uFFFF]")
_DEDUP_CONTROL_CHAR_PATTERN = re.compile(r"[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]")


def _sanitize_for_dedupe(text: str, replacement_char: str = "") -> str:
    """LightRAG sanitize_text_for_encoding 的同源复刻（读回比对用）。"""
    if not isinstance(text, str):
        return text
    text = text.strip()
    if not text:
        return text
    text = html.unescape(text)
    text = _DEDUP_SURROGATE_PATTERN.sub(replacement_char, text)
    text = _DEDUP_CONTROL_CHAR_PATTERN.sub(replacement_char, text)
    return text.strip()


# [jonex] A18 读回通道事实（真机实测 172.30.2.57，2026-10-10）：
# · 列表端点 `GET /documents/chunks?doc_id=` 读回时剥除 ns marker 与环绕空白
#   （`Reference/LightRAG/lightrag/api/routers/document_routes.py:3923-3928`
#   的 `\s*<!--yx:[0-9a-f]+-->\s*` 替换）⇒ 本地比对一律用注入前内容。
# · 超限片（>引擎 1200 token）被按窗口 1200 / 步长 1100 token 重切为多条记录，
#   相邻分片重叠约 130 字符；分片 = 全片 sanitize 后连续子串（探针 13/13 逐字
#   复现，`.local-verify/a18supp/`）。
_FRAGMENT_TAIL_RE = re.compile(r"<!--yx:[0-9a-f]{0,8}-{0,2}$")
_FRAGMENT_HEAD_RE = re.compile(r"^(?:yx:)?[0-9a-f]{1,8}-{0,2}-->")


def _fragment_piece_form(raw: str) -> str:
    """重切分片的规范化形态：剥边缘 U+FFFD（多 token 字符压窗口边缘的半截
    token decode 产物，探针实测）与边缘半截 ns marker（窗口切在 marker 内部；
    机制推论未及实测，残余更碎形态方向=loud，记入 T7 文档）。"""
    piece = _sanitize_for_dedupe(raw)
    piece = piece.strip("�")
    piece = _FRAGMENT_HEAD_RE.sub("", piece)
    piece = _FRAGMENT_TAIL_RE.sub("", piece)
    return piece.strip("�").strip()


def _fragments_match_content(
    content_key: str, anchors: set, record_contents: dict,
) -> bool:
    """duplicated 条（如超限片重跑被内容门拒）的分片形态容忍（Important-1）。

    超限片首推时被重切（同锚多条分片）、重跑被拒后「全片」不再逐条在场，
    故补分片判据：该条任一锚存在非空读回记录，逐条（归一后）均为内容
    sanitize 子串，且合计长度覆盖内容长度（防「分片缺失被当全量」掩蔽）。
    不得放宽为「锚在场即过」——同锚异内容（I6）的响亮失败必须保留。"""
    for anchor in anchors:
        pieces = [
            _fragment_piece_form(c) for c in (record_contents.get(anchor) or [])
        ]
        if not pieces:
            continue
        if all(piece in content_key for piece in pieces) and (
            sum(len(piece) for piece in pieces) >= len(content_key)
        ):
            return True
    return False
