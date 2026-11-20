"""编译状态回写必须真的落库 —— 埋点参数不许引用作用域里不存在的变量。

背景（2026-10-09 线上）：点「重新编译」，knowledge-base 日志出现

    ERROR jonex_core.common.database | database.py:137 | 未知异常, 已回滚: name 'tenant_id' is not defined
    WARNING ...reconciliation_service | 记录 LLM-Wiki 编译状态失败 doc=276ff8b7-...

追踪到 ``reconciliation_service.py`` 的 ``_record_openkb_status``：

    async def _record_openkb_status(self, doc, *, status, error="", warnings=None):
        ...
        await record_compile_task(
            session, tenant_id, document_id=doc.id,   # ← 签名里没有 tenant_id
            ...

数据运营二期那次（commit ``818b6a76``）加埋点时，照同族**另外两个有
``tenant_id`` 参数**的函数（``_record_openkb_status_by_id`` /
``openkb_service._record_openkb_status``）复制粘贴，写成了裸 ``tenant_id``；
而这一版签名收的是 ``doc`` 对象（同一函数 2620 行用的是 ``doc.tenant_id``）。

**后果不是「少一条日志」**：NameError 抛在 ``session.commit()`` **之前**，
整个事务回滚 —— 编译状态一个字都没写进库，文档永远停在 ``compiling``；
而且它被 ``except Exception`` 吞成一条 WARNING，只能翻日志才看得见。
"""

import ast
import builtins
import pathlib
import symtable
import unittest
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from capabilities.knowledge_base.services.reconciliation_service import ReconciliationService

_MODULE = "capabilities.knowledge_base.services.reconciliation_service"


class _FreshDocument:
    """``repo.get_required`` 的返回值（被原地改属性后 ``session.add``）。"""

    def __init__(self):
        self.llm_wiki_compile_status = None
        self.llm_wiki_compile_error = None
        self.llm_wiki_compile_warnings = None
        self.llm_wiki_compiled_at = None
        self.knowledge_base_id = "kb-1"
        self.content_generation = 3
        # commit 之后代码会读它做内存态同步（`meta = dict(fresh.extra_metadata or {})`）
        self.extra_metadata = {}


class _RecordCompileStatusTest(unittest.IsolatedAsyncioTestCase):
    """``_record_openkb_status(doc, ...)`` 的三件套：埋点、提交、内存态。"""

    async def test_compile_event_is_recorded_with_doc_tenant_id(self):
        """回归：埋点必须真的被调用，且第 2 个位置参数是 ``doc.tenant_id``。

        修前这里会因为 ``NameError: name 'tenant_id' is not defined``
        让 ``record_compile_task`` **一次都没被调用**（异常抛在实参求值阶段）。
        """
        doc = SimpleNamespace(id="doc-1", tenant_id="tenant-real",
                              extra_metadata=None, knowledge_base_id="kb-1")
        fresh = _FreshDocument()
        session = SimpleNamespace(add=lambda obj: None, commit=AsyncMock())

        class Repo:
            def __init__(self, _session):
                pass

            async def get_required(self, document_id, tenant_id):
                return fresh

        @asynccontextmanager
        async def session_ctx():
            yield session

        record_compile_task = AsyncMock(return_value=True)
        with patch(f"{_MODULE}.get_db_session", session_ctx), \
             patch(f"{_MODULE}.KnowledgeDocumentRepository", Repo), \
             patch(f"{_MODULE}.record_compile_task", record_compile_task):
            await ReconciliationService()._record_openkb_status(
                doc, status="compiling", warnings=["w1"])

        record_compile_task.assert_awaited_once()
        args, kwargs = record_compile_task.await_args
        self.assertEqual(args[1], "tenant-real",
                         "第 2 个位置参数必须是 doc.tenant_id（不是裸 tenant_id）")
        self.assertEqual(kwargs["document_id"], "doc-1")
        self.assertEqual(kwargs["compile_status"], "compiling")
        self.assertEqual(kwargs["kb_id"], "kb-1")
        self.assertEqual(kwargs["content_generation"], 3)

    async def test_status_write_is_committed_not_rolled_back(self):
        """回归：``session.commit()`` 必须到达 —— 证明事务没有中途炸掉。

        修前 ``NameError`` 让提交永不发生，``database.py`` 打出「已回滚」。
        """
        doc = SimpleNamespace(id="doc-2", tenant_id="tenant-real", extra_metadata=None)
        fresh = _FreshDocument()
        session = SimpleNamespace(add=lambda obj: None, commit=AsyncMock())

        class Repo:
            def __init__(self, _session):
                pass

            async def get_required(self, document_id, tenant_id):
                return fresh

        @asynccontextmanager
        async def session_ctx():
            yield session

        with patch(f"{_MODULE}.get_db_session", session_ctx), \
             patch(f"{_MODULE}.KnowledgeDocumentRepository", Repo), \
             patch(f"{_MODULE}.record_compile_task", AsyncMock(return_value=True)):
            await ReconciliationService()._record_openkb_status(doc, status="compiled")

        session.commit.assert_awaited_once()
        self.assertEqual(fresh.llm_wiki_compile_status, "compiled")

    async def test_in_memory_metadata_stays_consistent(self):
        """``doc.extra_metadata['openkb_compile_status']`` 与列保持同步。

        口径是**以库行为准**：代码写 ``meta = dict(fresh.extra_metadata or {})``，
        再补状态键、整体赋回 ``doc``（``doc`` 那份可能已过期，不参与合并）。
        所以这里把「其它键」放在 ``fresh`` 上，验证它们被带过来、而不是被丢掉。
        """
        doc = SimpleNamespace(id="doc-3", tenant_id="t", extra_metadata={"stale": 1})
        fresh = _FreshDocument()
        fresh.extra_metadata = {"keep": 1}
        session = SimpleNamespace(add=lambda obj: None, commit=AsyncMock())

        class Repo:
            def __init__(self, _session):
                pass

            async def get_required(self, document_id, tenant_id):
                return fresh

        @asynccontextmanager
        async def session_ctx():
            yield session

        with patch(f"{_MODULE}.get_db_session", session_ctx), \
             patch(f"{_MODULE}.KnowledgeDocumentRepository", Repo), \
             patch(f"{_MODULE}.record_compile_task", AsyncMock(return_value=True)):
            await ReconciliationService()._record_openkb_status(doc, status="failed",
                                                               error="boom")

        self.assertEqual(doc.extra_metadata["openkb_compile_status"], "failed")
        self.assertEqual(doc.extra_metadata["keep"], 1, "库行上的其它键必须被带过来")
        self.assertEqual(fresh.llm_wiki_compile_status, "failed")

    async def test_wiki_patrol_polls_shared_task_once_and_keeps_live_compilation(self):
        now = datetime.utcnow()
        rows = [SimpleNamespace(id=str(i), tenant_id="tenant", knowledge_base_id="wiki",
                                llm_wiki_task_id="engine-task",
                                llm_wiki_compile_requested_at=now - timedelta(hours=1))
                for i in range(20)]
        session = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(all=lambda: rows)))
        service = ReconciliationService()
        service._patrol_stale_recompile = AsyncMock(return_value=0)
        service._record_openkb_status_by_id = AsyncMock(return_value=True)
        compiler = SimpleNamespace(
            list_compile_tasks=AsyncMock(return_value={"tasks": []}),
            get_compile_status=AsyncMock(return_value={
                "status": "running", "started_at": (now - timedelta(hours=1)).isoformat(),
            }),
        )

        @asynccontextmanager
        async def session_ctx():
            yield session

        with patch(f"{_MODULE}.get_db_session", session_ctx), patch(
            "capabilities.knowledge_base.services.jonex_wiki_service.JonexWikiService",
            return_value=compiler,
        ):
            live = await service.patrol_jonex_wiki_compile()
            self.assertEqual(live["timed_out"], 0)
            compiler.get_compile_status.assert_awaited_once()
            service._record_openkb_status_by_id.assert_not_awaited()
            compiler.get_compile_status.return_value = {"status": "cancelled"}
            stopped = await service.patrol_jonex_wiki_compile()

        self.assertEqual(stopped["failed"], 20)
        self.assertEqual(compiler.get_compile_status.await_count, 2)
        for row, call in zip(rows, service._record_openkb_status_by_id.await_args_list):
            self.assertEqual(call.kwargs["expected_compile"], ("engine-task", row.llm_wiki_compile_requested_at))
            self.assertEqual(call.kwargs["error"], "JONEX_WIKI_COMPILE_CANCELLED")

    async def test_wiki_late_terminal_state_does_not_overwrite_new_attempt(self):
        session = SimpleNamespace(
            execute=AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: None)),
            commit=AsyncMock(),
        )

        @asynccontextmanager
        async def session_ctx():
            yield session

        track = AsyncMock()
        with patch(f"{_MODULE}.get_db_session", session_ctx), patch(f"{_MODULE}.record_compile_task", track):
            applied = await ReconciliationService()._record_openkb_status_by_id(
                "doc", "tenant", status="compiled", expected_compile=("old-task", datetime.utcnow()))

        self.assertFalse(applied)
        stmt = str(session.execute.await_args.args[0])
        self.assertIn("llm_wiki_task_id", stmt.split("WHERE")[1])
        self.assertIn("llm_wiki_compile_requested_at", stmt.split("WHERE")[1])
        track.assert_not_awaited()
        session.commit.assert_not_awaited()


# ── 类级闸门：把「未定义名」这种静默失败变成红灯 ──────────────────────────

def _undefined_names(path: pathlib.Path):
    """返回 ``(函数起始行, 函数名, 名字)`` —— 作用域里引用但根本没定义的名字。

    用 stdlib ``symtable`` 而不是正则/AST 手搓：它**正确建模闭包与嵌套作用域**，
    不会把「嵌套函数引用外层变量」误报（手搓版会，实测 18 处里 17 处是误报）。
    """
    table = symtable.symtable(path.read_text(encoding="utf-8"), str(path), "exec")
    module_names = {s.get_name() for s in table.get_symbols()}
    found = []

    def walk(tbl):
        for sym in tbl.get_symbols():
            name = sym.get_name()
            if sym.is_assigned() or sym.is_imported() or sym.is_parameter():
                continue
            # 只看「既不是自由变量（闭包捕获）、也不是局部变量」的引用
            if sym.is_referenced() and not sym.is_free() and not sym.is_local():
                if (name not in module_names and not hasattr(builtins, name)
                        and not name.startswith("__")):
                    found.append((tbl.get_lineno(), tbl.get_name(), name))
        for child in tbl.get_children():
            walk(child)

    walk(table)
    return sorted(set(found))


def test_backend_has_no_undefined_names():
    """后端不得存在「引用了但没定义」的名字（本地变量注解不算运行时求值，已排除）。

    这类 bug 的共同形态是**静默**：被 ``except Exception`` 吞成一条 WARNING、
    只在线上翻日志才看得见，而单元测试只要不覆盖那条分支就永远绿。
    `2026-10-09` 的 ``tenant_id`` 就是这么漏到线上的。
    """
    repo = pathlib.Path(__file__).resolve().parents[3]
    offenders = []
    for root in ("capabilities", "jonex_core", "api_gateway"):
        for path in (repo / root).rglob("*.py"):
            if "/tests/" in str(path) or "/.venv" in str(path):
                continue
            for lineno, func, name in _undefined_names(path):
                offenders.append(f"{path.relative_to(repo)}:{lineno} {func}() 引用未定义名 {name!r}")

    assert not offenders, "存在未定义名（运行时会 NameError）：\n" + "\n".join(offenders)
