from sqlalchemy import Column, BigInteger, String

from jonex_core.common.database import Base
from jonex_core.common.entity import TimestampMixin


class Permission(TimestampMixin, Base):
    __tablename__ = "permissions"
    __table_args__ = {"schema": "platform"}

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    code = Column(String(128), nullable=False, unique=True)
    name = Column(String(255), nullable=False)
    resource = Column(String(128), nullable=False)
    action = Column(String(64), nullable=False)
    description = Column(String(512))
    scope = Column(String(16), default="tenant")
