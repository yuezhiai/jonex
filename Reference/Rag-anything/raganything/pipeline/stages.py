"""Pipeline stage implementations."""

import asyncio
import hashlib
import inspect
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
    extract_text_metadata,
    get_processor_for_type,
    get_table_body,
    insert_text_content,
    insert_text_content_with_multimodal_content,
    normalize_table_rows,
    pack_rows,
    separate_content,
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
    row_start: int | None = None,
    row_end: int | None = None,
    start_time: float | None = None,
    end_time: float | None = None,
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

    Extra fields (table_idx, image_idx, row_start, row_end) are appended as
    ``key=value`` pairs — ``parse_file_source()`` ignores unknown keys
    silently, so these are backward-compatible.
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
    if row_start is not None and row_end is not None:
        parts.append(f"row_start={row_start}")
        parts.append(f"row_end={row_end}")
    if start_time is not None and end_time is not None:
        parts.append(f"tstart={start_time:.3f}")
        parts.append(f"tend={end_time:.3f}")
    if table_idx is not None:
        parts.append(f"table_idx={table_idx}")
    if image_idx is not None:
        parts.append(f"image_idx={image_idx}")
    parts.append(f"trace={trace_id}")
    return "|".join(parts)


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
        self._collect_text_chunks(chunks, ctx.content_list or [], tenant_id, kb_id,
                                  document_id, file_name)
        self._collect_multimodal_chunks(chunks, ctx.multimodal_results or [],
                                        tenant_id, kb_id, document_id, file_name)

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
    ) -> None:
        """Extract text + table chunks from MinerU content_list.

        [jonex] §table-chunking: tables are split into row-level chunks
        (each fitting within the budget, with header repetition) instead
        of being silently truncated at the budget boundary.
        """
        for item in content_list:
            t = item.get("type", "text")

            if t == "text":
                text = item.get("text", "")
            elif t == "table":
                # [jonex] §table-chunking: normalize HTML / list / markdown
                # tables into structured rows, then split into budget-sized
                # segments with header repetition per segment.
                raw_body = get_table_body(item)
                header, data_rows = normalize_table_rows(raw_body)
                if data_rows:
                    # [jonex] §table-chunking P2: reserve ~64 chars for
                    row_segments = pack_rows(
                        data_rows, self._chunk_body_budget, header,
                    )
                    for seg_idx, (seg_text, row_start, row_end) in enumerate(
                        row_segments
                    ):
                        # [jonex] §table-chunking P1-1: single oversize
                        # row → _split_long_text so the segment still
                        # fits within budget.
                        if len(seg_text) > self._chunk_max_chars:
                            logger.warning(
                                "[jonex] §table-chunking: table row segment "
                                "exceeds budget (rows=%d:%d, len=%d > max=%d), "
                                "splitting",
                                row_start, row_end, len(seg_text),
                                self._chunk_max_chars,
                            )
                            sub_segs = _split_long_text(
                                seg_text, self._chunk_body_budget,
                            )
                        else:
                            sub_segs = [seg_text]

                        for sub_seg in sub_segs:
                            page = item.get("page_idx")
                            table_idx = item.get("table_idx")
                            fs = _build_file_source(
                                tenant_id, kb_id, document_id,
                                file_name,
                                chunk_index=len(chunks),
                                page=page,
                                row_start=row_start,
                                row_end=row_end,
                                table_idx=table_idx,
                            )
                            text_for_upload = _inject_ns_token(
                                sub_seg, tenant_id, kb_id,
                                document_id,
                            )
                            chunks.append({
                                "text": text_for_upload,
                                "file_source": fs,
                                "type": "table_row",
                            })
                    continue  # Table handled via row-level chunks

                # Fallback: normalization produced nothing → treat raw
                # content (HTML or otherwise) as plain text below.
                text = (
                    raw_body if isinstance(raw_body, str)
                    else str(raw_body)
                )
            else:
                continue

            if not text or not text.strip():
                continue

            # [jonex] §table-chunking: replace silent truncation
            # (was ``text[:self._chunk_max_chars]``) with explicit
            # budget-aware splitting.  Over-budget blocks are split
            # at newline boundaries and pushed as multiple chunks.
            if len(text) > self._chunk_max_chars:
                logger.warning(
                    "[jonex] §table-chunking: block exceeds max_chars "
                    "(type=%s, len=%d > max=%d), splitting into segments",
                    t, len(text), self._chunk_max_chars,
                )
                text_segments = _split_long_text(text, self._chunk_body_budget)
            else:
                text_segments = [text]

            page = item.get("page_idx")
            line_start = item.get("line_start")
            line_end = item.get("line_end")
            char_start = item.get("char_start")
            char_end = item.get("char_end")
            table_idx = item.get("table_idx")

            for seg_idx, seg in enumerate(text_segments):
                # Character offsets are only valid for the first segment;
                # downstream segments cannot compute meaningful offsets
                # from the truncated text.
                cs: int | None = None
                ce: int | None = None
                if seg_idx == 0:
                    cs = char_start
                    ce = char_end

                file_source = _build_file_source(
                    tenant_id, kb_id, document_id, file_name,
                    chunk_index=len(chunks),
                    page=page,
                    line_start=line_start,
                    line_end=line_end,
                    char_start=cs,
                    char_end=ce,
                    table_idx=table_idx if t == "table" else None,
                )
                text_for_upload = _inject_ns_token(
                    seg, tenant_id, kb_id, document_id,
                )
                chunks.append({
                    "text": text_for_upload,
                    "file_source": file_source,
                    "type": t,
                })

    def _collect_multimodal_chunks(
        self, chunks: list[dict], multimodal_results: list[dict],
        tenant_id: str, kb_id: str, document_id: str, file_name: str,
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
            if description and description.strip():
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
                    start_time=start_time,
                    end_time=end_time,
                )
                chunks.append({
                    "text": _inject_ns_token(description, tenant_id, kb_id, document_id),
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
