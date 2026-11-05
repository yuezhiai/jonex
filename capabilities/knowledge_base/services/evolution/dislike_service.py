# -*- coding:utf-8 -*-
"""演化台服务化 ②b Task 3：点踩诊断 / 抽象层选项 / 采纳写入。

从已退役的本地桥接（宿主 CLI 版，代码已删除）的 `/dislikes`、
`/dislikes/{id}/context`、`/dislikes/{id}/replay`、`/dislikes/{id}/adopt-alias`、
`/metric-options`、`/concept-options`、`/failures/adopt-metric` 平移。三条口径原样保留：

- **读取器的 SQL 与 slim 形状**逐字对齐 `tests/eval/nl2sql/diagnose.py` 的
  `_DISLIKES_SQL` / `_FEEDBACK_BY_ID_SQL` / `_HISTORIES_BY_Q_SQL` / `_OBS_BY_Q_SQL`，
  **多一道租户过滤**（桥接版没有租户列）；
- **现场段 / 试跑的返回形状与文案**逐字对齐桥接（前端零改动）：`context` 的
  feedback 9 键、histories 正序 + `is_feedback_target`、observations slim 6 键；
  replay 的 `new_observations` slim 10 键与四档归因（answered_ok / 失败归因 /
  empty_result / no_observation），归因文案只来自 `services/nl2sql/diagnosis` 一份；
- **采纳写入**走抽象 schema 的人工确认口（`AbstractSchemaService.save`）：
  读到的 `status` 原样沿用（PUT 是整行覆盖，硬编码 `active` 会把 proposed 行
  静默激活）、`expected_version` 乐观锁、写后回读核对，并落 `alias` 台账。

与桥接的语义差异：

1. **replay 经 router 发 HTTP**（`EVOLUTION_ROUTER_BASE`，默认
   `http://host.docker.internal:8010`）：以现场签发的短时用户 JWT POST
   `search/mix`，与用户问答同一条链路（② 主答翻转 / DI 扇出 / 观测写入都由
   router 拦下），**不静默回退**到进程内检索（进程内实现已删除）。
2. **台账 / 备份落库**：`backup` 是 `db://evolution_ledgers/<id>`（旧桥接是工作区
   文件路径），台账条目落 `evolution_ledgers`（`ledger_type='alias'`）。
3. **采纳标记是条件更新**：`UPDATE ... SET adopted=true WHERE ... AND adopted=false`
   后回读，不用 toggle（两个并发采纳请求各 toggle 一次会净翻回 false）。

⚠️ 试跑**不落检索历史**（`save_history=False`）——落历史会污染本功能自己的诊断
现场时间线；观测行仍照写（router 拦下的 ask 链路自然写，2026-10-13 D3 拍板）。
"""
from __future__ import annotations

import asyncio
import datetime
import json
import math
import urllib.error
import urllib.request

from sqlalchemy import text

from jonex_core.common.config import get_config
from jonex_core.common.exceptions import (
    InvalidParameterError,
    ResourceNotFoundError,
    UpstreamServiceError,
)

from ..abstract_schema_service import AbstractSchemaService
from ..nl2sql import diagnosis as nl2sql_diag
from .ledger_service import LedgerService
from .tokens import issue_evolution_token

#: 点踩列表上限（桥接 `_DISLIKES_SQL` 的 LIMIT 同值）。
MAX_DISLIKES = 500
#: 现场段：该问句最近 N 条检索历史 / 最近 N 条观测（桥接同值）。
CONTEXT_HISTORY_LIMIT = 5
CONTEXT_OBSERVATION_LIMIT = 20
#: 试跑：只读该问句最近 N 条观测来判「本次新落」（桥接同值）。
REPLAY_OBSERVATION_LIMIT = 10


# ══════════════════════════════════════════════════════════════
# 读取器（SQL 与 tests/eval/nl2sql/diagnose.py 同口径 + 租户过滤）
# ══════════════════════════════════════════════════════════════

_DISLIKES_SQL = text("""
    SELECT f.created_at, f.id, f.history_id, f.query, f.answer, f.feedback_reason,
           f.feedback_comment, f.adopted, f.user_id,
           f.knowledge_base_ids,
           o.scene->>'anchor' AS anchor, o.scene->>'rounds' AS rounds,
           o.outcome, o.reason_code, left(o.sql, 300) AS sql
    FROM knowledge_base.knowledge_answer_feedback f
    LEFT JOIN LATERAL (
      SELECT scene, outcome, reason_code, sql
      FROM knowledge_base.nl2sql_observations o
      WHERE o.tenant_id = f.tenant_id AND o.question = f.query
        -- 基准（合成流量）观测不当点踩现场（Task 4）
        AND COALESCE(o.scene->>'evolution_bench', 'false') <> 'true'
      ORDER BY o.created_at DESC LIMIT 1
    ) o ON true
    WHERE f.feedback_type = 'dislike' AND f.is_deleted = 0
      AND f.tenant_id = :tenant
      AND f.created_at >= :since AND f.created_at < :until
    ORDER BY f.created_at DESC
    LIMIT :limit
""")

_FEEDBACK_BY_ID_SQL = text("""
    SELECT f.created_at, f.tenant_id, f.id, f.history_id, f.query, f.answer,
           f.feedback_reason, f.feedback_comment, f.adopted, f.user_id,
           f.knowledge_base_ids
    FROM knowledge_base.knowledge_answer_feedback f
    WHERE f.id = :feedback_id AND f.tenant_id = :tenant
      AND f.feedback_type = 'dislike' AND f.is_deleted = 0
    LIMIT 1
""")

#: ⚠️ `references` 是 PostgreSQL 保留字，裸写即 syntax error —— 必须保留双引号
#: （与 diagnose.py 同款真机冒烟结论）。
_HISTORIES_BY_Q_SQL = text("""
    SELECT id, user_id, query, knowledge_base_id, domain_space_id, mode, status,
           answer, "references", reasoning, result_count, reference_count, duration_ms,
           extra_metadata, searched_at, created_at
    FROM knowledge_base.knowledge_search_history
    WHERE tenant_id = :tenant AND query = :query AND is_deleted = 0
    ORDER BY created_at DESC
    LIMIT :limit
""")

#: 诊断要全量形态：不得加 outcome 过滤（成功/失败都是证据——「当时没走 ②」也要看得见）。
#: reason/row_count/data_source_id 在列集里：reason 供 exec_failed 的 marker 判定，
#: row_count 供「零行 ≠ 答上」，data_source_id 供多源扇出逐源落地。
_OBS_BY_Q_SQL = text("""
    SELECT created_at, outcome, reason_code, reason, sql, scene, row_count, data_source_id
    FROM knowledge_base.nl2sql_observations
    WHERE tenant_id = :tenant AND question = :question
      -- 基准（合成流量）观测不进现场时间线（Task 4）
      AND COALESCE(scene->>'evolution_bench', 'false') <> 'true'
    ORDER BY created_at DESC
    LIMIT :limit
""")

#: 采纳标记 = **条件更新**（不是 toggle）：只有当前 `adopted=false` 才置 true，
#: 并发两次采纳也只会生效一次，不会像 toggle 那样净翻回 false。
_MARK_ADOPTED_SQL = text("""
    UPDATE knowledge_base.knowledge_answer_feedback
    SET adopted = true
    WHERE id = :feedback_id AND tenant_id = :tenant AND adopted = false
""")

#: 条件更新后回读（回读值才是对外口径：已被别处置 true 时同样返回 True）。
_ADOPTED_BY_ID_SQL = text("""
    SELECT adopted FROM knowledge_base.knowledge_answer_feedback
    WHERE id = :feedback_id AND tenant_id = :tenant
    LIMIT 1
""")


def _first_kb_id(value) -> str | None:
    """`knowledge_base_ids` jsonb → 取 [0] 成 `kb_id`（处理跳转 / 试跑要用）。

    防御口径：先是 list（非空取 [0]）；裸露 asyncpg 不给 jsonb 解码器时是 str →
    尝试 json.loads；解析失败 / 非 list / 空 list 一律 None（miss 不该让整行读不出来）。
    """
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return None
    return value[0] if isinstance(value, list) and value else None


def _normalise(row) -> dict:
    """`scene` 统一成 dict（jsonb 解码防御，与 diagnose.py `_normalise` 同口径）。

    本栈（`postgresql+asyncpg`）连接级注册了 jsonb 解码器，实际拿到的是 dict；
    str 分支是针对裸 asyncpg / 老口径的防御。坏 JSON → `{}`（观测是旁路数据，
    一条坏行不该让整次现场裸崩）。
    """
    entry = dict(row)
    raw = entry.get("scene")
    if isinstance(raw, str):
        try:
            entry["scene"] = json.loads(raw)
        except json.JSONDecodeError:
            entry["scene"] = {}
    return entry


def _list_ts(value):
    """点踩列表的 `created_at` 口径：桥接用 `str(datetime)`，这里同款。

    `None` 保持 `None`（桥接无条件 `str()` 会产出字面量 "None"，前端按字符串显示 ——
    这里不复制这个瑕疵，但时间格式仍是 `str(datetime)`）。
    """
    return None if value is None else str(value)


def _local_iso(dt):
    """时间口径统一（现场段 / 试跑专用）：本机本地 naive → ISO（秒精度）。

    naive（history.created_at / obs.created_at 已是本地墙钟）→ 原样；
    aware（feedback.created_at / searched_at 是 UTC）→ astimezone() 转本地再丢 tz；
    字符串按 ISO 尝试解析（失败原样返回）；None → None。

    aware 分支落**宿主**帧；naive 列是**容器**墙钟（compose 钉 `TZ: Asia/Shanghai`）——
    宿主 TZ 须与容器一致，二者才同帧（replay 的 t0 比较同理）。
    """
    if dt is None:
        return None
    if isinstance(dt, str):
        try:
            dt = datetime.datetime.fromisoformat(dt)
        except ValueError:
            return dt                              # 解析不了原样返回（不许把信封打成 500）
    if not isinstance(dt, datetime.datetime):
        return str(dt)                             # 防御：非时间类型 str 化
    if dt.tzinfo is not None:                      # aware（UTC）→ 本地墙钟，再丢 tz
        dt = dt.astimezone().replace(tzinfo=None)
    return dt.isoformat(timespec="seconds")


def _parse_ts(value):
    """宽松时间解析：串 / aware 都归一为 naive 本地；坏值 → None（`_fresh_observations` 用）。"""
    try:
        ts = datetime.datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if ts.tzinfo is not None:                      # aware（+08:00 / Z）→ 本地朴素
        ts = ts.astimezone().replace(tzinfo=None)
    return ts


def _dislike_slim(row) -> dict:
    """点踩列表行（桥接 `/dislikes` 的 14 键形状）。

    `answer` 截 200 是桥接层口径；`sql` 已在 SQL 侧 `left(..., 300)`。
    `rounds` 对齐 `/dislikes` 先例（SQL `->>` 恒 text；真库 jsonb 数字型也统一 str）。
    """
    rounds = row.get("rounds")
    return {
        "created_at": _list_ts(row.get("created_at")),
        "id": row.get("id"),
        "history_id": row.get("history_id"),
        "query": row.get("query"),
        "answer": str(row.get("answer") or "")[:200],
        "feedback_reason": row.get("feedback_reason"),
        "feedback_comment": row.get("feedback_comment"),
        "adopted": bool(row.get("adopted")),
        "kb_id": _first_kb_id(row.get("knowledge_base_ids")),
        "anchor": row.get("anchor"),
        "rounds": str(rounds) if rounds is not None else None,
        "outcome": row.get("outcome"),
        "reason_code": row.get("reason_code"),
        "sql": row.get("sql"),
    }


async def list_dislikes(session, tenant_id: str, since: datetime.datetime,
                        until: datetime.datetime,
                        limit: int = MAX_DISLIKES) -> list[dict]:
    """窗口内的点踩反馈（slim 14 键，桥接 `/dislikes` 同形）。"""
    rows = (await session.execute(
        _DISLIKES_SQL,
        {"tenant": tenant_id, "since": since, "until": until, "limit": limit},
    )).mappings().all()
    return [_dislike_slim(row) for row in rows]


async def feedback_by_id(session, tenant_id: str,
                         feedback_id: str) -> dict | None:
    """单条点踩反馈（tenant + dislike + 未软删）；未命中回 None，不抛。

    列集 = 列表 SQL 的反馈表部分；`knowledge_base_ids` 换成 `kb_id`（与桥接
    `_run_feedback_query` 同口径），`created_at` 等原值返回由调用方处理。
    """
    row = (await session.execute(
        _FEEDBACK_BY_ID_SQL,
        {"tenant": tenant_id, "feedback_id": feedback_id},
    )).mappings().first()
    if row is None:
        return None
    data = dict(row)
    data["kb_id"] = _first_kb_id(data.pop("knowledge_base_ids", None))
    return data


async def histories_by_question(session, tenant_id: str, query: str,
                                limit: int = CONTEXT_HISTORY_LIMIT) -> list[dict]:
    """该问句最近 limit 条检索历史（SQL 给新→旧；桥接再反转为正序）。

    每次问答可能有**两条**历史：Jonex 服务端存的「本体层答案」快照与前端 onDone
    另存的「最终合并答案」快照（相隔数秒）；`feedback.history_id` 恒挂前者 ——
    只取它会对数据题显示「没找到内容」，故现场段改取时间线。
    """
    rows = (await session.execute(
        _HISTORIES_BY_Q_SQL,
        {"tenant": tenant_id, "query": query, "limit": limit},
    )).mappings().all()
    return [dict(row) for row in rows]


async def observations_by_question(session, tenant_id: str, question: str,
                                   limit: int = CONTEXT_OBSERVATION_LIMIT) -> list[dict]:
    """该问句最近 limit 行观测（**全 outcome 形态——不得过滤**：成功行也是证据）。"""
    rows = (await session.execute(
        _OBS_BY_Q_SQL,
        {"tenant": tenant_id, "question": question, "limit": limit},
    )).mappings().all()
    return [_normalise(row) for row in rows]


# ══════════════════════════════════════════════════════════════
# 现场段
# ══════════════════════════════════════════════════════════════

def _obs_slim(row: dict) -> dict:
    """观测行 → 桥接 slim（现场段与试跑档**共用一份口径**：6 键、sql 截 300、rounds 恒 str）。"""
    scene = row.get("scene") if isinstance(row.get("scene"), dict) else {}
    sql = row.get("sql")
    rounds = scene.get("rounds")
    return {"created_at": _local_iso(row.get("created_at")),
            "outcome": row.get("outcome"), "reason_code": row.get("reason_code"),
            "sql": str(sql)[:300] if sql is not None else None,
            "anchor": scene.get("anchor"),
            "rounds": str(rounds) if rounds is not None else None}


def _history_slim(row: dict, target_id) -> dict:
    answer = row.get("answer")
    answer = str(answer) if answer is not None else None
    return {
        "id": row.get("id"),                  # 内部字段（user_id 等）不回传：只造契约键
        # 新增时间列必须过 _local_iso（JSONResponse 对裸 datetime 会 500）
        "created_at": _local_iso(row.get("created_at")),
        "searched_at": _local_iso(row.get("searched_at")),
        "answer": answer,                     # 现场主体：全文保真，不截断
        "answer_len": len(answer) if answer is not None else None,
        "is_feedback_target": target_id == row.get("id"),
    }


def _feedback_slim(feedback: dict) -> dict:
    """现场段 feedback 9 键（桥接窄形状；**不是**列表行）。"""
    return {
        "created_at": _local_iso(feedback.get("created_at")),
        "id": feedback.get("id"),
        "history_id": feedback.get("history_id"),
        "query": feedback.get("query"),
        # answer 截 200 属桥接层口径（同 /dislikes）；观测形态在 observations 段
        "answer": str(feedback.get("answer") or "")[:200],
        "feedback_reason": feedback.get("feedback_reason"),
        "feedback_comment": feedback.get("feedback_comment"),
        "adopted": bool(feedback.get("adopted")),
        "kb_id": feedback.get("kb_id"),
    }


async def context(session, tenant_id: str, feedback_id: str) -> dict:
    """点踩诊断·现场段：反馈行 + 该问句最近 5 条历史（正序）+ 最近 20 条观测（全 outcome）。

    形状与旧桥接 `/dislikes/{id}/context` **逐键一致**（前端 `DislikeContext`）。
    feedback 未命中 → 1002（桥接 404/4005 的等价档）；该问句无快照 → `[]`
    （合法现场态，不报错）。
    """
    feedback = await feedback_by_id(session, tenant_id, feedback_id)
    if feedback is None:
        raise ResourceNotFoundError(message=f"找不到反馈：{feedback_id}")
    question = str(feedback.get("query") or "")
    histories = (await histories_by_question(
        session, tenant_id, question, limit=CONTEXT_HISTORY_LIMIT)
        if question else [])
    observations = (await observations_by_question(
        session, tenant_id, question, limit=CONTEXT_OBSERVATION_LIMIT)
        if question else [])
    target_id = feedback.get("history_id")
    return {
        "feedback": _feedback_slim(feedback),
        # SQL 给的是最近 5 条（新→旧），反转成正序（旧→新）
        "histories": [_history_slim(r, target_id) for r in reversed(histories or [])],
        "observations": [_obs_slim(r) for r in (observations or [])],
    }


# ══════════════════════════════════════════════════════════════
# 试跑 + 归因
# ══════════════════════════════════════════════════════════════

def _attribute_obs(obs: dict, question: str) -> dict:
    """单条观测行的归因（规则表 = `nl2sql_diag` 单一出处，与桥接同款）。

    entry 与 diagnose CLI 条目同构（question/scene 带上：`_action_for` 的两处细化判据
    ——指标缺口 / 现场 flags——就在这两个键上，缺了会静默退回基础文案；reason 已在
    观测列集里——exec_failed 的 marker 匹配靠它，缺了会误归 T/exec_other）。

    ⚠️ 传入的 obs 必须是**原始观测行**（scene 原样 dict、reason/sql 未截）；slim 行的
    scene 已展平，传错会静默丢现场细化。
    """
    entry = {"reason_code": obs.get("reason_code") or None,
             "reason": obs.get("reason"),
             "sql": obs.get("sql"),
             "question": question,
             "scene": obs.get("scene")}
    attr = nl2sql_diag.attribute(entry)
    return {**attr, "action": nl2sql_diag._action_for(attr["kind"], [entry])}


def _replay_obs_slim(row: dict, *, question: str) -> dict:
    """试跑档每行 slim = `_obs_slim` 的 6 键底 + `row_count`/`data_source_id` +
    `reason`（截 200）/`attribution`（failed 行四键、success 行 None）= **稳定 10 键**。
    """
    slim = _obs_slim(row)
    slim["row_count"] = row.get("row_count")
    slim["data_source_id"] = row.get("data_source_id")
    slim["reason"] = str(row.get("reason") or "")[:200] or None
    slim["attribution"] = (_attribute_obs(row, question)
                           if row.get("outcome") == "failed" else None)
    return slim


def _fresh_observations(rows, t0) -> list[dict]:
    """「本次试跑新落」的**全部**观测行：`created_at >= t0`，按 created_at 升序（稳定序）。

    **不单选**：router ② 主答按源问，多源 KB 一试跑可落成功 + 失败多行 ——「取最新
    一条」的顺序由延迟决定、归因会判反。坏行 / 无时间戳跳过、不抛。
    """
    fresh = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        ts = _parse_ts(row.get("created_at"))
        if ts is None or ts < t0:
            continue
        fresh.append((ts, row))
    fresh.sort(key=lambda pair: pair[0])
    return [row for _ts, row in fresh]


def _has_rows(row: dict) -> bool:
    """「这个源真答上了」的一半：`row_count > 0`（零行 ≠ 答上 —— 主答口径会走 DI 兜底）。

    None / 坏值保守判否（`int()` 收口：串数字也认，非数字不抛）。
    """
    try:
        return int(row.get("row_count") or 0) > 0
    except (TypeError, ValueError):
        return False


def _finite_float(raw: str):
    """`json.loads` 的 `parse_float` 钩子：溢出成 ±inf 的字面量（如 `1e999`）收成 None。

    Starlette JSONResponse 用 `allow_nan=False`，非有限数会让响应序列化裸 500 ——
    解析时即收成 None（与「缺失」同语义），与桥接 `_finite_float` 同款。
    """
    value = float(raw)
    return value if math.isfinite(value) else None


def _load_router_json(raw) -> dict:
    """router 响应解析（纯函数，供 `_call_router_mix` 与测试共用）。

    两条非有限数入口都要收口，否则 `JSONResponse(allow_nan=False)` 序列化会裸 500：

    - `parse_constant`：JSON 里的 `NaN` / `Infinity` / `-Infinity` 字面量
      （Python `json` 默认接受，非标准扩展）→ `None`；
    - `parse_float='_finite_float'`：数值溢出（`1e999` / `-1e999` → ±inf）→ `None`。

    `raw` 接受 `str` / `bytes` / `bytearray`（`json.loads` 原生支持）。非法 JSON
    照常抛 `ValueError`，由调用方的 `except (OSError, ValueError)` 收成 5xxx。
    """
    return json.loads(raw, parse_constant=lambda _name: None,
                      parse_float=_finite_float)


def _signed_replay_token(tenant_id: str, user_id: str | None) -> str:
    """现场签发短时用户 JWT（10 分钟）：router 的 `search/mix` 以用户身份解租户。

    签发口径统一收到 `tokens.issue_evolution_token`（基准臂共用同一张「演化身份」
    票，避免两处 stub 漂移）；函数名保留，试跑调用点与既有测试不动。
    `id` 必须是可 `int()` 的 sub（`decode_token` 校验），非数字身份（如测试 token
    的 `u_1`）退回 0 —— 只用于试跑的检索身份，不落任何写操作。
    """
    return issue_evolution_token(tenant_id, user_id, minutes=10)


def _call_router_mix(query: str, kb_id: str, authorization: str,
                     timeout: float = 300) -> dict:
    """试跑调用（模块级、可打桩）：以用户身份 POST router 的 `search/mix`，回 data 段。

    与前端同款请求面：只带 `Authorization`（租户从 JWT 解，不加 `X-Tenant-ID`）；
    body 六键就是用户问答在跑的同一契约，唯一分歧是 `save_history=False`（试跑
    落历史会污染诊断现场时间线）。HTTP / 网络 / 解析 / 形状错一律上抛
    `UpstreamServiceError`（5xxx），**不静默回退**到进程内检索。
    响应解析走 `_load_router_json`（非有限数收成 None，防 500）。
    """
    body = {"query": query, "mode": "mix", "top_k": 5, "with_reasoning": True,
            "save_history": False, "knowledge_base_ids": [kb_id]}
    base = str(get_config().EVOLUTION_ROUTER_BASE or "").rstrip("/")
    url = f"{base}/api/v1/knowledge-base/search/mix"
    req = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"),
        headers={"Authorization": authorization, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            doc = _load_router_json(resp.read())
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            # 401/403 = router 认人但不认「这次登录」：试跑 JWT 过期 / 签发身份被拒。
            # 不是网络问题，给可操作的提示（查 JWT 配置/路由），别误导成「router 不可达」
            raise UpstreamServiceError(
                message="router 拒绝试跑身份（请检查 JWT 配置/路由）") from exc
        # 1001 + kb 参数缺失 = 反馈所属知识库已被删除/重建（试跑恒带 kb_ids，
        # 被「无权限 KB 过滤」清空后就是这条）。现场 2026-10-21：用户重建 demo 库后
        # 点旧反馈的「试跑」，报成「router 不可达（HTTP 400）」——误导性很强。
        detail = ""
        try:
            raw = exc.read()
            doc = json.loads(raw.decode("utf-8"))
            code = doc.get("code") if isinstance(doc, dict) else None
            msg = str(doc.get("message") or "") if isinstance(doc, dict) else ""
            if code == 1001 and ("knowledge_base_ids" in msg or "service_id" in msg):
                raise UpstreamServiceError(
                    message="该反馈所属知识库已不存在或不可用（可能重建过）；"
                            "请在新知识库重新点踩后再试跑") from exc
            if msg:
                detail = f"：{msg[:160]}"
        except UpstreamServiceError:
            raise
        except Exception:  # noqa: BLE001 —— 读不出信封就回退到通用文案
            detail = ""
        raise UpstreamServiceError(
            message=f"router 返回 HTTP {exc.code}：试跑调用失败{detail}") from exc
    except urllib.error.URLError as exc:
        raise UpstreamServiceError(
            message=f"router 不可达：试跑调用失败（{exc.reason}）") from exc
    except (OSError, ValueError) as exc:
        raise UpstreamServiceError(
            message=f"router 不可达或响应不可解析：试跑调用失败（{exc}）") from exc
    data = doc.get("data") if isinstance(doc, dict) else None
    if not isinstance(data, dict):
        raise UpstreamServiceError(
            message=f"router 响应形状不对（期望对象）：{str(doc)[:200]}")
    return data


async def replay(session, tenant_id: str, user_id: str | None,
                 feedback_id: str) -> dict:
    """点踩诊断·试跑：以该反馈的 question + 用户身份**经 router** 重走真实检索链路。

    与旧桥接语义一致：POST `EVOLUTION_ROUTER_BASE/api/v1/knowledge-base/search/mix`
    （② 主答翻转 / DI 扇出 / 观测写入都在 router 拦下），身份用现场签发的 10 分钟
    用户 JWT。不可达 / 非 2xx（401/403 单独给「router 拒绝试跑身份」提示）→
    `UpstreamServiceError`，**不静默回退**进程内检索。
    其余判定与归因与桥接逐字一致：

    - `answered` = **任一**行 `success && row_count>0`（零行 ≠ 答上）；无新鲜行 → None；
    - 答上 → `answered_ok` 档（多源时文案带出「N 源中 M 源答上」）；
    - 未答上且有失败行 → 对**最新失败行**归因（`attribute` + 行动文案原样复用）；
    - 未答上、只有零行成功行 → `empty_result`；无任何新鲜行 → `no_observation`。

    `new_observations` 全量带回（slim 10 键，升序）。试跑**不落检索历史**
    （`save_history=False`），观测行由 router 拦下的 ask 链路自然写入。
    """
    feedback = await feedback_by_id(session, tenant_id, feedback_id)
    if feedback is None:
        raise ResourceNotFoundError(message=f"找不到反馈：{feedback_id}")
    kb_id = str(feedback.get("kb_id") or "").strip()
    if not kb_id:
        raise InvalidParameterError(message="该反馈没有 kb_id，无法试跑")
    question = str(feedback.get("query") or "")
    if not question.strip():
        raise InvalidParameterError(message="该反馈没有问题文本，无法试跑")

    t0 = datetime.datetime.now()             # naive 本地：与 obs.created_at 同帧比较
    authorization = f"Bearer {_signed_replay_token(tenant_id, user_id)}"
    try:
        # urllib 是阻塞调用：放线程里跑，别卡住 KB 服务的事件循环
        data = await asyncio.to_thread(
            _call_router_mix, question, kb_id, authorization)
    except UpstreamServiceError:
        raise
    except Exception as exc:  # noqa: BLE001 —— 试跑没跑起来必须是可读的 5xxx，不许裸 500
        raise UpstreamServiceError(
            message=f"router 不可达：试跑调用失败（{exc}）") from exc

    fresh = _fresh_observations(
        await observations_by_question(
            session, tenant_id, question, limit=REPLAY_OBSERVATION_LIMIT),
        t0)

    answered = (any(r.get("outcome") == "success" and _has_rows(r) for r in fresh)
                if fresh else None)
    failed = [r for r in fresh if r.get("outcome") == "failed"]
    if answered:
        # 试跑答上 = 无缺口档（产品分支，不动 attribute 规则表）。
        # 多源扇出时「任一源答上」≠ 整题无缺口：逐源去重判答上，文案带出「N 源中
        # M 源答上」——单源保持原文案逐字不变。
        by_src: dict = {}
        for row in fresh:
            key = str(row.get("data_source_id") or "")
            by_src[key] = by_src.get(key, False) or (
                row.get("outcome") == "success" and _has_rows(row))
        action = "试跑已答上——当前口径可解此题（原反馈可能是当时状态或已被其它修复覆盖）"
        if len(by_src) > 1:
            action += f"（{len(by_src)} 源中 {sum(by_src.values())} 源答上）"
        attribution = {"attribution": "none", "kind": "answered_ok", "sub_key": None,
                       "action": action}
    elif failed:
        # `failed[-1]` = 最新失败行（fresh 已按 created_at 升序）
        attribution = _attribute_obs(failed[-1], question)
    elif fresh:
        attribution = {"attribution": "none", "kind": "empty_result", "sub_key": None,
                       "action": "试跑执行成功但零行——按主答口径不算答上（会走 DI 兜底）"}
    else:
        attribution = {"attribution": "unknown", "kind": "no_observation", "sub_key": None,
                       "action": "本次试跑未落观测行，请人工查看试跑结果"}

    return {
        "replay": {
            "answered": answered,
            "source": data.get("source"),
            "answer_snippet": str(data.get("answer") or "")[:300],
            "data_blocks": data.get("data_blocks"),
            "new_observations": [_replay_obs_slim(r, question=question) for r in fresh],
        },
        "attribution": attribution,
    }


# ══════════════════════════════════════════════════════════════
# 抽象 schema 选项 / 采纳写入
# ══════════════════════════════════════════════════════════════

def _require_text(value, name: str) -> str:
    text_value = str(value or "").strip()
    if not text_value:
        raise InvalidParameterError(message=f"{name} 不能为空")
    return text_value


async def _mark_feedback_adopted(session, tenant_id: str,
                                 feedback_id: str) -> bool:
    """把反馈行标记为已采纳：条件 UPDATE（`adopted=false` 才生效）→ commit → 回读。

    不用 `AnswerFeedbackService.toggle_adopted`（toggle 不是 set：两个并发采纳请求
    各翻转一次会净翻回 false）。回读的是**库里当前的 adopted**，所以「别人刚刚已置
    true」也返回 True；行不存在 → False。
    """
    await session.execute(
        _MARK_ADOPTED_SQL,
        {"tenant": tenant_id, "feedback_id": feedback_id})
    await session.commit()
    row = (await session.execute(
        _ADOPTED_BY_ID_SQL,
        {"tenant": tenant_id, "feedback_id": feedback_id})).mappings().first()
    return bool(row.get("adopted")) if row else False


async def _schema_of(tenant_id: str, kb_id: str) -> dict:
    """读抽象 schema；没有就抛**可读**的 1002（不是空壳 / 不是 500）。"""
    data = await AbstractSchemaService().get(tenant_id, kb_id)
    if not isinstance(data, dict):
        raise ResourceNotFoundError(
            message=f"知识库 {kb_id} 还没有抽象 schema，无法读取候选/写入别名")
    return data


def _schema_body(data: dict) -> tuple[dict, str]:
    """取 `(schema, status)`；`status` 缺失拒绝写入（PUT 是整行覆盖，不兜默认值）。"""
    schema = data.get("schema") if isinstance(data.get("schema"), dict) else {}
    status = data.get("status")
    if not isinstance(status, str) or not status:
        raise UpstreamServiceError(
            message="抽象 schema 缺少 status，无法写入（避免把未确认的行静默激活）")
    return schema, status


def _now_iso() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def _cli_shell(ok: bool, message: str) -> dict:
    """旧 CLI 返回壳：`ok=false` 是业务性失败，前端照常渲染 stdout 尾部。"""
    return {"ok": bool(ok), "rc": 0 if ok else 1, "stdout_tail": message}


async def adopt_alias(session, tenant_id: str, user_id: str | None,
                      feedback_id: str, concept: str, alias: str,
                      note: str = "") -> dict:
    """采纳为概念别名：读抽象 schema → `add_concept_alias` → 乐观锁保存 → 回读核对。

    等价旧桥接 `POST /dislikes/{id}/adopt-alias` 背后的 `diagnose.py --adopt`：

    - `status` **沿用读到的值**（不硬编码 `active`）；
    - `expected_version` = 读到的 `schema_version`；
    - 写后回读核对别名是否真的在实体上（`verified`），并落 `alias` 台账；
    - 成功后**仅当该反馈尚未采纳**时才标记（条件 UPDATE + 回读，不用 toggle）；
      `ok=true` 的 `already_present`（幂等命中）同样标记；标记失败不拖垮采纳结果
      （吞异常前先 `session.rollback()` —— 半途失败会把会话留在脏事务态，不 rollback
      后续用同一 session 的动作会 PendingRollbackError，把成功采纳报成 500）。
    """
    concept = _require_text(concept, "concept")
    alias = _require_text(alias, "alias")
    feedback = await feedback_by_id(session, tenant_id, feedback_id)
    if feedback is None:
        raise ResourceNotFoundError(message=f"找不到反馈：{feedback_id}")
    kb_id = str(feedback.get("kb_id") or "").strip()
    if not kb_id:
        raise InvalidParameterError(message="该反馈没有 kb_id，无法采纳")

    data = await _schema_of(tenant_id, kb_id)
    schema, status = _schema_body(data)
    res = nl2sql_diag.add_concept_alias(
        schema.get("entities") or [], concept, alias)
    if not res["changed"]:
        ok = res["why"] == "already_present"
        message = f"不改（{res['why']}）：概念 {concept!r} 别名 {alias!r}"
        adopted_now = bool(feedback.get("adopted"))
        if ok and not adopted_now:
            # already_present 也是「采纳成功」：别名已在，反馈行仍要标记（幂等）
            try:
                adopted_now = await _mark_feedback_adopted(
                    session, tenant_id, feedback_id)
            except Exception:  # noqa: BLE001 —— 标记是留痕，不拖垮采纳本身
                await session.rollback()
                adopted_now = False
        return {**_cli_shell(ok, message), "feedback_adopted": adopted_now}

    version_before = data.get("schema_version")
    saved = await AbstractSchemaService().save(
        tenant_id, kb_id, {**schema, "entities": res["concepts"]},
        status=status, edited_by=user_id or "",
        expected_version=version_before,
        # 演化 UI 采纳属内部写：已有 evolution_ledgers 台账，不产保存快照。
        record_snapshot=False)

    readback = await AbstractSchemaService().get(tenant_id, kb_id)
    readback_schema = (readback.get("schema")
                       if isinstance(readback, dict)
                       and isinstance(readback.get("schema"), dict) else {})
    ok = any(
        str(entity.get("name") or "").strip() == concept
        and alias in [str(a).strip() for a in (entity.get("aliases") or [])]
        for entity in (readback_schema.get("entities") or [])
        if isinstance(entity, dict))
    version_after = ((readback or {}).get("schema_version")
                     if isinstance(readback, dict) else None)
    if version_after is None and isinstance(saved, dict):
        version_after = saved.get("schema_version")

    await LedgerService(session).append(tenant_id, "alias", {
        "id": feedback_id, "concept": concept, "alias": alias, "kb": kb_id,
        "kind": "entity",
        "version_before": version_before, "version_after": version_after,
        "verified": ok, "backup": f"db://evolution_ledgers/{feedback_id}",
        "at": _now_iso(), "note": note,
    })

    adopted_now = bool(feedback.get("adopted"))
    if ok and not adopted_now:
        try:
            adopted_now = await _mark_feedback_adopted(
                session, tenant_id, feedback_id)
        except Exception:  # noqa: BLE001 —— 标记是留痕的锦上添花，不拖垮采纳本身
            # 标记半途失败会把会话留在脏事务态：先 rollback 再吞，否则后续
            # 任何用这个 session 的动作都会 PendingRollbackError，把成功采纳报成 500
            await session.rollback()
            adopted_now = False

    message = (f"已采纳 {feedback_id}：{concept} += {alias!r}"
               f"（version {version_before}→{version_after}，"
               f"回读核对 {'✓' if ok else '✗'}）")
    return {**_cli_shell(ok, message), "feedback_adopted": adopted_now}


async def metric_options(tenant_id: str, kb_id: str) -> dict:
    """该 KB 抽象 schema 的指标名单（`{metrics:[{name, additivity}]}`）。"""
    kb_id = _require_text(kb_id, "kb")
    data = await _schema_of(tenant_id, kb_id)
    schema, _status = _schema_body(data)
    out = []
    for metric in schema.get("metrics") or []:
        if isinstance(metric, dict) and (metric.get("name") or "").strip():
            out.append({"name": metric["name"].strip(),
                        "additivity": metric.get("additivity") or "unknown"})
    return {"metrics": out}


def _has_landing(entity: dict) -> bool:
    """实体是否有可用落点（与 `concept_expansion._expand_candidates_v1` 同判据）。

    采纳别名只对**有落点**的概念有意义：候选只从落点展开，无落点的概念即使
    挂上别名也产生不了候选（2026-10-21 真机：度电比采纳到 m12_daily 后仍零候选）。
    落点形状沿用宽容口径：非 dict / `included=false` / 缺 ds 或 entity 都不算。
    """
    for source in entity.get("sources") or []:
        if not isinstance(source, dict) or source.get("included") is False:
            continue
        if (str(source.get("data_source_id") or "").strip()
                and str(source.get("entity_name") or "").strip()):
            return True
    return False


async def concept_options(tenant_id: str, kb_id: str) -> dict:
    """该 KB 抽象 schema 的概念名单（`{concepts:[{name, aliases, has_landing}]}`）。"""
    kb_id = _require_text(kb_id, "kb")
    data = await _schema_of(tenant_id, kb_id)
    schema, _status = _schema_body(data)
    out = []
    for entity in schema.get("entities") or []:
        if isinstance(entity, dict) and (entity.get("name") or "").strip():
            out.append({"name": entity["name"].strip(),
                        "aliases": [str(a) for a in (entity.get("aliases") or [])],
                        "has_landing": _has_landing(entity)})
    return {"concepts": out}


async def tool_options(tenant_id: str, kb_id: str) -> dict:
    """该 KB 的多轮工具名单（代码默认 ⊕ active 抽象行的 `tools` 覆盖）。

    返回 `{tools:[{name, description, effective_description, enabled, overridden}],
    source:"code|abstract"}`：
    - `description` = **代码默认**（`TOOL_SCHEMAS` 的 `function.description`）；
    - `effective_description` = 覆盖描述（非空且 active）否则代码默认；
    - `enabled` = 覆盖条目**显式** `enabled:false` 时为 False（其余一律 True）；
    - `overridden` = 该工具的有效状态与代码默认不同（描述变了或被停用）；
    - `source` = `abstract`（active 行有**至少一条有效覆盖**）/ `code`（否则，
      含非 active 行与只有无效条目的行）。

    非 active 行的覆盖**不生效**（与②消费口径一致）；形状供前端编辑与 RunDetail 展示。
    """
    # 延迟 import：`multiturn` 依赖 `ontology_llm`；本模块只在这一处需要工具常量。
    from ..nl2sql.multiturn import TOOL_SCHEMAS, extract_tool_overrides

    kb_id = _require_text(kb_id, "kb")
    data = await _schema_of(tenant_id, kb_id)
    schema, status = _schema_body(data)
    # 抽取规则与②消费同源（`extract_tool_overrides`）：active-only / description 非空 /
    # enabled is False，一处实现两处用。
    overrides, disabled = extract_tool_overrides(schema, status)

    out = []
    known: set[str] = set()
    for tool in TOOL_SCHEMAS:
        fn = tool.get("function") if isinstance(tool, dict) else None
        if not isinstance(fn, dict):
            continue
        name = str(fn.get("name") or "")
        known.add(name)
        description = str(fn.get("description") or "")
        effective = overrides.get(name) or description
        enabled = name not in disabled
        out.append({
            "name": name,
            "description": description,
            "effective_description": effective,
            "enabled": enabled,
            "overridden": effective != description or not enabled,
        })
    # `source` 只在**至少一条有效覆盖**（已知工具的描述覆盖 / 停用）时才标 abstract ——
    # 否则 active 行里只有无效条目（未知工具名 / 空覆盖）时，UI 标签会与数据矛盾。
    has_override = (any(n in known for n in overrides)
                    or any(n in known for n in disabled))
    return {"tools": out, "source": "abstract" if has_override else "code"}


async def adopt_metric(session, tenant_id: str, user_id: str | None,
                       adopt_id: str, metric: str, alias: str, kb_id: str,
                       note: str = "") -> dict:
    """采纳为指标别名：`add_metric_alias` → 乐观锁保存 → 回读核对 → `alias` 台账。

    与 `adopt_alias` 同一条写路径，只是目标键是 `schema.metrics`、台账 `kind="metric"`。
    台账的 `concept` 列放**指标名**（旧桥接此处 `concept` 恒 null、指标名只藏在文案里；
    台账页列名就是 concept，放指标名才读得出来）。
    """
    adopt_id = _require_text(adopt_id, "id")
    metric = _require_text(metric, "metric")
    alias = _require_text(alias, "alias")
    kb_id = _require_text(kb_id, "kb")

    data = await _schema_of(tenant_id, kb_id)
    schema, status = _schema_body(data)
    res = nl2sql_diag.add_metric_alias(
        schema.get("metrics") or [], metric, alias)
    if not res["changed"]:
        message = f"不改（{res['why']}）：指标 {metric!r} 别名 {alias!r}"
        return _cli_shell(res["why"] == "already_present", message)

    version_before = data.get("schema_version")
    saved = await AbstractSchemaService().save(
        tenant_id, kb_id, {**schema, "metrics": res["metrics"]},
        status=status, edited_by=user_id or "",
        expected_version=version_before,
        # 演化 UI 采纳属内部写：已有 evolution_ledgers 台账，不产保存快照。
        record_snapshot=False)

    readback = await AbstractSchemaService().get(tenant_id, kb_id)
    readback_schema = (readback.get("schema")
                       if isinstance(readback, dict)
                       and isinstance(readback.get("schema"), dict) else {})
    ok = any(
        str(item.get("name") or "").strip() == metric
        and alias in [str(a).strip() for a in (item.get("aliases") or [])]
        for item in (readback_schema.get("metrics") or [])
        if isinstance(item, dict))
    version_after = ((readback or {}).get("schema_version")
                     if isinstance(readback, dict) else None)
    if version_after is None and isinstance(saved, dict):
        version_after = saved.get("schema_version")

    await LedgerService(session).append(tenant_id, "alias", {
        "id": adopt_id, "concept": metric, "alias": alias, "kb": kb_id,
        "kind": "metric",
        "version_before": version_before, "version_after": version_after,
        "verified": ok, "backup": f"db://evolution_ledgers/{adopt_id}",
        "at": _now_iso(), "note": note,
    })

    message = (f"已采纳 {adopt_id}：指标 {metric} += {alias!r}"
               f"（version {version_before}→{version_after}，"
               f"回读核对 {'✓' if ok else '✗'}）")
    return _cli_shell(ok, message)
