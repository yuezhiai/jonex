#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""执行前的最后一道闸：**纯函数**，不连库、不调模型，最该被单测钉死。

判据都要么"是只读"，要么"拒绝"——**宁可误伤**（`WHERE remark = 'delete'` 这种
字符串里带关键字的会被拦掉），因为放过一条写语句的代价是生产库被改
（实测某源登记的是 `prod_user` / 生产库），而误伤的代价只是用户换个问法。

拦：多语句、非 SELECT 开头、任何写/DDL/存储过程关键字、SELECT INTO、
注释（生成 SQL 不需要注释，而注释是拼接绕过的主要载体 —— `#` 按方言判，见
`_HASH_REJECT_REASONS`）。

**方言口径**：`engine` 先过 `normalize_engine`，之后一律按规范键
（`postgres` / `mysql` / `sqlserver`；方言不明为空串）判「这个写法是不是这个方言的」。
这一层不能省：漏了归一化，`engine="MySQL"` 会让按方言的判据整个失效 —— 而失效的方向是
**放行一条「报有上限、实际没上限」的 SQL**（追加的 `LIMIT n` 落进 `#` 行注释里），
正是这个模块存在的理由被绕开。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

import sqlglot
from sqlglot import exp

# 方言别名表与服务层共用一份（`services/schema_source._ENGINES`），不在本文件另立第三张：
# 改那边等于改这里，改一处要改两处。
from ..schema_source import normalize_engine
from .forbidden import hit_forbidden

#: SQLite 风格布尔聚合 → PostgreSQL 原生函数。模型偶尔会写出
#: `logical_and(...)`/`logical_or(...)`（SQLite 函数名），在 Postgres 上直接
#: `function logical_and(boolean) does not exist`。这是**纯函数名替换**，不改语义。
_SQLITE_BOOL_AGG = {"logical_and": "bool_and", "logical_or": "bool_or"}


def normalize_dialect_sql(sql: str, engine: str) -> str:
    """按方言修正已知的跨方言函数名（只做确定性替换，不猜测语义）。"""
    if normalize_engine(engine) != "postgres" or not sql:
        return sql
    out = sql
    for src, dst in _SQLITE_BOOL_AGG.items():
        out = re.sub(rf"\b{src}\s*\(", f"{dst}(", out, flags=re.IGNORECASE)
    return out

#: 出现即拒（按词边界匹配，大小写不敏感）。
_FORBIDDEN = (
    "insert", "update", "delete", "drop", "alter", "create", "truncate",
    "grant", "revoke", "merge", "call", "exec", "execute", "into",
    "begin", "commit", "rollback", "set", "copy", "vacuum", "analyze",
    # 拖时间/读文件一类的函数名，出现即拒
    "pg_sleep", "sleep", "benchmark", "load_file", "xp_cmdshell",
)

_FORBIDDEN_RE = re.compile(r"\b(" + "|".join(_FORBIDDEN) + r")\b", re.IGNORECASE)

#: **正则兜底**：`FROM` / `JOIN` 后面紧跟的标识符（限定的点号、方括号、双引号、反引号都收）。
#: **全串扫**：子查询里的 `FROM x` 也会被扫到 —— 这正是我们要的。
#: `(?!\()` 只排除「FROM 后面直接是左括号」那种形状（那是派生表，不是表名）。
#:
#: ⚠️ 只在 AST 解析失败时使用：正则分不清「函数参数里的 FROM」与真正的表引用 ——
#: `EXTRACT(MONTH FROM stat_date)` / `SUBSTRING(x FROM 2 FOR 3)` / `TRIM(... FROM col)` /
#: `OVERLAY` 里的 `FROM 列` 都会被误当成表（真机「各厂10月份的发电量」因此被拒）。
_TABLE_RE = re.compile(
    r"\b(?:from|join)\s+(?!\()([A-Za-z_\[`\"][\w$.\-\[\]`\"]*)",
    re.IGNORECASE,
)

#: CTE 名（`WITH x AS (` / `WITH RECURSIVE x AS (` / `, y AS (`）—— 不是真表，校验时不能当越权。
#: 仅正则兜底路径使用；AST 路径直接读 `exp.CTE`。
_CTE_RE = re.compile(r"(?:\bwith\b|,)\s*(?:recursive\s+)?([A-Za-z_][\w$]*)\s+as\s*\(",
                     re.IGNORECASE)

#: `FROM` / `JOIN` **关键字本身**（不带后缀）—— 用来逐点核对每个表引用点是不是都被
#: `_TABLE_RE` 认了出来。`_TABLE_RE` 只在能认出表名时才命中，所以它命中不到的那些位置
#: 正是「提取器漏认」的位置（`FROM .db.dbo.apply_jobs` 这种前导点号是其中一种形状）。
#: 同样只在正则兜底路径使用。
_FROM_JOIN_RE = re.compile(r"\b(?:from|join)\b", re.IGNORECASE)

#: 规范化方言键 → sqlglot 方言名；方言不明（空串）走通用解析。
_DIALECT_BY_ENGINE = {"postgres": "postgres", "mysql": "mysql",
                      "sqlserver": "tsql", "clickhouse": "clickhouse"}

#: `FROM` / `JOIN` 右边直接以点号开头（`.db.dbo.t` / `..dbo.t`）—— 旧提取器认不出、
#: 按 fail-closed 拒；sqlglot 对这类畸形限定名过于宽容，这里保留旧口径（宁可误伤）。
_LEADING_DOT_REF_RE = re.compile(r"\b(?:from|join)\s+\.", re.IGNORECASE)

#: 表名归一后为空时的哨兵：绝不会出现在白名单里，于是必然被拒（认不出 ≠ 放行）。
_UNPARSEABLE_TABLE = "<无法解析的表名>"

#: `FROM` / `JOIN` 右边可接受的 AST 节点：普通表、派生表、括号连接组、LATERAL、VALUES。
#: 其余（`UNNEST` / 表函数等）不认识 → 回退正则，走旧的 fail-closed 口径。
_TABLE_SOURCE_NODES = (exp.Table, exp.Subquery, exp.Paren, exp.Lateral, exp.Values)


def _table_key(name: str) -> str:
    """把表名归一成「最后一段的小写」：`dbo.Jobs` / `[dbo].[jobs]` / `` `jobs` `` → `jobs`。

    **只比最后一段**是刻意的：清单里给的是限定名（`dbo.jobs`），而模型完全可能写不带 schema 的
    `jobs` —— 在默认 schema 下那是同一张表，判越权属误伤。

    ⚠️ 空段必须滤掉：`dbo.`（点后带空格被正则截断）归一成空串的话，会被下游的 `if t` 静默丢掉，
    于是**整条校验对这句 SQL 失效**（实测过：`FROM dbo. apply_jobs` 曾整个绕过）。

    滤空段只对**中间**那些段成立（`db..table` 是合法 T-SQL、空的是库名那段）：**末尾那个点**
    是另一回事 —— `dbo.` 后面那张表压根没被正则收进来，它一个表名段都没给出。这时回空串，
    交给 `_referenced_tables` 的哨兵兜住（回 `dbo` 会把 schema 名当表名，理由也说不清）。
    """
    cleaned = name.strip()
    for ch in "[]`\"":
        cleaned = cleaned.replace(ch, "")
    if cleaned.endswith("."):
        return ""
    parts = [p for p in cleaned.split(".") if p]
    return (parts[-1] if parts else "").strip().lower()


def _ast_referenced_tables(body: str, engine: str) -> set[str] | None:
    """用 sqlglot AST 取真实表；解析不了（或表源类型不认识）返回 `None`，由调用方回退正则。

    AST 才分得清「函数参数里的 `FROM`」与真正的表引用 —— 正则做不到这一点
    （`EXTRACT(MONTH FROM stat_date)` 会被误判成表 `stat_date`）。

    **认不出 ≠ 放行**：以下三种情况一律回退到正则兜底路径（旧口径本身是 fail-closed）：
    - 解析抛异常 / 解析结果为空；
    - `FROM` / `JOIN` 右边是 `_TABLE_SOURCE_NODES` 之外的节点（`UNNEST`、表函数等）；
    - 解析出空名表（`OPENROWSET(...)` / `generate_series(...)` 在 AST 里就是空名 `exp.Table`）。
    """
    if _LEADING_DOT_REF_RE.search(body):
        return None
    try:
        tree = sqlglot.parse_one(body, read=_DIALECT_BY_ENGINE.get(engine))
    except Exception:
        return None
    if tree is None:
        return None
    for source in (*tree.find_all(exp.From), *tree.find_all(exp.Join)):
        if not isinstance(source.this, _TABLE_SOURCE_NODES):
            return None
    cte_names = {
        str(cte.alias_or_name).lower()
        for cte in tree.find_all(exp.CTE)
        if cte.alias_or_name
    }
    found: set[str] = set()
    for table in tree.find_all(exp.Table):
        name = (table.name or "").strip()
        if not name:
            return None
        # CTE 名不能被 schema 限定：`public.apply_jobs` 里的 `apply_jobs` 是真表，
        # 不能因为同名的 CTE 存在就被剔掉（否则那句 SQL 的越权表会被静默放过）。
        qualified = bool((table.db or "").strip() or (table.catalog or "").strip())
        key = _table_key(name)
        if not qualified and key in cte_names:
            continue
        # 归一后是空串（如带引号但内容只有点号的畸形名）不能被静默丢掉 —— 丢了 = 这条 SQL 免检。
        found.add(key or _UNPARSEABLE_TABLE)
    return found


def _regex_referenced_tables(body: str) -> set[str]:
    """旧正则提取（**仅在 AST 解析失败时兜底**）：语句碰到的表名（归一后）。

    CTE 名不算表 —— 但**带点号的引用不算 CTE**。口径与改 AST 之前逐字节一致。
    """
    cte_names = {m.group(1).lower() for m in _CTE_RE.finditer(body)}
    found: set[str] = set()
    for m in _TABLE_RE.finditer(body):
        raw = m.group(1)
        if "." not in raw and _table_key(raw) in cte_names:
            continue
        key = _table_key(raw)
        found.add(key or _UNPARSEABLE_TABLE)
    return found


def _referenced_tables(body: str, engine: str = "") -> set[str]:
    """语句碰到的表名（归一后）。优先 AST；解析失败回退正则（旧口径）。"""
    ast_tables = _ast_referenced_tables(body, engine)
    if ast_tables is not None:
        return ast_tables
    return _regex_referenced_tables(body)


def _has_unparsed_table_reference(body: str) -> bool:
    """有没有哪个 `FROM` / `JOIN` 的右边是提取器**认不出来**的东西。

    **认不出 ≠ 放行** —— 有这种位置就说明表作用域校验对这句 SQL 是失效的，必须拒。

    合法语句里 `FROM` / `JOIN` 右边只可能是两种东西：一个能认出来的表引用（`_TABLE_RE` 命中），
    或派生表的左括号（`FROM (SELECT …) t`）。

    ⚠️ 判据是**逐点核对**，不是「归集出来的表集是不是空」：`WITH RECURSIVE r AS (…) SELECT * FROM r`
    的表集本来就是空的（`r` 是 CTE、不是表），但那属于**认出来了**那一档（`_TABLE_RE` 命中了、
    由 `_referenced_tables` 判成 CTE），不该被这道兜底误杀。
    """
    parsed = {m.start() for m in _TABLE_RE.finditer(body)}
    for m in _FROM_JOIN_RE.finditer(body):
        if m.start() in parsed:
            continue
        # `FROM (` = 派生表，不是表名 —— 与 `_TABLE_RE` 的 `(?!\()` 是同一个例外。
        if body[m.end():].lstrip().startswith("("):
            continue
        return True
    return False


#: `#` 出现即拒的方言 → 原因**按方言分开说**（同一个字符，方言里是两回事）：
#:
#: - MySQL / MariaDB：`#` 是**行注释**。危害是实的：末尾追加的 `LIMIT n` 会落在注释里，
#:   于是「报上限已设、实际一条都没限」——安全层不能有这个形状。
#: - SQL Server：`#` 是**临时表前缀**、不是注释。根因不成立，但临时表在我们这条链路里
#:   本来就建不出来（`create` / `into` 一律拒），一并拒掉没有损失。
#: - 方言不明（空串）：**也拒**。我们的兜底动作是**追加 `LIMIT n`**，赌错 = 上限静默丢失，
#:   而误伤只是用户换个问法。
#: - **PostgreSQL 故意不在表里**：`#>` / `#>>` / `#-` 是 JSONB 操作符
#:   （`SELECT data #>> '{a}' FROM t` 是合法只读查询），拦了是实打实的误伤。
_HASH_REJECT_REASONS = {
    "mysql": "不允许注释（`#` 是 MySQL / MariaDB 的行注释，会把注入的行数上限一起吞掉）",
    "sqlserver": "不允许注释（`#` 在 SQL Server 里是临时表前缀、不是注释；本链路不认带 `#` 的语句）",
    "": "不允许注释（方言不明：`#` 在部分方言里是行注释，会把注入的行数上限一起吞掉）",
}

#: 上限形态（枚举成常量，免得各处裸写字符串）。
_FORM_TOP = "top"                     # `SELECT TOP n`        —— SQL Server
_FORM_LIMIT = "limit"                 # `LIMIT n`             —— PG / MySQL
_FORM_LIMIT_OFFSET = "limit_offset"   # `LIMIT n OFFSET off`  —— PG / MySQL
_FORM_LIMIT_COMMA = "limit_comma"     # `LIMIT off, n`        —— MySQL / MariaDB

#: `SELECT TOP n`（`n` 收进 `count` 组）。末尾两个否定前瞻挡的是**行数语义不是条数**的 TOP
#: 修饰语：`TOP 5 PERCENT` 是 5% 行、`TOP 5 WITH TIES` 可能多于 5 行 —— 都不是「已限到 5 条」，
#: 当成已有上限就会放一批不该放的过去（改按无上限处理，让外层 TOP 兜）。
_TOP_RE = re.compile(
    r"^\s*select\s+top\s+(?P<count>\d+)\b(?!\s*percent\b|\s+with\s+ties\b)",
    re.IGNORECASE,
)

#: 末尾三种 `LIMIT` 写法，各自带**形态名**（形态名用来判「这写法是不是这个方言的」）。
#: 顺序 = 匹配优先级：逗号形态必须排在裸形态前面（`LIMIT 5, 10` 里的 `5` 是 offset 不是计数）。
#: 三种都把**计数**收进 `count` 命名组：收紧时只改这一段，offset 与其余文本一字不动。
_LIMIT_PATTERNS = (
    (_FORM_LIMIT_COMMA,
     re.compile(r"\blimit\s+(?P<offset>\d+)\s*,\s*(?P<count>\d+)\s*$", re.IGNORECASE)),
    (_FORM_LIMIT_OFFSET,
     re.compile(r"\blimit\s+(?P<count>\d+)\s+offset\s+(?P<offset>\d+)\s*$", re.IGNORECASE)),
    (_FORM_LIMIT,
     re.compile(r"\blimit\s+(?P<count>\d+)\s*$", re.IGNORECASE)),
)

#: 各方言**认**的上限形态。不在这张表里的写法 = 不是这个方言的语法 ——
#: 它既不能当「已有上限」（= 放行一条跑不了的 SQL），也不能在它后面再追加（= 拼出第二条上限）。
_NATIVE_CAP_FORMS = {
    "postgres": (_FORM_LIMIT, _FORM_LIMIT_OFFSET),
    "mysql": (_FORM_LIMIT, _FORM_LIMIT_OFFSET, _FORM_LIMIT_COMMA),
    "sqlserver": (_FORM_TOP,),
}

#: 方言不明时的兜底：按 LIMIT 系（我们的兜底动作本来就是追加 `LIMIT n`，
#: 而 `SELECT TOP n` 基本只有 SQL Server 这么写）。
_UNKNOWN_CAP_FORMS = (_FORM_LIMIT, _FORM_LIMIT_OFFSET, _FORM_LIMIT_COMMA)

#: 拒绝时给用户看的方言名（只用于拼句子，不参与判定）。
_DIALECT_LABELS = {"postgres": "PostgreSQL", "mysql": "MySQL", "sqlserver": "SQL Server"}


@dataclass(frozen=True)
class GuardResult:
    ok: bool
    sql: str
    reason: str = ""
    limit_applied: bool = False
    #: 拒绝细分（观测口径，D1）：`forbidden_pair` = 禁止同查；空串 = 其它安全闸。
    kind: str = ""


def _reject(sql: str, reason: str, kind: str = "") -> GuardResult:
    return GuardResult(ok=False, sql=sql, reason=reason, kind=kind)


def _form_is_native(form: str, engine: str) -> bool:
    """这个上限写法是不是**该方言的**语法。"""
    return form in _NATIVE_CAP_FORMS.get(engine, _UNKNOWN_CAP_FORMS)


def _wrong_dialect_reason(form: str, engine: str) -> str:
    """「模型写的上限不是这个方言的写法」→ 给一句能照着改的话（不猜、也不替它改写）。"""
    where = _DIALECT_LABELS.get(engine)
    if form == _FORM_TOP:
        if where is None:
            return "方言不明：`SELECT TOP n` 是 SQL Server 的写法，行数上限请写成 `LIMIT n`"
        return f"{where} 不支持 `SELECT TOP n`（SQL Server 的写法），行数上限请写成 `LIMIT n`"
    if engine == "sqlserver":
        # SQL Server 只认 TOP：`LIMIT` 在 T-SQL 里根本不是语法。
        return "SQL Server 不支持 `LIMIT …`（T-SQL 用 `SELECT TOP n …`），请改写"
    return (
        f"{where} 不支持 `LIMIT a, b`（那是 MySQL 的写法），"
        "请写成 `LIMIT b OFFSET a` 或 `LIMIT b`"
    )


def _find_declared_cap(body: str) -> tuple[str, re.Match] | None:
    """模型自己写的行数上限：返回 `(形态, 匹配)`；没有则 None。

    `TOP` 是行首形态、其余三种在末尾；一条语句本不该同时出现两个上限，
    真同时出现（病态输入）也只是先判 `TOP`。
    """
    top = _TOP_RE.search(body)
    if top is not None:
        return _FORM_TOP, top
    for form, pattern in _LIMIT_PATTERNS:
        m = pattern.search(body)
        if m is not None:
            return form, m
    return None


def _tighten_count(body: str, m: re.Match, max_rows: int) -> str:
    """只把 `count` 那一段数字换成 `max_rows`；offset / 大小写 / 空白一律原样。"""
    start, end = m.span("count")
    return f"{body[:start]}{max_rows}{body[end:]}"


def guard_sql(sql: str, *, engine: str, max_rows: int,
              allowed_tables: set[str] | None = None,
              forbidden_pairs=None, offset: int = 0) -> GuardResult:
    """校验并给 SQL 加上行数上限。

    `allowed_tables` 是**这次允许碰的表名集合**（可含限定名，比对时归一成最后一段）；
    `None` = 不做表作用域校验 —— 既有调用点与既有用例走的就是这一档，行为与加这个参数
    之前**逐字节相同**。

    `forbidden_pairs` 是「禁止同查」的表对（`ForbiddenPair` 列表，可为 None）。
    传了就在白名单校验之后判**两端是否同时出现**；`None` / 空 = 不校验，
    行为与加这个参数之前逐字节相同。

    Returns:
        `ok=True` 时 `sql` 是**可以执行**的那条（已带上限）；
        `ok=False` 时 `sql` 是**原样**，由调用方作为 `rejected_sql` 回给用户看
        —— 让人看见"模型想干什么"，比藏起来有用。
    """
    # 方言**先归一化**（`MySQL` / `  mysql ` / `mssql` 都落成规范键）。
    # 少了这一步，按方言的判据会被大小写与空白整个绕过 —— 评审实测：`engine="MySQL"` 时
    # `#` 那道闸形同不存在，放行出去的 SQL 里追加的 `LIMIT 100` 正好落在行注释里。
    engine_key = normalize_engine(engine)
    raw = (sql or "").strip()
    if not raw:
        return _reject(sql, "生成的 SQL 是空的")
    if ";" in raw.rstrip(";"):
        return _reject(sql, "只允许单条语句")
    body = raw.rstrip(";").strip()
    if "--" in body or "/*" in body:
        return _reject(sql, "不允许注释（它是拼接绕过的主要载体）")
    if "#" in body:
        hash_reason = _HASH_REJECT_REASONS.get(engine_key)
        if hash_reason is not None:
            return _reject(sql, hash_reason)
    if not re.match(r"^\s*(select|with)\b", body, re.IGNORECASE):
        return _reject(sql, "只允许 SELECT / WITH…SELECT")
    hit = _FORBIDDEN_RE.search(body)
    if hit:
        return _reject(sql, f"只允许只读查询，语句里出现了 `{hit.group(1)}`")
    # 表作用域：模型写它没被给的表 = **静默答错源**（实测它真的执行了、还返回了一个像样的数字），
    # 而「列错了」会由数据库响亮兜住（exec_failed）—— 两类失败的可见度差得远，所以先补表这一层。
    # `None` = 不校验（既有调用点与既有用例不受影响）。
    referenced = None
    ast_parsed = True
    if allowed_tables is not None or forbidden_pairs:
        referenced = _ast_referenced_tables(body, engine_key)
        if referenced is None:
            # AST 解析不了才回退正则；`ast_parsed` 决定下面那道「逐点核对」要不要跑 ——
            # 它本身就是正则实现，对函数参数里的 `FROM` 会误报。
            ast_parsed = False
            referenced = _regex_referenced_tables(body)
    # ⚠️ 禁止同查判定**排在白名单之前**：命中禁止对时，对端表通常也已被注入阶段剔出
    # 白名单（`related_tables` 里没有它）—— 白名单先判的话，用户看到的是
    # 「清单外的表: job_forward」，而真正可行动的成因是「这两张表同查会重复计数」。
    # 两条都违反而优先报禁止同查，不改变任何单条判定的通过/拒绝结果。
    if forbidden_pairs:
        hit = hit_forbidden(forbidden_pairs, referenced or set())
        if hit is not None:
            return _reject(
                sql,
                f"表 {hit.table_a} 与 {hit.table_b} 存在血缘派生关系，"
                "同一查询同时使用会重复计数（禁止同查）",
                kind="forbidden_pair",
            )
    if allowed_tables is not None:
        allowed = {_table_key(t) for t in allowed_tables if t}
        # 兜底（**仅正则路径**）：带 FROM/JOIN 却有认不出来的表引用 → 拒。正常语句不可能是这个形状；
        # 出现它只说明提取器没认出这句（`FROM .db.dbo.apply_jobs` 这种前导点号、或别的畸形写法），
        # 而「认不出」绝不能等于「放行」—— 那正是这道校验存在的意义。
        # AST 解析成功时不跑这道：它本身就是正则实现，会把 `EXTRACT(... FROM 列)` 误报成漏认。
        if not ast_parsed and _has_unparsed_table_reference(body):
            return _reject(sql, "语句里的表名解析不出来，无法确认它只查了给定的表")
        outside = sorted(referenced - allowed)
        if outside:
            return _reject(sql, "只允许查询给定的表，语句里出现了清单外的表：" + "、".join(outside))
    if offset not in (0, None):
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            return _reject(sql, "offset 必须是非负整数", kind="pagination")
        if engine_key == "sqlserver":
            return _reject(
                sql,
                "SQL Server 不支持自动 OFFSET 分页；请显式改写查询",
                kind="pagination",
            )
    result = _apply_limit(body, engine=engine_key, max_rows=max_rows, original=sql)
    if result.ok and offset:
        # `_apply_limit` 成功时 SQL 一定带 LIMIT（无论是新加的还是原有的）；
        # 在其后追加 OFFSET 在 postgres/mysql/clickhouse 都是合法语法。
        result = GuardResult(ok=True, sql=f"{result.sql} OFFSET {offset}",
                             limit_applied=result.limit_applied)
    return result


def _apply_limit(body: str, *, engine: str, max_rows: int, original: str) -> GuardResult:
    """按方言把行数上限落到 SQL 上。

    `engine` 是**归一化后**的规范键（`normalize_engine`：`postgres` / `mysql` / `sqlserver`，
    方言不明为 `""`）。

    - 模型已经写了上限、**且写法是该方言的**：不超就原样，超了就收紧（只改计数那一段）
    - 写法**不是该方言的**（给 SQL Server 写 `LIMIT 5`、给 PG 写 `LIMIT 5, 10` 或 `SELECT TOP 5`）：
      **拒** —— 当成已有上限 = 放行一条跑不了的 SQL，追加一个上限 = 拼出第二条上限，
      两条路都比让模型重写差（而这两条路的终点都是用户看到数据库原始语法错误）
    - 没有上限：PG/MySQL 末尾追加；SQL Server 包一层 `SELECT TOP n`
      —— 派生表里不允许 `ORDER BY`、也不允许 `WITH …`，这两种直接拒（与其猜，不如让模型重写）

    `original` 只用于拒绝时原样带回（调用方拿它当 `rejected_sql` 回给用户看），
    **不是** `body`（那份已经过 strip / 去尾分号）。
    """
    if engine == "sqlserver" and re.match(r"^\s*with\b", body, re.IGNORECASE):
        # `SELECT TOP n * FROM (WITH …) AS sub` 在 SQL Server 里**非法**（CTE 不能进派生表）。
        # 这里**不**去认「CTE 里是不是已经写了 TOP」：`_TOP_RE` 是行首锚定的、认不出藏在
        # `WITH` 后面的那个 TOP，更没法核对那个数字有没有超上限 —— 认不出就不能放行。
        return _reject(
            original,
            "SQL Server 下 `WITH …` 不能出现在子查询里（这里是自动包一层），"
            "请改写成 `SELECT TOP n …` 的写法",
        )

    declared = _find_declared_cap(body)
    if declared is not None:
        form, m = declared
        if not _form_is_native(form, engine):
            return _reject(original, _wrong_dialect_reason(form, engine))
        if int(m.group("count")) <= max_rows:
            return GuardResult(ok=True, sql=body, limit_applied=False)
        if form == _FORM_TOP:
            tightened = _TOP_RE.sub(f"SELECT TOP {max_rows}", body, count=1)
        else:
            tightened = _tighten_count(body, m, max_rows)
        return GuardResult(ok=True, sql=tightened, limit_applied=True)

    if engine == "sqlserver":
        if re.search(r"\border\s+by\b", body, re.IGNORECASE):
            # 子查询里的 ORDER BY 在 SQL Server 非法（没有 TOP/OFFSET 时）。
            # 与其猜模型想按什么排序，不如让它带着 TOP 重写一次。
            return _reject(
                original,
                "SQL Server 下请把行数上限写成 `SELECT TOP n …`（这里是自动包一层，"
                "而子查询不允许 ORDER BY）",
            )
        return GuardResult(
            ok=True, sql=f"SELECT TOP {max_rows} * FROM ({body}) AS _nl2sql_sub",
            limit_applied=True,
        )
    return GuardResult(ok=True, sql=f"{body} LIMIT {max_rows}", limit_applied=True)
