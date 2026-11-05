"""文本规范化 + SimHash（相似检测的候选预筛，不参与最终判定）。

口径变更（归一化方式 / 哈希 key / 闸门公式）会让存量签名全部失效，
如需改动必须安排全量重签。
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Iterator

_WS = re.compile(r"\s+")
_HASH_KEY = b"jonex-simhash-v1"  # 固定盐：跨进程/跨版本稳定
_LANE_MASK = 0xFFFFFFFF

# 每个字节 8 个 bit 的「分道」位图：第 i 个 bit 落在 32*i 位的 lane 上。
_EXPB = [
    sum(1 << (i * 32) for i in range(8) if (byte >> i) & 1)
    for byte in range(256)
]


def normalize_text(text: str) -> str:
    """NFKC → 去首尾 → 空白折叠 → 小写；英文/全角/连字统一后再做 shingle。"""
    normalized = unicodedata.normalize("NFKC", text or "")
    return _WS.sub(" ", normalized.strip()).lower()


def _validate_args(size: int, limit: int) -> None:
    if size < 1 or limit < 1 or limit > (1 << 31) - 1:
        raise ValueError("size must be >= 1 and 1 <= limit < 2**31")


def _iter_grams(text: str, *, size: int, limit: int) -> Iterator[tuple[str, int]]:
    """按独立哈希闸门抽样产出 (gram, 签名哈希)。

    闸门用摘要高 8 字节与 limit/total 阈值比较，而不是 h % stride：
    stride 由长度取整得到，长度跨 k*limit 时整批采样会重排（实测相似度掉到
    0.45~0.73）；阈值随 total 连续变化，小幅长度变化只翻转极少数采样点。

    同一 gram 重复出现时闸门「全进全出」；低多样性文本（如重复标点、空表格
    行）可能采到 0 个样本，此时 ``simhash64`` 返回 ``None``。
    """
    _validate_args(size, limit)

    def _gen() -> Iterator[tuple[str, int]]:
        total = len(text) - size + 1
        if total <= 0:
            return
        gate = (limit << 64) // total
        for i in range(total):
            digest = hashlib.blake2b(
                text[i:i + size].encode("utf-8"),
                digest_size=16,
                key=_HASH_KEY,
            ).digest()
            if int.from_bytes(digest[8:], "big") >= gate:
                continue
            yield text[i:i + size], int.from_bytes(digest[:8], "big")

    return _gen()


def iter_shingles(
    text: str, *, size: int = 5, limit: int = 200_000,
) -> Iterator[str]:
    """按内容哈希抽样产出字符 shingle（不是位置抽样；产出数约 limit）。"""
    return (gram for gram, _h in _iter_grams(text, size=size, limit=limit))


def simhash64(text: str, *, limit: int = 200_000) -> int | None:
    """返回低 63 位的非负 SimHash（与 PostgreSQL BIGINT 同口径）。

    - 抽样按内容哈希（闸门与长度连续相关），编辑后采样集合稳定；
    - 无可用 shingle 返回 ``None``（文本过短或低多样性导致 0 样本），
      调用方必须跳过，禁止把 0 当作有效签名；
    - 成本 ∝ len(text)，2M 字符约数秒；
    - 权重平票（0）归 0。
    """
    acc = 0
    count = 0
    for h in (
        h for _gram, h in _iter_grams(normalize_text(text), size=5, limit=limit)
    ):
        acc = (
            acc
            + _EXPB[h & 0xFF]
            + (_EXPB[(h >> 8) & 0xFF] << 256)
            + (_EXPB[(h >> 16) & 0xFF] << 512)
            + (_EXPB[(h >> 24) & 0xFF] << 768)
            + (_EXPB[(h >> 32) & 0xFF] << 1024)
            + (_EXPB[(h >> 40) & 0xFF] << 1280)
            + (_EXPB[(h >> 48) & 0xFF] << 1536)
            + (_EXPB[(h >> 56) & 0xFF] << 1792)
        )
        count += 1
    if count == 0:
        return None
    bits = 0
    for bit in range(64):
        lane = (acc >> (bit * 32)) & _LANE_MASK
        if lane * 2 > count:
            bits |= 1 << bit
    return bits & ((1 << 63) - 1)


def estimate_similarity(a: int, b: int) -> float:
    # 分母保持 64：bit 63 恒为 0，无关文本基线约 0.5，阈值 0.8 仍有充足余量
    return 1.0 - ((a ^ b).bit_count() / 64.0)
