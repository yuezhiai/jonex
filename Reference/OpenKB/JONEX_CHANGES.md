# OpenKB 悦溪（jonex）改动点清单

> 本文件记录悦溪平台对 vendored OpenKB 源码的所有改动点，便于后续升级 OpenKB 时
> 快速定位、重新 apply。**所有改动均以 `# [jonex]` 注释标记**，可全局搜索 `[jonex]` 定位。

## 一、注释规范

- 单行改动：行尾加 `# [jonex]`。
- 代码块改动：块首加 `# ── [jonex] <说明> ───`，块尾可加 `# ── [jonex] end ───`。
- 新增参数/字段：行尾 `# [jonex]`，参数名统一加前缀 `_jonex_` 或 `X-Jonex-`（HTTP 头）。
- 新增整文件：文件头注释标明 `# [jonex] 悦溪新增文件`。

## 二、改动分类

改动分两类：
1. **计量上下文透传（metering）**：让 OpenKB 内部发起的 LLM 调用带上 `X-Jonex-*` 头，使 llm-gateway 能记录 tenant/kb/doc/scene/trace 维度。
2. **配置适配（config）**：支持 `OPENKB_LLM_*` 环境变量走 llm-gateway。

---

## 三、改动清单

### (A) 计量上下文透传（metering）

| 文件 | 位置 | 说明 |
|------|------|------|
| `openkb/jonex_metering.py` | **新增文件** | contextvar 存取 + `build_metering_headers()` — 供 adapter 层按请求注入 X-Jonex-* headers |

scene 取值枚举：

| scene 值 | 触发场景 |
|----------|---------|
| `openkb_compile` | Wiki 编译（摘要/概念/实体页生成） |
| `openkb_query` | 知识库查询 |
| `openkb_chat` | 多轮对话 |
| `openkb_lint` | 语义 Lint 检查 |

### (B) LLM Gateway 配置适配

| 文件 | 位置 | 说明 |
|------|------|------|
| `openkb/config.py` | `resolve_credential_bundle()` 函数内 | 新增 `OPENKB_LLM_BASE_URL` / `OPENKB_LLM_API_KEY` 环境变量优先于标准 `OPENAI_API_BASE` / `LLM_API_KEY` |

---

## 四、改动汇总

| 文件 | 改动类型 | 行数估计 |
|------|---------|---------|
| `openkb/jonex_metering.py` | **新建** | ~70 |
| `openkb/config.py` | 修改 | ~8 |

**总计**：~80 行改动/新增。不修改 `api.py`、`compiler.py`。

---

## 五、agent/query.py — run_query 支持返回工具调用轨迹（2026-08-07）

- `run_query()` 新增 keyword-only 参数 `return_trace: bool = False`。
- `False`（默认）：行为与上游完全一致，返回 `str`。CLI / chat 路径不受影响。
- `True`（仅非流式）：返回 `(answer, trace)`，`trace` 是 agent 的 wiki 浏览轨迹（工具名 + 参数 + 输出预览），供 Jonex `/search/llmwiki` 渲染推理链。
- 新增模块级 helper `_extract_tool_trace(result)`，从 Agents SDK `RunResult.new_items` 提取 `tool_call_item` / `tool_call_output_item`。
- 配对复用已有的 `_resolve_tool_call_id()`（优先 call_id、回退 id），与 `iter_agent_response_events` 的 pending_calls 口径一致 — LiteLLM 路径的 output item 只有 id，按相邻顺序或纯 call_id 配对都会错。
- `get_image` 的输出不记录内容（base64 体积），只保留 `<image>` 占位与参数中的路径。
- 工具输出预览截断至 200 字符（`_TRACE_PREVIEW_LIMIT`）。
- trace 提取整体 try/except 兜底：失败时返回已收集的部分并打 warning 日志，绝不影响 answer 返回。为此新增模块级 `logger = logging.getLogger(__name__)`（该文件原先没有 logger）。
- **2026-08-10 语言**：compiler/query/linter 的 system prompt 从 `"Write all content in {language} language."` 改为 `"Write all content in the same language as the source document."`，不再依赖 config.yaml 的 `language` 字段。原文中文→输出中文，原文英文→输出英文。
- **2026-08-07 重构**：`_extract_tool_trace` → `_extract_run_trace`，产出改为按轮分组（`turns`），每轮含 `thinking`（从 `reasoning_item`/`message_output_item` 提取）、`llm_ms`（LLM 耗时）、`calls`（工具调用）。新增 `_TurnTimingHooks`（`RunHooks`）采集 `on_llm_start`/`on_llm_end` 耗时。新增 `_THINKING_PREVIEW_LIMIT=300`。新增 `_extract_thinking` helper（D16：最后一轮 message 与 final_output 一致则跳过）。D8 截断改为保留所有轮 + 只截 calls。`Runner.run` 传入 `hooks=hooks`（仅 `return_trace=True` 时）。
