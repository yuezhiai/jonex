"""LLM-Wiki Schema DTO（Pydantic v1 兼容）。

方案：docs/llmwiki-schema-settings-execution-plan.md §4/§9
- code 必须小写 ASCII（[a-z0-9 _-]）：resolve_entity_types 对畸形词表静默回落
  DEFAULT_ENTITY_TYPES（仅 warning）——Jonex 侧保存时必须显式拒绝。
- entity_types 是「受控词表」：编译时超出词表的 type 静默回落 other（非拒绝）。
"""
import re

try:
    from pydantic.v1 import BaseModel, Field, validator
except ImportError:
    from pydantic import BaseModel, Field, validator

from typing import Literal, Optional

# code 合法字符集（与 OpenKB resolve_entity_types 的清洗规则一致：[a-z0-9 _-]）
_CODE_RE = re.compile(r"^[a-z0-9 _-]+$")

DEFAULT_ENTITY_TYPES = [
    {"code": "person", "name": "人物", "description": "自然人个体"},
    {"code": "organization", "name": "组织", "description": "公司、机构、部门等组织实体"},
    {"code": "place", "name": "地点", "description": "城市、区域、场所"},
    {"code": "product", "name": "产品", "description": "产品、平台、服务"},
    {"code": "work", "name": "作品/文档", "description": "书籍、论文、法规等命名作品"},
    {"code": "event", "name": "事件", "description": "有明确时间范围的事件"},
    {"code": "other", "name": "其他", "description": "无法归入以上类型的兜底类型"},
]

# [jonex] 自定义追加块默认示例（用户可改；渲染时原样拼到 AGENTS.md 末尾）
DEFAULT_AGENTS_MD_EXTRA = """## Compile Rules (软约束)
以下规则由模型自觉遵循，非硬性限制：
- 概念创建门槛：central_or_recurring
- 每篇文档最多创建 8 个概念页、15 个实体页
- 必须使用 [[wikilink]] 关联概念与实体
- 保留原文关键事实
- 允许跨文档综合
"""


class LlmWikiEntityType(BaseModel):
    code: str
    name: str
    description: str = ""
    examples: list[str] = Field(default_factory=list)

    @validator("code")
    def _code_ascii(cls, v: str) -> str:
        v = (v or "").strip()
        if not v or not _CODE_RE.match(v):
            raise ValueError(
                "entity type code 必须为非空小写 ASCII（[a-z0-9 _-]），"
                "如 person / organization"
            )
        return v


class LlmWikiConceptType(BaseModel):
    """概念类型词表项（无硬校验路径——概念页无 type frontmatter，
    OpenKB 代码不消费该词表；仅渲染进 AGENTS.md 作分类引导）。"""

    code: str
    name: str
    description: str = ""

    @validator("code")
    def _code_ascii(cls, v: str) -> str:
        v = (v or "").strip()
        if not v or not _CODE_RE.match(v):
            raise ValueError(
                "concept type code 必须为非空小写 ASCII（[a-z0-9 _-]）"
            )
        return v


class SaveLlmWikiSchemaRequest(BaseModel):
    knowledge_base_id: str = Field(..., min_length=1, max_length=128)
    expected_schema_version: int = Field(..., ge=0)
    schema_name: str = Field(default="default", max_length=128)
    language: str = Field(default="zh-CN", max_length=32)
    model: Optional[str] = Field(default=None, max_length=128)
    entity_types: list[LlmWikiEntityType] = Field(default_factory=lambda: [LlmWikiEntityType(**t) for t in DEFAULT_ENTITY_TYPES])
    # [jonex] 概念类型词表（默认空，用户自行添加——OpenKB 无概念硬校验，
    # 仅渲染进 AGENTS.md 作分类引导）
    concept_types: list[LlmWikiConceptType] = Field(default_factory=list)
    # [jonex] 用户自定义追加块（多行 markdown，渲染时原样拼到 AGENTS.md 末尾）
    agents_md_extra: str = Field(default="", max_length=50_000)


class ImportLlmWikiSchemaRequest(BaseModel):
    knowledge_base_id: str = Field(..., min_length=1, max_length=128)
    expected_schema_version: int = Field(..., ge=0)
    yaml_text: str = Field(..., min_length=1, max_length=200_000)
    dry_run: bool = False


class RecompileOutdatedRequest(BaseModel):
    knowledge_base_id: str = Field(..., min_length=1, max_length=128)
    only_outdated: bool = True
