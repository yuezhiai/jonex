"""平台监控 · 调用方（``call_log.source``）用户名解析仓储（读侧）。

监控读侧**不按租户过滤**，但用户名解析必须用**每行 ``call_log.tenant_id``**
自身做租户作用域（``users.tenant_id = 行 tenant``），否则同值 source 会跨租户串号。

## 可解析的形态

``monitor.call_log.source`` 由两个采集点写入，形态混杂：

- Sidecar：``username`` / ``user:{user_id}`` / 认证类型（``apikey`` / ``jwt`` …）；
- llm-gateway：``X-Jonex-User-Id`` 原值（平台内调用方通常传数字用户 ID）或 ``platform``。

:meth:`MonitorCallerRepository.resolve_identities` 可解析 ``user:{id}`` / ``{id}`` /
``username`` / ``external_id`` / ``upstream_subject`` 五种输入形态；系统保留字
（``apikey`` / ``platform`` / ``anonymous`` / ``unknown`` …，见
:data:`jonex_core.common.monitor_identity.RESERVED_SOURCES`）一律不解析。

## 匹配优先级

``users.id`` → ``users.external_id`` → ``users.upstream_subject`` → ``users.username``。
``username`` 是 2026-10 新增的**最后一档**：历史行里存在用户名形态的 ``source``，
把它归并到同一个用户身份上，下拉才能按人合并、筛选才能一次命中全部形态。
同一 token 既命中 ``id`` 又命中 ``username`` 时，``id`` 优先（固定优先级）。
注：库中实际存在 ``(tenant_id, username) WHERE is_deleted = 0`` 唯一索引，
但模型文件未声明该索引；匹配仍以唯一键为准。

:meth:`MonitorCallerRepository.resolve_sources` 是它的**展示层薄封装**
（``(tenant_id, source)`` → ``username``），保留给 ``caller_name`` 回填使用。

## 安全与批量

- 只 ``SELECT`` 安全列 ``tenant_id / id / username / external_id / upstream_subject /
  is_deleted``；**禁止** ``select(User)``（会带出 ``password_hash`` / ``email``）。
- 一次 ``execute`` 取回全部映射（含墓碑判定），禁止逐行查库。
- 软删行只用于 **id 墓碑判定**，绝不作展示。

## id 墓碑规则

某数字 token 在本租户**没有活跃 id 命中、但存在同 id 软删行**时，视为未解析，
**不得**继续降级到 ``external_id`` / ``upstream_subject``（防「删号后同数字
external_id 顶替」造成串号）。

## 失败降级

- 普通 DB 异常 ⇒ 返回空映射（展示层回退原始 source，监控接口不得 500）；
- 缺表 / 缺列 / schema 不存在（SQLSTATE ``42P01`` / ``42703`` / ``3F000``）⇒
  按既有 ``_resilient`` 口径抛 ``4100 MonitorDataUnavailableError``。
"""

from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from sqlalchemy import or_, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from capabilities.platform.models.user import User
from capabilities.platform.repository.base import BaseRepository
from capabilities.platform.services.monitor_resilience import (
    extract_sqlstate,
    is_data_source_missing,
)
from jonex_core.common.exceptions import MonitorDataUnavailableError
from jonex_core.common.monitor_identity import (
    format_user_identity,
    is_reserved_source,
    is_user_identity,
    parse_source_token,
)

SourceKey = Tuple[str, str]

# ``parse_source_token`` 由 ``jonex_core.common.monitor_identity`` 导入即完成
# re-export：现有 ``from ...monitor_caller import parse_source_token`` 的调用方
# 不需要改，实现归属共享值对象模块。


@dataclass(frozen=True)
class CallerIdentity:
    """一个 ``source`` 解析出的用户身份（主键 + 展示名）。"""

    user_id: int
    username: str


class MonitorCallerRepository(BaseRepository[User]):
    """调用方用户名批量解析仓储。

    ``model`` 只用于满足基类泛型约束；本仓**不走**基类的租户 / 软删辅助方法
    （租户维度取自**每行调用记录**，而非请求上下文）。
    """

    model = User

    def __init__(self, session: AsyncSession):
        super().__init__(session)

    # ------------------------------------------------------------------
    # 批量解析
    # ------------------------------------------------------------------
    async def resolve_sources(self, pairs: Iterable[SourceKey]) -> Dict[SourceKey, str]:
        """批量解析 ``(tenant_id, source)`` → ``username``（展示层薄封装）。

        返回的 key 是**原始 source 字符串**（``"123"`` 与 ``"user:123"`` 各自成键，
        都映射到同一个用户名），调用方据此回填 ``caller_name``。解析不到
        （系统保留字 / 无命中 / 软删 / 墓碑）的 pair 不出现在返回值里。
        """
        identities = await self.resolve_identities(pairs)
        return {key: identity.username for key, identity in identities.items()}

    async def resolve_identities(
        self, pairs: Iterable[SourceKey]
    ) -> Dict[SourceKey, "CallerIdentity"]:
        """``(tenant_id, source)`` → 该 source 对应的用户身份。

        可解析的输入：``user:123`` / ``123`` / 用户名 / ``external_id`` /
        ``upstream_subject``。匹配优先级固定为
        ``id`` → ``external_id`` → ``upstream_subject`` → ``username``
        （``username`` 是最后一档，只用于把历史用户名形态归并到同一个用户）。
        系统保留字（``apikey`` 等）一律不解析。

        软删墓碑规则保持不变：同租户存在同 id 软删行、且无活跃 id 命中时，
        视为未解析，**不降级**到 ``external_id`` / ``upstream_subject`` /
        ``username``。

        **规范形态的全局兜底**：``user:<id>`` 的主键全局唯一，模拟态（管理员
        切到目标租户）产生的记录租户是目标租户、用户却属于原租户，按
        ``(tenant, id)`` 查不到。这类 token 在租户内未命中时按全局主键再匹配
        一次（主键不会复用，不存在串人）。裸数字形态不做该兜底，保持既有语义。

        一次 ``execute`` 批量查询，只取安全列；解析不到的 pair 不出现在返回值里。
        """
        pair_list = [
            (tenant_id, source) for tenant_id, source in pairs if tenant_id and source
        ]
        if not pair_list:
            return {}

        # 数字 token（``123`` / ``user:123``）只用于 id 匹配；其余形如用户名 /
        # external_id / upstream_subject 的原值用于后三档匹配（token 也进候选，
        # 兼容「external_id 就是数字」的历史语义）。
        tokens_by_tenant: Dict[str, Set[str]] = {}
        strings_by_tenant: Dict[str, Set[str]] = {}
        for tenant_id, source in pair_list:
            if is_reserved_source(source):
                continue
            token = parse_source_token(source)
            if token is not None:
                tokens_by_tenant.setdefault(tenant_id, set()).add(token)
            text = source.strip()
            if not text:
                continue
            candidates = strings_by_tenant.setdefault(tenant_id, set())
            candidates.add(text)
            if token is not None:
                candidates.add(token)
        if not tokens_by_tenant and not strings_by_tenant:
            return {}

        # ``user:<id>`` 是规范形态、主键全局唯一。模拟态下记录的租户是**目标租户**，
        # 而用户属于**原租户**，按 ``(tenant, id)`` 会查不到；这类 token 额外按
        # 全局 id 兜底（只认规范形态，裸数字仍走租户内匹配，保持既有语义）。
        global_ids: Set[int] = {
            int(token)
            for _, source in pair_list
            if is_user_identity(source)
            for token in [parse_source_token(source)]
            if token is not None
        }

        id_pairs: List[Tuple[str, int]] = []
        string_pairs: List[Tuple[str, str]] = []
        for tenant_id, tokens in tokens_by_tenant.items():
            for token in tokens:
                id_pairs.append((tenant_id, int(token)))
        for tenant_id, values in strings_by_tenant.items():
            for value in values:
                string_pairs.append((tenant_id, value))

        clauses: List[Any] = []
        if id_pairs:
            clauses.append(tuple_(User.tenant_id, User.id).in_(id_pairs))
        if global_ids:
            clauses.append(User.id.in_(global_ids))
        if string_pairs:
            clauses.extend(
                [
                    tuple_(User.tenant_id, User.external_id).in_(string_pairs),
                    tuple_(User.tenant_id, User.upstream_subject).in_(string_pairs),
                    tuple_(User.tenant_id, User.username).in_(string_pairs),
                ]
            )
        stmt = select(
            User.tenant_id,
            User.id,
            User.username,
            User.external_id,
            User.upstream_subject,
            User.is_deleted,
        ).where(or_(*clauses))
        try:
            rows = (await self.session.execute(stmt)).all()
        except Exception as exc:  # noqa: BLE001 —— 展示层解析失败不得让监控接口 500
            self._raise_if_data_source_missing(exc)
            return {}

        active_by_id: Dict[SourceKey, CallerIdentity] = {}
        deleted_ids: Set[SourceKey] = set()
        active_by_global_id: Dict[int, CallerIdentity] = {}
        active_by_external: Dict[SourceKey, CallerIdentity] = {}
        active_by_subject: Dict[SourceKey, CallerIdentity] = {}
        active_by_username: Dict[SourceKey, CallerIdentity] = {}

        for row in rows:
            tenant_id = row.tenant_id
            if (
                tenant_id not in tokens_by_tenant
                and tenant_id not in strings_by_tenant
                and row.id not in global_ids
            ):
                continue
            tokens = tokens_by_tenant.get(tenant_id, set())
            strings = strings_by_tenant.get(tenant_id, set())
            is_deleted = bool(row.is_deleted)
            row_id_token = None if row.id is None else str(row.id)
            if row_id_token and row_id_token in tokens:
                if is_deleted:
                    # 只用于墓碑判定，绝不作展示
                    deleted_ids.add((tenant_id, row_id_token))
                elif row.username:
                    active_by_id[(tenant_id, row_id_token)] = CallerIdentity(
                        row.id, row.username
                    )
            if is_deleted or not row.username or row.id is None:
                continue
            identity = CallerIdentity(row.id, row.username)
            if row.id in global_ids:
                active_by_global_id.setdefault(row.id, identity)
            if row.external_id and row.external_id in strings:
                active_by_external.setdefault((tenant_id, row.external_id), identity)
            if row.upstream_subject and row.upstream_subject in strings:
                active_by_subject.setdefault(
                    (tenant_id, row.upstream_subject), identity
                )
            if row.username in strings:
                active_by_username.setdefault((tenant_id, row.username), identity)

        result: Dict[SourceKey, CallerIdentity] = {}
        for tenant_id, source in pair_list:
            if is_reserved_source(source):
                continue
            token = parse_source_token(source)
            text = source.strip()
            if token is not None:
                token_key = (tenant_id, token)
                if token_key in active_by_id:
                    result[(tenant_id, source)] = active_by_id[token_key]
                    continue
                if token_key in deleted_ids:
                    # id 墓碑：不降级到 external_id / upstream_subject / username
                    continue
                if is_user_identity(source):
                    # 规范形态的全局兜底（模拟态：行租户 ≠ 用户所属租户）
                    global_hit = active_by_global_id.get(int(token))
                    if global_hit is not None:
                        result[(tenant_id, source)] = global_hit
                        continue
            candidates = [text] if text else []
            if token is not None and token != text:
                candidates.append(token)
            for mapping in (
                active_by_external,
                active_by_subject,
                active_by_username,
            ):
                hit = next(
                    (
                        mapping[(tenant_id, value)]
                        for value in candidates
                        if (tenant_id, value) in mapping
                    ),
                    None,
                )
                if hit is not None:
                    result[(tenant_id, source)] = hit
                    break
        return result

    async def expand_source_filter(self, source: Optional[str]) -> List[SourceKey]:
        """筛选值 → 该用户的全部原始形态 ``(tenant_id, source)`` 精确对。

        解析不到（系统保留字 / 未知值 / 无命中）返回 ``[]``，调用方回退原有的
        ``source`` 精确匹配。

        产出形态：``user:<id>``、``<id>``、``username``、``external_id``、
        ``upstream_subject``（空值跳过）。

        跨租户：``id`` 全局唯一定位到唯一用户；``username`` 形态可能命中多个
        租户的同名用户 —— 全部展开（这是既有语义，关键字搜索也这么做）。
        同一值若既命中 ``id`` 又命中 ``username``，``id`` 优先（与
        :meth:`resolve_identities` 的优先级一致）。
        """
        if not source or is_reserved_source(source):
            return []
        text = source.strip()
        if not text:
            return []
        token = parse_source_token(source)

        clauses: List[Any] = [
            User.username == text,
            User.external_id == text,
            User.upstream_subject == text,
        ]
        if token is not None:
            clauses.append(User.id == int(token))
        stmt = (
            select(
                User.tenant_id,
                User.id,
                User.username,
                User.external_id,
                User.upstream_subject,
            )
            .where(User.is_deleted == 0, or_(*clauses))
            .order_by(User.tenant_id.asc(), User.id.asc())
        )
        try:
            rows = (await self.session.execute(stmt)).all()
        except Exception as exc:  # noqa: BLE001 —— 解析失败只失去「按身份展开筛选」
            self._raise_if_data_source_missing(exc)
            return []

        if not rows:
            return []
        if token is not None:
            id_rows = [
                row for row in rows if row.id is not None and str(row.id) == token
            ]
            if id_rows:
                rows = id_rows

        result: List[SourceKey] = []
        seen: Set[SourceKey] = set()
        for row in rows:
            forms: List[Optional[str]] = [
                format_user_identity(row.id),
                None if row.id is None else str(row.id),
                row.username,
                row.external_id,
                row.upstream_subject,
            ]
            for value in forms:
                if not value:
                    continue
                pair = (row.tenant_id, value)
                if pair in seen:
                    continue
                seen.add(pair)
                result.append(pair)
        return result

    # ------------------------------------------------------------------
    # keyword → (tenant_id, source) 展开（按显示名搜索）
    # ------------------------------------------------------------------
    async def expand_keyword_sources(
        self,
        keyword: Optional[str],
        *,
        user_limit: int = 200,
        pair_limit: int = 1000,
    ) -> List[SourceKey]:
        """用户名关键字 → 精确 ``(tenant_id, source)`` 候选对。

        对每个命中 ``username ILIKE %keyword%`` 的活跃用户，产出
        ``username`` / ``id::text`` / ``external_id`` / ``upstream_subject``
        四个候选（空值跳过），另补 ``user:{id}`` 形态 —— Sidecar 在缺 username
        时写的就是 ``user:{user_id}``，不补会让这类历史行按显示名搜不到。

        上限：用户 ``user_limit``（默认 200），候选对 ``pair_limit``（默认 1000）。
        """
        if not keyword:
            return []
        pattern = f"%{keyword}%"
        stmt = (
            select(
                User.tenant_id,
                User.username,
                User.id,
                User.external_id,
                User.upstream_subject,
            )
            .where(User.is_deleted == 0, User.username.ilike(pattern))
            .order_by(User.id.asc())
            .limit(user_limit)
        )
        try:
            rows = (await self.session.execute(stmt)).all()
        except Exception as exc:  # noqa: BLE001 —— 解析失败只失去「按显示名搜索」能力
            self._raise_if_data_source_missing(exc)
            return []

        result: List[SourceKey] = []
        seen: Set[SourceKey] = set()
        for row in rows:
            candidates: List[Optional[str]] = [
                row.username,
                None if row.id is None else str(row.id),
                row.external_id,
                row.upstream_subject,
            ]
            if row.id is not None:
                candidates.append(f"user:{row.id}")
            for candidate in candidates:
                if not candidate:
                    continue
                pair = (row.tenant_id, candidate)
                if pair in seen:
                    continue
                seen.add(pair)
                result.append(pair)
                if len(result) >= pair_limit:
                    return result
        return result

    # ------------------------------------------------------------------
    # 失败口径
    # ------------------------------------------------------------------
    @staticmethod
    def _raise_if_data_source_missing(exc: BaseException) -> None:
        """缺表 / 缺列 / schema 不存在 ⇒ 与 ``_resilient`` 同口径抛 4100。"""
        if is_data_source_missing(exc):
            raise MonitorDataUnavailableError(
                details={
                    "reason": "data_source_missing",
                    "sqlstate": extract_sqlstate(exc),
                }
            ) from exc


__all__ = [
    "CallerIdentity",
    "MonitorCallerRepository",
    "SourceKey",
    "parse_source_token",
]
