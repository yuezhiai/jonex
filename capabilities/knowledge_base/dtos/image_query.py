"""以图问知识：图片转查询 DTO（不含 tenant_id）。

设计：docs/knowledge-base/image-query-search-execution-plan.md §4-B2

**为什么不复用检索 DTO**：本方案选了「两步式」——本组 DTO 只负责
「图片 → 查询文本」，检索仍走现有接口，所以检索那 4 个 DTO、必填表、
埋点一行都不用动。

⚠️ **入参校验的真身在这里**，不在 `capability.py` 的 `_REQUIRED_FIELDS`：
那张表由 `validate_input` 消费，而该方法在 KB 的调用路径上没有调用方
（`base.py` 只提供钩子）。`ImageQueryService` 里 `ImageQueryRequest(**data)`
才算数。

⚠️ **校验失败怎么冒出去**：pydantic 的 `@validator` 里 `raise ValueError`
会被 pydantic 包成 `ValidationError`，由 `KnowledgeBaseCapability.execute`
统一转 400「参数校验失败」——**这是本项目既有惯例**，不要在这里自己转
`InvalidParameterError`（那样会丢掉字段级信息，也与其它 DTO 不一致）。
业务性的拒绝（张数超限、格式不支持等）才由 service 抛 `InvalidParameterError`。
"""

from typing import List, Optional

try:
    from pydantic.v1 import BaseModel, Field, validator
except ImportError:  # pragma: no cover
    from pydantic import BaseModel, Field, validator


#: 允许的图片 MIME。**按 magic bytes 判定后的真实类型**落在这里，
#: 不信任上传方声明的 Content-Type。有意不含 SVG —— 它是可执行 XML，
#: 同源 inline 渲染有 XSS 面（Gateway 的 `_EXECUTABLE_MIME` 也是这个口径）。
ALLOWED_IMAGE_MIME = frozenset(
    {"image/jpeg", "image/png", "image/webp", "image/gif", "image/bmp"}
)


class ImageQueryImageIn(BaseModel):
    """单张图片：Gateway 已读字节并 base64 编码。"""

    image_base64: str = Field(..., min_length=1)  # 纯 base64，不带 data: 前缀
    mime_type: str = Field(..., max_length=64)    # 声明值，service 按 magic bytes 复核
    file_size: int = Field(..., ge=1)

    @validator("mime_type")
    def _normalize_mime(cls, v: str) -> str:
        v = (v or "").split(";", 1)[0].strip().lower()
        if v not in ALLOWED_IMAGE_MIME:
            raise ValueError("unsupported image type")
        return v

    class Config:
        extra = "forbid"


class ImageQueryRequest(BaseModel):
    """invoke data；不含 tenant_id。张数上限在 service 里按配置再核一次。

    DTO 上的 ``max_items=8`` 只是**报文层面的粗挡**（防超大数组打进来），
    真正的上限是 ``IMAGE_QUERY_MAX_IMAGES``（默认 4），由 service 按配置判 ——
    这样运维改配置不必改代码。
    """

    images: List[ImageQueryImageIn] = Field(..., min_items=1, max_items=8)
    question: Optional[str] = Field(default=None, max_length=500)

    @validator("question")
    def _strip_question(cls, v: Optional[str]) -> Optional[str]:
        v = (v or "").strip()
        return v or None

    class Config:
        extra = "forbid"  # 拒绝夹带 tenant_id 等未声明字段


class ImageQueryImageOut(BaseModel):
    """单张图片的识别结果。"""

    index: int                      # 对应请求里的顺序
    status: str                     # "ok" / "failed"
    error_code: Optional[int] = None
    image_id: Optional[str] = None  # 保存历史时回传；失败 / 暂存失败 / 开关关闭时为 None
    description: str = ""
    ocr_text: str = ""
    entities: List[str] = Field(default_factory=list)
    cached: bool = False            # 识别结果来自缓存或已持久化记录
    persisted: bool = False         # image_id 是否已是持久化图片（同图复用）


class ImageQueryResponse(BaseModel):
    query: str                      # 合并后直接用于检索的文本
    images: List[ImageQueryImageOut]
    entities: List[str] = Field(default_factory=list)  # 合并去重后的实体
    model: str = ""
    partial: bool = False           # 是否有图片识别失败
    duration_ms: int = 0
    # 后端生效中的限制值，前端缓存后以此为准：
    # {"max_images": 4, "max_bytes": 5242880, "max_total_bytes": 12582912}
    limits: dict = Field(default_factory=dict)


class QueryImageRef(BaseModel):
    """历史条目上的图片引用。

    ``view_url`` 由 **Gateway** 补签（service 不产生）—— 签票属于接口层的事，
    且 service 不知道外部访问地址长什么样。
    """

    id: str
    mime_type: str
    file_size: int
    view_url: Optional[str] = None


__all__ = [
    "ALLOWED_IMAGE_MIME",
    "ImageQueryImageIn",
    "ImageQueryRequest",
    "ImageQueryImageOut",
    "ImageQueryResponse",
    "QueryImageRef",
]
