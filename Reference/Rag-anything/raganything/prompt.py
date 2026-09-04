"""
Prompt templates for multimodal content processing

Contains all prompt templates used in modal processors for analyzing
different types of content (images, tables, equations, etc.)
"""

from __future__ import annotations
from collections.abc import ItemsView, Iterator, KeysView, ValuesView
from typing import Any


class PromptRegistry:
    """Stable prompt container with atomic snapshot swapping.

    Readers keep a reference to this object, while language switches replace the
    underlying prompt dictionary in one step via :meth:`swap`.
    """

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}

    def swap(self, prompts: dict[str, Any]) -> None:
        """Atomically replace the active prompt snapshot."""
        self._data = dict(prompts)

    def snapshot(self) -> dict[str, Any]:
        """Return a copy of the active prompt set."""
        return dict(self._data)

    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    def __setitem__(self, key: str, value: Any) -> None:
        self._data[key] = value

    def __delitem__(self, key: str) -> None:
        del self._data[key]

    def __contains__(self, key: object) -> bool:
        return key in self._data

    def __iter__(self) -> Iterator[str]:
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def keys(self) -> KeysView[str]:
        return self._data.keys()

    def items(self) -> ItemsView[str, Any]:
        return self._data.items()

    def values(self) -> ValuesView[Any]:
        return self._data.values()

    def __repr__(self) -> str:
        return f"PromptRegistry({self._data!r})"


PROMPTS = PromptRegistry()

# System prompts for different analysis types
PROMPTS["IMAGE_ANALYSIS_SYSTEM"] = (
    "You are an expert image analyst. Provide detailed, accurate descriptions."
)
PROMPTS["IMAGE_ANALYSIS_FALLBACK_SYSTEM"] = (
    "You are an expert image analyst. Provide detailed analysis based on available information."
)
PROMPTS["TABLE_ANALYSIS_SYSTEM"] = (
    "You are an expert data analyst. Provide detailed table analysis with specific insights."
)
PROMPTS["EQUATION_ANALYSIS_SYSTEM"] = (
    "You are an expert mathematician. Provide detailed mathematical analysis."
)
PROMPTS["GENERIC_ANALYSIS_SYSTEM"] = (
    "You are an expert content analyst specializing in {content_type} content."
)

# Image analysis prompt template
PROMPTS[
    "vision_prompt"
] = """Please analyze this image in detail and provide a JSON response with the following structure:

{{
    "detailed_description": "A concise (1-3 sentences) objective description of the image following these guidelines:
    - Describe only what is visible in the image; do not speculate or interpret intent
    - Identify main objects, people, text, and visual elements (only when confident)
    - Include key data or structure if relevant (charts, diagrams, etc.)
    - Always use specific names instead of pronouns
    - Do not use speculative wording such as \"possibly/likely/seemingly/perhaps/maybe\"",
    "entity_info": {{
        "entity_name": "{entity_name}",
        "entity_type": "image",
        "summary": "concise summary of the image content and its significance (max 100 words)"
    }}
}}

Additional context:
- Image Path: {image_path}
- Captions: {captions}
- Footnotes: {footnotes}

Focus on providing accurate, detailed visual analysis that would be useful for knowledge retrieval."""

# Image analysis prompt with context support
# [jonex] §image-refs F2 (final, mirrors §18 of
# docs/image-reference-accuracy-fix-plan.md): from "pure visual description"
# to "visual description + role in document", so descriptions carry document
# topic keywords and rerank better against topic queries. Includes review
# fixes F-2 (keep chart/data instruction) / F-3 (only use names when the
# context clearly identifies the correspondence; "naturally retain" ending).
PROMPTS[
    "vision_prompt_with_context"
] = """Please analyze this image in detail, considering the surrounding context. Provide a JSON response with the following structure:

{{
    "detailed_description": "A concise (1-3 sentences) description of the image and its role in the document, considering the surrounding context, following these guidelines:
    - First sentence: objectively describe what is visible in the image (objects, people, text, data); include key data or structure for charts or diagrams
    - Second sentence: based on the surrounding text context, state the role of this image in the document (e.g., illustration of a person/work, example of a topic, visualization of data, diagram of a process)
    - Only use a name from the context when the context clearly identifies this image as corresponding to that name (e.g., \"the image shows XX's work\", \"as shown in the figure\")
    - Always use specific names instead of pronouns
    - Only describe what you are confident about; do not speculate
    - Do not use speculative wording such as \"possibly/likely/seemingly/perhaps/maybe\"",
    "entity_info": {{
        "entity_name": "{entity_name}",
        "entity_type": "image",
        "summary": "concise summary of the image content, its topic, and relationship to surrounding context (max 100 words)"
    }}
}}

Context from surrounding content:
{context}

Image details:
- Image Path: {image_path}
- Captions: {captions}
- Footnotes: {footnotes}

If the context provides topic keywords related to the image, naturally retain these keywords in the description."""

# Image analysis prompt with deterministic anchor (assertive)
# [jonex] §image-refs E2 (mirrors §27 of
# docs/image-reference-accuracy-fix-plan.md): anchor is injected assertively
# ("This image is an illustration of {anchor}") — no room for the VLM to
# pick another topic. Low-quantization VLMs comply poorly with
# reference-style instructions ("this image may belong to..."). Anchor comes
# from MinerU structured output (cap/foot/col/heading), trustworthy enough.
# Works with chunk-prefix injection as a double guarantee: prefix lands in
# embedding deterministically; here the description stays semantically
# consistent with the anchor.
PROMPTS[
    "vision_prompt_with_anchor"
] = """This image is an illustration of {anchor}. Describe what is visible in the image and provide a JSON response with the following structure:

{{
    "detailed_description": "A concise (1-3 sentences) objective description of the image following these guidelines:
    - Describe only what is visible in the image; do not speculate or interpret intent
    - Identify main objects, people, text, and visual elements (only when confident)
    - Include key data or structure if relevant (charts, diagrams, etc.)
    - Always use specific names instead of pronouns
    - Do not use speculative wording such as \"possibly/likely/seemingly/perhaps/maybe\"",
    "entity_info": {{
        "entity_name": "{entity_name}",
        "entity_type": "image",
        "summary": "concise summary of the image content and its relation to {anchor} (max 100 words)"
    }}
}}

Context from the current page (background reference only):
{context}

Image details:
- Image Path: {image_path}
- Captions: {captions}
- Footnotes: {footnotes}

Focus on providing accurate, detailed visual analysis that would be useful for knowledge retrieval."""

# Image analysis prompt with text fallback
PROMPTS["text_prompt"] = """Based on the following image information, provide analysis:

Image Path: {image_path}
Captions: {captions}
Footnotes: {footnotes}

{vision_prompt}"""

# Table analysis prompt template
PROMPTS[
    "table_prompt"
] = """Please analyze this table content and provide a JSON response with the following structure:

{{
    "detailed_description": "A comprehensive analysis of the table including:
    - Table structure and organization
    - Column headers and their meanings
    - Key data points and patterns
    - Statistical insights and trends
    - Relationships between data elements
    - Significance of the data presented
    Always use specific names and values instead of general references.",
    "entity_info": {{
        "entity_name": "{entity_name}",
        "entity_type": "table",
        "summary": "concise summary of the table's purpose and key findings (max 100 words)"
    }}
}}

Table Information:
Image Path: {table_img_path}
Caption: {table_caption}
Body: {table_body}
Footnotes: {table_footnote}

Focus on extracting meaningful insights and relationships from the tabular data."""

# Table analysis prompt with context support
PROMPTS[
    "table_prompt_with_context"
] = """Please analyze this table content considering the surrounding context, and provide a JSON response with the following structure:

{{
    "detailed_description": "A comprehensive analysis of the table including:
    - Table structure and organization
    - Column headers and their meanings
    - Key data points and patterns
    - Statistical insights and trends
    - Relationships between data elements
    - Significance of the data presented in relation to surrounding context
    - How the table supports or illustrates concepts from the surrounding content
    Always use specific names and values instead of general references.",
    "entity_info": {{
        "entity_name": "{entity_name}",
        "entity_type": "table",
        "summary": "concise summary of the table's purpose, key findings, and relationship to surrounding content (max 100 words)"
    }}
}}

Context from surrounding content:
{context}

Table Information:
Image Path: {table_img_path}
Caption: {table_caption}
Body: {table_body}
Footnotes: {table_footnote}

Focus on extracting meaningful insights and relationships from the tabular data in the context of the surrounding content."""

# Equation analysis prompt template
PROMPTS[
    "equation_prompt"
] = """Please analyze this mathematical equation and provide a JSON response with the following structure:

{{
    "detailed_description": "A comprehensive analysis of the equation including:
    - Mathematical meaning and interpretation
    - Variables and their definitions
    - Mathematical operations and functions used
    - Application domain and context
    - Physical or theoretical significance
    - Relationship to other mathematical concepts
    - Practical applications or use cases
    Always use specific mathematical terminology.",
    "entity_info": {{
        "entity_name": "{entity_name}",
        "entity_type": "equation",
        "summary": "concise summary of the equation's purpose and significance (max 100 words)"
    }}
}}

Equation Information:
Equation: {equation_text}
Format: {equation_format}

Focus on providing mathematical insights and explaining the equation's significance."""

# Equation analysis prompt with context support
PROMPTS[
    "equation_prompt_with_context"
] = """Please analyze this mathematical equation considering the surrounding context, and provide a JSON response with the following structure:

{{
    "detailed_description": "A comprehensive analysis of the equation including:
    - Mathematical meaning and interpretation
    - Variables and their definitions in the context of surrounding content
    - Mathematical operations and functions used
    - Application domain and context based on surrounding material
    - Physical or theoretical significance
    - Relationship to other mathematical concepts mentioned in the context
    - Practical applications or use cases
    - How the equation relates to the broader discussion or framework
    Always use specific mathematical terminology.",
    "entity_info": {{
        "entity_name": "{entity_name}",
        "entity_type": "equation",
        "summary": "concise summary of the equation's purpose, significance, and role in the surrounding context (max 100 words)"
    }}
}}

Context from surrounding content:
{context}

Equation Information:
Equation: {equation_text}
Format: {equation_format}

Focus on providing mathematical insights and explaining the equation's significance within the broader context."""

# Generic content analysis prompt template
PROMPTS[
    "generic_prompt"
] = """Please analyze this {content_type} content and provide a JSON response with the following structure:

{{
    "detailed_description": "A comprehensive analysis of the content including:
    - Content structure and organization
    - Key information and elements
    - Relationships between components
    - Context and significance
    - Relevant details for knowledge retrieval
    Always use specific terminology appropriate for {content_type} content.",
    "entity_info": {{
        "entity_name": "{entity_name}",
        "entity_type": "{content_type}",
        "summary": "concise summary of the content's purpose and key points (max 100 words)"
    }}
}}

Content: {content}

Focus on extracting meaningful information that would be useful for knowledge retrieval."""

# Generic content analysis prompt with context support
PROMPTS[
    "generic_prompt_with_context"
] = """Please analyze this {content_type} content considering the surrounding context, and provide a JSON response with the following structure:

{{
    "detailed_description": "A comprehensive analysis of the content including:
    - Content structure and organization
    - Key information and elements
    - Relationships between components
    - Context and significance in relation to surrounding content
    - How this content connects to or supports the broader discussion
    - Relevant details for knowledge retrieval
    Always use specific terminology appropriate for {content_type} content.",
    "entity_info": {{
        "entity_name": "{entity_name}",
        "entity_type": "{content_type}",
        "summary": "concise summary of the content's purpose, key points, and relationship to surrounding context (max 100 words)"
    }}
}}

Context from surrounding content:
{context}

Content: {content}

Focus on extracting meaningful information that would be useful for knowledge retrieval and understanding the content's role in the broader context."""

# Modal chunk templates
PROMPTS["image_chunk"] = """
Image Content Analysis:
Image Path: {image_path}
Captions: {captions}
Footnotes: {footnotes}

Visual Analysis: {enhanced_caption}"""

PROMPTS["table_chunk"] = """Table Analysis:
Image Path: {table_img_path}
Caption: {table_caption}
Structure: {table_body}
Footnotes: {table_footnote}

Analysis: {enhanced_caption}"""

PROMPTS["equation_chunk"] = """Mathematical Equation Analysis:
Equation: {equation_text}
Format: {equation_format}

Mathematical Analysis: {enhanced_caption}"""

PROMPTS["generic_chunk"] = """{content_type} Content Analysis:
Content: {content}

Analysis: {enhanced_caption}"""

# Query-related prompts
PROMPTS["QUERY_IMAGE_DESCRIPTION"] = (
    "Please briefly describe the main content, key elements, and important information in this image."
)

PROMPTS["QUERY_IMAGE_ANALYST_SYSTEM"] = (
    "You are a professional image analyst who can accurately describe image content."
)

PROMPTS[
    "QUERY_TABLE_ANALYSIS"
] = """Please analyze the main content, structure, and key information of the following table data:

Table data:
{table_data}

Table caption: {table_caption}

Please briefly summarize the main content, data characteristics, and important findings of the table."""

PROMPTS["QUERY_TABLE_ANALYST_SYSTEM"] = (
    "You are a professional data analyst who can accurately analyze table data."
)

PROMPTS[
    "QUERY_EQUATION_ANALYSIS"
] = """Please explain the meaning and purpose of the following mathematical formula:

LaTeX formula: {latex}
Formula caption: {equation_caption}

Please briefly explain the mathematical meaning, application scenarios, and importance of this formula."""

PROMPTS["QUERY_EQUATION_ANALYST_SYSTEM"] = (
    "You are a mathematics expert who can clearly explain mathematical formulas."
)

PROMPTS[
    "QUERY_GENERIC_ANALYSIS"
] = """Please analyze the following {content_type} type content and extract its main information and key features:

Content: {content_str}

Please briefly summarize the main characteristics and important information of this content."""

PROMPTS["QUERY_GENERIC_ANALYST_SYSTEM"] = (
    "You are a professional content analyst who can accurately analyze {content_type} type content."
)

PROMPTS["QUERY_ENHANCEMENT_SUFFIX"] = (
    "\n\nPlease provide a comprehensive answer based on the user query and the provided multimodal content information."
)

# ── Audio Processing Prompts ────────────────────────────

PROMPTS["AUDIO_ANALYSIS_SYSTEM"] = (
    "You are an expert audio content analyst. "
    "Choose entity_type from: call, conversation, interview, lecture, meeting, podcast, unknown. "
    "Extract key information, speakers, topics, and, if present, decisions and action items."
)

PROMPTS["audio_group_summary_prompt"] = """Summarize this section of audio in 3-5 lines.
Include main topic and key points. Include, if present: decisions, action items.

File: {file_name} | Section [{start_time:.0f}s–{end_time:.0f}s]

Transcript:
{text}"""

PROMPTS["audio_reduce_prompt"] = """Synthesize these section summaries. Preserve key facts.
Include, if present: decisions and action items. Output 3-5 lines.

Batch {batch_index}/{total_batches}:
{summaries}"""

PROMPTS["audio_global_prompt"] = """Create a final global synthesis. Provide JSON:

{{
    "detailed_description": "Comprehensive synthesis: main topic, key arguments,
    decisions/action items (if present), speaker roles, overall significance",
    "entity_info": {{
        "entity_name": "{entity_name}",
        "entity_type": "MUST be one of: {type_enum}",
        "summary": "concise 100-word summary"
    }}
}}

File: {file_name} | Duration: {duration}s | Language: {language}

Consolidated Summary:
{reduced_summary}
{context}"""

PROMPTS["audio_chunk"] = """## Audio Segment [{start_time:.0f}s–{end_time:.0f}s]
File: {file_name} | Segment {segment_index}/{total_segments}
Language: {language} | Position: {relative_position:.0%}

### Transcript:
{transcript}
"""

# ── Video Processing Prompts ────────────────────────────

PROMPTS["video_chunk"] = """## Video Segment [{start_time:.0f}s–{end_time:.0f}s]

- **File**: {file_name}
- **Segment**: {segment_index}/{total_segments}
- **Language**: {language}
- **Keyframes**: {frame_count}

### Visual Context
{frame_descriptions}

### Transcript
{transcript}
"""

PROMPTS["video_group_summary_prompt"] = """Summarize this section of video in 3-5 lines.
Include main topic and key points. Include, if present: decisions, action items.

File: {file_name} | Section [{start_time:.0f}s–{end_time:.0f}s]

Transcript:
{text}"""

PROMPTS["video_reduce_prompt"] = """Synthesize these section summaries. Preserve key facts.
Include, if present: decisions and action items. Output 3-5 lines.

Batch {batch_index}/{total_batches}:
{summaries}"""

PROMPTS["video_global_prompt"] = """Create a final global synthesis. Provide JSON:

{{
    "detailed_description": "Comprehensive synthesis: main topic, key points,
    decisions/action items (if present), speaker roles, overall significance",
    "entity_info": {{
        "entity_name": "{entity_name}",
        "entity_type": "MUST be one of: {type_enum}",
        "summary": "concise 100-word summary"
    }}
}}

File: {file_name} | Duration: {duration}s | Language: {language}
Keyframes extracted: {keyframe_count}

Consolidated Summary:
{reduced_summary}
{context}"""
