"""
Utility functions for RAGAnything

Contains helper functions for content separation, text insertion, and other utilities
"""

import base64
import inspect
import re
from typing import Dict, List, Any, Optional, Tuple
from pathlib import Path
from lightrag.utils import logger


# [jonex] P2：表格网格化闭包已迁至 jonex_core.capability.atomic.ontology_compile.table_grid。
# 此处 re-export 保留调用点与 patch 点（测试 patch `raganything.utils.get_table_body`）；
# 同一份实现，不会有漂移。P4 退役时连同本段一起删。
from jonex_core.capability.atomic.ontology_compile.table_grid import (  # noqa: F401
    format_table_body,
    get_table_body,
    normalize_caption_list,
    normalize_table_grid,
    normalize_table_rows,
    table_grid_v2_enabled as _table_grid_v2_enabled,  # 原名保留，供 _compile_deps 注入
)
# 上述符号之外的**私有** helper 也被本仓既有测试经 `_utils.<name>` 取用
# （tests/test_table_grid_v2.py、tests/test_table_row_chunking.py）；同样转发，
# 否则那两个文件在 collection 期 AttributeError（断链）。仍是同一份实现。
from jonex_core.capability.atomic.ontology_compile.table_grid import (  # noqa: F401
    _HEADER_CELL_MAX_LEN,
    _HTMLTableGridExtractor,
    _HTMLTableRowExtractor,
    _detect_suspect_left_shift,
    _is_header_row,
    _normalize_cell_value,
    _table_header_max_len,
)


_HTML_TABLE_RE = re.compile(
    r"<table[\s>].*?</table>", re.IGNORECASE | re.DOTALL
)
_MD_PIPE_LINE_RE = re.compile(r"^\s*\|.*\|\s*$")
_MD_TABLE_MIN_LINES = 3  # 连续 ≥3 行管道行才判为表格（文档 O2 口径）


def extract_embedded_tables(text: str) -> List[Dict[str, Any]]:
    """[jonex] §table-grid-v2 O2: split a text block into alternating
    text / table segments.

    Detects HTML ``<table>…</table>`` fragments and runs of ≥3 consecutive
    markdown pipe lines inside a ``type == "text"`` block (md documents with
    embedded tables are never routed through the table branch otherwise, and
    their HTML/pipe markup gets hard-cut by LightRAG token chunking).

    Returns a list of ``{"kind": "text"|"table", "text": str}`` segments in
    original order.  A block without embedded tables returns a single
    ``{"kind": "text"}`` segment.  Table segments feed
    :func:`normalize_table_grid` via the table branch of
    ``_collect_text_chunks``; surrounding text segments become ordinary text
    chunks and serve as caption candidates for L4.1.
    """
    segments: List[Dict[str, Any]] = []

    # ── pass 1: HTML <table>…</table> fragments ──
    pos = 0
    for m in _HTML_TABLE_RE.finditer(text):
        if m.start() > pos:
            segments.append({"kind": "text", "text": text[pos:m.start()]})
        segments.append({"kind": "table", "text": m.group(0)})
        pos = m.end()
    if pos < len(text):
        segments.append({"kind": "text", "text": text[pos:]})
    if not segments:
        segments.append({"kind": "text", "text": text})

    # ── pass 2: markdown pipe-table runs inside text segments ──
    refined: List[Dict[str, Any]] = []
    for seg in segments:
        if seg["kind"] != "text":
            refined.append(seg)
            continue
        seg_text = seg["text"]
        lines = seg_text.split("\n")
        out_lines: List[str] = []
        i = 0
        n = len(lines)
        while i < n:
            if _MD_PIPE_LINE_RE.match(lines[i]):
                j = i
                while j < n and _MD_PIPE_LINE_RE.match(lines[j]):
                    j += 1
                if j - i >= _MD_TABLE_MIN_LINES:
                    if out_lines:
                        refined.append({
                            "kind": "text", "text": "\n".join(out_lines),
                        })
                        out_lines = []
                    refined.append({
                        "kind": "table", "text": "\n".join(lines[i:j]),
                    })
                    i = j
                    continue
            out_lines.append(lines[i])
            i += 1
        if out_lines:
            refined.append({"kind": "text", "text": "\n".join(out_lines)})

    # 纯空白 text 段（表格间的换行等）不产出，避免空 chunk
    refined = [
        s for s in refined
        if s["kind"] != "text" or s["text"].strip()
    ]
    return refined or [{"kind": "text", "text": text}]


_CAPTION_PREFIX = "表格明细：【表】"   # L4.2 明细行正文前缀 + 单行表标题
_CAPTION_MAX_HEAD_RATIO = 0.08        # 上下文头 ≤ chunk 总长 8%（L4.1 定量约束）
_CAPTION_TRUNCATE_KEEP = 30           # 超长标题截断保留前 N 字 + …
_CAPTION_BLACKLIST = {"none", "null", "n/a"}


def _valid_caption(raw: Any) -> Optional[str]:
    """[jonex] §table-grid-v2 L4.1: caption 有效性校验。

    判定无效并返回 ``None`` 的情形：``None`` / ``[]`` / ``""`` / 纯空白 /
    字面量 ``"None"``、``"none"``、``"null"``、``"N/A"``（大小写不敏感）/
    list 展平后为空 / 长度 > 60 字。否则返回展平 join 后的字符串。
    """
    flat = normalize_caption_list(raw)
    if not flat:
        return None
    cleaned = [
        s for s in flat
        if s.strip().lower() not in _CAPTION_BLACKLIST
    ]
    if not cleaned:
        return None
    joined = ", ".join(cleaned)
    if len(joined) > 60:
        return None
    return joined


def _detect_group_column(
    rows: List[List[str]], n_cols: int,
) -> Optional[int]:
    """[jonex] §table-grid-v2 O3: 分组键检测。

    rowspan forward-fill 的产物——某列在连续多行中同值（如「交易所」列）。
    选变化次数最少且 < 行数一半的列（只看前 8 列，分组键通常靠左）。
    """
    best_col: Optional[int] = None
    best_changes = len(rows)
    for c in range(min(n_cols, 8)):
        changes = 0
        prev: Optional[str] = None
        for row in rows:
            v = row[c].strip() if c < len(row) else ""
            if v != prev:
                changes += 1
                prev = v
        # 变化次数 ≤ 行数一半 → 存在连续同值段（rowspan forward-fill 产物）
        if changes < best_changes and changes <= len(rows) * 0.5:
            best_changes = changes
            best_col = c
    return best_col


def _group_value(row: List[str], group_col: Optional[int]) -> str:
    if group_col is None:
        return ""
    return row[group_col].strip() if group_col < len(row) else ""


def _render_caption_line(
    caption: str, budget: int, continuation: str = "",
) -> str:
    """L4.1: 单行上下文头「表格明细：【表】标题」，≤ 8% 预算，超长截断。

    组内续块（O3）追加「（续：分组值）」标注。

    [jonex] §C1-bis: *continuation* 承载两类语义、由调用点区分——
    ① O3 组内续块传**分组值**；② 行内切分（split_row_by_cells）传
    **已格式化的行号字符串**（如 ``f"第 {row_start + 1} 行"``，1-based）。
    本函数只做「传什么渲染什么」，不做任何格式化。
    """
    suffix = f"（续：{continuation}）" if continuation else ""
    max_head = max(16, int(budget * _CAPTION_MAX_HEAD_RATIO))
    cap_budget = max(1, max_head - len(_CAPTION_PREFIX) - len(suffix) - 1)
    cap = str(caption).strip()
    if len(cap) > cap_budget:
        cap = cap[:min(_CAPTION_TRUNCATE_KEEP, cap_budget)] + "…"
    if len(cap) > cap_budget:
        cap = cap[:max(1, cap_budget - 1)] + "…"
    return f"{_CAPTION_PREFIX}{cap}{suffix}\n"


# [jonex] §C1-bis: markdown 格式的列数阈值（pack_rows 与行内切分共用，
# 两处口径必须一致——行内切分需要知道该行是否走了 markdown 分支）。
DEFAULT_COLUMN_THRESHOLD = 6


def fmt_row(
    row: List[str],
    padded_header: List[str],
    n_cols: int,
    use_markdown: bool,
) -> str:
    """[jonex] §C1-bis: pack_rows 的行渲染逻辑提为模块级函数。

    行内切分（split_row_by_cells）需要复用同一渲染口径，才能用 `` | `` join
    出的单元格分隔符作为切分边界；两种格式都不产生换行（单元格值自带换行
    除外，见 table-parsing-retrieval-governance-plan.md §19.2）。
    """
    padded = list(row) + [""] * (n_cols - len(row))
    if use_markdown:
        return "| " + " | ".join(str(c) for c in padded) + " |"
    else:
        return " | ".join(
            f"{padded_header[i]}: {str(v)}" for i, v in enumerate(padded)
        )


def make_header_block(
    padded_header: List[str],
    n_cols: int,
    use_markdown: bool,
) -> str:
    """[jonex] §C1-bis: pack_rows 的表头块逻辑提为模块级函数。

    markdown 分支返回「列名行 + ``---`` 分隔行」，self-describing 分支返回
    空串（表头由每格的 ``列名:`` 前缀隐含）。
    """
    if use_markdown:
        sep = "| " + " | ".join(["---"] * n_cols) + " |"
        return (
            "| " + " | ".join(padded_header) + " |\n" + sep
        )
    else:
        # Self-describing format — header is implied by row labels
        return ""


# [jonex] §block-packing 文本块级打包（docs/text-block-packing-chunk-governance-plan.md §3.2/§4 改动 1）
# 判据与表格链路 caption L4.1 复用同一套「句末符」常量——utils.py 不能
# import stages.py（循环依赖），故在此定义同值常量，与
# stages.py:1131 的 _CAPTION_SENTENCE_END 保持同源同步。
_HEADING_SENTENCE_END = "。！？；."   # 句末符——正文结尾而非标题

_HEADING_PATTERNS = (
    r"^第[一二三四五六七八九十百零〇\d]+[章节条款部分篇]",   # 第一节 / 第3章
    r"^[\d]+(\.[\d]+)*[\s、.]",                      # 1. / 1.2.3 / 3、
    r"^[（(][一二三四五六七八九十\d]+[)）]",              # （一） / (3)
    r"^(图|表|附录|附表|附图)\s*[\d一二三四五六七八九十]+",  # 图1 / 表 3 / 附录A
    r"^[A-Z][.、]\s",                                   # A. / B、
)

_NOISE_NUM_RE = re.compile(r"^[\d]{1,4}$")
_NOISE_ROMAN_RE = re.compile(r"^[ivxlcdmIVXLCDM]{1,4}$")
_NOISE_SYMBOL_RE = re.compile(r"^[\W_]{1,4}$")


def is_noise_block(
    text: str,
    repeat_index: Optional[Dict[str, int]] = None,
) -> bool:
    """§block-packing C2/C3: 纯数字/纯符号页码、高重复页眉页脚判定。

    C2：strip 后长度 ≤ 4 且通篇只有数字 / 罗马数字 / 符号——这类块不可能
    承载语义；C3：长度 ≤ 20 且全文出现 ≥ 3 次（页眉页脚形态），阈值取 3
    是为了避开「章节名恰好重复一次」的正常情况（见 §3.2）。

    Args:
        repeat_index: 全文档短块（≤ 20 字）出现次数预扫结果（key 为 strip
            后文本）。为 None 时跳过 C3，仅做 C2 判定。
    """
    t = text.strip()
    if not t:
        return True
    if len(t) <= 4 and (
        _NOISE_NUM_RE.match(t)
        or _NOISE_ROMAN_RE.match(t)
        or _NOISE_SYMBOL_RE.match(t)
    ):
        return True
    if repeat_index is not None and len(t) <= 20:
        return repeat_index.get(t, 0) >= 3
    return False


def is_heading_like(text: str, max_len: int = 40) -> bool:
    """§block-packing C4/C5: 短 + 无句末符 + 无换行 → 视作标题。

    C4 编号模式命中仍受长度闸门约束：仅当 strip 后长度 ≤
    ``max(max_len * 2, 80)`` 才判 heading。编号样式的长正文段（如
    「1. 政策出台背景是……」数百字）绝不能判标题——否则包头渲染截断
    会真丢正文（review P0-1）。C5 三条同时满足才判定：strip 后长度 ≤
    *max_len*、末字符不在句末符集合、无换行。误判后果仅是「该句从正文
    位置挪到包头首行」，零信息损失。
    """
    t = text.strip()
    if not t:
        return False
    if any(re.match(p, t) for p in _HEADING_PATTERNS):
        return len(t) <= max(max_len * 2, 80)
    if len(t) > max_len:
        return False
    if "\n" in t:
        return False
    return t[-1] not in _HEADING_SENTENCE_END


def _render_pack_head(heads: List[str], budget: int) -> Tuple[str, str]:
    """渲染包头 ``【A / B】\n``，返回 ``(head, overflow)``。

    总长超过预算 8% 时截断显示，被截掉的完整包头作为 *overflow* 回传、
    由调用方转为正文首行（review P0-1：截断不得丢正文）。与 pack_rows 的
    caption 行同约定（≤ 8% 预算），见 §3.2。
    """
    if not heads:
        return "", ""
    max_head = max(16, int(budget * 0.08))
    full = "【" + " / ".join(heads) + "】"
    if len(full) <= max_head:
        return full + "\n", ""
    return full[: max(1, max_head - 1)] + "…\n", full


def pack_text_blocks(
    blocks: List[Dict[str, Any]],
    budget: int,
    *,
    heading_max_len: int = 40,
    heading_levels: int = 2,
    drop_noise: bool = True,
    repeat_index: Optional[Dict[str, int]] = None,
    carry: Optional[Dict[str, Any]] = None,
) -> Tuple[List[Dict[str, Any]], List[Tuple[str, str]]]:
    """§block-packing: 相邻文本块打包（pack_rows 的文本链路同构实现）。

    单遍顺序扫描 + 一个累积缓冲（§3.2.1 状态机）。输入是 stages 侧分派后
    的纯文本块序列（硬边界 table/image/equation 已由调用方在进本函数前
    冲刷），块内允许缺省字符范围锚点（O2 内嵌表格分支，见 §5.2）。

    标题页计入包跨度：新收标题的首包，包头渲染在包内 offset 0，标题页
    ≠ 正文页时写出 ``pspans``（首 entry 为 ``0@{标题页}``）；续包（标题
    跨预算/跨硬边界继承）page_start 取首个正文块页，``pspans`` 首 entry
    为 ``0@{首正文块页}``（review P0-2：不得把续包页码拉回标题页）。

    Args:
        blocks: [{"text","page_idx","char_start","char_end","line_start",
                  "line_end"}]，page_idx 之外的字段可缺省。
        budget: 字符预算（含包头；stages 侧按 RAG_TEXT_PACK_CHARS 传入）。
        heading_max_len: C5 判定长度上限（env RAG_TEXT_PACK_HEADING_MAX_LEN）。
        heading_levels: 包头保留的最近标题级数（默认 2）。
        drop_noise: False 时噪声块不丢弃、按正文吸附（零信息损失模式，
            env RAG_TEXT_PACK_DROP_NOISE，保守上线可先用该模式）。
        repeat_index: C3 用全文档短块出现次数预扫（stages 侧打包前一次
            O(n) 预扫，见 §8）。
        carry: 跨调用包头状态（review P1：标题须跨 table/image 硬边界
            继承）。传入 dict 时以其 ``heads`` / ``head_pages`` /
            ``heads_fresh`` 键为初值，结束时原地更新回传；None 则每次
            调用从头初始化（纯函数模式，既有测试用）。

    Returns:
        ``(packs, dropped)``。
        packs: [{"text","page_start","page_end","pspans","char_start",
                 "char_end","line_start","line_end","block_count","heading"}]。
        ``pspans`` 仅跨页包非 None（格式 ``offset@page;…``），``page_end``
        仅当 ≠ page_start 时非 None；字符范围锚点全缺省时对应字段为 None。
        dropped: [(text, reason), ...]，供采样日志与 blocks_dropped_noise
        计数透出（reason 目前恒为 "noise"）。
    """
    packs: List[Dict[str, Any]] = []
    dropped: List[Tuple[str, str]] = []

    if carry is not None:
        cur_heads = list(carry.get("heads") or [])
        head_pages = list(carry.get("head_pages") or [])
        heads_fresh = bool(carry.get("heads_fresh", True))
    else:
        cur_heads: List[str] = []
        head_pages: List[Optional[int]] = []
        heads_fresh = True
    cur_lines: List[str] = []
    body_chars = 0                      # 正文累计字符（含换行）
    cur_pspans: List[Tuple[int, Optional[int]]] = []   # (正文起点 offset, page)
    page_start: Optional[int] = None    # 包首块页号（新收标题包=标题页）
    cur_page: Optional[int] = None      # 已收最后一块的页号
    block_count = 0
    first_char_start = first_line_start = None
    last_char_end = last_line_end = None

    def head_chars() -> int:
        return len(_render_pack_head(cur_heads, budget)[0]) if cur_heads else 0

    def flush() -> None:
        nonlocal cur_lines, body_chars, cur_pspans, page_start, cur_page
        nonlocal block_count, first_char_start, first_line_start
        nonlocal last_char_end, last_line_end, heads_fresh
        if not cur_lines:
            # 空包无输出；cur_heads 跨包保留（长小节拆多包时每个包
            # 都带同一【章 / 节】包头，见 §3.2.1）。
            return
        head, head_overflow = _render_pack_head(cur_heads, budget)
        lines = list(cur_lines)
        if head_overflow:
            # 包头截断的溢出部分转为正文首行（review P0-1，零信息损失）
            lines.insert(0, head_overflow)
        text = head + "\n".join(lines)
        entries = [(0, page_start)] + cur_pspans
        pspans_str = None
        if len(entries) > 1 and entries[0][1] != entries[-1][1]:
            pspans_str = ";".join(f"{off}@{pg}" for off, pg in entries)
        packs.append({
            "text": text,
            "page_start": page_start,
            "page_end": cur_page if cur_page != page_start else None,
            "pspans": pspans_str,
            "char_start": first_char_start,
            "char_end": last_char_end,
            "line_start": first_line_start,
            "line_end": last_line_end,
            "block_count": block_count,
            "heading": head.rstrip("\n") or None,
        })
        # 本包已渲染过包头：此后 cur_heads 为「继承」态，续包 page_start
        # 取首个正文块页（review P0-2）。
        heads_fresh = False
        cur_lines = []
        body_chars = 0
        cur_pspans = []
        page_start = None
        cur_page = None
        block_count = 0
        first_char_start = first_line_start = None
        last_char_end = last_line_end = None

    def append_body(block: Dict[str, Any]) -> None:
        nonlocal body_chars, cur_pspans, page_start, cur_page
        nonlocal block_count, first_char_start, first_line_start
        nonlocal last_char_end, last_line_end
        text = block.get("text") or ""
        if not text:
            return
        page = block.get("page_idx")
        if page_start is None:
            # 新收标题（heads_fresh）→ 包首页取标题页；继承标题（续包）
            # → 取首个正文块页，避免页码被拉回标题页（review P0-2）。
            page_start = (
                head_pages[0]
                if (head_pages and heads_fresh)
                else page
            )
        # 页边界记录：与「上一个有效页」比较。首正文块时 cur_page 尚为
        # None，比较对象退化为 page_start（标题页 ≠ 正文页同样记一条，
        # 走查 chunk#0 的 22@2 即由此产生）。
        prev_page = cur_page if cur_page is not None else page_start
        if page is not None and prev_page is not None and page != prev_page:
            cur_pspans.append((head_chars() + body_chars, page))
        cur_lines.append(text)
        body_chars += len(text) + 1
        cur_page = page
        block_count += 1
        if first_char_start is None and block.get("char_start") is not None:
            first_char_start = block["char_start"]
        if first_line_start is None and block.get("line_start") is not None:
            first_line_start = block["line_start"]
        if block.get("char_end") is not None:
            last_char_end = block["char_end"]
        if block.get("line_end") is not None:
            last_line_end = block["line_end"]

    for block in blocks:
        text = block.get("text") or ""
        if not text:
            continue
        if is_noise_block(text, repeat_index):
            if drop_noise:
                dropped.append((text, "noise"))
                continue
            # DROP_NOISE=false：噪声按正文吸附（零信息损失模式）。
            # 注意 elif 结构：吸附路径必须跳过 heading 判定，否则
            # 页码「1」会被 C5 误吸进包头。
        elif is_heading_like(text, heading_max_len):
            flush()                      # 标题归属「下一包」而不是当前包
            cur_heads = (cur_heads + [text])[-heading_levels:]
            head_pages = (head_pages + [block.get("page_idx")])[-heading_levels:]
            heads_fresh = True           # 新收标题，下一包以标题页计页
            continue
        if body_chars + len(text) + 1 + head_chars() > budget and cur_lines:
            flush()                      # 预算满，先冲刷再装
        append_body(block)

    flush()                              # 收尾

    if carry is not None:
        # 跨硬边界继承的包头状态原地回传（review P1）
        carry["heads"] = cur_heads
        carry["head_pages"] = head_pages
        carry["heads_fresh"] = heads_fresh

    return packs, dropped


def pack_rows(
    rows: List[List[str]],
    budget: int,
    header: Optional[List[str]] = None,
    column_threshold: int = DEFAULT_COLUMN_THRESHOLD,
    caption: Optional[str] = None,
) -> List[Tuple[str, int, int]]:
    """Split table rows into budget-sized chunks with self-describing format.

    Each segment repeats the header and encodes each row as::

        col_name: value | col_name: value | ...

    For tables with few columns (≤ *column_threshold*), uses compact
    Markdown table format instead for better human readability.

    [jonex] §table-grid-v2 L4.1/O3:
      - *caption* adds a one-line context header ``表格明细：【表】标题`` to
        every segment (≤ 8% of the budget, truncated when over-long) — the
        subject-disambiguation header, and the only repeated metadata in the
        body (column list / signature / notes go to ``file_source``);
      - group-aware splitting: when a column keeps the same value across
        consecutive rows (rowspan forward-fill output), segments break at
        group boundaries first; continuation segments inside an over-budget
        group get a ``（续：分组值）`` suffix.

    Returns:
        ``[(segment_text, row_start_idx, row_end_idx), ...]``.
        Indices are 0-based within *rows*, exclusive end.
    """
    if not rows:
        return []

    header = header or []
    n_cols = max(len(r) for r in rows) if rows else 0
    if header:
        n_cols = max(n_cols, len(header))

    # Pad header to n_cols with generic column names
    padded_header = list(header) + [
        f"col_{i}" for i in range(len(header), n_cols)
    ]

    use_markdown = n_cols <= column_threshold

    hl = make_header_block(padded_header, n_cols, use_markdown)

    group_col = _detect_group_column(rows, n_cols)

    segments: List[Tuple[str, int, int]] = []
    i = 0
    n = len(rows)
    first_segment = True
    prev_flushed_group: Optional[str] = None

    while i < n:
        group_val = _group_value(rows[i], group_col)
        continuation = ""
        if (
            caption and group_col is not None and group_val
            and not first_segment and group_val == prev_flushed_group
        ):
            continuation = group_val  # 组内续块（O3）
        cap_text = _render_caption_line(caption, budget, continuation) if caption else ""

        seg_lines: List[str] = []
        seg_len = len(cap_text) + len(hl) + (1 if hl else 0)
        seg_start = i

        while i < n:
            row_str = fmt_row(rows[i], padded_header, n_cols, use_markdown)
            row_len = len(row_str) + 1  # +1 for newline
            g = _group_value(rows[i], group_col)
            if seg_lines and g and group_val and g != group_val:
                break  # O3: 分组边界优先断开（空分组值不参与）
            if seg_len + row_len > budget and seg_lines:
                break
            seg_lines.append(row_str)
            seg_len += row_len
            i += 1

        body = "\n".join(seg_lines)
        seg_text = cap_text + (hl + "\n" + body if hl else body)
        segments.append((seg_text, seg_start, i))
        prev_flushed_group = group_val
        first_segment = False

    return segments


def split_row_by_cells(
    row_text: str,
    budget: int,
    *,
    header_block: str = "",
    caption_line: str = "",
) -> List[Tuple[str, int, int]]:
    """[jonex] §C1-bis: 单行超预算时按单元格边界切分（table-parsing-retrieval-governance-plan.md §19.3）。

    切点取「不超过 budget 的最后一个 `` | ``」，而非精确 budget 处硬切——
    `` | `` 是 fmt_row 两种格式（markdown / self-describing）共用的单元格
    分隔符，也是行内唯一的语义边界。header_block / caption_line 计入每段
    预算，并统一作为前缀附在每个子段上（§19.4：列名有条件补、表标题
    无条件补）。

    仅当单个单元格自身超预算时才退化为字符硬切，并打 WARNING（cells_hard_cut
    计数由调用方通过「非尾段段末不含 `` | ``」特征完成，见 §19.3）。

    Returns:
        ``[(seg_text, cell_start, cell_end), ...]``。
        单元格序号 0-based 右开（与 pack_rows 的 row 区间口径一致，§19.5 的
        file_source 锚点直接使用，不做 1-based 转换）。seg_text 已含
        caption_line + header_block 前缀。
    """
    if not row_text:
        return []

    prefix = caption_line
    if header_block:
        prefix = prefix + header_block + "\n"
    body_budget = max(1, budget - len(prefix))

    n = len(row_text)
    if n <= body_budget:
        return [(prefix + row_text, 0, 1)]

    # 所有 " | " 分隔符的起始位置；cell 总数 = 分隔符数 + 1
    seps: List[int] = []
    hit = row_text.find(" | ")
    while hit != -1:
        seps.append(hit)
        hit = row_text.find(" | ", hit + 1)
    n_cells = len(seps) + 1

    segments: List[Tuple[str, int, int]] = []
    pos = 0
    cell_start = 0
    while pos < n:
        end = pos + body_budget
        if end >= n:
            segments.append((prefix + row_text[pos:], cell_start, n_cells))
            break
        # 找最后一个完整落在 [pos, end] 内的分隔符；切点 = 其后下一格的起点。
        # 切在 seps[k]+3 保证前段以 " | " 结尾、后段从新格开始。
        cut_k = -1
        for k, s in enumerate(seps):
            if s < pos:
                continue
            if s + 3 <= end:
                cut_k = k
            else:
                break
        if cut_k >= 0:
            cut = seps[cut_k] + 3
            segments.append((prefix + row_text[pos:cut], cell_start, cut_k + 1))
            pos = cut
            cell_start = cut_k + 1
            continue
        # 无完整分隔符落在窗口内：某个单元格自身超预算 → 字符硬切（可观测退化）。
        # 前段覆盖「起点落在窗口内」的所有格：m = 第一个不完整分隔符的全局序号，
        # 格 m 的结束边界在窗口外、部分内容在前段，故区间为 [cell_start, m+1)。
        # 下一段从格 m 继续（它还没结束）——硬切使该格出现在两段区间中，
        # 重叠是硬切的本质，正常按边界切时不发生。
        m = len(seps)
        for k, s in enumerate(seps):
            if s >= pos and s + 3 > end:
                m = k
                break
        logger.warning(
            "[jonex] §C1-bis: single cell exceeds body budget "
            "(cells=%d:%d, row_len=%d > budget=%d), "
            "falling back to hard char cut",
            cell_start, cell_start + m + 1, n, body_budget,
        )
        segments.append(
            (prefix + row_text[pos:end], cell_start, cell_start + m + 1)
        )
        pos = end
        cell_start = cell_start + m

    return segments


def get_equation_text_and_format(item: Dict[str, Any]) -> Tuple[str, str]:
    """Read equation content while preserving LaTeX aliases from content lists.

    Field priority follows MinerU first (``text`` + ``text_format``), then
    falls back to ``latex`` and ``equation`` aliases used by other parsers.
    The textual description is intentionally NOT concatenated into the
    equation body: the ``equation_chunk`` template has a separate
    ``enhanced_caption`` slot for that.
    """
    text = str(item.get("text", "") or "").strip()
    latex = str(item.get("latex", "") or "").strip()
    equation = str(item.get("equation", "") or "").strip()
    equation_format = str(item.get("text_format", "") or "").strip()

    if text:
        equation_text = text
    elif latex:
        equation_text = latex
        if not equation_format:
            equation_format = "latex"
    elif equation:
        equation_text = equation
    else:
        equation_text = ""

    return equation_text, equation_format


# ── [jonex] P1-4 结构感知切分已废弃（O5，2026-08-13）──
#
# structure_aware_chunk / _enrich_table_row_text / _classify_block_type /
# _LIST_ENTRY_PATTERNS / _STRUCTURE_AWARE_ENABLED 及 separate_content 中的
# 调用点已删除：该路径不在推送链路上（只被 separate_content 调用，而推送走
# _collect_text_chunks），是死代码；且只认 markdown `#` 标题，认不出 docx/xlsx
# 的普通文本标题。表标题回填能力由 §table-grid-v2 L4.1 的
# `_resolve_table_caption` + pack_rows caption 接管（来源不限 markdown heading）。
# 详见 docs/table-parsing-retrieval-governance-plan.md §11.4 O5 与 JONEX_CHANGES §21。


def separate_content(
    content_list: List[Dict[str, Any]],
) -> Tuple[str, List[Dict[str, Any]]]:
    """
    Separate text content and multimodal content

    For each text block, computes positional metadata:
      - page_idx:   page number from parser (0-based)
      - text_idx:   position index in content_list
      - line_start: starting line in the concatenated full text (1-based)
      - line_end:   ending line in the concatenated full text (1-based)

    Args:
        content_list: Content list from MinerU parsing

    Returns:
        (text_content, multimodal_items): Pure text content and multimodal items list.
        The last element of multimodal_items is a sentinel ``{"_text_meta": [...]}``
        carrying per-block metadata for downstream use.
    """
    text_parts = []
    text_meta_list: List[Dict[str, Any]] = []
    multimodal_items = []
    line_cursor = 1  # 1-based document-level line counter

    for index, item in enumerate(content_list):
        content_type = item.get("type", "text")

        if content_type == "text":
            # Text content — preserve positional metadata
            text = item.get("text", "")
            if text.strip():
                lines_in_block = text.count("\n") + 1
                text_parts.append(text)
                # Record per-block metadata for downstream chunk tracking
                text_meta_list.append({
                    "content": text,
                    "page_idx": item.get("page_idx", 0),
                    "text_idx": index,  # original position in content_list
                    "line_start": line_cursor,
                    "line_end": line_cursor + lines_in_block - 1,
                })
                # Advance cursor: this block's lines + 1 for the "\n\n" join
                # separator between blocks (adds one empty line)
                line_cursor += lines_in_block + 1
        else:
            # Multimodal content (image, table, equation, etc.)
            multimodal_item = dict(item)
            multimodal_item.setdefault("_content_list_index", index)
            multimodal_items.append(multimodal_item)

    # Merge all text content for backward-compatible full-text reference
    text_content = "\n\n".join(text_parts)

    # Attach metadata list to the return value for callers that need it.
    # Stored as an attribute on the string is not Pythonic, so we return
    # the metadata list separately via a sentinel key in multimodal_items.
    # Backward-compatible callers only use text_content and multimodal_items.
    multimodal_items.append({"_text_meta": text_meta_list})

    logger.info("Content separation complete:")
    logger.info(f"  - Text content length: {len(text_content)} characters")
    logger.info(f"  - Text blocks: {len(text_meta_list)}")
    logger.info(f"  - Multimodal items count: {len(multimodal_items) - 1}")

    # Count multimodal types
    modal_types = {}
    for item in multimodal_items:
        modal_type = item.get("type", "unknown")
        modal_types[modal_type] = modal_types.get(modal_type, 0) + 1

    if modal_types:
        logger.info(f"  - Multimodal type distribution: {modal_types}")

    return text_content, multimodal_items


def extract_text_metadata(multimodal_items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Extract text block metadata hidden in multimodal_items by separate_content.

    Returns:
        List of dicts with keys: content, page_idx, text_idx
    """
    for item in multimodal_items:
        if "_text_meta" in item:
            return item["_text_meta"]
    return []


def encode_image_to_base64(image_path: str) -> str:
    """
    Encode image file to base64 string

    Args:
        image_path: Path to the image file

    Returns:
        str: Base64 encoded string, empty string if encoding fails
    """
    try:
        with open(image_path, "rb") as image_file:
            encoded_string = base64.b64encode(image_file.read()).decode("utf-8")
        return encoded_string
    except Exception as e:
        logger.error(f"Failed to encode image {image_path}: {e}")
        return ""


def validate_image_file(image_path: str, max_size_mb: int = 50) -> bool:
    """
    Validate if a file is a valid image file

    Args:
        image_path: Path to the image file
        max_size_mb: Maximum file size in MB

    Returns:
        bool: True if valid, False otherwise
    """
    try:
        path = Path(image_path)

        logger.debug(f"Validating image path: {image_path}")
        logger.debug(f"Resolved path object: {path}")
        logger.debug(f"Path exists check: {path.exists()}")

        # Check if file exists and is not a symlink (for security)
        if not path.exists():
            logger.warning(f"Image file not found: {image_path}")
            return False

        if path.is_symlink():
            logger.warning(f"Blocking symlink for security: {image_path}")
            return False

        # Check file extension
        image_extensions = [
            ".jpg",
            ".jpeg",
            ".png",
            ".gif",
            ".bmp",
            ".webp",
            ".tiff",
            ".tif",
        ]

        path_lower = str(path).lower()
        has_valid_extension = any(path_lower.endswith(ext) for ext in image_extensions)
        logger.debug(
            f"File extension check - path: {path_lower}, valid: {has_valid_extension}"
        )

        if not has_valid_extension:
            logger.warning(f"File does not appear to be an image: {image_path}")
            return False

        # Check file size
        file_size = path.stat().st_size
        max_size = max_size_mb * 1024 * 1024
        logger.debug(
            f"File size check - size: {file_size} bytes, max: {max_size} bytes"
        )

        if file_size > max_size:
            logger.warning(f"Image file too large ({file_size} bytes): {image_path}")
            return False

        logger.debug(f"Image validation successful: {image_path}")
        return True

    except Exception as e:
        logger.error(f"Error validating image file {image_path}: {e}")
        return False


async def insert_text_content(
    lightrag,
    input: str | list[str] | list[dict],
    file_paths: str | list[str] | None = None,
    split_by_character: str | None = None,
    split_by_character_only: bool = False,
    ids: str | list[str] | None = None,
):
    """
    Insert text content into LightRAG with optional per-chunk metadata.

    When *input* is a ``list[dict]``, each dict must have a ``"content"`` key.
    Extra keys (``page_idx``, ``text_idx``, etc.) are forwarded as chunk metadata
    via ``lightrag.ainsert_custom_chunks``, bypassing LightRAG's internal
    token-based splitting.  This preserves positional information through the
    ingestion pipeline.

    When *input* is a plain ``str`` or ``list[str]``, behaviour is identical to
    the previous implementation (delegates to ``lightrag.ainsert``).

    Args:
        lightrag: LightRAG instance.
        input: Text to insert — plain string(s) or pre-chunked metadata dicts.
        file_paths: File path(s) for reference citation.
        split_by_character: Character to split on (plain-text mode only).
        split_by_character_only: Split by character only (plain-text mode only).
        ids: Document ID(s).
    """
    logger.info("Starting text content insertion into LightRAG...")

    # ── Metadata-aware path: list[dict] → ainsert_custom_chunks ─────
    if isinstance(input, list) and len(input) > 0 and isinstance(input[0], dict):
        # Build full text from all chunks for document storage
        full_text = "\n\n".join(
            item.get("content", "") if isinstance(item, dict) else str(item)
            for item in input
        )
        # Normalize file_paths to a single string for the doc entry
        fp = file_paths
        if isinstance(fp, list):
            fp = fp[0] if fp else ""
        fp = fp or ""

        doc_id = None
        if ids:
            doc_id = ids if isinstance(ids, str) else ids[0] if ids else None

        await lightrag.ainsert_custom_chunks(
            full_text=full_text,
            text_chunks=input,
            doc_id=doc_id,
            file_path=fp,
        )
        logger.info(
            f"Text content insertion complete "
            f"({len(input)} chunks with metadata)"
        )
        return

    # ── Plain-text path: str | list[str] → ainsert (backward compat) ─
    await lightrag.ainsert(
        input=input,
        file_paths=file_paths,
        split_by_character=split_by_character,
        split_by_character_only=split_by_character_only,
        ids=ids,
    )

    logger.info("Text content insertion complete")


async def insert_text_content_with_multimodal_content(
    lightrag,
    input: str | list[str],
    multimodal_content: list[dict[str, any]] | None = None,
    split_by_character: str | None = None,
    split_by_character_only: bool = False,
    ids: str | list[str] | None = None,
    file_paths: str | list[str] | None = None,
    scheme_name: str | None = None,
):
    """
    Insert pure text content into LightRAG

    Args:
        lightrag: LightRAG instance
        input: Single document string or list of document strings
        multimodal_content: Multimodal content list (optional)
        split_by_character: if split_by_character is not None, split the string by character, if chunk longer than
        chunk_token_size, it will be split again by token size.
        split_by_character_only: if split_by_character_only is True, split the string by character only, when
        split_by_character is None, this parameter is ignored.
        ids: single string of the document ID or list of unique document IDs, if not provided, MD5 hash IDs will be generated
        file_paths: single string of the file path or list of file paths, used for citation
        scheme_name: scheme name (optional)
    """
    logger.info("Starting text content insertion into LightRAG...")

    insert_kwargs = {
        "input": input,
        "file_paths": file_paths,
        "split_by_character": split_by_character,
        "split_by_character_only": split_by_character_only,
        "ids": ids,
    }

    try:
        insert_signature = inspect.signature(lightrag.ainsert)
        supported_params = insert_signature.parameters
        accepts_any_kwargs = any(
            parameter.kind == inspect.Parameter.VAR_KEYWORD
            for parameter in supported_params.values()
        )
    except (TypeError, ValueError):
        supported_params = {}
        accepts_any_kwargs = True

    if multimodal_content is not None and (
        accepts_any_kwargs or "multimodal_content" in supported_params
    ):
        insert_kwargs["multimodal_content"] = multimodal_content
    elif multimodal_content is not None:
        logger.warning(
            "LightRAG ainsert() does not accept multimodal_content; "
            "retrying with text-only insertion so doc_status is still created"
        )

    if scheme_name is not None and (
        accepts_any_kwargs or "scheme_name" in supported_params
    ):
        insert_kwargs["scheme_name"] = scheme_name
    elif scheme_name is not None:
        logger.warning(
            "LightRAG ainsert() does not accept scheme_name; "
            "continuing without it for compatibility"
        )

    await lightrag.ainsert(**insert_kwargs)

    logger.info("Text content insertion complete")


_PROCESSOR_KEY_MAP: Dict[str, str] = {
    "image": "image",
    "table": "table",
    "equation": "equation",
    "audio": "audio",
    "video": "video",
}


def get_processor_for_type(modal_processors: Dict[str, Any], content_type: str):
    """
    Get appropriate processor based on content type using registry.

    Args:
        modal_processors: Dictionary of available processors
        content_type: Content type

    Returns:
        Corresponding processor instance
    """
    key = _PROCESSOR_KEY_MAP.get(content_type)
    if key:
        proc = modal_processors.get(key)
        if proc is not None:
            return proc
    return modal_processors.get("generic")


def get_processor_supports(proc_type: str) -> List[str]:
    """Get processor supported features"""
    supports_map = {
        "image": [
            "Image content analysis",
            "Visual understanding",
            "Image description generation",
            "Image entity extraction",
        ],
        "table": [
            "Table structure analysis",
            "Data statistics",
            "Trend identification",
            "Table entity extraction",
        ],
        "equation": [
            "Mathematical formula parsing",
            "Variable identification",
            "Formula meaning explanation",
            "Formula entity extraction",
        ],
        "audio": [
            "Audio transcription via ASR",
            "Transcript segment chunking",
            "Audio content summarization",
            "Audio entity extraction with time metadata",
        ],
        "generic": [
            "General content analysis",
            "Structured processing",
            "Entity extraction",
        ],
    }
    return supports_map.get(proc_type, ["Basic processing"])


# ── Parser kwarg filtering ───────────────────────────────────────────

_PARSER_KWARG_KEYS = frozenset({
    "lang", "device", "start_page", "end_page",
    "formula", "table", "backend", "source",
})


def filter_parser_kwargs(kwargs):
    """Return only kwargs that are relevant parser configuration keys."""
    return {k: v for k, v in kwargs.items() if k in _PARSER_KWARG_KEYS}
