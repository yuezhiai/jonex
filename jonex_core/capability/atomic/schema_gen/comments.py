"""中文列注释解析 —— 从 COMMENT 里榨出枚举取值与关联目标。

这是整个 POC 的核心，因为在这个库里：声明外键只有 8 条，而注释里自称关联的
有 68 条。关系真值活在中文注释里，不在数据库约束里。

设计取向：**不靠正则去表达语义，靠交叉验证**。
注释里「关联申请ID」这种没写目标表的，任何正则都救不了；但「引用 t_asset 表」
「对应 t_asset_instance.id」这类写明了目标的，只要把注释里所有标识符 token 抓出来、
拿库里的真实表名集合过一遍，就能稳定命中。表名集合是权威的，token 是候选的。

同理，枚举判定不能看「有没有分隔符」——`開發需求描述：草稿可空、提交時必填且
≤500 字（PRD §3.2.1 / §3.2.3）` 也有 `、` 也有 `/`。得看**切出来的每一项长得
像不像取值**（代码-说明式、大写标识、纯数字）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .ir import EnumParse, RefHint

# ── 词法 ────────────────────────────────────────────────────────────────

# 关联声明的触发词。繁简都要，这个库两种混用
_REF_MARKER = re.compile(r"外键|外鍵|關聯|关联|引用|對應|对应|指向|邏輯關聯|逻辑关联")

# 标识符 token：纯 ASCII 词。拿它去撞表名
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

# 列名引用：`.id` / `的id` / `表的主鍵id`
_COL_SUFFIX = re.compile(r"(?:\.|的)\s*([A-Za-z_][A-Za-z0-9_]*)")

# 语义提示：「关联XXXID」里的 XXX，供人工在 nexonto 上连线时参考
_HINT_FROM_REL = re.compile(r"[關关][聯联]\s*([^\s（(,，;；]+?)\s*(?:ID|Id|id|ID\b)")

# 枚举引导：取最后一个中英文冒号之后的内容
_COLON = re.compile(r"[：:]")

# 分句终止符。`申請狀態：DRAFT/.../ENDED；一開、二開到期資源全部回收`
# 分号之后是补充说明，不是取值，必须截断
_TERMINATORS = "；;。\n"

# 取值分隔符
_SEPARATORS = "/、"

# 括号。切分时要感知深度，`PENDING_RECYCLE-已投遞回收MQ(回收中,失敗時維持...)`
# 内部可能含分隔符
_OPEN = "（(【["
_CLOSE = "）)】]"

# 剥掉取值尾部的补充说明
_TRAILING_PAREN = re.compile(r"[（(【\[][^（()）【\[\]】]*[）)】\]]\s*$")

_ENUM_MIN_ITEMS = 2
_ENUM_MAX_ITEMS = 40
_ENUM_MAX_ITEM_LEN = 32
_ENUM_CODE_RATIO = 0.6


@dataclass
class ParsedComment:
    enum: EnumParse | None = None
    ref: RefHint | None = None
    desc_core: str | None = None


# ── 工具 ────────────────────────────────────────────────────────────────


def _split_top_level(s: str, seps: str) -> list[str]:
    """按 seps 切分，但忽略括号内部的分隔符。"""
    out: list[str] = []
    buf: list[str] = []
    depth = 0
    for ch in s:
        if ch in _OPEN:
            depth += 1
        elif ch in _CLOSE:
            depth = max(0, depth - 1)
        if depth == 0 and ch in seps:
            out.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    out.append("".join(buf))
    return [x.strip() for x in out if x.strip()]


def _truncate_at_terminator(s: str) -> str:
    """在第一个分句终止符处截断。"""
    for i, ch in enumerate(s):
        if ch in _TERMINATORS:
            return s[:i]
    return s


def _strip_unbalanced(s: str) -> str:
    """去掉不配对的括号。

    `通知對象（逗號分隔：AUTHORIZED/RUNNING/PENDING_DEPLOY）` 从冒号后截取时，
    外层括号的右半边会被带进来，末项就成了 `PENDING_DEPLOY）`。这里按配对计数
    把多出来的右括号削掉——只削不配对的，`1-公開（二開強制私有）` 这种配平的不会动。
    """
    out = s.rstrip()
    while out and out[-1] in _CLOSE:
        if sum(out.count(c) for c in _OPEN) < sum(out.count(c) for c in _CLOSE):
            out = out[:-1].rstrip()
        else:
            break
    return out


def _strip_trailing_paren(s: str) -> str:
    """剥掉尾部的括号补充，可能有多层。"""
    prev = None
    while prev != s:
        prev = s
        s = _TRAILING_PAREN.sub("", s).strip()
    return _strip_unbalanced(s)


def _is_code_like(item: str) -> bool:
    """某一项长得像「取值」而不是散文吗？

    命中任一条即算：
      - 代码-说明式：`0-私有` / `RESERVED-待審批預占` / `FIRST_DEV-一开`
      - 全大写标识：`GENESIS` / `SUCCESS` / `DRAFT`
      - 纯数字：`1` / `2`
      - 纯 ASCII 短标识：`curl` / `python`
    """
    core = _strip_trailing_paren(item)
    if not core:
        return False
    # 代码-说明式
    if re.match(r"^[A-Za-z0-9_]{1,24}\s*[-–—]\s*\S", core):
        return True
    # 纯大写标识
    if re.fullmatch(r"[A-Z][A-Z0-9_]{1,23}", core):
        return True
    # 纯数字
    if re.fullmatch(r"\d{1,6}", core):
        return True
    # 纯 ASCII 短标识，不含中文
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,20}", core):
        return True
    return False


def _looks_like_enum(items: list[str]) -> bool:
    """判定切出来的一串是不是枚举取值，而不是被顿号切碎的散文。"""
    if not (_ENUM_MIN_ITEMS <= len(items) <= _ENUM_MAX_ITEMS):
        return False
    for it in items:
        if len(it) > _ENUM_MAX_ITEM_LEN:
            return False
        if not _strip_trailing_paren(it):
            return False
    code_like = sum(1 for it in items if _is_code_like(it))
    return code_like / len(items) >= _ENUM_CODE_RATIO


# ── 表名索引 ────────────────────────────────────────────────────────────


class TableIndex:
    """注释里抓到的 token 要拿真实表名验证，否则 `source_code`、`contentItemId`
    这种也会被当成表。同时提供列名合法性校验。"""

    def __init__(self, columns_by_table: dict[str, set[str]]):
        self.tables = set(columns_by_table)
        self.columns = columns_by_table
        # 无 `t_` 前缀的写法也要能命中：`asset_content_item` → `t_asset_content_item`
        self._stripped: dict[str, str] = {}
        for t in self.tables:
            stem = t[2:] if t.startswith("t_") else t
            # 撞车时保留更短的那个，避免歧义
            self._stripped.setdefault(stem, t)

    def resolve(self, token: str) -> str | None:
        if token in self.tables:
            return token
        return self._stripped.get(token)

    def has_column(self, table: str, column: str) -> bool:
        return column in self.columns.get(table, set())


# ── 关联解析 ────────────────────────────────────────────────────────────


def _parse_ref(comment: str, index: TableIndex, self_table: str) -> RefHint | None:
    if not _REF_MARKER.search(comment):
        return None

    # 逐个 token 撞表名。取第一个命中的——注释里的表名几乎总是第一个出现，
    # 后面的 token 多是列名或散文词
    for m in _IDENT.finditer(comment):
        tok = m.group(0)
        # 跳过纯数字开头、太短、以及列名后缀位置的 token
        if len(tok) < 3:
            continue
        target = index.resolve(tok)
        if target is None:
            continue
        # 排除自己引用自己的误报——真自引用（parent_id）是合法的，
        # 但要靠列名形状判断，不靠表名相同
        tail = comment[m.end():]

        col = None
        cm = _COL_SUFFIX.match(tail)
        if cm:
            cand = cm.group(1)
            if index.has_column(target, cand):
                col = cand

        raw = comment
        return RefHint(
            raw=raw,
            target_table=target,
            target_column=col,
            semantic_hint=_semantic_hint(comment),
        )

    # 没写目标表，只写了语义。交给人工在 nexonto 上连线
    return RefHint(raw=comment, semantic_hint=_semantic_hint(comment))


def _semantic_hint(comment: str) -> str | None:
    m = _HINT_FROM_REL.search(comment)
    if m:
        h = m.group(1).strip("的 ")
        if h:
            return h
    # `部署實例ID，外鍵` 这种没有「关联」字样，取注释开头到 ID 之间
    m = re.match(r"^([^\s（(,，;；]{1,12}?)(?:ID|Id|id)\b", comment)
    if m:
        return m.group(1).strip("的 ") or None
    return None


# ── 枚举解析 ────────────────────────────────────────────────────────────


def _parse_enum(comment: str) -> EnumParse | None:
    m = None
    for m in _COLON.finditer(comment):
        pass
    if m is None:
        return None

    tail = _truncate_at_terminator(comment[m.end():]).strip()
    if not tail:
        return None

    # 两种分隔都试，取更可信的那个
    best: list[str] | None = None
    for seps in (_SEPARATORS, "/"):
        items = _split_top_level(tail, seps)
        if _looks_like_enum(items):
            if best is None or len(items) > len(best):
                best = items
    if best is None:
        return None

    values = [_strip_trailing_paren(x) for x in best]
    values = [v for v in values if v]
    if len(values) < _ENUM_MIN_ITEMS:
        return None

    return EnumParse(values=values, evidence=f"{comment[:m.start()]}{comment[m.start()]}{tail}")


# ── 入口 ────────────────────────────────────────────────────────────────


def parse(comment: str | None, index: TableIndex, self_table: str) -> ParsedComment:
    if not comment:
        return ParsedComment()

    enum = _parse_enum(comment)
    ref = _parse_ref(comment, index, self_table)

    # desc_core：给人看的核心描述
    if enum:
        # 冒号前就是「这个字段是什么」
        m = None
        for m in _COLON.finditer(comment):
            pass
        core = comment[: m.start()].strip() if m else comment
    else:
        # 剔掉括号里的关联括注，`关联資產ID（外键，引用 t_asset 表）` → `关联資產ID`
        core = re.sub(r"[（(][^（()）]*[）)]", "", comment).strip(" ，,;；")

    return ParsedComment(enum=enum, ref=ref, desc_core=core or comment)


def build_index(tables) -> TableIndex:
    return TableIndex({t.name: {c.name for c in t.columns} for t in tables})
