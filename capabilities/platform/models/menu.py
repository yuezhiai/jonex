from sqlalchemy import Column, BigInteger, String, SmallInteger

from jonex_core.common.database import Base
from jonex_core.common.entity import SoftDeleteMixin, TimestampMixin


class Menu(TimestampMixin, SoftDeleteMixin, Base):
    __tablename__ = "menus"
    __table_args__ = {"schema": "platform"}

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    parent_id = Column(BigInteger, default=0)
    name = Column(String(128), nullable=False)
    path = Column(String(256))
    icon = Column(String(128))
    app_id = Column(BigInteger)
    sort_order = Column(SmallInteger, default=0)
    visible = Column(SmallInteger, default=1)
    status = Column(SmallInteger, default=1)
    permission_code = Column(String(128))
