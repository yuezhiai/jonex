"""
Specialized processors for different modalities

Includes:
- ContextExtractor: Universal context extraction for multimodal content
- ImageModalProcessor: Specialized processor for image content
- TableModalProcessor: Specialized processor for table content
- EquationModalProcessor: Specialized processor for equation content
- GenericModalProcessor: Processor for other modal content
"""

import re
import json
import time
import base64
import asyncio
import hashlib as _hashlib
from typing import Dict, Any, Tuple, List, Callable, Optional
from pathlib import Path
from dataclasses import dataclass

from lightrag.utils import (
    logger,
    compute_mdhash_id,
)
from lightrag.lightrag import LightRAG
from dataclasses import asdict
from lightrag.kg.shared_storage import get_namespace_data, get_pipeline_status_lock
from lightrag.operate import extract_entities, merge_nodes_and_edges

# Import prompt templates
from raganything.prompt import PROMPTS
from raganything.asr import AsrBackend, AsrCapability


# ── [jonex] 主解析提示词覆盖工具（KB 关联的 prompt 配置在解析时生效）──
class _BlankFormatDict(dict):
    """format_map 兜底：缺失占位符 → 空串，避免用户自定义 prompt 缺字段导致 KeyError。"""

    def __missing__(self, key):  # noqa: D401
        return ""


def _safe_format(template: str, **kwargs) -> str:
    """用空串兜底缺失占位符的 str.format；异常时回退原模板（不崩解析）。"""
    try:
        return template.format_map(_BlankFormatDict(kwargs))
    except Exception:
        return template


def _pick_prompt(prompt_overrides, base_code: str, has_context: bool,
                 default_base: str, default_ctx: str) -> str:
    """[jonex] base override 压过 _with_context：命中 base override 则两分支都用它；
    否则按 has_context 选内置 base / _with_context 变体。"""
    if prompt_overrides is not None:
        try:
            if prompt_overrides.has_override(base_code):
                return prompt_overrides.get_effective_prompt(base_code, default_base)
        except Exception:
            pass
    return default_ctx if has_context else default_base

from raganything.asr.backends.legacy import LegacyFunctionBackend
from raganything.utils import (
    format_table_body,
    get_equation_text_and_format,
    get_table_body,
    normalize_caption_list,
)


@dataclass
class ContextConfig:
    """Configuration for context extraction"""

    context_window: int = 1  # Window size for context extraction
    context_mode: str = "page"  # "page", "chunk", "token"
    max_context_tokens: int = 2000  # Maximum context tokens
    include_headers: bool = True  # Whether to include headers/titles
    include_captions: bool = True  # Whether to include image/table captions
    filter_content_types: List[str] = None  # Content types to include

    def __post_init__(self):
        if self.filter_content_types is None:
            self.filter_content_types = ["text"]


class ContextExtractor:
    """Universal context extractor supporting multiple content source formats"""

    def __init__(self, config: ContextConfig = None, tokenizer=None):
        """Initialize context extractor

        Args:
            config: Context extraction configuration
            tokenizer: Tokenizer for accurate token counting
        """
        self.config = config or ContextConfig()
        self.tokenizer = tokenizer

    def extract_context(
        self,
        content_source: Any,
        current_item_info: Dict[str, Any],
        content_format: str = "auto",
    ) -> str:
        """Extract context for current item from content source

        Args:
            content_source: Source content (list, dict, or other format)
            current_item_info: Information about current item (page_idx, index, etc.)
            content_format: Format hint for content source ("minerU", "text_chunks", "auto", etc.)

        Returns:
            Extracted context text
        """
        if not content_source and not self.config.context_window:
            return ""

        try:
            # Use format hint if provided, otherwise auto-detect
            if content_format == "minerU" and isinstance(content_source, list):
                return self._extract_from_content_list(
                    content_source, current_item_info
                )
            elif content_format == "text_chunks" and isinstance(content_source, list):
                return self._extract_from_text_chunks(content_source, current_item_info)
            elif content_format == "text" and isinstance(content_source, str):
                return self._extract_from_text_source(content_source, current_item_info)
            else:
                # Auto-detect content source format
                if isinstance(content_source, list):
                    return self._extract_from_content_list(
                        content_source, current_item_info
                    )
                elif isinstance(content_source, dict):
                    return self._extract_from_dict_source(
                        content_source, current_item_info
                    )
                elif isinstance(content_source, str):
                    return self._extract_from_text_source(
                        content_source, current_item_info
                    )
                else:
                    logger.warning(
                        f"Unsupported content source type: {type(content_source)}"
                    )
                    return ""
        except Exception as e:
            logger.error(f"Error extracting context: {e}")
            return ""

    def _extract_from_content_list(
        self, content_list: List[Dict], current_item_info: Dict
    ) -> str:
        """Extract context from MinerU-style content list

        Args:
            content_list: List of content items with page_idx and type info
            current_item_info: Current item information

        Returns:
            Context text from surrounding pages/chunks
        """
        if self.config.context_mode == "page":
            return self._extract_page_context(content_list, current_item_info)
        elif self.config.context_mode == "chunk":
            return self._extract_chunk_context(content_list, current_item_info)
        else:
            return self._extract_page_context(content_list, current_item_info)

    def _extract_page_context(
        self, content_list: List[Dict], current_item_info: Dict
    ) -> str:
        """Extract context based on page boundaries

        Args:
            content_list: List of content items
            current_item_info: Current item with page_idx

        Returns:
            Context text from surrounding pages
        """
        # [jonex] §12 MinerU 顶层 item 有 page_idx 但 item_info 为空字典，加 original 兜底
        current_page = current_item_info.get("page_idx")
        if current_page is None:
            current_page = (current_item.get("original") or {}).get("page_idx", 0)
        window_size = self.config.context_window

        start_page = max(0, current_page - window_size)
        end_page = current_page + window_size + 1

        context_texts = []

        for item in content_list:
            item_page = item.get("page_idx", 0)
            item_type = item.get("type", "")

            # Check if item is within context window and matches filter criteria
            if (
                start_page <= item_page < end_page
                and item_type in self.config.filter_content_types
            ):
                text_content = self._extract_text_from_item(item)
                if text_content and text_content.strip():
                    # Add page marker for better context understanding
                    if item_page != current_page:
                        context_texts.append(f"[Page {item_page}] {text_content}")
                    else:
                        context_texts.append(text_content)

        context = "\n".join(context_texts)
        return self._truncate_context(context)

    def _extract_chunk_context(
        self, content_list: List[Dict], current_item_info: Dict
    ) -> str:
        """Extract context based on content chunks

        Args:
            content_list: List of content items
            current_item_info: Current item with index info

        Returns:
            Context text from surrounding chunks
        """
        current_index = current_item_info.get("index", 0)
        window_size = self.config.context_window

        start_idx = max(0, current_index - window_size)
        end_idx = min(len(content_list), current_index + window_size + 1)

        context_texts = []

        for i in range(start_idx, end_idx):
            if i != current_index:
                item = content_list[i]
                item_type = item.get("type", "")

                if item_type in self.config.filter_content_types:
                    text_content = self._extract_text_from_item(item)
                    if text_content and text_content.strip():
                        context_texts.append(text_content)

        context = "\n".join(context_texts)
        return self._truncate_context(context)

    def _extract_text_from_item(self, item: Dict) -> str:
        """Extract text content from a content item

        Args:
            item: Content item dictionary

        Returns:
            Extracted text content
        """
        item_type = item.get("type", "")

        if item_type == "text":
            text = item.get("text", "")
            text_level = item.get("text_level", 0)

            # Add header indication for structured content·
            if self.config.include_headers and text_level > 0:
                return f"{'#' * text_level} {text}"
            return text

        elif item_type == "image" and self.config.include_captions:
            captions = item.get("image_caption", item.get("img_caption", []))
            if captions:
                return f"[Image: {', '.join(captions)}]"

        elif item_type == "table" and self.config.include_captions:
            captions = item.get("table_caption", [])
            if captions:
                return f"[Table: {', '.join(captions)}]"

        return ""

    def _extract_from_dict_source(
        self, dict_source: Dict, current_item_info: Dict
    ) -> str:
        """Extract context from dictionary-based content source

        Args:
            dict_source: Dictionary containing content
            current_item_info: Current item information

        Returns:
            Extracted context text
        """
        # Handle different dictionary structures
        if "content" in dict_source:
            context = str(dict_source["content"])
        elif "text" in dict_source:
            context = str(dict_source["text"])
        else:
            # Try to extract any string values
            text_parts = []
            for value in dict_source.values():
                if isinstance(value, str):
                    text_parts.append(value)
            context = "\n".join(text_parts)

        return self._truncate_context(context)

    def _extract_from_text_source(
        self, text_source: str, current_item_info: Dict
    ) -> str:
        """Extract context from plain text source

        Args:
            text_source: Plain text content
            current_item_info: Current item information

        Returns:
            Truncated text context
        """
        return self._truncate_context(text_source)

    def _extract_from_text_chunks(
        self, text_chunks: List[str], current_item_info: Dict
    ) -> str:
        """Extract context from simple text chunks list

        Args:
            text_chunks: List of text strings
            current_item_info: Current item information with index

        Returns:
            Context text from surrounding chunks
        """
        current_index = current_item_info.get("index", 0)
        window_size = self.config.context_window

        start_idx = max(0, current_index - window_size)
        end_idx = min(len(text_chunks), current_index + window_size + 1)

        context_texts = []
        for i in range(start_idx, end_idx):
            if i != current_index:  # Exclude current chunk
                if i < len(text_chunks):
                    chunk_text = str(text_chunks[i]).strip()
                    if chunk_text:
                        context_texts.append(chunk_text)

        context = "\n".join(context_texts)
        return self._truncate_context(context)

    def _truncate_context(self, context: str) -> str:
        """Truncate context to maximum token limit

        Args:
            context: Context text to truncate

        Returns:
            Truncated context text
        """
        if not context:
            return ""

        # Use tokenizer if available for accurate token counting
        if self.tokenizer:
            tokens = self.tokenizer.encode(context)
            if len(tokens) <= self.config.max_context_tokens:
                return context

            # Truncate to max tokens and decode back to text
            truncated_tokens = tokens[: self.config.max_context_tokens]
            truncated_text = self.tokenizer.decode(truncated_tokens)

            # Try to end at a sentence boundary
            last_period = truncated_text.rfind(".")
            last_newline = truncated_text.rfind("\n")

            if last_period > len(truncated_text) * 0.8:
                return truncated_text[: last_period + 1]
            elif last_newline > len(truncated_text) * 0.8:
                return truncated_text[:last_newline]
            else:
                return truncated_text + "..."
        else:
            # Fallback to character-based truncation if no tokenizer
            if len(context) <= self.config.max_context_tokens:
                return context

            # Simple truncation - fallback when no tokenizer available
            truncated = context[: self.config.max_context_tokens]

            # Try to end at a sentence boundary
            last_period = truncated.rfind(".")
            last_newline = truncated.rfind("\n")

            if last_period > len(truncated) * 0.8:
                return truncated[: last_period + 1]
            elif last_newline > len(truncated) * 0.8:
                return truncated[:last_newline]
            else:
                return truncated + "..."


class BaseModalProcessor:
    """Base class for modal processors"""

    def __init__(
        self,
        lightrag: LightRAG,
        modal_caption_func,
        context_extractor: ContextExtractor = None,
    ):
        """Initialize base processor

        Args:
            lightrag: LightRAG instance
            modal_caption_func: Function for generating descriptions
            context_extractor: Context extractor instance
        """
        self.lightrag = lightrag
        self.modal_caption_func = modal_caption_func

        # Use LightRAG's storage instances
        self.text_chunks_db = lightrag.text_chunks
        self.chunks_vdb = lightrag.chunks_vdb
        self.entities_vdb = lightrag.entities_vdb
        self.relationships_vdb = lightrag.relationships_vdb
        self.knowledge_graph_inst = lightrag.chunk_entity_relation_graph

        # Use LightRAG's configuration and functions
        self.embedding_func = lightrag.embedding_func
        self.llm_model_func = lightrag.llm_model_func
        self.global_config = asdict(lightrag)
        self.hashing_kv = lightrag.llm_response_cache
        self.tokenizer = lightrag.tokenizer

        # Initialize context extractor with tokenizer if not provided
        if context_extractor is None:
            self.context_extractor = ContextExtractor(tokenizer=self.tokenizer)
        else:
            self.context_extractor = context_extractor
            # Update tokenizer if context_extractor doesn't have one
            if self.context_extractor.tokenizer is None:
                self.context_extractor.tokenizer = self.tokenizer

        # Content source for context extraction
        self.content_source = None
        self.content_format = "auto"

    def set_content_source(self, content_source: Any, content_format: str = "auto"):
        """Set content source for context extraction

        Args:
            content_source: Source content for context extraction
            content_format: Format of content source ("minerU", "text_chunks", "auto")
        """
        self.content_source = content_source
        self.content_format = content_format
        logger.info(f"Content source set with format: {content_format}")

    def _get_context_for_item(self, item_info: Dict[str, Any]) -> str:
        """Get context for current processing item

        Args:
            item_info: Information about current item (page_idx, index, etc.)

        Returns:
            Context text for the item
        """
        if not self.content_source:
            return ""

        try:
            context = self.context_extractor.extract_context(
                self.content_source, item_info, self.content_format
            )
            if context:
                logger.debug(
                    f"Extracted context of length {len(context)} for item: {item_info}"
                )
            return context
        except Exception as e:
            logger.error(f"Error getting context for item {item_info}: {e}")
            return ""

    async def generate_description_only(
        self,
        modal_content,
        content_type: str,
        item_info: Dict[str, Any] = None,
        entity_name: str = None,
        prompt_overrides: Any = None,
    ) -> Tuple[str, Dict[str, Any]]:
        """
        Generate text description and entity info only, without entity relation extraction.
        Used for batch processing stage 1.

        Args:
            modal_content: Modal content to process
            content_type: Type of modal content
            item_info: Item information for context extraction
            entity_name: Optional predefined entity name

        Returns:
            Tuple of (description, entity_info)
        """
        # Subclasses must implement this method
        raise NotImplementedError("Subclasses must implement this method")

    async def _create_entity_and_chunk(
        self,
        modal_chunk: str,
        entity_info: Dict[str, Any],
        file_path: str,
        batch_mode: bool = False,
        doc_id: str = None,
        chunk_order_index: int = 0,
    ) -> Tuple[str, Dict[str, Any]]:
        """Create entity and text chunk"""
        # Create chunk
        chunk_id = compute_mdhash_id(str(modal_chunk), prefix="chunk-")
        tokens = len(self.tokenizer.encode(modal_chunk))

        # Use provided doc_id or generate one from chunk_id for backward compatibility
        actual_doc_id = doc_id if doc_id else chunk_id

        chunk_data = {
            "tokens": tokens,
            "content": modal_chunk,
            "chunk_order_index": chunk_order_index,
            "full_doc_id": actual_doc_id,  # Use proper document ID
            "file_path": file_path,
        }

        # Store chunk
        await self.text_chunks_db.upsert({chunk_id: chunk_data})

        # Store chunk in vector database for retrieval
        chunk_vdb_data = {
            chunk_id: {
                "content": modal_chunk,
                "full_doc_id": actual_doc_id,
                "tokens": tokens,
                "chunk_order_index": chunk_order_index,
                "file_path": file_path,
            }
        }
        await self.chunks_vdb.upsert(chunk_vdb_data)

        # Create entity node
        node_data = {
            "entity_id": entity_info["entity_name"],
            "entity_type": entity_info["entity_type"],
            "description": entity_info["summary"],
            "source_id": chunk_id,
            "file_path": file_path,
            "created_at": int(time.time()),
        }

        await self.knowledge_graph_inst.upsert_node(
            entity_info["entity_name"], node_data
        )

        # Insert entity into vector database
        entity_vdb_data = {
            compute_mdhash_id(entity_info["entity_name"], prefix="ent-"): {
                "entity_name": entity_info["entity_name"],
                "entity_type": entity_info["entity_type"],
                "content": f"{entity_info['entity_name']}\n{entity_info['summary']}",
                "source_id": chunk_id,
                "file_path": file_path,
            }
        }
        await self.entities_vdb.upsert(entity_vdb_data)

        # Process entity and relationship extraction
        chunk_results = await self._process_chunk_for_extraction(
            chunk_id, entity_info["entity_name"], batch_mode
        )

        return (
            entity_info["summary"],
            {
                "entity_name": entity_info["entity_name"],
                "entity_type": entity_info["entity_type"],
                "description": entity_info["summary"],
                "chunk_id": chunk_id,
            },
            chunk_results,
        )

    @staticmethod
    def _strip_thinking_tags(text: str) -> str:
        """Remove <think>/<thinking> tags produced by reasoning models.

        Models such as DeepSeek-R1 and Qwen2.5-think wrap their internal
        chain-of-thought in ``<think>…</think>`` or ``<thinking>…</thinking>``
        blocks before emitting the final answer.  When JSON parsing fails and
        the raw LLM response is used as a fallback, storing the entire response
        (including the reasoning preamble) pollutes the knowledge graph with
        internal model thoughts rather than actual content descriptions.

        This helper strips those blocks so that only the final answer text is
        stored or surfaced to callers.
        """
        cleaned = re.sub(
            r"<think>.*?</think>", "", text, flags=re.DOTALL | re.IGNORECASE
        )
        cleaned = re.sub(
            r"<thinking>.*?</thinking>", "", cleaned, flags=re.DOTALL | re.IGNORECASE
        )
        return cleaned.strip()

    def _robust_json_parse(self, response: str) -> dict:
        """Robust JSON parsing with multiple fallback strategies"""

        # Strategy 1: Try direct parsing first
        for json_candidate in self._extract_all_json_candidates(response):
            result = self._try_parse_json(json_candidate)
            if result:
                return result

        # Strategy 2: Try with basic cleanup
        for json_candidate in self._extract_all_json_candidates(response):
            cleaned = self._basic_json_cleanup(json_candidate)
            result = self._try_parse_json(cleaned)
            if result:
                return result

        # Strategy 3: Try progressive quote fixing
        for json_candidate in self._extract_all_json_candidates(response):
            fixed = self._progressive_quote_fix(json_candidate)
            result = self._try_parse_json(fixed)
            if result:
                return result

        # Strategy 4: Fallback to regex field extraction
        return self._extract_fields_with_regex(response)

    def _extract_all_json_candidates(self, response: str) -> list:
        """Extract all possible JSON candidates from response"""
        candidates = []

        # Pre-process: Remove thinking/reasoning tags that some models use
        # This handles models like qwen2.5-think, deepseek-r1 that wrap reasoning in tags
        cleaned_response = re.sub(
            r"<think>.*?</think>", "", response, flags=re.DOTALL | re.IGNORECASE
        )
        cleaned_response = re.sub(
            r"<thinking>.*?</thinking>",
            "",
            cleaned_response,
            flags=re.DOTALL | re.IGNORECASE,
        )

        # Method 1: JSON in code blocks
        json_blocks = re.findall(
            r"```(?:json)?\s*(\{.*?\})\s*```", cleaned_response, re.DOTALL
        )
        candidates.extend(json_blocks)

        # Method 2: Balanced braces
        brace_count = 0
        start_pos = -1

        for i, char in enumerate(cleaned_response):
            if char == "{":
                if brace_count == 0:
                    start_pos = i
                brace_count += 1
            elif char == "}":
                brace_count -= 1
                if brace_count == 0 and start_pos != -1:
                    candidates.append(cleaned_response[start_pos : i + 1])

        # Method 3: Simple regex fallback
        simple_match = re.search(r"\{.*\}", cleaned_response, re.DOTALL)
        if simple_match:
            candidates.append(simple_match.group(0))

        return candidates

    def _try_parse_json(self, json_str: str) -> dict:
        """Try to parse JSON string, return None if failed"""
        if not json_str or not json_str.strip():
            return None

        try:
            return json.loads(json_str)
        except (json.JSONDecodeError, ValueError):
            return None

    def _basic_json_cleanup(self, json_str: str) -> str:
        """Basic cleanup for common JSON issues"""
        # Remove extra whitespace
        json_str = json_str.strip()

        # Fix common quote issues
        json_str = json_str.replace('"', '"').replace('"', '"')  # Smart quotes
        json_str = json_str.replace(""", "'").replace(""", "'")  # Smart apostrophes

        # Fix trailing commas (simple case)
        json_str = re.sub(r",(\s*[}\]])", r"\1", json_str)

        return json_str

    def _progressive_quote_fix(self, json_str: str) -> str:
        """Progressive fixing of quote and escape issues"""
        # Only escape unescaped backslashes before quotes
        json_str = re.sub(r'(?<!\\)\\(?=")', r"\\\\", json_str)

        # Fix unescaped backslashes in string values (more conservative)
        def fix_string_content(match):
            content = match.group(1)
            # Only escape obvious problematic patterns
            content = re.sub(r"\\(?=[a-zA-Z])", r"\\\\", content)  # \alpha -> \\alpha
            return f'"{content}"'

        json_str = re.sub(r'"([^"]*(?:\\.[^"]*)*)"', fix_string_content, json_str)
        return json_str

    def _extract_fields_with_regex(self, response: str) -> dict:
        """Extract required fields using regex as last resort"""
        logger.warning("Using regex fallback for JSON parsing")

        # Extract detailed_description
        desc_match = re.search(
            r'"detailed_description":\s*"([^"]*(?:\\.[^"]*)*)"', response, re.DOTALL
        )
        description = desc_match.group(1) if desc_match else ""

        # Extract entity_name
        name_match = re.search(r'"entity_name":\s*"([^"]*(?:\\.[^"]*)*)"', response)
        entity_name = name_match.group(1) if name_match else "unknown_entity"

        # Extract entity_type
        type_match = re.search(r'"entity_type":\s*"([^"]*(?:\\.[^"]*)*)"', response)
        entity_type = type_match.group(1) if type_match else "unknown"

        # Extract summary
        summary_match = re.search(
            r'"summary":\s*"([^"]*(?:\\.[^"]*)*)"', response, re.DOTALL
        )
        summary = summary_match.group(1) if summary_match else description[:100]

        return {
            "detailed_description": description,
            "entity_info": {
                "entity_name": entity_name,
                "entity_type": entity_type,
                "summary": summary,
            },
        }

    def _extract_json_from_response(self, response: str) -> str:
        """Legacy method - now handled by _extract_all_json_candidates"""
        candidates = self._extract_all_json_candidates(response)
        return candidates[0] if candidates else None

    def _fix_json_escapes(self, json_str: str) -> str:
        """Legacy method - now handled by progressive strategies"""
        return self._progressive_quote_fix(json_str)

    async def _process_chunk_for_extraction(
        self, chunk_id: str, modal_entity_name: str, batch_mode: bool = False
    ):
        """Process chunk for entity and relationship extraction"""
        chunk_data = await self.text_chunks_db.get_by_id(chunk_id)
        if not chunk_data:
            logger.error(f"Chunk {chunk_id} not found")
            return

        # Create text chunk for vector database
        chunk_vdb_data = {
            chunk_id: {
                "content": chunk_data["content"],
                "full_doc_id": chunk_id,
                "tokens": chunk_data["tokens"],
                "chunk_order_index": chunk_data["chunk_order_index"],
                "file_path": chunk_data["file_path"],
            }
        }

        await self.chunks_vdb.upsert(chunk_vdb_data)

        pipeline_status = await get_namespace_data("pipeline_status")
        pipeline_status_lock = get_pipeline_status_lock()

        # Prepare chunk for extraction
        chunks = {chunk_id: chunk_data}

        # Extract entities and relationships
        chunk_results = await extract_entities(
            chunks=chunks,
            global_config=self.global_config,
            pipeline_status=pipeline_status,
            pipeline_status_lock=pipeline_status_lock,
            llm_response_cache=self.hashing_kv,
        )

        # Add "belongs_to" relationships for all extracted entities
        processed_chunk_results = []
        for maybe_nodes, maybe_edges in chunk_results:
            for entity_name in maybe_nodes.keys():
                if entity_name != modal_entity_name:  # Skip self-relationship
                    # Create belongs_to relationship
                    relation_data = {
                        "description": f"Entity {entity_name} belongs to {modal_entity_name}",
                        "keywords": "belongs_to,part_of,contained_in",
                        "source_id": chunk_id,
                        "weight": 10.0,
                        "file_path": chunk_data.get("file_path", "manual_creation"),
                    }
                    await self.knowledge_graph_inst.upsert_edge(
                        entity_name, modal_entity_name, relation_data
                    )

                    relation_id = compute_mdhash_id(
                        entity_name + modal_entity_name, prefix="rel-"
                    )
                    relation_vdb_data = {
                        relation_id: {
                            "src_id": entity_name,
                            "tgt_id": modal_entity_name,
                            "keywords": relation_data["keywords"],
                            "content": f"{relation_data['keywords']}\t{entity_name}\n{modal_entity_name}\n{relation_data['description']}",
                            "source_id": chunk_id,
                            "file_path": chunk_data.get("file_path", "manual_creation"),
                        }
                    }
                    await self.relationships_vdb.upsert(relation_vdb_data)

                    # Add to maybe_edges
                    maybe_edges[(entity_name, modal_entity_name)] = [relation_data]

            processed_chunk_results.append((maybe_nodes, maybe_edges))

        if not batch_mode:
            # Merge with correct file_path parameter
            file_path = chunk_data.get("file_path", "manual_creation")
            doc_id = chunk_data.get("full_doc_id")
            await merge_nodes_and_edges(
                chunk_results=chunk_results,
                knowledge_graph_inst=self.knowledge_graph_inst,
                entity_vdb=self.entities_vdb,
                relationships_vdb=self.relationships_vdb,
                global_config=self.global_config,
                full_entities_storage=self.lightrag.full_entities,
                full_relations_storage=self.lightrag.full_relations,
                doc_id=doc_id,
                pipeline_status=pipeline_status,
                pipeline_status_lock=pipeline_status_lock,
                llm_response_cache=self.hashing_kv,
                entity_chunks_storage=self.lightrag.entity_chunks,
                relation_chunks_storage=self.lightrag.relation_chunks,
                current_file_number=1,
                total_files=1,
                file_path=file_path,
            )

            # Ensure all storage updates are complete
            await self.lightrag._insert_done()

        return processed_chunk_results


class ImageModalProcessor(BaseModalProcessor):
    """Processor specialized for image content"""

    def __init__(
        self,
        lightrag: LightRAG,
        modal_caption_func,
        context_extractor: ContextExtractor = None,
    ):
        """Initialize image processor

        Args:
            lightrag: LightRAG instance
            modal_caption_func: Function for generating descriptions (supporting image understanding)
            context_extractor: Context extractor instance
        """
        super().__init__(lightrag, modal_caption_func, context_extractor)

    def _encode_image_to_base64(self, image_path: str) -> str:
        """Encode image to base64"""
        try:
            with open(image_path, "rb") as image_file:
                encoded_string = base64.b64encode(image_file.read()).decode("utf-8")
            return encoded_string
        except Exception as e:
            logger.error(f"Failed to encode image {image_path}: {e}")
            return ""

    async def generate_description_only(
        self,
        modal_content,
        content_type: str,
        item_info: Dict[str, Any] = None,
        entity_name: str = None,
        prompt_overrides: Any = None,
    ) -> Tuple[str, Dict[str, Any]]:
        """
        Generate image description and entity info only, without entity relation extraction.
        Used for batch processing stage 1.

        Args:
            modal_content: Image content to process
            content_type: Type of modal content ("image")
            item_info: Item information for context extraction
            entity_name: Optional predefined entity name

        Returns:
            Tuple of (enhanced_caption, entity_info)
        """
        try:
            # Parse image content (reuse existing logic)
            if isinstance(modal_content, str):
                try:
                    content_data = json.loads(modal_content)
                except json.JSONDecodeError:
                    content_data = {"description": modal_content}
            else:
                content_data = modal_content

            image_path = content_data.get("img_path")
            captions = content_data.get(
                "image_caption", content_data.get("img_caption", [])
            )
            footnotes = content_data.get(
                "image_footnote", content_data.get("img_footnote", [])
            )

            # Validate image path
            if not image_path:
                raise ValueError(
                    f"No image path provided in modal_content: {modal_content}"
                )

            # Convert to Path object and check if it exists
            image_path_obj = Path(image_path)
            if not image_path_obj.exists():
                raise FileNotFoundError(f"Image file not found: {image_path}")

            # Extract context for current item
            context = ""
            if item_info:
                context = self._get_context_for_item(item_info)

            # Build detailed visual analysis prompt (KB 主解析提示词覆盖 vision_prompt)
            _tmpl = _pick_prompt(
                prompt_overrides, "vision_prompt", bool(context),
                PROMPTS["vision_prompt"],
                PROMPTS.get("vision_prompt_with_context", PROMPTS["vision_prompt"]),
            )
            vision_prompt = _safe_format(
                _tmpl,
                context=context,
                entity_name=entity_name
                if entity_name
                else "unique descriptive name for this image",
                image_path=image_path,
                captions=captions if captions else "None",
                footnotes=footnotes if footnotes else "None",
            )

            # Encode image to base64
            image_base64 = self._encode_image_to_base64(image_path)
            if not image_base64:
                raise RuntimeError(f"Failed to encode image to base64: {image_path}")

            # [jonex] Retry VLM call + parse up to 2× covering both
            # HTTP/connection errors and malformed-JSON / missing-field
            # errors.  Low-quantization / heretic models occasionally
            # output bad JSON; transient network issues also happen.
            last_error = None
            for attempt in range(3):
                try:
                    response = await self.modal_caption_func(
                        vision_prompt,
                        image_data=image_base64,
                        system_prompt=PROMPTS["IMAGE_ANALYSIS_SYSTEM"],
                    )
                    enhanced_caption, entity_info = self._parse_response(
                        response, entity_name,
                    )
                    return enhanced_caption, entity_info
                except (json.JSONDecodeError, ValueError) as e:
                    last_error = e
                    if attempt < 2:
                        logger.warning(
                            "VLM retry %d/2 for %s: %s",
                            attempt + 1, image_path, e,
                        )
                    else:
                        raise
                except Exception as e:
                    last_error = e
                    if attempt < 2:
                        logger.warning(
                        "VLM call retry %d/2 for %s [%s]: %s",
                        attempt + 1, image_path, type(e).__name__, e,
                    )
                    else:
                        raise

        except Exception as e:
            logger.error(
                "Error generating image description [%s]: %s",
                type(e).__name__, e,
            )
            # Fallback processing
            fallback_entity = {
                "entity_name": entity_name
                if entity_name
                else f"image_{compute_mdhash_id(str(modal_content))}",
                "entity_type": "image",
                "summary": f"Image content: {str(modal_content)[:100]}",
            }
            return str(modal_content), fallback_entity

    async def process_multimodal_content(
        self,
        modal_content,
        content_type: str,
        file_path: str = "manual_creation",
        entity_name: str = None,
        item_info: Dict[str, Any] = None,
        batch_mode: bool = False,
        doc_id: str = None,
        chunk_order_index: int = 0,
    ) -> Tuple[str, Dict[str, Any]]:
        """Process image content with context support"""
        try:
            # Generate description and entity info
            enhanced_caption, entity_info = await self.generate_description_only(
                modal_content, content_type, item_info, entity_name
            )

            # Build complete image content
            if isinstance(modal_content, str):
                try:
                    content_data = json.loads(modal_content)
                except json.JSONDecodeError:
                    content_data = {"description": modal_content}
            else:
                content_data = modal_content

            image_path = content_data.get("img_path", "")
            captions = content_data.get(
                "image_caption", content_data.get("img_caption", [])
            )
            footnotes = content_data.get(
                "image_footnote", content_data.get("img_footnote", [])
            )

            modal_chunk = PROMPTS["image_chunk"].format(
                image_path=image_path,
                captions=", ".join(captions) if captions else "None",
                footnotes=", ".join(footnotes) if footnotes else "None",
                enhanced_caption=enhanced_caption,
            )

            return await self._create_entity_and_chunk(
                modal_chunk,
                entity_info,
                file_path,
                batch_mode,
                doc_id,
                chunk_order_index,
            )

        except Exception as e:
            logger.error(f"Error processing image content: {e}")
            # Fallback processing
            fallback_entity = {
                "entity_name": entity_name
                if entity_name
                else f"image_{compute_mdhash_id(str(modal_content))}",
                "entity_type": "image",
                "summary": f"Image content: {str(modal_content)[:100]}",
            }
            return str(modal_content), fallback_entity

    def _parse_response(
        self, response: str, entity_name: str = None
    ) -> Tuple[str, Dict[str, Any]]:
        """Parse model response"""
        try:
            response_data = self._robust_json_parse(response)

            description = response_data.get("detailed_description", "")
            entity_data = response_data.get("entity_info", {})

            if not description or not entity_data:
                raise ValueError("Missing required fields in response")

            if not all(
                key in entity_data for key in ["entity_name", "entity_type", "summary"]
            ):
                raise ValueError("Missing required fields in entity_info")

            entity_data["entity_name"] = (
                entity_data["entity_name"] + f" ({entity_data['entity_type']})"
            )
            if entity_name:
                entity_data["entity_name"] = entity_name

            return description, entity_data

        except (json.JSONDecodeError, AttributeError, ValueError) as e:
            logger.error(f"Error parsing image analysis response: {e}")
            logger.debug(f"Raw response: {response}")
            cleaned = self._strip_thinking_tags(response)
            fallback_entity = {
                "entity_name": entity_name
                if entity_name
                else f"image_{compute_mdhash_id(cleaned)}",
                "entity_type": "image",
                "summary": cleaned[:100] + "..." if len(cleaned) > 100 else cleaned,
            }
            return cleaned, fallback_entity


class TableModalProcessor(BaseModalProcessor):
    """Processor specialized for table content"""

    async def generate_description_only(
        self,
        modal_content,
        content_type: str,
        item_info: Dict[str, Any] = None,
        entity_name: str = None,
        prompt_overrides: Any = None,
    ) -> Tuple[str, Dict[str, Any]]:
        """
        Generate table description and entity info only, without entity relation extraction.
        Used for batch processing stage 1.

        Args:
            modal_content: Table content to process
            content_type: Type of modal content ("table")
            item_info: Item information for context extraction
            entity_name: Optional predefined entity name

        Returns:
            Tuple of (enhanced_caption, entity_info)
        """
        try:
            # Parse table content (reuse existing logic)
            if isinstance(modal_content, str):
                try:
                    content_data = json.loads(modal_content)
                except json.JSONDecodeError:
                    content_data = {"table_body": modal_content}
            else:
                content_data = modal_content

            table_img_path = content_data.get("img_path")
            table_caption = normalize_caption_list(content_data.get("table_caption"))
            table_body = format_table_body(get_table_body(content_data))
            table_footnote = normalize_caption_list(content_data.get("table_footnote"))

            # Extract context for current item
            context = ""
            if item_info:
                context = self._get_context_for_item(item_info)

            # Build table analysis prompt with context
            if context:
                table_prompt = PROMPTS.get(
                    "table_prompt_with_context", PROMPTS["table_prompt"]
                ).format(
                    context=context,
                    entity_name=entity_name
                    if entity_name
                    else "descriptive name for this table",
                    table_img_path=table_img_path,
                    table_caption=table_caption if table_caption else "None",
                    table_body=table_body,
                    table_footnote=table_footnote if table_footnote else "None",
                )
            else:
                table_prompt = PROMPTS["table_prompt"].format(
                    entity_name=entity_name
                    if entity_name
                    else "descriptive name for this table",
                    table_img_path=table_img_path,
                    table_caption=table_caption if table_caption else "None",
                    table_body=table_body,
                    table_footnote=table_footnote if table_footnote else "None",
                )

            # Call LLM for table analysis
            response = await self.modal_caption_func(
                table_prompt,
                system_prompt=PROMPTS["TABLE_ANALYSIS_SYSTEM"],
            )

            # Parse response (reuse existing logic)
            enhanced_caption, entity_info = self._parse_table_response(
                response, entity_name
            )

            return enhanced_caption, entity_info

        except Exception as e:
            logger.error(f"Error generating table description: {e}")
            # Fallback processing
            fallback_entity = {
                "entity_name": entity_name
                if entity_name
                else f"table_{compute_mdhash_id(str(modal_content))}",
                "entity_type": "table",
                "summary": f"Table content: {str(modal_content)[:100]}",
            }
            return str(modal_content), fallback_entity

    async def process_multimodal_content(
        self,
        modal_content,
        content_type: str,
        file_path: str = "manual_creation",
        entity_name: str = None,
        item_info: Dict[str, Any] = None,
        batch_mode: bool = False,
        doc_id: str = None,
        chunk_order_index: int = 0,
    ) -> Tuple[str, Dict[str, Any]]:
        """Process table content with context support"""
        try:
            # Generate description and entity info
            enhanced_caption, entity_info = await self.generate_description_only(
                modal_content, content_type, item_info, entity_name
            )

            # Parse table content for building complete chunk
            if isinstance(modal_content, str):
                try:
                    content_data = json.loads(modal_content)
                except json.JSONDecodeError:
                    content_data = {"table_body": modal_content}
            else:
                content_data = modal_content

            table_img_path = content_data.get("img_path")
            table_caption = normalize_caption_list(content_data.get("table_caption"))
            table_body = format_table_body(get_table_body(content_data))
            table_footnote = normalize_caption_list(content_data.get("table_footnote"))

            # Build complete table content
            modal_chunk = PROMPTS["table_chunk"].format(
                table_img_path=table_img_path,
                table_caption=", ".join(table_caption) if table_caption else "None",
                table_body=table_body,
                table_footnote=", ".join(table_footnote) if table_footnote else "None",
                enhanced_caption=enhanced_caption,
            )

            return await self._create_entity_and_chunk(
                modal_chunk,
                entity_info,
                file_path,
                batch_mode,
                doc_id,
                chunk_order_index,
            )

        except Exception as e:
            logger.error(f"Error processing table content: {e}")
            # Fallback processing
            fallback_entity = {
                "entity_name": entity_name
                if entity_name
                else f"table_{compute_mdhash_id(str(modal_content))}",
                "entity_type": "table",
                "summary": f"Table content: {str(modal_content)[:100]}",
            }
            return str(modal_content), fallback_entity

    def _parse_table_response(
        self, response: str, entity_name: str = None
    ) -> Tuple[str, Dict[str, Any]]:
        """Parse table analysis response"""
        try:
            response_data = self._robust_json_parse(response)

            description = response_data.get("detailed_description", "")
            entity_data = response_data.get("entity_info", {})

            if not description or not entity_data:
                raise ValueError("Missing required fields in response")

            if not all(
                key in entity_data for key in ["entity_name", "entity_type", "summary"]
            ):
                raise ValueError("Missing required fields in entity_info")

            entity_data["entity_name"] = (
                entity_data["entity_name"] + f" ({entity_data['entity_type']})"
            )
            if entity_name:
                entity_data["entity_name"] = entity_name

            return description, entity_data

        except (json.JSONDecodeError, AttributeError, ValueError) as e:
            logger.error(f"Error parsing table analysis response: {e}")
            logger.debug(f"Raw response: {response}")
            cleaned = self._strip_thinking_tags(response)
            fallback_entity = {
                "entity_name": entity_name
                if entity_name
                else f"table_{compute_mdhash_id(cleaned)}",
                "entity_type": "table",
                "summary": cleaned[:100] + "..." if len(cleaned) > 100 else cleaned,
            }
            return cleaned, fallback_entity


class EquationModalProcessor(BaseModalProcessor):
    """Processor specialized for equation content"""

    async def generate_description_only(
        self,
        modal_content,
        content_type: str,
        item_info: Dict[str, Any] = None,
        entity_name: str = None,
        prompt_overrides: Any = None,
    ) -> Tuple[str, Dict[str, Any]]:
        """
        Generate equation description and entity info only, without entity relation extraction.
        Used for batch processing stage 1.

        Args:
            modal_content: Equation content to process
            content_type: Type of modal content ("equation")
            item_info: Item information for context extraction
            entity_name: Optional predefined entity name

        Returns:
            Tuple of (enhanced_caption, entity_info)
        """
        try:
            # Parse equation content (reuse existing logic)
            if isinstance(modal_content, str):
                try:
                    content_data = json.loads(modal_content)
                except json.JSONDecodeError:
                    content_data = {"equation": modal_content}
            else:
                content_data = modal_content

            equation_text, equation_format = get_equation_text_and_format(content_data)

            # Extract context for current item
            context = ""
            if item_info:
                context = self._get_context_for_item(item_info)

            # Build equation analysis prompt with context
            if context:
                equation_prompt = PROMPTS.get(
                    "equation_prompt_with_context", PROMPTS["equation_prompt"]
                ).format(
                    context=context,
                    equation_text=equation_text,
                    equation_format=equation_format,
                    entity_name=entity_name
                    if entity_name
                    else "descriptive name for this equation",
                )
            else:
                equation_prompt = PROMPTS["equation_prompt"].format(
                    equation_text=equation_text,
                    equation_format=equation_format,
                    entity_name=entity_name
                    if entity_name
                    else "descriptive name for this equation",
                )

            # Call LLM for equation analysis
            response = await self.modal_caption_func(
                equation_prompt,
                system_prompt=PROMPTS["EQUATION_ANALYSIS_SYSTEM"],
            )

            # Parse response (reuse existing logic)
            enhanced_caption, entity_info = self._parse_equation_response(
                response, entity_name
            )

            return enhanced_caption, entity_info

        except Exception as e:
            logger.error(f"Error generating equation description: {e}")
            # Fallback processing
            fallback_entity = {
                "entity_name": entity_name
                if entity_name
                else f"equation_{compute_mdhash_id(str(modal_content))}",
                "entity_type": "equation",
                "summary": f"Equation content: {str(modal_content)[:100]}",
            }
            return str(modal_content), fallback_entity

    async def process_multimodal_content(
        self,
        modal_content,
        content_type: str,
        file_path: str = "manual_creation",
        entity_name: str = None,
        item_info: Dict[str, Any] = None,
        batch_mode: bool = False,
        doc_id: str = None,
        chunk_order_index: int = 0,
    ) -> Tuple[str, Dict[str, Any]]:
        """Process equation content with context support"""
        try:
            # Generate description and entity info
            enhanced_caption, entity_info = await self.generate_description_only(
                modal_content, content_type, item_info, entity_name
            )

            # Parse equation content for building complete chunk
            if isinstance(modal_content, str):
                try:
                    content_data = json.loads(modal_content)
                except json.JSONDecodeError:
                    content_data = {"equation": modal_content}
            else:
                content_data = modal_content

            equation_text, equation_format = get_equation_text_and_format(content_data)

            # Build complete equation content
            modal_chunk = PROMPTS["equation_chunk"].format(
                equation_text=equation_text,
                equation_format=equation_format,
                enhanced_caption=enhanced_caption,
            )

            return await self._create_entity_and_chunk(
                modal_chunk,
                entity_info,
                file_path,
                batch_mode,
                doc_id,
                chunk_order_index,
            )

        except Exception as e:
            logger.error(f"Error processing equation content: {e}")
            # Fallback processing
            fallback_entity = {
                "entity_name": entity_name
                if entity_name
                else f"equation_{compute_mdhash_id(str(modal_content))}",
                "entity_type": "equation",
                "summary": f"Equation content: {str(modal_content)[:100]}",
            }
            return str(modal_content), fallback_entity

    def _parse_equation_response(
        self, response: str, entity_name: str = None
    ) -> Tuple[str, Dict[str, Any]]:
        """Parse equation analysis response with robust JSON handling"""
        try:
            response_data = self._robust_json_parse(response)

            description = response_data.get("detailed_description", "")
            entity_data = response_data.get("entity_info", {})

            if not description or not entity_data:
                raise ValueError("Missing required fields in response")

            if not all(
                key in entity_data for key in ["entity_name", "entity_type", "summary"]
            ):
                raise ValueError("Missing required fields in entity_info")

            entity_data["entity_name"] = (
                entity_data["entity_name"] + f" ({entity_data['entity_type']})"
            )
            if entity_name:
                entity_data["entity_name"] = entity_name

            return description, entity_data

        except (json.JSONDecodeError, AttributeError, ValueError) as e:
            logger.error(f"Error parsing equation analysis response: {e}")
            logger.debug(f"Raw response: {response}")
            cleaned = self._strip_thinking_tags(response)
            fallback_entity = {
                "entity_name": entity_name
                if entity_name
                else f"equation_{compute_mdhash_id(cleaned)}",
                "entity_type": "equation",
                "summary": cleaned[:100] + "..." if len(cleaned) > 100 else cleaned,
            }
            return cleaned, fallback_entity


class GenericModalProcessor(BaseModalProcessor):
    """Generic processor for other types of modal content"""

    async def generate_description_only(
        self,
        modal_content,
        content_type: str,
        item_info: Dict[str, Any] = None,
        entity_name: str = None,
        prompt_overrides: Any = None,
    ) -> Tuple[str, Dict[str, Any]]:
        """
        Generate generic modal description and entity info only, without entity relation extraction.
        Used for batch processing stage 1.

        Args:
            modal_content: Generic modal content to process
            content_type: Type of modal content
            item_info: Item information for context extraction
            entity_name: Optional predefined entity name

        Returns:
            Tuple of (enhanced_caption, entity_info)
        """
        try:
            # Extract context for current item
            context = ""
            if item_info:
                context = self._get_context_for_item(item_info)

            # Build generic analysis prompt (KB 主解析提示词覆盖 generic_prompt)
            _tmpl = _pick_prompt(
                prompt_overrides, "generic_prompt", bool(context),
                PROMPTS["generic_prompt"],
                PROMPTS.get("generic_prompt_with_context", PROMPTS["generic_prompt"]),
            )
            generic_prompt = _safe_format(
                _tmpl,
                context=context,
                content_type=content_type,
                entity_name=entity_name
                if entity_name
                else f"descriptive name for this {content_type}",
                content=str(modal_content),
            )

            # Call LLM for generic analysis
            response = await self.modal_caption_func(
                generic_prompt,
                system_prompt=PROMPTS["GENERIC_ANALYSIS_SYSTEM"].format(
                    content_type=content_type
                ),
            )

            # Parse response (reuse existing logic)
            enhanced_caption, entity_info = self._parse_generic_response(
                response, entity_name, content_type
            )

            return enhanced_caption, entity_info

        except Exception as e:
            logger.error(f"Error generating {content_type} description: {e}")
            # Fallback processing
            fallback_entity = {
                "entity_name": entity_name
                if entity_name
                else f"{content_type}_{compute_mdhash_id(str(modal_content))}",
                "entity_type": content_type,
                "summary": f"{content_type} content: {str(modal_content)[:100]}",
            }
            return str(modal_content), fallback_entity

    async def process_multimodal_content(
        self,
        modal_content,
        content_type: str,
        file_path: str = "manual_creation",
        entity_name: str = None,
        item_info: Dict[str, Any] = None,
        batch_mode: bool = False,
        doc_id: str = None,
        chunk_order_index: int = 0,
    ) -> Tuple[str, Dict[str, Any]]:
        """Process generic modal content with context support"""
        try:
            # Generate description and entity info
            enhanced_caption, entity_info = await self.generate_description_only(
                modal_content, content_type, item_info, entity_name
            )

            # Build complete content
            modal_chunk = PROMPTS["generic_chunk"].format(
                content_type=content_type.title(),
                content=str(modal_content),
                enhanced_caption=enhanced_caption,
            )

            return await self._create_entity_and_chunk(
                modal_chunk,
                entity_info,
                file_path,
                batch_mode,
                doc_id,
                chunk_order_index,
            )

        except Exception as e:
            logger.error(f"Error processing {content_type} content: {e}")
            # Fallback processing
            fallback_entity = {
                "entity_name": entity_name
                if entity_name
                else f"{content_type}_{compute_mdhash_id(str(modal_content))}",
                "entity_type": content_type,
                "summary": f"{content_type} content: {str(modal_content)[:100]}",
            }
            return str(modal_content), fallback_entity

    def _parse_generic_response(
        self, response: str, entity_name: str = None, content_type: str = "content"
    ) -> Tuple[str, Dict[str, Any]]:
        """Parse generic analysis response"""
        try:
            response_data = self._robust_json_parse(response)

            description = response_data.get("detailed_description", "")
            entity_data = response_data.get("entity_info", {})

            if not description or not entity_data:
                raise ValueError("Missing required fields in response")

            if not all(
                key in entity_data for key in ["entity_name", "entity_type", "summary"]
            ):
                raise ValueError("Missing required fields in entity_info")

            entity_data["entity_name"] = (
                entity_data["entity_name"] + f" ({entity_data['entity_type']})"
            )
            if entity_name:
                entity_data["entity_name"] = entity_name

            return description, entity_data

        except (json.JSONDecodeError, AttributeError, ValueError) as e:
            logger.error(f"Error parsing {content_type} analysis response: {e}")
            logger.debug(f"Raw response: {response}")
            cleaned = self._strip_thinking_tags(response)
            fallback_entity = {
                "entity_name": entity_name
                if entity_name
                else f"{content_type}_{compute_mdhash_id(cleaned)}",
                "entity_type": content_type,
                "summary": cleaned[:100] + "..." if len(cleaned) > 100 else cleaned,
            }
            return cleaned, fallback_entity


# ===========================================================================
# Audio Processing
# ===========================================================================

MAX_ENTITY_TRANSCRIPT_PREVIEW = 1000
CACHE_SCHEMA_VERSION = 1
AUDIO_ENTITY_TYPE_ENUM = frozenset({
    "meeting", "lecture", "podcast", "interview", "call", "conversation", "unknown"
})


def _sha256(file_path: str) -> str:
    """Full SHA-256 of file via 1MB-chunked read."""
    h = _hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _asr_config_hash(func: Callable) -> str:
    """Stable hash from ASR function identity for cache invalidation."""
    identity = getattr(func, "asr_identity", None)
    if identity:
        return _hashlib.md5(str(identity).encode()).hexdigest()[:8]
    raw = f"{getattr(func, '__module__', '')}.{getattr(func, '__qualname__', '')}"
    raw += f"|{getattr(func, 'asr_model', '')}"
    return _hashlib.md5(raw.encode()).hexdigest()[:8]


class AsrModalProcessor(BaseModalProcessor):
    """Processor for audio: ASR transcription + MapReduce summarization + time-aligned chunking."""

    def __init__(self, lightrag, modal_caption_func, asr_model_func=None,
                 asr_backend: Optional[AsrBackend] = None,
                 preprocess_audio_func=None, config=None, tokenizer=None,
                 context_extractor=None):
        super().__init__(lightrag, modal_caption_func, context_extractor)

        # Backend-first: prefer asr_backend, fall back to legacy func
        if asr_backend is not None:
            self.asr_backend = asr_backend
        elif asr_model_func is not None:
            self.asr_backend = LegacyFunctionBackend(config, asr_model_func)
        else:
            self.asr_backend = None

        # Keep self.asr_model_func for the existing _asr_config_hash and cache code
        if asr_model_func is not None:
            self.asr_model_func = asr_model_func
        elif asr_backend is not None:
            self.asr_model_func = asr_backend.create_func()
        else:
            self.asr_model_func = None

        self.preprocess_audio_func = preprocess_audio_func or (lambda p, o: p)
        self._config = config
        self._tokenizer = tokenizer
        self._asr_semaphore = asyncio.Semaphore(
            config.max_parallel_asr if config else 1
        )

        # Warn only for legacy func path (backend handles its own identity)
        if asr_backend is None and asr_model_func is not None:
            if not getattr(asr_model_func, "asr_identity", None):
                logger.warning(
                    "asr_model_func has no 'asr_identity' attribute. "
                    "Cache may not invalidate correctly across model/config changes. "
                    "Set func.asr_identity = 'engine:model' for reliable caching."
                )

    # ── orchestration ────────────────────────────────────

    async def process_multimodal_content(
        self,
        modal_content,
        content_type: str,
        file_path: str = "",
        entity_name: str = None,
        item_info: Dict[str, Any] = None,
        batch_mode: bool = False,
        doc_id: str = None,
        chunk_order_index: int = 0,
    ):
        """Individual processing entry — delegates to generate_description_only.

        For audio, multi-chunk generation happens in the batch pipeline
        (_convert_to_lightrag_chunks_type_aware). This method provides
        a single-chunk fallback compatible with individual processing.
        """
        enhanced_caption, entity_info = await self.generate_description_only(
            modal_content, content_type, item_info, entity_name
        )
        return enhanced_caption, entity_info, []

    async def generate_description_only(self, modal_content, content_type,
                                        item_info=None, entity_name=None,
                                        prompt_overrides=None):
        """Main entry point: ASR → segment → MapReduce → entity info."""
        audio_path = modal_content.get("audio_path")
        if not audio_path:
            raise ValueError(f"No audio_path in modal_content: {modal_content}")

        asr_result = await self._load_or_transcribe(audio_path)
        transcript = asr_result.get("transcript", "").strip()
        if not transcript:
            logger.warning(f"Empty transcript for {audio_path}, skipping")
            return "", {
                "entity_name": Path(audio_path).stem,
                "entity_type": "unknown",
                "summary": "",
                "chunk_count": 0,
                "audio_source_id": "",
            }

        segments = self._segment_with_overlap(asr_result)
        global_summary, entity_info = await self._recursive_mapreduce(
            segments, entity_name, item_info, audio_path, asr_result,
        )

        audio_source_id = asr_result.get("audio_sha256", "")[:12]
        entity_info["chunk_count"] = len(segments)
        entity_info["transcript_preview"] = transcript[:MAX_ENTITY_TRANSCRIPT_PREVIEW]
        entity_info["audio_sha256"] = asr_result.get("audio_sha256", "")
        entity_info["audio_source_id"] = audio_source_id

        if entity_info.get("entity_type", "") not in AUDIO_ENTITY_TYPE_ENUM:
            entity_info["entity_type"] = "unknown"

        modal_content["_audio_segments"] = segments
        modal_content["_asr_result"] = asr_result

        return global_summary, entity_info

    # ── transcript path ─────────────────────────────────

    def _transcript_path(self, audio_path: str) -> Path:
        output_dir = (getattr(self._config, "parser_output_dir", None)
                      if self._config else None)
        return Path(output_dir or ".") / f"{Path(audio_path).stem}.transcript.json"

    # ── cache I/O (lazy validation order) ─────────────────

    def _load_cached_transcript(self, audio_path: str) -> dict | None:
        tp = self._transcript_path(audio_path)
        if not tp.exists():
            return None
        try:
            cached = json.loads(tp.read_text(encoding="utf-8"))
        except Exception:
            return None
        if cached.get("schema_version") != CACHE_SCHEMA_VERSION:
            return None
        ap = Path(audio_path)
        if not ap.exists():
            return None
        if cached.get("audio_size") != ap.stat().st_size:
            return None
        if cached.get("audio_mtime") != ap.stat().st_mtime:
            return None
        if cached.get("audio_sha256") != _sha256(audio_path):
            return None
        if cached.get("asr_config_hash") != _asr_config_hash(self.asr_model_func):
            return None
        return cached

    async def _persist_transcript(self, asr_result: dict, audio_path: str):
        tp = self._transcript_path(audio_path)
        await asyncio.to_thread(lambda: tp.parent.mkdir(parents=True, exist_ok=True))
        await asyncio.to_thread(
            lambda: tp.write_text(
                json.dumps(asr_result, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        )

    # ── ASR (with cache orchestration) ───────────────────

    async def _load_or_transcribe(self, audio_path: str) -> dict:
        cached = self._load_cached_transcript(audio_path)
        if cached is not None:
            logger.info(f"Using cached transcript for {audio_path}")
            return cached

        pp_path = await asyncio.to_thread(
            self.preprocess_audio_func, audio_path,
            getattr(self._config, "parser_output_dir", ".") if self._config else ".",
        )
        asr_result = await self._transcribe(pp_path)

        asr_result["schema_version"] = CACHE_SCHEMA_VERSION
        asr_result["audio_sha256"] = await asyncio.to_thread(_sha256, audio_path)
        asr_result["audio_size"] = Path(audio_path).stat().st_size
        asr_result["audio_mtime"] = Path(audio_path).stat().st_mtime
        asr_result["asr_config_hash"] = _asr_config_hash(self.asr_model_func)
        asr_result["created_at"] = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())

        await self._persist_transcript(asr_result, audio_path)
        return asr_result

    async def _transcribe(self, audio_path: str) -> dict:
        async with self._asr_semaphore:
            timeout = (getattr(self._config, "audio_asr_timeout", 600)
                       if self._config else 600)
            if AsrCapability.ASYNC in self.asr_backend.capabilities:
                return await asyncio.wait_for(
                    self.asr_backend.atranscribe(audio_path),
                    timeout=timeout,
                )
            return await asyncio.wait_for(
                asyncio.to_thread(self.asr_backend.transcribe, audio_path),
                timeout=timeout,
            )

    # ── segment chunking (atomic, overlap, long-seg safe) ─

    def _estimate_tokens(self, text: str) -> int:
        if self._tokenizer is not None:
            return len(self._tokenizer.encode(text))
        return max(1, -(-len(text) // 3))  # ceil(len/3), CJK-safe

    def _build_chunk(self, segs: list, idx: int, total_dur: float) -> dict:
        mid = (segs[0]["start_time"] + segs[-1]["end_time"]) / 2.0
        rp = mid / total_dur if total_dur > 0 else 0.0
        return {
            "start_time": segs[0]["start_time"],
            "end_time": segs[-1]["end_time"],
            "text": " ".join(s["text"] for s in segs),
            "segment_index": idx,
            "source_segment_indices": [s["_original_index"] for s in segs],
            "confidence": (sum(s.get("confidence") or 0.0 for s in segs)
                           / max(len(segs), 1)),
            "has_low_confidence": any(s["is_low_confidence"] for s in segs),
            "speaker_labels": list({s.get("speaker_label")
                                    for s in segs if s.get("speaker_label")}),
            "relative_position": min(max(rp, 0.0), 1.0),
        }

    def _segment_with_overlap(self, asr_result: dict) -> list:
        raw = asr_result.get("segments", [])
        min_conf = (getattr(self._config, "min_asr_confidence", 0.0)
                    if self._config else 0.0)
        total_dur = max(asr_result.get("duration", 0.0), 0.01)

        segments = []
        for orig_idx, s in enumerate(raw):
            text = (s.get("text", "") or "").strip()
            segments.append({
                "start_time": s.get("start", 0.0),
                "end_time": s.get("end", 0.0),
                "text": text,
                "confidence": s.get("confidence"),
                "is_low_confidence": (s.get("confidence") or 0.0) < min_conf,
                "speaker_label": s.get("speaker_label"),
                "_original_index": orig_idx,
            })

        valid = [s for s in segments if s["text"]]
        if not valid:
            mp = total_dur / 2.0
            return [{
                "start_time": 0.0, "end_time": total_dur, "text": "",
                "segment_index": 0, "source_segment_indices": [0],
                "is_low_confidence": False, "confidence": 0.0,
                "relative_position": min(max(mp / total_dur, 0.0), 1.0),
                "speaker_labels": [],
            }]

        limit = (getattr(self._config, "audio_chunk_token_size", 600)
                 if self._config else 600)
        merged, buf_segs, buf_tokens, seg_idx = [], [], 0, 0

        for seg in valid:
            seg_tok = self._estimate_tokens(seg["text"])

            if seg_tok > limit:
                if buf_segs:
                    merged.append(self._build_chunk(buf_segs, seg_idx, total_dur))
                    seg_idx += 1
                    overlap = buf_segs[-1]
                    buf_segs = [overlap]
                    buf_tokens = self._estimate_tokens(overlap["text"])
                merged.append(self._build_chunk([seg], seg_idx, total_dur))
                logger.warning(
                    f"Single segment exceeds token limit ({seg_tok} > {limit}), kept as-is"
                )
                seg_idx += 1
                overlap = seg
                buf_segs = [overlap]
                buf_tokens = seg_tok
                continue

            if buf_segs and buf_tokens + seg_tok > limit:
                merged.append(self._build_chunk(buf_segs, seg_idx, total_dur))
                seg_idx += 1
                overlap = buf_segs[-1]
                buf_segs = [overlap]
                buf_tokens = self._estimate_tokens(overlap["text"])

            buf_segs.append(seg)
            buf_tokens += seg_tok

        if buf_segs:
            merged.append(self._build_chunk(buf_segs, seg_idx, total_dur))

        return merged

    # ── recursive MapReduce summarization ────────────────

    def _token_truncate(self, text: str, max_tokens: int) -> str:
        """Truncate text to ~max_tokens using tokenizer or CJK-safe char estimate."""
        if self._tokenizer is not None:
            tokens = self._tokenizer.encode(text)
            if len(tokens) <= max_tokens:
                return text
            return self._tokenizer.decode(tokens[:max_tokens])
        limit = max_tokens * 3
        return text[:limit] if len(text) > limit else text

    async def _recursive_mapreduce(self, segments, entity_name, item_info,
                                   audio_path, asr_result):
        batch_size = (getattr(self._config, "audio_summarize_batch_size", 8)
                      if self._config else 8)
        max_batches = (getattr(self._config, "audio_summarize_max_batches", 20)
                       if self._config else 20)
        file_name = Path(audio_path).name
        n_segs = len(segments)

        limit = max_batches * batch_size
        batches = []
        if n_segs <= limit:
            for i in range(0, n_segs, batch_size):
                batches.append(segments[i:i + batch_size])
        else:
            head_end = (max_batches - 1) * batch_size
            for i in range(0, head_end, batch_size):
                batches.append(segments[i:i + batch_size])
            batches.append(segments[head_end:])
            logger.info(
                f"Audio has {n_segs} segments; last batch absorbs {n_segs - head_end} segments"
            )

        # NOTE: group_summary is written into segment dicts by mutation. These dicts
        # are freshly created by _segment_with_overlap — no external shared state.

        async def summarize_batch(batch, batch_idx):
            seg_text = "\n\n".join(
                f"[{s['start_time']:.0f}s–{s['end_time']:.0f}s] {s['text']}"
                for s in batch
            )
            prompt = PROMPTS["audio_group_summary_prompt"].format(
                text=seg_text,
                start_time=batch[0]["start_time"],
                end_time=batch[-1]["end_time"],
                file_name=file_name,
                batch_index=batch_idx,
            )
            try:
                response = await self.modal_caption_func(
                    prompt,
                    system_prompt="Summarize this section concisely. Output plain text, 3-5 lines.",
                )
                return {
                    "summary": self._strip_thinking_tags(response).strip(),
                    "seg_indices": batch_idx,
                }
            except Exception:
                logger.error(f"Batch {batch_idx} summarization failed")
                return {"summary": "", "seg_indices": batch_idx}

        results = await asyncio.gather(
            *[summarize_batch(b, i) for i, b in enumerate(batches)],
            return_exceptions=True,
        )
        group_summaries = [
            r for r in results
            if not isinstance(r, Exception) and r["summary"].strip()
        ]

        for bi, batch in enumerate(batches):
            gs = ""
            if not isinstance(results[bi], Exception):
                gs = (results[bi]["summary"] or "").strip()
            for s in batch:
                s["group_summary"] = gs

        MAX_REDUCE_TOKEN_BUDGET = 400
        summaries = [gs["summary"] for gs in group_summaries if gs["summary"].strip()]
        while len(summaries) > 1:
            batches_reduce = [
                summaries[i:i + batch_size]
                for i in range(0, len(summaries), batch_size)
            ]
            results_reduce = await asyncio.gather(*[
                self._reduce_batch(
                    [self._token_truncate(s, MAX_REDUCE_TOKEN_BUDGET) for s in b],
                    i, len(batches_reduce),
                )
                for i, b in enumerate(batches_reduce)
            ], return_exceptions=True)
            summaries = [
                r for r in results_reduce
                if not isinstance(r, Exception) and r.strip()
            ]

        reduced_text = summaries[0] if summaries else (
            " ".join(s["text"] for s in segments)
        )[:2000]
        global_summary, entity_info = await self._final_synthesize(
            reduced_text, entity_name, item_info, audio_path, asr_result,
            prompt_overrides=prompt_overrides,
        )
        return global_summary, entity_info

    async def _reduce_batch(self, summaries: list, batch_idx: int, total: int) -> str:
        joined = "\n\n".join(
            f"[{i}] {s}" for i, s in enumerate(summaries) if s.strip()
        )
        if not joined.strip():
            return ""
        prompt = PROMPTS["audio_reduce_prompt"].format(
            summaries=joined, batch_index=batch_idx, total_batches=total,
        )
        try:
            response = await self.modal_caption_func(
                prompt,
                system_prompt="Synthesize. Output plain text, 3-5 lines.",
            )
            return self._strip_thinking_tags(response).strip()
        except Exception:
            return joined[:2000]

    async def _final_synthesize(self, reduced_text, entity_name, item_info,
                                audio_path, asr_result, prompt_overrides=None):
        file_name = Path(audio_path).name
        context = self._get_context_for_item(item_info) if item_info else ""

        # KB 主解析提示词覆盖 audio_global_prompt（无 _with_context 变体，has_context=False）
        _tmpl = _pick_prompt(
            prompt_overrides, "audio_global_prompt", False,
            PROMPTS["audio_global_prompt"], PROMPTS["audio_global_prompt"],
        )
        prompt = _safe_format(
            _tmpl,
            entity_name=entity_name or f"Audio content from {file_name}",
            reduced_summary=reduced_text,
            file_name=file_name,
            duration=asr_result.get("duration", 0),
            language=asr_result.get("language", "unknown"),
            context=context,
            type_enum=", ".join(sorted(AUDIO_ENTITY_TYPE_ENUM)),
        )
        response = await self.modal_caption_func(
            prompt, system_prompt=PROMPTS["AUDIO_ANALYSIS_SYSTEM"],
        )
        parsed = self._robust_json_parse(response)

        global_summary = parsed.get("detailed_description", reduced_text[:500])
        entity_info = parsed.get("entity_info", {
            "entity_name": Path(audio_path).stem,
            "entity_type": "unknown",
            "summary": global_summary[:200],
        })
        entity_info["duration"] = asr_result.get("duration", 0)
        entity_info["language"] = asr_result.get("language", "unknown")
        return global_summary, entity_info
