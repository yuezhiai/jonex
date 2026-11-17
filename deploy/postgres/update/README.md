# postgres/update —— 存量库增量 DDL

存量库的表结构变更都落在这个目录，由 `apply.sh`（`make db-migrate`）人工触发执行。

```text
update/
├── apply.sh                      # 执行器：发现单元 → 按单元登记 → 按单元执行
├── README.md
└── 20261112160612_描述.sql       # 新增的独立增量：<14 位时间戳>_<描述>.sql
```

**2026-11-12 起历史基线文件已删除**：原先 91 份历史增量脚本被合并成一个
`000_baseline.sql`（段标记分隔），这批脚本在**所有现有环境都已执行过**；全新库由
`deploy/postgres/migrations/` 的 12 份终态 DDL 一次建好，不需要再回放历史。
仓库里继续留着它只有坏处：任何人看到 91 个段都会以为还需要跑，而实际跑一遍是纯浪费
（其中 62 处无 `ON CONFLICT` 的种子 `INSERT` 还可能撞主键）。

## 命名约定

新增增量一律用 **「14 位时间戳 + 描述」**，例如
`20261112160612_add_kb_archive_flag.sql`（时间戳 = `YYYYMMDDHHMMSS`）。

- `apply.sh` 的执行顺序 = **文件名字典序**；14 位时间戳的字典序天然就是时间顺序，
  不再需要「编号必须大于 107」这种人工约定；
- 同一批变更用同一时间戳前缀，文件名整体仍是字典序；
- 老交付包里遗留的 `NNN_*.sql` 仍能执行，但 `apply.sh` 会打印 `WARN` 提示改成时间戳命名；
- `update/` 下**最多**允许一个 `*_baseline.sql`（兼容历史合并体，当前为 0 个）。

## 新增一个增量

1. 新建 `update/<YYYYMMDDHHMMSS>_<描述>.sql`（`date +%Y%m%d%H%M%S` 取当前时间）；
2. 文件自带 `BEGIN; ... COMMIT;`，全部 DDL 用 `CREATE ... IF NOT EXISTS` /
   `ALTER ... ADD COLUMN IF NOT EXISTS` / `ON CONFLICT DO NOTHING` 保持幂等；
3. 同一份结构变更**必须**也补进 `migrations/`——但**不是**逐字节复制本单元：
   `migrations/` 是按权威库终态生成的建库脚本，要补的是**终态写法**
   （新表写成表体；新列写进 `CREATE TABLE` 表体，真要 `ALTER` 就紧跟建表之后；
   新索引/约束各自一条）。落位见 `../migrations/README.md`；
4. 跑下面三条，缺一样都会在现场表现为服务起来就 `relation/column ... does not exist`：

   ```bash
   bash deploy/postgres/update/apply.sh --dry-run        # 待应用清单符合预期
   bash deploy/postgres/verify_schema_parity.sh          # 新库 vs 升级库：列/约束/索引/视图 0 差异
   .venv/bin/python -m pytest -q tests/unit/test_deploy_migration_mirror.py \
       tests/unit/test_migration_terminal_ddl.py tests/unit/test_orm_table_migration_coverage.py
   ```

## 版本表与执行语义

状态记录在 `public.schema_migrations(filename varchar(255) primary key, applied_at timestamptz)`：

| 行为 | 语义 |
| --- | --- |
| 单元名 | 文件名（基线时代是段名 = 原脚本文件名） |
| 已应用判断 | `SELECT filename` 读全量 → 逐个比对，命中跳过 |
| 登记时机 | **执行成功才登记**；失败不登记、`exit 1`，修好后重跑会重试该单元 |
| 幂等写法 | `INSERT ... ON CONFLICT (filename) DO NOTHING` |
| 失败语义 | `psql -v ON_ERROR_STOP=1` + 文件自带事务 ⇒ 整个文件回滚，不留半截 |
| `--dry-run` | 不执行、不登记（但会创建这张空表） |
| `--only <name>` | 忽略登记状态强制重跑该单元 |
| 结构前提豁免 | 少数历史脚本依赖的旧结构已被删除，`apply.sh` 会按库的真实结构判定并「登记为已应用但跳过」；`JONEX_PG_DISABLE_STRUCTURAL_SKIP=1` 可关掉 |
| 全新库豁免 | `JONEX_PG_SKIP_FILE` 指定的清单同样「登记但跳过」 |

⚠️ 没有 advisory lock / 文件锁：**不要两个人同时跑**。

注意：`initdb` 走 `migrations/` 建库的路径**不写** `public.schema_migrations`，
所以全新库第一次 `make db-migrate` 时这张表还不存在（会被创建成空表）。

## 常用命令

```bash
# 预览待应用单元（不执行、不登记）
make db-migrate-dry

# 交互确认后执行
make db-migrate

# 跳过确认直接执行（CI/自动化）
make db-migrate-yes

# 单独补跑某个单元（已登记也照跑）
bash deploy/postgres/update/apply.sh --only 20261112160612_add_kb_archive_flag.sql --yes
```

`update/` 为空（当前就是没有新增量）时，`apply.sh` 打印
「没有迁移脚本」并以 0 退出 —— 「无事可做」不是错误。

## 如果以后又要把历史脚本合并回来

`apply.sh` 保留了对「单文件 + 分段」的兼容：把合并体放进 `update/*_baseline.sql`
（文件名不限，`000_baseline.sql` / `20261112160612_baseline.sql` 都认），
段内用下列标记，脚本会**先按段执行、按段登记**（登记名 = 段名 = 原脚本文件名），
再按字典序跑独立增量脚本。

```sql
-- >>> SECTION: 080_task_center_drop_catalog.sql
...该脚本原文，逐字节...
-- <<< SECTION: 080_task_center_drop_catalog.sql
```

## 与代码注释的关系

业务代码里「建列脚本：`deploy/postgres/update/050_task_source.sql`」这类历史注释，
指的是**已删除的历史基线里的同名段**——它们对应的结构现在都在 `migrations/` 的终态 DDL 里，
注释只是溯源线索，不要按它去找文件。
