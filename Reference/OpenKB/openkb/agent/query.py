"""Q&A agent for querying the OpenKB knowledge base."""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, AsyncIterator

from agents import Agent, RunHooks, Runner, ToolOutputImage, ToolOutputText, function_tool

from openkb.agent.tools import (
    artifact_event_from_write,
    get_wiki_page_content,
    read_wiki_file,
    read_wiki_image,
    write_kb_file,
)
from openkb.config import LlmCredentialBundle, resolve_model_settings
from openkb.schema import get_agents_md

logger = logging.getLogger(__name__)

MAX_TURNS = 50

_QUERY_INSTRUCTIONS_TEMPLATE = """\
You are OpenKB, a knowledge-base Q&A agent. You answer questions by searching the wiki.

{schema_md}

## Search strategy
1. Read index.md to see all documents and concepts with brief summaries.
   Each document is marked (short) or (pageindex) to indicate its type.
2. Read relevant summary pages (summaries/) for document overviews.
   Summaries may omit details — if you need more, follow the summary's
   `full_text` frontmatter field to the source (see step 4).
3. Read concept pages (concepts/) for cross-document synthesis.
4. For "who/what is X" questions about a specific named person, organization,
   place, or product, read the matching page in entities/ first.
5. When you need detailed source document content, each summary page has a
   `full_text` frontmatter field with the path to the original document content:
   - Short documents (doc_type: short): read_file with that path.
   - PageIndex documents (doc_type: pageindex): use get_page_content(doc_name, pages)
     with tight page ranges. The summary shows document tree structure with page
     ranges to help you target. Never fetch the whole document.
6. Source content may reference images. Short-doc .md pages link them
   note-relative (e.g. ![image](images/doc/file.png), resolved from
   wiki/sources/); long-doc JSON page metadata lists them wiki-root-relative
   (e.g. sources/images/doc/file.png). Pass either form as seen to the
   get_image tool — it accepts both.
7. Synthesize a clear, concise, well-cited answer grounded in wiki content.

Answer based only on wiki content. Be concise.
Before each tool call, output one short sentence explaining the reason.

If you cannot find relevant information, say so clearly.
"""


def build_query_agent(
    wiki_root: str,
    model: str,
    language: str = "en",
    bundle: "LlmCredentialBundle | None" = None,
) -> Agent:
    """Build and return the Q&A agent."""
    schema_md = get_agents_md(Path(wiki_root))
    instructions = _QUERY_INSTRUCTIONS_TEMPLATE.format(schema_md=schema_md)
    instructions += "\n\nIMPORTANT: Answer in the same language as the user's question."

    @function_tool
    def read_file(path: str) -> str:
        """Read a Markdown file from the wiki.
        Args:
            path: File path relative to wiki root (e.g. 'summaries/paper.md').
        """
        return read_wiki_file(path, wiki_root)

    @function_tool
    def get_page_content(doc_name: str, pages: str) -> str:
        """Get text content of specific pages from a PageIndex (long) document.
        Only use for documents with doc_type: pageindex. For short documents,
        use read_file instead.
        Args:
            doc_name: Document name (e.g. 'attention-is-all-you-need').
            pages: Page specification (e.g. '3-5,7,10-12').
        """
        return get_wiki_page_content(doc_name, pages, wiki_root)

    @function_tool
    def get_image(image_path: str) -> ToolOutputImage | ToolOutputText:
        """View an image from the wiki.

        Use when a question asks about a specific figure, chart, or diagram
        you'd need to see to answer accurately.

        Args:
            image_path: Image path as it appears in the content — either
                wiki-root-relative ('sources/images/doc/p1_img1.png') or
                note-relative as used in sources/ .md pages
                ('images/doc/p1_img1.png').
        """
        result = read_wiki_image(image_path, wiki_root)
        if result["type"] == "image":
            return ToolOutputImage(image_url=result["image_url"])
        return ToolOutputText(text=result["text"])

    from agents.model_settings import ModelSettings

    if bundle is not None:
        model_settings = {
            "parallel_tool_calls": (
                bundle.parallel_tool_calls if bundle.parallel_tool_calls_explicit else False
            ),
            "extra_headers": bundle.extra_headers or None,
            "extra_args": {"timeout": bundle.timeout} if bundle.timeout is not None else None,
        }
    else:
        model_settings = resolve_model_settings()

    return Agent(
        name="wiki-query",
        instructions=instructions,
        tools=[read_file, get_page_content, get_image],
        model=f"litellm/{model}",
        model_settings=ModelSettings(**model_settings),
    )


def _resolve_tool_call_id(raw_item: Any) -> str | None:
    """Resolve a tool call's correlation id exactly as the Agents SDK's
    ``ToolCallItem.call_id`` / ``ToolCallOutputItem.call_id`` property does:
    prefer ``call_id``, fall back to ``id``, dict-aware.

    The ChatCompletions/LiteLLM path emits only ``id`` (no ``call_id``) on the
    output item, so without the ``id`` fallback the key written on the
    ``tool_call`` side and the key read on the ``tool_call_output_item`` side
    disagree, ``pending_calls.pop`` misses, and the ``output/*.html`` artifact
    card silently never fires. Deriving the key with this one helper on BOTH
    sides keeps them aligned.
    """
    if isinstance(raw_item, dict):
        return raw_item.get("call_id") or raw_item.get("id")
    return getattr(raw_item, "call_id", None) or getattr(raw_item, "id", None)


# [jonex] 工具输出预览长度。图片工具不记内容，只记路径（见方案 D4/D5）。
_TRACE_PREVIEW_LIMIT = 200
# [jonex] 每轮思考文本预览长度（见方案 D15）。
_THINKING_PREVIEW_LIMIT = 300


def _extract_thinking(item: Any, final_output: str) -> str | None:
    """[jonex] 从 reasoning_item / message_output_item 提取思考文本。

    D16：最后一轮的 message_output_item 若与 final_output 一致，跳过。
    """
    raw = getattr(item, "raw_item", None)
    texts: list[str] = []
    if getattr(item, "type", "") == "reasoning_item":
        for s in getattr(raw, "summary", []) or []:
            t = getattr(s, "text", "") or ""
            if t:
                texts.append(t)
    elif getattr(item, "type", "") == "message_output_item":
        from agents import ItemHelpers
        try:
            t = ItemHelpers.text_message_output(item)
            if t:
                # D16: 压缩空白后与 final_output 比较
                compressed = " ".join(t.split())
                final_compressed = " ".join((final_output or "").split())
                if compressed == final_compressed or final_compressed.startswith(compressed):
                    return None
                texts.append(t)
        except Exception:
            return None
    if not texts:
        return None
    thinking = " ".join(" ".join(texts).split())
    if len(thinking) <= _THINKING_PREVIEW_LIMIT:
        return thinking
    return thinking[:_THINKING_PREVIEW_LIMIT - 1] + "…"


def _extract_run_trace(result: Any, llm_durations: list[float]) -> dict:
    """[jonex] 从 Agents SDK RunResult 提取按轮分组的 wiki 浏览轨迹。

    返回 {"turns": [...], "turn_count": N, "llm_total_ms": ...}。
    每轮: {"index": N, "thinking": "...", "llm_ms": ..., "calls": [...]}。
    """
    import json

    final_output: str = getattr(result, "final_output", "") or ""
    items = getattr(result, "new_items", []) or []
    turns: list[dict] = []
    index: dict[str, dict] = {}
    current_turn: dict | None = None

    def _new_turn() -> dict:
        t: dict = {"index": len(turns), "thinking": None, "llm_ms": None, "calls": []}
        turns.append(t)
        return t

    try:
        prev_type: str = ""
        for item in items:
            itype = getattr(item, "type", "")
            raw = getattr(item, "raw_item", None)

            # ── 轮次切分（D13）──
            if itype in ("reasoning_item", "message_output_item", "tool_call_item") and prev_type == "tool_call_output_item":
                current_turn = _new_turn()
            if current_turn is None:
                current_turn = _new_turn()
            prev_type = itype

            # ── thinking（D15/D16）──
            if itype in ("reasoning_item", "message_output_item"):
                thinking_text = _extract_thinking(item, final_output)
                if thinking_text:
                    existing = current_turn.get("thinking") or ""
                    current_turn["thinking"] = (existing + " " + thinking_text).strip() if existing else thinking_text

            # ── tool_call_item（配对同 D4）──
            elif itype == "tool_call_item":
                raw_args = getattr(raw, "arguments", "") or ""
                try:
                    args = json.loads(raw_args) if raw_args else {}
                except (ValueError, TypeError):
                    args = {"_raw": str(raw_args)[:_TRACE_PREVIEW_LIMIT]}
                entry = {"tool": getattr(raw, "name", "?"), "args": args}
                current_turn["calls"].append(entry)
                call_id = _resolve_tool_call_id(raw)
                if isinstance(call_id, str) and call_id:
                    index[call_id] = entry

            elif itype == "tool_call_output_item":
                call_id = _resolve_tool_call_id(raw)
                entry = index.get(call_id) if isinstance(call_id, str) else None
                if entry is None:
                    continue
                if entry.get("tool") == "get_image":
                    entry["output_preview"] = "<image>"
                    continue
                text = " ".join(str(getattr(item, "output", "") or "").split())
                entry["output_chars"] = len(text)
                entry["output_preview"] = (
                    text if len(text) <= _TRACE_PREVIEW_LIMIT
                    else text[:_TRACE_PREVIEW_LIMIT - 1] + "…"
                )
    except Exception as exc:                      # noqa: BLE001
        logger.warning(
            "[jonex] run trace 提取失败（已降级，answer 不受影响）: %s: %s",
            type(exc).__name__, exc,
        )

    # ── 校验 + 对齐 llm 耗时（D14）──
    if len(turns) != len(llm_durations):
        logger.warning(
            "[jonex] turns=%d != llm_durations=%d，轮次切分与 LLM 调用次数不一致，llm_ms 全部置空",
            len(turns), len(llm_durations),
        )
    else:
        for i, ms in enumerate(llm_durations):
            turns[i]["llm_ms"] = int(ms * 1000)

    return {
        "turns": turns,
        "turn_count": len(turns),
        "llm_total_ms": int(sum(llm_durations) * 1000) if llm_durations else None,
    }


# ── [jonex] LLM 耗时采集 hooks（D14）──
class _TurnTimingHooks(RunHooks):
    """通过 RunHooks 采集每轮 LLM 调用耗时，写入外部列表。"""

    def __init__(self, durations_out: list[float]):
        self._durations = durations_out
        self._started: dict[int, float] = {}

    async def on_llm_start(self, context, agent, *args, **kwargs):
        self._started[id(context)] = time.time()

    async def on_llm_end(self, context, agent, output, *args, **kwargs):
        t0 = self._started.pop(id(context), None)
        if t0 is not None:
            self._durations.append(time.time() - t0)


async def iter_agent_response_events(
    agent: Agent,
    input_data: str | list[dict[str, Any]],
    *,
    max_turns: int = MAX_TURNS,
    run_config: Any = None,
) -> AsyncIterator[dict[str, Any]]:
    """Yield non-TTY events for a streamed agent response.

    The CLI renders these events to stdout; the REST API serializes the same
    events as SSE. Events: ``{"event": "delta", "data": {"text": ...}}`` for
    each response-text delta, ``{"event": "tool_call", "data": {...}}`` for
    tool invocations, and a final ``{"event": "final", "data": {"answer": ...,
    "history": [...]}}`` carrying the complete answer and reusable Agents SDK
    history.
    """
    from agents import RawResponsesStreamEvent, RunItemStreamEvent
    from openai.types.responses import ResponseTextDeltaEvent

    result = (
        Runner.run_streamed(agent, input_data, max_turns=max_turns, run_config=run_config)
        if run_config
        else Runner.run_streamed(agent, input_data, max_turns=max_turns)
    )
    collected: list[str] = []
    pending_calls: dict[str, tuple[str, str]] = {}

    async for event in result.stream_events():
        if isinstance(event, RawResponsesStreamEvent):
            if isinstance(event.data, ResponseTextDeltaEvent):
                text = event.data.delta
                if text:
                    collected.append(text)
                    yield {"event": "delta", "data": {"text": text}}
        elif isinstance(event, RunItemStreamEvent):
            item = event.item
            if item.type == "tool_call_item":
                raw_item = item.raw_item
                name = getattr(raw_item, "name", "?")
                arguments = getattr(raw_item, "arguments", "") or ""
                call_id = _resolve_tool_call_id(raw_item)
                if call_id:
                    pending_calls[call_id] = (name, arguments)
                yield {"event": "tool_call", "data": {"name": name, "arguments": arguments}}
            elif item.type == "tool_call_output_item":
                raw_item = item.raw_item
                call_id = _resolve_tool_call_id(raw_item)
                name, arguments = (
                    pending_calls.pop(call_id, ("", "")) if isinstance(call_id, str) else ("", "")
                )
                payload = artifact_event_from_write(
                    name, arguments, str(getattr(item, "output", "") or "")
                )
                if payload is not None:
                    yield {"event": "artifact", "data": payload}

    answer = "".join(collected).strip()
    if not answer:
        answer = (result.final_output or "").strip()
    yield {
        "event": "final",
        "data": {
            "answer": answer,
            "history": result.to_input_list(),
        },
    }


def build_chat_agent(
    kb_dir: Path,
    model: str,
    language: str = "en",
    bundle: "LlmCredentialBundle | None" = None,
) -> Agent:
    """Build the chat agent: query agent + a write tool restricted to
    ``<kb>/wiki/explorations/**`` and ``<kb>/output/**`` + a ``ShellTool``
    advertising locally-installed Anthropic-style skills.

    This is the variant used by the interactive ``openkb chat`` REPL so users
    can iterate on generated artifacts (e.g. ``output/skills/<name>/``) via
    natural-language follow-ups without giving the agent unrestricted write
    access to the wiki.

    Skill discovery: ``openkb/agent/skills.scan_local_skills`` looks in
    ``<kb>/skills/``, ``~/.openkb/skills/``, ``~/.claude/skills/`` for
    ``SKILL.md`` files. Any found skill is exposed to the agent via
    ``ShellTool.environment.skills`` so the model can ``cat`` the skill body
    and follow its instructions when the user's request matches.
    """
    wiki_root = str(kb_dir / "wiki")
    kb_root = str(kb_dir)
    base = build_query_agent(wiki_root, model, language=language, bundle=bundle)

    @function_tool
    def write_file(path: str, content: str) -> str:
        """Write a text file under the KB.

        Allowed paths (relative to KB root):
          * ``wiki/explorations/**`` — chat-derived notes.
          * ``output/**``            — generator artifacts (skills, etc.).

        Any other path is rejected. Parent directories are created.

        Args:
            path: File path relative to KB root
                (e.g. ``"output/skills/demo/SKILL.md"``).
            content: Full text content to write (overwrites if file exists).
        """
        return write_kb_file(path, content, kb_root)

    extra_tools: list = [write_file]
    skill_instructions_addendum = ""

    # Skill discovery via function tools. The agents SDK has a richer
    # ``ShellTool``+``ShellToolLocalSkill`` mechanism for this, but those
    # are OpenAI Responses-API hosted tools; LiteLLM routes through
    # ChatCompletions which rejects hosted tools. So we use plain
    # ``function_tool`` primitives that work with any LiteLLM-routed model.
    from openkb.agent.skills import scan_local_skills

    skills = scan_local_skills(kb_dir)
    skill_index = {s["name"]: s for s in skills}

    if skill_index:
        skill_list_text = _format_skill_list(skills)

        @function_tool
        def list_skills() -> str:
            """List skills available in this environment.

            Returns a text catalog of installed Anthropic-style skills.
            Each entry has a name and a one-line description; use the
            description to decide whether the skill matches the user's
            request, then call ``read_skill(name)`` to load its body.
            """
            return skill_list_text

        @function_tool
        def read_skill(name: str) -> str:
            """Read a skill's ``SKILL.md`` body.

            Call this once you've decided a skill matches the user's
            request. The returned text is the full skill instructions
            (frontmatter stripped). Follow it as your working method
            and write outputs via the ``write_file`` tool.

            Args:
                name: skill name as listed by ``list_skills``.
            """
            entry = skill_index.get(name)
            if entry is None:
                return f"Unknown skill: {name!r}. Call list_skills() to see available skills."
            md_path = Path(entry["path"]) / "SKILL.md"
            try:
                text = md_path.read_text(encoding="utf-8")
            except OSError as exc:
                return f"Could not read {md_path}: {exc}"
            # Strip frontmatter, return body only.
            from openkb.agent.skills import _parse_frontmatter

            _, body = _parse_frontmatter(text)
            return body

        extra_tools.extend([list_skills, read_skill])

        # Build the prompt addendum listing skill names + descriptions
        # right inside the system prompt so the model sees them up front
        # and knows what to look for, even before deciding to call
        # list_skills(). This is the difference between "agent
        # eventually discovers skills" and "agent treats skill use as
        # the default for matching requests".
        skill_lines = []
        for s in skills:
            desc_one_line = " ".join(s["description"].split())
            skill_lines.append(f"- **{s['name']}** — {desc_one_line}")
        skill_instructions_addendum = (
            "\n\n## Available skills\n\n"
            "The following Anthropic-style skill packages are installed in "
            "this environment. **When a user request matches a skill's "
            "description (e.g. 'make a deck', 'generate slides', 'draft a "
            "report'), you MUST call `read_skill(name)` to load that "
            "skill's full instructions and follow them strictly** — do not "
            "freestyle the output format if a skill covers it.\n\n"
            + "\n".join(skill_lines)
            + "\n\nIf no listed skill matches the request, proceed with "
            "your default tools."
        )

    new_instructions = (base.instructions or "") + skill_instructions_addendum
    return base.clone(
        tools=[*base.tools, *extra_tools],
        instructions=new_instructions,
    )


def _format_skill_list(skills: list[dict[str, str]]) -> str:
    """Render the skill catalog as a compact text block for the agent."""
    if not skills:
        return "No skills installed."
    lines = [f"{len(skills)} skill(s) available:\n"]
    for s in skills:
        lines.append(f"- {s['name']}")
        # Indent description; keep it one paragraph so the agent reads it fast.
        desc = " ".join(s["description"].split())
        lines.append(f"    {desc}")
    lines.append("\nTo use a skill, call read_skill(name) and follow its instructions.")
    return "\n".join(lines)


async def run_query(
    question: str,
    kb_dir: Path,
    model: str,
    stream: bool = False,
    *,
    raw: bool = False,
    run_config: Any = None,
    bundle: LlmCredentialBundle | None = None,
    return_trace: bool = False,          # [jonex] 新增
) -> str | tuple[str, list[dict]]:
    """Run a Q&A query against the knowledge base.

    Args:
        question: The user's question.
        kb_dir: Root of the knowledge base.
        model: LLM model name.
        stream: If True, print response tokens to stdout as they arrive.
        raw: If True, write raw markdown source instead of rendering it
            (still keeps tool-call line styling).
        return_trace: [jonex] If True (non-streaming only), return
            (answer, trace) tuple where trace is a list of tool-call dicts.

    Returns:
        str when return_trace=False (default), or (str, list[dict]) when True.
    """
    import sys

    from agents import RawResponsesStreamEvent, RunItemStreamEvent
    from openai.types.responses import ResponseTextDeltaEvent

    from openkb.config import resolve_effective_config

    config = resolve_effective_config(kb_dir)[0]
    language: str = config.get("language", "en")

    wiki_root = str(kb_dir / "wiki")

    agent = build_query_agent(wiki_root, model, language=language, bundle=bundle)

    # [jonex] stream + return_trace 互斥：流式是 CLI 路径，不支持 trace
    if return_trace and stream:
        logger.warning("return_trace 在流式模式下不支持，已忽略")
        return_trace = False

    if not stream:
        llm_durations: list[float] = []
        hooks = _TurnTimingHooks(llm_durations) if return_trace else None
        if run_config:
            result = await Runner.run(agent, question, max_turns=MAX_TURNS, run_config=run_config, hooks=hooks)
        else:
            result = await Runner.run(agent, question, max_turns=MAX_TURNS, hooks=hooks)
        answer = result.final_output or ""
        if not return_trace:
            return answer
        # [jonex] 提取 agent 的 wiki 浏览轨迹（按轮分组 + 耗时），供 Jonex 侧渲染推理链。
        return answer, _extract_run_trace(result, llm_durations)

    import os

    use_color = sys.stdout.isatty() and not os.environ.get("NO_COLOR", "")

    from openkb.agent.chat import (
        _build_style,
        _fmt,
        _format_tool_line,
        _make_markdown,
        _make_rich_console,
    )

    style = _build_style(use_color)

    from rich.live import Live

    if use_color and not raw:
        console = _make_rich_console()
    else:
        console = None  # type: ignore[assignment]

    def _start_live() -> Live | None:
        if console is None:
            return None
        lv = Live(console=console, vertical_overflow="visible")
        lv.start()
        return lv

    live: Live | None = None
    last_was_text = False
    need_blank_before_text = False
    result = (
        Runner.run_streamed(agent, question, max_turns=MAX_TURNS, run_config=run_config)
        if run_config
        else Runner.run_streamed(agent, question, max_turns=MAX_TURNS)
    )
    collected: list[str] = []
    segment: list[str] = []
    try:
        live = _start_live()
        async for event in result.stream_events():
            if isinstance(event, RawResponsesStreamEvent):
                if isinstance(event.data, ResponseTextDeltaEvent):
                    text = event.data.delta
                    if text:
                        if need_blank_before_text:
                            if console is not None:
                                print()
                                segment = []
                                live = _start_live()
                            else:
                                sys.stdout.write("\n")
                            need_blank_before_text = False
                        collected.append(text)
                        segment.append(text)
                        last_was_text = True
                        if live:
                            if "\n" in text:
                                joined = "".join(segment)
                                visible = joined[: joined.rfind("\n") + 1]
                                if visible:
                                    live.update(_make_markdown(visible))
                        else:
                            sys.stdout.write(text)
                            sys.stdout.flush()
            elif isinstance(event, RunItemStreamEvent):
                item = event.item
                if item.type == "tool_call_item":
                    if last_was_text:
                        if live:
                            if segment:
                                live.update(_make_markdown("".join(segment)))
                            live.stop()
                            live = None
                        else:
                            sys.stdout.write("\n")
                            sys.stdout.flush()
                        last_was_text = False
                    raw_item = item.raw_item
                    name = getattr(raw_item, "name", "?")
                    args = getattr(raw_item, "arguments", "") or ""
                    if live:
                        live.stop()
                        live = None
                    _fmt(style, ("class:tool", _format_tool_line(name, args) + "\n"))
                    need_blank_before_text = True
                elif item.type == "tool_call_output_item":
                    pass
    finally:
        if live:
            if segment:
                live.update(_make_markdown("".join(segment)))
            live.stop()
        print()
    return "".join(collected) if collected else result.final_output or ""


def build_run_config_from_bundle(model: str, bundle: "LlmCredentialBundle | None") -> Any:
    """Build an Agents-SDK `RunConfig` from a credential bundle.

    When *bundle* is `None` (CLI path), returns `None` so the runner falls
    back to the default provider (process-wide `litellm.api_key` / env vars).
    When a bundle is supplied, a dedicated `LitellmModel` instance is created
    with the per-KB `api_key` and `base_url` so concurrent requests on the
    shared event-loop thread never read each other's credentials.

    The model is passed to `LitellmModel` *verbatim* (e.g. ``openai/gpt-4o``)
    because `LitellmModel` feeds it straight to ``litellm.acompletion``. The
    ``litellm/`` prefix is an Agent-layer convention to select the backend and
    must NOT be added here -- doing so yields ``litellm/openai/...`` which
    litellm rejects as an unknown provider.
    """
    if bundle is None:
        return None
    from agents import RunConfig
    from agents.extensions.models.litellm_model import LitellmModel

    litellm_model = LitellmModel(
        model=model,
        base_url=bundle.base_url,
        api_key=bundle.api_key,
    )
    return RunConfig(model=litellm_model)
