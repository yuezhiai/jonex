"""租户级安全配置覆盖值。

为什么单独一张表（而不是给 ``system_configs`` 加 tenant_id）
------------------------------------------------------------
``platform.system_configs`` 是**平台共享元数据**：没有 ``tenant_id``、``config_key``
全局唯一，后台任务用 ``list_all_shared()`` 读它。把租户运行数据混进那张表会同时踩两件事：

1. 项目规范（CLAUDE.md 编辑要求）：「不要把平台共享元数据和租户运行数据混为一类」；
2. 既有读法：``list_all_shared()`` / ``get_by_key()`` 的语义会变成「取到任意一个租户的值」，
   排查时无法区分看到的是全局值还是某个租户的覆盖值。

所以本表只存**覆盖值**：某租户没配过的键，这里**没有行**，读取时回落到平台默认/代码默认。
「没配」与「配成了和默认一样的值」因此是可区分的 —— 前者删行即恢复默认，后者是显式选择。
"""

from sqlalchemy import BigInteger, Column, String, Text, UniqueConstraint

from jonex_core.common.database import Base
from jonex_core.common.entity import TenantMixin, TimestampMixin


class TenantSecurityConfig(TenantMixin, TimestampMixin, Base):
    """租户级安全配置（一租户一键一行，只存覆盖值）。

    ``config_key`` 与 ``system_configs`` 同名同义（``session_timeout`` 等），
    但这里**只在租户内唯一**：``UniqueConstraint("tenant_id", "config_key")``。
    只按 ``config_key`` 唯一会让「第一个租户设过之后，别的租户再也设不了」。
    """

    __tablename__ = "tenant_security_configs"
    __table_args__ = (
        UniqueConstraint("tenant_id", "config_key", name="uq_tenant_security_configs_tenant_key"),
        {"schema": "platform"},
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    config_key = Column(String(128), nullable=False)
    config_value = Column(Text)
    value_type = Column(String(32), default="int")
    description = Column(String(512))
