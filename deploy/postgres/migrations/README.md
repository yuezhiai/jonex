# deploy/postgres/migrations

**只给全新库用**：这个目录挂到容器的 `/docker-entrypoint-initdb.d`，PostgreSQL 镜像在
**数据卷首次初始化**时按文件名字典序执行一次；存量库一个文件都不会自动跑（存量库走
[`../update/`](../update/README.md) 的增量）。

## 一句话：这里放的是「终态 schema」，不是「历史语句的回放」

2026-11-12 二次改造后，本目录由**权威库的 `pg_dump --schema-only`** 生成（权威库 =
跑过完整 `update/` 升级链的那个存量库，PG 15.19）：

- 每张表**只有一条** `CREATE TABLE IF NOT EXISTS`，终态列 / 类型 / 可空 / 默认值全写在表体里；
- 索引（`CREATE [UNIQUE] INDEX`）、约束（`ALTER TABLE ... ADD CONSTRAINT`）、外键、视图各自独立成条；
- **没有**「先建表、后面再 `ALTER TABLE ... ADD COLUMN` 补丁」的历史堆叠，也**没有**
  「建表时叫 `concepts`、后面 `RENAME COLUMN` 成 `schema`」这种顺序依赖；
- 注释统一在 `091_comments.sql`，种子与「种子之后的数据修正」统一在 `090_seed_data.sql`。

**改表结构就改这里的表体**（不是加 `ALTER` 补丁），改完**必须**跑下面「验证」一节。

## 文件布局

一个库（schema）一个文件，种子与注释固定收尾，中间留出编号空间：

| 文件 | 内容 |
| --- | --- |
| `001_schemas.sql` | 8 个 schema：platform / knowledge_base / business_domain / task_center / metering / monitor / data_governance / ontology_compile |
| `002_platform.sql` | `platform`：登录 / RBAC / 菜单 / 租户 / 审计 / 应用注册 / 身份同步 |
| `003_knowledge_base.sql` | `knowledge_base`：KB / 文档 / 检索 / 数据源 / 结构化与本体 / 演化台 |
| `004_business_domain.sql` | `business_domain`：空间 / 服务 / 引擎 / 适配器 / 技能 / 模板 |
| `005_task_center.sql` | `task_center`：任务定义 / 版本 / 触发 / 运行 / 子任务 / 运行器 |
| `006_metering.sql` | `metering`：LLM 用量明细与日汇总（含 4 个视图） |
| `007_monitor.sql` | `monitor`：调用链与尝试记录 |
| `008_data_governance.sql` | `data_governance`：标准资产 / 合同 / 质量 / 落标绑定 |
| `010_ontology_compile.sql` | 本体编译（compiled schema）子系统，表同属 `ontology_compile` |
| `090_seed_data.sql` | 种子数据 + **种子之后的数据修正**（手写，不参与 dump 生成） |
| `091_comments.sql` | 表 / 列 / schema 注释（由 dump 重新生成） |

规则：

- **011–089 留给以后的库**。新库文件按号段往后排，种子/注释固定停在 090/091。
- **顺序即语义**：`090_seed_data.sql` 必须晚于全部 DDL。它除了种子 `INSERT`，还收着
  原本排在种子之后执行的数据修正（`structured_enabled` 置真、菜单收尾、任务中心预置层、
  `data_access_methods` 的 `dam_database` 等）—— 提到种子之前会静默失效（2026-11-12
  实测：`t` 变 `f`、菜单少一行）。
- **外键集中放文件末尾**：dump 把所有 `FOREIGN KEY` 排在最后，这里保持同样做法；跨文件的
  外键统一落在 `010_ontology_compile.sql` 末尾（那时所有表都已存在）。
- 事务（`BEGIN;` / `COMMIT;`）在每个文件内**自平衡**；`090` 的「种子之后」区每块各自包裹。

## 重新生成（权威库变了的时候）

生成器已入库：`deploy/postgres/tools/gen_terminal_ddl.py`（一次性维护脚本，不是运行期依赖）。
它把 `pg_dump --schema-only` 的输出按 schema 拆成 `001–008/010`，外键集中到文件末尾，
`COMMENT ON` 归 `091`，并做三件事：加 `IF NOT EXISTS`、视图改 `DROP VIEW + CREATE`、
应用下面的「漂移排除」清单。

```bash
# 1) 导权威库 schema（PG 版本要和部署一致）
pg_dump --schema-only --no-owner --no-privileges -h <host> -U jonex -d jonex > /tmp/prod_schema.sql

# 2) 先输出到临时目录核对
python deploy/postgres/tools/gen_terminal_ddl.py --dump /tmp/prod_schema.sql --out /tmp/gen_out --write

# 3) 覆盖（009/090 是手写的，不要覆盖）+ 验证
cp -f /tmp/gen_out/00*.sql /tmp/gen_out/010*.sql /tmp/gen_out/091*.sql deploy/postgres/migrations/
bash deploy/postgres/verify_schema_parity.sh
```

## 幂等性

`CREATE SCHEMA/TABLE/SEQUENCE/INDEX` 一律带 `IF NOT EXISTS`；视图用 `DROP VIEW IF EXISTS`
+ `CREATE VIEW`（PostgreSQL 没有 `CREATE VIEW IF NOT EXISTS`）。

约束（`ALTER TABLE ... ADD CONSTRAINT`）与种子 `INSERT` 不保证重跑无错 —— 建库只跑一次，
存量库迭代走 `update/`。别把「重复执行不报错」当成这里的验收标准。

## 验证（改完必须跑）

```bash
# 1) 结构一致性：临时库跑一遍 migrations/，与目标库逐项比列/约束/索引/视图
bash deploy/postgres/verify_schema_parity.sh            # 与本机 jonex 比
bash deploy/postgres/verify_schema_parity.sh --keep     # 保留临时库便于排查

# 2) 守门测试
.venv/bin/python -m pytest -q \
  tests/unit/test_orm_table_migration_coverage.py \
  tests/unit/test_migration_terminal_ddl.py \
  tests/unit/test_deploy_migration_mirror.py
```

`verify_schema_parity.sh` 是**唯一权威判据**：它真建一个库、真跑这 12 份文件、再把
`information_schema.columns` / `pg_constraint` / `pg_index` / 视图定义逐条比对，差异非 0
就退非 0。历史教训：只靠「读文件」「起服务看崩溃」都不够 —— 曾连着三轮分别漏了 7 张表、
28 个列、12 个索引 + 4 个 CHECK，都是真建库才发现的。

守门测试分工：

- `test_orm_table_migration_coverage.py`：每个（带 schema 的）ORM 模型表、每个 ORM 列、每个
  裸 SQL 模块级列清单常量（如 `ontology_compile/task_store._COLUMNS`），都必须在本目录
  的 DDL 里出现，否则全新库第一次读库就 500。
- `test_migration_terminal_ddl.py`：`update/` 单元里建过/改过的表、加过的列、建过的索引，
  在终态 DDL 里都必须存在；事务成对、DDL 幂等、无历史补丁块。
- `test_deploy_migration_mirror.py`：本目录建的每张表，存量库升级路径上也必须建得出来。

## 加新表时两条路都要改

1. 全新库：把 DDL 写进对应库文件的表体（`IF NOT EXISTS`），索引/约束紧跟其后；
2. 存量库：在 `update/` 里加增量（历史 91 份已于 2026-11-12 删除，`update/` 现在只有
   `apply.sh` 和新增量；新增量命名 `<14 位时间戳>_<描述>.sql`，如
   `20261112160612_add_kb_archive_flag.sql`）。

只改 `update/` 是本项目最常见的事故来源：全新库会缺表/缺列/缺索引，现场表现为服务起来
就崩 `relation ... does not exist` / `column ... does not exist`。

## 生产库漂移：有 3 项**故意不纳入**建库脚本

生成源是生产库，但生产库上有三处**脚本链里从来没有过**的东西（人工加的），纳入会让全新库
比升级库更严（同一份数据在新库插得进去、在升级库插不进去，或反过来），所以排除：

| 漂移项 | 生产库 | 脚本口径 | 依据 |
| --- | --- | --- | --- |
| `knowledge_base.uq_space_name_tenant` | 有 | 无 | 旧 25 份 `migrations/` 与 `update/` 全链路都没有 |
| `business_domain.uq_prompt_template_name_space` | 有 | 无 | 同上 |
| `business_domain.prompt_templates.current_version` 默认值 | `'1.0'` | `'1'` | 旧 `005_business_domain.sql` 里是 `DEFAULT '1'` |

结论：`verify_schema_parity.sh` 的比对基准是**升级链建出的库**（`jonex`），不是生产库；
与生产库比会稳定多这 2 个索引、差这 1 个默认值。要把这 2 个索引收编，就另开一个
`update/` 增量单元（幂等 `CREATE UNIQUE INDEX IF NOT EXISTS`），两处同时补，再更新本表。

## 历史：为什么不再「建表 + 末尾补丁块」

2026-11-12 上午的第一版改造把 25 份历史脚本按库归并，缺的对象补成文件末尾的
`-- >>> 并入 <原文件名>` 补丁块。结果一天内被现场抓了三轮：

| 轮次 | 判据 | 抓到的缺口 |
| --- | --- | --- |
| 1 | 与旧 25 份布局等价 | 7 张表（`kb_tools` / `task_center_documents` / `compile_tasks` …） |
| 2 | ORM 模型 vs 建库 DDL | 12 表 28 列（`ingest_backend` / `last_report` / `error_params` …） |
| 3 | 升级链库 vs 新库（列/索引/约束） | `compile_tasks.content_generation` + 12 个索引 + 4 个 CHECK |
| 4 | 数据行 | `data_access_methods.dam_database`（其余 3 张表的差异是生产库运行期数据） |

根因不是「补得不够多」，而是**表示法错**：initdb 脚本应当描述**终态**，却被拿来堆放
「历史增量语句」。补丁必须落在「建这张表的那个文件里」、`DO` 块要按内容判 DDL/DML、
部分唯一索引让 `ON CONFLICT` 目标推不出来 …… 顺序敏感性一路累积。现在改成 dump 生成
终态 DDL，`ALTER` 补丁块归零（`003_knowledge_base.sql` 从 3829 行降到 1679 行）。

## 手工执行

```bash
cd deploy/postgres
psql -U jonex -d jonex -f init.sql        # init.sql 用 \ir 按上表顺序逐份引入

# 单份（幂等，可重复跑）
psql -U jonex -d jonex -f migrations/003_knowledge_base.sql
```

## 旧文件名 → 现文件（读历史文档时对照）

终态 DDL 之后，「某份历史脚本的内容」这个概念已不存在；下表只用于读懂旧文档/旧 issue：

| 旧 | 现 |
| --- | --- |
| `004_knowledge_base.sql` / `009_structured_data.sql` / `012` / `016` / `019_document_versions` / `020` / `033`–`038` | `003_knowledge_base.sql` |
| `005_business_domain.sql` / `015` / `017` | `004_business_domain.sql` |
| `006_seed_data.sql` / `018_template_scenarios_menu.sql` | `090_seed_data.sql` |
| `007_comments.sql` | `091_comments.sql` |
| `008_task_center.sql` | `005_task_center.sql` |
| `010_data_governance.sql` / `019_data_governance_assets.sql` | `008_data_governance.sql` |

`tests/unit/migration_sections.py` 的 `section("<旧文件名>")` 会按 `LEGACY_TO_FILE` 返回
映射到的整份库文件（旧测试按旧名取内容时不至于消失）。
