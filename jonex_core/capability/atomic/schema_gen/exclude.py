"""从本体里剔除框架表与备份表。

这些表进了本体不是「多了一点噪声」，而是会污染检索：用户问「有哪些表记录数据变更」，
`flyway_schema_history` 和 `t_x_bak_20260724` 会被当成正经答案返回。

**剔除但不静默**——每一张被排除的表连同理由写进 losses.md。
如果某张表其实该留下，看报告就能发现并改 EXCLUDE 配置。
"""

from __future__ import annotations

import re

# 迁移/版本管理的记账表。它们的「字段」是框架自己的元数据，不是业务概念
_FRAMEWORK = {
    "flyway_schema_history",
    "schema_migrations",
    "alembic_version",
    "databasechangelog",
    "databasechangeloglock",
    "sqllog",
}

# 手工备份与临时产物。带日期后缀的备份是这类库里最常见的脏东西
_BACKUP_PATTERNS = [
    re.compile(r"_bak$"),
    re.compile(r"_bak_\d{4,8}$"),
    re.compile(r"_backup(_\d{4,8})?$"),
    re.compile(r"_old$"),
    re.compile(r"_copy$"),
    re.compile(r"_tmp$"),
    re.compile(r"_\d{8}$"),  # 纯日期后缀，多为快照表
]


def exclusion_reason(table: str) -> str | None:
    """返回剔除理由，None 表示保留。"""
    if table.lower() in _FRAMEWORK:
        return "迁移/框架记账表"
    for p in _BACKUP_PATTERNS:
        if p.search(table.lower()):
            return "备份表/临时产物"
    return None
