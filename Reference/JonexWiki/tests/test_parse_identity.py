"""解析身份（parse identity）：A 归一化 AST + B 手工版本号 + 硬/软分量分级。

背景（2026-10-20 事故）：往 `domain/parsing/parse_task.py`（解析任务编排）里传了一行
编译模型参数，与解析输出毫无关系，却让**所有 KB 的所有历史产物**瞬间判为过期、整库
编译全挂 —— 因为旧实现把 `domain/parsing/` 与 `source_prep/` 下全部 `.py` 都算进指纹。

背景（2026-10-22 优化）：收窄成显式清单后，按**字节**哈希仍让「改注释 / 跑格式化 /
动 docstring」触发全库重解析 + 重编译。现在规则代码取**归一化 AST**，并把分量分成
硬门（阻断编译）与软门（只告警）。

这里钉住：
1. 编排文件（`parse_task.py`）不在规则清单里，改它不改指纹；
2. 规则清单里的文件**真正的逻辑改动**必须改变 code 分量；
3. 注释 / 空行 / 格式化 / docstring 改动**不得**改变 code 分量；
4. `PARSE_OUTPUT_VERSION`（B）是唯一能让全世界一起重解析的硬门；
5. 只有硬门分量漂移才算 `needs_reparse`；code/rule_set/schema 漂移只告警；
6. 产物记分量摘要，过期时能说出「哪一项变了」，而不是笼统的「解析规则已变化」。
"""

import hashlib
import inspect
import json
from dataclasses import asdict

import pytest

from jonex_wiki.domain.parsing import identity, parse_task


class _Settings:
    """只提供指纹用得到的字段（PackingOptions / InspectionPolicy 也从这里读）。"""

    text_encoding = "utf-8"
    mineru_model_version = "vlm"
    pack_small_tokens = 4000
    pack_target_tokens = 4000
    pack_max_tokens = 5000
    pack_table_target_tokens = 2000
    pack_row_max_tokens = 5000
    pack_max_chars = 32000
    pack_context_tokens = 256
    pack_previous_tokens = 128
    inspect_enabled = True


@pytest.fixture()
def package_root(tmp_path, monkeypatch):
    """把规则清单的根目录指到临时树，便于安全地改文件内容。"""
    for rel in identity.PARSE_RULE_SOURCES:
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"x = 1  # {rel}\n", encoding="utf-8")
    monkeypatch.setattr(identity, "_PACKAGE_ROOT", tmp_path)
    identity.parser_code_checksum.cache_clear()
    yield tmp_path
    identity.parser_code_checksum.cache_clear()


def _rewrite(package_root, rel, text):
    (package_root / rel).write_text(text, encoding="utf-8")
    identity.parser_code_checksum.cache_clear()   # 生产里是新进程，测试里手动失效


def test_orchestration_files_are_not_rules():
    """回归：编排/导出/指纹自身不得进清单（进 = 改一行就让全库报警）。"""
    for rel in ("domain/parsing/parse_task.py",
                "domain/parsing/identity.py",
                "domain/parsing/__init__.py",
                "domain/parsing/mineru/__init__.py",
                "domain/engine/compile/source_prep/__init__.py"):
        assert rel not in identity.PARSE_RULE_SOURCES, rel


def test_rule_sources_exist():
    """清单里每个文件都必须真实存在（改名/删除后不能静默漏掉）。"""
    missing = [rel for rel in identity.PARSE_RULE_SOURCES
               if not (identity._PACKAGE_ROOT / rel).is_file()]
    assert missing == []


def test_editing_non_rule_file_keeps_signature(package_root):
    """A 的核心承诺：改编排文件内容，指纹**不变**。"""
    before = identity.parser_code_checksum()
    target = package_root / "domain/parsing/parse_task.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("# 与解析输出无关的编排改动\n", encoding="utf-8")
    assert identity.parser_code_checksum() == before


def test_real_rule_change_changes_code_checksum(package_root):
    """A 的另一半：规则文件的**逻辑**变了，code 分量必须变（否则旧产物被误判有效）。"""
    before = identity.parser_code_checksum()
    rel = identity.PARSE_RULE_SOURCES[0]
    _rewrite(package_root, rel, "x = 2\n")
    assert identity.parser_code_checksum() != before


@pytest.mark.parametrize("text", [
    "x = 1  # 只改了注释\n",
    "x=1\n# 末尾多空行\n",
    "\n\nx = 1\n",
    '"""模块 docstring 改了也不该失效。"""\nx = 1\n',
])
def test_comment_format_docstring_do_not_change_code_checksum(package_root, text):
    """本次优化的承诺：注释 / 格式 / docstring 改动不改变 code 分量。"""
    before = identity.parser_code_checksum()
    _rewrite(package_root, identity.PARSE_RULE_SOURCES[0], text)
    assert identity.parser_code_checksum() == before


def test_function_docstring_does_not_change_code_checksum(package_root):
    rel = identity.PARSE_RULE_SOURCES[0]
    _rewrite(package_root, rel, 'def f():\n    """docstring 长度变了。"""\n    return 1\n')
    before = identity.parser_code_checksum()
    _rewrite(package_root, rel, 'def f():\n    """另一段更长的 docstring 说明。"""\n    return 1\n')
    assert identity.parser_code_checksum() == before
    # 但函数体真变了 → 必须变
    _rewrite(package_root, rel, 'def f():\n    return 2\n')
    assert identity.parser_code_checksum() != before


def test_unparseable_rule_file_falls_back_to_bytes(package_root):
    """极端兜底：语法错误的文件退回字节哈希，而不是让整个解析身份崩掉。"""
    before = identity.parser_code_checksum()
    _rewrite(package_root, identity.PARSE_RULE_SOURCES[0], "def broken(:\n")
    assert identity.parser_code_checksum() != before
    assert len(identity.parser_code_checksum()) == 64


def test_adding_rule_source_changes_signature(package_root, monkeypatch):
    """清单本身进哈希：新增条目 = 规则集合变了。"""
    before = identity.parse_input_signature(_Settings())
    extra = "domain/parsing/mineru/__init__.py"
    (package_root / extra).write_text("# 原来不算规则\n", encoding="utf-8")
    monkeypatch.setattr(identity, "PARSE_RULE_SOURCES",
                        identity.PARSE_RULE_SOURCES + (extra,))
    identity.parser_code_checksum.cache_clear()
    try:
        assert identity.parse_input_signature(_Settings()) != before
    finally:
        identity.parser_code_checksum.cache_clear()


def test_output_version_bump_changes_signature(package_root, monkeypatch):
    """B：产物形状变了但规则清单没变时，靠版本号让旧产物失效。"""
    before = identity.parse_input_signature(_Settings())
    monkeypatch.setattr(identity, "PARSE_OUTPUT_VERSION",
                        identity.PARSE_OUTPUT_VERSION + 1)
    assert identity.parse_input_signature(_Settings()) != before


def test_output_version_has_no_env_override(monkeypatch):
    """产物版本号只有代码里一处，**没有**部署侧开关（2026-10-22 决策）。

    留 env 覆盖的代价：代码里那个数从此不代表线上代次（没人知道现在到底是几），
    而它恰恰是排障时唯一的依据、也是唯一的全局重解析开关。这条用例锁住「别再把它
    加回来」：环境变量设成什么都不影响指纹。
    """
    from jonex_wiki.infra.settings import Settings

    # Settings() 会 fail-fast 校验必需项；只喂够它启动的最小集合
    monkeypatch.setenv("JONEX_WIKI_API_KEY", "test-key")
    monkeypatch.setenv("LLM_BINDING_HOST", "http://llm-gateway:8787")
    monkeypatch.setenv("LLM_BINDING_API_KEY", "test-key")
    monkeypatch.delenv("JONEX_WIKI_PARSE_OUTPUT_VERSION", raising=False)
    baseline = identity.parse_input_signature(Settings())

    monkeypatch.setenv("JONEX_WIKI_PARSE_OUTPUT_VERSION", "9")
    settings = Settings()
    assert not hasattr(settings, "parse_output_version_override")
    assert identity.parse_input_signature(settings) == baseline
    assert identity.parse_identity_payload(settings)["parse_output_version"] == \
        identity.PARSE_OUTPUT_VERSION


def test_packing_budget_changes_signature(package_root):
    """打包预算进了指纹（同一份文档会切出不同的块边界）。"""

    class _Tight(_Settings):
        pack_target_tokens = 1234

    assert identity.parse_input_signature(_Tight()) != \
        identity.parse_input_signature(_Settings())


def test_signature_is_stable(package_root):
    assert identity.parse_input_signature(_Settings()) == \
        identity.parse_input_signature(_Settings())


def test_components_cover_payload(package_root):
    """分量摘要必须覆盖 payload 的每一项，否则「哪一项变了」会有盲区。"""
    payload = identity.parse_identity_payload(_Settings())
    components = identity.parse_input_components(_Settings())
    assert set(components) == set(payload)
    assert all(len(value) == 16 for value in components.values())
    # code 分量必须等于 code 指纹的短摘要（否则排障时会指错项）
    assert components["code"] == hashlib.sha256(
        json.dumps(payload["code"], ensure_ascii=False).encode()).hexdigest()[:16]


def test_identity_drift(package_root):
    expected = identity.parse_input_components(_Settings())
    # 一致 → 没有漂移
    assert identity.identity_drift(expected, expected) == []
    # 旧产物没记分量 → unknown，不能说「没变化」
    assert identity.identity_drift(None, expected) == ["unknown"]
    assert identity.identity_drift({}, expected) == ["unknown"]
    # 只有 code 变了 → 只报 code（这就是「改代码」与「改配置」的区分）
    drifted = dict(expected, code="0000000000000000")
    assert identity.identity_drift(drifted, expected) == ["code"]


def test_blocking_drift_only_cares_about_hard_components(package_root):
    """硬门：只有产物版本 / 打包 / 体检 / 编码 / MinerU 版本漂移才阻断编译。"""
    expected = identity.parse_input_components(_Settings())

    # code / rule_set 漂移 → 不阻断（软门）
    soft = dict(expected, code="0" * 16, rule_set="1" * 16)
    assert identity.blocking_drift(soft, expected) == []
    assert sorted(identity.advisory_drift(soft, expected)) == ["code", "rule_set"]

    # 硬门任意一项漂移 → 阻断
    for key in identity.BLOCKING_COMPONENTS:
        drifted = dict(expected, **{key: "f" * 16})
        assert identity.blocking_drift(drifted, expected) == [key], key
        assert identity.advisory_drift(drifted, expected) == []

    # 没记分量的旧产物 → 无法证明可信 → 阻断
    assert identity.blocking_drift(None, expected) == ["unknown"]
    assert identity.advisory_drift(None, expected) == []


def test_old_schema_artifacts_stay_current(package_root):
    """迁移承诺：schema 2 产物（老身份算法产出）在新算法下仍是 current。

    `schema` 属软门 —— 否则改一次指纹算法就等于全库重解析。
    """
    expected = identity.parse_input_components(_Settings())
    legacy = dict(expected, schema="0" * 16, code="1" * 16, rule_set="2" * 16)
    assert identity.blocking_drift(legacy, expected) == []
    assert identity.advisory_drift(legacy, expected) == ["schema"]
    # 诊断口径仍能看到全部差异
    assert sorted(identity.identity_drift(legacy, expected)) == ["code", "rule_set", "schema"]


def test_payload_records_output_version(package_root):
    payload = identity.parse_identity_payload(_Settings())
    assert payload["parse_output_version"] == identity.PARSE_OUTPUT_VERSION
    assert payload["schema"] == identity.PARSE_IDENTITY_SCHEMA
    assert payload["rule_set"] == list(identity.PARSE_RULE_SOURCES)
    assert payload["packing"] == asdict(
        __import__("jonex_wiki.domain.engine.compile.source_prep.chunking",
                   fromlist=["PackingOptions"]).PackingOptions.from_settings(_Settings()))


def test_both_artifact_writers_stamp_components():
    """回归：产物有**两个**写入方，只改一处会让另一类产物永远报 "unknown"。

    2026-10-20 就踩过：`domain/parsing/parse_task.py`（引擎自解析）改了，
    `application._ingest_parsed_document_locked`（平台已解析产物入库）漏了。
    """
    from jonex_wiki.application import WikiApplication

    ingest_src = inspect.getsource(WikiApplication._ingest_parsed_document_locked)
    assert "parse_input_components" in ingest_src
    assert "source_metadata[\"parse_input_components\"]" in ingest_src

    parse_task_src = inspect.getsource(parse_task)
    assert "source_metadata[\"parse_input_components\"]" in parse_task_src


def test_source_port_gates_on_components_not_full_signature():
    """回归：编译守卫必须走 `blocking_drift`（分量），不能退回整签名相等。

    整签名相等 = 改一行注释就让全库重解析 + 重编译。
    """
    from jonex_wiki.adapters import source_port

    src = inspect.getsource(source_port.SourceArtifactPortAdapter.get_artifacts)
    assert "blocking_drift" in src
    assert "parse_input_signature" not in src

    init_src = inspect.getsource(source_port.SourceArtifactPortAdapter.__init__)
    assert "parse_input_components" in init_src
    assert "parse_input_signature" not in init_src


def test_identity_log_lines_carry_version_and_every_component(package_root):
    """启动期两行日志：代次一行 + 各分量摘要一行（排障的对照基准）。

    产物版本号撤掉部署侧开关后，启动日志是确认线上真实代次的唯一途径；分量那行
    必须按 `parse_input_components` 的键顺序逐个给出短摘要，才能和产物里记的
    `parse_input_components` 直接对位比较。
    """
    settings = _Settings()
    lines = identity.identity_log_lines(settings)

    assert len(lines) == 2
    head, detail = lines
    assert f"output_version={identity.PARSE_OUTPUT_VERSION}" in head
    assert f"schema={identity.PARSE_IDENTITY_SCHEMA}" in head
    assert f"rules={len(identity.PARSE_RULE_SOURCES)}" in head
    components = identity.parse_input_components(settings)
    assert f"code={components['code'][:12]}" in head
    assert f"signature={identity.parse_input_signature(settings)[:12]}" in head

    assert detail.startswith("parse identity components: ")
    for name, digest in components.items():
        assert f"{name}={digest[:12]}" in detail


def test_startup_logs_parse_identity():
    """回归：`WikiApplication.start()` 必须真的把这两行打出来（别只写个 helper）。"""
    from jonex_wiki.application import WikiApplication

    src = inspect.getsource(WikiApplication.start)
    assert "identity_log_lines" in src
    assert "log.info" in src


def test_license_header_above_docstring_is_ignored(package_root):
    """在已有 docstring 之上再插一段文档（许可证头），不该让产物失效。

    只剥第一条裸字符串的话，原 docstring 会被降级成普通字符串表达式留在 AST 里 ——
    实测踩过：加一段模块级说明，`code` 分量就变了。
    """
    rel = identity.PARSE_RULE_SOURCES[0]
    original = '"""原模块 docstring。"""\nimport os\n\nVALUE = 1\n'
    _rewrite(package_root, rel, original)
    before = identity.parser_code_checksum()

    _rewrite(package_root, rel, '"""新加的许可证头。"""\n' + original)
    assert identity.parser_code_checksum() == before

    # 真逻辑改动仍然要被抓到
    _rewrite(package_root, rel, '"""新加的许可证头。"""\n' + original.replace("VALUE = 1", "VALUE = 2"))
    assert identity.parser_code_checksum() != before


def test_every_payload_component_is_classified(package_root):
    """新增指纹分量必须显式分级：漏了等于「漂移被静默放过」。

    `blocking_drift` / `advisory_drift` 都只遍历各自清单，payload 里多出一个键
    而两份清单都没收它时，这个分量的变化既不阻断、也不告警 —— 只出现在
    `identity_drift` 诊断里，等于隐形失效。加分量时这条会红，逼你去选边。
    """
    payload = identity.parse_identity_payload(_Settings())
    classified = (set(identity.BLOCKING_COMPONENTS)
                  | set(identity.ADVISORY_COMPONENTS)
                  | {identity.SCHEMA_COMPONENT})
    assert classified == set(payload), (
        f"未分级的分量: {sorted(set(payload) - classified)}；"
        f"分级里多余的: {sorted(classified - set(payload))}")
