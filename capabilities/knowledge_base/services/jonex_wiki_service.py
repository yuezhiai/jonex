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


# ── [jonex] 编译前「解析身份」预检（整库级）──────────────────────────────────
# 一次 jonex_wiki 编译产出的是**整库一个 bundle**：引擎 source_port 会拉本库全部已
# 解析文档，任何一份产物的解析身份与当前解析规则不一致，就在「取源材料」阶段硬失败
# （跳过它 = 静默丢页）。于是**单文档「重新编译」照样会被别的文档卡住**。
# 判据只有引擎能回答（规则清单/代码/打包预算/体检策略/编码/MinerU 版本），平台只读
# 结论、不复制第二份。预检是**增强项**：引擎没实现或此刻读不到，一律放行 —— 不然一次
# 只读调用故障就会让所有「重新编译」全线不可用，而下游本来就有硬校验兜底。

async def fetch_stale_parsed_sources(client, tenant_id: str, kb_id: str) -> list[dict]:
    """本库需要重新解析的文档（读不到就当没有，绝不阻断编译）。"""
    try:
        result = await client.parse_status(
            kb_name=kb_id, tenant_id=tenant_id, kb_id=kb_id)
    except NotImplementedError:
        return []
    except Exception as exc:   # noqa: BLE001 — 预检绝不阻断编译
        logger.warning(
            "[jonex] 解析身份预检不可用，跳过（不影响编译）: kb=%s err=%s", kb_id, exc)
        return []
    documents = (result or {}).get("documents") or []
    return [item for item in documents if item.get("needs_reparse")]


def raise_unhealable_stale(kb_id: str, stale: list[dict]) -> None:
    """[jonex] 过期且**搬不动**时才拒编译：把是哪几份、过期在哪一项一起交给调用方。

    「搬不动」= 平台侧文档不在 ready（还在解析 / 解析失败）或共享卷上没有解析产物 ——
    这两种情况重编一定失败，提前说清楚比让引擎在取源材料阶段抛 ValidationError 强。
    """
    names = [str(item.get("source_name") or item.get("document_id") or "")
             for item in stale]
    shown = "、".join(names[:3]) + ("…" if len(names) > 3 else "")
    raise ResourceConflictError(
        message=translate(
            "err.jonex_wiki.stale_parse_identity",
            params={"count": len(stale), "names": shown},
            fallback=(f"本知识库有 {len(stale)} 份文档的解析产物缺失或尚未解析完成（{shown}），"
                      "无法编译；请先对这些文档执行「重新解析」，等它们完成后再次编译"),
        ),
        details={
            "kb_id": kb_id,
            "needs_reparse_count": len(stale),
            # 每份带 drift：identity 里到底哪一项变了（["unknown"] = 旧产物没记分量）
            "documents": [
                {"document_id": item.get("document_id"),
                 "source_name": item.get("source_name"),
                 "status": item.get("status"),
                 "drift": item.get("drift") or [],
                 "refresh_method": item.get("refresh_method")}
                for item in stale
            ],
        },
    )


async def assert_kb_parsed_sources_current(tenant_id: str, kb_id: str) -> None:
    """模块级入口：用户点按钮那条路（ontology_service）的预检。

    **只做自愈**（把已解析好的过期产物重新 ingest 一遍），搬不动的才拒 —— 见
    `JonexWikiService.refresh_stale_sources` 里「为什么必须自愈，而不是拒绝」。

    构造服务失败（D 只是增强项）就记一条 warning 放行：下游 `recompile_jonex_wiki`
    还会再预检一次，不会漏。
    """
    try:
        service = JonexWikiService()
        await service.refresh_stale_sources(tenant_id, kb_id)
    except ResourceConflictError:
        raise
    except Exception as exc:   # noqa: BLE001 — 预检绝不阻断编译
        logger.warning("[jonex] 解析身份预检不可用，跳过（不影响编译）: kb=%s err=%s", kb_id, exc)


async def stage_jonex_wiki_ingest_source(
    document_id: str, *, file_name: str = "", storage_key: str = "",
    storage_backend: str = "", file_path: str = "",
    tenant_id: str = "", kb_id: str = "", content_generation: int | None = None,
    ingest_backend: str = "",
    source_context: dict | None = None,
) -> str:
    """返回 jonex_wiki ingest 用的相对 source_path。

    原件、版本化解析观察和资产组成不可变消费目录；原生表格以原件为入口，
    其他模态以 content_list.json 为入口，均通过同一份范围和代次回执校验。
    """
    from .wiki_input_delivery import publish_input_bundle
    if not tenant_id or not kb_id:
        raise ValueError("WIKI_INPUT_SCOPE_REQUIRED")
    if content_generation is None or not ingest_backend:
        from ..repository import KnowledgeDocumentRepository
        from .ingest_backend import backend_for_document
        async with get_db_session() as session:
            row = await KnowledgeDocumentRepository(session).get_required(document_id, tenant_id)
            if row.knowledge_base_id != kb_id:
                raise ValueError("WIKI_INPUT_SCOPE_MISMATCH")
            content_generation = row.content_generation
            ingest_backend = backend_for_document(row)
            source_context = source_context or dict(row.extra_metadata or {})
    inputs_root = Path(os.getenv("KB_INPUT_DIR", "/app/inputs")).resolve()
    storage = get_object_storage_for(storage_backend)
    key = storage_key or file_path
    legacy = inputs_root / "parsed" / document_id
    if legacy.resolve().parent != (inputs_root / "parsed").resolve():
        raise ValueError("WIKI_INPUT_UNSAFE_DOCUMENT_ID")
    raw_path = legacy / _JONEX_WIKI_CONTENT_LIST
    raw = raw_path.read_bytes() if raw_path.is_file() else None
    snapshot = None
    assets = {}
    import asyncio
    import tempfile
    inputs_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".wiki-original-", dir=inputs_root) as folder:
        original_path = Path(folder) / "source"
        try:
            if key:
                await storage.get_to_path(key, str(original_path))
            elif file_path and Path(file_path).is_file():
                original_path = Path(file_path)
            else:
                raise FileNotFoundError(document_id)
        except Exception as exc:
            raise ResourceNotFoundError(message=translate("err.jonex_wiki.no_original_source",
                fallback="无法读取文档原始文件，请先执行「重新解析」"),
                details={"document_id": document_id}) from exc
        return await asyncio.to_thread(publish_input_bundle, root=inputs_root,
            tenant_id=tenant_id, kb_id=kb_id, document_id=document_id,
            generation=int(content_generation or 0), file_name=file_name, original=original_path,
            snapshot=snapshot, raw=raw, legacy_dir=legacy, asset_bytes=assets, source_context=source_context)


class JonexWikiService:
    """jonex_wiki 编译服务 — 编排 jonex_wiki 编译任务（镜像 KnowledgeCompilerService）。"""

    def __init__(self):
        self._client = get_jonex_wiki_client()
        # [jonex] DB 辅助方法复用 openkb 的 KnowledgeCompilerService（引擎无关，
        # 都写 llm_wiki_compile_* 列）。它只用 DB、不碰 openkb client。
        self._db = KnowledgeCompilerService()

    async def query_resources(self, tenant_id: str, kb_id: str, operation: str, options: dict) -> dict:
        return await self._client.query_resources(tenant_id, kb_id, operation, options)

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

    async def _compile_model(self, tenant_id: str, kb_id: str) -> str:
        """[jonex] P3b（方案 B）：本次任务该用的编译模型（空 = 不下发）。

        只有 `model_setting_service.resolve_dispatch_models` 一处能回答这个问题；
        取不到就返回空串，报文不带这个键，引擎回落自己的部署默认 —— 平台**不替
        引擎决定默认值**。

        取值时机是**发起任务时**：模型随任务报文进引擎的任务快照，所以之后改配置
        只影响新任务，在途任务保持原样。
        """
        from .model_setting_service import resolve_dispatch_models

        resolved = await resolve_dispatch_models(tenant_id, kb_id, kb_type="jonex_wiki")
        return resolved.get("compile_wiki", "")

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
        model = await self._compile_model(tenant_id, kb_id)
        try:
            return await self._client.compile_parsed_document(
                kb_name=kb_name,
                parsed_artifact=parsed_artifact,
                tenant_id=tenant_id,
                kb_id=kb_id,
                model=model,
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
                model=model,
            )

    async def compile_documents(self, kb_name: str, tenant_id: str, kb_id: str,
                                parsed_artifacts: list[dict]) -> dict:
        model = await self._compile_model(tenant_id, kb_id)
        try:
            return await self._client.compile_parsed_documents(
                kb_name, parsed_artifacts, tenant_id, kb_id, model=model)
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
            return await self._client.compile_parsed_documents(
                kb_name, parsed_artifacts, tenant_id, kb_id, model=model)

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
        query_context: dict | None = None,
        progress_session: str | None = None,
    ) -> dict:
        """搜索已编译的 Wiki 知识。"""
        options = {"thinking_mode": thinking_mode} if thinking_mode is not None else {}
        if query_context is not None:
            options['query_context'] = query_context
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
        """删除 jonex_wiki 中指定 document_id 的编译产物（引擎侧会顺带排一次重编）。"""
        return await self._client.remove_document(
            kb_name=kb_name, document_id=document_id,
            tenant_id=tenant_id, kb_id=kb_id,
            model=await self._compile_model(tenant_id, kb_id),
        )

    async def delete_kb(self, kb_name: str, tenant_id: str, kb_id: str) -> dict:
        """删除 jonex_wiki KB 目录。"""
        return await self._client.delete_kb(
            kb_name=kb_name, tenant_id=tenant_id, kb_id=kb_id,
        )

    async def query_graph(self, tenant_id: str, kb_id: str, operation: str, **options) -> dict:
        return await self._client.query_graph(tenant_id, kb_id, operation, **options)

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
        request = dict(request or {})
        if not request.get("model"):
            request["model"] = await self._compile_model(tenant_id, kb_id)
        return await self._client.draft_outline(
            kb_name=kb_name, tenant_id=tenant_id, kb_id=kb_id,
            request=request, user_id=user_id,
        )

    async def adjust_outline(self, kb_name: str, tenant_id: str, kb_id: str,
                             request: dict, user_id=None) -> dict:
        """按指令调整目录（action adjust_wiki_outline）。"""
        request = dict(request or {})
        if not request.get("model"):
            request["model"] = await self._compile_model(tenant_id, kb_id)
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
        artifact = await self.parsed_ingest_artifact(
            document_id=document_id, file_name=file_name, storage_key=storage_key,
            storage_backend=storage_backend, source_file_path=source_file_path,
            source_name=title, tenant_id=tenant_id, kb_id=kb_id,
        )
        return await self.compile_document(
            kb_name=kb_name, tenant_id=tenant_id, kb_id=kb_id,
            parsed_artifact=artifact,
        )

    async def parsed_ingest_artifact(
        self, *, document_id: str, file_name: str = "", storage_key: str = "",
        storage_backend: str = "", source_file_path: str = "", source_name: str = "",
        tenant_id: str = "", kb_id: str = "",
    ) -> dict:
        """[jonex] 构造 jonex_wiki ingest 形状的产物引用（重新编译与本库自愈共用）。"""
        rel_source = await stage_jonex_wiki_ingest_source(
            document_id, file_name=file_name, storage_key=storage_key,
            storage_backend=storage_backend, file_path=source_file_path,
            tenant_id=tenant_id, kb_id=kb_id)

        return {
            "source_path": rel_source,
            "source_name": source_name or file_name or document_id,
            "document_id": document_id,
        }

    async def refresh_stale_sources(
        self, tenant_id: str, kb_id: str, stale: list[dict] | None = None,
    ) -> int:
        """[jonex] 自愈：把「平台已解析好、但引擎侧产物过期」的文档重新 ingest 一遍。

        ── 为什么必须自愈，而不是拒绝 ──

        引擎产物的代次（``content_generation``）只在 **ingest** 时前进，不在「重新解析」
        时前进：平台重新解析只换共享卷上的 ``content_list.json``，引擎仍是旧代次。而
        ingest 恰恰发生在**编译**这条路里（``recompile_document`` → ``compile_document``）。
        所以「过期就拒编译」会把唯一的刷新路径也关掉 —— 用户按提示重新解析后仍然编不过，
        **死锁**（2026-10-21 现网复现）。

        这里先对过期文档逐份补一次 ingest（只搬平台已有的解析产物，不重新解析、不造数据），
        再让正常编译过去。搬不动（文档不在 ready / 产物缺失）的才交给调用方明确拒绝。

        返回补了几份。
        """
        from ..models import DocStatus
        from ..repository import KnowledgeDocumentRepository

        if stale is None:
            stale = await self.stale_parsed_sources(tenant_id, kb_id)
        if not stale:
            return 0

        artifacts: list[dict] = []
        blocked: list[dict] = []
        async with get_db_session() as session:
            repo = KnowledgeDocumentRepository(session)
            for item in stale:
                document_id = str(item.get("document_id") or "")
                row = await repo.get_by_id(document_id, tenant_id)
                if row is None or (row.status or "") != DocStatus.READY.value:
                    blocked.append(item)
                    continue
                try:
                    artifacts.append(await self.parsed_ingest_artifact(
                        document_id=document_id,
                        tenant_id=tenant_id, kb_id=kb_id,
                        file_name=row.file_name or "",
                        storage_key=row.storage_key or "",
                        storage_backend=(row.storage_backend or "local").strip().lower(),
                        source_file_path=row.file_path or "",
                        source_name=row.file_name or document_id,
                    ))
                except ResourceNotFoundError:
                    blocked.append(item)

        if blocked:
            raise_unhealable_stale(kb_id, blocked)
        if not artifacts:
            return 0
        await self.compile_documents(kb_name=kb_id, tenant_id=tenant_id, kb_id=kb_id,
                                     parsed_artifacts=artifacts)
        logger.info("[jonex] 解析产物自愈：已重新导入 %d 份过期文档 kb=%s", len(artifacts), kb_id)
        return len(artifacts)

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

    async def stale_parsed_sources(self, tenant_id: str, kb_id: str) -> list[dict]:
        """[jonex] 本库哪些文档的解析产物已过期（只读预检）。"""
        return await fetch_stale_parsed_sources(self._client, tenant_id, kb_id)

    async def ensure_parsed_sources_current(self, tenant_id: str, kb_id: str) -> int:
        """[jonex] 过期就**先自愈**（重新 ingest 已解析好的产物），搬不动的才拒。

        返回补 ingest 的份数。「搬不动」时抛 `ResourceConflictError`（details 里带每份的
        drift），由调用方决定怎么呈现：
        - 用户点「重新编译」/ 任务中心 compile 步（都走 `recompile_jonex_wiki`）：直接抛给
          用户，带份数与文件名；
        - 对账巡检（`ReconciliationService._ensure_jonex_parse_identity`）：**不抛给用户**，
          改成逐份回写 failed 并跳过本轮（见那里的注释）。
        """
        stale = await self.stale_parsed_sources(tenant_id, kb_id)
        if not stale:
            return 0
        healed = await self.refresh_stale_sources(tenant_id, kb_id, stale)
        remaining = await self.stale_parsed_sources(tenant_id, kb_id)
        if remaining:
            raise_unhealable_stale(kb_id, remaining)
        return healed

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

        # The consumer receipt, not a legacy shared filename, decides readiness.

        # ── [jonex] 整库解析身份预检 + 自愈 ──
        # 一次 jonex_wiki 编译产出的是**整库一个 bundle**：引擎 source_port 会拉本库
        # 全部已解析文档，任何一份产物的解析身份与当前解析规则不一致就在「取源材料」
        # 阶段硬失败（跳过它 = 静默丢页）。于是**单文档「重新编译」照样会被别的文档
        # 卡住**。
        #
        # 但「过期就拒」会死锁：产物代次只在 **ingest** 时前进（平台「重新解析」只换共享
        # 卷上的 content_list.json），而 ingest 就发生在这条编译路径里。所以这里先自愈
        # （把过期文档按平台已有解析产物重新导入），搬不动的才当场拒 —— 详见
        # `refresh_stale_sources` 的文档串。
        await self.ensure_parsed_sources_current(tenant_id, kb_id)

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
