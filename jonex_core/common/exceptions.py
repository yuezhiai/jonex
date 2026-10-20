#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""
Jonex 平台 - 统一异常体系

定义平台所有业务异常的层级结构和错误码规范
错误码规范：
  1xxx - 通用错误
  2xxx - 能力相关错误
  3xxx - 认证授权错误
  4xxx - 数据相关错误
  5xxx - 服务依赖错误
"""

from typing import Optional, Dict, Any


class JonexException(Exception):
    """
    Jonex 平台基础异常类

    所有自定义异常必须继承自该类，便于统一处理
    """
    code: int = 1000
    status_code: int = 500
    default_message: str = "服务器内部错误"

    def __init__(
        self,
        message: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
        cause: Optional[Exception] = None,
        params: Optional[Dict[str, Any]] = None,
    ):
        """
        Args:
            message: 用户友好的错误信息（显式传入时跳过翻译，作为 fallback）
            details: 额外的错误详情（不包含敏感信息）
            cause: 原始异常（用于链式追踪）
            params: 模板占位符实参，供 translate(code, params=...) 渲染含变量消息
        """
        self.message = message or self.default_message
        self.details = details or {}
        self.cause = cause
        self.params = params or {}
        self._message_explicit = message is not None
        super().__init__(self.message)

    def to_dict(self) -> Dict[str, Any]:
        """转为字典格式（用于响应序列化）"""
        result = {
            "code": self.code,
            "message": self.message,
        }
        if self.details:
            result["details"] = self.details
        if self.params:
            result["params"] = self.params
        return result


# ==================== 1xxx 通用错误 ====================
class InternalError(JonexException):
    """内部错误"""
    code = 1000
    status_code = 500
    default_message = "服务器内部错误"


class InvalidParameterError(JonexException):
    """参数无效"""
    code = 1001
    status_code = 400
    default_message = "请求参数无效"


class ResourceNotFoundError(JonexException):
    """资源未找到"""
    code = 1002
    status_code = 404
    default_message = "请求的资源不存在"


class ResourceConflictError(JonexException):
    """资源冲突"""
    code = 1003
    status_code = 409
    default_message = "资源状态冲突"


class OperationNotSupportedError(JonexException):
    """操作不支持"""
    code = 1004
    status_code = 405
    default_message = "操作不被支持"


class UnsafeTargetUrlError(JonexException):
    """出站目标 URL 不安全（非 HTTPS / 私网 / 回环 / 链路本地）。

    当前剩余生产点是 :func:`validate_redirect_url`：出站 HTTP 30x 重定向目标的逐跳校验，
    它仍走 :func:`validate_target_url` 的默认严格档（仅 https、禁私网 / 回环 / 链路本地）。

    两条用户可填路径都已改走 `InvalidTargetUrlError`（1007）：数据源 `datasource_url`
    （2026-09-23 放宽）与 SSO `issuer_url`（与数据源同档放宽），故这两处不再触发 1005。
    """
    code = 1005
    status_code = 400
    default_message = "接口地址不安全，仅支持 HTTPS 且禁止私网/回环地址"


class ResourceAlreadyExistsError(JonexException):
    """资源已存在（调用方指定的 id 被占用）。

    与 `ResourceConflictError`（1003「资源状态冲突」）分开：那个说的是**状态**不允许
    （终态回退、草稿已被发版……），这个说的是**身份**已经被占了 —— 调用方指定了一个
    已存在的 id，原样重发结果一样，得换一个 id。

    典型场景：`task_center.init_task` 收下调用方自带的 `task_id`（运行方自己生成、便于
    把 id 先落到自己的行上），撞了已有任务时抛它。`details` 带 `task_id`。
    """
    code = 1006
    status_code = 409
    default_message = "资源已存在"


class InvalidTargetUrlError(JonexException):
    """出站目标 URL 格式非法（scheme 不是 http/https，或缺少主机名）。

    与 `UnsafeTargetUrlError`（1005）分开：那个说的是「地址**不安全**」（非 HTTPS，
    或指向私网 / 回环 / 链路本地），剩余生产点在出站 30x 重定向的逐跳校验
    （`validate_redirect_url`）；这个说的是「地址**格式**不对」。

    两条用户可填路径**共用**本错误：数据源 `datasource_url`（「用户与组织同步」的
    接口地址）与 SSO `issuer_url`（「单点登录」的服务地址）—— 两条都已放行 http 与
    私网 / 回环地址，此时唯一还会触发它的就是 scheme 非法（`file://` / `gopher://` /
    `ftp://` ……）或没有主机名（`https:///path-only`）。

    文案只讲格式、不提私网：放行私网之后再说「禁止私网」会二次误导用户。
    """
    code = 1007
    status_code = 400
    default_message = "接口地址格式错误，需以 http:// 或 https:// 开头"


class ModelNotConfiguredError(JonexException):
    """模型名未配置（既没有 purpose 变量、也没有兜底变量）。

    模型名的唯一来源是环境变量。2026-10-08 的事故是「代码里硬编码旧模型名作兜底」：
    上游改了名，兜底却把旧名字静默捡回来，排查时日志里看不出任何线索。所以这里
    **宁可响亮失败**，也不留字面量兜底 —— 读不到就该拦在配置层，不该让一个可能
    早已失效的模型名流到上游才炸。

    `details` 带 `purpose_env`（该配的业务变量名）与 `default_env`（兜底变量名），
    方便运维直接照着改；`params` 带 `field` 供 i18n 模板渲染。
    """

    code = 1008
    status_code = 500
    default_message = "模型名称未配置"


# ==================== 2xxx 能力相关错误 ====================
class CapabilityError(JonexException):
    """能力错误基类"""
    code = 2000
    status_code = 500
    default_message = "能力调用失败"


class CapabilityNotFoundError(CapabilityError):
    """能力未找到"""
    code = 2001
    status_code = 404
    default_message = "请求的能力不存在"


class CapabilityInvokeError(CapabilityError):
    """能力调用失败"""
    code = 2002
    status_code = 500
    default_message = "能力执行失败"


class CapabilityTimeoutError(CapabilityError):
    """能力调用超时"""
    code = 2003
    status_code = 504
    default_message = "能力调用超时"


class CapabilityValidationError(CapabilityError):
    """能力输入验证失败"""
    code = 2004
    status_code = 400
    default_message = "能力输入参数无效"


class CapabilityIdFormatError(CapabilityError):
    """能力 ID 格式错误"""
    code = 2005
    status_code = 400
    default_message = "能力 ID 格式无效"


# ==================== 3xxx 认证授权错误 ====================
class AuthError(JonexException):
    """认证错误基类"""
    code = 3000
    status_code = 401
    default_message = "认证失败"


class MissingApiKeyError(AuthError):
    """缺少 API Key"""
    code = 3001
    status_code = 401
    default_message = "缺少 X-API-Key 请求头"


class InvalidApiKeyError(AuthError):
    """API Key 无效"""
    code = 3002
    status_code = 401
    default_message = "无效的 API Key"


class InvalidCredentialsError(AuthError):
    """登录凭据无效（用户名或密码错误）"""
    code = 3008
    status_code = 401
    default_message = "用户名或密码错误"


class TokenExpiredError(AuthError):
    """Token 已过期"""
    code = 3003
    status_code = 401
    default_message = "认证 Token 已过期"


class InternalAuthError(AuthError):
    """内部服务认证失败"""
    code = 3004
    status_code = 403
    default_message = "内部服务认证失败"


class TenantIsolationError(AuthError):
    """租户隔离违规"""
    code = 3005
    status_code = 403
    default_message = "无权访问该租户资源"


class PermissionDeniedError(AuthError):
    """权限不足"""
    code = 3006
    status_code = 403
    default_message = "权限不足"


class RateLimitExceededError(AuthError):
    """限流超限"""
    code = 3007
    status_code = 429
    default_message = "请求过于频繁，请稍后重试"


class AccountLockedError(AuthError):
    """账户因多次登录失败被锁定"""
    code = 3009
    status_code = 403
    default_message = "账户已锁定，请稍后重试"


class AccountDisabledError(AuthError):
    """账号被管理员停用（users.status != 1）。

    与 InvalidCredentialsError 区分：本异常只在密码已校验通过后抛出，
    调用方已证明知道正确凭据，据此返回明确原因不构成账号枚举泄漏。
    """
    code = 3013
    status_code = 403
    default_message = "账号已停用，请联系管理员"


class OidcAuthorizationCodeError(AuthError):
    """OIDC 授权码无效 / 过期 / 已用（重放），或兑换失败。"""
    code = 3014
    status_code = 400
    default_message = "授权码兑换失败，请重新发起登录"


class OidcStateMismatchError(AuthError):
    """OIDC state 缺失 / 不匹配 / 已消费（回调防 CSRF）。"""
    code = 3015
    status_code = 400
    default_message = "state 缺失或不匹配"


class OidcNonceMismatchError(AuthError):
    """id_token nonce 不匹配（防重放）。"""
    code = 3016
    status_code = 400
    default_message = "nonce 校验失败"


class OidcPkceVerifierError(AuthError):
    """PKCE code_verifier 缺失或不匹配。"""
    code = 3017
    status_code = 400
    default_message = "PKCE code_verifier 校验失败"


class TokenValidationError(AuthError):
    """OIDC token 验签失败 / 算法混淆（HS256 走 RS256 拒收）/ iss·aud·azp 不匹配。"""
    code = 3018
    status_code = 401
    default_message = "Token 验签失败"


class RealmTenantMismatchError(AuthError):
    """Realm 与租户映射不匹配（跨租户 Realm 混淆 / 禁用租户）。"""
    code = 3020
    status_code = 403
    default_message = "Realm 与租户映射不匹配"


class RefreshTokenRevokedError(AuthError):
    """refresh token 已撤销 / 重放（登出后携带已登出 refresh 续期 / rotation 旧 token 复用）。"""
    code = 3019
    status_code = 401
    default_message = "refresh token 已失效，请重新登录"


class OidcShadowUserConflictError(AuthError):
    """SSO 影子用户供给身份冲突（username 命中本地密码账号 / 已绑定其他 sub）。

    不对外泄露「用户名是否已存在」——message 走 i18n 兜底为模糊提示，避免账号枚举。
    """
    code = 3021
    status_code = 403
    default_message = "登录失败，账号身份冲突，请联系管理员"


class OidcSyncRequiredError(AuthError):
    """SSO 首登策略为 synced_only：未同步用户（无影子用户）拒绝登录。

    仅在 Keycloak token 校验通过后、影子用户供给「新建」分支抛出；命中存量
    影子用户 / username 回填不受影响，不泄露「用户是否存在」。
    """
    code = 3022
    status_code = 403
    default_message = "当前租户仅允许已同步用户登录，请联系管理员"


class OidcSessionConflictError(AuthError):
    """OIDC 回调失败：身份提供方侧浏览器已有登录会话（Keycloak ``already_logged_in``）。

    浏览器已持有 Keycloak 会话时重走一次全新授权码流程，Keycloak 侧
    ``type=IDENTITY_PROVIDER_LOGIN_ERROR / error=already_logged_in``，
    回跳 callback 时编码为
    ``error=temporarily_unavailable&error_description=authentication_expired``。
    语义是**会话冲突**而非授权码问题：文案须引导先登出 / 换无痕窗口，
    不能让用户按「重新发起登录」反复重试（重试必再撞同一错误）。
    """
    code = 3023
    status_code = 409
    default_message = "当前浏览器已有登录会话，请先退出登录或改用无痕窗口后重试"


class OidcAuthorizationCodeExpiredError(AuthError):
    """OIDC 回调带 ``error=expired_code`` / ``invalid_grant``：授权码过期或已被使用（URL 重放）。

    与 :class:`OidcAuthorizationCodeError`（3014，授权码兑换环节失败）区分：
    本异常由**回调参数**判定，发生在兑换请求发出之前。
    """
    code = 3024
    status_code = 400
    default_message = "登录链接已失效（授权码已过期或已被使用），请重新发起登录"


class OidcCallbackRejectedError(AuthError):
    """OIDC 回调带未归类的 ``error``（access_denied / invalid_request / server_error 等）。

    原始 ``error`` 经 ``params`` 渲染进用户文案，``error`` / ``error_description``
    原值经 ``details`` 与日志透出，便于排障（不落敏感值）。
    """
    code = 3025
    status_code = 400
    default_message = "登录被身份提供方拒绝（错误：{error}），请重新发起登录"


class ImpersonationRefreshForbiddenError(AuthError):
    """模拟态禁止用原租户 refresh cookie 续期（防跨租户静默覆盖）。

    模拟态自己**没有任何 refresh 凭证**（``create_impersonation_token`` 只签发 access），
    若允许续期，`/auth/refresh` 会就近借用登录留下的**原租户** refresh cookie 换回原租户
    access token —— 界面显示目标租户、请求实际以原租户身份落地（读写双向串租户）。
    本异常是纵深防御层的第一道闸（主逻辑在前端短路），判据见
    ``/auth/impersonate`` 种下的标记 cookie。

    ⛔ **必须 403 而不是 401**：前端 ``request.ts`` 的 401 分支会触发
    ``emitSessionExpired()`` → ``clearAuthStorage()``，而后者会删掉
    ``JONEX_IMPERSONATION_BACKUP_KEY``（原租户 token 的唯一副本）⇒ 管理员被登出
    **且回不到原租户**。403 既不命中 ``canAutoRetry``（要求 401）也不命中登出分支。
    """
    code = 3026
    status_code = 403
    default_message = "模拟租户会话不支持续期，请退出模拟后重试"


class OidcAccessDeniedError(AuthError):
    """SSO 回调失败：IdP 认证已通过，但平台侧认定该账号无访问权限。

    只在 ``id_token`` 验签通过、身份已确认**之后**抛出（影子用户供给与账号状态判定）。
    区别于同段的其他 OIDC 异常（state 失效 / 授权码过期 / PKCE 不符）—— 那些是技术或
    配置故障，用户做什么都没用；本异常对应的三档，用户去找管理员是能解决的。

    ``reason`` 是**语义化枚举**（``sync_required`` / ``account_disabled`` /
    ``identity_conflict``），直接进错误页的 URL query：前端据此选文案，不需要维护
    后端错误码表。

    ``post_login_redirect_uri`` / ``tenant_id`` 随异常带出，是因为路由层拿不到
    OIDC state 记录（``state`` 已被一次性消费），没有这两个值就拼不出 302 目标。

    本异常在路由层就被接走转成 302，正常情况下不会走到全局异常处理器；万一漏接，
    它会退化成一条 403 JSON —— 与改造前的行为一致，不会变成 500。

    ⚠️ 本异常是**非通用构造**的：三个构造参数里 ``reason`` 与 ``post_login_redirect_uri``
    是必填位置参数，``exc_cls(message=..., details=..., params=...)`` 这种通用重建方式套不上。
    这正是它**刻意不登记进 EXCEPTION_REGISTRY** 的原因（见该表 3026 条目后的注释）——
    不要「顺手补漏」把它登记进去。
    """

    code = 3027
    status_code = 403
    default_message = "当前账号无访问权限，请联系管理员"

    def __init__(
        self,
        reason: str,
        post_login_redirect_uri: str,
        tenant_id: Optional[str] = None,
        message: Optional[str] = None,
    ):
        super().__init__(message=message)
        self.reason = reason
        self.post_login_redirect_uri = post_login_redirect_uri
        self.tenant_id = tenant_id


class ImpersonationForbiddenError(AuthError):
    """租户模拟被拒绝（无 platform:admin 或嵌套切换）"""
    code = 3010
    status_code = 403
    default_message = "无权进行租户模拟"


class ImpersonationTargetError(AuthError):
    """租户模拟目标非法（不存在 / default 占位 / == 当前租户）"""
    code = 3011
    status_code = 400
    default_message = "租户模拟目标非法"


class NotImpersonatedError(AuthError):
    """非模拟态调用退出模拟端点"""
    code = 3012
    status_code = 400
    default_message = "当前未处于租户模拟态"


# ==================== 4xxx 数据相关错误 ====================
class DataError(JonexException):
    """数据错误基类"""
    code = 4000
    status_code = 500
    default_message = "数据处理失败"


class DatabaseError(DataError):
    """数据库错误"""
    code = 4001
    status_code = 500
    default_message = "数据库操作失败"


class CacheError(DataError):
    """缓存错误"""
    code = 4002
    status_code = 500
    default_message = "缓存操作失败"


class DataIntegrityError(DataError):
    """数据完整性错误"""
    code = 4003
    status_code = 400
    default_message = "数据完整性约束违反"


class QuotaExceededError(DataError):
    """配额已用尽（阻断创建/上传）。

    details 固定携带需求字段：
    quotaKey / used / reserved / limit / unit。
    """
    code = 4004
    status_code = 409
    default_message = "配额已用尽"


class SyncTaskStateError(DataError):
    """同步任务状态机非法转移（同步中重复发起 / 终态回退 / 同步中停用删除）"""
    code = 4005
    status_code = 409
    default_message = "同步任务状态非法"


class MonitorDataUnavailableError(DataError):
    """平台监控 · 监控数据源缺失 / 历史重算未完成（``4100``）。

    两种同源场景共用一个码：

    * **监控数据源缺失** —— 监控读接口起来时发现其取数表 / 采集链路根本不在位
      （不是「查询没命中」，那是空集 ``200``）；
    * **历史重算未完成** —— 聚合口径切换后历史数据尚在重算，此刻给不出可信数。

    两者都是「**服务端侧暂时给不出这份数**，稍后再来」，故 ``status_code`` 取
    ``503``（取值曾议 503 或 409，此处定为 503；与「采集链路 / 数据源
    不可用」的 5001 同读感，但那是**上游调用失败**，与这里「自家数缺」不同源，故另立一码）。

    ⛔ **本类必须保持「无必填位置参数」** —— ``EXCEPTION_REGISTRY`` 的反查链路
    （``api_gateway/deps.py:raise_from_capability_result`` → ``get_exception_class``）会按
    通用形状 ``exc_cls(message=..., details=..., params=...)`` 重建异常；带必填位参会
    在重建时 ``TypeError`` → 500（先例：``3027 OidcAccessDeniedError`` 因此**刻意不登记**）。
    本类不覆写 ``__init__``，天然满足 —— 后人若加必填参数，请连同登记一起摘掉。

    「超出保留期 / 自定义查询上限」**不归本码**：沿用既有 ``4004``
    （``QuotaExceededError``），``41xx`` 只此一枚。
    """
    code = 4100
    status_code = 503
    default_message = "监控数据源缺失或历史重算未完成，请稍后重试"


# ==================== 5xxx 服务依赖错误 ====================
class ServiceError(JonexException):
    """服务错误基类"""
    code = 5000
    status_code = 503
    default_message = "服务不可用"


class ServiceUnavailableError(ServiceError):
    """服务不可用"""
    code = 5001
    status_code = 503
    default_message = "依赖服务不可用"


class ServiceDiscoveryError(ServiceError):
    """服务发现错误"""
    code = 5002
    status_code = 503
    default_message = "服务发现失败"


class UpstreamServiceError(ServiceError):
    """上游服务错误"""
    code = 5003
    status_code = 502
    default_message = "上游服务返回错误"


class ServiceTimeoutError(ServiceError):
    """服务调用超时"""
    code = 5004
    status_code = 504
    default_message = "服务调用超时"


class DataSourceConnectionError(ServiceError):
    """主数据源连接失败（DNS 解析失败 / TLS 失败 / 连接拒绝）"""
    code = 5005
    status_code = 502
    default_message = "主数据源连接失败"


class DataSourceAuthError(ServiceError):
    """主数据源认证失败（Token 无效 / 过期 / 被拒）"""
    code = 5006
    status_code = 502
    default_message = "主数据源认证失败"


class DataSourceContractError(ServiceError):
    """主数据源 API 契约不符（缺字段 / 字段类型不符 / 增量游标无效）"""
    code = 5007
    status_code = 502
    default_message = "主数据源数据契约不符"


class DataSourceNotVerifiedError(ServiceError):
    """数据源尚未通过连接测试（波 3 门控：必须先测通才能同步）

    `status_code=409` 与 `SyncTaskStateError`(4005) 同族 —— 都是「当前资源状态不允许
    此操作」，不是「上游出错」（5004-5007 全是 502/504）。
    """
    code = 5008
    status_code = 409
    default_message = "主数据源尚未通过连接测试"


# ==================== 异常映射工具 ====================
EXCEPTION_REGISTRY: Dict[int, type] = {
    # 通用错误（支持平台错误码和 HTTP 状态码双映射）
    400: InvalidParameterError,
    1000: InternalError,
    1001: InvalidParameterError,
    1002: ResourceNotFoundError,
    1003: ResourceConflictError,
    1004: OperationNotSupportedError,
    1005: UnsafeTargetUrlError,
    1006: ResourceAlreadyExistsError,
    1007: InvalidTargetUrlError,
    # 能力相关
    2000: CapabilityError,
    2001: CapabilityNotFoundError,
    2002: CapabilityInvokeError,
    2003: CapabilityTimeoutError,
    2004: CapabilityValidationError,
    2005: CapabilityIdFormatError,
    # 认证授权
    3000: AuthError,
    3001: MissingApiKeyError,
    3002: InvalidApiKeyError,
    3003: TokenExpiredError,
    3004: InternalAuthError,
    3005: TenantIsolationError,
    3006: PermissionDeniedError,
    3007: RateLimitExceededError,
    3008: InvalidCredentialsError,
    3009: AccountLockedError,
    3010: ImpersonationForbiddenError,
    3011: ImpersonationTargetError,
    3012: NotImpersonatedError,
    3013: AccountDisabledError,
    3014: OidcAuthorizationCodeError,
    3015: OidcStateMismatchError,
    3016: OidcNonceMismatchError,
    3017: OidcPkceVerifierError,
    3018: TokenValidationError,
    3019: RefreshTokenRevokedError,
    3020: RealmTenantMismatchError,
    3021: OidcShadowUserConflictError,
    3022: OidcSyncRequiredError,
    3023: OidcSessionConflictError,
    3024: OidcAuthorizationCodeExpiredError,
    3025: OidcCallbackRejectedError,
    3026: ImpersonationRefreshForbiddenError,
    # 3027 OidcAccessDeniedError **刻意不登记**（不是漏了）：
    # 它的构造带必填位置参数
    # （``__init__(reason, post_login_redirect_uri, tenant_id=None, message=None)``）。
    # 一旦登记，api_gateway/deps.py 的 raise_from_capability_result 会按通用形状
    # ``exc_cls(message=..., details=..., params=...)`` 重建它 —— reason 缺失 → TypeError → 500。
    # 今天打不着只是因为 api_gateway/routes/auth.py 对下游 4xx 原样透传；这层保护不牢靠，
    # 所以用「不登记」从源头堵死。它由调用方（路由层）就地接住，本就不需要码表反查。
    # 数据相关
    4000: DataError,
    4001: DatabaseError,
    4002: CacheError,
    4003: DataIntegrityError,
    4004: QuotaExceededError,
    4005: SyncTaskStateError,
    4100: MonitorDataUnavailableError,
    # 服务依赖
    5000: ServiceError,
    5001: ServiceUnavailableError,
    5002: ServiceDiscoveryError,
    5003: UpstreamServiceError,
    5004: ServiceTimeoutError,
    5005: DataSourceConnectionError,
    5006: DataSourceAuthError,
    5007: DataSourceContractError,
    5008: DataSourceNotVerifiedError,
}


def get_exception_class(code: int) -> type:
    """
    根据错误码获取对应的异常类

    Args:
        code: 错误码

    Returns:
        异常类
    """
    return EXCEPTION_REGISTRY.get(code, JonexException)
