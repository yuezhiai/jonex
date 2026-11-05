"""生成期有界探测：把「这一列里到底是什么」变成**可审计的证据**。

补的是报告里的**缺口 B（全链路零数据接触）**：在此之前，从读 catalog 到 ② 出 SQL，
**没有任何一环问过数据库「这列里有什么值」** —— 模型只能靠常识猜中文值（猜错就静默答 0）。

两条硬规矩：
1. **只存派生的证据，不存原始行**（`dbo.jobs` 取 200 行是 469KB，派生的证据只有几 KB）。
2. **取样不是抽样**：`SELECT TOP n *` / `LIMIT n`（按方言）取的是存储顺序的前 n 行，
   对有序表会偏 —— 所以字段名里一律带 `in_sample`，消费方当**样**不当**准**。
"""
from __future__ import annotations

import asyncio
import datetime
import decimal
import math
import re
from typing import Any

#: 每列最多留几个样本值。
MAX_SAMPLES = 5

#: 单个样本值的字符上限：`TOP 200 *` 可能捞到一整段备注，原样存进去会把证据撑爆。
MAX_VALUE_CHARS = 80

#: 采样行数上限。写死常量，**不来自任何外部输入**。
SAMPLE_ROWS = 200

#: 探测总预算（秒）。用完即停，已探到的照常落库。
PROBE_BUDGET_S = 300.0

#: 单表超时（秒）。⚠️ 既要喂给适配器（管连接建立），**也要包在 `asyncio.wait_for` 外面**
#: （管语句本身）—— 只给前者时慢查询没有上界，同包 `nl2sql/runner.py` 的注释解释过这件事。
TABLE_TIMEOUT_S = 60.0

#: 表名白名单：可选一段 schema 限定，其余字符一律拒。
#: ⚠️ 这是本设计**唯一新增的攻击面** —— 探测 SQL 是自拼的、不过 guard，
#: 表名又来自编译 schema（可被人工编辑），所以必须在拼串之前挡一道。
_IDENT_RE = re.compile(r"^[A-Za-z_][\w$]*(\.[A-Za-z_][\w$]*)?$")


def _is_missing(value: Any) -> bool:
    """只有 `None` 算缺失。空串是**真实值**（GBK varchar 里很常见），照实计数。"""
    return value is None


def _jsonable(value: Any) -> str:
    """取样值 → 能进 JSONB 的字符串。"""
    if isinstance(value, (datetime.date, datetime.datetime)):
        return value.isoformat(sep=" ") if isinstance(value, datetime.datetime) else value.isoformat()
    if isinstance(value, decimal.Decimal):
        return str(value)
    if isinstance(value, float) and math.isnan(value):
        return "NaN"
    return str(value)[:MAX_VALUE_CHARS]


def _distinct_key(value: Any) -> tuple:
    """去重键带上类型：`True` 与 `1` 在 Python 里相等，但它们是两个不同的取值。"""
    return (type(value).__name__, str(value))


def _numeric_range(values: list[Any]) -> list[Any] | None:
    """全列都是数值时给 `[min, max]`；混了非数值就不给（别硬转）。

    ⚠️ **NaN / Inf 一律整列不给**（评审查出）：`_jsonable` 挡了 NaN 而这里原来没挡 ——
    两条路不对称，而后果比「值不准」严重得多：**PostgreSQL 的 jsonb 拒绝 `NaN`/`Infinity`**，
    一条 NaN 会让**整份证据（所有表）写库失败**，再被 `generate_schema` 那段宽 `except` 吞成一条 warning，
    表现为「探测跑了、证据没了」。能存 NaN 的引擎（postgres `float8`）是一等公民，所以必须挡在源头。
    """
    nums: list[float] = []
    for v in values:
        if isinstance(v, bool) or not isinstance(v, (int, float, decimal.Decimal)):
            return None
        number = float(v)
        if not math.isfinite(number):
            return None
        nums.append(number)
    return [min(nums), max(nums)] if nums else None


def derive_evidence(columns: list[str], rows: list[list], *, max_samples: int = MAX_SAMPLES) -> dict:
    """取样行 → 每列证据（**纯函数**，不连库不调模型，所以最该被单测钉死）。"""
    out: dict[str, dict] = {}
    for index, name in enumerate(columns):
        values = [row[index] for row in rows if index < len(row)]
        present = [v for v in values if not _is_missing(v)]

        seen: dict[tuple, Any] = {}
        for v in present:
            seen.setdefault(_distinct_key(v), v)

        entry: dict[str, Any] = {"non_null": len(present), "distinct_in_sample": len(seen)}
        # 样本里跳过空串：它是真实值，但当样本没有信息量
        samples = [_jsonable(v) for v in seen.values() if not (isinstance(v, str) and not v.strip())]
        if samples:
            entry["samples"] = samples[:max_samples]
        rng = _numeric_range(present)
        if rng is not None:
            entry["numeric_range"] = rng
        out[str(name)] = entry
    return {"sample_rows": len(rows), "columns": out}


def _sample_sql(cfg: dict, table: str, sample_rows: int) -> str:
    """按**方言**拼取样语句：SQL Server 只有 `TOP n`，PG/MySQL 用 `LIMIT n`。

    `engine` 先过 `normalize_engine`（与 guard / 适配器同一份词表，不另立第三张）。
    """
    from .schema_source import normalize_engine

    engine = normalize_engine(str(cfg.get("engine") or ""))
    if engine == "sqlserver":
        return f"SELECT TOP {int(sample_rows)} * FROM {table}"
    return f"SELECT * FROM {table} LIMIT {int(sample_rows)}"


def _redact(text: str, cfg: dict) -> str:
    """把错误文本里可能出现的凭据抹掉。

    `errors` 会随 `evidence` 落进 JSONB（spec §六），而 `cfg` 里握着**解密后的只读口令**；
    驱动报文一般不带它，但「一般」不该由我们赌。
    """
    for key in ("password", "readonly_password"):
        secret = str(cfg.get(key) or "")
        if secret:
            text = text.replace(secret, "***")
    return text


def safe_table_name(name: str) -> str | None:
    """表名过白名单；不合格返回 `None`（**调用方必须跳过**，绝不做「清洗后照用」）。"""
    candidate = (name or "").strip()
    return candidate if _IDENT_RE.match(candidate) else None


def evidence_key(entity: dict) -> str:
    """编译实体 → 证据表的键。**写入方与读取方必须共用这一个**：对空白值先 strip、再回退实体名。

    两边各写一遍的话，`"  "` 这种空白 `source_object_id` 会让写入落在实体名下、读取却去查 `"  "` ——
    证据静默取不到（本线反复治的就是这类「不报错、只是没有」的失效）。
    """
    if not isinstance(entity, dict):
        return ""
    key = str(entity.get("source_object_id") or "").strip()
    return key or str(entity.get("name") or "").strip()


async def probe_source(source, cfg: dict, tables: list[str], *,
                       sample_rows: int = SAMPLE_ROWS,
                       budget_s: float = PROBE_BUDGET_S,
                       now: Any = None) -> dict:
    """逐表取样。**失败不抛**（证据是增益，schema 是主产物），逐表 try/except + 全局预算。

    `now` 只用于测试注入时间；生产传 `None`（取当前 UTC）。
    """
    import time

    started = time.monotonic()
    probed: dict[str, dict] = {}
    errors: list[str] = []
    seen: set[str] = set()

    for raw in tables or []:
        table = safe_table_name(str(raw or ""))
        if table is None:
            # ⚠️ 不合格就**跳过**，绝不清洗后照用 —— 这是拼串前的最后一道闸
            errors.append(f"{str(raw)[:60]}: 表名含非法字符，跳过")
            continue
        if table in seen:
            continue
        seen.add(table)

        if time.monotonic() - started > budget_s:
            errors.append(f"{table}: 探测预算（{budget_s:g}s）已用完，未探")
            continue

        try:
            # ⚠️ **两处都是评审退回来的**（本模块的名字叫「有界探测」，这两条不补就名不副实）：
            # ① `run_query(timeout=…)` 在三套适配器里**只管连接建立**（asyncpg `connect(timeout=)` /
            #    aiomysql `connect_timeout` / pymssql `login_timeout`），**语句本身没有上界** ——
            #    一条打在大表上的慢查询能把 `generate_schema` 挂到天荒地老（全局预算兜不住：它在每张表**之前**才查一次）。
            #    同包的 `nl2sql/runner.py` 早就解决过这件事，这里照抄它。
            # ② 行数上限是**方言**问题：`TOP n` 是 T-SQL 专写，PG/MySQL 上每张表都是语法错 →
            #    证据静默为空（而 postgres / mysql 是一等适配器）。
            result = await asyncio.wait_for(
                source.run_query(cfg, _sample_sql(cfg, table, sample_rows), timeout=TABLE_TIMEOUT_S),
                timeout=TABLE_TIMEOUT_S,
            )
        except Exception as exc:  # noqa: BLE001 —— 一张表探不动不该让整趟探测失败
            detail = _redact(str(exc), cfg)[:120]
            if isinstance(exc, asyncio.TimeoutError):
                # ⚠️ `wait_for` 抛的超时**自身不带任何文本**（`str(exc)` 恒为空），原样落库就成了
                # 「dbo.slow: TimeoutError: 」—— 事后读这条记录的人分不清它是超时、还是别的空消息异常。
                detail = f"超时 {TABLE_TIMEOUT_S:g}s 未返回"
            errors.append(f"{table}: {type(exc).__name__}: {detail}")
            continue

        rows = [list(r) for r in (result.get("rows") or [])]
        entry = derive_evidence([str(c) for c in (result.get("columns") or [])], rows)
        entry["row_estimate"] = f">={sample_rows}" if len(rows) >= sample_rows else str(len(rows))
        probed[table] = entry

    stamp = now or datetime.datetime.now(datetime.timezone.utc)
    return {
        "probed_at": stamp.isoformat(),
        "sample_rows": int(sample_rows),
        "tables": probed,
        "errors": errors,
    }


#: 「值包含」候选验证的每列取样条数。与 `SAMPLE_ROWS` 分开：那个是整源粗探（TOP n 行），
#: 这个是按候选列定向精探（distinct n 个值），口径不同、预算也不同。
DISTINCT_SAMPLE_ROWS = 200


def safe_column_name(name: str) -> str | None:
    """列名过白名单；不合格返回 `None`（调用方必须跳过）。与 `safe_table_name` 同一正则。

    探测 SQL 是自拼的（不过 guard），列名同样来自编译 schema（可被人工编辑）——
    与表名同属「拼串前的最后一道闸」。
    """
    return safe_table_name(name)


def distinct_samples_sql(cfg: dict, table: str, column: str,
                         sample_rows: int = DISTINCT_SAMPLE_ROWS) -> str:
    """按方言拼「某列取 distinct 样本」语句。

    ⚠️ 调用方必须先过 `safe_table_name` / `safe_column_name` —— 本函数不做校验。
    """
    from .schema_source import normalize_engine

    engine = normalize_engine(str(cfg.get("engine") or ""))
    if engine == "sqlserver":
        return f"SELECT DISTINCT TOP {int(sample_rows)} {column} FROM {table}"
    return f"SELECT DISTINCT {column} FROM {table} LIMIT {int(sample_rows)}"


async def probe_distinct(source, cfg: dict, table: str, column: str, *,
                         sample_rows: int = DISTINCT_SAMPLE_ROWS,
                         timeout_s: float = TABLE_TIMEOUT_S) -> list[str]:
    """取某列 distinct 样本（跨源补边候选的验证探针）。**失败照抛**。

    与 `probe_source` 的分工：那个是「整源粗探、失败不抛」的增益路径（证据是增益、
    schema 是主产物）；这个是「按候选列定向精探」——失败要让调用方知道（该候选判
    「不可测」），吞掉异常等于把失败伪装成「没找到值」。所以这里不兜、直接抛。

    安全口径与超时口径共用：表/列名先过白名单（不合格直接 `ValueError`），语句外面
    照 `probe_source` 同款包 `asyncio.wait_for`（`run_query` 的 timeout 只管连接建立）。
    """
    safe_table = safe_table_name(table)
    safe_column = safe_column_name(column)
    if safe_table is None or safe_column is None:
        raise ValueError(f"表/列名含非法字符: {table}.{column}")
    result = await asyncio.wait_for(
        source.run_query(
            cfg, distinct_samples_sql(cfg, safe_table, safe_column, sample_rows),
            timeout=timeout_s,
        ),
        timeout=timeout_s,
    )
    values = [
        _jsonable(row[0])
        for row in (result.get("rows") or [])
        if row and row[0] is not None
    ]
    return values[:sample_rows]
