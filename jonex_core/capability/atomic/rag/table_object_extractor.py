# -*- coding: utf-8 -*-
"""[jonex] 表格对象抽取（Row-as-Object）
（docs/table-parsing-retrieval-governance-plan.md 增补二 §10.2 + 增补十表头判定分层架构）。

核心判断：表格是天然关系型数据，一行 = 一个对象实例，列名 = 属性名。
不需要 LLM 猜——确定性映射进本体，绕开通用文本抽取的三个结构性问题：

- P1 属性全空：列名→属性直接结构化写入 attributes；
- P2 200 实体硬截断：表格对象不走 `ONTOLOGY_EXTRACT_MAX_ENTITIES` 截断，
  改用 `TABLE_OBJECT_MAX_ROWS`（默认 2000）每表行数上限；
- P3 同名合并污染：canonical_name 用「主键（+限定值）」在生成时保证 KB 内唯一，
  不参与 LightRAG 按名合并。

LLM 职责收缩到「每表一次」：表级判定（含表头行/说明行/层数 + 对象类型 +
主键列 + 限定列，增补十分层架构第 ② 层），结果按 `hash(表标题+列名签名)` 缓存；
列名→标准属性名映射（可选）。解析失败一律退回纯规则，不阻塞入库。

挂点：atomic-rag `_run_ontology_extraction`（ctx.content_list 可用时），
产出并入 `ontology_data`，由 kb-service reconciliation 写 Neo4j。
"""
import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ── 主键词表（10.2.1 优先级 2）──
_PK_NAME_PATTERNS = (
    "名称", "简称", "品种", "代码", "编码", "编号", "ID", "客户",
    "机构", "字段", "错误码", "证券代码", "营业部",
)
# 度量值列（非主键候选）：纯数字/金额/百分比/日期
_METRIC_RE = re.compile(
    r"^\s*[\d,，.+\-]+(%|％|元|万元|亿元|手|克|千克|吨|％|%|\s)*\s*$"
)
# 分组关系类型（分组列值 → 对象实例）
_GROUP_RELATION_TYPE = "包含"
# 多级表头拼接时跳过的占位符（与 raganything/utils.py 的
# _HEADER_PLACEHOLDERS 同口径——「—」等表示沿用上一级列名）
_HEADER_PLACEHOLDERS = {"—", "-", "–", "--", "/", "\\"}


@dataclass
class TableVerdict:
    """表级判定结果（LLM 或规则 fallback）。"""

    header_row_indices: List[int] = field(default_factory=list)
    note_row_indices: List[int] = field(default_factory=list)
    levels: int = 0
    object_type: str = ""
    pk_columns: List[str] = field(default_factory=list)
    qualifier_columns: List[str] = field(default_factory=list)
    column_mapping: Dict[str, str] = field(default_factory=dict)  # 原列名 → 标准属性名
    source: str = "rules"  # llm / cache / rules


def table_cache_key(table_caption: str, column_list: str) -> str:
    """缓存键：表标题 + 列名签名（10.2.4 / 增补十第 ⑤ 层）。"""
    raw = f"{table_caption}||{column_list}"
    return hashlib.md5(raw.encode()).hexdigest()


def _is_metric(value: str) -> bool:
    """度量值判定（非主键候选）：纯数字/金额/百分比。"""
    if not value or not value.strip():
        return True  # 空值不算有效主键
    return bool(_METRIC_RE.match(value.strip()))


def infer_pk_columns(header: List[str], rows: List[List[str]]) -> List[str]:
    """主键识别规则兜底（10.2.1）：
    1. 列名匹配主体词表（按词表顺序第一个命中）；
    2. 兜底：第一个「唯一值率 > 0.8 且非度量」的列；
    3. 复合主键：单列唯一率 < 0.8 时追加限定列直到唯一（每列唯一率 > 0.5）。

    返回列名列表（复合主键按顺序）。
    """
    if not rows or not header:
        return []

    n_cols = len(header)
    n_rows = len(rows)

    def col_values(col_idx: int) -> List[str]:
        return [
            (rows[r][col_idx] if col_idx < len(rows[r]) else "")
            for r in range(n_rows)
        ]

    def unique_ratio(col_idx: int) -> float:
        values = [v.strip() for v in col_values(col_idx) if v.strip()]
        if not values:
            return 0.0
        return len(set(values)) / len(values)

    # 优先级 2：词表匹配（多个命中时按唯一率取最佳——「品种」0.67 与
    # 「代码及合约」1.0 并列命中时选后者）
    best: Optional[int] = None
    best_ratio = 0.0
    for pattern in _PK_NAME_PATTERNS:
        for idx, col in enumerate(header):
            if pattern.lower() in (col or "").lower():
                ratio = unique_ratio(idx)
                if ratio >= 0.5 and ratio > best_ratio:
                    best_ratio = ratio
                    best = idx

    if best is None:
        # 优先级 3：高基数且非度量
        for idx, col in enumerate(header):
            if _is_metric(col or ""):
                continue  # 列名本身是度量词
            values = [v.strip() for v in col_values(idx) if v.strip()]
            if not values:
                continue
            if any(_is_metric(v) for v in values[: min(20, len(values))]):
                # 列值是度量 → 排除（采样前 20 行判断）
                continue
            ratio = unique_ratio(idx)
            if ratio > best_ratio:
                best_ratio = ratio
                best = idx
    if best is None:
        return []

    pk = [header[best]]
    # 复合主键：单列唯一率不达标时追加限定列，直到组合唯一率 ≥ 0.8
    if unique_ratio(best) < 0.8:
        remaining = [i for i in range(n_cols) if i != best]
        for i in remaining:
            combined = [
                f"{rows[r][best].strip()}|"
                f"{rows[r][i].strip() if i < len(rows[r]) else ''}"
                for r in range(n_rows)
            ]
            combined = [c for c in combined if not c.endswith("|")]
            if not combined:
                continue
            ratio = len(set(combined)) / len(combined)
            if ratio >= 0.8:
                pk.append(header[i])
                break
    return pk


def _compose_header_parts(
    header_rows: List[List[str]], n_cols: int,
) -> List[List[str]]:
    """多级表头逐列拆解为父级链（供拼接与属性组共用）：
    ``审批授权情况 / 部门负责人`` → ``["审批授权情况", "部门负责人"]``；
    占位符（「—」等）与重复值跳过；全空列返回 ``[]``。"""
    out: List[List[str]] = []
    for c in range(n_cols):
        parts: List[str] = []
        seen: set = set()
        for row in header_rows:
            v = row[c].strip() if c < len(row) else ""
            if v and v not in seen and v not in _HEADER_PLACEHOLDERS:
                parts.append(v)
                seen.add(v)
        out.append(parts)
    return out


def _compose_header_columns(
    header_rows: List[List[str]], n_cols: int,
) -> List[str]:
    """多级表头逐列拼接（与 raganything/utils.py 的 `_compose_header`
    同口径——jonex_core 不反向依赖 vendored，故在平台侧复刻）：
    ``审批授权情况 / 部门负责人`` → ``审批授权情况-部门负责人``；
    占位符（「—」等）与重复值跳过；仍为空列保留 ``col_N`` 兜底。"""
    out: List[str] = []
    for c, parts in enumerate(_compose_header_parts(header_rows, n_cols)):
        out.append("-".join(parts) if parts else f"col_{c}")
    return out


def std_attribute_name(
    column: str, mapping: Optional[Dict[str, str]],
) -> str:
    """列名 → 标准属性名（§10.2.4 第二次 LLM 产物的消费入口）：
    映射表命中取标准名，否则原样返回。"""
    if not mapping or not column:
        return column
    return mapping.get(column) or column


def apply_header_verdict(
    header: List[str],
    rows: List[List[str]],
    verdict: TableVerdict,
) -> Tuple[List[str], List[List[str]], List[int]]:
    """[jonex] §table-grid-v2 步14 补缺：消费表级判定的表头/说明行结论。

    规则推断把「规则认定的表头」剥掉后，LLM 可能判定剩余的 data_rows 中
    还有表头行（多级表头未被规则识别）或说明行（长文本未被规则吸净）。
    此前 `header_row_indices` / `note_row_indices` 只校验不消费（对象抽取
    仍用规则表头）——本函数补齐下游消费：

    - ``header_row_indices``（相对 rows，0-based）→ 与既有 header 逐列
      拼接为多级列名（与 `_infer_table_header` 同拼接口径）；
    - ``note_row_indices`` → 从对象行剔除；
    - 其余行按原序保留，返回的 ``row_map[i]`` = 该行在原 rows 中的索引
      （source_chunks 行锚点不因剔除而错位）。

    无额外表头/说明行（或全越界）时原样返回，零副作用。
    主键/限定列的列名重映射由调用方按列位置做（列序恒定）。
    """
    h_idx = [i for i in verdict.header_row_indices if 0 <= i < len(rows)]
    note_idx = {i for i in verdict.note_row_indices if 0 <= i < len(rows)}
    if not h_idx and not note_idx:
        return header, rows, list(range(len(rows)))
    n_cols = len(header)
    new_header = _compose_header_columns([header] + [rows[i] for i in h_idx], n_cols)
    kept_rows: List[List[str]] = []
    row_map: List[int] = []
    for i, row in enumerate(rows):
        if i in h_idx or i in note_idx:
            continue
        row_map.append(i)
        kept_rows.append(row)
    return new_header, kept_rows, row_map


def detect_group_column(
    header: List[str], rows: List[List[str]],
) -> Optional[int]:
    """分组列检测（10.2.3）：rowspan forward-fill 产物——某列连续多行同值，
    变化次数 ≤ 行数一半（只看前 8 列，分组键通常靠左）。"""
    if not rows:
        return None
    n_cols = len(header)
    n_rows = len(rows)
    best_col: Optional[int] = None
    best_changes = n_rows
    for c in range(min(n_cols, 8)):
        changes = 0
        prev: Optional[str] = None
        for row in rows:
            v = (row[c].strip() if c < len(row) else "")
            if v != prev:
                changes += 1
                prev = v
        if changes < best_changes and changes <= n_rows * 0.5:
            best_changes = changes
            best_col = c
    return best_col


@dataclass
class TableObject:
    """一行 → 一个对象实例（10.2.2）。"""

    canonical_name: str
    entity_type: str
    aliases: List[str] = field(default_factory=list)
    attributes: Dict[str, Any] = field(default_factory=dict)
    source_chunks: List[Dict[str, Any]] = field(default_factory=list)
    pk_value: str = ""
    table_sig: str = ""


class TableObjectExtractor:
    """表格对象抽取器（确定性映射 + 表级 LLM 判定）。"""

    def __init__(self, cache: Optional[Dict[str, TableVerdict]] = None):
        self._cache = cache if cache is not None else {}

    # ── 表级判定：LLM 主判 + 校验 + 规则 fallback（增补十 ②③④⑤）──

    @staticmethod
    def _validate_verdict(
        verdict: TableVerdict, n_rows: int,
    ) -> bool:
        """③ 输出校验（代码硬约束）：
        行号连续、从 0 开始、≤ 总行数、header 与 note 不重叠、层级 ≤3。
        """
        h = verdict.header_row_indices
        if not h:
            return False
        if h != list(range(h[0], h[0] + len(h))):  # 连续
            return False
        if h[0] != 0:
            return False
        if max(h) >= n_rows:
            return False
        if set(h) & set(verdict.note_row_indices):
            return False
        if len(h) > 3:
            return False
        return True

    async def analyze_table(
        self,
        table_caption: str,
        header: List[str],
        rows: List[List[str]],
        scope: Optional[Dict[str, Any]] = None,
        tbox_attributes: Optional[List[str]] = None,
    ) -> TableVerdict:
        """表级判定（LLM 主判，合并表头判定模型化）+ 列名→TBox 属性映射。

        输入：表标题 + 列名清单 + 前 5 行样例；输出 TableVerdict。
        缓存命中 → cache；LLM 失败/校验不过 → 规则 fallback。
        第二次 LLM 调用（列名映射，§10.2.4）失败只丢弃映射，不阻塞判定；
        映射随判定一起按 `hash(表标题+列名签名)` 缓存。
        """
        column_list = "、".join(header)
        key = table_cache_key(table_caption, column_list)
        cached = self._cache.get(key)
        if cached is not None:
            cached.source = "cache"
            return cached

        verdict = await self._llm_verdict(
            table_caption, header, rows, scope,
        )
        if verdict is not None and self._validate_verdict(verdict, len(rows)):
            verdict.source = "llm"
            verdict.column_mapping = await self._llm_column_mapping(
                header, scope, tbox_attributes,
            )
            self._cache[key] = verdict
            return verdict

        # ④ 规则 fallback（LLM 不可用/校验失败时必须能降级）
        logger.info(
            "TableObjectExtractor: LLM 判定不可用或校验失败，回退规则主键识别 "
            "(caption=%s)", table_caption[:40],
        )
        verdict = TableVerdict(
            object_type="",
            pk_columns=infer_pk_columns(header, rows),
            source="rules",
        )
        # 表头/说明行取规则推断的既有结果（调用方已跑 normalize_table_grid，
        # 此处不重跑——header 本身就是规则推断产物）
        return verdict

    async def _llm_verdict(
        self,
        table_caption: str,
        header: List[str],
        rows: List[List[str]],
        scope: Optional[Dict[str, Any]],
    ) -> Optional[TableVerdict]:
        """LLM 表级判定（每表一次）。失败返回 None。"""
        try:
            from .ontology_extractor import _openai_chat
            from .prompts.table_object import TABLE_OBJECT_TYPE_PROMPT

            sample_rows = min(5, len(rows))
            samples = "\n".join(
                f"行 {i}: {' | '.join(str(c) for c in rows[i][: len(header)])}"
                for i in range(sample_rows)
            )
            prompt = TABLE_OBJECT_TYPE_PROMPT.format(
                table_caption=table_caption or "（无标题）",
                column_list="、".join(header),
                row_count=len(rows),
                sample_rows=sample_rows,
                row_samples=samples,
            )
            raw = await _openai_chat(
                [{"role": "user", "content": prompt}],
                temperature=0,
                scope=scope,
                json_mode=True,
            )
            data = json.loads(raw)
            return TableVerdict(
                header_row_indices=[int(i) for i in data.get("header_row_indices", [])],
                note_row_indices=[int(i) for i in data.get("note_row_indices", [])],
                levels=int(data.get("levels", 0)),
                object_type=str(data.get("object_type", "")),
                pk_columns=[str(c) for c in data.get("pk_columns", [])],
                qualifier_columns=[
                    str(c) for c in data.get("qualifier_columns", [])
                ],
            )
        except Exception as exc:  # noqa: BLE001 — 判定失败必须降级
            logger.warning("TableObjectExtractor LLM verdict failed: %s", exc)
            return None

    async def _llm_column_mapping(
        self,
        header: List[str],
        scope: Optional[Dict[str, Any]],
        tbox_attributes: Optional[List[str]] = None,
    ) -> Dict[str, str]:
        """§10.2.4 第二次 LLM：列名 → TBox 标准属性名（每表一次）。

        输出 `{原列名: 标准属性名}`；失败/校验不过返回 `{}`（映射是
        增强项，缺失时对象抽取用原列名，不阻塞入库）。
        """
        try:
            from .ontology_extractor import _openai_chat
            from .prompts.table_object import TABLE_COLUMN_MAPPING_PROMPT

            prompt = TABLE_COLUMN_MAPPING_PROMPT.format(
                column_list="、".join(header),
                tbox_attributes="、".join(tbox_attributes) if tbox_attributes else "（无）",
            )
            raw = await _openai_chat(
                [{"role": "user", "content": prompt}],
                temperature=0,
                scope=scope,
                json_mode=True,
            )
            data = json.loads(raw)
            mapping = data.get("mapping", {}) or {}
            out: Dict[str, str] = {}
            for k, v in mapping.items():
                kk = str(k).strip()
                vv = str(v).strip()
                if kk and vv and kk in header and vv != kk:
                    out[kk] = vv
            return out
        except Exception as exc:  # noqa: BLE001 — 映射失败只丢弃，不影响判定
            logger.warning("TableObjectExtractor column mapping failed: %s", exc)
            return {}

    # ── 对象实例生成（每行一次，纯代码）──

    def build_objects(
        self,
        table_caption: str,
        header: List[str],
        rows: List[List[str]],
        verdict: TableVerdict,
        table_sig: str,
        table_idx: Optional[int] = None,
        row_indices: Optional[List[int]] = None,
        header_parts: Optional[List[List[str]]] = None,
    ) -> List[TableObject]:
        """行 → 对象实例（10.2.2）。主键值缺失的行跳过（无法命名）。

        row_indices：可选，rows 各行在原数据行序列中的索引（表头/说明行
        剔除后传入，保证 source_chunks 的 row_start/row_end 锚点不错位）。

        verdict.column_mapping（§10.2.4）：列名映射为标准属性名后，
        attributes 同时写原列名键与标准名键（S7 槽位命中两种问法都可达）。

        header_parts（层级关系 §10.2.3）：与 header 对齐的多级表头父级链
        （`_compose_header_parts` 产出）。父级链长度 >1 的列记入属性组——
        ``审批授权情况-部门负责人`` 记为属性组 ``审批授权情况`` 下的
        ``部门负责人``（嵌套 dict）；单级表头列保持扁平。
        """
        objects: List[TableObject] = []
        pk_indices = [
            header.index(c) for c in verdict.pk_columns if c in header
        ]
        qual_indices = [
            header.index(c) for c in verdict.qualifier_columns if c in header
        ]
        if not pk_indices:
            return objects

        for r, row in enumerate(rows):
            pk_values = [
                (row[i].strip() if i < len(row) else "") for i in pk_indices
            ]
            pk_values = [v for v in pk_values if v]
            if not pk_values:
                continue
            pk_main = pk_values[0]
            qual_values = [
                (row[i].strip() if i < len(row) else "") for i in qual_indices
            ]
            qual_values = [v for v in qual_values if v]
            # 复合主键：主键值自身就含多段
            if len(pk_values) > 1:
                pk_main = "|".join(pk_values)
            canonical = (
                f"{pk_main}（{'、'.join(qual_values)}）"
                if qual_values
                else pk_main
            )
            attributes: Dict[str, Any] = {}
            for idx, col in enumerate(header):
                v = row[idx].strip() if idx < len(row) else ""
                if not v:
                    continue
                parts = (
                    header_parts[idx]
                    if header_parts is not None and idx < len(header_parts)
                    else None
                )
                if parts and len(parts) > 1:
                    # 层级关系：父级链 → 嵌套属性组，叶子列名过映射
                    leaf = std_attribute_name(parts[-1], verdict.column_mapping)
                    node: Dict[str, Any] = attributes
                    for g in parts[:-1]:
                        child = node.get(g)
                        if not isinstance(child, dict):
                            child = {}
                            node[g] = child
                        node = child
                    node[leaf] = v
                    if leaf != parts[-1]:
                        node[parts[-1]] = v
                else:
                    attributes[col] = v
                    std = std_attribute_name(col, verdict.column_mapping)
                    if std != col:
                        attributes[std] = v
            src_row = row_indices[r] if row_indices else r
            objects.append(TableObject(
                canonical_name=canonical,
                entity_type=verdict.object_type or "unknown",
                aliases=[pk_main, f"{table_caption}·{pk_main}"]
                if table_caption else [pk_main],
                attributes=attributes,
                source_chunks=[{
                    "row_start": src_row, "row_end": src_row + 1,
                    "table_idx": table_idx,
                    "table_sig": table_sig,
                }],
                pk_value=pk_main,
                table_sig=table_sig,
            ))
        return objects

    # ── 关系生成（纯代码 + 规则）──

    def build_group_relations(
        self,
        header: List[str],
        rows: List[List[str]],
        objects: List[TableObject],
        verdict: TableVerdict,
    ) -> List[Dict[str, str]]:
        """分组关系（10.2.3）：forward-fill 分组列 → 包含关系。

        对象列表与 rows 行序一致（build_objects 保序），按分组列值把
        连续同值段的对象挂到「分组名（分组列: 值）」聚合节点下。
        """
        group_col = detect_group_column(header, rows)
        if group_col is None:
            return []
        group_type = header[group_col]
        relations: List[Dict[str, str]] = []
        for obj, row in zip(objects, rows):
            gv = (row[group_col].strip() if group_col < len(row) else "")
            if not gv:
                continue
            relations.append({
                "source_name": f"{group_type}·{gv}",
                "source_type": group_type,
                "target_name": obj.canonical_name,
                "target_type": obj.entity_type,
                "relation_type": _GROUP_RELATION_TYPE,
            })
        return relations
