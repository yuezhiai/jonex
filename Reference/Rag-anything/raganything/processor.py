"""
Document processing functionality for RAGAnything

Backward-compatibility mixin.  New code should use the Pipeline
(DocumentPipeline + stages) directly.  This mixin is kept so that
``RAGAnything(QueryMixin, BatchMixin, ProcessorMixin)`` continues
to provide ``insert_content_list``, ``parse_document``,
``process_document_complete_lightrag_api``, and doc-status query
methods as a convenience layer.
"""

import asyncio
import os
import time
from pathlib import Path
from typing import Any, Dict, List

from lightrag.kg.shared_storage import get_namespace_data, get_pipeline_status_lock
from lightrag.utils import compute_mdhash_id

from raganything.base import DocStatus
from raganything.doc_status import current_doc_status_timestamp
from raganything.parsers import MineruParser, MineruExecutionError, Parser, get_parser
from raganything.utils import (
    insert_text_content,
    insert_text_content_with_multimodal_content,
    separate_content,
)


class ProcessorMixin:
    """ProcessorMixin class containing document processing functionality for RAGAnything"""

    def _get_file_reference(self, file_path: str) -> str:
        """
        Get file reference based on use_full_path configuration.
        """
        if self.config.use_full_path:
            return str(file_path)
        else:
            return os.path.basename(file_path)

    # ── Doc-status helpers (still used by process_document_complete_lightrag_api) ──

    async def _ensure_doc_status_record(
        self,
        doc_id: str,
        file_path: str,
        *,
        scheme_name: str | None = None,
        status: DocStatus = DocStatus.READY,
    ) -> Dict[str, Any]:
        """Create a minimal doc_status entry when LightRAG has not created one yet."""
        current_doc_status = await self.lightrag.doc_status.get_by_id(doc_id)
        if current_doc_status:
            return current_doc_status

        timestamp = current_doc_status_timestamp()
        doc_status_payload: Dict[str, Any] = {
            "status": status,
            "content": "",
            "content_summary": "",
            "content_length": 0,
            "error_msg": "",
            "chunks_count": 0,
            "chunks_list": [],
            "created_at": timestamp,
            "updated_at": timestamp,
            "file_path": self._get_file_reference(file_path),
        }
        if scheme_name is not None:
            doc_status_payload["scheme_name"] = scheme_name

        await self.lightrag.doc_status.upsert({doc_id: doc_status_payload})
        await self.lightrag.doc_status.index_done_callback()
        return await self.lightrag.doc_status.get_by_id(doc_id) or doc_status_payload

    async def _upsert_doc_status(
        self,
        doc_id: str,
        file_path: str,
        *,
        scheme_name: str | None = None,
        **updates,
    ) -> Dict[str, Any]:
        """Merge doc_status updates while preserving any existing LightRAG fields."""
        current_doc_status = await self._ensure_doc_status_record(
            doc_id,
            file_path,
            scheme_name=scheme_name,
        )
        updated_doc_status = {
            **current_doc_status,
            **updates,
            "updated_at": current_doc_status_timestamp(),
        }
        await self.lightrag.doc_status.upsert({doc_id: updated_doc_status})
        await self.lightrag.doc_status.index_done_callback()
        return updated_doc_status

    # ── Parsing ──────────────────────────────────────────────────────────

    async def parse_document(
        self,
        file_path: str,
        output_dir: str = None,
        parse_method: str = None,
        display_stats: bool = None,
        **kwargs,
    ) -> tuple[List[Dict[str, Any]], str]:
        """
        Parse document with caching support.

        Delegates cache management to ``ParseCacheManager``.
        """
        from raganything.cache import ParseCacheManager

        if output_dir is None:
            output_dir = self.config.parser_output_dir
        if parse_method is None:
            parse_method = self.config.parse_method
        if display_stats is None:
            display_stats = self.config.display_content_stats

        self.logger.info(f"Starting document parsing: {file_path}")

        file_path = Path(file_path)
        if not file_path.exists():
            raise FileNotFoundError(f"File not found: {file_path}")

        callback_file = str(file_path)
        callback_manager = getattr(self, "callback_manager", None)
        parse_start_time = time.time()
        if callback_manager is not None:
            callback_manager.dispatch(
                "on_parse_start",
                file_path=callback_file,
                parser=self.config.parser,
            )

        # Check cache via ParseCacheManager
        parse_cache_mgr = (
            ParseCacheManager(self.parse_cache, self.config)
            if getattr(self, "parse_cache", None) is not None
            else None
        )
        cache_key = (
            ParseCacheManager.generate_cache_key(file_path, parse_method, self.config, **kwargs)
            if parse_cache_mgr
            else None
        )
        if parse_cache_mgr and cache_key:
            cached = await parse_cache_mgr.get_cached_result(
                cache_key, file_path, parse_method, **kwargs
            )
            if cached is not None:
                content_list, doc_id = cached
                self.logger.info(f"Using cached parsing result for: {file_path}")
                if display_stats:
                    self.logger.info(
                        f"* Total blocks in cached content_list: {len(content_list)}"
                    )
                if callback_manager is not None:
                    duration = time.time() - parse_start_time
                    callback_manager.dispatch(
                        "on_parse_complete",
                        file_path=callback_file,
                        content_blocks=len(content_list),
                        doc_id=doc_id,
                        duration_seconds=duration,
                    )
                return content_list, doc_id

        # Choose appropriate parsing method based on file extension
        ext = file_path.suffix.lower()

        try:
            doc_parser = getattr(self, "doc_parser", None)
            if doc_parser is None:
                doc_parser = get_parser(self.config.parser)
                self.doc_parser = doc_parser

            self.logger.info(
                f"Using {self.config.parser} parser with method: {parse_method}"
            )

            if ext in [".pdf"]:
                content_list = await asyncio.to_thread(
                    doc_parser.parse_pdf,
                    pdf_path=file_path,
                    output_dir=output_dir,
                    method=parse_method,
                    **kwargs,
                )
            elif ext in [
                ".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".tif", ".gif", ".webp",
            ]:
                try:
                    content_list = await asyncio.to_thread(
                        doc_parser.parse_image,
                        image_path=file_path,
                        output_dir=output_dir,
                        **kwargs,
                    )
                except NotImplementedError:
                    self.logger.warning(
                        f"{self.config.parser} parser doesn't support image parsing, falling back to MinerU"
                    )
                    content_list = await asyncio.to_thread(
                        MineruParser().parse_image,
                        image_path=file_path,
                        output_dir=output_dir,
                        **kwargs,
                    )
            elif ext in [".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx", ".html", ".htm", ".xhtml"]:
                content_list = await asyncio.to_thread(
                    doc_parser.parse_office_doc,
                    doc_path=file_path,
                    output_dir=output_dir,
                    **kwargs,
                )
            elif ext in Parser.AUDIO_FORMATS:
                content_list = await asyncio.to_thread(
                    doc_parser.parse_audio,
                    audio_path=file_path,
                    output_dir=output_dir,
                    **kwargs,
                )
            elif ext in Parser.VIDEO_FORMATS:
                content_list = await asyncio.to_thread(
                    doc_parser.parse_video,
                    video_path=file_path,
                    output_dir=output_dir,
                    **kwargs,
                )
            elif ext in Parser.TEXT_FORMATS:
                # [jonex] 纯文本（txt/md/json）本地解析，绕过 MinerU API——
                # MinerU online 服务端不支持这些类型（v4/file-urls/batch 拒绝），
                # 且纯文本无需 VLM。base.Parser.parse_text 按空行分段切块。
                # 此前走 else → doc_parser.parse_document → mineru_online 发 API
                # → "unsupported file type" 解析失败。
                content_list = await asyncio.to_thread(
                    doc_parser.parse_text,
                    file_path=file_path,
                    output_dir=output_dir,
                    **kwargs,
                )
            else:
                content_list = await asyncio.to_thread(
                    doc_parser.parse_document,
                    file_path=file_path,
                    method=parse_method,
                    output_dir=output_dir,
                    **kwargs,
                )

        except MineruExecutionError as e:
            self.logger.error(f"Mineru command failed: {e}")
            if callback_manager is not None:
                callback_manager.dispatch(
                    "on_parse_error",
                    file_path=callback_file,
                    error=e,
                    parser=self.config.parser,
                )
            raise
        except Exception as e:
            self.logger.error(
                f"Error during parsing with {self.config.parser} parser: {str(e)}"
            )
            if callback_manager is not None:
                callback_manager.dispatch(
                    "on_parse_error",
                    file_path=callback_file,
                    error=e,
                    parser=self.config.parser,
                )
            raise

        msg = f"Parsing {file_path} complete! Extracted {len(content_list)} content blocks"
        self.logger.info(msg)

        if len(content_list) == 0:
            raise ValueError("Parsing failed: No content was extracted")

        # Generate doc_id based on content
        doc_id = self._generate_content_based_doc_id(content_list)

        # Store result in cache
        if parse_cache_mgr and cache_key:
            await parse_cache_mgr.store_cached_result(
                cache_key, content_list, doc_id, file_path, parse_method, **kwargs
            )

        if display_stats:
            self.logger.info("\nContent Information:")
            self.logger.info(f"* Total blocks in content_list: {len(content_list)}")
            block_types: Dict[str, int] = {}
            for block in content_list:
                if isinstance(block, dict):
                    block_type = block.get("type", "unknown")
                    if isinstance(block_type, str):
                        block_types[block_type] = block_types.get(block_type, 0) + 1
            self.logger.info("* Content block types:")
            for block_type, count in block_types.items():
                self.logger.info(f"  - {block_type}: {count}")

        if callback_manager is not None:
            duration = time.time() - parse_start_time
            callback_manager.dispatch(
                "on_parse_complete",
                file_path=callback_file,
                content_blocks=len(content_list),
                doc_id=doc_id,
                duration_seconds=duration,
            )

        return content_list, doc_id

    # ── process_document_complete (overridden by RAGAnything → Pipeline) ──

    async def process_document_complete(
        self,
        file_path: str,
        output_dir: str = None,
        parse_method: str = None,
        display_stats: bool = None,
        split_by_character: str | None = None,
        split_by_character_only: bool = False,
        doc_id: str | None = None,
        file_name: str | None = None,
        **kwargs,
    ):
        """
        Complete document processing workflow.

        .. note::
           ``RAGAnything`` overrides this method with a Pipeline-based
           implementation.  This version is only used when ``ProcessorMixin``
           is instantiated directly.
        """
        callback_manager = getattr(self, "callback_manager", None)
        doc_start_time = time.time()
        stage = "parse"
        file_name = file_name or self._get_file_reference(file_path)

        try:
            init_result = await self._ensure_lightrag_initialized()
            if not init_result or not init_result.get("success"):
                raise RuntimeError(
                    f"LightRAG initialization failed: {(init_result or {}).get('error', 'unknown error')}"
                )

            if output_dir is None:
                output_dir = self.config.parser_output_dir
            if parse_method is None:
                parse_method = self.config.parse_method
            if display_stats is None:
                display_stats = self.config.display_content_stats

            self.logger.info(f"Starting complete document processing: {file_path}")

            content_list, content_based_doc_id = await self.parse_document(
                file_path, output_dir, parse_method, display_stats, **kwargs
            )

            if doc_id is None:
                doc_id = content_based_doc_id

            text_content, multimodal_items = separate_content(content_list)

            if not text_content.strip():
                await self._upsert_doc_status(
                    doc_id, file_name,
                    status=DocStatus.HANDLING, error_msg="",
                )

            stage = "text_insert"
            if text_content.strip():
                if callback_manager is not None:
                    callback_manager.dispatch(
                        "on_text_insert_start",
                        file_path=file_name, text_length=len(text_content), doc_id=doc_id,
                    )
                insert_start = time.time()
                await insert_text_content(
                    self.lightrag,
                    input=text_content,
                    file_paths=file_name,
                    split_by_character=split_by_character,
                    split_by_character_only=split_by_character_only,
                    ids=doc_id,
                )
                await self._upsert_doc_status(
                    doc_id, file_name,
                    status=DocStatus.HANDLING, error_msg="",
                )
                if callback_manager is not None:
                    callback_manager.dispatch(
                        "on_text_insert_complete",
                        file_path=file_name, duration_seconds=time.time() - insert_start, doc_id=doc_id,
                    )

            # Multimodal processing — delegate to pipeline if available
            stage = "multimodal"
            # Filter out _text_meta sentinel (always appended by separate_content)
            real_multimodal = [
                item for item in multimodal_items if "_text_meta" not in item
            ]
            if real_multimodal:
                if hasattr(self, "pipeline") and self.pipeline is not None:
                    from raganything.pipeline import PipelineContext, PipelineServices
                    ctx = PipelineContext(
                        file_path=str(file_path), file_name=file_name,
                        doc_id=doc_id, content_list=content_list,
                        multimodal_items=multimodal_items,
                    )
                    services = PipelineServices(
                        config=self.config, lightrag=self.lightrag,
                        doc_parser=self.doc_parser, logger=self.logger,
                        modal_processors=getattr(self, "modal_processors", {}),
                        callback_manager=callback_manager,
                    )
                    await self.pipeline.execute(ctx, services)
                else:
                    self.logger.info(
                        f"No pipeline available, skipping multimodal processing for {doc_id}"
                    )
            else:
                await self._mark_multimodal_processing_complete(doc_id)

        except Exception as exc:
            if doc_id is not None:
                try:
                    await self._upsert_doc_status(
                        doc_id, file_name,
                        status=DocStatus.FAILED, error_msg=str(exc),
                    )
                except Exception:
                    pass
            if callback_manager is not None:
                callback_manager.dispatch(
                    "on_document_error",
                    file_path=str(file_path), doc_id=doc_id, stage=stage, error=exc,
                )
            raise

        self.logger.info(f"Document {file_path} processing complete!")
        if callback_manager is not None:
            callback_manager.dispatch(
                "on_document_complete",
                file_path=str(file_path), doc_id=doc_id,
                duration_seconds=time.time() - doc_start_time,
            )

    # ── LightRAG API variant ────────────────────────────────────────────

    async def process_document_complete_lightrag_api(
        self,
        file_path: str,
        output_dir: str = None,
        parse_method: str = None,
        display_stats: bool = None,
        split_by_character: str | None = None,
        split_by_character_only: bool = False,
        doc_id: str | None = None,
        scheme_name: str | None = None,
        parser: str | None = None,
        **kwargs,
    ):
        """
        API exclusively for LightRAG calls: complete document processing workflow
        using ``insert_text_content_with_multimodal_content``.
        """
        file_name = self._get_file_reference(file_path)
        doc_pre_id = f"doc-pre-{file_name}"
        pipeline_status = None
        pipeline_status_lock = None
        current_doc_status = {}

        async def mark_initialization_failed(error_msg: str) -> None:
            lightrag = getattr(self, "lightrag", None)
            doc_status = getattr(lightrag, "doc_status", None)
            if doc_status is None:
                return
            try:
                existing_status = await doc_status.get_by_id(doc_pre_id)
                failed_status = {
                    "status": DocStatus.FAILED,
                    "content": "", "error_msg": error_msg,
                    "content_summary": "", "multimodal_content": [],
                    "scheme_name": scheme_name,
                    "content_length": 0, "created_at": "",
                    "updated_at": current_doc_status_timestamp(),
                    "file_path": file_name,
                }
                if existing_status:
                    failed_status = {
                        **existing_status,
                        "status": DocStatus.FAILED,
                        "error_msg": error_msg,
                        "updated_at": current_doc_status_timestamp(),
                    }
                await doc_status.upsert({doc_pre_id: failed_status})
                await doc_status.index_done_callback()
            except Exception:
                pass

        if parser:
            self.config.parser = parser

        try:
            result = await self._ensure_lightrag_initialized()
            if not result or not result.get("success"):
                error_msg = (result or {}).get("error", "unknown error")
                self.logger.error(
                    f"LightRAG initialization failed: {error_msg}; "
                    f"skipping document processing for {file_path}"
                )
                await mark_initialization_failed(str(error_msg))
                return False

            if output_dir is None:
                output_dir = self.config.parser_output_dir
            if parse_method is None:
                parse_method = self.config.parse_method
            if display_stats is None:
                display_stats = self.config.display_content_stats

            self.logger.info(f"Starting complete document processing: {file_path}")

            current_doc_status = await self.lightrag.doc_status.get_by_id(doc_pre_id)
            if not current_doc_status:
                await self.lightrag.doc_status.upsert(
                    {doc_pre_id: {
                        "status": DocStatus.READY, "content": "", "error_msg": "",
                        "content_summary": "", "multimodal_content": [],
                        "scheme_name": scheme_name, "content_length": 0,
                        "created_at": "", "updated_at": "", "file_path": file_name,
                    }}
                )
                current_doc_status = await self.lightrag.doc_status.get_by_id(doc_pre_id)

            pipeline_status = await get_namespace_data("pipeline_status")
            pipeline_status_lock = get_pipeline_status_lock()

            async with pipeline_status_lock:
                pipeline_status.update({"scan_disabled": True})
                pipeline_status["history_messages"].append("Now is not allowed to scan")

            await self.lightrag.doc_status.upsert(
                {doc_pre_id: {
                    **current_doc_status,
                    "status": DocStatus.HANDLING, "error_msg": "",
                }}
            )

            try:
                content_list, content_based_doc_id = await self.parse_document(
                    file_path, output_dir, parse_method, display_stats, **kwargs
                )
            except MineruExecutionError as e:
                error_message = "\n".join(str(m) for m in e.error_msg) if isinstance(e.error_msg, list) else str(e.error_msg)
                await self.lightrag.doc_status.upsert(
                    {doc_pre_id: {**current_doc_status, "status": DocStatus.FAILED, "error_msg": error_message}}
                )
                return False
            except Exception as e:
                await self.lightrag.doc_status.upsert(
                    {doc_pre_id: {**current_doc_status, "status": DocStatus.FAILED, "error_msg": str(e)}}
                )
                return False

            if doc_id is None:
                doc_id = content_based_doc_id

            await self._upsert_doc_status(
                doc_id, file_name, scheme_name=scheme_name,
                status=DocStatus.HANDLING, error_msg="",
            )

            text_content, multimodal_items = separate_content(content_list)

            if text_content.strip():
                await insert_text_content_with_multimodal_content(
                    self.lightrag,
                    input=text_content,
                    multimodal_content=multimodal_items,
                    file_paths=file_name,
                    split_by_character=split_by_character,
                    split_by_character_only=split_by_character_only,
                    ids=doc_id,
                    scheme_name=scheme_name,
                )

            self.logger.info(f"Document {file_path} processing completed successfully")
            return True

        except Exception as e:
            self.logger.error(f"Error processing document {file_path}: {str(e)}")
            await self.lightrag.doc_status.upsert(
                {doc_pre_id: {**current_doc_status, "status": DocStatus.FAILED, "error_msg": str(e)}}
            )
            await self.lightrag.doc_status.index_done_callback()

            if pipeline_status_lock and pipeline_status:
                try:
                    async with pipeline_status_lock:
                        pipeline_status.update({"scan_disabled": False})
                        pipeline_status["latest_message"] = f"RAGAnything processing failed for {file_name}: {str(e)}"
                        pipeline_status["history_messages"].append(
                            f"RAGAnything processing failed for {file_name}: {str(e)}"
                        )
                        pipeline_status["history_messages"].append("Now is allowed to scan")
                except Exception:
                    pass
            return False

        finally:
            if pipeline_status_lock is not None and pipeline_status is not None:
                try:
                    async with pipeline_status_lock:
                        pipeline_status.update({"scan_disabled": False})
                        pipeline_status["latest_message"] = f"RAGAnything processing completed for {file_name}"
                        pipeline_status["history_messages"].append(
                            f"RAGAnything processing completed for {file_name}"
                        )
                        pipeline_status["history_messages"].append("Now is allowed to scan")
                except Exception:
                    pass

    # ── insert_content_list (bypass parser) ───────────────────────────────

    async def insert_content_list(
        self,
        content_list: List[Dict[str, Any]],
        file_path: str = "unknown_document",
        split_by_character: str | None = None,
        split_by_character_only: bool = False,
        doc_id: str | None = None,
        display_stats: bool = None,
    ):
        """Insert content list directly without document parsing."""
        callback_manager = getattr(self, "callback_manager", None)
        doc_start_time = time.time()

        init_result = await self._ensure_lightrag_initialized()
        if not init_result or not init_result.get("success"):
            raise RuntimeError(
                f"LightRAG initialization failed: {(init_result or {}).get('error', 'unknown error')}"
            )

        if display_stats is None:
            display_stats = self.config.display_content_stats

        self.logger.info(
            f"Starting direct content list insertion for: {file_path} ({len(content_list)} items)"
        )

        if doc_id is None:
            doc_id = self._generate_content_based_doc_id(content_list)

        file_ref = self._get_file_reference(file_path)

        if display_stats:
            self.logger.info("\nContent Information:")
            self.logger.info(f"* Total blocks in content_list: {len(content_list)}")
            block_types: Dict[str, int] = {}
            for block in content_list:
                if isinstance(block, dict):
                    block_type = block.get("type", "unknown")
                    if isinstance(block_type, str):
                        block_types[block_type] = block_types.get(block_type, 0) + 1
            self.logger.info("* Content block types:")
            for block_type, count in block_types.items():
                self.logger.info(f"  - {block_type}: {count}")

        text_content, multimodal_items = separate_content(content_list)

        if not text_content.strip():
            await self._upsert_doc_status(
                doc_id, file_ref,
                status=DocStatus.HANDLING, error_msg="",
            )

        if text_content.strip():
            if callback_manager is not None:
                callback_manager.dispatch(
                    "on_text_insert_start",
                    file_path=file_ref, text_length=len(text_content), doc_id=doc_id,
                )
            insert_start = time.time()
            await insert_text_content(
                self.lightrag,
                input=text_content,
                file_paths=file_ref,
                split_by_character=split_by_character,
                split_by_character_only=split_by_character_only,
                ids=doc_id,
            )
            await self._upsert_doc_status(
                doc_id, file_ref,
                status=DocStatus.HANDLING, error_msg="",
            )
            if callback_manager is not None:
                callback_manager.dispatch(
                    "on_text_insert_complete",
                    file_path=file_ref, duration_seconds=time.time() - insert_start, doc_id=doc_id,
                )

        # Multimodal — delegate to pipeline if available
        # Filter out _text_meta sentinel (always appended by separate_content)
        real_multimodal = [
            item for item in multimodal_items if "_text_meta" not in item
        ]
        if real_multimodal:
            if hasattr(self, "pipeline") and self.pipeline is not None:
                from raganything.pipeline import PipelineContext, PipelineServices
                ctx = PipelineContext(
                    file_path=file_path, file_name=file_ref,
                    doc_id=doc_id, content_list=content_list,
                    multimodal_items=multimodal_items,
                )
                services = PipelineServices(
                    config=self.config, lightrag=self.lightrag,
                    doc_parser=self.doc_parser, logger=self.logger,
                    modal_processors=getattr(self, "modal_processors", {}),
                    callback_manager=callback_manager,
                )
                await self.pipeline.execute(ctx, services)
            else:
                self.logger.info(
                    f"No pipeline available, skipping multimodal processing for {doc_id}"
                )
        else:
            await self._mark_multimodal_processing_complete(doc_id)

        self.logger.info(f"Content list insertion complete for: {file_path}")
        if callback_manager is not None:
            callback_manager.dispatch(
                "on_document_complete",
                file_path=file_path, doc_id=doc_id,
                duration_seconds=time.time() - doc_start_time,
            )

    # ── Helper: generate content-based doc_id ────────────────────────────

    def _generate_content_based_doc_id(self, content_list: List[Dict[str, Any]]) -> str:
        content_hash_data = []
        for item in content_list:
            if isinstance(item, dict):
                if item.get("type") == "text" and item.get("text"):
                    content_hash_data.append(item["text"].strip())
                elif item.get("type") == "image" and item.get("img_path"):
                    content_hash_data.append(f"image:{item['img_path']}")
                elif item.get("type") == "table" and item.get("table_body"):
                    content_hash_data.append(f"table:{item['table_body']}")
                elif item.get("type") == "equation" and item.get("text"):
                    content_hash_data.append(f"equation:{item['text']}")
                else:
                    content_hash_data.append(str(item))
        content_signature = "\n".join(content_hash_data)
        return compute_mdhash_id(content_signature, prefix="doc-")

    # ── Helper: mark multimodal complete ─────────────────────────────────

    async def _mark_multimodal_processing_complete(self, doc_id: str):
        """Mark multimodal content processing as complete."""
        try:
            current_doc_status = await self.lightrag.doc_status.get_by_id(doc_id)
            if current_doc_status:
                final_status = current_doc_status.get("status") or DocStatus.PROCESSED
                if final_status != DocStatus.FAILED:
                    final_status = DocStatus.PROCESSED
                update_payload = {
                    **current_doc_status,
                    "status": final_status,
                    "multimodal_processed": True,
                    "updated_at": current_doc_status_timestamp(),
                }
                try:
                    await self.lightrag.doc_status.upsert({doc_id: update_payload})
                except Exception as exc:
                    self.logger.debug(
                        "Falling back to schema-compatible doc_status update for %s: %s", doc_id, exc,
                    )
                    fallback_payload = {
                        **current_doc_status,
                        "status": final_status,
                        "updated_at": current_doc_status_timestamp(),
                    }
                    await self.lightrag.doc_status.upsert({doc_id: fallback_payload})
                    await self._set_multimodal_status_record(doc_id, True)
                await self.lightrag.doc_status.index_done_callback()
        except Exception as e:
            self.logger.warning(
                f"Error marking multimodal processing as complete for document {doc_id}: {e}"
            )

    async def _set_multimodal_status_record(self, doc_id: str, processed: bool) -> None:
        if (
            not hasattr(self, "multimodal_status_cache")
            or self.multimodal_status_cache is None
        ):
            return
        await self.multimodal_status_cache.upsert(
            {doc_id: {
                "multimodal_processed": processed,
                "updated_at": current_doc_status_timestamp(),
            }}
        )
        await self.multimodal_status_cache.index_done_callback()

    async def _get_multimodal_processed_flag(
        self, doc_id: str, doc_status: Dict[str, Any] | None = None
    ) -> bool:
        if doc_status is not None and "multimodal_processed" in doc_status:
            return bool(doc_status.get("multimodal_processed", False))
        if (
            hasattr(self, "multimodal_status_cache")
            and self.multimodal_status_cache is not None
        ):
            compatibility_status = await self.multimodal_status_cache.get_by_id(doc_id)
            if compatibility_status is not None:
                return bool(compatibility_status.get("multimodal_processed", False))
        return False

    async def _get_multimodal_status_record(self, doc_id: str) -> Dict[str, Any] | None:
        if (
            not hasattr(self, "multimodal_status_cache")
            or self.multimodal_status_cache is None
        ):
            return None
        return await self.multimodal_status_cache.get_by_id(doc_id)

    # ── Document status queries ──────────────────────────────────────────

    async def is_document_fully_processed(self, doc_id: str) -> bool:
        try:
            doc_status = await self.lightrag.doc_status.get_by_id(doc_id)
            if not doc_status:
                return False
            text_processed = doc_status.get("status") == DocStatus.PROCESSED
            multimodal_processed = await self._get_multimodal_processed_flag(doc_id, doc_status)
            return text_processed and multimodal_processed
        except Exception as e:
            self.logger.error(f"Error checking document processing status for {doc_id}: {e}")
            return False

    async def get_document_processing_status(self, doc_id: str) -> Dict[str, Any]:
        try:
            doc_status = await self.lightrag.doc_status.get_by_id(doc_id)
            if not doc_status:
                return {
                    "exists": False, "text_processed": False,
                    "multimodal_processed": False, "fully_processed": False,
                    "chunks_count": 0,
                }
            text_processed = doc_status.get("status") == DocStatus.PROCESSED
            multimodal_processed = await self._get_multimodal_processed_flag(doc_id, doc_status)
            fully_processed = text_processed and multimodal_processed
            return {
                "exists": True,
                "text_processed": text_processed,
                "multimodal_processed": multimodal_processed,
                "fully_processed": fully_processed,
                "chunks_count": doc_status.get("chunks_count", 0),
                "chunks_list": doc_status.get("chunks_list", []),
                "status": doc_status.get("status", ""),
                "updated_at": doc_status.get("updated_at", ""),
                "raw_status": doc_status,
            }
        except Exception as e:
            self.logger.error(f"Error getting document processing status for {doc_id}: {e}")
            return {
                "exists": False, "error": str(e),
                "text_processed": False, "multimodal_processed": False,
                "fully_processed": False, "chunks_count": 0,
            }
