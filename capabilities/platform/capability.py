"""
平台管理能力 (business.platform.v1)

作为独立容器运行，提供：
- 认证业务逻辑（login/refresh/ticket，由 Sidecar 代理调用）
- 平台管理 CRUD（用户/角色/权限/菜单/应用/配置/审计/任务）
"""
import logging

from jonex_core.capability import BaseCapability
from jonex_core.capability.models import (
    CapabilityMetadata,
    CapabilityRequest,
    CapabilityResponse,
    CapabilityType,
)
from jonex_core.common.config import get_config
from jonex_core.common.security_guards import (
    assert_secure_jwt_secret,
    is_insecure_keycloak_client_secret,
)

logger = logging.getLogger(__name__)


def _assert_keycloak_client_secret_configured(config) -> None:
    """生产环境守卫：KEYCLOAK_CLIENT_SECRET 必须已从**仓库内公开值**覆盖。

    生产漏配时平台能力会静默使用公开可知的字符串作为 Keycloak confidential
    client secret，攻击者可据此伪造 client 身份。

    > ⚠️ 2026-10-14：原先只比对哨兵串 ``change-me-in-production``，**漏掉公开的
    > ``jonex-platform-public-pkce-client``**（实测生产正在用）。现改用共享判定
    > :func:`is_insecure_keycloak_client_secret`。

    fail-closed：生产未正确配置则抛 RuntimeError，拒绝启动。
    """
    if not config.is_production:
        return
    # SSO 未启用的部署不消费该密钥（全仓运行时唯一读取者即本守卫），
    # 不得让与 SSO 无关的部署因哨兵值而启动失败。
    if not config.sso_enabled:
        return
    if is_insecure_keycloak_client_secret(config.KEYCLOAK_CLIENT_SECRET):
        raise RuntimeError(
            "KEYCLOAK_CLIENT_SECRET 未配置或仍为仓库内公开的默认值"
            "（含 change-me-in-production、jonex-platform-public-pkce-client）。"
            "生产环境必须在 .env 或环境变量中设置 KEYCLOAK_CLIENT_SECRET 为真实随机 secret，"
            "否则 Keycloak confidential client secret 可被攻击者猜到。"
        )


class PlatformCapability(BaseCapability):
    """平台管理能力 — 独立容器"""

    def __init__(self):
        super().__init__()

    def _build_metadata(self) -> CapabilityMetadata:
        return CapabilityMetadata(
            capability_id="platform",
            capability_name="平台管理能力",
            capability_type=CapabilityType.BUSINESS,
            version="v1",
            description="用户认证、RBAC、菜单、应用、系统配置、审计日志、任务调度",
            author="jonex",
        )

    async def validate_input(self, request: CapabilityRequest) -> bool:
        return True

    async def execute(self, request: CapabilityRequest) -> CapabilityResponse:
        return CapabilityResponse.ok(data={"message": "platform capability"})

    async def initialize(self) -> None:
        """启动审计日志 sink 后台任务 + 自动同步调度器"""
        # 安全守卫：生产环境 KEYCLOAK_CLIENT_SECRET 必须已覆盖默认哨兵值，
        # fail-closed 拒绝启动（RuntimeError 不吞掉）。
        _assert_keycloak_client_secret_configured(get_config())
        # 安全守卫：JWT_SECRET 必须为真实随机值（**生产档 ENV=prod 硬拦**，非生产只告警；
        # 见 jonex_core/common/security_guards.py 约束 2）。平台侧既签发用户 JWT，
        # 又用 _sign_payload 计算 MCP Key 哈希，泄漏即可伪造管理员并批量作废用户 Key。
        assert_secure_jwt_secret(get_config())
        try:
            from capabilities.platform.services.audit_log_sink import get_audit_log_sink
            sink = get_audit_log_sink()
            sink.start()
            logger.info("AuditLogSink 已启动")
        except Exception:
            logger.exception("AuditLogSink 启动失败")

        # 调用明细采集写口（monitor.call_log / monitor.call_attempt）。
        # 同 AuditLogSink 的理由：采集是旁路，起不来绝不能拖垮 platform 本体。
        # 采集器住在共享内核（jonex_core/common/monitor_collector.py），各采集方各自起一份。
        try:
            from jonex_core.common.monitor_collector import get_monitor_collector
            get_monitor_collector().start()
            logger.info("MonitorCollector 已启动")
        except Exception:
            logger.exception("MonitorCollector 启动失败（不影响平台功能）")

        # 企业身份集成「自动同步」调度器（闹钟 + 电池）：扫到期租户 → CAS 抢占 → 发起同步。
        # ⚠️ **必须 try/except 包住**：`deploy/start_capability.py:98` 是裸的
        # `await capability.initialize()`，抛出即能力起不来 —— 而 platform 是认证关键
        # 服务（登录 / 刷新 / SSO 都走它），**自动同步起不来绝不能拖垮登录**。
        # 失败只丢一行 ERROR，调度器不跑而已（用户可继续用「立即同步」手动触发）。
        try:
            from capabilities.platform.services.identity_sync_scheduler import start_scheduler
            await start_scheduler()
            logger.info("IdentitySyncScheduler 已启动")
        except Exception:
            logger.exception("IdentitySyncScheduler 启动失败（不影响登录等平台功能）")

        # 审计保留清理调度器：按 AUDIT_RETENTION_DAYS 定期删过期审计日志
        # （此前无任何调用方，保留天数是死配置）。同样 try/except 兜住 —— 清理
        # 起不来不能拖垮认证关键服务；``interval <= 0`` 时 start 返回 None，即显式关闭。
        try:
            from capabilities.platform.services.audit_retention_scheduler import (
                start_scheduler as start_audit_retention_scheduler,
            )
            audit_retention_scheduler = await start_audit_retention_scheduler()
            if audit_retention_scheduler is None:
                logger.info(
                    "AuditRetentionScheduler 已关闭"
                    "（AUDIT_RETENTION_SCHEDULER_INTERVAL_SECONDS<=0）"
                )
            else:
                logger.info("AuditRetentionScheduler 已启动")
        except Exception:
            logger.exception("AuditRetentionScheduler 启动失败（不影响登录等平台功能）")

        # 监控明细保留清理调度器：按 MONITOR_RETENTION_DAYS 定期删过期的
        # monitor.call_log / monitor.call_attempt。同上用 try/except 兜住。
        try:
            from capabilities.platform.services.monitor_retention_scheduler import (
                start_scheduler as start_monitor_retention_scheduler,
            )
            monitor_retention_scheduler = await start_monitor_retention_scheduler()
            if monitor_retention_scheduler is None:
                logger.info(
                    "MonitorRetentionScheduler 已关闭"
                    "（MONITOR_RETENTION_SCHEDULER_INTERVAL_SECONDS<=0）"
                )
            else:
                logger.info("MonitorRetentionScheduler 已启动")
        except Exception:
            logger.exception("MonitorRetentionScheduler 启动失败（不影响平台功能）")

    async def shutdown(self) -> None:
        """停止审计日志 sink，flush 剩余条目；停自动同步调度器"""
        try:
            from capabilities.platform.services.audit_log_sink import get_audit_log_sink
            sink = get_audit_log_sink()
            await sink.stop()
            logger.info("AuditLogSink 已停止")
        except Exception:
            logger.exception("AuditLogSink 停止失败")

        try:
            from jonex_core.common.monitor_collector import get_monitor_collector
            await get_monitor_collector().stop()
            logger.info("MonitorCollector 已停止")
        except Exception:
            logger.exception("MonitorCollector 停止失败")

        try:
            from capabilities.platform.services.identity_sync_scheduler import stop_scheduler
            await stop_scheduler()
            logger.info("IdentitySyncScheduler 已停止")
        except Exception:
            logger.exception("IdentitySyncScheduler 停止失败")

        try:
            from capabilities.platform.services.monitor_retention_scheduler import (
                stop_scheduler as stop_monitor_retention_scheduler,
            )
            await stop_monitor_retention_scheduler()
            logger.info("MonitorRetentionScheduler 已停止")
        except Exception:
            logger.exception("MonitorRetentionScheduler 停止失败")

        try:
            from capabilities.platform.services.audit_retention_scheduler import (
                stop_scheduler as stop_audit_retention_scheduler,
            )
            await stop_audit_retention_scheduler()
            logger.info("AuditRetentionScheduler 已停止")
        except Exception:
            logger.exception("AuditRetentionScheduler 停止失败")

    def register_routes(self, app):
        """注册平台管理 REST API 路由"""
        from capabilities.platform.api import create_platform_router

        router = create_platform_router()
        app.include_router(router, prefix="/api/v1")
