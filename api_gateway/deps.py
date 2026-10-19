"""
Gateway 共享依赖 — 避免 main.py 与 routes/ 之间循环导入。
"""
from fastapi import Depends, Header

from jonex_core.common.exceptions import MissingApiKeyError, get_exception_class, JonexException, CapabilityInvokeError
from jonex_core.common.i18n import translate


async def require_auth_header(authorization: str = Header(None)) -> str:
    """认证门：校验 Authorization 的**签名**，不只检查前缀。

    历史实现只检查 ``Bearer `` 前缀，``Bearer x`` 也能放行；Sidecar 随后走
    API Key 认证、user_id 为空，能力层的分级守卫（依赖真实用户身份）整段跳过。
    这里在网关入口完成验签，让所有 invoke 路由拿到确定的用户身份。

    * 缺头 / 非 Bearer / 空 token → 401；
    * ``jonex_test_*`` 仍受 ``require_test_token_allowed()`` 的环境门控；
    * 其余 token 走 ``UserAuth.decode_token``，验签失败/过期由它抛 401；
    * ``type`` 非 ``user``（如 refresh）不能当访问令牌。

    身份解析仍由 ``api_gateway.identity`` 独立完成；本依赖只做认证门。
    """
    if not authorization:
        raise MissingApiKeyError(message=translate("err.auth.missing_auth_header", fallback="缺少 Authorization 请求头"))
    if not authorization.startswith("Bearer "):
        raise MissingApiKeyError(
            message=translate("err.auth.invalid_auth_format", fallback="Authorization 格式无效，请使用: Bearer <token>")
        )
    token = authorization[7:].strip()
    if not token:
        raise MissingApiKeyError(
            message=translate("err.auth.empty_bearer_token", fallback="Authorization 令牌为空")
        )
    if token.startswith("jonex_test_"):
        from jonex_core.common.tenant import require_test_token_allowed

        require_test_token_allowed()
        return authorization
    from jonex_core.security.user_auth import get_user_auth

    payload = get_user_auth().decode_token(token)
    if payload.get("type") != "user":
        raise MissingApiKeyError(
            message=translate("err.auth.user_token_required", fallback="需要用户访问令牌")
        )
    return authorization


def raise_from_capability_result(result: dict):
    """把下游能力信封错误重建为带真实 code/params 的异常，交全局 handler 翻译。

    下游若已用 code+params 抛出，Gateway 重建的异常带真实 code+params，
    经 handler -> error_response 命中模板翻译；若仍是历史显式原文，params 为空、原样返回不回归。
    """
    code = result.get("code", 2002)
    if isinstance(code, str):
        try:
            code = int(code)
        except (ValueError, TypeError):
            code = 2002
    exc_cls = get_exception_class(code)
    if not issubclass(exc_cls, JonexException):
        exc_cls = CapabilityInvokeError
    raise exc_cls(
        message=result.get("message"),
        details=result.get("details") or None,
        params=result.get("params") or None,
    )
