"""Pipeline stage implementations."""

import asyncio
import hashlib
import inspect
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from lightrag.kg.shared_storage import get_namespace_data, get_pipeline_status_lock
from lightrag.operate import extract_entities, merge_nodes_and_edges
from lightrag.utils import compute_mdhash_id, sanitize_text_for_encoding

from raganything.chunk_utils import apply_chunk_template, sample_frames
from raganything.event_bus import PipelineEvent
from raganything.parsers.base import Parser
from raganything.parsers.mineru import MineruParser
from raganything.pipeline.base import (
    PipelineContext,
    PipelineServices,
    Stage,
    StageResult,
    merge_context,
)
from raganything.pipeline_mode import PipelineMode
from raganything.router import ParserRegistry
from raganything.service.http_lightrag_client import LightRAGError, TrackStatus
from raganything.utils import (
    DEFAULT_COLUMN_THRESHOLD,
    _render_caption_line,
    _table_grid_v2_enabled,
    _valid_caption,
    extract_embedded_tables,
    extract_text_metadata,
    fmt_row,
    get_processor_for_type,
    get_table_body,
    insert_text_content,
    insert_text_content_with_multimodal_content,
    make_header_block,
    normalize_table_grid,
    normalize_table_rows,
    pack_rows,
    pack_text_blocks,
    separate_content,
    split_row_by_cells,
)


logger = logging.getLogger(__name__)


# ── Stage 1: File Validation ──────────────────────────────────────────

class FileValidateStage(Stage):
    """Validate that the input file exists and has a recognized extension."""

    def __init__(self, supported_extensions: Optional[List[str]] = None):
        self._supported_extensions = supported_extensions

    async def execute(
        self, ctx: PipelineContext, services: PipelineServices
    ) -> StageResult:
        path = Path(ctx.file_path)
        if not path.exists():
            return StageResult(error=f"File not found: {ctx.file_path}")

        ext = path.suffix.lower()
        supported = self._supported_extensions
        if supported is not None and ext not in supported and ext not in Parser.TEXT_FORMATS:
            if services.logger:
                services.logger.warning(f"Unrecognized extension: {ext}")

        if services.logger:
            services.logger.info(f"Starting document parsing: {ctx.file_path}")
        return StageResult()


# ── Stage 2: Parse Document ───────────────────────────────────────────

# [jonex] #4: transient parse error markers (aligned with v1 _is_transient_parse_error)
_TRANSIENT_PARSE_ERROR_MARKERS = (
    "unexpected_eof_while_reading",
    "eof occurred in violation of protocol",
    "ssl",
    "failed to download mineru result",
    "connection reset",
    "connection aborted",
    "timed out",
    "temporary failure in name resolution",
    "max retries exceeded",
)


def _is_transient_parse_error(exc: BaseException) -> bool:
    """[jonex] Check if a parse exception is transient (network/SSL) and retryable."""
    import ssl
    import urllib.error

    cur: BaseException | None = exc
    seen = 0
    while cur is not None and seen < 10:
        if isinstance(cur, (ssl.SSLError, urllib.error.URLError, TimeoutError, ConnectionError)):
            return True
        msg = str(cur).lower()
        if any(marker in msg for marker in _TRANSIENT_PARSE_ERROR_MARKERS):
            return True
        cur = cur.__cause__ or cur.__context__
        seen += 1
    return False


class ParseStage(Stage):
    """Parse document using the configured parser and ParserRegistry."""

    def __init__(self, registry: Optional[ParserRegistry] = None):
        self._registry = registry or _default_parser_registry()
        # [jonex] #4: transient error retry config
        self._retry_max = int(os.getenv("RAG_PARSE_RETRY_MAX", "3"))
        self._retry_base = float(os.getenv("RAG_PARSE_RETRY_BASE_SEC", "2.0"))
        # [jonex] §table-grid-v2 L3: 启动打印生效值（§5 防「以为回退了其实没回退」）
        logger.info(
            "[jonex] §table-grid-v2 L3: XLSX_NATIVE_NORMALIZE=%s "
            "（true=openpyxl 表格主轨+MinerU 图片轨 / false=xlsx 全量交 MinerU）",
            _xlsx_native_normalize_enabled(),
        )

    async def execute(
        self, ctx: PipelineContext, services: PipelineServices
    ) -> StageResult:
        # ── Cancel check: before starting parse ──────────────
        if ctx.cancel_event and ctx.cancel_event.is_set():
            return StageResult(error="Task cancelled before parse")

        path = Path(ctx.file_path)
        ext = path.suffix.lower()
        config = services.config
        parse_method = getattr(config, "parse_method", "auto")
        output_dir = getattr(config, "parser_output_dir", "./output")
        parse_cache = services.parse_cache
        force_reparse = ctx.force_reparse
        parser_name = getattr(ctx, "parser_type", "") or None  # per-task parser（preset 链路）

        # ── Callback: parse start ──
        cb = services.callback_manager
        if cb:
            cb.dispatch("on_parse_start", file_path=ctx.file_name,
                        parser=getattr(config, "parser", ""))
        parse_start_time = time.time()

        cache_key = None
        if parse_cache is not None and not force_reparse:
            cache_key = parse_cache.generate_cache_key(
                path, parse_method, config, parser_name=parser_name,
            )
            cached = await parse_cache.get_cached_result(
                cache_key, path, parse_method, parser_name=parser_name,
            )
            if cached is not None:
                # ── Cancel check: after cache hit ──────────
                if ctx.cancel_event and ctx.cancel_event.is_set():
                    return StageResult(error="Task cancelled after cache hit")

                content_list, doc_id = cached
                # [jonex] §table-grid-v2 L3: 缓存命中同样执行 xlsx 双路合并
                # （merge 幂等：已含 native 指纹时原样放行）。合并后重算
                # doc_id 保持内容标识一致。
                if _xlsx_native_normalize_enabled() and ext in (".xlsx", ".xls"):
                    content_list, _dropped = _merge_xlsx_native_tables(
                        content_list, path,
                    )
                    doc_id = _generate_doc_id(content_list)
                if services.logger:
                    services.logger.info(f"Using cached parsing result for: {ctx.file_path}")
                if services.event_bus:
                    await services.event_bus.publish(PipelineEvent(
                        type="document_parsed", doc_id=doc_id, file_path=ctx.file_name,
                    ))
                if cb:
                    cb.dispatch("on_parse_complete", file_path=ctx.file_name,
                                content_blocks=len(content_list), doc_id=doc_id,
                                duration_seconds=time.time() - parse_start_time)
                return StageResult(content_list=content_list, doc_id=doc_id)
        elif force_reparse and services.logger:
            services.logger.info(f"Force re-parse enabled, skipping cache for: {ctx.file_path}")

        route = self._registry.lookup(ext)
        if route is None:
            return StageResult(error=f"No parser handler for extension: {ext}")

        # ── Parse with transient retry (#4) ────────────────────────
        # Call the primary handler or its fallback, each with retry loop
        content_list = await self._parse_with_retry(
            route.handler, route.fallback, services, ctx, path, ext,
            output_dir, parse_method,
        )
        if content_list is None:
            # error already surfaced inside _parse_with_retry
            if route.fallback is None:
                return StageResult(error=f"No handler for extension: {ext}")
            return StageResult(error=f"Parsing failed for extension: {ext}")

        # ── Cancel check: after parse completes (long-running parsers like MinerU) ──
        if ctx.cancel_event and ctx.cancel_event.is_set():
            return StageResult(error="Task cancelled after parse completed")

        if not content_list:
            return StageResult(error="Parsing failed: No content was extracted")

        # [jonex] §table-grid-v2 L3: xlsx 双路合并（openpyxl 表格主轨 +
        # MinerU 图片/公式轨；MinerU 表格项丢弃）。合并后再生成 doc_id。
        dropped_tables = 0
        if _xlsx_native_normalize_enabled() and ext in (".xlsx", ".xls"):
            content_list, dropped_tables = _merge_xlsx_native_tables(
                content_list, path,
            )
        if dropped_tables:
            # O4 口径：MinerU 表格项丢弃数量透出到 table_stats
            ctx.table_stats["mineru_table_dropped"] = (
                ctx.table_stats.get("mineru_table_dropped", 0) + dropped_tables
            )

        doc_id = _generate_doc_id(content_list)

        if parse_cache is not None:
            await parse_cache.store_cached_result(
                cache_key, content_list, doc_id, path, parse_method, parser_name=parser_name,
            )

        if services.logger:
            services.logger.info(
                f"Parsing complete: {len(content_list)} blocks, doc_id={doc_id}"
            )

        if services.event_bus:
            await services.event_bus.publish(PipelineEvent(
                type="document_parsed", doc_id=doc_id, file_path=ctx.file_name,
            ))
        if cb:
            cb.dispatch("on_parse_complete", file_path=ctx.file_name,
                        content_blocks=len(content_list), doc_id=doc_id,
                        duration_seconds=time.time() - parse_start_time)
        return StageResult(content_list=content_list, doc_id=doc_id)

    async def _parse_with_retry(
        self, handler, fallback, services, ctx, path, ext,
        output_dir, parse_method,
    ):
        """[jonex] #4: Call parser with transient-error retry loop.

        Only retries on transient network/SSL errors (aligned with v1).
        Hard failures (format unsupported, corrupt content) propagate immediately.
        NotImplementedError triggers fallback handler (also with retry).
        """
        logger = services.logger
        doc_parser = services.doc_parser
        handlers_to_try = [(handler, "primary")]
        if fallback:
            handlers_to_try.append((fallback, "fallback"))

        last_error: Exception | None = None

        for h, h_label in handlers_to_try:
            for attempt in range(1, self._retry_max + 1):
                # Cancel check before each attempt
                if ctx.cancel_event and ctx.cancel_event.is_set():
                    if logger:
                        logger.warning(f"Parse cancelled before attempt {attempt}")
                    return None

                try:
                    result = await asyncio.to_thread(
                        _call_handler, h, doc_parser, path,
                        output_dir, parse_method,
                    )
                    return result
                except NotImplementedError:
                    if h_label == "primary" and fallback:
                        if logger:
                            logger.warning(
                                f"Handler not implemented for ext={ext}, using fallback"
                            )
                        break  # move to fallback handler
                    return None  # no fallback available → caller returns error
                except Exception as exc:
                    last_error = exc
                    is_transient = _is_transient_parse_error(exc)
                    if not is_transient or attempt >= self._retry_max:
                        if logger:
                            logger.error(
                                f"Parse failed ({h_label}): {type(exc).__name__}: {exc}"
                                + (f" (attempt {attempt}/{self._retry_max})" if is_transient else " (hard failure, not retrying)")
                            )
                        if h_label == "fallback" or not fallback:
                            raise  # last handler → propagate
                        break  # try fallback
                    # Transient → retry with exponential backoff
                    backoff = self._retry_base * (2 ** (attempt - 1))
                    if logger:
                        logger.warning(
                            f"Parse transient error ({h_label} attempt {attempt}/{self._retry_max}): "
                            f"{type(exc).__name__}: {exc}. Retrying in {backoff:.1f}s"
                        )
                    await asyncio.sleep(backoff)

        raise last_error  # type: ignore[misc]


# ── Stage 3: Insert Text Content ──────────────────────────────────────

class TextInsertStage(Stage):
    """Separate and insert pure text content into LightRAG."""

    async def execute(
        self, ctx: PipelineContext, services: PipelineServices
    ) -> StageResult:
        if not ctx.content_list:
            return StageResult()

        text_content, multimodal_items = separate_content(ctx.content_list)

        # Extract per-block metadata (page_idx, text_idx) hidden by separate_content
        text_meta = extract_text_metadata(multimodal_items)
        # Remove the sentinel from multimodal_items so it doesn't leak downstream
        multimodal_items = [m for m in multimodal_items if "_text_meta" not in m]

        if not text_content.strip():
            return StageResult(multimodal_items=multimodal_items)

        file_ref = ctx.get_file_reference(
            getattr(services.config, "use_full_path", False)
        )

        # ── Callback: text insert start ──
        cb = services.callback_manager
        if cb:
            cb.dispatch("on_text_insert_start", file_path=ctx.file_name,
                        text_length=len(text_content), doc_id=ctx.doc_id)
        text_insert_start_time = time.time()

        if text_meta:
            # Metadata-aware path: pass list[dict] to preserve page_idx / text_idx
            await insert_text_content(
                services.lightrag,
                input=text_meta,
                file_paths=file_ref,
                ids=ctx.doc_id,
            )
            if services.logger:
                services.logger.info(
                    f"Text content inserted: {len(text_meta)} blocks "
                    f"with position metadata"
                )
        else:
            # Fallback: plain string path (e.g. external callers with no parser metadata)
            await insert_text_content(
                services.lightrag,
                input=text_content,
                file_paths=file_ref,
                ids=ctx.doc_id,
            )
            if services.logger:
                services.logger.info(
                    f"Text content inserted: {len(text_content)} characters"
                )

        if services.event_bus:
            await services.event_bus.publish(PipelineEvent(
                type="text_inserted", doc_id=ctx.doc_id, file_path=ctx.file_name,
            ))
        if cb:
            cb.dispatch("on_text_insert_complete", file_path=ctx.file_name,
                        duration_seconds=time.time() - text_insert_start_time,
                        doc_id=ctx.doc_id)
        return StageResult(multimodal_items=multimodal_items)


# ── Stage 4: Process Multimodal Content ───────────────────────────────

class MultimodalStage(Stage):
    """Process multimodal content (images, tables, equations, audio, video)."""

    def __init__(self, mode: PipelineMode = PipelineMode.STANDALONE):
        self._mode = mode

    async def execute(
        self, ctx: PipelineContext, services: PipelineServices
    ) -> StageResult:
        # If multimodal_items not set by a previous stage (e.g. HTTP pipeline
        # which skips TextInsertStage), derive them from content_list.
        if not ctx.multimodal_items and ctx.content_list:
            _, multimodal_items = separate_content(ctx.content_list)
            multimodal_items = [m for m in multimodal_items if "_text_meta" not in m]
            ctx = merge_context(ctx, StageResult(multimodal_items=multimodal_items))

        if not ctx.multimodal_items:
            if services.event_bus:
                await services.event_bus.publish(PipelineEvent(
                    type="multimodal_complete", doc_id=ctx.doc_id or "",
                    file_path=ctx.file_name,
                ))
            return StageResult()

        if self._mode == PipelineMode.LIGHTRAG_INTEGRATED:
            text_items, _ = separate_content(ctx.content_list or [])
            if text_items:
                await insert_text_content_with_multimodal_content(
                    services.lightrag, text_items,
                    multimodal_content=ctx.multimodal_items,
                    file_paths=ctx.file_name, ids=ctx.doc_id,
                )
                if services.event_bus:
                    await services.event_bus.publish(PipelineEvent(
                        type="multimodal_complete", doc_id=ctx.doc_id or "",
                        file_path=ctx.file_name,
                    ))
                return StageResult()
            # No text items but there ARE multimodal items (e.g. pure video/audio):
            # fall through to STANDALONE path so VideoModalProcessor can run.

        # STANDALONE mode
        processors = getattr(services, "modal_processors", {})
        # [jonex] 多模态描述并发（scene=raganything_ingest）。HTTP 模式下 services.lightrag=None，
        # 原兜底恒为 2，把并发锁死。优先读 config.max_parallel_multimodal（env MAX_PARALLEL_MULTIMODAL），
        # 回退嵌入模式的 lightrag.max_parallel_insert，最后回退 2。
        _mm_concurrency = (
            getattr(services.config, "max_parallel_multimodal", 0)
            or getattr(services.lightrag, "max_parallel_insert", 0)
            or 2
        )
        semaphore = asyncio.Semaphore(max(1, int(_mm_concurrency)))
        cb = services.callback_manager
        multimodal_items = ctx.multimodal_items
        total_items = len(multimodal_items)

        # ── Callback: multimodal start ──
        if cb:
            cb.dispatch("on_multimodal_start", file_path=ctx.file_name,
                        item_count=total_items)
        multimodal_start_time = time.time()

        async def _process(item: Dict, idx: int) -> Optional[Dict]:
            async with semaphore:
                content_type = item.get("type", "unknown")
                # [jonex] 方案 C：将 COS URL 注入 video modal_content，供 MPS backend 使用
                if content_type == "video":
                    mps_url = getattr(ctx, "mps_video_url", "") or ""
                    if mps_url:
                        item = {**item, "mps_video_url": mps_url}
                processor = get_processor_for_type(processors, content_type)
                if not processor:
                    if services.logger:
                        services.logger.warning(
                            f"No processor for type: {content_type}"
                        )
                    return None
                desc, entity_info = await processor.generate_description_only(
                    modal_content=item, content_type=content_type,
                    prompt_overrides=getattr(ctx, "prompt_overrides", None),
                )
                # ── Callback: multimodal item complete ──
                if cb:
                    cb.dispatch("on_multimodal_item_complete",
                                file_path=ctx.file_name,
                                item_index=idx + 1, total_items=total_items,
                                item_type=content_type)
                return {
                    "index": idx, "chunk_order_index": idx,
                    "type": content_type, "content_type": content_type,
                    "description": desc, "entity_info": entity_info,
                    "original": item, "original_item": item,
                    "item_info": item.get("item_info", {}),
                }

        tasks = [_process(item, i) for i, item in enumerate(ctx.multimodal_items)]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        valid = [r for r in results if isinstance(r, dict)]
        # Log any exceptions that were swallowed by gather
        for i, r in enumerate(results):
            if isinstance(r, BaseException):
                if services.logger:
                    services.logger.error(
                        f"Multimodal item {i} failed: {type(r).__name__}: {r}"
                    )
        if services.logger:
            services.logger.info(
                f"Multimodal processing: {len(valid)}/{len(ctx.multimodal_items)} items"
            )

        # ── Callback: multimodal complete ──
        if cb:
            cb.dispatch("on_multimodal_complete", file_path=ctx.file_name,
                        processed_count=len(valid),
                        duration_seconds=time.time() - multimodal_start_time)

        if services.event_bus:
            await services.event_bus.publish(PipelineEvent(
                type="multimodal_complete", doc_id=ctx.doc_id or "",
                file_path=ctx.file_name,
            ))
        return StageResult(multimodal_results=valid)


# ── Stage 4a: Multimodal Chunk Conversion & Storage ────────────────────

class MultimodalChunkStage(Stage):
    """Convert multimodal descriptions to LightRAG chunks, store them and main entities."""

    async def execute(
        self, ctx: PipelineContext, services: PipelineServices
    ) -> StageResult:
        multimodal_results = ctx.multimodal_results
        if not multimodal_results:
            return StageResult()

        lightrag = services.lightrag
        config = services.config

        # Rebuild lightrag_chunks from multimodal_results
        lightrag_chunks = _build_lightrag_chunks(
            multimodal_results, ctx, lightrag, config,
        )
        if not lightrag_chunks:
            return StageResult()

        # Store chunks
        await lightrag.text_chunks.upsert(lightrag_chunks)
        await lightrag.chunks_vdb.upsert(lightrag_chunks)

        # Store multimodal main entities
        await _store_main_entities(
            multimodal_results, lightrag_chunks, ctx, lightrag, config,
        )

        if services.logger:
            services.logger.info(
                f"Stored {len(lightrag_chunks)} multimodal chunks and main entities"
            )

        return StageResult()


# ── Stage 4b: Entity Extraction & Belongs-To Relations ─────────────────

class EntityExtractStage(Stage):
    """Extract entities from multimodal chunks via LLM, add belongs_to relations."""

    async def execute(
        self, ctx: PipelineContext, services: PipelineServices
    ) -> StageResult:
        multimodal_results = ctx.multimodal_results
        if not multimodal_results:
            return StageResult()

        lightrag = services.lightrag
        config = services.config

        # Rebuild chunks for entity extraction
        lightrag_chunks = _build_lightrag_chunks(
            multimodal_results, ctx, lightrag, config,
        )

        pipeline_status = await get_namespace_data("pipeline_status")
        pipeline_lock = get_pipeline_status_lock()

        chunk_results = await extract_entities(
            chunks=lightrag_chunks,
            global_config=lightrag.__dict__,
            pipeline_status=pipeline_status,
            pipeline_status_lock=pipeline_lock,
            llm_response_cache=lightrag.llm_response_cache,
            text_chunks_storage=lightrag.text_chunks,
        )

        enhanced = _add_belongs_to_relations(chunk_results, multimodal_results)

        if services.logger:
            services.logger.info(
                f"Extracted entities from {len(lightrag_chunks)} multimodal chunks"
            )

        return StageResult(chunk_results=enhanced)


# ── Stage 5: Entity Merge ─────────────────────────────────────────────

class EntityMergeStage(Stage):
    """Merge extracted entities into LightRAG knowledge graph, update doc_status."""

    async def execute(
        self, ctx: PipelineContext, services: PipelineServices
    ) -> StageResult:
        chunk_results = ctx.chunk_results
        if not chunk_results:
            return StageResult()

        pipeline_status = await get_namespace_data("pipeline_status")
        pipeline_lock = get_pipeline_status_lock()
        file_ref = ctx.get_file_reference(
            getattr(services.config, "use_full_path", False)
        )

        await merge_nodes_and_edges(
            chunk_results=chunk_results,
            knowledge_graph_inst=services.lightrag.chunk_entity_relation_graph,
            entity_vdb=services.lightrag.entities_vdb,
            relationships_vdb=services.lightrag.relationships_vdb,
            global_config=services.lightrag.__dict__,
            full_entities_storage=services.lightrag.full_entities,
            full_relations_storage=services.lightrag.full_relations,
            doc_id=ctx.doc_id,
            pipeline_status=pipeline_status,
            pipeline_status_lock=pipeline_lock,
            llm_response_cache=services.lightrag.llm_response_cache,
            entity_chunks_storage=services.lightrag.entity_chunks,
            relation_chunks_storage=services.lightrag.relation_chunks,
            current_file_number=1,
            total_files=1,
            file_path=file_ref,
        )
        await services.lightrag._insert_done()

        # Collect chunk_ids for doc_status update
        chunk_ids = _collect_chunk_ids(ctx.multimodal_results or [], ctx, services.lightrag, services.config)
        if services.event_bus:
            await services.event_bus.publish(PipelineEvent(
                type="multimodal_complete",
                doc_id=ctx.doc_id or "",
                file_path=ctx.file_name,
                data={"chunk_ids": chunk_ids},
            ))

        return StageResult()


# ── Helpers ───────────────────────────────────────────────────────────

def _default_parser_registry() -> ParserRegistry:
    r = ParserRegistry()
    r.register([".pdf"], Parser.parse_pdf)
    r.register(list(Parser.IMAGE_FORMATS), Parser.parse_image,
               fallback=MineruParser().parse_image)
    r.register(list(Parser.OFFICE_FORMATS), Parser.parse_document)
    r.register(list(Parser.AUDIO_FORMATS), Parser.parse_audio)
    r.register(list(Parser.VIDEO_FORMATS), Parser.parse_video)
    # .txt/.md/.markdown 走纯文本快路径（绕开 MinerU），须在 catch-all "*" 之前注册
    r.register(list(Parser.TEXT_FORMATS), Parser.parse_text)
    r.register(["*"], Parser.parse_document)
    return r


def _call_handler(handler: Callable, parser_instance: Any, path: Path,
                  output_dir: str, parse_method: str) -> List[Dict[str, Any]]:
    """Call a parser handler, resolving the actual method on the parser instance.

    Parser class methods (e.g. Parser.parse_pdf) are registered as Callable
    references.  At invocation time, the corresponding method is looked up
    on the actual parser instance so subclass overrides take effect.
    Bound methods (e.g. MineruParser().parse_image) are called directly.
    """
    if hasattr(handler, "__self__"):
        # Already bound — call directly with path as first arg
        return handler(str(path), output_dir=output_dir, method=parse_method)

    # Unbound method — resolve on the parser instance
    method_name = getattr(handler, "__name__", None)
    if method_name and hasattr(parser_instance, method_name):
        bound = getattr(parser_instance, method_name)
    else:
        bound = handler

    kwargs: Dict[str, Any] = {"output_dir": output_dir}
    try:
        sig = inspect.signature(bound)
        params = list(sig.parameters.keys())
    except (ValueError, TypeError):
        params = []

    if "method" in params:
        kwargs["method"] = parse_method

    return bound(str(path), **kwargs)


# ── Multimodal pipeline helpers ────────────────────────────────────────


def _build_lightrag_chunks(
    multimodal_data_list: List[Dict[str, Any]],
    ctx: PipelineContext,
    lightrag: Any,
    config: Any,
) -> Dict[str, Any]:
    """Convert multimodal description results to LightRAG chunk format.

    Adapted from ProcessorMixin._convert_to_lightrag_chunks_type_aware.
    """
    chunks: Dict[str, Any] = {}
    file_ref = ctx.get_file_reference(getattr(config, "use_full_path", False))

    for data in multimodal_data_list:
        description = data["description"]
        entity_info = data["entity_info"]
        chunk_order_index = data["chunk_order_index"]
        content_type = data["content_type"]
        original_item = data["original_item"]

        if content_type == "audio":
            segments = original_item.get("_audio_segments", [])
            asr_result = original_item.get("_asr_result", {})
            total = len(segments)
            audio_source_id = asr_result.get("audio_sha256", "")[:12]
            for seg in segments:
                chunk_item = dict(original_item)
                chunk_item["asr_transcript"] = seg["text"]
                chunk_item["asr_start_time"] = seg["start_time"]
                chunk_item["asr_end_time"] = seg["end_time"]
                chunk_item["asr_segment_index"] = seg["segment_index"]
                chunk_item["asr_total_segments"] = total
                chunk_item["asr_duration"] = asr_result.get("duration", 0)
                chunk_item["asr_language"] = asr_result.get("language", "unknown")
                chunk_item["asr_relative_position"] = seg.get("relative_position", 0.0)
                formatted = apply_chunk_template("audio", chunk_item, "")
                chunk_id = compute_mdhash_id(formatted, prefix="chunk-")
                tokens = len(lightrag.tokenizer.encode(formatted))
                chunks[chunk_id] = {
                    "content": formatted, "tokens": tokens,
                    "full_doc_id": ctx.doc_id,
                    "chunk_order_index": chunk_order_index,
                    "file_path": file_ref, "llm_cache_list": [],
                    "is_multimodal": True,
                    "modal_entity_name": entity_info.get("entity_name", ""),
                    "original_type": content_type,
                    "page_idx": data["item_info"].get("page_idx", 0),
                    "start_time": seg["start_time"],
                    "end_time": seg["end_time"],
                    "source_segment_indices": seg.get("source_segment_indices", []),
                    "relative_position": seg.get("relative_position", 0.0),
                    "audio_source_id": audio_source_id,
                    "group_summary": seg.get("group_summary", ""),
                    "speaker_labels": seg.get("speaker_labels", []),
                }
            continue

        if content_type == "video":
            segments = original_item.get("_audio_segments", [])
            asr_result = original_item.get("_asr_result", {})
            total = len(segments)
            video_source_id = original_item.get("_video_source_id", "")
            for seg_idx, seg in enumerate(segments):
                if not seg.get("text", "").strip() and not seg.get("frames"):
                    continue
                chunk_item = dict(original_item)
                chunk_item["asr_transcript"] = seg.get("text", "")
                chunk_item["asr_start_time"] = seg.get("start_time", 0.0)
                chunk_item["asr_end_time"] = seg.get("end_time", 0.0)
                chunk_item["asr_segment_index"] = seg.get("segment_index", seg_idx)
                chunk_item["asr_total_segments"] = total
                chunk_item["asr_duration"] = asr_result.get("duration", 0)
                chunk_item["asr_language"] = asr_result.get("language", "unknown")
                chunk_item["asr_relative_position"] = seg.get("relative_position", 0.0)
                seen_times = set()
                owned_frames = []
                for f in seg.get("frames", []):
                    t = round(f.get("frame_time", 0), 3)
                    if t not in seen_times and f.get("owner_segment", 0) == seg_idx:
                        seen_times.add(t)
                        owned_frames.append(f)
                owned_frames = sample_frames(owned_frames, max_frames=5)
                rendered_frames = [f for f in owned_frames if f.get("description")]
                frame_descriptions = "\n".join(
                    f"- [{f['frame_time']:.1f}s] {f.get('condensed', f.get('description', ''))}"
                    for f in rendered_frames
                )
                chunk_item["frame_descriptions"] = frame_descriptions
                chunk_item["frame_count"] = len(rendered_frames)
                formatted = apply_chunk_template("video", chunk_item, "")
                chunk_id = compute_mdhash_id(
                    f"{video_source_id}:{int(seg.get('start_time', 0) * 1000)}-{int(seg.get('end_time', 0) * 1000)}",
                    prefix="chunk-",
                )
                MAX_CHUNK_TOKENS = getattr(config, "video_chunk_token_size", 600)
                tokens = len(lightrag.tokenizer.encode(formatted))
                while tokens > MAX_CHUNK_TOKENS and rendered_frames:
                    rendered_frames = rendered_frames[:-1]
                    frame_descriptions = "\n".join(
                        f"- [{f['frame_time']:.1f}s] {f.get('condensed', f.get('description', ''))}"
                        for f in rendered_frames
                    )
                    chunk_item["frame_descriptions"] = frame_descriptions
                    chunk_item["frame_count"] = len(rendered_frames)
                    formatted = apply_chunk_template("video", chunk_item, "")
                    tokens = len(lightrag.tokenizer.encode(formatted))
                chunks[chunk_id] = {
                    "content": formatted, "tokens": tokens,
                    "full_doc_id": ctx.doc_id,
                    "chunk_order_index": chunk_order_index,
                    "file_path": file_ref, "llm_cache_list": [],
                    "is_multimodal": True,
                    "modal_entity_name": entity_info.get("entity_name", ""),
                    "original_type": content_type,
                    "page_idx": data.get("item_info", {}).get("page_idx", 0),
                    "start_time": seg.get("start_time", 0.0),
                    "end_time": seg.get("end_time", 0.0),
                    "source_segment_indices": seg.get("source_segment_indices", []),
                    "relative_position": seg.get("relative_position", 0.0),
                    "speaker_labels": seg.get("speaker_labels", []),
                    "group_summary": seg.get("group_summary", ""),
                    "video_source_id": video_source_id,
                    "frame_ids": [f.get("frame_id", "") for f in owned_frames],
                    "frame_paths": [f.get("frame_path", "") for f in owned_frames],
                    "frame_times": [f.get("frame_time", 0.0) for f in owned_frames],
                    "frame_descriptions": [f.get("description", "") for f in owned_frames],
                    "frame_condensed": [f.get("condensed", "") for f in owned_frames],
                    "frame_ocr_texts": [f.get("ocr_text", "") for f in owned_frames],
                    "frame_extractive_terms": [f.get("extractive_terms", []) for f in owned_frames],
                }
            continue

        # Standard content types: image, table, equation, generic
        formatted = apply_chunk_template(content_type, original_item, description)
        chunk_id = compute_mdhash_id(formatted, prefix="chunk-")
        tokens = len(lightrag.tokenizer.encode(formatted))
        chunks[chunk_id] = {
            "content": formatted, "tokens": tokens,
            "full_doc_id": ctx.doc_id,
            "chunk_order_index": chunk_order_index,
            "file_path": file_ref, "llm_cache_list": [],
            "is_multimodal": True,
            "modal_entity_name": entity_info["entity_name"],
            "original_type": content_type,
            "page_idx": data.get("item_info", {}).get("page_idx", 0),
        }

    return chunks


async def _store_main_entities(
    multimodal_data_list: List[Dict[str, Any]],
    lightrag_chunks: Dict[str, Any],
    ctx: PipelineContext,
    lightrag: Any,
    config: Any,
) -> None:
    """Store multimodal main entities to KG, entities_vdb, and full_entities.

    Adapted from ProcessorMixin._store_multimodal_main_entities +
    _store_multimodal_entities_to_full_entities.
    """
    entities_to_store: Dict[str, Any] = {}
    file_ref = ctx.get_file_reference(getattr(config, "use_full_path", False))

    for data in multimodal_data_list:
        entity_info = data["entity_info"]
        entity_name = entity_info["entity_name"]
        description = data["description"]
        content_type = data["content_type"]
        original_item = data["original_item"]

        formatted = apply_chunk_template(content_type, original_item, description)
        chunk_id = compute_mdhash_id(formatted, prefix="chunk-")
        entity_id = compute_mdhash_id(entity_name, prefix="ent-")

        entity_data = {
            "entity_name": entity_name,
            "entity_type": entity_info.get("entity_type", content_type),
            "content": entity_info.get("summary", description),
            "source_id": chunk_id,
            "file_path": file_ref,
        }
        entities_to_store[entity_id] = entity_data

    if not entities_to_store:
        return

    for entity_data in entities_to_store.values():
        node_data = {
            "entity_id": entity_data["entity_name"],
            "entity_type": entity_data["entity_type"],
            "description": entity_data["content"],
            "source_id": entity_data["source_id"],
            "file_path": entity_data["file_path"],
            "created_at": int(time.time()),
        }
        await lightrag.chunk_entity_relation_graph.upsert_node(
            entity_data["entity_name"], node_data,
        )

    await lightrag.entities_vdb.upsert(entities_to_store)
    await lightrag.entities_vdb.index_done_callback()

    # Store in full_entities
    if ctx.doc_id and lightrag.full_entities:
        current_doc_entities = await lightrag.full_entities.get_by_id(ctx.doc_id)
        if current_doc_entities is None:
            entity_names = [e["entity_name"] for e in entities_to_store.values()]
            doc_entities_data = {
                "entity_names": entity_names,
                "count": len(entity_names),
                "update_time": int(time.time()),
            }
        else:
            existing_names = list(current_doc_entities.get("entity_names", []))
            seen = set(existing_names)
            for e in entities_to_store.values():
                if e["entity_name"] not in seen:
                    existing_names.append(e["entity_name"])
                    seen.add(e["entity_name"])
            doc_entities_data = {
                **current_doc_entities,
                "entity_names": existing_names,
                "count": len(existing_names),
                "update_time": int(time.time()),
            }
        await lightrag.full_entities.upsert({ctx.doc_id: doc_entities_data})
        await lightrag.full_entities.index_done_callback()


def _add_belongs_to_relations(
    chunk_results: List[Tuple], multimodal_data_list: List[Dict[str, Any]]
) -> List[Tuple]:
    """Add belongs_to relations linking extracted entities to parent multimodal entities.

    Adapted from ProcessorMixin._batch_add_belongs_to_relations_type_aware.
    """
    chunk_to_modal_entity: Dict[str, str] = {}
    chunk_to_file_path: Dict[str, str] = {}

    for data in multimodal_data_list:
        description = data["description"]
        content_type = data["content_type"]
        original_item = data["original_item"]
        formatted = apply_chunk_template(content_type, original_item, description)
        chunk_id = compute_mdhash_id(formatted, prefix="chunk-")
        chunk_to_modal_entity[chunk_id] = data["entity_info"]["entity_name"]
        chunk_to_file_path[chunk_id] = data.get("file_path", "multimodal_content")

    enhanced: List[Tuple] = []
    belongs_to_count = 0

    for maybe_nodes, maybe_edges in chunk_results:
        chunk_id = None
        for nodes_dict in maybe_nodes.values():
            if nodes_dict:
                chunk_id = nodes_dict[0].get("source_id")
                break

        if chunk_id and chunk_id in chunk_to_modal_entity:
            modal_entity_name = chunk_to_modal_entity[chunk_id]
            file_path = chunk_to_file_path.get(chunk_id, "multimodal_content")

            for entity_name in maybe_nodes.keys():
                if entity_name != modal_entity_name:
                    belongs_to_relation = {
                        "src_id": entity_name,
                        "tgt_id": modal_entity_name,
                        "description": f"Entity {entity_name} belongs to {modal_entity_name}",
                        "keywords": "belongs_to,part_of,contained_in",
                        "source_id": chunk_id,
                        "weight": 10.0,
                        "file_path": file_path,
                    }
                    edge_key = (entity_name, modal_entity_name)
                    if edge_key not in maybe_edges:
                        maybe_edges[edge_key] = []
                    maybe_edges[edge_key].append(belongs_to_relation)
                    belongs_to_count += 1

        enhanced.append((maybe_nodes, maybe_edges))

    return enhanced


def _collect_chunk_ids(
    multimodal_results: List[Dict[str, Any]],
    ctx: PipelineContext,
    lightrag: Any,
    config: Any,
) -> List[str]:
    """Collect chunk_ids from multimodal results for doc_status update."""
    chunk_ids = []
    for data in multimodal_results:
        description = data["description"]
        content_type = data["content_type"]
        original_item = data["original_item"]

        if content_type == "audio":
            segments = original_item.get("_audio_segments", [])
            for seg in segments:
                chunk_item = dict(original_item)
                chunk_item["asr_transcript"] = seg.get("text", "")
                chunk_item["asr_start_time"] = seg.get("start_time", 0.0)
                chunk_item["asr_end_time"] = seg.get("end_time", 0.0)
                chunk_item["asr_segment_index"] = seg.get("segment_index", 0)
                chunk_item["asr_total_segments"] = len(segments)
                chunk_item["asr_duration"] = original_item.get("_asr_result", {}).get("duration", 0)
                chunk_item["asr_language"] = original_item.get("_asr_result", {}).get("language", "unknown")
                chunk_item["asr_relative_position"] = seg.get("relative_position", 0.0)
                formatted = apply_chunk_template("audio", chunk_item, "")
                chunk_ids.append(compute_mdhash_id(formatted, prefix="chunk-"))
        elif content_type == "video":
            segments = original_item.get("_audio_segments", [])
            video_source_id = original_item.get("_video_source_id", "")
            for seg in segments:
                if not seg.get("text", "").strip() and not seg.get("frames"):
                    continue
                chunk_id = compute_mdhash_id(
                    f"{video_source_id}:{int(seg.get('start_time', 0) * 1000)}-{int(seg.get('end_time', 0) * 1000)}",
                    prefix="chunk-",
                )
                chunk_ids.append(chunk_id)
        else:
            formatted = apply_chunk_template(content_type, original_item, description)
            chunk_ids.append(compute_mdhash_id(formatted, prefix="chunk-"))

    return chunk_ids


def _generate_doc_id(content_list: List[Dict[str, Any]]) -> str:
    parts = []
    for item in content_list:
        if not isinstance(item, dict):
            parts.append(str(item))
            continue
        t = item.get("type")
        if t == "text" and item.get("text"):
            parts.append(item["text"].strip())
        elif t == "image" and item.get("img_path"):
            parts.append(f"image:{item['img_path']}")
        elif t == "table" and item.get("table_body"):
            parts.append(f"table:{item['table_body']}")
        elif t == "equation" and item.get("text"):
            parts.append(f"equation:{item['text']}")
        else:
            parts.append(str(item))
    return compute_mdhash_id("\n".join(parts), prefix="doc-")


# ── v2 HTTP mode: PushChunksStage ──────────────────────────────────────


def _inject_ns_token(
    text: str, tenant_id: str, kb_id: str, document_id: str
) -> str:
    """[jonex] 注入命名空间 token，使 chunk 内容 hash 带上 (tenant, kb, doc) 维度。

    对齐 v1 LightRAGAdapter 的行为（原 lightrag_adapter.py 上传前注入）：
    LightRAG 按 chunk 内容 hash 全局去重，跨文档/跨 KB 的相同文本会被去重合并，
    导致只有首篇文档抽到实体、其余文档图为空（本体阶段"无候选实体"→失败）。
    在文本末尾追加短 hash 的 HTML 注释 marker，使每个 (tenant, kb, doc) 拥有独立
    抽取记录、保证 ABox 隔离；同文档幂等重传时 token 相同 → hash 不变 → 仍可去重，
    不浪费 LLM。消费侧（task_manager 本体抽取）按 `<!--yx:[a-f0-9]{8}-->` 过滤该 token。
    """
    ns_raw = f"{tenant_id}|{kb_id}|{document_id}"
    ns_hash = hashlib.md5(ns_raw.encode()).hexdigest()[:8]
    return text + f"\n<!--yx:{ns_hash}-->"


def _split_long_text(text: str, max_chars: int) -> list[str]:
    """[jonex] §table-chunking: Split over-budget text at newline boundaries.

    When a text/table block exceeds *max_chars*, this splits it into segments
    that each fit within the budget.  Newlines are preferred as split points
    when the cut lands in the latter half of the segment (preserving paragraph
    integrity).  If no suitable newline is found the segment is cut at exactly
    *max_chars*.
    """
    if len(text) <= max_chars:
        return [text]
    segments: list[str] = []
    pos = 0
    while pos < len(text):
        end = pos + max_chars
        if end >= len(text):
            segments.append(text[pos:])
            break
        chunk = text[pos:end]
        nl = chunk.rfind("\n")
        if nl > max_chars // 2:
            end = pos + nl + 1
        segments.append(text[pos:end])
        pos = end
    return segments


def _load_table_tokenizer():
    """[jonex] §table-grid-v2 O1: lazily load a tokenizer with the same
    encoding LightRAG uses for its second-pass chunking (tiktoken
    gpt-4o-mini).

    Returns ``None`` when tiktoken/LightRAG is unavailable (e.g. unit tests
    without the dependency) — the oversize assertion then no-ops.
    """
    try:
        from lightrag.utils import TiktokenTokenizer

        return TiktokenTokenizer("gpt-4o-mini")
    except Exception as exc:  # noqa: BLE001 — 断言能力缺失不能影响推送
        logger.warning(
            "[jonex] §table-grid-v2 O1: tokenizer 加载失败（%s），"
            "表格 chunk 超限断言跳过", exc,
        )
        return None


def _count_col_unnamed(header: list[str]) -> int:
    """[jonex] §table-grid-v2 O4: count ``col_N`` fallback columns in a header."""
    return sum(1 for c in header if re.fullmatch(r"col_\d+", c))


# file_source 总长预算（字符）。LightRAG 删除文档时按 file_path 去 inputs 目录
# 删文件，Linux 文件名上限 255 字节（UTF-8 中文 3 字节/字）——留 240 字符
# 保守预算（≈ 720 字节内？不对：240 ASCII 字符 ≈ 240 字节；中文按 3 字节会超
# 255，但 file_source 主体是 ASCII（id/键名），旁路字段截断后中文占比小）。
_FILE_SOURCE_MAX_CHARS = 240


def _truncate_source_field(value: str, max_chars: int) -> str:
    """旁路字段截断：超限保留前 max_chars 字 + …"""
    if not value or len(value) <= max_chars:
        return value
    return value[:max_chars] + "…"


_CAPTION_PREV_BLOCK_MAX_DIST = 3     # 向前搜索的最大 block 距离（L4.1 防误抓）
_CAPTION_PREV_BLOCK_MAX_LEN = 60     # prev block 作为标题的长度上限（字）
_CAPTION_SENTENCE_END = "。！？；."   # 句末符——正文结尾而非标题


def _table_signature(header: list[str]) -> str:
    """[jonex] §table-grid-v2 L4.1: 列名签名（table_sig 旁路）。"""
    return hashlib.md5("|".join(header).encode()).hexdigest()[:8]


def _resolve_table_caption(
    item: dict, content_list: list[dict], idx: int,
    file_name: str, sheet_hint: str | None = None,
) -> tuple[str, str]:
    """[jonex] §table-grid-v2 L4.1: 表标题 4 级来源链。

    1. ``table_caption``（经 ``_valid_caption`` 校验——空值/字面量 "None"/
       超 60 字一律无效，防止写出「【表】None」）；
    2. 向前最近的 text block，须同时满足：距离 ≤ 3 个 block、长度 ≤ 60 字、
       不以句末符结尾（正文尾巴不是标题）、不含换行；
    3. sheet 名（xlsx 主轨预留，通常已在级别 1 命中）；
    4. 文件名（兜底，保证标题永不缺失）。

    Returns ``(caption, source_level)``，source_level ∈
    ``caption / prev_block / sheet / filename``（计入 table_stats.caption_source）。
    """
    cap = _valid_caption(item.get("table_caption"))
    if cap:
        return cap, "caption"
    for j in range(
        idx - 1,
        max(-1, idx - 1 - _CAPTION_PREV_BLOCK_MAX_DIST),
        -1,
    ):
        prev = content_list[j]
        if prev.get("type") != "text":
            continue
        txt = (prev.get("text") or "").strip()
        if not txt:
            continue
        if len(txt) > _CAPTION_PREV_BLOCK_MAX_LEN:
            continue
        if txt[-1] in _CAPTION_SENTENCE_END:
            continue
        if "\n" in txt:
            continue
        return txt, "prev_block"
    if sheet_hint:
        return sheet_hint, "sheet"
    return file_name, "filename"


def _xlsx_native_normalize_enabled() -> bool:
    """[jonex] §table-grid-v2 L3: read the XLSX_NATIVE_NORMALIZE switch.

    Unset → openpyxl 主轨 + MinerU 图片轨（default true）；显式 ``false`` →
    xlsx 全量交 MinerU（旧行为）。见 plan §5 灰度表。
    """
    return os.getenv("XLSX_NATIVE_NORMALIZE", "true").lower() in (
        "1", "true", "yes", "on",
    )


def _merge_xlsx_native_tables(
    content_list: list[dict], path: str,
) -> tuple[list[dict], int]:
    """[jonex] §table-grid-v2 L3: xlsx 双路合并（幂等）。

    openpyxl 主轨产出表格 item，MinerU 只保留非表格项。跨包依赖平台侧
    ``jonex_core.capability.atomic.rag.spreadsheet_normalizer``（atomic-rag
    容器内 jonex_core 已 COPY；ImportError 时静默回退 MinerU 全量，保证
    上游升级时不炸）。
    """
    try:
        from jonex_core.capability.atomic.rag.spreadsheet_normalizer import (
            merge_native_tables,
        )
    except ImportError:
        logger.warning(
            "[jonex] §table-grid-v2 L3: jonex_core.spreadsheet_normalizer 不可用，"
            "xlsx 双路合并跳过（回退 MinerU 全量）"
        )
        return content_list, 0
    return merge_native_tables(content_list, path)


def _first_present(primary: dict, fallback: dict, *keys: str):
    for key in keys:
        if key in primary and primary[key] is not None:
            return primary[key]
        if key in fallback and fallback[key] is not None:
            return fallback[key]
    return None


def _build_file_source(
    tenant_id: str,
    kb_id: str,
    document_id: str,
    file_name: str,
    chunk_index: int = 0,
    *,
    trace_id: str = "",
    page: int | None = None,
    line_start: int | None = None,
    line_end: int | None = None,
    char_start: int | None = None,
    char_end: int | None = None,
    table_idx: int | None = None,
    image_idx: int | None = None,
    asset_ext: str | None = None,
    row_start: int | None = None,
    row_end: int | None = None,
    cell_start: int | None = None,
    cell_end: int | None = None,
    start_time: float | None = None,
    end_time: float | None = None,
    ctype: str | None = None,
    table_sig: str | None = None,
    table_cols: str | None = None,
    notes: str | None = None,
    entity_hint: str | None = None,
    page_end: int | None = None,
    pspans: str | None = None,
    stats: dict | None = None,
) -> str:
    """Build a file_source string compatible with v1 parse_file_source().

    Format::

        kb={kb}|doc={doc}|tenant={t}|file={f}|chunk={idx}
          |cstart={s}|cend={e}   ← line_start/line_end (MinerU 行号)
          |char_start={s}|char_end={e}  ← char_start/char_end (parse_text 字符位置)
          |page={n}
          |row_start={n}|row_end={n}   ← [jonex] §table-chunking 表行区间
          |tstart={t}|tend={t}   ← start_time/end_time (视频/音频时间轴)
          |table_idx={n}|image_idx={n}|trace={trace}
          |ctype={type}|table_sig={hash}|table_cols={cols}|notes={notes}
            ← [jonex] §table-ctypes / §table-grid-v2 L4.1
            chunk 类型与表格元数据旁路（table-parsing-retrieval-governance-plan.md
            改动 37；值内的 | 转义为空格，防止破坏分隔结构）
          |ehint={entity_hint}
            ← [jonex] 第五批 改动 20（方案 C 入库通道）：表格 chunk 的主体
            实体提示（= L4.1 表标题 heading），检索期主体一致性过滤的信号源
            （与 ctype 同机制，零 vendored 改动）

    Extra fields (table_idx, image_idx, row_start, row_end, ctype, …) are
    appended as ``key=value`` pairs — ``parse_file_source()`` ignores unknown
    keys silently, so these are backward-compatible.
    """
    parts = [
        f"kb={kb_id}",
        f"doc={document_id}",
        f"tenant={tenant_id}",
        f"file={file_name}",
        f"chunk={chunk_index}",
    ]
    if line_start is not None and line_end is not None:
        parts.append(f"cstart={line_start}")
        parts.append(f"cend={line_end}")
    if char_start is not None and char_end is not None:
        parts.append(f"char_start={char_start}")
        parts.append(f"char_end={char_end}")
    if page is not None:
        parts.append(f"page={page}")
    if page_end is not None and page_end != page:
        # [jonex] §block-packing 2d: 跨页包末页（仅打包路径写）
        parts.append(f"page_end={page_end}")
    if pspans:
        # [jonex] §block-packing 2d: 页边界表（offset@page;…，60 字符上限）。
        # 截断会留下半截 entry（resolve 跳过、中间页丢失），与 240 防御
        # 丢段共用 pspans_truncated 观测口径（review 低优先）。
        truncated = _truncate_source_field(pspans, 60)
        if len(truncated) != len(pspans) and stats is not None:
            stats["pspans_truncated"] = (
                stats.get("pspans_truncated", 0) + 1
            )
        parts.append(f"pspans={truncated}")
    if row_start is not None and row_end is not None:
        parts.append(f"row_start={row_start}")
        parts.append(f"row_end={row_end}")
        # [jonex] §C1-bis §19.5①: 行内格区间（0-based 右开），写入条件
        # 跟随 row 区间——只对 table_row chunk 有意义，且只在行内切分
        # （split_row_by_cells）真正发生时非 None。
        if cell_start is not None and cell_end is not None:
            parts.append(f"cell_start={cell_start}")
            parts.append(f"cell_end={cell_end}")
    if start_time is not None and end_time is not None:
        parts.append(f"tstart={start_time:.3f}")
        parts.append(f"tend={end_time:.3f}")
    if table_idx is not None:
        parts.append(f"table_idx={table_idx}")
    if image_idx is not None:
        parts.append(f"image_idx={image_idx}")
    if asset_ext is not None:
        # [jonex] §image-refs P0-5/P1-3: 资产扩展名旁路（P1 上传成功的
        # 图片才写；ext 白名单由 normalize_asset_ext 保证，此处直写）
        parts.append(f"aext={asset_ext}")
    if ctype is not None:
        parts.append(f"ctype={ctype}")
    if table_sig is not None:
        parts.append(f"table_sig={table_sig}")
    if table_cols is not None:
        table_cols = _truncate_source_field(table_cols, 60)
        parts.append(f"table_cols={str(table_cols).replace('|', ' ')}")
    if notes is not None:
        notes = _truncate_source_field(notes, 40)
        parts.append(f"notes={str(notes).replace('|', ' ')}")
    if entity_hint is not None:
        # [jonex] 改动 20：ehint= 主体实体提示（表格标题 heading）
        entity_hint = _truncate_source_field(str(entity_hint), 40)
        parts.append(f"ehint={entity_hint.replace('|', ' ')}")
    parts.append(f"trace={trace_id}")
    # [jonex] §table-grid-v2 修复：file_source 被 LightRAG 当作「文件路径」
    # 使用（删除文档时按 file_path 去 inputs 目录删文件），Linux 文件名上限
    # 255 字节。元数据旁路（table_cols/notes）膨胀后超限 → [Errno 36] File
    # name too long → 删除失败 → reparse 收敛循环不收敛（线上实测）。
    # 构建后总长防御：超预算再截 table_cols（完整列名在 chunk 正文里已有，
    # self-describing 格式，旁路截断不丢信息）。
    source = "|".join(parts)
    if len(source) > _FILE_SOURCE_MAX_CHARS:
        over = len(source) - _FILE_SOURCE_MAX_CHARS
        # 从 table_cols 段榨出空间（定位并截断）
        idx = source.find("table_cols=")
        if idx >= 0:
            end = source.find("|", idx)
            end = end if end >= 0 else len(source)
            tc_len = end - idx
            keep = max(8, tc_len - over - 1)
            source = source[:idx + len("table_cols=") + keep] + "…" + source[end:]
        # [jonex] §C1-bis §19.5①: 仍超限则丢弃 cell_* 段——行级锚点
        # 仍在，只丢行内格区间；cell 段晚于 table_cols 加入，优先牺牲。
        if len(source) > _FILE_SOURCE_MAX_CHARS:
            source = source.replace(f"|cell_start={cell_start}", "")
            source = source.replace(f"|cell_end={cell_end}", "")
        # [jonex] §block-packing 2d: 仍超限则丢弃 pspans 段并计数——
        # pspans 是精度增强项，丢弃后降级到 page/page_end 粗锚点，
        # 不影响正确性。
        if len(source) > _FILE_SOURCE_MAX_CHARS and pspans:
            idx = source.find("|pspans=")
            if idx >= 0:
                end = source.find("|", idx + 1)
                end = end if end >= 0 else len(source)
                source = source[:idx] + source[end:]
                if stats is not None:
                    stats["pspans_truncated"] = (
                        stats.get("pspans_truncated", 0) + 1
                    )
    return source


# ── [jonex] P0-1 dup-failed helper functions ──────────────────────────


def _extract_dup_original_id(status: TrackStatus) -> str | None:
    """Extract original_doc_id from a failed TrackStatus when the failure is a
    duplicate-content rejection (``Content already exists``).

    Priority:
    1. Structured ``doc_metadata.is_duplicate`` → ``doc_metadata.original_doc_id``
    2. Fallback regex on ``status.error`` for ``Original doc_id: <id>``
    """
    # 结构化检测（优先）
    meta = status.doc_metadata
    if isinstance(meta, dict) and meta.get("is_duplicate"):
        oid = meta.get("original_doc_id")
        if oid and isinstance(oid, str):
            return oid
    # 兜底：error 文本匹配
    err = status.error or ""
    if "Content already exists" in err:
        m = re.search(r"Original doc_id:\s*(\S+)", err)
        if m:
            return m.group(1)
    return None


async def _query_doc_status(
    http_client: Any,
    doc_id: str,
    *,
    tenant_id: str,
    kb_id: str,
) -> str | None:
    """Query LightRAG for the current status of *doc_id* (LightRAG-internal id).

    Returns one of ``"processed"``, ``"pending"``, ``"processing"``, ``"failed"``,
    or ``None`` when the document is not found or the query fails.
    """
    try:
        doc_info = await http_client.get_document_status(
            doc_id, tenant_id=tenant_id, kb_id=kb_id,
        )
    except Exception:
        return None
    if not isinstance(doc_info, dict):
        return None
    raw = doc_info.get("status")
    if raw is None:
        return None
    return str(raw).lower()


def _expected_doc_id(chunk: dict) -> str:
    """[jonex] ② push 超时确认：复算 LightRAG 对该 chunk 文本分配的 doc_id。

    口径必须与 LightRAG ``apipeline_enqueue_documents`` 完全一致——
    ``compute_mdhash_id(sanitize_text_for_encoding(text), prefix="doc-")``
    （见 Reference/LightRAG/lightrag/lightrag.py），否则查不到对应 doc。
    用于超时确认阶段直接查该 doc 真实状态，规避 track_id 重推变 dup 后
    "内容已 processed 却因 track 未确认而假超时（RAG_PUSH_TIMEOUT）" 的问题。
    """
    text = chunk.get("text", "") or ""
    return compute_mdhash_id(sanitize_text_for_encoding(text), prefix="doc-")


class AssetUploadStage(Stage):
    """[jonex] §image-refs P1-2: 把文档内嵌图片上传到平台对象存储。

    设计要点（image-reference-chain-execution-plan.md §4.1 / §5 P1）：
    - 复用 multimodal_results 的 item["index"] 做对象键，不另起枚举
      （与 PushChunksStage._collect_multimodal_chunks 同源，key 才不错位）；
    - gate：RAG_ASSET_UPLOAD_ENABLED（默认 on），best-effort——单张失败只
      WARNING，不阻塞主链路（doc 仍 READY）；
    - 上传结果 {image_idx: ext} 经 StageResult.asset_exts 回传 ctx，
      PushChunksStage 据此在 file_source 写 aext=（P1-3，上传失败的图片
      不写，检索侧据此判定无 URL 可取）。
    """

    async def execute(
        self, ctx: PipelineContext, services: PipelineServices
    ) -> StageResult:
        if os.getenv("RAG_ASSET_UPLOAD_ENABLED", "true").lower() not in (
            "1", "true", "yes", "on",
        ):
            return StageResult()

        results = ctx.multimodal_results or []
        if not results:
            return StageResult()

        # 平台侧依赖（jonex_core 对象存储）——非平台环境（standalone 研究用 /
        # 镜像未打包 jonex_core）整体跳过，best-effort 不阻塞主链路
        try:
            from jonex_core.common.object_storage import (
                build_asset_key,
                get_object_storage,
                normalize_asset_ext,
            )

            storage = get_object_storage()
        except Exception as exc:  # noqa: BLE001
            if services.logger:
                services.logger.warning(
                    "[jonex] §image-refs: jonex_core 对象存储不可用，跳过图片资产上传: %s",
                    exc,
                )
            return StageResult()

        asset_exts: dict[int, str] = {}
        # [jonex] review 修正：doc 锚点与 PushChunksStage 同源——
        # ctx.document_id 为空时兜底 ctx.doc_id（与 chunk 侧
        # `ctx.document_id or ctx.doc_id or ""` 同一表达式），
        # 否则上传 key 的 doc 段是空串、file_source 的 doc= 却落到
        # ctx.doc_id，检索侧推导的 key 与实际 key 分叉（§4.1 同源约束）。
        doc_anchor = ctx.document_id or ctx.doc_id or ""
        if not doc_anchor:
            # 两侧兜底后仍为空 → doc 段空串的 key 无法回链检索侧，
            # 上传无意义，整体跳过（比上传一个无人能定位的 key 更安全）
            if services.logger:
                services.logger.warning(
                    "[jonex] §image-refs: document_id/doc_id 均为空，跳过图片资产上传（无文档锚点）"
                )
            return StageResult()
        for item in results:
            if item.get("content_type", item.get("type", "image")) != "image":
                continue
            image_idx = item.get("index")
            if image_idx is None:
                continue
            img_path = (item.get("original") or {}).get("img_path") or ""
            if not img_path or not os.path.isfile(img_path):
                continue
            ext = normalize_asset_ext(os.path.splitext(img_path)[1].lstrip(".")) or "png"
            try:
                key = build_asset_key(
                    ctx.tenant_id, ctx.kb_id, doc_anchor, image_idx, ext,
                )
                await storage.put_bytes(
                    key, Path(img_path).read_bytes(), content_type=f"image/{ext}",
                )
                asset_exts[image_idx] = ext
            except Exception as exc:  # noqa: BLE001
                if services.logger:
                    services.logger.warning(
                        "[jonex] §image-refs: 图片资产上传失败 idx=%s path=%s: %s",
                        image_idx, img_path, exc,
                    )

        if not asset_exts:
            return StageResult()
        return StageResult(asset_exts=asset_exts)


class PushChunksStage(Stage):
    """HTTP mode: collect all text + multimodal chunks → push to :9621/documents/text.

    Replaces TextInsertStage + MultimodalChunkStage + EntityExtractStage + EntityMergeStage
    when running against an external LightRAG Server.

    Failure model (design §3.4):
      - Transient (network timeout, 5xx): retry up to 3× with exponential backoff.
      - Non-retryable (4xx): skip that chunk, continue with remaining.
      - track_status timeout: mark task FAILED.
      - At least 1 chunk succeeded → SUCCESS; all failed → FAILED.
    """

    def __init__(self):
        self._push_concurrency = int(os.getenv("RAG_HTTP_PUSH_CONCURRENCY", "8"))
        self._chunk_max_chars = int(os.getenv("RAG_CHUNK_MAX_CHARS", "12000"))
        # [jonex] §table-grid-v2: 启动打印生效值，避免「以为回退了其实没回退」
        # （table-parsing-retrieval-governance-plan.md §5）。
        logger.info(
            "[jonex] §table-grid-v2: RAG_TABLE_GRID_V2=%s "
            "（true=网格化+表头推断新路径 / false=旧 normalize_table_rows）",
            _table_grid_v2_enabled(),
        )
        # [jonex] §table-grid-v2 O1: 表格专用切块预算（字符）。只作用于表格分支与
        # pack_rows；文本链路仍用 _chunk_max_chars（RAG_CHUNK_MAX_CHARS，12000）
        # 由 LightRAG 按 token+overlap 切分，行为不变。
        self._table_chunk_max_chars = int(os.getenv("RAG_TABLE_CHUNK_MAX_CHARS", "900"))
        self._table_chunk_body_budget = max(200, self._table_chunk_max_chars - 64)
        # 表格 chunk 超限断言阈值（token）：与 LightRAG 端 CHUNK_SIZE 对齐
        # （.env.rag 的 CHUNK_SIZE，默认 1200）。推送前用 tokenizer 实测每个
        # 表格 chunk，超过 → WARNING + table_stats.oversize_table_chunks。
        self._lightrag_chunk_size = int(os.getenv("RAG_LIGHTRAG_CHUNK_SIZE", "1200"))
        self._table_tokenizer = _load_table_tokenizer()
        logger.info(
            "[jonex] §table-grid-v2 O1: RAG_TABLE_CHUNK_MAX_CHARS=%d "
            "（表格专用预算，文本链路仍用 RAG_CHUNK_MAX_CHARS=%d）；"
            "超限断言阈值=%d token（tokenizer %s）",
            self._table_chunk_max_chars, self._chunk_max_chars,
            self._lightrag_chunk_size,
            "可用" if self._table_tokenizer else "不可用（断言跳过）",
        )
        self._track_timeout = float(os.getenv("RAG_TRACK_TIMEOUT_SECONDS", "1800"))   # 全局安全网
        self._per_chunk_timeout = float(os.getenv("RAG_TRACK_PER_CHUNK_TIMEOUT_SECONDS", "900"))
        self._per_chunk_max_retries = int(os.getenv("RAG_TRACK_PER_CHUNK_MAX_RETRIES", "2"))
        # [jonex] §13.4 item1：超时窗口按 chunk 数动态下限（floor，仅放大不缩小）。
        # 超大文档（数千 chunk）抽取长尾远超固定 900/1800s → 被误判超时→重推变 dup。
        # 有效窗口 = clamp(base, SCALE×chunk数, CEIL)；SCALE=0 关闭（回退固定值）。
        self._track_scale_per_chunk = float(os.getenv("RAG_TRACK_SCALE_PER_CHUNK_SEC", "2"))
        self._track_scale_ceil = float(os.getenv("RAG_TRACK_SCALE_CEIL_SEC", "10800"))
        self._retry_max = 3
        self._retry_base = 2.0  # seconds
        # [jonex] 1-A：track 终态 failed 的 chunk 也纳入有界重推（复用 per-chunk 重试预算），
        # 仅预算耗尽后才判永久硬失败，避免「几个 chunk 抖动 → 严格模式整体失败 → reparse 全量回滚
        # 258 个新 doc」的放大链。设 false 可回退到旧行为（终态 failed 立即判硬失败、不重推）。
        self._retry_terminal_failed = os.getenv(
            "RAG_PUSH_RETRY_TERMINAL_FAILED", "true"
        ).lower() in ("1", "true", "yes", "on")
        # [jonex] #6: strict chunk-by-chunk doc_id/track_id confirmation
        self._require_doc_ids = os.getenv("RAG_REQUIRE_DOC_IDS", "true").lower() in (
            "1", "true", "yes", "on",
        )
        # 防误配：per-chunk 超时须小于全局网，否则 per-track 淘汰永不触发
        if self._per_chunk_timeout >= self._track_timeout:
            logger.warning(
                "RAG_TRACK_PER_CHUNK_TIMEOUT_SECONDS(%.0f) >= RAG_TRACK_TIMEOUT_SECONDS(%.0f)，"
                "已收敛为全局网的 0.8 倍以保证 per-chunk 淘汰生效",
                self._per_chunk_timeout, self._track_timeout,
            )
            self._per_chunk_timeout = self._track_timeout * 0.8

        # [jonex] §table-chunking: reserve ~64 chars for the _inject_ns_token
        # suffix so the final chunk body does not exceed _chunk_max_chars.
        self._chunk_body_budget = max(200, self._chunk_max_chars - 64)

        # [jonex] §block-packing 文本块级打包
        # （text-block-packing-chunk-governance-plan.md §4 改动 2a）。
        self._text_pack_enabled = os.getenv(
            "RAG_TEXT_BLOCK_PACKING", "true"
        ).lower() in ("1", "true", "yes", "on")
        # 与表格链路 _table_chunk_body_budget=836 同构：配置层只暴露一个字符预算，
        # token 换算前置固化进默认值（1260 = 900 token × 1.4，o200k_base 实测校准，
        # 见方案 §3.1「字符↔token 换算系数」）。冲刷前另有 tokenizer 实测断言兜底。
        self._text_pack_budget = int(os.getenv("RAG_TEXT_PACK_CHARS", "1260"))
        self._text_pack_max_tokens = int(os.getenv("RAG_TEXT_PACK_MAX_TOKENS", "1200"))
        self._text_pack_heading_max_len = int(
            os.getenv("RAG_TEXT_PACK_HEADING_MAX_LEN", "40")
        )
        self._text_pack_drop_noise = os.getenv(
            "RAG_TEXT_PACK_DROP_NOISE", "true"
        ).lower() in ("1", "true", "yes", "on")
        logger.info(
            "[jonex] §block-packing: RAG_TEXT_BLOCK_PACKING=%s "
            "budget=%d chars max=%d tokens heading_max_len=%d drop_noise=%s",
            self._text_pack_enabled, self._text_pack_budget,
            self._text_pack_max_tokens, self._text_pack_heading_max_len,
            self._text_pack_drop_noise,
        )

    async def execute(
        self, ctx: PipelineContext, services: PipelineServices
    ) -> StageResult:
        http_client = services.http_client
        if http_client is None:
            return StageResult(error="PushChunksStage requires http_client in PipelineServices")

        tenant_id = ctx.tenant_id
        kb_id = ctx.kb_id
        # file_source 的 doc= 锚点必须用 KB 文档 id（ctx.document_id），
        # 不能用 ctx.doc_id——后者已被 ParseStage 覆盖为解析内容哈希，
        # 会导致 KB 按 document_id 过滤（图谱/本体抽取/删除）全部落空。
        document_id = ctx.document_id or ctx.doc_id or ""
        file_name = ctx.file_name

        # ── [jonex] Callback: push_chunks start ──
        cb = services.callback_manager
        if cb:
            cb.dispatch("on_push_chunks_start", file_path=ctx.file_name)

        # ── 1. Collect all chunks ───────────────────────────────────
        chunks: list[dict] = []
        # [jonex] §table-grid-v2 O4: 表格处理可观测统计，透出到任务结果
        # （task_manager 汇总进 result_summary.extensions["table_stats"]）。
        table_stats: dict = {}
        ctx.table_stats = table_stats
        self._collect_text_chunks(chunks, ctx.content_list or [], tenant_id, kb_id,
                                  document_id, file_name, stats=table_stats)
        self._collect_multimodal_chunks(chunks, ctx.multimodal_results or [],
                                        tenant_id, kb_id, document_id, file_name,
                                        asset_exts=ctx.asset_exts)

        total_chunks = len(chunks)
        if total_chunks == 0:
            if services.logger:
                services.logger.warning("PushChunksStage: no chunks to push")
            return StageResult()

        # ── 1.5 Content-based dedup (avoid TOCTOU orphan track_ids) ─
        # Two chunks with identical text produce the same content_doc_id in
        # LightRAG.  If pushed concurrently, the TOCTOU race in
        # apipeline_enqueue_documents (filter_keys vs upsert) can leave orphan
        # track_ids that poll forever.  Dedup here: only POST once per unique
        # content, then share the track_id/doc_id across duplicates.
        content_hash_to_first_idx: dict[str, int] = {}
        for idx, chunk in enumerate(chunks):
            text = chunk.get("text", "") or ""
            if not text:
                continue
            content_doc_id = compute_mdhash_id(
                sanitize_text_for_encoding(text), prefix="doc-"
            )
            if content_doc_id not in content_hash_to_first_idx:
                content_hash_to_first_idx[content_doc_id] = idx

        dedup_skipped = total_chunks - len(content_hash_to_first_idx)
        dedup_map: dict[int, int] = {}  # duplicate_idx → first_occurrence_idx
        for idx, chunk in enumerate(chunks):
            text = chunk.get("text", "") or ""
            if not text:
                continue
            content_doc_id = compute_mdhash_id(
                sanitize_text_for_encoding(text), prefix="doc-"
            )
            first_idx = content_hash_to_first_idx.get(content_doc_id)
            if first_idx is not None and first_idx != idx:
                dedup_map[idx] = first_idx

        if dedup_skipped > 0 and services.logger:
            services.logger.info(
                "PushChunksStage: content dedup skipped %d/%d chunks "
                "(only %d unique content hashes)",
                dedup_skipped, total_chunks, len(content_hash_to_first_idx),
            )

        # Only POST unique chunks (by content hash)
        unique_indices = sorted(content_hash_to_first_idx.values())

        if services.logger:
            services.logger.info(
                "PushChunksStage: pushing %d unique chunks to :9621 "
                "(total %d, dedup %d)",
                len(unique_indices), total_chunks, dedup_skipped,
            )

        # ── 2. Concurrent POST with cancellation check ──────────────
        semaphore = asyncio.Semaphore(self._push_concurrency)
        track_ids: dict[int, str] = {}  # chunk_index → track_id
        failed_indices: set[int] = set()
        duplicated_indices: set[int] = set()  # [jonex] #5: duplicated at :9621

        async def _push_one(idx: int, chunk: dict) -> None:
            if ctx.cancel_event and ctx.cancel_event.is_set():
                return  # cancelled — don't push more

            async with semaphore:
                try:
                    result = await self._push_with_retry(
                        http_client, chunk, tenant_id, kb_id,
                    )
                    track_ids[idx] = result.track_id
                    # [jonex] #5: track duplicated for all-duplicated guard
                    if result.status == "duplicated":
                        duplicated_indices.add(idx)
                    # If :9621 returns doc_ids synchronously, collect now
                    if result.doc_ids:
                        ctx.collected_doc_ids.extend(result.doc_ids)
                except Exception as e:
                    if services.logger:
                        services.logger.warning(
                            f"PushChunksStage: chunk {idx} failed: {e}"
                        )
                    failed_indices.add(idx)

        # Push unique chunks only (dedup by content hash)
        tasks = [_push_one(i, chunks[i]) for i in unique_indices]
        await asyncio.gather(*tasks, return_exceptions=True)

        # ── 3. Check cancellation after push phase ──────────────────
        if ctx.cancel_event and ctx.cancel_event.is_set():
            # Already-pushed chunks stay in :9621; doc_ids collected so far are kept
            ctx.pending_track_ids = list(track_ids.values())
            ctx.total_chunk_count = total_chunks
            ctx.failed_chunk_count = len(failed_indices)
            return StageResult(
                error="Task cancelled during chunk push",
                content_list=ctx.content_list,
                multimodal_results=ctx.multimodal_results,
            )

        # ── 4. Per-chunk timeout + retry loop ─────────────────────
        terminal_hard_failed: set[int] = set()  # [jonex] #6: terminal state=="failed"

        # [jonex] §13.4 item1：按 chunk 数动态放大轮询窗口（floor，仅放大不缩小），
        # 保持 per-chunk < global（沿用 0.8 倍防误配口径）。SCALE=0 → 用固定值。
        # 方法级定义，供轮询循环与 §5 严格判定文案共用。
        eff_track_timeout = self._track_timeout
        eff_per_chunk_timeout = self._per_chunk_timeout
        if self._track_scale_per_chunk > 0 and total_chunks > 0:
            dyn = self._track_scale_per_chunk * total_chunks
            eff_track_timeout = min(self._track_scale_ceil, max(self._track_timeout, dyn))
            eff_per_chunk_timeout = min(
                eff_track_timeout * 0.8, max(self._per_chunk_timeout, dyn)
            )
            if services.logger and eff_track_timeout > self._track_timeout:
                services.logger.info(
                    "PushChunksStage: §13.4 动态超时窗口 chunks=%d track=%.0fs per_chunk=%.0fs "
                    "(base track=%.0f/per_chunk=%.0f)",
                    total_chunks, eff_track_timeout, eff_per_chunk_timeout,
                    self._track_timeout, self._per_chunk_timeout,
                )

        if track_ids:
            # 反查：track_id → chunk_index（重推后需更新）
            tid_to_idx: dict[str, int] = {tid: idx for idx, tid in track_ids.items()}
            pending_ids = list(track_ids.values())
            retry_round = 0
            ctx.pending_track_ids = []

            while pending_ids:
                if ctx.cancel_event and ctx.cancel_event.is_set():
                    ctx.pending_track_ids = list(pending_ids)
                    ctx.total_chunk_count = total_chunks
                    ctx.failed_chunk_count = len(failed_indices | terminal_hard_failed)
                    return StageResult(
                        error="Task cancelled during chunk tracking",
                        content_list=ctx.content_list,
                        multimodal_results=ctx.multimodal_results,
                    )

                try:
                    terminal, still_pending = await http_client.batch_track_status(
                        pending_ids,
                        tenant_id=tenant_id,
                        kb_id=kb_id,
                        max_wait_seconds=eff_track_timeout,
                        per_track_timeout_seconds=eff_per_chunk_timeout,
                    )
                except Exception as e:
                    if services.logger:
                        services.logger.error(f"PushChunksStage: batch_track_status failed: {e}")
                    return StageResult(error=f"track_status polling failed: {e}")

                # 收集完成 doc_ids；[jonex] 1-A：终态 failed 先暂存本轮，不立刻判永久硬失败
                # [jonex] P0-1：轮询层识别 dup-failed，按原件状态三态判定，避免
                # 「N 个 dup 误判 → strict 整体失败 → reparse 全量回滚」放大链。
                round_terminal_failed_idx: list[int] = []
                dup_benign = 0   # 原件 processed → 良性成功，不计 hard_failed
                dup_wait = 0     # 原件 pending/processing → 继续轮询，不计 hard_failed
                dup_wait_tids: list[str] = []  # 保留在 polling 集合继续等原件完成
                for tid, status in terminal.items():
                    if status.state == "completed":
                        ctx.collected_doc_ids.extend(status.doc_ids)
                    else:
                        # ── [jonex] P0-1: dup-failed 三态判定 ──────────
                        original_doc_id = _extract_dup_original_id(status)
                        if original_doc_id:
                            original_status = await _query_doc_status(
                                http_client, original_doc_id,
                                tenant_id=tenant_id, kb_id=kb_id,
                            )
                            if original_status == "processed":
                                dup_benign += 1
                                continue  # 良性成功：内容已可检索
                            # 在途态（pending/processing/preprocessed）→ 原件仍在
                            # pipeline 中，等待完成。preprocessed = 文本已入库、
                            # 多模态待处理，也是健康的在途态，不应判 hard_failed。
                            if original_status in ("pending", "processing", "preprocessed"):
                                dup_wait += 1
                                dup_wait_tids.append(tid)
                                continue  # 保留轮询，等原件完成
                            if original_status is None:
                                # [jonex] 原件查不到（404 或端点未部署）→ 保守落入
                                # hard_failed；get_document_status 内部已打 WARNING
                                pass
                            # original_status == "failed" / None → 落入 hard_failed
                        # ── [jonex] P0-1 end ────────────────────────────

                        idx = tid_to_idx.get(tid)
                        if idx is not None:
                            round_terminal_failed_idx.append(idx)

                if dup_benign or dup_wait:
                    if services.logger:
                        services.logger.info(
                            "PushChunksStage: P0-1 dup 三态判定 benign=%d wait=%d"
                            "（不计 hard_failed；wait=%d 继续轮询等待原件完成）",
                            dup_benign, dup_wait, dup_wait,
                        )

                # 区分「per-chunk 超时」与「全局网剩余（仍在处理）」
                timeout_tids = [t for t, s in still_pending.items() if s.state == "timeout"]
                other_pending = [t for t, s in still_pending.items() if s.state != "timeout"]
                # [jonex] P0-1: dup_wait 保留在轮询集合，每轮重查原件状态
                if dup_wait_tids:
                    other_pending.extend(dup_wait_tids)

                # [jonex] 1-A：本轮是否还能重推（未耗尽 per-chunk 预算）。
                # 关闭开关或预算耗尽时，终态 failed 立即落永久硬失败（旧行为）。
                can_retry = retry_round < self._per_chunk_max_retries
                retry_terminal_failed = self._retry_terminal_failed and can_retry
                if not retry_terminal_failed:
                    for idx in round_terminal_failed_idx:
                        terminal_hard_failed.add(idx)
                    round_terminal_failed_idx = []

                if not timeout_tids and not round_terminal_failed_idx and not dup_wait_tids:
                    ctx.pending_track_ids = list(still_pending.keys())
                    break
                # [jonex] P0-1: retry 预算耗尽时，dup_wait 不再无限轮询。
                # 原件经历 per_chunk_max_retries 轮仍 pending → 大概率 orphan，
                # 释放为 benign（内容本应已在原件中），靠任务级 HARD 最终兜底。
                if not can_retry:
                    if dup_wait_tids:
                        if services.logger:
                            services.logger.warning(
                                "PushChunksStage: P0-1 dup_wait=%d 超过 %d 轮仍 pending，"
                                "释放为 benign（靠任务 HARD 兜底）",
                                len(dup_wait_tids), self._per_chunk_max_retries,
                            )
                        dup_wait_tids = []
                    ctx.pending_track_ids = list(still_pending.keys())
                    break

                # 重推超时 chunk +（1-A）终态失败 chunk：原文重新 upload_text，拿新 track_id
                retry_round += 1
                if ctx.cancel_event and ctx.cancel_event.is_set():
                    ctx.pending_track_ids = list(pending_ids)
                    ctx.total_chunk_count = total_chunks
                    ctx.failed_chunk_count = len(failed_indices | terminal_hard_failed)
                    return StageResult(
                        error="Task cancelled during chunk retry",
                        content_list=ctx.content_list,
                        multimodal_results=ctx.multimodal_results,
                    )

                # 去重：dup_wait 跨轮累积可能产生重复 track_id
                new_pending_ids: list[str] = list(dict.fromkeys(other_pending))
                # 去重合并：超时 chunk 下标 + 终态失败 chunk 下标
                repush_idx: list[int] = []
                _seen_idx: set[int] = set()
                # [jonex] §13.4 item2：超时 chunk 重推前先查真实 doc 状态，避免把"慢但健康"
                # 的 chunk 重推成 dup（新 track 永不 completed）。
                #   - processed        → 判为已确认（收集 doc_id，不重推）；
                #   - pending/processing → 继续轮询原 track（重新入 pending，不重推、不造 dup）；
                #   - failed/查不到     → 才重推（真失败/内容确实没进）。
                _t2_confirmed = 0
                _t2_wait = 0
                for _t in timeout_tids:
                    _i = tid_to_idx.get(_t)
                    if _i is None or _i in _seen_idx:
                        continue
                    _st = None
                    try:
                        _did = _expected_doc_id(chunks[_i])
                        _st = await _query_doc_status(
                            http_client, _did, tenant_id=tenant_id, kb_id=kb_id,
                        )
                    except Exception:
                        _st = None
                    _seen_idx.add(_i)
                    if _st == "processed":
                        ctx.collected_doc_ids.append(_did)
                        _t2_confirmed += 1
                    elif _st in ("pending", "processing", "preprocessed"):
                        new_pending_ids.append(_t)
                        _t2_wait += 1
                    else:
                        repush_idx.append(_i)
                if (_t2_confirmed or _t2_wait) and services.logger:
                    services.logger.info(
                        "PushChunksStage: §13.4 item2 超时复查——已确认 %d、继续轮询 %d、"
                        "待重推 %d（避免重推在途 chunk 造 dup）",
                        _t2_confirmed, _t2_wait, len(repush_idx),
                    )
                for _i in round_terminal_failed_idx:
                    if _i not in _seen_idx:
                        _seen_idx.add(_i)
                        repush_idx.append(_i)

                for idx in repush_idx:
                    try:
                        result = await self._push_with_retry(
                            http_client, chunks[idx], tenant_id, kb_id,
                        )
                        # duplicated 且同步带回 doc_ids → 直接完成
                        if result.doc_ids:
                            ctx.collected_doc_ids.extend(result.doc_ids)
                            continue
                        track_ids[idx] = result.track_id
                        tid_to_idx[result.track_id] = idx
                        new_pending_ids.append(result.track_id)
                    except Exception as e:
                        if services.logger:
                            services.logger.warning(
                                f"PushChunksStage: chunk {idx} 超时/失败重推失败: {e}"
                            )
                        terminal_hard_failed.add(idx)
                if services.logger:
                    services.logger.info(
                        f"PushChunksStage: per-chunk 重试 round={retry_round}/"
                        f"{self._per_chunk_max_retries}, 重推 {len(repush_idx)} chunk "
                        f"(超时 {len(timeout_tids)} + 终态失败 {len(round_terminal_failed_idx)})"
                    )
                pending_ids = new_pending_ids

        # ── 4.5 Propagate dedup results ────────────────────────────
        # Content-identical chunks shared the first occurrence's track_id.
        # After polling, propagate track_ids and mark duplicates so they
        # count correctly in step 5.
        if dedup_map:
            for dup_idx, first_idx in dedup_map.items():
                if first_idx in track_ids:
                    track_ids[dup_idx] = track_ids[first_idx]
                    duplicated_indices.add(dup_idx)
                elif first_idx in failed_indices:
                    # First occurrence failed to push → duplicate also counts as failed
                    failed_indices.add(dup_idx)
            if services.logger:
                services.logger.info(
                    "PushChunksStage: dedup propagated %d duplicate chunk(s)",
                    len(dedup_map),
                )

        # ── 5. Determine result (classified) ─────────────────────────
        # [jonex] 阶段4：reparse_strict 走任务级严格推送（全量成功才算成功），
        # 不依赖全局 RAG_REQUIRE_DOC_IDS 环境开关。
        require_doc_ids = self._require_doc_ids or bool(
            (getattr(ctx, "config_snapshot", None) or {}).get("strict_push")
        )
        # [jonex] #5: duplicated / pushed counts
        total_pushed = len(track_ids)
        duplicated_count = len(duplicated_indices)
        # [jonex] #6: hard_failed = push exceptions + terminal failed
        hard_failed = failed_indices | terminal_hard_failed
        # [jonex] #6: timed_out = has track_id, still pending, not hard_failed
        timed_out: set[int] = set()
        for idx, tid in track_ids.items():
            if tid in ctx.pending_track_ids and idx not in hard_failed:
                timed_out.add(idx)

        # [jonex] ②（治本）：strict 下超时确认前，按内容 doc_id 复查真实状态。
        # 超大文档长尾 chunk 常被 per-chunk 窗口误判超时、重推变 dup 又不返回
        # completed，但其内容其实已在 LightRAG processed（后台抽取完成，仅 track
        # 确认滞后）。直接复算 doc_id 查该 doc：已 processed → 判为已确认（收集
        # doc_id、移出 timed_out），消除假 RAG_PUSH_TIMEOUT 触发的整单回滚。
        if require_doc_ids and timed_out:
            verified: set[int] = set()
            for idx in list(timed_out):
                try:
                    did = _expected_doc_id(chunks[idx])
                    st = await _query_doc_status(
                        http_client, did, tenant_id=tenant_id, kb_id=kb_id,
                    )
                except Exception:
                    st = None
                if st == "processed":
                    ctx.collected_doc_ids.append(did)
                    verified.add(idx)
            if verified:
                timed_out -= verified
                if services.logger:
                    services.logger.info(
                        "PushChunksStage: ② 超时确认复查——%d 个超时 chunk 实际已 "
                        "processed 判为已确认（内容已入库，仅 track 确认滞后）；"
                        "剩余未确认 %d",
                        len(verified), len(timed_out),
                    )

        confirmed_count = total_chunks - len(hard_failed) - len(timed_out)

        # Persist counters to ctx (for task_manager + KB reconciliation)
        ctx.total_chunk_count = total_chunks
        ctx.failed_chunk_count = len(hard_failed)
        ctx.timeout_chunk_count = len(timed_out)
        ctx.duplicated_chunk_count = duplicated_count
        ctx.total_pushed_count = total_pushed

        # [jonex] #6: strict confirmation when RAG_REQUIRE_DOC_IDS=true 或 reparse_strict
        if require_doc_ids:
            if hard_failed:
                first_reason = ""
                if failed_indices:
                    first_reason = f" (push failure on chunk {min(failed_indices)})"
                elif terminal_hard_failed:
                    first_reason = f" (track_status=failed on chunk {min(terminal_hard_failed)})"
                return StageResult(
                    error=(
                        f"LightRAG 入库部分失败：{len(hard_failed)}/{total_chunks} chunks 硬失败"
                        f"{first_reason}"
                    )
                )
            if timed_out:
                return StageResult(
                    error=(
                        f"RAG_PUSH_TIMEOUT: {len(timed_out)}/{total_chunks} chunks "
                        f"轮询超时未确认（{eff_track_timeout:.0f}s）"
                    )
                )

        # RAG_REQUIRE_DOC_IDS=false — preserved宽松semantics
        if confirmed_count == 0 and not ctx.collected_doc_ids:
            return StageResult(error=f"All {total_chunks} chunks failed to push")

        if services.logger:
            services.logger.info(
                f"PushChunksStage complete: {confirmed_count}/{total_chunks} confirmed, "
                f"{len(hard_failed)} hard_failed, "
                f"{len(timed_out)} timed_out, "
                f"{duplicated_count} duplicated, "
                f"{len(ctx.collected_doc_ids)} doc_ids collected"
            )

        return StageResult()

    # ── Chunk collection helpers ─────────────────────────────────────

    def _collect_text_chunks(
        self, chunks: list[dict], content_list: list[dict],
        tenant_id: str, kb_id: str, document_id: str, file_name: str,
        stats: dict | None = None,
    ) -> None:
        """Extract text + table chunks from MinerU content_list.

        [jonex] §table-chunking: tables are split into row-level chunks
        (each fitting within the budget, with header repetition) instead
        of being silently truncated at the budget boundary.

        [jonex] §table-grid-v2 O4: *stats* (``ctx.table_stats``) collects
        observability counters — ``tables_total`` / ``tables_normalized`` /
        ``tables_fallback`` / ``rows_total`` / ``cols_unnamed`` /
        ``oversize_table_chunks`` / ``header_levels`` 分布 — surfaced in
        the task result summary.
        """
        stats = stats if stats is not None else {}

        # ── [jonex] §block-packing 阶段 A：caption 预解析（必须严格早于
        # 打包与噪声丢弃——L4.1 的 prev_block 判据（≤60 字短块）在打包后
        # 会被合并成大包或丢弃，见方案 §5.1）。判据、入参、返回值与现状
        # 完全一致，只是把调用时机提前；输入是未经修改的原始 content_list。
        # 仅打包开启时执行：开关关闭走 1:1 原路径，表格分支现场解析即可
        # （结果一致），避免向 content_list 写入 _resolved_caption /
        # _caption_source 污染原数据（review 低优先）。
        if self._text_pack_enabled and _table_grid_v2_enabled():
            self._prefill_table_captions(content_list, file_name)

        if not self._text_pack_enabled:
            # ── 打包开关关闭：原逐块 1:1 路径，行为与改造前完全一致
            # （灰度回退通道，见方案 §6）。
            for item_idx, item in enumerate(content_list):
                t = item.get("type", "text")

                if t == "text":
                    text = item.get("text", "")
                    if not text or not text.strip():
                        continue
                    # [jonex] §table-grid-v2 O2: 内嵌表格探测——md 文档里的
                    # HTML/管道表格混在 text block 里，从未走表格分支，被
                    # LightRAG 按 token 从标签中间硬切。拆段后表格片段走
                    # normalize_table_grid 表格分支；前后文本单独成 chunk
                    # （同时是 L4.1 表标题的 prev_block 候选）。
                    embedded = (
                        extract_embedded_tables(text)
                        if _table_grid_v2_enabled()
                        else [{"kind": "text", "text": text}]
                    )
                    if len(embedded) > 1:
                        embedded_table_idx = item.get("table_idx", 0)
                        for seg in embedded:
                            if seg["kind"] == "table":
                                self._emit_table_chunks(
                                    chunks, stats, item, seg["text"],
                                    tenant_id, kb_id, document_id, file_name,
                                    table_idx_override=embedded_table_idx,
                                    content_list=content_list,
                                    item_idx=item_idx,
                                )
                                embedded_table_idx += 1
                            elif seg["text"] and seg["text"].strip():
                                # 拆段后字符偏移不再精确，锚点只保留 page
                                self._emit_text_chunks(
                                    chunks, item, seg["text"],
                                    tenant_id, kb_id, document_id, file_name,
                                    ctype="text", with_char_offsets=False,
                                )
                        continue
                    self._emit_text_chunks(
                        chunks, item, text,
                        tenant_id, kb_id, document_id, file_name,
                        ctype=t,
                    )
                elif t == "table":
                    self._emit_table_chunks(
                        chunks, stats, item, get_table_body(item),
                        tenant_id, kb_id, document_id, file_name,
                        content_list=content_list, item_idx=item_idx,
                    )
                else:
                    continue
            return

        # ── [jonex] §block-packing 阶段 B/C：文本块打包 + 硬边界冲刷。
        # 全文档短块（≤20 字）出现次数预扫一次，供 C3 高重复页眉页脚
        # 判定（阈值 ≥3，见方案 §8）。
        repeat_index: dict[str, int] = {}
        for item in content_list:
            if item.get("type") != "text":
                continue
            t = (item.get("text") or "").strip()
            if 0 < len(t) <= 20:
                repeat_index[t] = repeat_index.get(t, 0) + 1

        # [jonex] §block-packing review P1：包头状态（cur_heads/head_pages/
        # heads_fresh）外提为方法局部状态，经 carry 在多次 pack_text_blocks
        # 调用间传递——标题跨 table/image/equation 硬边界继承，硬边界后的
        # 首个文本包同样带【章 / 节】包头。
        pack_carry: dict = {}
        pending: list[dict] = []
        for item_idx, item in enumerate(content_list):
            t = item.get("type", "text")

            if t == "text":
                text = item.get("text", "")
                if not text or not text.strip():
                    continue
                embedded = (
                    extract_embedded_tables(text)
                    if _table_grid_v2_enabled()
                    else [{"kind": "text", "text": text}]
                )
                if len(embedded) > 1:
                    # O2 内嵌表格：表段是硬边界——先冲刷 pending 再走表格
                    # 分支；拆出的文本段只有 page 锚点（§5.2 降级态），
                    # 按块进 pending 参与打包。
                    embedded_table_idx = item.get("table_idx", 0)
                    for seg in embedded:
                        if seg["kind"] == "table":
                            self._flush_text_pack(
                                chunks, pending, tenant_id, kb_id,
                                document_id, file_name, stats, repeat_index,
                                pack_carry,
                            )
                            pending = []
                            self._emit_table_chunks(
                                chunks, stats, item, seg["text"],
                                tenant_id, kb_id, document_id, file_name,
                                table_idx_override=embedded_table_idx,
                                content_list=content_list,
                                item_idx=item_idx,
                            )
                            embedded_table_idx += 1
                        elif seg["text"] and seg["text"].strip():
                            pending.append({
                                "text": seg["text"],
                                "page_idx": item.get("page_idx"),
                            })
                    continue
                pending.append(item)
            elif t == "table":
                # 硬边界：先冲刷文本包再走既有表格分支
                self._flush_text_pack(
                    chunks, pending, tenant_id, kb_id, document_id,
                    file_name, stats, repeat_index, pack_carry,
                )
                pending = []
                self._emit_table_chunks(
                    chunks, stats, item, get_table_body(item),
                    tenant_id, kb_id, document_id, file_name,
                    content_list=content_list, item_idx=item_idx,
                )
            else:
                # image/equation 等：不产出 chunk，但作为硬边界打断
                # 相邻文本块的打包（方案 §3.2 C1）。
                self._flush_text_pack(
                    chunks, pending, tenant_id, kb_id, document_id,
                    file_name, stats, repeat_index, pack_carry,
                )
                pending = []
        self._flush_text_pack(
            chunks, pending, tenant_id, kb_id, document_id,
            file_name, stats, repeat_index, pack_carry,
        )

    def _prefill_table_captions(
        self, content_list: list[dict], file_name: str,
    ) -> None:
        """[jonex] §block-packing 阶段 A：在原始块序上预先解析表格 caption。

        打包/噪声丢弃会改变表格前的短文本块形态，L4.1 的 prev_block 判据
        （≤60 字短块）在打包后必然失效（方案 §5.1）。预解析只搬移时机：
        调用现有 _resolve_table_caption、判据常量一个不改、输入是未经任何
        修改的原始 content_list，因此输出与打包功能上线前逐字节相同。
        """
        for idx, item in enumerate(content_list):
            t = item.get("type", "text")
            if t == "table":
                cap, src = _resolve_table_caption(
                    item, content_list, idx, file_name,
                )
                item["_resolved_caption"] = cap
                item["_caption_source"] = src
            elif t == "text" and len(extract_embedded_tables(
                item.get("text", "")
            )) > 1:
                # O2 内嵌表格：text 块含表格片段的同样按其 idx 预解析
                cap, src = _resolve_table_caption(
                    item, content_list, idx, file_name,
                )
                item["_resolved_caption"] = cap
                item["_caption_source"] = src

    def _flush_text_pack(
        self, chunks: list[dict], blocks: list[dict],
        tenant_id: str, kb_id: str, document_id: str, file_name: str,
        stats: dict, repeat_index: dict[str, int],
        carry: dict | None = None,
    ) -> None:
        """[jonex] §block-packing: pending 文本块 → pack_text_blocks →
        逐包 _emit_text_chunks(pack=...)，同时写观测计数器（方案 §4 改动 2e）。

        *carry* 为跨硬边界的包头状态（review P1），由 _collect_text_chunks
        持有、每次调用传回并在 pack_text_blocks 内原地更新；同时承载
        「每文档前 20 个被丢弃块采样」进度（noise_sampled 键，§3.2）。
        """
        if not blocks:
            return
        stats["blocks_total"] = stats.get("blocks_total", 0) + len(blocks)
        packs, dropped = pack_text_blocks(
            blocks, self._text_pack_budget,
            heading_max_len=self._text_pack_heading_max_len,
            drop_noise=self._text_pack_drop_noise,
            repeat_index=repeat_index,
            carry=carry,
        )
        if dropped:
            stats["blocks_dropped_noise"] = (
                stats.get("blocks_dropped_noise", 0) + len(dropped)
            )
            # [jonex] §block-packing 3.2: 每文档记录前 20 个被丢弃块的原文
            # 与命中判据（§6 P1 灰度门槛人工核对用；pack_text_blocks 以
            # (text, reason) 对返回）。
            sampled = 0
            if carry is not None:
                sampled = carry.get("noise_sampled", 0)
            to_sample = dropped[: max(0, 20 - sampled)]
            if to_sample:
                logger.warning(
                    "[jonex] §block-packing: %d noise block(s) dropped from "
                    "%s — sample(first %d): %r",
                    len(dropped), file_name, len(to_sample),
                    [(t.replace("\n", "\\n")[:60], r) for t, r in to_sample],
                )
                if carry is not None:
                    carry["noise_sampled"] = sampled + len(to_sample)
        if packs:
            old_n = stats.get("packs_total", 0)
            new_n = old_n + len(packs)
            stats["packs_total"] = new_n
            stats["blocks_packed"] = stats.get("blocks_packed", 0) + sum(
                p["block_count"] for p in packs
            )
            stats["packs_cross_page"] = stats.get("packs_cross_page", 0) + sum(
                1 for p in packs if p.get("pspans")
            )
            pack_chars = sum(len(p["text"]) for p in packs)
            old_avg = stats.get("avg_pack_chars", 0)
            stats["avg_pack_chars"] = round(
                (old_avg * old_n + pack_chars) / new_n
            )
        for pack in packs:
            # item 传 {}：打包路径锚点全部取自 pack 元数据，item 不消费
            # （review 低优先，避免把 pack 本体当 item 引起误读）。
            self._emit_text_chunks(
                chunks, {}, pack["text"], tenant_id, kb_id,
                document_id, file_name, ctype="text", pack=pack, stats=stats,
            )

    def _emit_table_chunks(
        self, chunks: list[dict], stats: dict, item: dict,
        raw_body, tenant_id: str, kb_id: str, document_id: str,
        file_name: str, table_idx_override: int | None = None,
        content_list: list[dict] | None = None,
        item_idx: int = 0,
    ) -> None:
        """[jonex] §table-grid-v2: table → row-level chunks.

        Shared by the ``type=="table"`` content branch and O2 embedded-table
        segments (which pass ``table_idx_override``).  On normalization
        failure the raw body falls back to plain-text emission with a
        WARNING + ``tables_fallback`` counter (O4 — no more silent
        degradation).

        [jonex] §table-grid-v2 L4.1: resolves the table caption via
        ``_resolve_table_caption`` (4-level source chain) and passes it to
        ``pack_rows`` (one-line ``【表】`` context header); column signature /
        list / notes ride in ``file_source`` (table_sig/table_cols/notes),
        not in the body.
        """
        # [jonex] §table-grid-v2 O4: 表格项计数（含 fallback 与 col_N
        # 占位可观测——此前 normalize 失败静默降级，无人知晓）。
        stats["tables_total"] = stats.get("tables_total", 0) + 1
        # [jonex] §table-grid-v2 L1/L2: 默认走网格化 + 表头推断新路径；
        # RAG_TABLE_GRID_V2=false 回退旧 normalize_table_rows。
        # meta（header_levels/notes/n_cols）在 L4.1（步 10）写入
        # file_source 与 pack_rows 上下文头时消费。
        if _table_grid_v2_enabled():
            header, data_rows, meta = normalize_table_grid(raw_body)
        else:
            header, data_rows = normalize_table_rows(raw_body)
            meta = {}
        stats["cols_unnamed"] = stats.get("cols_unnamed", 0) + _count_col_unnamed(header)
        # L3 兜底轨命中数（Excel 序列号 → 日期），随 meta 透出
        stats["dates_heuristic"] = (
            stats.get("dates_heuristic", 0) + meta.get("dates_heuristic", 0)
        )
        # O4-bis: B 型错位（左移）嫌疑行数——col_ 归零后唯一能暴露
        # rowspan 标注不完整错位的可观测信号
        stats["suspect_left_shift"] = (
            stats.get("suspect_left_shift", 0)
            + meta.get("suspect_left_shift", 0)
        )
        # [jonex] §18.3 观测口径：表头层级分布（header_levels=0 即表头
        # 推断失败、全列 col_N 的根因信号）。仅统计 grid-v2 路径——
        # 旧 normalize_table_rows 无 meta，混入会污染 0 档语义。
        if _table_grid_v2_enabled():
            hl_counter = stats.setdefault("header_levels", {})
            hl = str(meta.get("header_levels", 0) or 0)
            hl_counter[hl] = hl_counter.get(hl, 0) + 1
        if data_rows:
            stats["tables_normalized"] = stats.get("tables_normalized", 0) + 1
            stats["rows_total"] = stats.get("rows_total", 0) + len(data_rows)
            # [jonex] §table-grid-v2 L4.1: 表标题 4 级来源链（有效性校验 +
            # 防误抓约束）；命中层级计入 table_stats.caption_source。
            # 列名清单/签名/表前说明走 file_source 旁路，不占正文 embedding。
            caption = None
            table_sig = None
            table_cols = None
            notes = None
            if _table_grid_v2_enabled():
                if "_resolved_caption" in item:
                    # [jonex] §block-packing 阶段 A 已预解析（打包前在
                    # 原始块序上解析，结果与现场解析一致）
                    caption = item["_resolved_caption"]
                    caption_source = item["_caption_source"]
                else:
                    caption, caption_source = _resolve_table_caption(
                        item, content_list or [], item_idx, file_name,
                    )
                src_counter = stats.setdefault("caption_source", {})
                src_counter[caption_source] = (
                    src_counter.get(caption_source, 0) + 1
                )
                table_sig = _table_signature(header)
                # 列名 join 用 unit separator（\x1f）：file_source 按 | 分割
                # 键值对，列名本身可能含空格/顿号，join 分隔符必须与键值
                # 分隔符（|）和可见标点都无冲突（O4-bis 修正，此前
                # "|".join + replace('|',' ') 在列名含空格时边界不可区分）。
                table_cols = "\x1f".join(header)
                notes = " ".join(meta.get("notes") or []) or None
            # [jonex] §table-grid-v2 O1: 表格专用预算（RAG_TABLE_CHUNK_MAX_CHARS）。
            # 表格行是语义原子，平台切分后不再被 LightRAG 二次硬切；
            # 文本链路仍用 _chunk_body_budget（RAG_CHUNK_MAX_CHARS-64）。
            row_segments = pack_rows(
                data_rows, self._table_chunk_body_budget, header,
                caption=caption,
            )
            for seg_idx, (seg_text, row_start, row_end) in enumerate(
                row_segments
            ):
                page = item.get("page_idx")
                table_idx = (
                    table_idx_override
                    if table_idx_override is not None
                    else item.get("table_idx")
                )
                # [jonex] §C1-bis: 单行超表格预算 → 按单元格边界切分，
                # 替代 _split_long_text 盲切（阈值此前误用文本链路的
                # 12000，且 _split_long_text 对无换行表格行退化为精确
                # 字符位截断，把单元格切碎）。pack_rows 单行超预算时
                # 恒为 1 行段（row_end - row_start == 1）。
                if (
                    len(seg_text) > self._table_chunk_max_chars
                    and row_end - row_start == 1
                ):
                    # [jonex] §C1-bis §19.6: 超长行观测——判断「数据形态
                    # （长文本备注列）」vs「T1 归一化缺陷（多行误合成一行）」。
                    stats["oversize_rows"] = (
                        stats.get("oversize_rows", 0) + 1
                    )
                    if stats["oversize_rows"] <= 5:
                        logger.warning(
                            "[jonex] §C1-bis oversize_rows sample "
                            "(table_idx=%s, row=%d, len=%d): %.200s",
                            table_idx, row_start, len(seg_text), seg_text,
                        )
                    # 与 pack_rows 同口径重算渲染上下文（§19.4：
                    # 列名有条件补、表标题无条件补）。
                    n_cols = max(len(r) for r in data_rows)
                    if header:
                        n_cols = max(n_cols, len(header))
                    use_markdown = n_cols <= DEFAULT_COLUMN_THRESHOLD
                    padded_header = list(header) + [
                        f"col_{i}" for i in range(len(header), n_cols)
                    ]
                    header_block = make_header_block(
                        padded_header, n_cols, use_markdown,
                    )
                    # §19.4 硬约束：continuation 传格式化字符串、
                    # 行号 1-based（row_start 是 0-based）。
                    cap_line = (
                        _render_caption_line(
                            caption, self._table_chunk_body_budget,
                            f"第 {row_start + 1} 行",
                        )
                        if caption
                        else ""
                    )
                    row_text = fmt_row(
                        data_rows[row_start], padded_header, n_cols,
                        use_markdown,
                    )
                    sub_rows = split_row_by_cells(
                        row_text, self._table_chunk_body_budget,
                        header_block=header_block, caption_line=cap_line,
                    )
                elif len(seg_text) > self._chunk_max_chars:
                    # 多行段超文本预算（O1 断言观测的异常形态）：
                    # 保留 _split_long_text 兜底，不按单元格边界切。
                    sub_rows = [
                        (t, None, None)
                        for t in _split_long_text(
                            seg_text, self._chunk_body_budget,
                        )
                    ]
                else:
                    sub_rows = [(seg_text, None, None)]

                for sub_idx, (sub_seg, cell_start, cell_end) in enumerate(
                    sub_rows
                ):
                    # [jonex] §C1-bis §19.3: cells_hard_cut——非尾段段末
                    # 不含 " | " 即发生字符硬切（正常按边界切的段末恒以
                    # 分隔符结尾；硬切点不可能恰好落在分隔符终点，无歧义）。
                    if (
                        sub_idx < len(sub_rows) - 1
                        and not sub_seg.endswith(" | ")
                    ):
                        stats["cells_hard_cut"] = (
                            stats.get("cells_hard_cut", 0) + 1
                        )
                    fs = _build_file_source(
                        tenant_id, kb_id, document_id,
                        file_name,
                        chunk_index=len(chunks),
                        page=page,
                        row_start=row_start,
                        row_end=row_end,
                        table_idx=table_idx,
                        ctype="table_row",
                        table_sig=table_sig,
                        table_cols=table_cols,
                        notes=notes,
                        # [jonex] 改动 20：表标题 heading 作为主体实体提示
                        # 入库（方案 C 通道，file_source ehint= 键）
                        entity_hint=caption,
                        cell_start=cell_start,
                        cell_end=cell_end,
                    )
                    text_for_upload = _inject_ns_token(
                        sub_seg, tenant_id, kb_id,
                        document_id,
                    )
                    # [jonex] §table-grid-v2 O1: 超限断言——用与
                    # LightRAG 二次切分同口径的 tokenizer 实测，超过
                    # CHUNK_SIZE 会被 LightRAG 硬切（表头/锚点错位），
                    # 打 WARNING 并计数，是「二次切分是否真的被消除」
                    # 的直接观测指标。
                    if self._table_tokenizer is not None:
                        try:
                            n_tokens = len(self._table_tokenizer.encode(
                                text_for_upload
                            ))
                        except Exception:
                            n_tokens = 0
                        if n_tokens > self._lightrag_chunk_size:
                            stats["oversize_table_chunks"] = (
                                stats.get("oversize_table_chunks", 0) + 1
                            )
                            logger.warning(
                                "[jonex] §table-grid-v2 O1: 表格 chunk "
                                "超过 LightRAG 二次切分阈值 "
                                "(%d > %d tokens, rows=%d:%d)，将触发硬切；"
                                "建议下调 RAG_TABLE_CHUNK_MAX_CHARS",
                                n_tokens, self._lightrag_chunk_size,
                                row_start, row_end,
                            )
                    chunks.append({
                        "text": text_for_upload,
                        "file_source": fs,
                        "type": "table_row",
                    })
            return  # Table handled via row-level chunks

        # [jonex] §table-grid-v2 O4: fallback 不再静默——打 WARNING
        # 并计数（此前 normalize 失败无告警无计数，问题被完全隐藏）。
        stats["tables_fallback"] = stats.get("tables_fallback", 0) + 1
        logger.warning(
            "[jonex] §table-grid-v2 O4: 表格归一化失败，回退原始文本 "
            "(tables_fallback=%d)",
            stats["tables_fallback"],
        )
        # Fallback: normalization produced nothing → treat raw
        # content (HTML or otherwise) as plain text.
        text = raw_body if isinstance(raw_body, str) else str(raw_body)
        self._emit_text_chunks(
            chunks, item, text, tenant_id, kb_id, document_id, file_name,
            ctype="table", table_idx=item.get("table_idx"),
        )

    def _emit_text_chunks(
        self, chunks: list[dict], item: dict, text: str,
        tenant_id: str, kb_id: str, document_id: str, file_name: str,
        ctype: str = "text", table_idx: int | None = None,
        with_char_offsets: bool = True,
        pack: dict | None = None,
        stats: dict | None = None,
    ) -> None:
        """Plain-text chunk emission with budget-aware splitting.

        [jonex] §table-chunking: replace silent truncation
        (was ``text[:self._chunk_max_chars]``) with explicit
        budget-aware splitting.  Over-budget blocks are split
        at newline boundaries and pushed as multiple chunks.

        [jonex] §block-packing 改动 2c: *pack* 存在时锚点（page / page_end /
        pspans / char / line 范围）从包元数据取而非单个 item；冲刷前
        tokenizer 实测断言（复用表格链路 tokenizer），超 _text_pack_max_tokens
        时按换算阈值 _split_long_text(text, int(max_tokens × 1.4)) 兜底切分
        ——必须显式传换算阈值：原路径传 _chunk_body_budget（11936 字符）会
        把断言架空。切出的续段只保留 page，不带 pspans。*pack* 为 None 时
        行为与改造前完全一致。
        """
        stats = stats if stats is not None else {}
        if pack is not None:
            # 打包路径：字符预算已在 pack_text_blocks 内控制，此处做
            # tokenizer 实测断言兜底（1260 字符 × 1.4 系数在 o200k_base
            # 下实测 ≈ 840 token，正常不应触发）。
            tokenizer = self._table_tokenizer
            over_budget = False
            if tokenizer is not None:
                try:
                    over_budget = (
                        len(tokenizer.encode(text)) > self._text_pack_max_tokens
                    )
                except Exception:  # noqa: BLE001 — 断言失败不能影响推送
                    over_budget = False
            if over_budget:
                logger.warning(
                    "[jonex] §block-packing: pack exceeds max_tokens "
                    "(len=%d chars > %d tokens), splitting at converted "
                    "char budget",
                    len(text), self._text_pack_max_tokens,
                )
                stats["oversize_text_chunks"] = (
                    stats.get("oversize_text_chunks", 0) + 1
                )
                text_segments = _split_long_text(
                    text, max_chars=int(self._text_pack_max_tokens * 1.4)
                )
            else:
                text_segments = [text]

            page = pack.get("page_start")
            page_end = pack.get("page_end")
            pspans = pack.get("pspans")
            line_start = pack.get("line_start")
            line_end = pack.get("line_end")
            char_start = pack.get("char_start")
            char_end = pack.get("char_end")
        else:
            if len(text) > self._chunk_max_chars:
                logger.warning(
                    "[jonex] §table-chunking: block exceeds max_chars "
                    "(type=%s, len=%d > max=%d), splitting into segments",
                    ctype, len(text), self._chunk_max_chars,
                )
                text_segments = _split_long_text(text, self._chunk_body_budget)
            else:
                text_segments = [text]

            page = item.get("page_idx")
            line_start = item.get("line_start")
            line_end = item.get("line_end")
            char_start = item.get("char_start")
            char_end = item.get("char_end")
            page_end = None
            pspans = None

        for seg_idx, seg in enumerate(text_segments):
            is_first = seg_idx == 0
            # Character offsets are only valid for the first segment;
            # downstream segments cannot compute meaningful offsets
            # from the truncated text.
            cs: int | None = None
            ce: int | None = None
            if is_first and with_char_offsets:
                cs = char_start
                ce = char_end
            if is_first:
                seg_page_end = page_end
                seg_pspans = pspans
                seg_line_start = line_start
                seg_line_end = line_end
            elif pack is not None:
                # 打包路径续段：锚点降级只保留 page（方案 2c）
                seg_page_end = None
                seg_pspans = None
                seg_line_start = None
                seg_line_end = None
            else:
                # 原路径续段：line 锚点沿用现状，仅 char 范围失效
                seg_page_end = None
                seg_pspans = None
                seg_line_start = line_start
                seg_line_end = line_end

            file_source = _build_file_source(
                tenant_id, kb_id, document_id, file_name,
                chunk_index=len(chunks),
                page=page,
                page_end=seg_page_end,
                pspans=seg_pspans,
                line_start=seg_line_start,
                line_end=seg_line_end,
                char_start=cs,
                char_end=ce,
                table_idx=table_idx,
                ctype=ctype,
                stats=stats,
            )
            text_for_upload = _inject_ns_token(
                seg, tenant_id, kb_id, document_id,
            )
            chunks.append({
                "text": text_for_upload,
                "file_source": file_source,
                "type": ctype,
            })

    def _collect_multimodal_chunks(
        self, chunks: list[dict], multimodal_results: list[dict],
        tenant_id: str, kb_id: str, document_id: str, file_name: str,
        asset_exts: dict[int, str] | None = None,
    ) -> None:
        """Extract image/audio/video description chunks from VLM results.

        For video: pushes the MapReduce summary + per-frame VLM descriptions
        (with timestamps) + per-segment ASR transcripts as individual chunks.
        For audio: pushes the summary + per-segment ASR transcripts.
        """
        for item in multimodal_results:
            content_type = item.get("content_type", item.get("type", "image"))
            item_info = item.get("item_info", {})
            original = item.get("original", {}) or {}
            page = item_info.get("page_idx")
            if page is None:
                page = original.get("page_idx")   # [jonex] §12 MinerU 顶层 page_idx 兜底
            image_idx = item.get("index")

            # ── 1. Main summary chunk (MapReduce output) ──────────
            description = item.get("description", "")
            # [jonex] §table-grid-v2 L4.2 纵深防御：LLM 摘要响应解析偶发返回
            # 嵌套 dict（如 detailed_description 被输出成子对象）——此处兜底
            # 序列化，避免 'dict' object has no attribute 'strip' 炸掉整条
            # pipeline。根修在 TableModalProcessor._parse_table_response。
            if isinstance(description, dict):
                description = json.dumps(description, ensure_ascii=False)
            if description and isinstance(description, str) and description.strip():
                start_time = _first_present(item, item_info, "start_time")
                end_time = _first_present(item, item_info, "end_time")
                # 若 item 层无时间数据，从 _audio_segments 推导整个视频的时间范围
                if start_time is None and end_time is None:
                    segs = original.get("_audio_segments") or []
                    if segs:
                        start_time = _first_present(segs[0], {}, "start_time", "start")
                        end_time = _first_present(segs[-1], {}, "end_time", "end")
                file_source = _build_file_source(
                    tenant_id, kb_id, document_id, file_name,
                    chunk_index=len(chunks),
                    page=page,
                    image_idx=image_idx if content_type == "image" else None,
                    # [jonex] §image-refs P1-3: 资产上传成功才写 aext=
                    # （上传失败的图片不写，检索侧据此判定无 URL 可取）
                    asset_ext=(
                        (asset_exts or {}).get(image_idx)
                        if content_type == "image"
                        else None
                    ),
                    start_time=start_time,
                    end_time=end_time,
                    # 表格摘要的 ctype 用 table_summary（与明细行 table_row 区分，
                    # 供检索侧双路召回使用）；其余模态沿用 content_type
                    ctype=("table_summary" if content_type == "table" else content_type),
                )
                # [jonex] §table-grid-v2 L4.2: 摘要正文前缀「表格概览：」，
                # 与明细行的「表格明细：【表】」在 embedding 空间区分开
                # （与 ctype 双保险：前缀负责向量层面、ctype 负责过滤层面）。
                summary_text = description
                if content_type == "table":
                    summary_text = f"表格概览：{description}"
                elif content_type == "image":
                    # [jonex] §image-refs P0-5: 图片描述正文前缀，与表格摘要的
                    # 「表格概览：」同构——让图片 chunk 在 embedding 空间与
                    # 普通文本 chunk 可区分（与 ctype 双保险：前缀负责向量
                    # 层面、ctype 负责过滤层面）。
                    summary_text = f"图片描述：{description}"
                chunks.append({
                    "text": _inject_ns_token(summary_text, tenant_id, kb_id, document_id),
                    "file_source": file_source,
                    "type": content_type,
                })

            # ── 2. Per-frame VLM description chunks (video) ──────
            keyframes = original.get("_video_keyframes", []) or []
            for fi, frame in enumerate(keyframes):
                frame_desc = frame.get("scene_description") or frame.get("description", "")
                if not frame_desc or not frame_desc.strip():
                    continue
                frame_time = frame.get("frame_time", 0.0)
                fs = _build_file_source(
                    tenant_id, kb_id, document_id, file_name,
                    chunk_index=len(chunks),
                    start_time=frame_time,
                    end_time=frame_time,
                    ctype="video_frame",
                )
                chunks.append({
                    "text": _inject_ns_token(
                        f"[frame @ {frame_time:.1f}s] {frame_desc}",
                        tenant_id, kb_id, document_id,
                    ),
                    "file_source": fs,
                    "type": "video_frame",
                })

            # ── 3. Per-segment ASR transcript chunks (audio/video) ──
            segments = original.get("_audio_segments", []) or []
            for seg in segments:
                seg_text = seg.get("text", "")
                if not seg_text or not seg_text.strip():
                    continue
                # 兼容 start/end 和 start_time/end_time 两种字段名
                s_start = _first_present(seg, {}, "start_time", "start")
                s_end = _first_present(seg, {}, "end_time", "end")
                if s_start is None:
                    s_start = 0.0
                if s_end is None:
                    s_end = 0.0
                s_start = float(s_start) if s_start is not None else 0.0
                s_end = float(s_end) if s_end is not None else 0.0
                fs = _build_file_source(
                    tenant_id, kb_id, document_id, file_name,
                    chunk_index=len(chunks),
                    start_time=s_start,
                    end_time=s_end,
                    ctype="audio_segment",
                )
                chunks.append({
                    "text": _inject_ns_token(seg_text, tenant_id, kb_id, document_id),
                    "file_source": fs,
                    "type": "audio_segment",
                })

    # ── Retry logic ──────────────────────────────────────────────────

    async def _push_with_retry(
        self, http_client: Any, chunk: dict,
        tenant_id: str, kb_id: str,
    ) -> Any:
        """Push a single chunk to :9621 with retry on transient errors.

        Non-retryable errors (4xx) propagate immediately.
        Transient errors (timeout, 5xx, connection) are retried up to 3×.
        """
        last_exc: Exception | None = None

        for attempt in range(1, self._retry_max + 1):
            try:
                result = await http_client.upload_text(
                    text=chunk["text"],
                    file_source=chunk["file_source"],
                    tenant_id=tenant_id,
                    kb_id=kb_id,
                )
                return result
            except Exception as e:
                last_exc = e
                # 4xx → not retryable
                if isinstance(e, LightRAGError) and 400 <= getattr(e, 'code', 500) < 500:
                    raise
                if attempt < self._retry_max:
                    backoff = self._retry_base ** attempt  # 2, 4, 8
                    await asyncio.sleep(backoff)

        raise last_exc  # type: ignore[misc]
