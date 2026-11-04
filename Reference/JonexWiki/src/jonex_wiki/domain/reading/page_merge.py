"""Reading-side aliases require an explicit, authoritative identity.

Same titles, equal normalized spellings, richer bodies or more claims cannot
prove that two pages concern the same subject. The default is an identity
mapping: preserve every page, reference and graph endpoint. A caller with a
verified canonical identity may explicitly supply ``identity_of``. Such an
identity is scoped by that caller to the current tenant, Bundle and subject;
this module never derives it from a title or a domain-specific naming rule.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable, Dict, Mapping, Optional, Sequence, Tuple

# 检索/证据侧页面：永不参与归并（同标题也可能是不同来源意义上的页面）
_EXCLUDED_PREFIXES = ("tables/", "table-views/", "sources/", "source-evidence/", "raw/")

_WHITESPACE_RE = re.compile(r"[\s\u3000]+")
_INTERPUNCT_RE = re.compile(r"[·・\u00b7]")


def normalize_title(title: str) -> str:
    """Legacy cosmetic label normalization; never evidence of subject identity."""
    text = (title or "").strip()
    text = _INTERPUNCT_RE.sub("", _WHITESPACE_RE.sub("", text))
    return text.casefold()


def is_merge_candidate(path: str) -> bool:
    """该路径是否参与同名归并（排除检索/证据侧目录）。"""
    return not path.startswith(_EXCLUDED_PREFIXES)


@dataclass(frozen=True)
class MergeMap:
    """展示侧归并结果：loser→winner 重定向 + 每个 winner 的全体成员。"""

    redirects: Mapping[str, str]
    members: Mapping[str, Tuple[str, ...]]
    titles: Mapping[str, str]

    @property
    def merged_count(self) -> int:
        """被并入别处的页面数（= 展示列表减少的条目数）。"""
        return len(self.redirects)

    def is_identity(self) -> bool:
        return not self.redirects

    def resolve(self, path: str) -> str:
        """把任意路径映射到展示用路径；非 loser 原样返回。"""
        return self.redirects.get(path, path)

    def cluster(self, path: str) -> Tuple[str, ...]:
        """返回该路径所属簇的全部成员（含 winner，已排序）；孤立页返回单元素。"""
        winner = self.resolve(path)
        return self.members.get(winner, (winner,))


_IDENTITY = MergeMap(redirects={}, members={}, titles={})


def build_merge_map(
    page_index: Mapping[str, Any],
    *,
    weight_of: Optional[Callable[[str], int]] = None,
    size_of: Optional[Callable[[str], int]] = None,
    is_candidate: Optional[Callable[[str], bool]] = None,
    identity_of: Optional[Callable[[str], Optional[str]]] = None,
) -> MergeMap:
    """Group only paths for which the caller supplies the same verified identity.

    Ranking chooses a presentation representative after identity is established;
    weights and body sizes are never evidence of identity. Missing identities
    keep their pages separate. The ordinary reader supplies no resolver.
    """
    if identity_of is None:
        return _IDENTITY
    if is_candidate is None:
        is_candidate = is_merge_candidate

    buckets: Dict[str, list] = {}
    titles: Dict[str, str] = {}
    for path in page_index:
        path = str(path)
        if not is_candidate(path):
            continue
        entry = page_index[path]
        title = ""
        if isinstance(entry, dict):
            title = str(entry.get("title") or "")
        key = identity_of(path)
        if not isinstance(key, str) or not key.strip():
            continue
        titles[path] = title
        buckets.setdefault(key, []).append(path)

    redirects: Dict[str, str] = {}
    members: Dict[str, Tuple[str, ...]] = {}
    for key, paths in buckets.items():
        if len(paths) < 2:
            continue
        ranked = sorted(
            paths,
            key=lambda p: (
                -(weight_of(p) if weight_of is not None else 0),
                -(size_of(p) if size_of is not None else 0),
                p.count("/"),
                p,
            ),
        )
        winner = ranked[0]
        members[winner] = tuple(sorted(paths))
        for loser in ranked[1:]:
            redirects[loser] = winner

    if not redirects:
        return _IDENTITY
    return MergeMap(redirects=redirects, members=members, titles=titles)


def remap_outline_paths(outline: Any, redirects: Mapping[str, str]) -> Any:
    """把目录（DB 里的 LLM 目录）中指向 loser 的条目重定向到 winner。

    结构无关的递归改写：任何含 ``path`` 键的字典都会被改写；任何一个
    「字典列表」内部按改写后的 path 去重（同一小节里两个 loser 并到同一 winner
    时只留一条）。主题/小节的层级与顺序一律原样保留。

    为什么在读取时做：目录落在 ``wiki_outlines`` 表里，是知识库级的，**不在
    bundle 内**，所以在编译产物里改不到它；而且读取时投影不必写库，等下一次
    「重新生成目录」基于归并后的页面清单起草完，重定向自然失效。
    """
    if not outline or not redirects:
        return outline

    def walk(node: Any) -> Any:
        if isinstance(node, list):
            out = []
            seen = set()
            for item in node:
                mapped = walk(item)
                if isinstance(mapped, dict) and isinstance(mapped.get("path"), str):
                    path = mapped["path"]
                    if path in seen:
                        continue
                    seen.add(path)
                out.append(mapped)
            return out
        if isinstance(node, dict):
            result = {}
            for key, value in node.items():
                if key == "path" and isinstance(value, str):
                    result[key] = redirects.get(value, value)
                else:
                    result[key] = walk(value)
            return result
        return node

    return walk(outline)
