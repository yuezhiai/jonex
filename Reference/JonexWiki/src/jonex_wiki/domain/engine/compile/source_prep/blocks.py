"""Versioned parser-facing data contracts.

These models are intentionally independent of the old platform document and
database models.  A parse run produces a JSON-safe :class:`ParseResult`; the
compiler consumes its ordered :class:`ParseBlock` projections.
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from jonex_wiki.kernel.contracts import SourceArtifact


class InputKind(str, Enum):
    FILE = "file"
    URL = "url"


class InputRoute(str, Enum):
    TEXT_DIRECT = "text_direct"
    STRUCTURED_DIRECT = "structured_direct"
    SPREADSHEET_NATIVE = "spreadsheet_native"
    MINERU = "mineru"


@dataclass(frozen=True)
class InputRef:
    """Stable identity of a source before parsing."""

    value: str
    kind: InputKind
    name: str
    extension: str
    original_url: Optional[str] = None

    @classmethod
    def from_value(cls, value: str | Path) -> "InputRef":
        raw = str(value)
        if raw.startswith(("http://", "https://")):
            from urllib.parse import urlsplit

            parsed = urlsplit(raw)
            name = Path(parsed.path).name or "download"
            extension = Path(name).suffix.lower()
            return cls(raw, InputKind.URL, name, extension, original_url=raw)
        path = Path(raw).expanduser()
        return cls(str(path), InputKind.FILE, path.name, path.suffix.lower())


@dataclass
class ParseBlock:
    """One ordered, source-addressable projection from a parser result."""

    block_id: str
    position: int
    text: str
    block_type: str = "text"
    page_number: Optional[int] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ParseResult:
    """Canonical output of the parse stage.

    ``source_sha256`` always refers to the original input bytes.  For a URL,
    ``source_name`` and ``source_url`` retain the origin even when the parser
    works on a temporary downloaded file.
    """

    source_id: str
    document_id: str
    source_name: str
    source_sha256: str
    route: InputRoute
    parser_id: str
    parser_version: str
    blocks: List[ParseBlock]
    markdown: str = ""
    source_url: Optional[str] = None
    raw_output_dir: Optional[str] = None
    warnings: List[str] = field(default_factory=list)
    schema: str = "jonex_wiki.parse_result.v1"
    run_id: Optional[str] = None
    reading_materials: Optional[Dict[str, Any]] = None

    @property
    def block_count(self) -> int:
        return len(self.blocks)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema": self.schema,
            "source_id": self.source_id,
            "document_id": self.document_id,
            "source_name": self.source_name,
            "source_sha256": self.source_sha256,
            "source_url": self.source_url,
            "route": self.route.value,
            "parser_id": self.parser_id,
            "parser_version": self.parser_version,
            "block_count": self.block_count,
            "blocks": [block.to_dict() for block in self.blocks],
            "markdown": self.markdown,
            "raw_output_dir": self.raw_output_dir,
            "warnings": list(self.warnings),
            "run_id": self.run_id,
            "reading_materials": self.reading_materials,
        }

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "ParseResult":
        """Rehydrate a stored parse result without importing platform models."""

        blocks = [
            ParseBlock(
                block_id=str(item["block_id"]),
                position=int(item["position"]),
                text=str(item.get("text") or ""),
                block_type=str(item.get("block_type") or "text"),
                page_number=(
                    int(item["page_number"])
                    if item.get("page_number") is not None
                    else None
                ),
                metadata=dict(item.get("metadata") or {}),
            )
            for item in payload.get("blocks", [])
        ]
        return cls(
            source_id=str(payload["source_id"]),
            document_id=str(payload["document_id"]),
            source_name=str(payload.get("source_name") or "source"),
            source_sha256=str(payload["source_sha256"]),
            route=InputRoute(str(payload.get("route") or InputRoute.MINERU.value)),
            parser_id=str(payload.get("parser_id") or "unknown"),
            parser_version=str(payload.get("parser_version") or "unknown"),
            blocks=blocks,
            markdown=str(payload.get("markdown") or ""),
            source_url=payload.get("source_url"),
            raw_output_dir=payload.get("raw_output_dir"),
            warnings=list(payload.get("warnings") or []),
            schema=str(payload.get("schema") or "jonex_wiki.parse_result.v1"),
            run_id=payload.get("run_id"),
            reading_materials=payload.get("reading_materials"),
        )

    def to_source_artifacts(
        self,
        *,
        content_generation: int = 1,
        tenant_id: str = "local",
    ) -> List[SourceArtifact]:
        """Project blocks into the stable LLM-Wiki compiler contract.

        The parser-native fields remain under ``source_metadata``.  The
        compiler therefore receives plain text for retrieval while page,
        bounding-box, table, image, and raw parser information stays available
        for evidence review.
        """

        del tenant_id  # tenant scoping belongs to the deployment boundary.
        artifacts: List[SourceArtifact] = []
        for block in self.blocks:
            if not block.text:
                continue
            metadata = dict(block.metadata)
            metadata["original_blob"] = "sha256:" + self.source_sha256
            metadata.setdefault("block_type", block.block_type)
            metadata.setdefault("source_name", self.source_name)
            metadata.setdefault("route", self.route.value)
            metadata.setdefault("parser_id", self.parser_id)
            metadata.setdefault("parser_version", self.parser_version)
            metadata.setdefault("parser_state", "completed")
            if "input_copy" in block.metadata:
                metadata["original_local_path"] = block.metadata["input_copy"]
                metadata.pop("input_copy", None)
            metadata.setdefault(
                "source_location",
                {
                    "page_number": block.page_number,
                    "position": block.position,
                },
            )
            artifacts.append(
                SourceArtifact(
                    source_id=self.source_id,
                    document_id=self.document_id,
                    content_generation=content_generation,
                    parser_id=self.parser_id,
                    parser_version=self.parser_version,
                    chunk_id=block.block_id,
                    text=block.text,
                    page_number=block.page_number,
                    position=block.position,
                    source_metadata=metadata,
                    sha256=self.source_sha256,
                )
            )
        if self.reading_materials is not None:
            # Carry the source-level catalog exactly once, on the first emitted
            # artifact.  Bundle composition reads it from there and strips it
            # before per-chunk passage metadata is stored or prompted.
            for artifact in artifacts:
                artifact.source_metadata["source_reading_materials"] = self.reading_materials
                break
        return artifacts


def sha256_file(path: Path) -> str:
    """Hash a file in bounded chunks so large inputs do not fill memory."""

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def blocks_from_items(
    items: Iterable[Dict[str, Any]], *, source_name: str, parser_id: str,
    route: InputRoute, packing: Any = None, inspection=None, report=None,
) -> List[ParseBlock]:
    """Retain raw items as provenance; pack only recognized evidence fields.

    ``inspection``/``report``:编译前体检（见 ``source_prep.screening``）。结构化路由下
    表格被分流成 ``table_observation``、不进 ``pack_text``，所以体检在这里只作用于
    文本单元（超限摘除）；没有正文的原始 item 走既有的 ``opaque`` /
    ``unrepresented_items`` 计数通道，不再另造一套空表检测。
    """
    from jonex_wiki.domain.engine.compile.source_prep.chunking import pack_text
    normalized, locations, cursor = [], [], 0
    table_blocks: List[ParseBlock] = []
    opaque = []
    for position, original in enumerate(items):
        raw = dict(original) if isinstance(original, dict) else {"value": original}
        # MinerU-HTML 把表格正文嵌在 content.html（type=complex_table/table），
        # caption/footnote 也一并下移;先提到顶层,便于 _item_text 与元数据统一读取。
        content = raw.get("content")
        if isinstance(content, dict):
            for key, value in content.items():
                if key not in raw:
                    raw[key] = value
        text = _item_text(raw)
        # A source picture is a readable resource even without OCR/description.
        # This is a source anchor, never a fabricated description of its pixels.
        if str(raw.get("type") or "") in {"image", "chart"}:
            relative = raw.get("img_path") or raw.get("image_path") or raw.get("chart_path")
            if isinstance(relative, str) and relative:
                from jonex_wiki.domain.image_identity import image_anchor, plain
                marker = image_anchor(source_name, position, relative)
                raw["wiki_image_anchor"] = marker
                caption = plain(raw.get("image_caption") or raw.get("chart_caption") or raw.get("caption"), 200)
                alt = caption.replace('[', '').replace(']', '').replace('\n', ' ')
                text = (text + "\n\n" if text else "") + f"![{alt}]({marker})"
                if not _item_text(raw):
                    raw["evidence_kind"] = "source_image_unread"
        if not text:
            opaque.append({"position": position, "raw_item": raw})
            continue
        if str(raw.get("type") or raw.get("original_type") or "").casefold() in {"table", "complex_table"}:
            table_blocks.append(ParseBlock(
                block_id="{}-table-{}".format(Path(source_name).stem, position),
                position=position,
                text=text,
                block_type="table_observation",
                page_number=_page_number(raw),
                metadata={"source_name": source_name, "parser_id": parser_id,
                          "route": route.value, "source_position": position,
                          "page_number": _page_number(raw), "bbox": raw.get("bbox"),
                          "caption": raw.get("table_caption") or raw.get("caption"),
                          "footnote": raw.get("table_footnote") or raw.get("footnote"),
                          "native_locator": raw.get("native_locator"),
                          "evidence_kind": raw.get("evidence_kind", "parsed_table_observation"),
                          "quality": raw.get("quality"), "parent_object": raw.get("parent_object"),
                          "raw_item": raw, "compile_table_values": False},
            ))
            continue
        if str(raw.get("type") or "") in {"audio", "video", "image", "chart"}:
            # Media anchors must not be collapsed into a packed paragraph spanning
            # unrelated scenes. Keep the original payload and its evidence kind.
            kind = str(raw["type"])
            table_blocks.append(ParseBlock(
                block_id="{}-{}-{}".format(Path(source_name).stem, kind, position),
                position=position, text=text, block_type=kind, page_number=_page_number(raw),
                metadata={"source_name": source_name, "parser_id": parser_id,
                    "route": route.value, "source_position": position, "raw_item": raw,
                    "evidence_kind": raw.get("evidence_kind", "generated_description" if raw.get("description") else "observed_text"),
                    "time_start": raw.get("time_start"), "time_end": raw.get("time_end"),
                    "spans": raw.get("spans", []), "parent_object": raw.get("parent_object"),
                    "native_locator": raw.get("native_locator"), "bbox": raw.get("bbox"),
                    "source_locations": [{"position": position, "page_number": _page_number(raw),
                        "bbox": raw.get("bbox"), "time_start": raw.get("time_start"),
                        "time_end": raw.get("time_end"), "raw_item": raw}]}))
            continue
        piece = text + ("\n\n" if not text.endswith("\n\n") else "")
        normalized.append(piece)
        locations.append({"position": position, "page_number": _page_number(raw),
            "char_start": cursor, "char_end": cursor + len(piece), "bbox": raw.get("bbox"), "raw_item": raw})
        cursor += len(piece)
    if not normalized and not table_blocks and opaque:
        raise ValueError("MinerU produced no recognized evidence fields; raw content_list retained")
    blocks = pack_text("".join(normalized), prefix=Path(source_name).stem, options=packing,
                      inspection=inspection, report=report) if normalized else []
    for block in blocks:
        start, end = block.metadata["char_start"], block.metadata["char_end"]
        refs = [item for item in locations if item["char_end"] > start and item["char_start"] < end]
        block.metadata.update(source_name=source_name, parser_id=parser_id, route=route.value,
            source_locations=refs)
        pages = {item["page_number"] for item in refs}
        block.page_number = next(iter(pages)) if len(pages) == 1 else None
        if opaque and block is blocks[0]:
            block.metadata["unrepresented_items"] = opaque
        block.metadata["source_position"] = min((item["position"] for item in refs), default=block.position)
    combined = sorted([*blocks, *table_blocks], key=lambda block: (block.metadata.get("source_position", block.position), block.block_type != "table_observation"))
    for position, block in enumerate(combined):
        block.position = position
    return combined


#: 类型不在 ``_item_text`` 的别名表里时用的兜底正文字段（保持既有行为不变）。
_DEFAULT_EVIDENCE_FIELDS = (("text", "content", "text_content"),)


def _item_text(item: Dict[str, Any]) -> str:
    # MinerU-HTML 把表格正文嵌在 content.html（type=complex_table/table），
    # caption/footnote 也一并下移。先提上来统一按顶层字段处理。
    content = item.get("content")
    if isinstance(content, dict):
        for key, value in content.items():
            if key not in item:
                item[key] = value
    kind = str(item.get("type") or item.get("original_type") or "text")
    if kind == "complex_table":
        kind = "table"
    aliases = {
        "table": (("table_caption", "caption"), ("table_body", "html", "text", "content"), ("table_footnote", "footnote")),
        "image": (("image_caption", "img_caption", "caption"), ("text", "text_content", "ocr_text"), ("description",), ("image_footnote", "img_footnote")),
        "chart": (("chart_caption", "caption"), ("content", "text", "text_content", "ocr_text"), ("description",), ("chart_footnote", "footnote")),
        "list": (("list_items", "text", "content"),),
        "equation": (("equation", "text", "content"), ("description",)),
        "interline_equation": (("equation", "text", "content"), ("description",)),
        # [jonex] 视频/音频：转写正文由 atomic-rag 的 `_textualize_multimodal`
        # 写在 **`description`**（item["description"] = 逐场景转写正文；
        # 见 Reference/Rag-anything/.../service/task_manager.py）。此前别名表
        # 里没有这两类，视频 item 落进下面的 `_DEFAULT_EVIDENCE_FIELDS`
        # （text/content/text_content）取到空串 → 进 `opaque` → 整份文档被判
        # 「MinerU produced no recognized evidence fields」。2026-10-09 线上
        # 线上因此连转写**成功**的视频也编译不了。
        # `description` 排第一：转写正文就在那儿，`text` 只是别的生产者的兜底。
        "video": (("description", "transcript", "text", "content", "text_content"),),
        "audio": (("description", "transcript", "text", "content", "text_content"),),
    }
    groups = aliases.get(kind, _DEFAULT_EVIDENCE_FIELDS)
    result = []
    for group in groups:
        for key in group:
            value = item.get(key)
            values = value if isinstance(value, list) else [value]
            parts = [part for part in values if isinstance(part, str) and part.strip()]
            if parts:
                for part in parts:
                    if kind == "table" and "<table" in part.lower():
                        from .chunking import PackingError
                        from .tables import html_table_to_markdown
                        try:
                            part = html_table_to_markdown(part)
                        except PackingError:
                            # 无法可靠展开为网格(重叠/嵌套/非法 span)时保留原始
                            # HTML,让 table_observation 原样落库,再由
                            # persist_table_observations 标记 ambiguous。
                            pass
                    if part not in result:
                        result.append(part)
                break
    text = "\n".join(result)
    if item.get("text_level") and text and not text.startswith("#"):
        text = "#" * min(6, max(1, int(item["text_level"]))) + " " + text
    return text


def _page_number(item: Dict[str, Any]) -> Optional[int]:
    for key in ("page_number", "page_no"):
        value = item.get(key)
        if value is not None:
            try:
                return int(value)
            except (TypeError, ValueError):
                pass
    value = item.get("page_idx")
    if value is not None:
        try:
            return int(value) + 1
        except (TypeError, ValueError):
            pass
    return None
