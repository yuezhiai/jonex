"""中文处理 —— 簡繁归一化与别名派生。

这个库同時混用簡体和繁体：`資產主表`、`資产推薦-配置明細表`、`资产名称`。
用户提问只会用其中一种写法。不归一化的话，用简体问「资产」就召不回注释写作
「資產」的表——这个库 89 张表里相当一部分的注释是繁体，等于一半的召回直接废掉。

归一化的做法不是二选一，而是**三态并存**：原文、简体、繁体都进 aliases。
多出来的两个别名成本极低（只是几个字符串），但保证了任一写法都能命中。
"""

from __future__ import annotations

import re

import zhconv

# 派短别名时只剥末尾的「表」/「主表」。
#
# 曾经把「明细表」「记录表」「配置表」也一起剥，结果 `资产资源申请明细表` 和
# `资产资源申请表` 都变成「资产资源申请」，撞成一团。那些词是**区分性**的，
# 不是冗余结构词——剥掉等于把主表和它的明细表混为一谈。
_STRUCT_SUFFIX = re.compile(r"(主表|表)$")

# 注释里常见的括注尾巴，派生别名时去掉
_TAIL_NOISE = re.compile(r"[（(][^（()）]*[）)]\s*$")

# 别名长度上限。过长的「别名」实质是描述，放进 aliases 只会污染全文索引
_MAX_ALIAS = 24


def to_simplified(s: str) -> str:
    return zhconv.convert(s, "zh-cn")


def to_traditional(s: str) -> str:
    return zhconv.convert(s, "zh-tw")


def simplified_or_none(s: str | None) -> str | None:
    """`to_simplified` 的空安全包装。

    `short_alias()` 在没有注释时返回 None，直接喂给 zhconv 会 `len(None)` 崩掉。
    这个转换在链路里反复出现，集中收口一次。
    """
    return to_simplified(s) if s else None


def variants(*parts: str | None, limit: int = _MAX_ALIAS) -> list[str]:
    """把若干候选片段展开成 原文 / 简体 / 繁体 三态，去重、去噪、保序。

    顺序即优先级：原文在前，简体次之。下游若截断别名数，砍掉的是最靠后的。
    """
    out: list[str] = []
    seen: set[str] = set()
    for p in parts:
        if not p:
            continue
        for form in (p, to_simplified(p), to_traditional(p)):
            f = form.strip()
            if not f or len(f) > limit:
                continue
            k = f.lower()
            if k in seen:
                continue
            seen.add(k)
            out.append(f)
    return out


def _split_outside_brackets(s: str, seps: str, limit: int = 1) -> list[str]:
    """按分隔符切分，但**忽略括号内部**的同类字符。

    `二開中間件供給結果表（apply 1:N）：審批通過後…` 里的 `1:N` 是括号内的说明，
    不是句读。曾经无脑按 `[：:，,；;。]` 切，结果短别名被截成
    `二開中間件供給結果表（apply 1`——括号都没闭合。
    """
    out: list[str] = []
    buf: list[str] = []
    depth = 0
    for ch in s:
        if ch in "（(【[":
            depth += 1
        elif ch in "）)】]":
            depth = max(0, depth - 1)
        if depth == 0 and ch in seps and len(out) < limit:
            out.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    out.append("".join(buf))
    return out


def short_alias(comment: str | None) -> str | None:
    """从表注释里挤出短的核心词。

    `資產主表` → `资产`；`二開中間件供給結果表（apply 1:N）：審批通過…` →
    `二开中间件供给结果`。短别名是检索的主力——用户问「资产相关的表」时打的就是
    这个词，而不是完整注释「资产主表」。
    """
    if not comment:
        return None
    s = _TAIL_NOISE.sub("", comment).strip()
    # 注释常写成「A：B」的形式，取冒号前的部分当核心词（括号内的冒号不算）
    s = _split_outside_brackets(s, "：:，,；;。")[0].strip()
    # 切完可能剩下一个没闭合的左括号（分隔符在括号后），把尾巴削掉
    for op, cl in (("（", "）"), ("(", ")")):
        if s.count(op) > s.count(cl):
            s = s[: s.rfind(op)].strip()
    stripped = _STRUCT_SUFFIX.sub("", s).strip()
    return stripped or None
