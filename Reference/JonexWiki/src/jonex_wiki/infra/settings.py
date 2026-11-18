import os
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings


def _default_llm_model() -> str:
    """``LLM_MODEL`` 缺失时的兜底：``DEFAULT_LLM_MODEL``。

    不再写字面量兜底 —— 模型名的唯一来源是环境变量。历史上这里写死了一个模型名，
    上游改名后它会静默把旧名字捡回来（排查时日志里看不出线索）。

    刻意**不在构造期抛错**：`Settings()` 的纯构造场景（测试、只读端点）不该被
    配置问题连坐；真正发请求时会校验非空，见 ``infra/gateway.py`` 的
    ``GwModelNotConfigured``。
    """
    return os.getenv("DEFAULT_LLM_MODEL", "").strip()


# [jonex] 调用时重读 env 的模型名解析（Settings.llm_model 是进程启动时的冻结值）。
def current_llm_model() -> str:
    """**调用时**从环境变量解析模型名：``LLM_MODEL`` → ``DEFAULT_LLM_MODEL`` → ``""``。

    与 ``Settings.llm_model``（字段 alias ``LLM_MODEL`` + ``default_factory`` 回落
    ``DEFAULT_LLM_MODEL``）**同一套优先级**，区别只在**读取时机**：本函数每次调用都
    重读 ``os.environ``，所以改完 env 之后**下一次编译/查询**即生效，不必等进程重启。

    为什么需要它：``Settings`` 在 app 工厂里只构造一次（``main.py``），字段值等于
    **进程启动那一刻**的 env。而模型名是"部署当下"的属性 —— 编译要按当下的 env 编，
    查询也要按当下的 env 查（bundle Manifest 里记的那条只是编译期快照）。
    """
    return os.getenv("LLM_MODEL", "").strip() or _default_llm_model()


class Settings(BaseSettings):
    model_config = {"env_file": ".env", "env_prefix": "JONEX_WIKI_", "extra": "ignore",
                    "populate_by_name": True}  # 允许字段名与 validation_alias 同时可用(测试 kwargs / 生产 env)

    # 认证:单一内部 key(X-API-Key);业务租户来自 X-Tenant-ID header(方案 B)
    api_key: str = ""
    # 平台解析产物输入根(共享卷挂载点):ingest 端点从这里读 source_path
    input_root: str = "/app/inputs"
    media_keyframes_enabled: bool = True
    media_keyframes_max_frames: int = Field(default=32, ge=1, le=256)
    media_keyframes_interval_seconds: float = Field(default=5.0, ge=0.25, le=120)
    pdf_table_repair_enabled: bool = True
    pdf_text_layer_enabled: bool = True
    pdf_text_layer_max_pages: int = Field(default=128, ge=1, le=2048)
    visual_inspection_enabled: bool = True
    visual_inspection_model: str = ""
    # 产物根目录
    data_root: str = "~/.jonex-wiki"
    # 网关(环境变量不带 JONEX_WIKI_ 前缀,显式 alias)
    llm_binding_host: str = Field(default="", validation_alias="LLM_BINDING_HOST")
    llm_binding_api_key: str = Field(default="", validation_alias="LLM_BINDING_API_KEY")
    llm_model: str = Field(default_factory=_default_llm_model, validation_alias="LLM_MODEL")
    # 单次 chat 调用超时。OpenAI SDK 默认 600s,一次挂起就能堵死整个页面串行链
    # (页面内 passage 逐条串行),也会让延迟统计彻底失真;超时后交给 complete 的重试预算
    llm_timeout: float = Field(default=120.0, validation_alias="LLM_TIMEOUT")
    # query_tasks 终态行保留时长(秒)。done/failed/cancelled 且 finished_at 早于该窗口的
    # 行由启动 GC 清除,防 query_tasks 无界增长(方案 §6.4)。
    query_task_ttl_seconds: int | None = Field(default=None, ge=0)
    # 成本闸门
    compile_max_documents: int = 1000
    compile_page_drop_threshold: float = 0.5   # 空产出守卫骤降阈值:page_count 较上一版下跌超此比例拒绝发布
    # ── 编译链路调度旋钮(唯一权威在本文件;经 ManifestBuilder 进 manifest.curation)──
    # 页面 passage 并发度。没有「不限」语义:要串行就显式写 1(0/负数非法)。
    # 这是部署默认值的唯一入口；由 manifest 传给分层编译器。
    compile_concurrency: int = Field(default=15, ge=1)
    # passage 永久失败率上限(0..1):超过即 fail closed;production 策略下按降级审计发布。
    compile_max_passage_failure_rate: float = 0.05
    # 并发
    api_parse_concurrency: int = Field(default=15, ge=1)
    api_compile_concurrency: int = Field(default=15, ge=1)
    api_query_concurrency: int = 4
    api_outline_concurrency: int = 2
    blocking_pool_size: int = Field(default=15, ge=1)
    # 上传/解析
    max_upload_bytes: int = 104_857_600
    keep_bundles: int | None = Field(default=1, ge=0)  # 当前版本另计；默认额外保留 1 个历史版本
    # MinerU(不带前缀,显式 alias)
    mineru_api_token: str = Field(default="", validation_alias="MINERU_API_TOKEN")
    mineru_api_base_url: str = Field(default="https://mineru.net", validation_alias="MINERU_API_BASE_URL")
    mineru_poll_interval: float = Field(default=5.0, validation_alias="MINERU_POLL_INTERVAL")
    mineru_poll_timeout: float = Field(default=1800.0, validation_alias="MINERU_POLL_TIMEOUT")
    # MinerU(§15 收敛新增,不带前缀,显式 alias)
    mineru_model_version: str = Field(default="vlm", validation_alias="MINERU_MODEL_VERSION")
    mineru_local_backend: str = Field(default="pipeline", validation_alias="MINERU_LOCAL_BACKEND")
    mineru_parse_max_retries: int = Field(default=3, validation_alias="MINERU_PARSE_MAX_RETRIES")
    mineru_parse_retry_backoff: float = Field(default=3.0, validation_alias="MINERU_PARSE_RETRY_BACKOFF")
    mineru_selfhost_base_url: str = Field(default="http://127.0.0.1:8000", validation_alias="MINERU_SELFHOST_BASE_URL")
    mineru_selfhost_backend: str = Field(default="pipeline", validation_alias="MINERU_SELFHOST_BACKEND")
    mineru_selfhost_lang: str = Field(default="ch", validation_alias="MINERU_SELFHOST_LANG")
    mineru_selfhost_return_images: bool = Field(default=False, validation_alias="MINERU_SELFHOST_RETURN_IMAGES")
    # 数据库(分项配置;可选 DATABASE_URL 覆盖,如本地 SQLite 快速体验)
    db_host: str = Field(default="postgres", validation_alias="DB_HOST")
    db_port: int = Field(default=5432, validation_alias="DB_PORT")
    db_username: str = Field(default="jonex", validation_alias="DB_USERNAME")
    db_password: str = Field(default="change-me", validation_alias="DB_PASSWORD")
    db_name: str = Field(default="jonex", validation_alias="DB_NAME")
    db_schema: str = Field(default="jonex_wiki", validation_alias="DB_SCHEMA")
    database_url_override: str = Field(default="", validation_alias="DATABASE_URL")

    # ── §15 配置治理:core 引擎旋钮收敛进 Settings(唯一权威) ──
    # compile + semantic review
    compile_output_tokens: int = 16384
    semantic_review_output_tokens: int = 4096
    semantic_review: str = Field(default="off", validation_alias="LLM_WIKI_SEMANTIC_REVIEW")
    # One file-reading agent route for both query and article compilation.
    agent_reasoning_effort: Literal['off', 'high', 'xhigh'] = 'off'
    agent_cli_path: str = ''
    agent_model_catalog: str = ''
    agent_query_search_seconds: float = Field(default=130.0, gt=0, allow_inf_nan=False)
    agent_query_finish_seconds: float = Field(default=24.0, gt=0, allow_inf_nan=False)
    # Limits for each article-writing attempt.
    agent_compile_batch_max_calls: int = Field(default=200, ge=1, le=500)
    agent_compile_batch_timeout_seconds: float = Field(default=900.0, gt=0, le=3600, allow_inf_nan=False)
    agent_compile_batch_idle_seconds: float = Field(default=300.0, gt=0, le=3600, allow_inf_nan=False)
    agent_compile_batch_max_input_bytes: int = Field(default=2 * 1024 * 1024, ge=64 * 1024, le=4 * 1024 * 1024)
    agent_compile_max_input_tokens: int = Field(default=6_000_000, ge=100_000, le=100_000_000)
    agent_compile_final_input_tokens: int = Field(default=240_000, ge=4_000, le=10_000_000)
    agent_compile_turn_max_output_tokens: int = Field(default=16_384, ge=1024, le=65_536)
    compile_input_max_restarts: int = Field(default=1, ge=0, le=10)
    # Optional private diagnostic recording. Empty means no raw request log.
    agent_record_root: str = ''
    # prepared cache
    query_material_cache_bytes: int = Field(default=16 * 1024 * 1024, ge=0)
    # 解析杂项
    allow_private_urls: bool = False
    text_encoding: str = "utf-8"
    # PackingOptions(扁平,默认与 chunking.py 的 PackingOptions 字段一致)
    pack_small_tokens: int = 4000
    pack_target_tokens: int = 4000
    # prose token 上限:上限用 token 表达,中英文才共用同一个上限(见 chunking.PackingOptions)
    pack_max_tokens: int = 5000
    pack_table_target_tokens: int = 2000
    # 须 ≤ pack_max_tokens(表格预算排序断言),随其同步下调
    pack_row_max_tokens: int = 5000
    pack_max_chars: int = 32000
    pack_context_tokens: int = 256
    pack_previous_tokens: int = 128
    # ── 编译输入体检(jonex_wiki 链路)──
    # 体检只做一件事:把**注定装不下**的单元(结构超限)摘出编译面并留档,而不是让整份
    # 文档硬失败;详见 source_prep/inspect.py。关掉 = 完全回到"超限即 PackingError"的
    # 旧语义。这里没有比例闸门:摘除判据只有一条,不需要阈值兜底(整体不可信该由编译侧
    # max_passage_failure_rate 负责)。
    inspect_enabled: bool = True
    # Default for model calls which do not explicitly select thinking mode.
    thinking_default: str = "off"
    query_token_cache_bytes: int = Field(default=16 * 1024 * 1024, ge=0)

    @property
    def database_url(self) -> str:
        """分项拼装;显式 DATABASE_URL(如 sqlite)优先。"""
        if self.database_url_override:
            return self.database_url_override
        return (f"postgresql+asyncpg://{self.db_username}:{self.db_password}"
                f"@{self.db_host}:{self.db_port}/{self.db_name}")

    @field_validator("thinking_default", mode="before")
    @classmethod
    def _valid_thinking_default(cls, value):
        """把 on/enabled、off/disabled 归一化;非法值 fail-fast(同 core 的 QUERY_THINKING_INVALID)。"""
        normalized = str(value or "off").strip().casefold()
        if normalized not in {"on", "enabled", "off", "disabled", "scene_default"}:
            raise ValueError(
                f"JONEX_WIKI_THINKING_DEFAULT 取值非法: {value!r}(仅支持 off/on/scene_default)")
        return normalized


    @field_validator("compile_max_passage_failure_rate", mode="before")
    @classmethod
    def _valid_compile_max_passage_failure_rate(cls, value):
        """永久失败率必须在 [0, 1]。"""
        rate = float(value)
        if not 0.0 <= rate <= 1.0:
            raise ValueError(
                f"JONEX_WIKI_COMPILE_MAX_PASSAGE_FAILURE_RATE 取值非法: {value!r}(须在 0..1)")
        return rate


    def runtime_env(self) -> dict[str, str]:
        """把运行时旋钮序列化为 env 名→值(供引擎注入)。

        引擎模块经 ``current_env()`` 读这些名字(默认回退 ``os.environ`` 保持
        monkeypatch.setenv 测试兼容)。布尔统一序列化为 ``on``/``off``,与各
        ``_*_enabled()`` 的 casefold 解析一致。
        """
        def on_off(value: bool) -> str:
            return "on" if value else "off"

        env: dict[str, str] = {
            # compile + semantic review
            "JONEX_WIKI_COMPILE_OUTPUT_TOKENS": str(self.compile_output_tokens),
            "JONEX_WIKI_SEMANTIC_REVIEW_OUTPUT_TOKENS": str(self.semantic_review_output_tokens),
            "LLM_WIKI_SEMANTIC_REVIEW": self.semantic_review,
            # packing
            "JONEX_WIKI_PACK_SMALL_TOKENS": str(self.pack_small_tokens),
            "JONEX_WIKI_PACK_TARGET_TOKENS": str(self.pack_target_tokens),
            "JONEX_WIKI_PACK_MAX_TOKENS": str(self.pack_max_tokens),
            "JONEX_WIKI_PACK_TABLE_TARGET_TOKENS": str(self.pack_table_target_tokens),
            "JONEX_WIKI_PACK_ROW_MAX_TOKENS": str(self.pack_row_max_tokens),
            "JONEX_WIKI_PACK_MAX_CHARS": str(self.pack_max_chars),
            "JONEX_WIKI_PACK_CONTEXT_TOKENS": str(self.pack_context_tokens),
            "JONEX_WIKI_PACK_PREVIOUS_TOKENS": str(self.pack_previous_tokens),
            # 编译输入体检
            "JONEX_WIKI_INSPECT_ENABLED": on_off(self.inspect_enabled),
            # 模型名(编译与查询都按**当下**的 env 走,见 current_llm_model)。
            # ⚠ 必须在这里序列化:查询/编译链路把执行包在 runtime_environment(...) 里,
            # current_env() 会**完全遮蔽** os.environ —— 不进这张映射的名字,
            # env 文件里写多少都不生效(test_query_budget_env.py 记录过同一个坑)。
            "LLM_MODEL": current_llm_model() or self.llm_model,
            # prepared cache
            "JONEX_WIKI_QUERY_MATERIAL_CACHE_BYTES": str(self.query_material_cache_bytes),
            "JONEX_WIKI_QUERY_TOKEN_CACHE_BYTES": str(self.query_token_cache_bytes),
            "JONEX_WIKI_AGENT_REASONING_EFFORT": self.agent_reasoning_effort,
            "JONEX_WIKI_AGENT_CLI_PATH": self.agent_cli_path,
            "JONEX_WIKI_AGENT_MODEL_CATALOG": self.agent_model_catalog,
            # 解析杂项
            "JONEX_WIKI_ALLOW_PRIVATE_URLS": on_off(self.allow_private_urls),
            "JONEX_WIKI_TEXT_ENCODING": self.text_encoding,
            "MINERU_MODEL_VERSION": self.mineru_model_version,
            "MINERU_LOCAL_BACKEND": self.mineru_local_backend,
            "MINERU_PARSE_MAX_RETRIES": str(self.mineru_parse_max_retries),
            "MINERU_PARSE_RETRY_BACKOFF": str(self.mineru_parse_retry_backoff),
            "MINERU_SELFHOST_BASE_URL": self.mineru_selfhost_base_url,
            "MINERU_SELFHOST_BACKEND": self.mineru_selfhost_backend,
            "MINERU_SELFHOST_LANG": self.mineru_selfhost_lang,
            "MINERU_SELFHOST_RETURN_IMAGES": on_off(self.mineru_selfhost_return_images),
            "MINERU_API_BASE_URL": self.mineru_api_base_url,
            "MINERU_API_TOKEN": self.mineru_api_token,
            "MINERU_POLL_INTERVAL": str(self.mineru_poll_interval),
            "MINERU_POLL_TIMEOUT": str(self.mineru_poll_timeout),
        }
        return env

    @model_validator(mode="after")
    def _require_secrets(self):
        """spec §7.1/§12.3:缺认证/网关配置拒绝启动(fail-fast)。"""
        if not self.api_key:
            raise ValueError("JONEX_WIKI_API_KEY 未配置,拒绝启动")
        if not self.llm_binding_host or not self.llm_binding_api_key:
            raise ValueError("LLM_BINDING_HOST / LLM_BINDING_API_KEY 未配置,拒绝启动")
        return self
