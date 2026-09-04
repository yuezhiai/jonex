#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""
Jonex 平台 - 系统级配额配置模块

单环境变量 ``JONEX_SYSTEM_QUOTA_CONFIG``（JSON 字符串）承载全部配额上限，
对当前部署环境内所有租户统一生效（Preview 阶段不支持租户级覆盖）。

- 解析 + 校验在服务启动时执行：配置缺失 / JSON 非法 / 字段缺失 / 非正整数 /
  单知识库文档上限大于单租户文档总上限 → 抛 QuotaConfigError，拒绝启动。
- 容量统一按 MiB 配置，后端按 Byte 计算（to_bytes）。
- 文件类型按「配额类别」分类：document / spreadsheet / image / audio / video / other。

"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from functools import lru_cache
from typing import Optional


class QuotaConfigError(Exception):
    """配额配置非法（拒绝启动）。"""


# 配额项定义：配置键 -> (类型, 展示单位)
# 类型：count=整数个数 / bytes=按 MiB 配置、按 Byte 计算 / duration=分钟（二期）
_QUOTA_DEFS: dict[str, tuple[str, str]] = {
    "tenantKnowledgeBaseLimit": ("count", "个"),
    "tenantDocumentLimit": ("count", "个"),
    "tenantStorageLimitMiB": ("bytes", "MiB"),
    "knowledgeBaseDocumentLimit": ("count", "个"),
    "uploadFileCountLimit": ("count", "个"),
    "documentFileSizeLimitMiB": ("bytes", "MiB"),
    "spreadsheetFileSizeLimitMiB": ("bytes", "MiB"),
    "imageFileSizeLimitMiB": ("bytes", "MiB"),
    "audioFileSizeLimitMiB": ("bytes", "MiB"),
    "audioDurationLimitMinutes": ("duration", "分钟"),
    "videoFileSizeLimitMiB": ("bytes", "MiB"),
    "videoDurationLimitMinutes": ("duration", "分钟"),
}

QUOTA_KEYS: tuple[str, ...] = tuple(_QUOTA_DEFS)

_BYTES_KEYS: frozenset[str] = frozenset(
    k for k, (kind, _) in _QUOTA_DEFS.items() if kind == "bytes"
)

# 类别 -> 大小配额键（仅 bytes 类）；other 无对应键
_SIZE_LIMIT_BY_CATEGORY: dict[str, str] = {
    "document": "documentFileSizeLimitMiB",
    "spreadsheet": "spreadsheetFileSizeLimitMiB",
    "image": "imageFileSizeLimitMiB",
    "audio": "audioFileSizeLimitMiB",
    "video": "videoFileSizeLimitMiB",
}


def _unit_of(key: str) -> str:
    return _QUOTA_DEFS[key][1]


@dataclass(frozen=True)
class SystemQuota:
    """已校验的配额配置快照。values 为原始配置值（正整数）。"""

    values: dict[str, int]

    def get(self, key: str) -> int:
        """返回原始配置值（count 项=个数，bytes 项=MiB，duration 项=分钟）。"""
        return self.values[key]

    def to_bytes(self, key: str) -> int:
        """bytes 项转为 Byte；非 bytes 项原样返回。"""
        value = self.values[key]
        if key in _BYTES_KEYS:
            return value * 1024 * 1024
        return value

    def limit(self, key: str) -> int:
        """归一化限额：bytes 项返回 Byte，其余返回原值。校验/展示统一入口。"""
        return self.to_bytes(key)

    def unit(self, key: str) -> str:
        return _unit_of(key)


def size_limit_key(category: str) -> Optional[str]:
    """文件配额类别 -> 大小配额键；无对应项返回 None。"""
    return _SIZE_LIMIT_BY_CATEGORY.get(category)


def load_system_quota(raw: Optional[str] = None) -> SystemQuota:
    """解析并校验配额配置。

    raw 为 None 时读环境变量 ``JONEX_SYSTEM_QUOTA_CONFIG``；缺失/非法抛 QuotaConfigError。
    """
    if raw is None:
        raw = os.getenv("JONEX_SYSTEM_QUOTA_CONFIG")
    if not raw or not raw.strip():
        raise QuotaConfigError("JONEX_SYSTEM_QUOTA_CONFIG 未配置")

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise QuotaConfigError(f"JONEX_SYSTEM_QUOTA_CONFIG 不是合法 JSON: {exc}") from exc

    if not isinstance(data, dict):
        raise QuotaConfigError("JONEX_SYSTEM_QUOTA_CONFIG 必须为 JSON 对象")

    values: dict[str, int] = {}
    for key in QUOTA_KEYS:
        if key not in data:
            raise QuotaConfigError(f"JONEX_SYSTEM_QUOTA_CONFIG 缺少字段: {key}")
        val = data[key]
        # 正整数校验：拒绝 bool（bool 是 int 子类）、拒绝 0/负数/非整数
        if isinstance(val, bool) or not isinstance(val, int) or val <= 0:
            raise QuotaConfigError(f"配额字段 {key} 必须为正整数，当前值: {val!r}")
        values[key] = val

    if values["knowledgeBaseDocumentLimit"] > values["tenantDocumentLimit"]:
        raise QuotaConfigError(
            "knowledgeBaseDocumentLimit 不得大于 tenantDocumentLimit"
        )

    return SystemQuota(values=values)


@lru_cache(maxsize=1)
def get_system_quota() -> SystemQuota:
    """获取系统配额单例（首次调用即解析校验，非法抛错）。"""
    return load_system_quota()


# ── 文件类型 → 配额类别 ─────────────────────────────────────────

_MIME_PREFIX_CATEGORY: tuple[tuple[str, str], ...] = (
    ("image/", "image"),
    ("audio/", "audio"),
    ("video/", "video"),
)

_SPREADSHEET_MIME: frozenset[str] = frozenset({
    "application/vnd.ms-excel",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.ms-excel.sheet.macroenabled.12",
    "text/csv",
    "application/csv",
})

_EXT_CATEGORY: dict[str, str] = {
    # document
    ".pdf": "document", ".doc": "document", ".docx": "document",
    ".ppt": "document", ".pptx": "document",
    ".txt": "document", ".md": "document", ".markdown": "document",
    # spreadsheet
    ".xls": "spreadsheet", ".xlsx": "spreadsheet", ".csv": "spreadsheet", ".ods": "spreadsheet",
    # image
    ".png": "image", ".jpg": "image", ".jpeg": "image", ".gif": "image",
    ".bmp": "image", ".tiff": "image", ".tif": "image", ".webp": "image",
    # audio
    ".mp3": "audio", ".wav": "audio", ".flac": "audio", ".aac": "audio",
    ".m4a": "audio", ".ogg": "audio", ".wma": "audio", ".opus": "audio", ".amr": "audio",
    # video
    ".mp4": "video", ".avi": "video", ".mov": "video", ".mkv": "video",
    ".flv": "video", ".wmv": "video", ".webm": "video", ".m4v": "video",
    ".mpg": "video", ".mpeg": "video", ".3gp": "video",
}


def classify_quota_category(mime_type: Optional[str], file_name: Optional[str]) -> str:
    """按 mime_type 优先、扩展名兜底，把文件归入配额类别。

    返回 document / spreadsheet / image / audio / video / other。
    other = 无法识别，上传时按需求 §4.1 第一步「文件类型校验」拒绝。
    """
    mt = (mime_type or "").strip().lower()
    for prefix, category in _MIME_PREFIX_CATEGORY:
        if mt.startswith(prefix):
            return category
    if mt in _SPREADSHEET_MIME:
        return "spreadsheet"
    if mt == "application/pdf" or mt.startswith("text/"):
        return "document"
    ext = os.path.splitext(file_name or "")[1].lower()
    return _EXT_CATEGORY.get(ext, "other")


__all__ = [
    "QUOTA_KEYS",
    "QuotaConfigError",
    "SystemQuota",
    "classify_quota_category",
    "get_system_quota",
    "load_system_quota",
    "size_limit_key",
]
