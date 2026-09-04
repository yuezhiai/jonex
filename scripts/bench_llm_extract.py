"""deepseek-v4-flash 抽取侧吞吐压测脚本（临时基准工具）。

复刻 LightRAG 实体/关系抽取场景，向 OpenAI 兼容的 chat/completions 端点
在不同并发档位下发送"抽取型"请求，统计：
  · 成功率 / 429 限流数
  · 单请求端到端延迟（p50/p95/max）
  · prompt/completion token 数
  · 单请求 decode 吞吐（completion_tokens / latency, tok/s）
  · 整档聚合吞吐（req/s、completion tok/s）

⚠️ 该脚本会向计费上游发真实请求、消耗 token。默认档位与 max_tokens 已压小；
   如需更激进的压测请自行调大 BENCH_* 变量，注意成本与上游限流。

配置来源（优先级：显式环境变量 > deploy/.env > 内置默认）：
  BENCH_LLM_HOST   端点 base（含 /v1），默认取 .env 的 LLMGW_UPSTREAM_LLM_HOST，
                   缺省 https://tokenhub.tencentmaas.com/v1
  BENCH_LLM_KEY    Bearer key，默认取 .env 的 LLMGW_UPSTREAM_LLM_API_KEY
  BENCH_LLM_MODEL  模型名，默认 deepseek-v4-flash-202605
  BENCH_THINKING   off(默认，复刻抽取场景注入 thinking.disabled) | on
  BENCH_LEVELS     并发档位，默认 1,2,4,8
  BENCH_REQ        每档请求数，默认 8
  BENCH_MAXTOK     max_tokens，默认 1024

用法：
    uv run python scripts/bench_llm_extract.py
"""

import asyncio
import os
import statistics
import time
from pathlib import Path

import httpx

# ── 读取 deploy/.env（best-effort，仅取需要的键；不覆盖已存在的真实环境变量）──
_ENV_FILE = Path(__file__).resolve().parent.parent / "deploy" / ".env"


def _load_env_file() -> dict[str, str]:
    vals: dict[str, str] = {}
    try:
        for line in _ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            vals[k.strip()] = v.strip()
    except Exception:
        pass
    return vals


_ENV = _load_env_file()


def _cfg(explicit_key: str, env_key: str, default: str) -> str:
    return os.getenv(explicit_key) or _ENV.get(env_key) or default


HOST = _cfg("BENCH_LLM_HOST", "LLMGW_UPSTREAM_LLM_HOST", "https://tokenhub.tencentmaas.com/v1").rstrip("/")
API_KEY = _cfg("BENCH_LLM_KEY", "LLMGW_UPSTREAM_LLM_API_KEY", "")
MODEL = os.getenv("BENCH_LLM_MODEL", "deepseek-v4-flash-202605")
ENDPOINT = f"{HOST}/chat/completions"

THINKING_OFF = os.getenv("BENCH_THINKING", "off").lower() != "on"
CONCURRENCY_LEVELS = [int(x) for x in os.getenv("BENCH_LEVELS", "1,2,4,8").split(",")]
REQUESTS_PER_LEVEL = int(os.getenv("BENCH_REQ", "8"))
MAX_TOKENS = int(os.getenv("BENCH_MAXTOK", "1024"))

# 抽取阶段要识别的实体类型（与 deploy/.env.rag 的 ENTITY_TYPES 精神一致，取代表性子集）
ENTITY_TYPES = (
    "组织,人员,产品,技术,方法,系统,设备,模型,数据,功能,概念,流程,任务,项目,"
    "指标,标准,政策,风险,市场,行业,地点,时间"
)

# 一段代表性中文 chunk（~1200 字符，模拟入库文本）
CHUNK = (
    "Jonex 平台是一个插件化 AI 能力平台框架，通过可组合的 capability 对外提供业务服务。"
    "平台采用多租户架构，所有业务数据按 tenant_id 强隔离。知识库能力封装 RAG 检索，"
    "向量检索经 Milvus，本体图谱存储于 Neo4j，文档与对象走 MinIO 或腾讯云 COS。"
    "所有 LLM 与 Embedding 调用统一经 llm-gateway 出口，由网关完成 token 计量与限流，"
    "上游对接腾讯 tokenhub 的 deepseek-v4-flash 模型。前端采用微前端架构，shell 宿主"
    "通过 Module Federation 远程挂载核心业务、平台管理、生态管理、专家访谈等子应用。"
    "研发团队负责人张伟在 2026 年第二季度主导了检索召回链路的性能优化项目，"
    "将文档入库耗时从 49 分钟显著下降，主要手段包括 embedding 并发标定与批量入队改造。"
) * 2

# 抽取型 system/user prompt（要求输出 JSON 实体/关系，贴近 LightRAG 抽取任务形态）
SYSTEM_PROMPT = (
    "You are an information extraction assistant. Extract entities and relationships "
    "from the given Chinese text. Output ONLY a JSON object with two arrays: "
    '"entities" (each: name, type, description) and "relationships" '
    "(each: source, target, relation, description). Do not add extra commentary."
)
USER_PROMPT = (
    f"Entity types to consider: {ENTITY_TYPES}\n\n"
    f"Text:\n{CHUNK}\n\n"
    "Extract all entities and relationships as JSON."
)


def _build_payload() -> dict:
    payload: dict = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": USER_PROMPT},
        ],
        "temperature": 0.0,
        "max_tokens": MAX_TOKENS,
        "stream": False,
    }
    # 复刻抽取场景：注入 tokenhub 专有的 thinking 关闭格式（网关对抽取场景也是这么做的）
    if THINKING_OFF:
        payload["thinking"] = {"type": "disabled"}
    return payload


async def one_request(client: httpx.AsyncClient) -> dict:
    """返回单请求指标 dict。"""
    t0 = time.perf_counter()
    try:
        resp = await client.post(
            ENDPOINT,
            headers={"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"},
            json=_build_payload(),
        )
        dt = time.perf_counter() - t0
        if resp.status_code == 429:
            return {"ok": False, "dt": dt, "rate_limited": True, "err": "429 限流"}
        if resp.status_code != 200:
            return {"ok": False, "dt": dt, "err": f"HTTP {resp.status_code}: {resp.text[:160]}"}
        data = resp.json()
        usage = data.get("usage") or {}
        return {
            "ok": True,
            "dt": dt,
            "prompt_tokens": usage.get("prompt_tokens", 0),
            "completion_tokens": usage.get("completion_tokens", 0),
        }
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "dt": time.perf_counter() - t0, "err": repr(e)[:160]}


async def run_level(concurrency: int, total: int) -> None:
    sem = asyncio.Semaphore(concurrency)
    results: list[dict] = []

    limits = httpx.Limits(max_connections=concurrency + 4, max_keepalive_connections=concurrency + 4)
    async with httpx.AsyncClient(timeout=httpx.Timeout(600.0), limits=limits) as client:

        async def guarded():
            async with sem:
                results.append(await one_request(client))

        wall0 = time.perf_counter()
        await asyncio.gather(*(guarded() for _ in range(total)))
        wall = time.perf_counter() - wall0

    ok = [r for r in results if r["ok"]]
    rate_limited = sum(1 for r in results if r.get("rate_limited"))
    lat = sorted(r["dt"] for r in ok)

    def pct(p: float) -> float:
        if not lat:
            return 0.0
        k = min(len(lat) - 1, int(round(p * (len(lat) - 1))))
        return lat[k]

    comp = sum(r.get("completion_tokens", 0) for r in ok)
    prm = sum(r.get("prompt_tokens", 0) for r in ok)
    req_thr = len(ok) / wall if wall > 0 else 0.0
    comp_thr = comp / wall if wall > 0 else 0.0
    # 单请求平均 decode 速率（每请求 completion_tokens/延迟 的均值）
    per_req_tps = statistics.mean(
        [r["completion_tokens"] / r["dt"] for r in ok if r["dt"] > 0 and r.get("completion_tokens")]
        or [0.0]
    )

    print(
        f"并发={concurrency:<3} 请求={total:<3} 成功={len(ok):<3} 429={rate_limited:<2} "
        f"墙钟={wall:6.1f}s | 请求吞吐={req_thr:5.2f} req/s 生成吞吐={comp_thr:6.1f} tok/s | "
        f"单请求 p50={pct(0.5):6.2f}s p95={pct(0.95):6.2f}s max={max(lat or [0]):6.2f}s | "
        f"单请求decode≈{per_req_tps:5.1f} tok/s | 平均 prompt={prm // max(len(ok),1)} "
        f"completion={comp // max(len(ok),1)}"
    )
    errs = [r.get("err") for r in results if not r["ok"] and r.get("err")][:3]
    if errs:
        print(f"    示例错误: {errs}")


async def main() -> None:
    print(f"目标: {ENDPOINT}  模型: {MODEL}")
    print(f"thinking={'disabled(抽取场景)' if THINKING_OFF else 'enabled'} "
          f"max_tokens={MAX_TOKENS} 每档请求={REQUESTS_PER_LEVEL}")
    print(f"chunk 长度: {len(CHUNK)} 字符\n")
    if not API_KEY:
        print("❌ 缺少 API key：请设置 BENCH_LLM_KEY 或确认 deploy/.env 的 "
              "LLMGW_UPSTREAM_LLM_API_KEY 有值")
        return

    print("预热（首请求，含可能的连接/排队）...")
    async with httpx.AsyncClient(timeout=httpx.Timeout(600.0)) as client:
        r = await one_request(client)
        if r["ok"]:
            print(f"预热: 成功 延迟={r['dt']:.2f}s prompt={r.get('prompt_tokens')} "
                  f"completion={r.get('completion_tokens')}\n")
        else:
            print(f"预热失败: {r.get('err')}\n（后续档位可能同样失败，请先排查端点/key/模型名）\n")

    for level in CONCURRENCY_LEVELS:
        await run_level(level, REQUESTS_PER_LEVEL)


if __name__ == "__main__":
    asyncio.run(main())
