"""
Chinese (中文) prompt templates for multimodal content processing.

Provides Chinese-language prompt templates as an alternative to the default
English templates.  Users can activate these at process level by calling
``set_prompt_language("zh")`` from :mod:`raganything.prompt_manager`.

Addresses GitHub issue #85 — prompt language support.
"""

from __future__ import annotations
from typing import Any

PROMPTS_ZH: dict[str, Any] = {}

# System prompts for different analysis types
PROMPTS_ZH["IMAGE_ANALYSIS_SYSTEM"] = (
    "你是一位专业的图像分析专家。请提供详细、准确的描述。"
)
PROMPTS_ZH["IMAGE_ANALYSIS_FALLBACK_SYSTEM"] = (
    "你是一位专业的图像分析专家。请根据现有信息提供详细分析。"
)
PROMPTS_ZH["TABLE_ANALYSIS_SYSTEM"] = (
    "你是一位专业的数据分析师。请提供包含具体洞察的详细表格分析。"
)
PROMPTS_ZH["EQUATION_ANALYSIS_SYSTEM"] = "你是一位数学专家。请提供详细的数学分析。"
PROMPTS_ZH["GENERIC_ANALYSIS_SYSTEM"] = "你是一位专注于{content_type}内容的专业分析师。"

# Image analysis prompt template
PROMPTS_ZH["vision_prompt"] = """请详细分析这张图片，并以以下JSON结构提供回答：

{{
    "detailed_description": "用 1-3 句话客观描述图片内容要点，遵循以下指导：
    - 只描述图片中可见的内容，不推测、不解读创作意图
    - 识别主要对象、人物、文字和视觉元素（有把握才写）
    - 如涉及图表、图解等，包含关键数据或结构信息
    - 始终使用具体名称而非代词
    - 禁止使用'可能/看似/或许/似乎/大概'等推测措辞",
    "entity_info": {{
        "entity_name": "{entity_name}",
        "entity_type": "image",
        "summary": "图片内容及其重要性的简明摘要（不超过100字）"
    }}
}}

附加信息：
- 图片路径：{image_path}
- 标注：{captions}
- 脚注：{footnotes}

请专注于提供准确、详细的视觉分析，以便于知识检索。"""

# Image analysis prompt with context support
# [jonex] §image-refs F1（终稿，docs/image-reference-accuracy-fix-plan.md §18）：
# 从「纯视觉描述」改为「视觉描述 + 文档角色关联」，让图片描述携带文档主题
# 关键词，改善主题 query 下的 rerank 相关性。含 Review 修正 F-2（保留图表
# 指令）/F-3（名称关联限定「上下文明确指明对应关系」，结尾「自然保留」）。
PROMPTS_ZH[
    "vision_prompt_with_context"
] = """请结合上下文详细分析这张图片，并以以下JSON结构提供回答：

{{
    "detailed_description": "结合上下文用 1-3 句话描述图片内容及其在文档中的角色，遵循以下指导：
    - 第一句：客观描述图片中可见的主要内容（对象、人物、文字、数据；如涉及图表或图解，包含关键数据或结构信息）
    - 第二句：基于周围文本上下文，说明此图在文档中的角色（如：某人物/作品的配图、某主题的示例、某数据的可视化、某流程的图解）
    - 仅当上下文明确指明此图与某名称的对应关系（如'图为 XX 的作品''如图所示'等指代）时，在描述中使用该名称
    - 始终使用具体名称而非代词
    - 只描述有把握的内容，禁止推测
    - 禁止使用'可能/看似/或许/似乎/大概'等措辞",
    "entity_info": {{
        "entity_name": "{entity_name}",
        "entity_type": "image",
        "summary": "图片内容、所属主题及与文档上下文关系的简明摘要（不超过100字）"
    }}
}}

周围内容上下文：
{context}

图片详细信息：
- 图片路径：{image_path}
- 标注：{captions}
- 脚注：{footnotes}

若上下文已提供与图片相关的主题关键词，在描述中自然保留这些关键词。"""

# Image analysis prompt with deterministic anchor (assertive)
# [jonex] §image-refs E2（docs/image-reference-accuracy-fix-plan.md §27）：
# 锚点在 VLM 调用前确定后以「断言式」注入——"此图是 {anchor} 的配图"，
# 不给选择余地。低量化 VLM 对参考式指令（"此图可能属于…"）遵从度低，
# 断言式指令下描述围绕锚点主题展开；锚点来自 MinerU 结构化产物
# （cap/foot/col/heading），可信度足够。与 chunk 前缀注入双保险：
# 前缀确定性进 embedding，此处保证 VLM 描述与锚点语义一致。
PROMPTS_ZH[
    "vision_prompt_with_anchor"
] = """此图是 {anchor} 的配图。请描述图中可见内容，并以以下JSON结构提供回答：

{{
    "detailed_description": "用 1-3 句话客观描述图中可见内容要点，遵循以下指导：
    - 只描述图片中可见的内容，不推测、不解读创作意图
    - 识别主要对象、人物、文字和视觉元素（有把握才写）
    - 如涉及图表、图解等，包含关键数据或结构信息
    - 始终使用具体名称而非代词
    - 禁止使用'可能/看似/或许/似乎/大概'等推测措辞",
    "entity_info": {{
        "entity_name": "{entity_name}",
        "entity_type": "image",
        "summary": "图片内容及其与 {anchor} 关系的简明摘要（不超过100字）"
    }}
}}

本页内容上下文（仅作背景参考）：
{context}

图片详细信息：
- 图片路径：{image_path}
- 标注：{captions}
- 脚注：{footnotes}

请专注于提供准确、详细的视觉分析，以便于知识检索。"""

# Image analysis prompt with text fallback
PROMPTS_ZH["text_prompt"] = """根据以下图片信息提供分析：

图片路径：{image_path}
标注：{captions}
脚注：{footnotes}

{vision_prompt}"""

# Table analysis prompt template
# [jonex] §table-grid-v2 L4.2 收紧：必须列出主体名 + 列名清单 + 行数，
# 禁止统计性推断（臆测既无用又污染检索）。
# ⚠️ detailed_description 必须是**字符串**（要点用中文句号/分号平铺，
# 不得输出嵌套 JSON 子对象——否则解析失败整条 pipeline 崩溃）。
PROMPTS_ZH["table_prompt"] = """请分析此表格内容，并以以下JSON结构提供回答：

{{
    "detailed_description": "对表格的忠实描述，字符串类型（要点用中文句号或分号平铺，不得使用嵌套JSON结构）。必须包含：主体名称（表格标题或所属主体，如公司名/机构名/文档主题）；列名清单（完整列出所有列标题）；行数（数据行的数量）；关键数据点（始终使用原文的具体名称和数值，而非笼统引用）。禁止统计性推断、趋势猜测、对数据含义的臆测，只描述表格中实际存在的内容。",
    "entity_info": {{
        "entity_name": "{entity_name}",
        "entity_type": "table",
        "summary": "表格目的和关键发现的简明摘要（不超过100字）"
    }}
}}

表格信息：
图片路径：{table_img_path}
标题：{table_caption}
内容：{table_body}
脚注：{table_footnote}

请专注于从表格数据中提取有意义的洞察和关系。"""

# Table analysis prompt with context support
PROMPTS_ZH[
    "table_prompt_with_context"
] = """请结合上下文分析此表格内容，并以以下JSON结构提供回答：

{{
    "detailed_description": "对表格的忠实描述，字符串类型（要点用中文句号或分号平铺，不得使用嵌套JSON结构）。必须包含：主体名称（表格标题或所属主体，如公司名/机构名/文档主题）；列名清单（完整列出所有列标题）；行数（数据行的数量）；关键数据点（始终使用原文的具体名称和数值，而非笼统引用）；表格与周围内容的关系（仅在确有依据时说明）。禁止统计性推断、趋势猜测、对数据含义的臆测，只描述表格中实际存在的内容。",
    "entity_info": {{
        "entity_name": "{entity_name}",
        "entity_type": "table",
        "summary": "表格目的、关键发现及与周围内容关系的简明摘要（不超过100字）"
    }}
}}

周围内容上下文：
{context}

表格信息：
图片路径：{table_img_path}
标题：{table_caption}
内容：{table_body}
脚注：{table_footnote}

请专注于在上下文背景下从表格数据中提取有意义的洞察和关系。"""

# Equation analysis prompt template
PROMPTS_ZH["equation_prompt"] = """请分析此数学公式，并以以下JSON结构提供回答：

{{
    "detailed_description": "对公式的全面分析，包括：
    - 数学含义和解释
    - 变量及其定义
    - 使用的数学运算和函数
    - 应用领域和背景
    - 物理或理论意义
    - 与其他数学概念的关系
    - 实际应用或用例
    始终使用准确的数学术语。",
    "entity_info": {{
        "entity_name": "{entity_name}",
        "entity_type": "equation",
        "summary": "公式目的和重要性的简明摘要（不超过100字）"
    }}
}}

公式信息：
公式：{equation_text}
格式：{equation_format}

请专注于提供数学洞察和解释公式的重要性。"""

# Equation analysis prompt with context support
PROMPTS_ZH[
    "equation_prompt_with_context"
] = """请结合上下文分析此数学公式，并以以下JSON结构提供回答：

{{
    "detailed_description": "对公式的全面分析，包括：
    - 数学含义和解释
    - 在上下文中变量的定义
    - 使用的数学运算和函数
    - 基于周围材料的应用领域和背景
    - 物理或理论意义
    - 与上下文中提到的其他数学概念的关系
    - 实际应用或用例
    - 公式如何与更广泛的讨论或框架相关联
    始终使用准确的数学术语。",
    "entity_info": {{
        "entity_name": "{entity_name}",
        "entity_type": "equation",
        "summary": "公式目的、重要性及在上下文中作用的简明摘要（不超过100字）"
    }}
}}

周围内容上下文：
{context}

公式信息：
公式：{equation_text}
格式：{equation_format}

请专注于在更广泛的上下文中提供数学洞察和解释公式的重要性。"""

# Generic content analysis prompt template
PROMPTS_ZH["generic_prompt"] = """请分析此{content_type}内容，并以以下JSON结构提供回答：

{{
    "detailed_description": "对内容的全面分析，包括：
    - 内容结构和组织
    - 关键信息和元素
    - 组件之间的关系
    - 背景和重要性
    - 与知识检索相关的细节
    始终使用适合{content_type}内容的专业术语。",
    "entity_info": {{
        "entity_name": "{entity_name}",
        "entity_type": "{content_type}",
        "summary": "内容目的和要点的简明摘要（不超过100字）"
    }}
}}

内容：{content}

请专注于提取对知识检索有用的有意义信息。"""

# Generic content analysis prompt with context support
PROMPTS_ZH[
    "generic_prompt_with_context"
] = """请结合上下文分析此{content_type}内容，并以以下JSON结构提供回答：

{{
    "detailed_description": "对内容的全面分析，包括：
    - 内容结构和组织
    - 关键信息和元素
    - 组件之间的关系
    - 与周围内容相关的背景和重要性
    - 此内容如何与更广泛的讨论相联系或支持
    - 与知识检索相关的细节
    始终使用适合{content_type}内容的专业术语。",
    "entity_info": {{
        "entity_name": "{entity_name}",
        "entity_type": "{content_type}",
        "summary": "内容目的、要点及与周围上下文关系的简明摘要（不超过100字）"
    }}
}}

周围内容上下文：
{context}

内容：{content}

请专注于提取对知识检索有用的信息，并理解内容在更广泛上下文中的作用。"""

# Modal chunk templates
PROMPTS_ZH["image_chunk"] = """
图片内容分析：
图片路径：{image_path}
标注：{captions}
脚注：{footnotes}

视觉分析：{enhanced_caption}"""

PROMPTS_ZH["table_chunk"] = """表格分析：
图片路径：{table_img_path}
标题：{table_caption}
结构：{table_body}
脚注：{table_footnote}

分析：{enhanced_caption}"""

PROMPTS_ZH["equation_chunk"] = """数学公式分析：
公式：{equation_text}
格式：{equation_format}

数学分析：{enhanced_caption}"""

PROMPTS_ZH["generic_chunk"] = """{content_type}内容分析：
内容：{content}

分析：{enhanced_caption}"""

# Query-related prompts
PROMPTS_ZH["QUERY_IMAGE_DESCRIPTION"] = (
    "请简要描述这张图片的主要内容、关键元素和重要信息。"
)

PROMPTS_ZH["QUERY_IMAGE_ANALYST_SYSTEM"] = (
    "你是一位能准确描述图片内容的专业图像分析师。"
)

PROMPTS_ZH["QUERY_TABLE_ANALYSIS"] = """请分析以下表格数据的主要内容、结构和关键信息：

表格数据：
{table_data}

表格标题：{table_caption}

请简要总结表格的主要内容、数据特征和重要发现。"""

PROMPTS_ZH["QUERY_TABLE_ANALYST_SYSTEM"] = (
    "你是一位能准确分析表格数据的专业数据分析师。"
)

PROMPTS_ZH["QUERY_EQUATION_ANALYSIS"] = """请解释以下数学公式的含义和用途：

LaTeX公式：{latex}
公式标题：{equation_caption}

请简要说明这个公式的数学意义、应用场景和重要性。"""

PROMPTS_ZH["QUERY_EQUATION_ANALYST_SYSTEM"] = "你是一位能清晰解释数学公式的数学专家。"

PROMPTS_ZH[
    "QUERY_GENERIC_ANALYSIS"
] = """请分析以下{content_type}类型内容并提取其主要信息和关键特征：

内容：{content_str}

请简要总结此内容的主要特征和重要信息。"""

PROMPTS_ZH["QUERY_GENERIC_ANALYST_SYSTEM"] = (
    "你是一位能准确分析{content_type}类型内容的专业内容分析师。"
)

PROMPTS_ZH["QUERY_ENHANCEMENT_SUFFIX"] = (
    "\n\n请基于用户查询和提供的多模态内容信息，提供全面的回答。"
)

# ── Audio Processing Prompts (Chinese) ─────────────────────

PROMPTS_ZH["AUDIO_ANALYSIS_SYSTEM"] = (
    "你是一位专业的音频内容分析师。"
    "请从以下类型中选择entity_type：call, conversation, interview, lecture, meeting, podcast, unknown。"
    "提取关键信息、说话人、主题，以及（若存在）决策和行动项。"
)

PROMPTS_ZH["audio_group_summary_prompt"] = """请用3-5行总结此音频段落。
包括主要主题和关键要点。若存在，请包括：决策、行动项。

文件: {file_name} | 段落 [{start_time:.0f}s–{end_time:.0f}s]

转录文本:
{text}"""

PROMPTS_ZH["audio_reduce_prompt"] = """请综合这些段落摘要。保留关键事实。
若存在，请包括决策和行动项。输出3-5行。

批次 {batch_index}/{total_batches}:
{summaries}"""

PROMPTS_ZH["audio_global_prompt"] = """请基于综合摘要创建最终全局分析。提供JSON格式：

{{
    "detailed_description": "全面综合：主要主题、关键论点、决策/行动项（若存在）、说话人角色、整体意义",
    "entity_info": {{
        "entity_name": "{entity_name}",
        "entity_type": "必须从以下选择: {type_enum}",
        "summary": "简洁的100字摘要"
    }}
}}

文件: {file_name} | 时长: {duration}s | 语言: {language}

综合摘要:
{reduced_summary}
{context}"""

PROMPTS_ZH["audio_chunk"] = """## 音频段落 [{start_time:.0f}s–{end_time:.0f}s]
文件: {file_name} | 段落 {segment_index}/{total_segments}
语言: {language} | 位置: {relative_position:.0%}

### 转录文本:
{transcript}
"""

# ── Video Processing Prompts (Chinese) ─────────────────────

PROMPTS_ZH["video_chunk"] = """## 视频段落 [{start_time:.0f}s–{end_time:.0f}s]

- **文件**: {file_name}
- **段落**: {segment_index}/{total_segments}
- **语言**: {language}
- **关键帧**: {frame_count}

### 视觉上下文
{frame_descriptions}

### 转录文本
{transcript}
"""

PROMPTS_ZH["video_group_summary_prompt"] = """请用3-5行总结此视频段落。
包括主要主题和关键要点。若存在，请包括：决策、行动项。

文件: {file_name} | 段落 [{start_time:.0f}s–{end_time:.0f}s]

转录文本:
{text}"""

PROMPTS_ZH["video_reduce_prompt"] = """请综合这些段落摘要。保留关键事实。
若存在，请包括决策和行动项。输出3-5行。

批次 {batch_index}/{total_batches}:
{summaries}"""

PROMPTS_ZH["video_global_prompt"] = """请创建最终全局综合。提供JSON格式：

{{
    "detailed_description": "全面综合：主要主题、关键论点、决策/行动项（若存在）、说话人角色、整体意义",
    "entity_info": {{
        "entity_name": "{entity_name}",
        "entity_type": "必须从以下选择: {type_enum}",
        "summary": "简洁的100字摘要"
    }}
}}

文件: {file_name} | 时长: {duration}s | 语言: {language}
关键帧提取: {keyframe_count}

综合摘要:
{reduced_summary}
{context}"""
