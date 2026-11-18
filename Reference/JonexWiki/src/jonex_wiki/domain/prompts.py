import hashlib
import re
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from jonex_wiki.kernel.errors import ConflictError, ValidationError
from jonex_wiki.storage.models import PromptTemplateRow

# ──────────────────────────────────────────────
#  Prompt keys (two layers)
# ──────────────────────────────────────────────
#
# Layer 1 (PUBLIC_PROMPT_KEYS): user-overridable, exposed to the prompt API.
# Layer 2 (INTERNAL_PROMPT_KEYS): frozen internals; default text maintained in
# code and not surfaced for customization.
#
# Only compile_main is user-editable; source understanding retains internal compile prompts.

PUBLIC_PROMPT_KEYS = ("compile_main",)

INTERNAL_PROMPT_KEYS = ("compile_structured", "compile_triage")

PROMPT_KEYS = PUBLIC_PROMPT_KEYS + INTERNAL_PROMPT_KEYS

# compile_main 固定骨架：编译正确性的硬约束（身份/输出格式/页面路径/证据支撑），
# 不开放给用户自定义。用户可改的是追加在骨架之后的「编译偏好」suffix（存
# prompt_templates 的 compile_main 行，默认空串）。执行时 system prompt =
# COMPILE_MAIN_SKELETON + (suffix 非空 ? "\n\n" + suffix : "")。
COMPILE_MAIN_SKELETON = (
    "You are a rigorous wiki compiler. "
    "Return exactly the requested structured JSON object and nothing else. "
    "Wiki page paths are always lowercase ASCII, contain no spaces or leading "
    "slashes, and are slash-separated with at least a category and a file "
    "component ending in '.md' (for example 'atlas/overview.md'). "
    "Every claim must be SUPPORTED by its cited source evidence: use an exact "
    "source statement or minimal decontextualization, and never add an unsupported "
    "number, date, unit, negation, comparison, table relation, or derived fact; "
    "a derived fact must be explicitly computable from the cited table evidence. "
    "Use optional navigation targets when they help a reader continue. "
    "Preserve the source's relation wording; omit uncertain hints without "
    "discarding its facts."
)

DEFAULT_PROMPTS = {
    "compile_main": "",
    "compile_structured": (
        "You are a rigorous wiki compiler. "
        "Return exactly the requested structured JSON object and nothing else."
    ),
    "compile_triage": (
        "The normal Wiki compile returned an empty selection. Decide whether "
        "this passage genuinely needs no standalone Claim or whether Claim "
        "compilation should be retried. Accept source-only only for material "
        "such as a directory, disclaimer, header/footer, isolated continuation "
        "fragment, layout-only text, or content already fully covered. If the "
        "passage contains any independently useful factual statement, number, "
        "rule, condition, exception, entity relation, or table meaning, choose "
        "retry_claim_compilation. Never judge source-only merely because the "
        "first model response was empty. Give a concrete reason grounded in "
        "passage.text.\n\nPassage:\n{passage}\n"
    ),
}

# (必填变量, 允许变量)。必填集缺了会 422；允许集里的变量会被插值填充,
# 已有自定义模板不引用也照样通过校验(参见旧 `max_claims` 的处理)。
#
# Only compile_triage requires an interpolated passage.
VARIABLE_CONTRACT = {
    "compile_main": (frozenset(), frozenset()),
    "compile_structured": (frozenset(), frozenset()),
    "compile_triage": (frozenset({"passage"}), frozenset({"passage"})),
}

_VAR_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")


def validate_template(prompt_key: str, template: str) -> None:
    """spec §3.4 变量契约:未知变量/缺必填 → 422(列出非法变量)。"""
    if prompt_key not in VARIABLE_CONTRACT:
        raise ValidationError(f"未知 prompt_key: {prompt_key}")
    required, allowed = VARIABLE_CONTRACT[prompt_key]
    referenced = set(_VAR_RE.findall(template))
    unknown = referenced - allowed
    missing = required - referenced
    if unknown or missing:
        parts = []
        if unknown:
            parts.append(f"未知变量: {', '.join(sorted(unknown))}")
        if missing:
            parts.append(f"缺少必填变量: {', '.join(sorted(missing))}")
        raise ValidationError("模板变量契约违反;" + ";".join(parts))
    if len(template.encode("utf-8")) > 64 * 1024:
        raise ValidationError("模板超过 64KB 上限")


def template_sha(template: str) -> str:
    return hashlib.sha256(template.encode("utf-8")).hexdigest()[:16]


@dataclass
class ResolvedPrompt:
    source: str  # "default" | "custom"
    version: int | None  # default 为 None(spec §3.4)
    template_sha: str
    template: str


class PromptRepo:
    async def get(
        self, session: AsyncSession, tenant_id: str, kb_id: str, prompt_key: str
    ) -> PromptTemplateRow | None:
        return await session.get(PromptTemplateRow, (tenant_id, kb_id, prompt_key))

    async def upsert(
        self,
        session: AsyncSession,
        *,
        tenant_id,
        kb_id,
        prompt_key,
        template: str,
        expected_version: int,
    ) -> PromptTemplateRow:
        row = await self.get(session, tenant_id, kb_id, prompt_key)
        if row is None:
            if expected_version != 0:
                raise ConflictError(
                    f"expected_version={expected_version} 但该 prompt 不存在(新建传 0)"
                )
            row = PromptTemplateRow(
                tenant_id=tenant_id,
                kb_id=kb_id,
                prompt_key=prompt_key,
                template=template,
                version=1,
            )
            session.add(row)
        else:
            if row.version != expected_version:
                raise ConflictError(
                    f"expected_version={expected_version} 与当前版本 {row.version} 不匹配(他人已修改)"
                )
            row.template = template
            row.version += 1
        await session.flush()
        return row

    async def delete(self, session: AsyncSession, tenant_id, kb_id, prompt_key) -> bool:
        row = await self.get(session, tenant_id, kb_id, prompt_key)
        if row is None:
            return False
        await session.delete(row)
        await session.flush()
        return True


async def resolve_prompt(
    session: AsyncSession, tenant_id: str, kb_id: str, prompt_key: str
) -> ResolvedPrompt:
    row = await PromptRepo().get(session, tenant_id, kb_id, prompt_key)
    if row is not None:
        return ResolvedPrompt(
            "custom", row.version, template_sha(row.template), row.template
        )
    tpl = DEFAULT_PROMPTS[prompt_key]
    return ResolvedPrompt("default", None, template_sha(tpl), tpl)
