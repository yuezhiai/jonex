"""
Configuration classes for RAGAnything

Contains configuration dataclasses with environment variable support
"""

from dataclasses import dataclass, field
from typing import List
from lightrag.utils import get_env_value


@dataclass
class RAGAnythingConfig:
    """Configuration class for RAGAnything with environment variable support"""

    # Directory Configuration
    # ---
    working_dir: str = field(default=get_env_value("WORKING_DIR", "./rag_storage", str))
    """Directory where RAG storage and cache files are stored."""

    # Parser Configuration
    # ---
    parse_method: str = field(default=get_env_value("PARSE_METHOD", "auto", str))
    """Default parsing method for document parsing: 'auto', 'ocr', or 'txt'."""

    parser_output_dir: str = field(default=get_env_value("OUTPUT_DIR", "./output", str))
    """Default output directory for parsed content."""

    parser: str = field(default=get_env_value("PARSER", "mineru", str))
    """Parser selection: 'mineru', 'mineru_online', 'docling', or 'paddleocr'."""

    display_content_stats: bool = field(
        default=get_env_value("DISPLAY_CONTENT_STATS", True, bool)
    )
    """Whether to display content statistics during parsing."""

    # Multimodal Processing Configuration
    # ---
    enable_image_processing: bool = field(
        default=get_env_value("ENABLE_IMAGE_PROCESSING", True, bool)
    )
    """Enable image content processing."""

    enable_table_processing: bool = field(
        default=get_env_value("ENABLE_TABLE_PROCESSING", True, bool)
    )
    """Enable table content processing."""

    enable_equation_processing: bool = field(
        default=get_env_value("ENABLE_EQUATION_PROCESSING", True, bool)
    )
    """Enable equation content processing."""

    # Audio Processing Configuration
    # ---
    enable_audio_processing: bool = field(
        default=get_env_value("ENABLE_AUDIO_PROCESSING", True, bool)
    )
    """Enable audio content processing with ASR transcription."""

    audio_asr_timeout: int = field(
        default=get_env_value("AUDIO_ASR_TIMEOUT", 600, int)
    )
    """Maximum seconds to wait for ASR transcription per file."""

    max_parallel_asr: int = field(
        default=get_env_value("MAX_PARALLEL_ASR", 1, int)
    )
    """Maximum concurrent ASR transcription calls."""

    audio_chunk_token_size: int = field(
        default=get_env_value("AUDIO_CHUNK_TOKEN_SIZE", 600, int)
    )
    """Target token count per transcript segment chunk."""

    min_asr_confidence: float = field(
        default=get_env_value("MIN_ASR_CONFIDENCE", 0.0, float)
    )
    """Minimum ASR confidence; segments below this are marked low-confidence but not deleted."""

    audio_summarize_batch_size: int = field(
        default=get_env_value("AUDIO_SUMMARIZE_BATCH_SIZE", 8, int)
    )
    """Number of transcript segments to group per LLM summarization call."""

    audio_summarize_max_batches: int = field(
        default=get_env_value("AUDIO_SUMMARIZE_MAX_BATCHES", 20, int)
    )
    """Maximum number of summarization batches before tail absorption."""

    # ASR Backend Configuration
    # ---
    asr_binding: str = field(
        default=get_env_value("ASR_BINDING", "whisper", str)
    )
    """ASR backend selection: 'whisper' or 'openai_compatible'."""

    asr_model: str = field(
        default=get_env_value("ASR_MODEL", "large-v3", str)
    )
    """ASR model name."""

    asr_api_key: str = field(
        default=get_env_value("ASR_API_KEY", "", str)
    )
    """ASR API key for cloud services. Does not fall back to LLM key."""

    asr_base_url: str = field(
        default=get_env_value("ASR_BASE_URL", "", str)
    )
    """ASR API endpoint URL. Does not fall back to LLM host."""

    asr_backend_options: dict = field(default_factory=dict)
    """Backend-specific options (e.g. device, compute_type, download_root).

    Set programmatically when constructing RAGAnythingConfig.
    Example: RAGAnythingConfig(asr_backend_options={"device": "cuda"})
    """

    # Vision/VLM Binding Configuration
    # ---
    vision_binding_host: str = field(
        default=get_env_value("VISION_BINDING_HOST", "", str)
    )
    """VLM/Vision API endpoint URL. Falls back to LLM_BINDING_HOST if not set."""

    vision_binding_api_key: str = field(
        default=get_env_value("VISION_BINDING_API_KEY", "", str)
    )
    """VLM/Vision API key. Falls back to LLM_BINDING_API_KEY if not set."""

    # Video Processing Configuration
    # ---
    enable_video_processing: bool = field(
        default=get_env_value("ENABLE_VIDEO_PROCESSING", True, bool)
    )
    """Enable video content processing (audio transcription + keyframe extraction)."""

    video_keyframe_algorithm: str = field(
        default=get_env_value("VIDEO_KEYFRAME_ALGORITHM", "interval", str)
    )
    """Keyframe extraction algorithm: 'interval', 'scene', 'iframes', or 'hybrid'.

    - interval: uniform time-spacing (fastest, most predictable)
    - scene: ffprobe scene-change detection (content-aware)
    - iframes: I-frame positions from encoding metadata (natural cut points, no decode cost)
    - hybrid: I-frames + interval gap-fill (best of both: cheap metadata + coverage)
    """

    video_keyframe_interval: int = field(
        default=get_env_value("VIDEO_KEYFRAME_INTERVAL", 10, int)
    )
    """Extract a keyframe every N seconds (fallback interval / gap-fill spacing)."""

    video_max_frames: int = field(
        default=get_env_value("VIDEO_MAX_FRAMES", 50, int)
    )
    """Maximum number of keyframes to extract per video."""

    video_extract_audio: bool = field(
        default=get_env_value("VIDEO_EXTRACT_AUDIO", True, bool)
    )
    """Whether to extract and transcribe the audio track from video."""

    video_chunk_token_size: int = field(
        default=get_env_value("VIDEO_CHUNK_TOKEN_SIZE", 600, int)
    )
    """Target token size for video chunks."""

    video_summarize_batch_size: int = field(
        default=get_env_value("VIDEO_SUMMARIZE_BATCH_SIZE", 8, int)
    )
    """Batch size for MapReduce summarization."""

    video_summarize_max_batches: int = field(
        default=get_env_value("VIDEO_SUMMARIZE_MAX_BATCHES", 20, int)
    )
    """Maximum batches for MapReduce summarization."""

    max_parallel_vlm: int = field(
        default=get_env_value("MAX_PARALLEL_VLM", 2, int)
    )
    """Maximum concurrent VLM calls within a single video."""

    vlm_timeout: int = field(
        default=get_env_value("VLM_TIMEOUT", 60, int)
    )
    """Timeout in seconds for a single VLM call."""

    vlm_model_name: str = field(
        default=get_env_value("VLM_MODEL_NAME", "", str)
    )
    """VLM model name for cache key invalidation."""

    vlm_prompt_version: str = field(
        default=get_env_value("VLM_PROMPT_VERSION", "v1", str)
    )
    """VLM prompt template version for cache invalidation."""

    video_vlm_contextual_prompt: bool = field(
        default=get_env_value("VIDEO_VLM_CONTEXTUAL_PROMPT", False, bool)
    )
    """Whether VLM prompt includes temporal context from adjacent frames."""

    vlm_force_time_interval: bool = field(
        default=get_env_value("VLM_FORCE_TIME_INTERVAL", False, bool)
    )
    """Force time-interval sampling (skip scene detection)."""

    # COS Image Transport Configuration
    # ---
    cos_bucket: str = field(
        default=get_env_value("COS_BUCKET", "", str)
    )
    """COS bucket name. Empty disables COS transport."""

    cos_appid: str = field(
        default=get_env_value("COS_APPID", "", str)
    )
    """COS account appid. Required for domain construction."""

    cos_region: str = field(
        default=get_env_value("COS_REGION", "ap-guangzhou", str)
    )
    """COS region."""

    cos_secret_id: str = field(
        default=get_env_value("COS_SECRET_ID", "", str)
    )
    """COS secret ID. Optional if passing existing client."""

    cos_secret_key: str = field(
        default=get_env_value("COS_SECRET_KEY", "", str)
    )
    """COS secret key. Optional if passing existing client."""

    cos_key_prefix: str = field(
        default=get_env_value("COS_KEY_PREFIX", "rag_anything/video", str)
    )
    """COS object key prefix for uploaded frames."""

    cos_max_concurrent: int = field(
        default=get_env_value("COS_MAX_CONCURRENT", 5, int)
    )
    """Max concurrent COS uploads."""

    max_concurrent_video: int = field(
        default=get_env_value("MAX_CONCURRENT_VIDEO", 1, int)
    )
    """Global concurrency limit for video processing."""

    video_cache_max_entries: int = field(
        default=get_env_value("VIDEO_CACHE_MAX_ENTRIES", 100, int)
    )
    """Maximum cached video analysis entries before LRU eviction."""

    # Video Analysis Backend Selection
    # ---
    video_analysis_binding: str = field(
        default=get_env_value("VIDEO_ANALYSIS_BINDING", "local", str)
    )
    """Default video analysis backend binding. Built-in options: ``"local"``, ``"mps"``.

    - ``local`` — local pipeline (ASR + keyframes + VLM description)
    - ``mps``   — Tencent Cloud MPS VideoComprehension (cloud-based)

    When multiple backends are available, this setting controls which one
    is used as the default. Per-video override can be implemented via
    :meth:`VideoModalProcessor._select_backend`.
    """

    # MPS (Media Processing Service) Configuration
    # ---
    mps_secret_id: str = field(
        default=get_env_value("MPS_SECRET_ID", "", str)
    )
    """Tencent Cloud SecretId for MPS authentication."""

    mps_secret_key: str = field(
        default=get_env_value("MPS_SECRET_KEY", "", str)
    )
    """Tencent Cloud SecretKey for MPS authentication."""

    mps_region: str = field(
        default=get_env_value("MPS_REGION", "ap-guangzhou", str)
    )
    """MPS service region (e.g. ``ap-guangzhou``, ``ap-beijing``)."""

    mps_cos_bucket: str = field(
        default=get_env_value("MPS_COS_BUCKET", "", str)
    )
    """COS bucket name where video files are stored."""

    mps_cos_region: str = field(
        default=get_env_value("MPS_COS_REGION", "ap-guangzhou", str)
    )
    """COS bucket region."""

    mps_definition: int = field(
        default=get_env_value("MPS_DEFINITION", 33, int)
    )
    """MPS AI analysis template definition ID.
    33 = VideoComprehension (default)."""

    mps_timeout: int = field(
        default=get_env_value("MPS_TIMEOUT", 600, int)
    )
    """Maximum time (seconds) to wait for an MPS task to complete."""

    mps_poll_interval: int = field(
        default=get_env_value("MPS_POLL_INTERVAL", 3, int)
    )
    """Interval (seconds) between MPS task status polls."""

    mps_max_retries: int = field(
        default=get_env_value("MPS_MAX_RETRIES", 3, int)
    )
    """Maximum number of retries for transient DescribeTaskDetail failures."""

    mps_prompt_category: str = field(
        default=get_env_value("MPS_PROMPT_CATEGORY", "mps_video_understanding", str)
    )
    """Prompt template category name for MPS video understanding analysis."""

    # Batch Processing Configuration
    # ---
    max_concurrent_files: int = field(
        default=get_env_value("MAX_CONCURRENT_FILES", 1, int)
    )
    """Maximum number of files to process concurrently."""

    supported_file_extensions: List[str] = field(
        default_factory=lambda: [
            x.strip()
            for x in get_env_value(
                "SUPPORTED_FILE_EXTENSIONS",
                ".pdf,.jpg,.jpeg,.png,.bmp,.tiff,.tif,.gif,.webp,.doc,.docx,.ppt,.pptx,.xls,.xlsx,.txt,.md,.html,.htm,.xhtml",
                str,
            ).split(",")
        ]
    )
    """List of supported file extensions for batch processing."""

    recursive_folder_processing: bool = field(
        default=get_env_value("RECURSIVE_FOLDER_PROCESSING", True, bool)
    )
    """Whether to recursively process subfolders in batch mode."""

    # [jonex] 多模态描述并发度（scene=raganything_ingest）
    # ---
    max_parallel_multimodal: int = field(
        default=get_env_value("MAX_PARALLEL_MULTIMODAL", 4, int)
    )
    """[jonex] 单文档内多模态描述 LLM 调用的并发上限（scene=raganything_ingest）。

    HTTP 入库模式下 PipelineServices.lightrag=None，MultimodalStage 原先兜底恒为 2，
    把表格/公式/文本块描述的并发锁死。此字段替代硬编码兜底，可经环境变量
    MAX_PARALLEL_MULTIMODAL 调高以吃满上游 RPM 额度。

    注意：该信号量是「每文档」创建的，实际并发 ≈ WORKER_COUNT × max_parallel_multimodal，
    调高时需与上游限流（如 200 次/分钟）及 lightrag_extract/ontology 抽取的并发额度一并权衡。"""

    # [jonex] VLM 独立并发度（§image-refs 排查衍生：图片描述直连远端 VLM，
    # 不经 llm-gateway，单次可 100s+，与表格/公式的 2~3s LLM 快任务共用
    # max_parallel_multimodal 会 head-of-line blocking，且远端 VLM 槽位有限）
    # ---
    max_parallel_vlm: int = field(
        default=get_env_value("MAX_PARALLEL_VLM", 2, int)
    )
    """[jonex] 单文档内图片/视频关键帧 VLM 调用的并发上限（独立于 max_parallel_multimodal）。

    MultimodalStage 中 image 项只吃此信号量（不占通用信号量），video 项两者都吃
    （关键帧 VLM + MapReduce LLM）。远端 VLM 为 35B MoE thinking 模型，单图实测
    4~105s，槽位有限——并发过高会让排队请求超过驱动超时（默认 120s），触发
    ReadTimeout 重试风暴（详见 JONEX_CHANGES §39）。"""

    # Context Extraction Configuration
    # ---
    # [jonex] §image-refs F3：默认 1→2——覆盖「正文末尾提图、图在下一页」的
    # 常见跨 1~2 页排版；附录集中配图（>2 页）自然拿不到远处正文 context，
    # 描述保持纯视觉、rerank 分低被 TopN 淘汰（自然衰减，§14.5）。
    # env CONTEXT_WINDOW 未显式配置时才生效。
    context_window: int = field(default=get_env_value("CONTEXT_WINDOW", 2, int))
    """Number of pages/chunks to include before and after current item for context."""

    context_mode: str = field(default=get_env_value("CONTEXT_MODE", "page", str))
    """Context extraction mode: 'page' for page-based, 'chunk' for chunk-based."""

    max_context_tokens: int = field(
        default=get_env_value("MAX_CONTEXT_TOKENS", 2000, int)
    )
    """Maximum number of tokens in extracted context."""

    include_headers: bool = field(default=get_env_value("INCLUDE_HEADERS", True, bool))
    """Whether to include document headers and titles in context."""

    include_captions: bool = field(
        default=get_env_value("INCLUDE_CAPTIONS", True, bool)
    )
    """Whether to include image/table captions in context."""

    context_filter_content_types: List[str] = field(
        default_factory=lambda: [
            x.strip()
            for x in get_env_value("CONTEXT_FILTER_CONTENT_TYPES", "text", str).split(
                ","
            )
        ]
    )
    """Content types to include in context extraction (e.g., 'text', 'image', 'table')."""

    content_format: str = field(default=get_env_value("CONTENT_FORMAT", "minerU", str))
    """Default content format for context extraction when processing documents."""

    # Path Handling Configuration
    # ---
    use_full_path: bool = field(default=get_env_value("USE_FULL_PATH", False, bool))
    """Whether to use full file path (True) or just basename (False) for file references in LightRAG."""

    def __post_init__(self):
        """Post-initialization setup for backward compatibility"""
        # Support legacy environment variable names for backward compatibility
        legacy_parse_method = get_env_value("MINERU_PARSE_METHOD", None, str)
        if legacy_parse_method and not get_env_value("PARSE_METHOD", None, str):
            self.parse_method = legacy_parse_method
            import warnings

            warnings.warn(
                "MINERU_PARSE_METHOD is deprecated. Use PARSE_METHOD instead.",
                DeprecationWarning,
                stacklevel=2,
            )

    @property
    def mineru_parse_method(self) -> str:
        """
        Backward compatibility property for old code.

        .. deprecated::
           Use `parse_method` instead. This property will be removed in a future version.
        """
        import warnings

        warnings.warn(
            "mineru_parse_method is deprecated. Use parse_method instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        return self.parse_method

    @mineru_parse_method.setter
    def mineru_parse_method(self, value: str):
        """Setter for backward compatibility"""
        import warnings

        warnings.warn(
            "mineru_parse_method is deprecated. Use parse_method instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        self.parse_method = value
