"""按「生产库终态」重新生成 deploy/postgres/migrations/001–010 + 091。

用法（一次性维护脚本，不是运行期依赖）::

    # 1) 从权威库导 schema（PG 版本与目标库保持一致，建议用 postgres 容器里的 pg_dump）
    pg_dump --schema-only --no-owner --no-privileges -h <host> -U jonex -d jonex > /tmp/prod_schema.sql

    # 2) 先生成到临时目录，肉眼/脚本核对后再覆盖正式目录
    python deploy/postgres/tools/gen_terminal_ddl.py --dump /tmp/prod_schema.sql --out /tmp/gen_out

    # 3) 覆盖 + 验证
    cp -f /tmp/gen_out/*.sql deploy/postgres/migrations/     # 009/090 是手写的，别覆盖
    bash deploy/postgres/verify_schema_parity.sh

真源：`pg_dump --schema-only` 导出的权威库 schema（跑过完整 `update/` 升级链的那个库，PG 15.19）。

与上一版（历史语句回放）的区别：这里每个对象只有**一条终态语句** —— 建表体自带终态列/默认值，
索引、约束、注释各自独立成条，不再有 `ALTER TABLE ... ADD COLUMN` 补丁块，也不再有
「建表时是 concepts、后面改名成 schema」这种顺序依赖。

分文件规则（沿用已批准的布局）：
  001 schemas ｜ 002 platform ｜ 003 knowledge_base ｜ 004 business_domain ｜ 005 task_center
  006 metering（含视图）｜ 007 monitor ｜ 008 data_governance
  010 ontology_compile ｜ 090 seed（保留，不动）｜ 091 comments（由 dump 重新生成）

排除：`jonex_wiki` / `mes_demo` / `test_mcp01` / `test_mcp02` / `monitor123`（外部引擎或测试
手工建的 schema）、`public`（lightrag 运行时表 + apply.sh 的 schema_migrations）、
`*_bak_*` / `*_archive` / `*_pre_*` 备份表（升级过程产物，全新库不该有）。
"""
import argparse
import pathlib
import re

DUMP = pathlib.Path("/tmp/remote_schema.sql")
OUT = pathlib.Path("deploy/postgres/migrations")

LIB = {
    "platform": ("002_platform.sql", "平台库：登录、RBAC、菜单、应用注册、审计、MCP 密钥"),
    "knowledge_base": ("003_knowledge_base.sql", "知识库库：知识库、文档状态机、结构化/本体、检索事件"),
    "business_domain": ("004_business_domain.sql", "业务域库：领域空间、服务、引擎、适配器、技能、模板"),
    "task_center": ("005_task_center.sql", "任务中心库：定义/版本/触发、任务与子任务调度"),
    "metering": ("006_metering.sql", "计量库：调用明细与用量视图"),
    "monitor": ("007_monitor.sql", "监控库：调用链与尝试记录"),
    "data_governance": ("008_data_governance.sql", "数据治理库：资产、标准、质量、保留策略"),
    "ontology_compile": ("010_ontology_compile.sql", "本体编译库：编译任务与知识库级编译快照"),
}
SCHEMAS_FILE = ("001_schemas.sql", "建库：全部 schema")
COMMENTS_FILE = ("091_comments.sql", "全部 COMMENT ON（表/列注释）")

DROP_SCHEMAS = {"jonex_wiki", "mes_demo", "test_mcp01", "test_mcp02", "monitor123", "public"}
DROP_TABLE_PAT = re.compile(r"_bak_\d+$|^mcp_keys_pre_|^mcp_write_keys_archive$")

CHUNK_RE = re.compile(r"^--\n-- Name: .*?$\n--\n", re.M)

# 生产库里**不是脚本建的**对象（人工加过），不纳入建库脚本，否则全新库会比升级库更严：
#   * uq_space_name_tenant / uq_prompt_template_name_space —— 旧 25 份 migrations 与
#     update/ 全链路都没有，生产库上被单独加过唯一索引；
#   * prompt_templates.current_version 默认值被手工改成 '1.0'，脚本口径是 '1'。
# 这属于生产库漂移，脚本保持与升级链一致；要收编请另开 update/ 增量单元。
EXCLUDE_INDEXES = {"uq_space_name_tenant", "uq_prompt_template_name_space"}
EXCLUDE_INDEX_TABLES_PREFIX = ()
TEXT_OVERRIDES = [
    ("current_version character varying(32) DEFAULT '1.0'::character varying NOT NULL",
     "current_version character varying(32) DEFAULT '1'::character varying NOT NULL"),
]


def strip_preamble(text: str) -> str:
    lines = [ln for ln in text.splitlines()
             if not ln.startswith("\\restrict") and not ln.startswith("\\unrestrict")
             and not ln.startswith("SET ") and not ln.startswith("SELECT pg_catalog.set_config")
             and not ln.startswith("-- Dumped")]
    return "\n".join(lines)


def split_chunks(text: str):
    """按 pg_dump 的 `-- Name: ...; Type: ...` 头切块，返回 [(header, body)]。"""
    marks = list(CHUNK_RE.finditer(text))
    out = []
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        body = text[m.end():end].strip()
        header = m.group(0)
        if body:
            out.append((header, body))
    return out


def schema_of(header: str, body: str) -> str | None:
    m = re.search(r"Schema: ([a-z_0-9]+);", header)
    if m:
        return m.group(1)
    m = re.search(r"\b(?:TABLE|INDEX|SEQUENCE|VIEW|CONSTRAINT|COLUMN|DEFAULT)\s+[a-z_0-9]+\s+"
                  r"[a-z_0-9]+; Type: ([A-Z ]+); Schema: ([a-z_0-9]+);", header)
    if m:
        return m.group(2)
    m = re.match(r"CREATE (?:TABLE|SEQUENCE|VIEW|MATERIALIZED VIEW|UNIQUE INDEX|INDEX)\s+"
                 r"(?:IF NOT EXISTS\s+)?([a-z_0-9]+)\.", body, re.I)
    if m:
        return m.group(1)
    m = re.search(r"\bON\s+([a-z_0-9]+)\.[a-z_0-9]+", body)
    if m:
        return m.group(1)
    m = re.match(r"ALTER TABLE\s+([a-z_0-9]+)\.", body, re.I)
    if m:
        return m.group(1)
    return None


def table_of(body: str) -> str | None:
    ine = r"(?:IF NOT EXISTS\s+)?"
    for pat in (
        r"CREATE TABLE\s+" + ine + r"([a-z_0-9]+\.[a-z_0-9]+)",
        r"ALTER TABLE(?:\s+ONLY)?\s+([a-z_0-9]+\.[a-z_0-9]+)",
        r"CREATE (?:UNIQUE )?INDEX\s+" + ine + r"[a-z_0-9]+\s+ON\s+([a-z_0-9]+\.[a-z_0-9]+)",
        r"COMMENT ON (?:TABLE|COLUMN)\s+([a-z_0-9]+\.[a-z_0-9]+)",
        r"CREATE SEQUENCE\s+" + ine + r"([a-z_0-9]+\.[a-z_0-9]+)",
        r"ALTER SEQUENCE\s+([a-z_0-9]+\.[a-z_0-9]+)",
        r"CREATE VIEW\s+" + ine + r"([a-z_0-9]+\.[a-z_0-9]+)",
    ):
        m = re.match(pat, body, re.I)
        if m:
            return m.group(1)
    return None


def is_dropped_table(body: str, header: str) -> bool:
    name = table_of(body) or ""
    if not name:
        return False
    table = name.split(".")[-1]
    return bool(DROP_TABLE_PAT.search(table))


IDEMPOTENT = [
    (re.compile(r"^CREATE SCHEMA (?!IF NOT EXISTS)", re.M), "CREATE SCHEMA IF NOT EXISTS "),
    (re.compile(r"^CREATE TABLE (?!IF NOT EXISTS)", re.M), "CREATE TABLE IF NOT EXISTS "),
    (re.compile(r"^CREATE UNIQUE INDEX (?!IF NOT EXISTS)", re.M), "CREATE UNIQUE INDEX IF NOT EXISTS "),
    (re.compile(r"^CREATE INDEX (?!IF NOT EXISTS)", re.M), "CREATE INDEX IF NOT EXISTS "),
    (re.compile(r"^CREATE SEQUENCE (?!IF NOT EXISTS)", re.M), "CREATE SEQUENCE IF NOT EXISTS "),
]


def make_idempotent(body: str) -> str:
    """建库脚本要能重跑：CREATE 补 IF NOT EXISTS（pg_dump 不带），视图则先 DROP 再建
    —— PostgreSQL 没有 `CREATE VIEW IF NOT EXISTS`。"""
    if body.startswith("CREATE VIEW ") or body.startswith("CREATE MATERIALIZED VIEW "):
        m = re.match(r"CREATE (MATERIALIZED )?VIEW\s+(?:IF NOT EXISTS\s+)?([a-z_0-9]+\.[a-z_0-9]+)", body, re.I)
        kind = "MATERIALIZED VIEW" if m and m.group(1) else "VIEW"
        return f"DROP {kind} IF EXISTS {m.group(2)};\n\n{body}" if m else body
    for pat, repl in IDEMPOTENT:
        body = pat.sub(repl, body)
    return body


def make_overrides(body: str) -> str:
    for old, new in TEXT_OVERRIDES:
        body = body.replace(old, new)
    return body


def main(write: bool):
    text = strip_preamble(DUMP.read_text())
    chunks = split_chunks(text)
    print(f"dump 切出 {len(chunks)} 块")

    files: dict[str, list[str]] = {}
    schemas: list[str] = []
    comments: list[str] = []
    fks: list[tuple[str, str]] = []
    skipped = {"schema": 0, "table": 0, "other": 0}

    for header, body in chunks:
        body = make_idempotent(body)
        schema = schema_of(header, body)
        if body.startswith("CREATE SCHEMA"):
            name = re.match(r"CREATE SCHEMA(?:\s+IF NOT EXISTS)?\s+([a-z_0-9]+)", body, re.I) \
                .group(1).lower()
            if name in DROP_SCHEMAS:
                skipped["schema"] += 1
                continue
            schemas.append(body)
            continue
        if schema in DROP_SCHEMAS or schema is None:
            skipped["schema" if schema is None else "schema"] += 1
            continue
        if is_dropped_table(body, header):
            skipped["table"] += 1
            continue

        m_idx = re.match(r"CREATE (?:UNIQUE )?INDEX (?:IF NOT EXISTS )?([a-z_0-9]+)", body)
        if m_idx and m_idx.group(1) in EXCLUDE_INDEXES:
            skipped["manual"] = skipped.get("manual", 0) + 1
            continue
        if body.startswith("COMMENT ON SCHEMA"):
            m = re.match(r"COMMENT ON SCHEMA\s+([a-z_0-9]+)", body)
            if m and m.group(1) in DROP_SCHEMAS:
                skipped["schema"] += 1
                continue
            comments.append(body)
            continue
        if body.startswith("COMMENT ON"):
            comments.append(body)
            continue
        target = LIB.get(schema)
        if not target:
            skipped["other"] += 1
            print("  ? 未归类的 schema:", schema, body.splitlines()[0][:80])
            continue
        if re.search(r"\bADD CONSTRAINT\b.*\bFOREIGN KEY\b", body, re.S):
            fks.append((target[0], body))
            continue
        files.setdefault(target[0], []).append(body)

    if not write:
        print("（dry-run，不写文件）")
        return

    # 文件头只留标题：来源/生成方式/验证方法统一写在 README.md，别在每份文件里重复一遍。
    HEADER = ("-- ============================================================================\n"
              "-- {title}\n"
              "-- ============================================================================\n\n")

    def write_file(name, title, body_lines):
        body_lines = [make_overrides(b) for b in body_lines]
        path = OUT / name
        path.write_text(HEADER.format(title=title) + "\n\n".join(body_lines) + "\n")
        print(f"  写入 {name}: {len(body_lines)} 条语句, {len(path.read_text().splitlines())} 行")

    write_file(*SCHEMAS_FILE, schemas)
    for schema, (name, title) in LIB.items():
        if name in files:
            write_file(name, title, files[name])
    # 外键：优先放「引用方表」所在文件末尾；若被引用的表在更后面的文件里，就统一放到最后一个
    # 库文件（010）的末尾——那里所有表都已建好，顺序永远成立。
    order = [n for n, _ in LIB.values()]
    idx = {name: i for i, name in enumerate(order)}
    last = order[-1]
    per_file: dict[str, list[str]] = {}
    for filename, body in fks:
        ref = re.search(r"REFERENCES\s+([a-z_0-9]+)\.", body)
        ref_schema = ref.group(1) if ref else None
        target = filename
        if ref_schema and ref_schema in LIB:
            if idx[LIB[ref_schema][0]] > idx[filename]:
                target = last
        per_file.setdefault(target, []).append(body)
    for filename, bodies in per_file.items():
        path = OUT / filename
        path.write_text(path.read_text().rstrip("\n")
                        + "\n\n-- 外键约束（dump 把外键统一放在末尾，这里保持同样的做法，避免建表顺序问题）\n\n"
                        + "\n\n".join(bodies) + "\n")
        print(f"  追加外键到 {filename}: {len(bodies)} 条")
    write_file(*COMMENTS_FILE, comments)
    print(f"跳过：schema {skipped['schema']}、备份表 {skipped['table']}、其它 {skipped['other']}")


def cli() -> None:
    global DUMP, OUT
    parser = argparse.ArgumentParser(description="按权威库 schema dump 重新生成 migrations 终态 DDL")
    parser.add_argument("--dump", required=True, help="pg_dump --schema-only 的输出文件")
    parser.add_argument("--out", required=True, help="输出目录（建议先用临时目录）")
    parser.add_argument("--write", action="store_true", help="真正写文件；不加只做 dry-run")
    args = parser.parse_args()
    DUMP = pathlib.Path(args.dump)
    OUT = pathlib.Path(args.out)
    OUT.mkdir(parents=True, exist_ok=True)
    main(write=args.write)


if __name__ == "__main__":
    cli()
