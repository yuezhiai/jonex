"""Identity of the parser inputs that produced persisted SourceArtifacts.

── 这个指纹是干什么的 ──

每份解析产物（``gen-N/source_artifacts.jsonl``）都记着一个
``parse_input_signature``。"解析身份"就是「产出这份产物的规则集合」的指纹：

    SHA256( 规则清单 + 规则代码 + 打包预算 + 体检策略 + 文本编码 + MinerU 版本 )

编译时拿产物里记的值和**现在算出来的值**比对，不一致就整体拒绝编译
（``adapters/source_port.py``）—— bundle 必须是可复现的产物，混两代解析结果会得到
无法解释的页面。这个硬失败是对的，问题只在于「什么算规则」必须定准。

── A：只哈希真正决定产物内容的文件（``PARSE_RULE_SOURCES``）──

2026-10-20 之前这里是「把 ``domain/parsing/`` 与 ``source_prep/`` 下**所有** ``.py``
都哈希」。于是往 ``parse_task.py``（解析任务编排：解析完顺带排编译任务）里传一个
编译模型参数，就让**所有 KB 的所有历史产物**瞬间判为过期、全库编译全挂 —— 一次与
解析输出毫无关系的改动，代价却是全量重解析。

所以现在显式列清单。**「加进来才算」**：

* 新文件若**不改写**解析产物（任务编排、纯导出、测试辅助），**不要**加进来：
  加进来 = 它一改就全库重解析。典型不在清单里：``parse_task.py``（编排）、
  ``identity.py``（算指纹自己）、``__init__.py``。
* 新文件若参与「原始文件 → block / 阅读材料 / 表结构 / 打包」这条链，**必须**加进来，
  否则改了它旧产物会被**误判有效**（比多解析一次危险得多）。
* 清单本身进指纹：增删条目必然改变签名，不会有「加了文件但签名没变」的静默失效。
* 判断只有一句话：**这个文件的内容变了，同一份输入文件产出的产物会不一样吗？**

── B：改产物形状但不在清单里的改动，必须手工 +1（``PARSE_OUTPUT_VERSION``）──

``parse_task.py`` 负责组装产物（哪几个字段写进 ``source_metadata``、怎么序列化）。
它不在清单里（它是编排，不是规则），所以**凡是要动产物内容/shape 的改动，包括改
``parse_task.py`` 的组装逻辑，必须把 ``PARSE_OUTPUT_VERSION`` +1**。这是 A 的兜底：
漏进清单的规则文件还能靠它拦住；两条都漏 = 旧产物被当成新的用。

改本文件指纹算法自身的 payload 形状（字段增删/含义变化）时 bump
``PARSE_IDENTITY_SCHEMA``。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from functools import lru_cache
from pathlib import Path

from jonex_wiki.domain.engine.compile.source_prep.chunking import PackingOptions
from jonex_wiki.domain.engine.compile.source_prep.inspect import InspectionPolicy

# 产物内容的**手工版本号**（B）。改产物形状/内容而没进 PARSE_RULE_SOURCES → +1。
#   1 = 初版（structure-pack 产物 + parse_input_signature）
#   2 = 产物带 parse_input_components（分量摘要，让过期原因可诊断；2026-10-21）
PARSE_OUTPUT_VERSION = 4  # source-image anchors and no-text images remain readable

# 指纹 payload 自身的结构版本（与 A/B 无关）：字段增删或含义变化 → +1。
#   1 = code / packing / inspection / text_encoding / mineru_model_version
#   2 = 显式规则清单 rule_set + parse_output_version（2026-10-20 收窄指纹范围）
PARSE_IDENTITY_SCHEMA = 3

# ── 规则集合（A）：只有这些文件的改动才代表「解析规则变了」 ──
# 路径相对 `src/jonex_wiki/`；**增删条目会让全部历史产物失效**（清单进哈希）。
PARSE_RULE_SOURCES: tuple[str, ...] = (
    "domain/image_identity.py",
    "domain/parsing/consumer_input.py",
    "domain/parsing/media_timeline.py",
    "domain/parsing/pdf_text_layer.py",
    "domain/engine/compile/source_prep/docx_native.py",
    # 解析入口：原始文件 → block
    "domain/parsing/direct.py",
    "domain/parsing/mineru/common.py",
    "domain/parsing/mineru/cloud.py",
    "domain/parsing/mineru/local.py",
    "domain/parsing/mineru/self_host.py",
    # 解析后的投影 / 打包 / 体检 / 表结构（产物内容的实际决定者）
    "domain/engine/compile/source_prep/blocks.py",
    "domain/engine/compile/source_prep/chunking.py",
    "domain/engine/compile/source_prep/classify.py",
    "domain/engine/compile/source_prep/direct_ingest.py",
    "domain/engine/compile/source_prep/header_interpretation.py",
    "domain/engine/compile/source_prep/inspect.py",
    "domain/engine/compile/source_prep/raw_manifest.py",
    "domain/engine/compile/source_prep/reading_materials.py",
    "domain/engine/compile/source_prep/screening.py",
    "domain/engine/compile/source_prep/source_archive.py",
    "domain/engine/compile/source_prep/spreadsheet_view.py",
    "domain/engine/compile/source_prep/table_artifact.py",
    "domain/engine/compile/source_prep/tables.py",
)

_PACKAGE_ROOT = Path(__file__).resolve().parents[2]   # …/jonex_wiki


@lru_cache(maxsize=1)
def parser_code_checksum() -> str:
    """规则清单内文件的逐文件哈希（含路径，重命名也算变化）。"""
    digest = hashlib.sha256()
    for rel in PARSE_RULE_SOURCES:
        path = _PACKAGE_ROOT / rel
        digest.update(rel.encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def parse_identity_payload(settings) -> dict:
    """参与指纹的全部输入（也是排障时「到底哪一项变了」的答案来源）。"""
    return {
        "schema": PARSE_IDENTITY_SCHEMA,
        "parse_output_version": PARSE_OUTPUT_VERSION,
        "rule_set": list(PARSE_RULE_SOURCES),
        "code": parser_code_checksum(),
        "packing": asdict(PackingOptions.from_settings(settings)),
        "inspection": asdict(InspectionPolicy.from_settings(settings)),
        "text_encoding": settings.text_encoding,
        "mineru_model_version": settings.mineru_model_version,
        "media_keyframes": {"enabled": getattr(settings, "media_keyframes_enabled", True),
            "max_frames": getattr(settings, "media_keyframes_max_frames", 32),
            "interval_seconds": getattr(settings, "media_keyframes_interval_seconds", 5.0)},
        "pdf_text_layer": {"enabled": getattr(settings, "pdf_text_layer_enabled", True),
            "max_pages": getattr(settings, "pdf_text_layer_max_pages", 128)},
    }


def _canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":")).encode("utf-8")


def parse_input_signature(settings) -> str:
    """Hash the rule set, its code and the parse settings; never credentials."""
    return hashlib.sha256(_canonical(parse_identity_payload(settings))).hexdigest()


def parse_input_components(settings) -> dict[str, str]:
    """每个分量各自的短摘要 —— 写进产物，用来回答「哪一项变了」。

    只记最终大指纹的话，产物过期时只能说「解析规则已变化」，「是代码变了还是
    MinerU 版本变了」得靠反推（2026-10-20 排障时就是这么被卡住的）。
    """
    payload = parse_identity_payload(settings)
    return {
        key: hashlib.sha256(_canonical(value)).hexdigest()[:16]
        for key, value in payload.items()
    }


def identity_drift(stored: dict | None, expected: dict[str, str]) -> list[str]:
    """对比产物里记的分量与当前分量，返回**发生了变化的项名**。

    ``[]`` = 一致；``["unknown"]`` = 产物没记分量（旧产物），无从细分。
    """
    if not stored:
        return ["unknown"]
    return sorted(key for key, value in expected.items() if stored.get(key) != value)
