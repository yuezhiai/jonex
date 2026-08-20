"""PipelineBuilder — flexible stage composition with insert/replace."""

from typing import List

from raganything.pipeline.base import Stage
from raganything.pipeline.stages import (
    AssetUploadStage,
    EntityExtractStage,
    EntityMergeStage,
    FileValidateStage,
    MultimodalChunkStage,
    MultimodalStage,
    ParseStage,
    PushChunksStage,
    TextInsertStage,
)
from raganything.pipeline_mode import PipelineMode


class PipelineBuilder:
    """Build a DocumentPipeline with named stages, supporting insert and replace."""

    def __init__(self):
        self._stages: List[Stage] = []
        self._names: List[str] = []

    def add_stage(self, name: str, stage: Stage) -> "PipelineBuilder":
        self._stages.append(stage)
        self._names.append(name)
        return self

    def add_before(self, before_name: str, name: str, stage: Stage) -> "PipelineBuilder":
        idx = self._names.index(before_name)
        self._stages.insert(idx, stage)
        self._names.insert(idx, name)
        return self

    def add_after(self, after_name: str, name: str, stage: Stage) -> "PipelineBuilder":
        idx = self._names.index(after_name)
        self._stages.insert(idx + 1, stage)
        self._names.insert(idx + 1, name)
        return self

    def replace(self, old_name: str, name: str, stage: Stage) -> "PipelineBuilder":
        idx = self._names.index(old_name)
        self._stages[idx] = stage
        self._names[idx] = name
        return self

    def build(self):
        from raganything.pipeline import DocumentPipeline
        return DocumentPipeline(list(self._stages))


def create_default_pipeline(mode: PipelineMode = PipelineMode.STANDALONE):
    """Return the standard document processing pipeline.

    DocStatus is handled by EventBus + DocStatusListener, not a pipeline stage.
    """
    return (
        PipelineBuilder()
        .add_stage("validate", FileValidateStage())
        .add_stage("parse", ParseStage())
        .add_stage("text_insert", TextInsertStage())
        .add_stage("multimodal", MultimodalStage(mode=mode))
        .add_stage("multimodal_chunk", MultimodalChunkStage())
        .add_stage("entity_extract", EntityExtractStage())
        .add_stage("entity_merge", EntityMergeStage())
        .build()
    )


def create_http_pipeline():
    """Return the HTTP-mode document processing pipeline.

    HTTP mode: validate → parse → multimodal → push_chunks
    Entity extraction and graph merge are delegated to :9621 server-side.

    Used when RAGAnything is initialized with an http_client (production path).
    """
    return (
        PipelineBuilder()
        .add_stage("validate", FileValidateStage())
        .add_stage("parse", ParseStage())
        .add_stage("multimodal", MultimodalStage(mode=PipelineMode.STANDALONE))
        # [jonex] §image-refs P1-2: 图片资产上传（multimodal 之后、push_chunks
        # 之前——aext 必须赶在 chunk 写入 file_source 前就位；best-effort，
        # RAG_ASSET_UPLOAD_ENABLED 关闭时为空转）
        .add_stage("asset_upload", AssetUploadStage())
        .add_stage("push_chunks", PushChunksStage())
        .build()
    )
