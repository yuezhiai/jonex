"""Ordered structural packing shared by Direct and MinerU.

The default counter is an explicitly named estimate, not a model tokenizer.
Offsets address decoded characters; snapshot_text owns each source character
exactly once, whereas repeated table headers belong to the evidence surface.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Callable, Mapping

from jonex_wiki.domain.engine.compile.source_prep.blocks import ParseBlock
from jonex_wiki.kernel.runtime_env import current_env

STRATEGY = "structure-pack-2"
# prose(非表格/非记录)块的字符窗口上限。**不是 env 旋钮**:没有独立的
# ``JONEX_WIKI_*`` 入口,只由本常量决定;生效值还要与整包字符硬上限取小
# (见 ``PackingOptions.text_window``),所以它不可能突破 ``max_chars``。
# 取 20000 的理由:估算口径 ASCII 4 字符≈1 token,而 prose 的 token 上限是
# 5000,所以只要 token 预算兜住,字符数最多就是 4×5000=20000。
# 它是**兜底**而非主约束——当前取值下先撞 token 预算,本窗口不参与;只有把
# token 预算调高到 5000 以上,它才会重新成为生效的上限。
PROSE_WINDOW_CHARS = 20_000
# 记录/表格类单元不参与字符窗口裁剪:它们要么整体成块,要么按行/记录切。
WINDOW_EXEMPT_KINDS = frozenset({"record", "table", "html_table"})


def estimate_tokens(text: str) -> int:
    # Non-ASCII characters count individually; ASCII runs use ceil(chars/4).
    return sum((len(part) + 3) // 4 if part.isascii() else len(part)
               for part in re.findall(r"[\x00-\x7f]+|[^\x00-\x7f]+", text))


@dataclass(frozen=True)
class PackingOptions:
    small_tokens: int = 4000
    target_tokens: int = 4000
    # prose 的 token 上限。取 5000 而不是 6000:估算口径是中英文中立的
    # (中文 1 字符≈1 token,ASCII 4 字符≈1 token),上限**用 token 表达**,
    # 中英文才落在同一个上限上——两者都是 5000 est-token,区别只是
    # 中文≈5000 字符、英文≈20000 字符。字符窗口见 PROSE_WINDOW_CHARS。
    max_tokens: int = 5000
    table_target_tokens: int = 2000
    # 须 ≤ max_tokens(见 __post_init__ 的表格预算排序断言),故随 max_tokens 同步下调。
    row_max_tokens: int = 5000
    max_chars: int = 32000
    context_tokens: int = 256
    previous_tokens: int = 128

    @property
    def text_window(self) -> int:
        """prose 块的生效字符窗口:不得突破整包字符硬上限。

        ``max_chars`` 是整包硬上限(``fits()`` 判据,含表格);本属性只给 prose
        用。当前 ``PROSE_WINDOW_CHARS``(20000)< ``max_chars``(32000),所以
        取值就是常量本身;取 min 是为了让"窗口不得越过整包上限"这条不变量在
        ``max_chars`` 被下调时依然成立。
        """
        return min(self.max_chars, PROSE_WINDOW_CHARS)

    def __post_init__(self):
        if any(value <= 0 for value in asdict(self).values()):
            raise ValueError("packing budgets must be positive")
        if not self.small_tokens <= self.max_tokens or not self.target_tokens <= self.max_tokens:
            raise ValueError("small/target budget exceeds passage limit")
        if not self.table_target_tokens <= self.row_max_tokens <= self.max_tokens:
            raise ValueError("invalid table budget ordering")

    @classmethod
    def from_env(cls, environment: Mapping[str, str] | None = None):
        env = environment if environment is not None else current_env()
        values = {key: int(env[f"JONEX_WIKI_PACK_{key.upper()}"])
                  for key in cls.__dataclass_fields__
                  if f"JONEX_WIKI_PACK_{key.upper()}" in env}
        return cls(**values)

    @classmethod
    def from_settings(cls, settings):
        """Read the flat ``pack_*`` Settings fields (配置唯一权威是 Settings)。"""
        return cls(
            small_tokens=settings.pack_small_tokens,
            target_tokens=settings.pack_target_tokens,
            max_tokens=settings.pack_max_tokens,
            table_target_tokens=settings.pack_table_target_tokens,
            row_max_tokens=settings.pack_row_max_tokens,
            max_chars=settings.pack_max_chars,
            context_tokens=settings.pack_context_tokens,
            previous_tokens=settings.pack_previous_tokens,
        )


def split_table_row(line: str) -> list[str]:
    value = line.strip()
    if value.startswith("|"):
        value = value[1:]
    if value.endswith("|") and len(re.search(r"\\*$", value[:-1]).group()) % 2 == 0:
        value = value[:-1]
    cells, start = [], 0
    for match in re.finditer(r"\|", value):
        before = value[:match.start()]
        if len(re.search(r"\\*$", before).group()) % 2 == 0:
            cells.append(value[start:match.start()].strip().replace(r"\|", "|"))
            start = match.end()
    cells.append(value[start:].strip().replace(r"\|", "|"))
    return cells


def is_separator_row(cells) -> bool:
    return bool(cells) and all(re.fullmatch(r":?-{3,}:?", cell.replace(" ", "")) for cell in cells)


def markdown_header_line_count(lines: list[str]) -> int:
    """Repeat only the Markdown header and its explicit separator.

    Data words, repeated labels and numeric shapes do not prove another
    header tier. Native and HTML consumers retain their own structural
    metadata; a plain pipe grid cannot reconstruct it from cell values.
    """
    if len(lines) >= 2 and is_separator_row(split_table_row(lines[1])):
        return 2
    return 0


@dataclass(frozen=True)
class Unit:
    start: int
    end: int
    kind: str


def identify_units(text: str) -> list[Unit]:
    lines = text.splitlines(keepends=True)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    units, i = [], 0
    while i < len(lines):
        start = i
        fence = re.match(r"^ {0,3}(`{3,}|~{3,})", lines[i])
        kind = "text"
        if fence:
            marker = fence.group(1)
            i += 1
            while i < len(lines):
                close = re.match(r"^ {0,3}" + re.escape(marker[0]) + "{" + str(len(marker)) + r",}\s*$", lines[i])
                i += 1
                if close:
                    break
            kind = "code"
        elif re.search(r"<table\b", lines[i], re.IGNORECASE):
            closed = re.search(r"</table\s*>", lines[i], re.IGNORECASE)
            i += 1
            while not closed and i < len(lines):
                closed = re.search(r"</table\s*>", lines[i], re.IGNORECASE)
                i += 1
                if closed:
                    break
            kind = "html_table"
        elif i + 1 < len(lines) and "|" in lines[i] and is_separator_row(split_table_row(lines[i + 1])):
            i += 2
            while i < len(lines) and lines[i].strip() and "|" in lines[i]:
                i += 1
            kind = "table"
        else:
            i += 1
            while i < len(lines):
                if not lines[i - 1].strip() or re.match(r"^ {0,3}(#{1,6}\s|`{3,}|~{3,}|[-+*]\s|\d+[.)]\s)", lines[i]):
                    break
                if re.search(r"<table\b", lines[i], re.IGNORECASE):
                    break
                if i + 1 < len(lines) and "|" in lines[i] and is_separator_row(split_table_row(lines[i + 1])):
                    break
                i += 1
        units.append(Unit(offsets[start], offsets[i], kind))
    return units


class PackingError(ValueError):
    """The original artifact is retained; no completed parse may be published."""


def pack_text(text: str, *, prefix: str, options: PackingOptions | None = None,
              counter: Callable[[str], int] = estimate_tokens,
              counter_name: str = "unicode-ascii-estimate-v1",
              units: list[Unit] | None = None,
              inspection=None, report=None) -> list[ParseBlock]:
    """结构打包。

    ``inspection``/``report`` 是**可选**的编译前体检（``source_prep.inspect`` +
    ``source_prep.screening``）：开了体检时，注定装不下的单元会被摘出编译面并记进
    ``report``（原文由 ``source_archive`` 留档），其余单元照常打包；关了（默认）就完全
    是本文档正文描述的既有语义——超限即 ``PackingError``。

    体检只处理"注定装不下"的单元；引擎不变量被破坏（字符级装不下、空白块无法再平衡等）
    依旧是硬失败，不受 ``inspection`` 影响。
    """
    config = options or PackingOptions.from_env()
    units = identify_units(text) if units is None else units
    screened = False
    if inspection is not None and getattr(inspection, "enabled", False):
        # 延迟导入：筛查要用本模块的预算判据，模块级导入会形成循环。
        from jonex_wiki.domain.engine.compile.source_prep.screening import screen_units
        original_units = units
        units = screen_units(text, units, config, inspection, report, counter=counter)
        screened = units is not original_units
    blocks: list[ParseBlock] = []
    policy = {"strategy": STRATEGY, "counter": counter_name, "budgets": asdict(config),
              # 窗口不是 pack_* 字段(没有 env 入口),单独记进 policy 让 bundle 自证口径
              "prose_window_chars": PROSE_WINDOW_CHARS}
    # prose 块的字符窗口(≤ max_chars)
    window = config.text_window

    def fits(value, budget):
        return len(value) <= config.max_chars and counter(value) <= budget

    def emit(value, start, end, kind="text", **meta):
        blocks.append(ParseBlock(f"{prefix}:block:{len(blocks):08d}", len(blocks), value,
            block_type=kind, metadata={"char_start": start, "char_end": end,
            "snapshot_text": text[start:end], "packing": policy, **meta}))

    # 小文档捷径:整篇一次成块。**体检摘掉过单元时不做**(``screened``)——捷径
    # 发射的是整篇 ``text``,会把被隔离的切片又带回编译面。
    if (not screened and text.strip() and len(text) <= window and fits(text, config.small_tokens)
            and all(u.kind != "table" or fits(text[u.start:u.end], config.table_target_tokens) for u in units)):
        emit(text, 0, len(text))
        return blocks

    pending_start = pending_end = None

    def flush():
        nonlocal pending_start, pending_end
        if pending_start is not None:
            emit(text[pending_start:pending_end], pending_start, pending_end)
        pending_start = pending_end = None

    for unit_index, unit in enumerate(units):
        value = text[unit.start:unit.end]
        if unit.kind == "html_table" and not fits(value, config.table_target_tokens):
            flush()
            row_matches = list(re.finditer(
                r"<tr\b[^>]*>.*?</tr\s*>", value,
                re.IGNORECASE | re.DOTALL,
            ))
            if not row_matches:
                raise PackingError(
                    f"HTML table has no complete rows at {unit.start}:{unit.end}"
                )
            prefix_text = value[:row_matches[0].start()]
            suffix_text = value[row_matches[-1].end():]
            header = prefix_text + row_matches[0].group(0)
            if not fits(header + suffix_text, config.row_max_tokens):
                raise PackingError(f"HTML table header exceeds limit at {unit.start}")
            group = ""
            group_start = unit.start + row_matches[0].end()
            group_row = 0
            first = True
            data_rows = row_matches[1:]
            for row_index, match in enumerate(data_rows):
                row = match.group(0)
                if not fits(header + row + suffix_text, config.row_max_tokens):
                    raise PackingError(
                        f"HTML table row exceeds limit at characters "
                        f"{unit.start + match.start()}:{unit.start + match.end()}"
                    )
                if group and not fits(
                    header + group + row + suffix_text,
                    config.table_target_tokens,
                ):
                    emit(
                        header + group + suffix_text,
                        unit.start if first else group_start,
                        unit.start + match.start(),
                        "html_table",
                        table_id=f"{prefix}:table:{unit.start}",
                        row_range=[group_row, row_index],
                        header_source=[unit.start, unit.start + row_matches[0].end()],
                        table_partial=True,
                    )
                    first = False
                    group = ""
                    group_start = unit.start + match.start()
                    group_row = row_index
                group += row
            if group:
                emit(
                    header + group + suffix_text,
                    unit.start if first else group_start,
                    unit.end,
                    "html_table",
                    table_id=f"{prefix}:table:{unit.start}",
                    row_range=[group_row, len(data_rows)],
                    header_source=[unit.start, unit.start + row_matches[0].end()],
                    table_partial=not first,
                )
            else:
                emit(value, unit.start, unit.end, "html_table")
            continue
        if unit.kind == "table" and not fits(value, config.table_target_tokens):
            flush()
            lines = value.splitlines(keepends=True)
            header_lines = markdown_header_line_count(lines)
            raw_header = "".join(lines[:header_lines])
            # Repeated qualifiers are copied from exact adjacent source units.
            qualifiers = ""
            qualifier_sources = []
            if unit_index:
                previous = units[unit_index - 1]
                candidate = text[previous.start:previous.end]
                if previous.kind == "text" and counter(candidate) <= config.context_tokens:
                    qualifiers = candidate + ("\n" if not candidate.endswith("\n") else "")
                    qualifier_sources.append([previous.start, previous.end])
            suffix = ""
            following_index = unit_index + 1
            while following_index < len(units) and not text[units[following_index].start:units[following_index].end].strip():
                following_index += 1
            if following_index < len(units):
                following = units[following_index]
                candidate = text[following.start:following.end]
                if re.match(r"\s*(?:\[\^|注[:：]|备注[:：]|Note[:：]|\*\s)", candidate):
                    suffix = candidate
                    qualifier_sources.append([following.start, following.end])
            header = qualifiers + raw_header
            row_start = unit.start + len(raw_header)
            group, group_start, first, row_index = "", row_start, True, 0
            group_row = 0
            for row in lines[header_lines:]:
                if len(split_table_row(row)) != len(split_table_row(lines[0])):
                    raise PackingError(f"table column count mismatch at {row_start}")
                if not fits(header + row + suffix, config.row_max_tokens):
                    raise PackingError(f"table row exceeds limit at characters {row_start}:{row_start + len(row)}")
                if group and not fits(header + group + row + suffix, config.table_target_tokens):
                    emit(header + group + suffix, unit.start if first else group_start, row_start, "table",
                         table_id=f"{prefix}:table:{unit.start}", row_range=[group_row, row_index],
                         header_source=[unit.start, unit.start + len(raw_header)], qualifier_sources=qualifier_sources, table_partial=True)
                    first = False
                    group, group_start, group_row = "", row_start, row_index
                group += row
                row_start += len(row)
                row_index += 1
            if group:
                emit(header + group + suffix, unit.start if first else group_start, unit.end, "table",
                     table_id=f"{prefix}:table:{unit.start}", row_range=[group_row, row_index],
                     header_source=[unit.start, unit.start + len(raw_header)], qualifier_sources=qualifier_sources, table_partial=not first)
            elif header:
                if not fits(header, config.row_max_tokens):
                    raise PackingError(f"table header exceeds limit at {unit.start}")
                emit(header, unit.start, unit.end, "table")
            continue
        # prose 超出字符窗口时同样走硬切分:否则"窗口恰好卡在 token 预算之上"的
        # ASCII/混合文本会整块输出,窗口形同虚设(表格/记录不参与,见 WINDOW_EXEMPT_KINDS)。
        # 当前 token 上限 5000、窗口 20000 时本项恒不触发(见 PROSE_WINDOW_CHARS);
        # 保留它是留给"token 预算被调高到 5000 以上"情形的护栏。
        over_window = unit.kind not in WINDOW_EXEMPT_KINDS and len(value) > window
        if not fits(value, config.max_tokens) or over_window:
            flush()
            if unit.kind in {"record", "table", "html_table"}:
                raise PackingError(f"complete {unit.kind} exceeds limit at {unit.start}:{unit.end}")
            start = unit.start
            while start < unit.end:
                lo, hi = start + 1, min(unit.end, start + window)
                while lo <= hi:
                    middle = (lo + hi) // 2
                    if fits(text[start:middle], config.max_tokens):
                        lo = middle + 1
                    else:
                        hi = middle - 1
                end = hi
                if end <= start:
                    raise PackingError(f"counter cannot fit character at {start}")
                if end < unit.end:
                    boundary = max(text.rfind("\n", start, end), text.rfind(" ", start, end))
                    if boundary > start + (end - start) // 2:
                        end = boundary + 1
                previous = text[unit.start:start]
                while previous and counter(previous) > min(config.previous_tokens, config.context_tokens):
                    previous = previous[max(1, len(previous) // 4):]
                emit(text[start:end], start, end, unit.kind, forced_split=True,
                     contextual_text=previous)
                start = end
            continue
        if pending_start is not None and (
                # 体检摘掉过单元时,单元之间会留下缺口:合并**不得跨过缺口**,
                # 否则被隔离的原文会随相邻块的合并重新进入编译面。
                # 正常路径下 units 连续覆盖全文,本判据恒为假(纯护栏)。
                pending_end != unit.start
                or not fits(text[pending_start:unit.end], config.max_tokens)
                or len(text[pending_start:unit.end]) > window):
            # 合并也不得越过字符窗口(否则相邻短块会把窗口一点点撑破)
            flush()
        if pending_start is None:
            pending_start = unit.start
        pending_end = unit.end
        if counter(text[pending_start:pending_end]) >= config.target_tokens:
            flush()
    flush()
    # Rebalance a whitespace tail against an adjacent ordinary source slice.
    # Never append past the hard limit, and never mutate repeated table evidence.
    for index in range(len(blocks) - 1, -1, -1):
        blank = blocks[index]
        if blank.text.strip() or len(blocks) == 1:
            continue
        neighbor = blocks[index - 1] if index else blocks[1]
        # 只合并**字符相邻**的块:体检摘掉单元后块之间可能有缺口,跨缺口合并
        # 会同时污染正文与 snapshot_text(证据切片)。正常路径下块本就连绵相接。
        # 相邻方向取决于合并方向:index>0 时邻居在前(char_end 对 char_start),
        # index==0 时邻居在后(char_start 对 char_end)。
        adjacent = (neighbor.metadata["char_end"] == blank.metadata["char_start"] if index
                    else neighbor.metadata["char_start"] == blank.metadata["char_end"])
        if not adjacent:
            continue
        left, right = (neighbor, blank) if index else (blank, neighbor)
        value = left.text + right.text
        start, end = left.metadata["char_start"], right.metadata["char_end"]
        # 表格按整包上限判;prose 还必须守住字符窗口(见 text_window/PROSE_WINDOW_CHARS)
        merge_budget = config.max_chars if neighbor.block_type == "table" else window
        if fits(value, config.max_tokens) and len(value) <= merge_budget:
            neighbor.text = value
            neighbor.metadata.update(char_start=start, char_end=end, snapshot_text=text[start:end])
            blocks.pop(index)
        else:
            if neighbor.block_type == "table":
                raise PackingError("isolated whitespace cannot fit beside table within budget")
            # Move a substantive character with the whitespace to keep both calls meaningful.
            split = len(left.text) - 1 if index else len(left.text) + 1
            while split > 0 and split < len(value) and (not value[:split].strip() or not value[split:].strip()):
                split += -1 if index else 1
            if not (value[:split].strip() and value[split:].strip()
                    and fits(value[:split], config.max_tokens) and fits(value[split:], config.max_tokens)):
                raise PackingError(f"whitespace run exceeds packing capacity at {start}:{end}")
            left.text, right.text = value[:split], value[split:]
            left.metadata.update(char_start=start, char_end=start + split, snapshot_text=text[start:start + split])
            right.metadata.update(char_start=start + split, char_end=end, snapshot_text=text[start + split:end])
    for position, block in enumerate(blocks):
        block.position = position
    # 没有文档级闸门:摘除判据只有一条(不摘就崩打包器),比例不需要阈值兜底;
    # 真要"整体不可信就整份失败",那是编译侧 max_passage_failure_rate 的职责。
    return blocks
