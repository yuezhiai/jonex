# -*- coding:utf-8 -*-
"""监控调用方标识（``monitor.call_log.source``）的解析与生成。

监控的「调用方」有两种性质完全不同的取值，必须分清：

- **稳定标识** ``user:<本地用户主键>``：代表某个平台内真实用户。主键是
  ``BIGSERIAL`` 全局唯一，用户改名后它不变，所以采集端只写它。
- **系统标识**：``apikey`` / ``platform`` / ``anonymous`` / ``unknown`` 等，
  代表「某个系统在调」而不是某个人，原样写入，不做解析。

展示名（``users.username``）**只在读侧从数据库取**，绝不写进 ``source``：
用户名可变、且只在租户内唯一，写进记录后改名就会让同一个人分裂成两条来源。

历史数据里还存在裸数字（旧采集端写 ``user_id`` 原值）与用户名形态，
读侧解析时与 ``user:<id>`` 等价对待，迁移会把它们归一。
"""

import re
from typing import Optional, Union

# 与 ``monitor.call_log.source`` 列宽（VARCHAR(128)）对齐
SOURCE_MAX_LEN = 128

# 非用户形态的保留值：它们各有既定语义，不能当作人名或用户主键解析
RESERVED_SOURCES = frozenset(
    {"apikey", "platform", "anonymous", "unknown", "test", "jwt", "mcp", "system"}
)

_MAX_BIGINT = 2**63 - 1

# 历史裸 ID 与规范形态共用同一 token；组内只认 ASCII 数字
_LEGACY_ID_RE = re.compile(r"^([0-9]{1,18})$")
_USER_PREFIX_RE = re.compile(r"^user:([0-9]{1,18})$")
# 生成稳定标识时更严格：正整数、无前导零、不越界
_STRICT_ID_RE = re.compile(r"^[1-9][0-9]{0,18}$")


def parse_source_token(source: Optional[str]) -> Optional[str]:
    """``source`` → 用户主键 token；非用户形态返回 ``None``。

    ``123`` 与 ``user:123`` 都产出 ``"123"`` —— 两者是同一个人的不同历史写法。
    保留字（``apikey`` 等）与用户名形态返回 ``None``，由调用方决定回退策略。
    """
    if not source:
        return None
    match = _LEGACY_ID_RE.match(source)
    if match:
        return match.group(1)
    match = _USER_PREFIX_RE.match(source)
    if match:
        return match.group(1)
    return None


def is_user_identity(source: Optional[str]) -> bool:
    """``source`` 是否已是规范形态 ``user:<id>``。"""
    return bool(source) and _USER_PREFIX_RE.match(source) is not None


def is_reserved_source(source: Optional[str]) -> bool:
    """``source`` 是否为系统标识（大小写不敏感）。"""
    return bool(source) and source.strip().lower() in RESERVED_SOURCES


def format_user_identity(user_id: Union[int, str, None]) -> Optional[str]:
    """本地用户主键 → ``user:<id>``；无法确定为合法主键时返回 ``None``。

    只接受 ASCII 正整数且不越 BIGINT：``external_id``、UUID、``mcp`` 这类
    「看起来像 ID 但不是本地主键」的值一律不加前缀 —— 加了会把调用方
    错误归因到另一个真实账号。

    已是 ``user:<id>`` 的入参会原样归一（幂等），便于采集与迁移共用。
    """
    if user_id is None or isinstance(user_id, bool):
        return None
    if isinstance(user_id, int):
        value = user_id
    else:
        raw = str(user_id).strip()
        if raw.lower().startswith("user:"):
            raw = raw[5:].strip()
        if _STRICT_ID_RE.match(raw) is None:
            return None
        value = int(raw)
    if value <= 0 or value > _MAX_BIGINT:
        return None
    return f"user:{value}"


__all__ = [
    "RESERVED_SOURCES",
    "SOURCE_MAX_LEN",
    "format_user_identity",
    "is_reserved_source",
    "is_user_identity",
    "parse_source_token",
]
