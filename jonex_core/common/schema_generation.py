#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""schema 行的进程内「代」计数器 —— P1 版本化读取的**缓存失效信号**。

为什么不能只靠 `schema_version`：② 的 schema 行缓存按 `(version_id, schema_version)`
换键，但**写入方不一定推进 active 版本** —— 画布保存、回滚写回、演化采纳的 PUT
都只改行；而演化基准是「父臂先问（填缓存）→ 写库 → 候选臂再问」，靠 TTL 等过期
会让候选臂在窗口内仍读到**写前**的行 ⇒ 候选 == 父 ⇒ 一个根本没被测量的补丁也能过关。

契约：任何**成功**的 schema 行写入（`OntologyAbstractSchemaRepository.upsert`、
`OntologySchemaRepository.upsert_compiled_schema` / `set_evidence`，各自含全部成功
返回路径）都调 `bump_schema_generation()`；② 读侧把**未命中那一刻采样**的
`schema_generation()` 放进缓存键 ⇒ **本进程**的写入立刻换键、下一次读取必重读。

关于「回滚」的两点澄清（别读成「回滚不推进」）：
- **演化回滚（业务回滚）** = 把旧内容写回行，走的就是上面两个 upsert ⇒ **会**推进 ——
  这正是要让 ② 重读旧内容的场景；
- **数据库事务回滚**（写成功但 commit 前失败）会留下一次**无害的多推**：bump 已发生、
  行没落库 ⇒ 只是让 ② 多读一次库。方向安全（宁可多读，不可读旧）。
  真正的冲突分支（1003 / CAS 未命中）在 bump **之前** raise，不推进。

⚠️ 进程内计数器（每 worker 一份）：同一 worker 的写后读立刻可见；别的 worker 的写入
要等该 worker 的下一次读（那时缓存键仍是旧代，最多滞后 TTL）。跨进程精确失效要
Redis 广播，超出本迭代 —— 不要把它当成跨进程一致性保证。
"""
from __future__ import annotations

import threading

_lock = threading.Lock()
_generation = 0


def schema_generation() -> int:
    """当前代号（只增不减）。"""
    with _lock:
        return _generation


def bump_schema_generation() -> int:
    """推进一代并返回新值 —— **每次成功的 schema 行写入**调用一次。"""
    global _generation
    with _lock:
        _generation += 1
        return _generation


def reset_schema_generation() -> None:
    """归零（**仅测试用**：模块级状态会跨用例活着）。"""
    global _generation
    with _lock:
        _generation = 0


__all__ = [
    "bump_schema_generation",
    "reset_schema_generation",
    "schema_generation",
]
