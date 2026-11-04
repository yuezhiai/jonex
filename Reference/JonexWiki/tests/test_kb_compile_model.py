"""P3b（方案 B）：KB 级编译模型走**任务快照透传**，引擎侧不另存副本。

四条必须钉住的事实：

1. 模型随任务走：`TaskRepo.create(compile_model=...)` 写进 `TaskRow.summary`，
   `ManifestBuilder.build(compile_model=...)` 读它，取不到才回落部署 env。
2. 引擎侧**没有** KB 配置副本：`kb_settings` 表 / `KbSettingsRepo` /
   `get|put_compile_model` 能力动作一律不存在（防止有人再把双事实源加回来）。
3. 改配置只影响**之后**建立的任务：复用已排队任务时刷新快照，`compiling`
   的任务保持自己的快照不动。
4. 目录（outline）也是编译产物：自动起草沿用编译任务带的模型。

`ManifestBuilder.build()` 只用 session 读 prompt，这里换替身，不连真库。
"""

import inspect
from types import SimpleNamespace

import pytest

from jonex_wiki.adapters.manifest import ManifestBuilder


class _FakeSettings:
    llm_model = "env-model"
    compile_concurrency = 4
    compile_max_passage_failure_rate = 0.05

    def runtime_env(self):
        return {}


@pytest.fixture()
def manifest_env(monkeypatch):
    """把 build() 的外部依赖换成替身：prompt 读取 + 当下 env。"""
    from jonex_wiki.adapters import manifest as manifest_mod

    state = SimpleNamespace(env={"LLM_MODEL": "env-model"})

    async def fake_resolve_prompt(session, tenant_id, kb_id, key):
        # 提示词内容与本主题无关；只保留 build() 需要的形状
        return SimpleNamespace(template="", template_sha=f"sha-{key}")

    monkeypatch.setattr(manifest_mod, "resolve_prompt", fake_resolve_prompt)
    monkeypatch.setattr(manifest_mod, "current_env", lambda: state.env)
    return state


# ────────────────────────────────────────────────────────────────
# manifest 取值：任务模型 → env 兜底
# ────────────────────────────────────────────────────────────────


async def test_curation_uses_task_model(manifest_env):
    manifest = await ManifestBuilder(_FakeSettings()).build(
        None, tenant_id="t1", kb_id="kb1", compile_model="task-model")
    assert manifest["models"]["curation"] == "task-model"


async def test_curation_falls_back_to_env(manifest_env):
    """任务没带模型 ⇒ 回落 env —— 与改造前逐字一致。"""
    manifest = await ManifestBuilder(_FakeSettings()).build(
        None, tenant_id="t1", kb_id="kb1")
    assert manifest["models"]["curation"] == "env-model"


async def test_curation_ignores_blank_task_model(manifest_env):
    """"没指定"与"指定成空白"必须是同一件事：都走 env。"""
    manifest = await ManifestBuilder(_FakeSettings()).build(
        None, tenant_id="t1", kb_id="kb1", compile_model="   ")
    assert manifest["models"]["curation"] == "env-model"


async def test_query_always_env_even_with_task_model(manifest_env):
    """查询期不吃 KB 覆盖（产品口径：这次只做解析/编译）。"""
    manifest = await ManifestBuilder(_FakeSettings()).build(
        None, tenant_id="t1", kb_id="kb1", compile_model="task-model")
    assert manifest["models"]["query"] == "env-model"


async def test_env_change_does_not_invalidate_manifest_when_task_model_set(manifest_env):
    """任务带了模型 ⇒ 部署 env 变化不再改动编译身份（不会整库重编）。"""
    from jonex_wiki.domain.engine.compile.engine import _compilation_manifest

    builder = ManifestBuilder(_FakeSettings())
    b1 = await builder.build(None, tenant_id="t1", kb_id="kb1", compile_model="task-model")
    manifest_env.env = {"LLM_MODEL": "other-env-model"}
    b2 = await builder.build(None, tenant_id="t1", kb_id="kb1", compile_model="task-model")
    assert _compilation_manifest(b1) == _compilation_manifest(b2)


async def test_task_model_change_changes_compilation_identity(manifest_env):
    """换模型 ⇒ 编译身份变 ⇒ 下一次编译整库重放（有意为之）。"""
    from jonex_wiki.domain.engine.compile.engine import _compilation_manifest

    builder = ManifestBuilder(_FakeSettings())
    a = await builder.build(None, tenant_id="t1", kb_id="kb1", compile_model="m1")
    b = await builder.build(None, tenant_id="t1", kb_id="kb1", compile_model="m2")
    assert _compilation_manifest(a) != _compilation_manifest(b)


# ────────────────────────────────────────────────────────────────
# 方案 A 的痕迹必须彻底消失
# ────────────────────────────────────────────────────────────────


def test_no_engine_side_kb_settings_table():
    """引擎侧不许再有 KB 配置副本：表、repo、ORM 三类痕迹一起查。"""
    from jonex_wiki.storage import models, repos

    assert not hasattr(models, "KbSettingsRow")
    assert not hasattr(repos, "KbSettingsRepo")
    assert "kb_settings" not in models.Base.metadata.tables


def test_application_no_compile_model_api():
    """能力侧不再有 get/put_compile_model —— 模型只能经任务进来。"""
    from jonex_wiki.application import WikiApplication

    assert not hasattr(WikiApplication, "get_compile_model")
    assert not hasattr(WikiApplication, "put_compile_model")


def test_manifest_builder_does_not_read_kb_table():
    """build() 的模型只能来入参，不能去读库（回归守卫）。"""
    src = inspect.getsource(ManifestBuilder.build)
    assert "compile_model" in src
    assert "KbSettingsRepo" not in src
    assert "kb_settings" not in src


# ────────────────────────────────────────────────────────────────
# 任务快照语义
# ────────────────────────────────────────────────────────────────


def _task(status="queued", summary=None):
    return SimpleNamespace(status=status, summary=summary)


class _CapturingSession:
    """只记录 add() 进来的行，flush 不做事 —— TaskRepo.create 的全部依赖。"""

    def __init__(self):
        self.added = None

    def add(self, row):
        self.added = row

    async def flush(self):
        return None


async def test_create_stamps_compile_model_on_task():
    """建任务时把模型写进 summary；没带就完全不写（summary 保持 None）。"""
    from jonex_wiki.kernel.task import TaskKind
    from jonex_wiki.storage.repos import TaskRepo

    session = _CapturingSession()
    await TaskRepo().create(session, tenant_id="t1", kb_id="kb1",
                            kind=TaskKind.compile, compile_model="m1")
    assert session.added.summary == {"compile_model": "m1"}

    await TaskRepo().create(session, tenant_id="t1", kb_id="kb1", kind=TaskKind.compile)
    assert session.added.summary is None


def test_restamp_updates_queued_task():
    """复用的排队任务要换成本次请求的模型（否则新配置编不进去）。"""
    from jonex_wiki.storage.repos import TaskRepo

    task = _task(status="queued", summary={"compile_model": "old"})
    TaskRepo.restamp_compile_model(task, "new")
    assert task.summary["compile_model"] == "new"


def test_restamp_preserves_other_summary_keys():
    from jonex_wiki.storage.repos import TaskRepo

    task = _task(status="queued", summary={"compile_model": "old", "outcome": "skip"})
    TaskRepo.restamp_compile_model(task, "new")
    assert task.summary == {"compile_model": "new", "outcome": "skip"}


def test_restamp_skips_running_task():
    """在途任务保持自己的快照 —— 这是方案 B 的核心承诺。"""
    from jonex_wiki.storage.repos import TaskRepo

    task = _task(status="compiling", summary={"compile_model": "old"})
    TaskRepo.restamp_compile_model(task, "new")
    assert task.summary["compile_model"] == "old"


def test_restamp_noop_without_model():
    """没带模型就不动（不能把已有快照清成空）。"""
    from jonex_wiki.storage.repos import TaskRepo

    task = _task(status="queued", summary={"compile_model": "old"})
    TaskRepo.restamp_compile_model(task, "")
    assert task.summary["compile_model"] == "old"


# ────────────────────────────────────────────────────────────────
# 管道真的接上了（源码级断言：值不是只在别处躺着）
# ────────────────────────────────────────────────────────────────


def test_compile_runner_reads_task_snapshot():
    from jonex_wiki.domain.compile_task import create_compile_runner

    src = inspect.getsource(create_compile_runner)
    assert 'row.summary or {}).get("compile_model")' in src
    # 两次 build（编译开始 / 发布前复核）都带同一个值
    assert src.count("compile_model=compile_model") >= 2


def test_parse_runner_propagates_to_compile_task():
    """解析任务带的模型要原样交给它自动排的编译任务。"""
    from jonex_wiki.domain.parsing.parse_task import create_parse_runner

    src = inspect.getsource(create_parse_runner)
    assert 'row.summary or {}).get("compile_model")' in src


def test_outline_runner_propagates_to_draft():
    """目录自动起草沿用编译任务的模型（同一次编译里不该出现两个模型）。"""
    from jonex_wiki.domain.outline import task as outline_task

    runner_src = inspect.getsource(outline_task.build_outline_runner)
    assert 'row.summary or {}).get("compile_model")' in runner_src
    # 真正把值塞进 draft 报文的是 _auto_draft（runner 只负责取值传下去）
    draft_src = inspect.getsource(outline_task._auto_draft)
    assert '"model": compile_model' in draft_src


def test_compile_api_accepts_model():
    """引擎 /compile 接口能收 model（平台直接触发编译时用）。"""
    from jonex_wiki.api.routers import compile as compile_router

    src = inspect.getsource(compile_router.trigger_compile)
    assert "compile_model" in src


def test_prompt_edit_queue_carries_model():
    """prompt 编辑/删除触发的重编也要带模型（平台随调用给）。"""
    from jonex_wiki.application import WikiApplication

    src = inspect.getsource(WikiApplication._queue_compile_after_prompt_edit)
    assert "compile_model" in src
    assert "restamp_compile_model" in src
