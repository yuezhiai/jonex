# [jonex] 新增文件 — Jonex Wiki 编译服务
"""knowledge_base 侧 jonex_wiki 编译/查询/删除编排服务。

通过 RemoteJonexWikiClient → Sidecar /invoke 调用 jonex_wiki 适配器容器。
与 OpenKB（KnowledgeCompilerService）镜像，但三处关键差异：
- jonex_wiki 无 init_kb（首次 ingest 即建 KB）；
- compile_parsed_document 的 parsed_artifact 是 ingest 形状
  ``{source_path, source_name, document_id}``（engine 端 ``POST /ingest``），
  不是 openkb 的 ``{document_id, source_file_path, parsed_markdown_path, ...}``；
- list_graph 由引擎把 Wiki 页面 + 页间 links 投影为图谱（jonex_wiki 不产
  结构化本体图，但编译产出的 Wiki 页间链接可作为实体/关系图）。

DB 辅助方法（claim / set_task_id / record_status / schema version）引擎无关，
都写 ``llm_wiki_compile_*`` 列——直接复用 openkb 的 ``KnowledgeCompilerService``
（它只用 DB、不碰 openkb client），不复制粘贴这些 SQL。
"""
import logging
import os
from pathlib import Path

from jonex_core.capability.atomic.jonex_wiki.client import get_jonex_wiki_client
from jonex_core.common.database import get_db_session
from jonex_core.common.exceptions import ResourceConflictError, ResourceNotFoundError
from jonex_core.common.i18n import translate
from jonex_core.common.object_storage import get_object_storage_for

from .openkb_service import KnowledgeCompilerService

logger = logging.getLogger(__name__)

_NATIVE_SHEET_SUFFIXES = {".xlsx", ".xlsm"}

# [jonex] MinerU 结构化产物文件名：平台对 jonex_wiki 不做 markdown 重组，直写
# 原始 content_list.json（对齐独立部署 parse task 的 MINERU 产物）。
_JONEX_WIKI_CONTENT_LIST = "content_list.json"

# [jonex] 引擎冲突的机器判据：引擎 DomainError 带稳定 code（conflict / not_found /
# validation_failed …，kernel/errors.py:15-22），适配器把它原样透传进信封
# details.error_code（jonex_wiki_adapter.py:270-272），客户端再挂到异常的 details 上。
# **绝不认 message 文案**：那句「同知识库已存在同名文档」是引擎里硬编码的人话，
# 引擎或适配器一旦做本地化/改词，靠文案分支的自愈就会**静默失效**（变死代码）。
_ENGINE_ERROR_CODE_KEY = "engine_error_code"
_ENGINE_CONFLICT_CODE = "conflict"


def is_engine_document_conflict(exc: BaseException) -> bool:
    """[jonex] 该异常是否为引擎的文档级冲突（err_code=conflict）。

    只看稳定机器码；拿不到机器码（非引擎错误、或旧信封没带）一律判 False → 调用方
    保持原样抛出，不做任何猜测性动作。
    """
    details = getattr(exc, "details", None) or {}
    return str(details.get(_ENGINE_ERROR_CODE_KEY) or "") == _ENGINE_CONFLICT_CODE


def jonex_wiki_artifact_path(document_id: str) -> Path:
    """[jonex] jonex_wiki 解析产物 content_list.json 的绝对路径。

    与 openkb_artifact_path 镜像，但 jonex_wiki 拿 MinerU 结构化 content_list
    （不做 markdown 重组），路径约定 {KB_INPUT_DIR}/parsed/{document_id}/content_list.json。
    """
    inputs_root = os.getenv("KB_INPUT_DIR", "/app/inputs")
    return Path(inputs_root) / "parsed" / document_id / _JONEX_WIKI_CONTENT_LIST


def jonex_wiki_artifact_exists(document_id: str) -> bool:
    """[jonex] 判断 jonex_wiki 解析产物是否已在共享卷上。"""
    return jonex_wiki_artifact_path(document_id).is_file()


async def stage_jonex_wiki_ingest_source(
    document_id: str, *, file_name: str = "", storage_key: str = "",
    storage_backend: str = "", file_path: str = "",
) -> str:
    """返回 jonex_wiki ingest 用的相对 source_path。

    原生表格(.xlsx/.xlsm)把**原始二进制**落到共享卷 ``parsed/{document_id}/source{ext}``，
    让引擎按扩展名走 ``SPREADSHEET_NATIVE`` 确定性解析;其余文档转发 MinerU 结构化
    产物 ``content_list.json``(引擎走 MINERU 路由,不做 markdown 重组)。
    """
    suffix = Path(file_name or "").suffix.lower()
    if suffix not in _NATIVE_SHEET_SUFFIXES:
        return f"parsed/{document_id}/{_JONEX_WIKI_CONTENT_LIST}"

    inputs_root = Path(os.getenv("KB_INPUT_DIR", "/app/inputs"))
    dst_dir = inputs_root / "parsed" / document_id
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst = dst_dir / f"source{suffix}"

    storage = get_object_storage_for(storage_backend)
    key = storage_key or file_path
    data = await storage.get_bytes(key) if key else None
    if data is None and file_path:
        p = Path(file_path)
        if p.is_file():
            data = p.read_bytes()
    if data is None:
        raise ResourceNotFoundError(
            message=translate(
                "err.jonex_wiki.no_original_source",
                fallback="无法读取文档原始文件，无法按原生表格入库，请先执行「重新解析」",
            ),
            details={"document_id": document_id},
        )
    dst.write_bytes(data)
    return f"parsed/{document_id}/source{suffix}"


class JonexWikiService:
    """jonex_wiki 编译服务 — 编排 jonex_wiki 编译任务（镜像 KnowledgeCompilerService）。"""

    def __init__(self):
        self._client = get_jonex_wiki_client()
        # [jonex] DB 辅助方法复用 openkb 的 KnowledgeCompilerService（引擎无关，
        # 都写 llm_wiki_compile_* 列）。它只用 DB、不碰 openkb client。
        self._db = KnowledgeCompilerService()

    # ── 基础能力（对 jonex_wiki 容器的薄封装）──

    async def init_kb(self, kb_name: str, tenant_id: str, kb_id: str) -> dict:
        """[jonex] jonex_wiki 无 init_kb：首次 ingest 即建 KB。no-op + 日志。

        不抛错、不调 client——调用方（KB 创建一条龙）按「无 init 步骤」继续。
        """
        logger.info("[jonex_wiki] init_kb 为 no-op（首次 ingest 即建 KB） kb=%s", kb_name)
        return {
            "kb": kb_name,
            "created": False,
            "note": "jonex_wiki 首次 ingest 即建",
        }

    async def compile_document(
        self,
        kb_name: str,
        tenant_id: str,
        kb_id: str,
        parsed_artifact: dict,
    ) -> dict:
        """将 Jonex parsed artifact 送入 jonex_wiki ingest 管道。

        parsed_artifact 是 ingest 形状 {source_path, source_name, document_id}。

        [jonex] 同名孤儿自愈：引擎按 (tenant_id, kb_id, source_name) 认文档，且要求
        document_id 与本次一致（application.py:250-254）。删除传播失败时引擎会留下
        一条 deleted_at 仍为 NULL 的孤儿——`document_service.delete_document` 对引擎侧
        remove_document 的异常只 logger.warning 吞掉（document_service.py:1927-1928），
        却照样软删平台行。那条孤儿会把文件名永久占住，使同名重传后的「重新编译」
        「重新解析」全部报「同知识库已存在同名文档」，且**删了再传也绕不过**（占位的是
        引擎那条记录，与平台当前是哪条文档无关）。

        这里触发只认引擎透传的稳定 err_code（conflict），占位者只从**平台自己的数据**
        推：引擎占位的 document_id 必定曾是平台文档，而删除传播失败时平台行已软删、
        引擎行还活着 —— 所以「本 KB 内已软删的同名文档」就是孤儿的唯一合法来源。
        全程不读引擎报错文案，也就不会因引擎本地化/改词而静默失效。
        """
        try:
            return await self._client.compile_parsed_document(
                kb_name=kb_name,
                parsed_artifact=parsed_artifact,
                tenant_id=tenant_id,
                kb_id=kb_id,
            )
        except Exception as exc:
            if not is_engine_document_conflict(exc):
                raise
            # source_name 就是送给引擎的文件名（引擎认文档用的键），直接拿它匹配。
            source_name = str(parsed_artifact.get("source_name") or "")
            stale_ids = (
                await self._deleted_same_name_documents(
                    tenant_id, kb_id, source_name,
                    exclude_id=str(parsed_artifact.get("document_id") or ""),
                ) if source_name else []
            )
            if not stale_ids:
                # 没有「已软删的同名平台文档」→ 引擎占位者不是删除遗留：可能是真·同名
                # 冲突（另一条活跃文档占着名字），也可能冲突与文件名无关。交回调用方，
                # 不猜、也不动引擎数据。
                raise
            released = False
            for stale_id in stale_ids:
                if await self._release_engine_orphan(kb_name, tenant_id, kb_id, stale_id):
                    released = True
            if not released:
                raise
            # 只重试一次：孤儿已让出名字，再撞只可能是新问题，直接抛。
            return await self._client.compile_parsed_document(
                kb_name=kb_name,
                parsed_artifact=parsed_artifact,
                tenant_id=tenant_id,
                kb_id=kb_id,
            )

    async def compile_documents(self, kb_name: str, tenant_id: str, kb_id: str,
                                parsed_artifacts: list[dict]) -> dict:
        try:
            return await self._client.compile_parsed_documents(kb_name, parsed_artifacts, tenant_id, kb_id)
        except Exception as exc:
            if not is_engine_document_conflict(exc):
                raise
            released = False
            for artifact in parsed_artifacts:
                stale_ids = await self._deleted_same_name_documents(
                    tenant_id, kb_id, artifact["source_name"], exclude_id=artifact["document_id"])
                for stale_id in stale_ids:
                    released |= await self._release_engine_orphan(kb_name, tenant_id, kb_id, stale_id)
            if not released:
                raise
            return await self._client.compile_parsed_documents(kb_name, parsed_artifacts, tenant_id, kb_id)

    async def _deleted_same_name_documents(
        self, tenant_id: str, kb_id: str, source_name: str, exclude_id: str,
    ) -> list[str]:
        """[jonex] 本 KB 内**已软删**的同名文档 id（排除本次要编译的那条）。

        只查平台自己的表：删除传播失败会留下「平台已软删 / 引擎仍活跃」的一对，这些
        软删行正是引擎孤儿的来源；一条都查不到就说明冲突不是删除遗留。
        """
        from sqlalchemy import select

        from ..models import KnowledgeDocument

        conditions = [
            KnowledgeDocument.tenant_id == tenant_id,
            KnowledgeDocument.knowledge_base_id == kb_id,
            KnowledgeDocument.file_name == source_name,
            KnowledgeDocument.is_deleted == 1,
        ]
        if exclude_id:
            conditions.append(KnowledgeDocument.id != exclude_id)
        async with get_db_session() as session:
            rows = (await session.execute(
                select(KnowledgeDocument.id).where(*conditions)
            )).scalars().all()
        return list(rows)

    async def _release_engine_orphan(
        self, kb_name: str, tenant_id: str, kb_id: str, document_id: str,
    ) -> bool:
        """[jonex] 清掉引擎侧同名孤儿（soft-delete + 引擎侧合并触发一次编译）。

        返回 True = 名字已让出、可以重试；False = 清理失败，调用方应把原始的同名冲突
        原样抛给用户（不掩盖、也不假装成功）。
        """
        try:
            await self._client.remove_document(
                kb_name=kb_name, document_id=document_id,
                tenant_id=tenant_id, kb_id=kb_id,
            )
        except ResourceNotFoundError:
            # 引擎侧已不存在/已软删：名字本来就空着，直接重试即可。
            logger.info(
                "[jonex_wiki] 同名孤儿已不在引擎侧，直接重试编译: kb=%s stale_doc=%s",
                kb_id, document_id,
            )
            return True
        except Exception:
            logger.warning(
                "[jonex_wiki] 同名孤儿清理失败，保持原同名冲突上报: kb=%s stale_doc=%s",
                kb_id, document_id, exc_info=True,
            )
            return False
        logger.warning(
            "[jonex_wiki] 已清理删除传播失败留下的引擎同名孤儿，重试编译: kb=%s stale_doc=%s",
            kb_id, document_id,
        )
        return True

    async def search_compiled_knowledge(
        self, kb_name: str, tenant_id: str, kb_id: str, question: str,
        return_trace: bool = False,  # [jonex]
        thinking_mode: str | None = None,
        progress_session: str | None = None,
    ) -> dict:
        """搜索已编译的 Wiki 知识。"""
        options = {"thinking_mode": thinking_mode} if thinking_mode is not None else {}
        if progress_session:
            options["progress_session"] = progress_session
        return await self._client.query(
            kb_name=kb_name, question=question, tenant_id=tenant_id, kb_id=kb_id,
            return_trace=return_trace,
            **options,
        )

    async def remove_document(
        self, kb_name: str, tenant_id: str, kb_id: str, document_id: str,
    ) -> dict:
        """删除 jonex_wiki 中指定 document_id 的编译产物。"""
        return await self._client.remove_document(
            kb_name=kb_name, document_id=document_id,
            tenant_id=tenant_id, kb_id=kb_id,
        )

    async def delete_kb(self, kb_name: str, tenant_id: str, kb_id: str) -> dict:
        """删除 jonex_wiki KB 目录。"""
        return await self._client.delete_kb(
            kb_name=kb_name, tenant_id=tenant_id, kb_id=kb_id,
        )

    async def get_graph(self, kb_name: str, tenant_id: str, kb_id: str,
                        document_id: str = "") -> dict:
        """列出 jonex_wiki 的 entities/relationships（文档级过滤可选）。

        引擎把 Wiki 页面（concept/table）与页间 links 投影为图谱，这里透传
        list_graph 保持与 openkb 相同的 entities/relationships 契约。
        """
        return await self._client.list_graph(
            kb_name=kb_name, tenant_id=tenant_id, kb_id=kb_id,
            document_id=document_id,
        )

    async def read_page(self, kb_name: str, tenant_id: str, kb_id: str, path: str) -> dict:
        """读取 Wiki 页面 markdown（供 parse_result_service 透传）。"""
        return await self._client.read_page(
            kb_name=kb_name, path=path, tenant_id=tenant_id, kb_id=kb_id,
        )

    async def list_wiki_contents(self, kb_name: str, tenant_id: str, kb_id: str,
                                 document_id: str = "", view: str = "") -> dict:
        """列出 Wiki 页面树（action list_wiki_contents），文档级过滤可选。

        view="counts"：只返回三类页数（编译资产 tab 的统计卡与 tab 计数），
        不返回页面条目——避免为三个数字搬运上千个页面对象。
        """
        return await self._client.list_wiki_contents(
            kb_name=kb_name, tenant_id=tenant_id, kb_id=kb_id,
            document_id=document_id, view=view,
        )

    async def get_wiki_backlinks(self, kb_name: str, path: str, tenant_id: str, kb_id: str,
                                 document_id: str = "") -> dict:
        """「链入本页」：谁链接到了这一页（action get_wiki_backlinks）。"""
        return await self._client.get_wiki_backlinks(
            kb_name=kb_name, path=path, tenant_id=tenant_id, kb_id=kb_id,
            document_id=document_id,
        )

    async def get_wiki_claims(self, kb_name: str, tenant_id: str, kb_id: str, *,
                              path: str = "", document_id: str = "",
                              limit: int = 500) -> dict:
        """claim 级溯源明细（action get_wiki_claims）。"""
        return await self._client.get_wiki_claims(
            kb_name=kb_name, tenant_id=tenant_id, kb_id=kb_id,
            path=path, document_id=document_id, limit=limit,
        )

    async def get_outline(self, kb_name: str, tenant_id: str, kb_id: str,
                          document_id: str = "") -> dict:
        """读 Wiki 阅读目录（action list_wiki_outline），文档级过滤可选。"""
        return await self._client.get_outline(
            kb_name=kb_name, tenant_id=tenant_id, kb_id=kb_id,
            document_id=document_id,
        )

    async def draft_outline(self, kb_name: str, tenant_id: str, kb_id: str,
                            request: dict, user_id=None) -> dict:
        """LLM 起草目录（action draft_wiki_outline），覆盖旧 active。"""
        return await self._client.draft_outline(
            kb_name=kb_name, tenant_id=tenant_id, kb_id=kb_id,
            request=request, user_id=user_id,
        )

    async def adjust_outline(self, kb_name: str, tenant_id: str, kb_id: str,
                             request: dict, user_id=None) -> dict:
        """按指令调整目录（action adjust_wiki_outline）。"""
        return await self._client.adjust_outline(
            kb_name=kb_name, tenant_id=tenant_id, kb_id=kb_id,
            request=request, user_id=user_id,
        )

    async def get_compile_status(self, kb_name: str, tenant_id: str, kb_id: str,
                                 task_id: str) -> dict:
        """查询单个编译任务状态（排障/手工验证用）。"""
        return await self._client.get_compile_status(
            kb_name=kb_name, task_id=task_id, tenant_id=tenant_id, kb_id=kb_id,
        )

    async def list_compile_tasks(self, kb_name: str, tenant_id: str, kb_id: str,
                                 document_id: str = "") -> dict:
        """列出该 KB 的编译任务（对账巡检批量拉取；document_id 可选过滤）。"""
        return await self._client.list_compile_tasks(
            kb_name=kb_name, tenant_id=tenant_id, kb_id=kb_id,
            document_id=document_id,
        )

    async def recompile_document(
        self, kb_name: str, tenant_id: str, kb_id: str, document_id: str,
        *, source_file_path: str = "", title: str = "", file_name: str = "",
        storage_key: str = "", storage_backend: str = "",
    ) -> dict:
        """[jonex] 按共享卷上已有的解析产物重新编译（ingest 形状，不重新解析）。

        语义与 openkb 的 recompile_document 一致（「以 Jonex parsed artifact 为
        权威源重编译 Wiki」），但产物形状改为 jonex_wiki ingest 形状：
        ``{source_path, source_name, document_id}``。

        原生表格(.xlsx/.xlsm)重新入库时转发原始二进制(走 SPREADSHEET_NATIVE)；
        其余文档转发 MinerU 结构化产物 content_list.json(走 MINERU 路由)。

        产物不存在时抛 ResourceNotFoundError（调用方已前置校验过，这里是兜底）。
        """
        suffix = Path(file_name or "").suffix.lower()
        if suffix in _NATIVE_SHEET_SUFFIXES:
            rel_source = await stage_jonex_wiki_ingest_source(
                document_id, file_name=file_name, storage_key=storage_key,
                storage_backend=storage_backend, file_path=source_file_path,
            )
        else:
            art_abs = jonex_wiki_artifact_path(document_id)
            if not art_abs.is_file():
                raise ResourceNotFoundError(
                    message=translate(
                        "err.jonex_wiki.no_parsed_artifact",
                        fallback="未找到该文档的解析产物，无法重新编译，请先执行「重新解析」",
                    ),
                    details={"document_id": document_id, "expected_path": str(art_abs)},
                )
            # 传「相对 inputs 卷根」路径：两端各自解析（platform 与 jonex_wiki 挂载点不同）
            rel_source = f"parsed/{document_id}/{_JONEX_WIKI_CONTENT_LIST}"

        artifact = {
            "source_path": rel_source,
            "source_name": title or file_name or document_id,
            "document_id": document_id,
        }

        return await self.compile_document(
            kb_name=kb_name, tenant_id=tenant_id, kb_id=kb_id,
            parsed_artifact=artifact,
        )

    # ── [jonex] 重新编译编排（从 ontology_service.retry_extract 调用）──

    async def queue_task_compile(
        self, tenant_id: str, document_id: str, kb_id: str, execution_id: str,
    ) -> None:
        """Persist this execution's batch request; the existing patrol submits it."""
        from ..models import DocStatus
        from ..models.task_center import ACTIVE_STATES
        from ..repository import KnowledgeDocumentRepository
        from ..repository.task_center_repository import TaskCenterDocumentRepository

        async with get_db_session() as session:
            # Fence cancellation/replay with the same row that owns this step.
            owner = await TaskCenterDocumentRepository(session).read(
                document_id, tenant_id, for_update=True,
            )
            if (owner is None or owner.state not in ACTIVE_STATES
                    or owner.current_step != "compile" or owner.execution_id != execution_id):
                raise ResourceConflictError(message=translate(
                    "err.kb.task_center.takeover_closed",
                    params={"task_id": owner.task_id if owner else "", "state": "<changed>"},
                    fallback="Wiki 编译步骤已交还或被替换",
                ))
            doc = await KnowledgeDocumentRepository(session).get_required(document_id, tenant_id)
            if doc.knowledge_base_id != kb_id or doc.status == DocStatus.DELETING.value:
                raise ResourceConflictError(message=translate(
                    "err.jonex_wiki.doc_busy", fallback="文档不属于该知识库或正在删除",
                ))
            metadata = dict(doc.extra_metadata or {})
            if metadata.get("jonex_wiki_compile_execution_id") == execution_id:
                return  # A repeated dispatch must not reset an already submitted batch.
            parsed_execution = metadata.get("jonex_wiki_compile_parse_execution_id")
            if parsed_execution and parsed_execution == owner.handled_execution_id:
                # The batch included this parsed source before compile concurrency
                # allowed its handler to run. Join that result instead of resubmitting.
                metadata.pop("jonex_wiki_compile_parse_execution_id")
                metadata["jonex_wiki_compile_execution_id"] = execution_id
                doc.extra_metadata = metadata
                await session.commit()
                return
            if doc.llm_wiki_compile_status == "compiling":
                raise ResourceConflictError(message=translate(
                    "err.jonex_wiki.already_compiling", fallback="该文档正在编译中，请等待完成后再重试",
                ))
            metadata["jonex_wiki_compile_execution_id"] = execution_id
            doc.extra_metadata = metadata
            doc.llm_wiki_compile_status = "stale"
            doc.llm_wiki_task_id = None
            doc.llm_wiki_compile_error = None
            doc.llm_wiki_compile_warnings = None
            doc.llm_wiki_compile_requested_at = None
            doc.llm_wiki_compiled_at = None
            await session.commit()

    async def recompile_jonex_wiki(
        self, tenant_id: str, document_id: str, kb_id: str,
        *, doc_status: str = "", source_file_path: str = "", title: str = "",
        from_task_chain: bool = False,
    ) -> dict:
        """[jonex] jonex_wiki「重新编译」：按共享卷已有产物重跑 jonex_wiki 编译。

        任务化语义（对照 recompile_openkb）：claim（列）→ 提交 ingest（容器内后台
        执行）→ task_id 写列 → 立即返回。终态由对账巡检 patrol_jonex_wiki_compile
        轮询回写。

        ── `from_task_chain`：这次是任务中心派的 `compile` 步 ──

        为真时**跳过解析期的忙校验**（`PARSING` / `PENDING` / `INGESTING`），只拦
        `DELETING`（那个是真危险：删到一半再编译，产物随时会消失）。

        为什么链上可以跳：链上这一步**只可能在解析步成功之后才跑到**（先后由编排的
        `depends_on` 保证），而那条忙校验防的是**用户手点**撞上 MinerU 正在覆盖写解析
        产物。不跳会被一个很窄的窗误伤 —— 看门狗是「先上报、后把文档置 READY」，
        任务中心消化完上报就派这一步，那一刻文档行可能还停在 `parsing`，忙校验会把一个
        其实已经解析成功的活**当场拒掉**（这一步被判失败，而解析明明好好的）。

        ⚠️ 链上这一档**必须由调用方显式传**（默认为假）：它是「我是任务中心那条路」的
        声明，与 `OntologyService.retry_extract` 的 `strict` 是同性质的东西 —— 判据同源，
        别加第二个开关。（形状与 `recompile_openkb` 的同名参数逐字对齐。）
        """
        from ..models import DocStatus

        if from_task_chain:
            # 链上来的：只读一次当前状态，只拦「正在删除」。`doc_status` 那一档不用
            # 调用方给 —— 读行上的是权威，也省得链上那个窄窗里拿到过期值。
            status_now = await self._current_doc_status(tenant_id, document_id)
            if status_now == DocStatus.DELETING.value:
                raise ResourceConflictError(
                    message=translate("err.jonex_wiki.doc_busy",
                                      fallback="文档正在解析/入库/删除中，请等待完成后再重新编译"),
                    details={"document_id": document_id, "status": status_now},
                )
        else:
            # ── 忙碌校验：避免与 MinerU 覆盖写 content_list.json 竞争 ──
            busy = {DocStatus.PARSING.value, DocStatus.PENDING.value,
                    DocStatus.INGESTING.value, DocStatus.DELETING.value}
            if doc_status in busy:
                raise ResourceConflictError(
                    message=translate("err.jonex_wiki.doc_busy",
                                      fallback="文档正在解析/入库/删除中，请等待完成后再重新编译"),
                    details={"document_id": document_id, "status": doc_status},
                )

        # ── 产物前置校验：缺失时立即报错，不 claim、不投任务（避免假成功）──
        if not jonex_wiki_artifact_exists(document_id):
            raise ResourceNotFoundError(
                message=translate("err.jonex_wiki.no_parsed_artifact",
                                  fallback="未找到该文档的解析产物，无法重新编译，请先执行「重新解析」"),
                details={"document_id": document_id},
            )

        # ── [jonex] LLM-Wiki Schema fencing（方案 §6）：读 active 版本 → claim
        # 绑定 target。无 schema 返回 None = 未启用 fencing，按旧行为编译。──
        target_ver = await self.get_active_schema_version(tenant_id, kb_id)

        # ── 原子 claim（列）：状态非 compiling 才抢到（防并发双投）──
        if not await self.claim_compile(tenant_id, document_id,
                                        target_schema_version=target_ver):
            raise ResourceConflictError(
                message=translate("err.jonex_wiki.already_compiling",
                                  fallback="该文档正在编译中，请等待完成后再重试"),
                details={"document_id": document_id},
            )

        # ── 读原始文档元数据（file_name/storage），供原生表格转发原始二进制 ──
        from ..repository import KnowledgeDocumentRepository
        async with get_db_session() as session:
            repo = KnowledgeDocumentRepository(session)
            doc = await repo.get_required(document_id, tenant_id)
            file_name = doc.file_name or ""
            storage_key = doc.storage_key or ""
            storage_backend = doc.storage_backend or ""
            file_path = doc.file_path or ""
            doc_dict = doc.to_dict()

        # ── 提交任务（复用 recompile_document 的产物校验/相对路径拼装）──
        # 投递失败必须回滚 claim，否则永久卡 compiling
        try:
            result = await self.recompile_document(
                kb_name=kb_id, tenant_id=tenant_id, kb_id=kb_id,
                document_id=document_id,
                source_file_path=file_path, title=title,
                file_name=file_name, storage_key=storage_key,
                storage_backend=storage_backend,
            )
            await self.set_task_id(tenant_id, document_id, result["task_id"])
        except Exception as exc:
            # [jonex] 「KB 忙」不是真失败：与 _dispatch_jonex_wiki_compile 同一条不变量——
            # 回退为可重试的 stale，交给编译巡检重投；否则文档会永久停在 failed
            # （巡检只捞 compiling，不会自愈）。判据复用既有 marker 集合，不新造概念。
            from .reconciliation_service import _is_jonex_wiki_compile_slot_busy

            if _is_jonex_wiki_compile_slot_busy(exc):
                await self._record_openkb_status(
                    tenant_id, document_id, status="stale",
                    error="JONEX_WIKI_COMPILE_BUSY: 该知识库已有编译任务在执行，已排队等待重投",
                )
                logger.warning(
                    "[jonex] jonex_wiki 重新编译让位（KB 已有运行中编译，回退 stale 待重投）: "
                    "doc=%s", document_id,
                )
                raise ResourceConflictError(
                    message="该知识库已有编译任务在执行，已排队等待重投",
                    details={"document_id": document_id},
                ) from exc
            await self._record_openkb_status(
                tenant_id, document_id, status="failed",
                error=f"JONEX_WIKI_RECOMPILE_SUBMIT_FAILED: {exc}"[:1000],
            )
            raise

        return doc_dict

    # ── [jonex] DB 辅助方法（薄委托 openkb KnowledgeCompilerService，引擎无关）──

    async def _current_doc_status(self, tenant_id: str, document_id: str) -> str:
        """读文档行当前状态（给「链上来的」那一档判 `DELETING` 用）。

        与上面几个一样是**薄委托** `self._db`（`KnowledgeCompilerService`，引擎无关的 DB 层）。
        """
        return await self._db._current_doc_status(tenant_id, document_id)

    async def get_active_schema_version(self, tenant_id: str, kb_id: str) -> int | None:
        """读 KB 当前 active LLM-Wiki Schema 版本（编译绑定用，方案 §6）。"""
        return await self._db.get_active_schema_version(tenant_id, kb_id)

    async def claim_compile(self, tenant_id: str, document_id: str, *,
                            target_schema_version: int | None = None,
                            batch: bool = False) -> bool:
        """原子抢占编译权（列化）：状态非 compiling 才置 compiling。"""
        if batch:
            from ..models.task_center import ACTIVE_STATES
            from ..repository import KnowledgeDocumentRepository
            from ..repository.task_center_repository import TaskCenterDocumentRepository

            async with get_db_session() as session:
                owner = await TaskCenterDocumentRepository(session).read(
                    document_id, tenant_id, for_update=True,
                )
                doc = await KnowledgeDocumentRepository(session).get_required(document_id, tenant_id)
                # Task-center parsing does not pass through the legacy hook
                # that writes stale. NULL is also a ready, never-compiled source.
                if doc.status != "ready" or doc.llm_wiki_compile_status not in (None, "stale"):
                    return False
                metadata = dict(doc.extra_metadata or {})
                parsed = False
                if owner is not None and owner.state in ACTIVE_STATES:
                    parsed = (owner.current_step == "parse" and owner.execution_id is None
                              and owner.handled_execution_id is not None)
                    queued = (owner.current_step == "compile" and owner.execution_id is not None
                              and metadata.get("jonex_wiki_compile_execution_id") == owner.execution_id)
                    if not (parsed or queued):
                        return False
                claimed = await self._db.claim_compile(
                    tenant_id, document_id, target_schema_version=target_schema_version,
                )
                if claimed and parsed:
                    # The owner lock makes a concurrently arriving compile handler
                    # wait until it can see that its parsed source joined this batch.
                    metadata["jonex_wiki_compile_parse_execution_id"] = owner.handled_execution_id
                    doc.extra_metadata = metadata
                return claimed
        return await self._db.claim_compile(
            tenant_id, document_id, target_schema_version=target_schema_version,
        )

    async def set_task_id(self, tenant_id: str, document_id: str, task_id: str) -> None:
        """提交任务后把容器 task_id 写列（patrol 按它轮询）。"""
        return await self._db.set_task_id(tenant_id, document_id, task_id)

    async def _record_openkb_status(self, tenant_id: str, document_id: str, *,
                                    status: str, error: str = "", warnings=None) -> None:
        """回写 LLM-Wiki 编译状态（列化），复用 openkb 的引擎无关实现。"""
        return await self._db._record_openkb_status(
            tenant_id, document_id, status=status, error=error, warnings=warnings,
        )
