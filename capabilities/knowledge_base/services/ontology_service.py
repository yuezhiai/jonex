"""Ontology extraction service for Knowledge Base."""

import logging
from typing import Optional

from jonex_core.capability.atomic.ontology_compile.client import (  # [jonex] P2 compile 步新后端
    get_ontology_compile_client,
)
from jonex_core.capability.atomic.rag.client import get_rag_client
from jonex_core.common.database import get_db_session
from jonex_core.common.exceptions import (
    InvalidParameterError,
    ResourceConflictError,
    ResourceNotFoundError,
)
from jonex_core.common.i18n import translate
from jonex_core.common.tenant import require_tenant

from ..models import OntologyStatus
from ..ontology_write_state import clear_ontology_write_state
from ..repository import KnowledgeDocumentRepository
from ..repository.task_center_repository import TaskCenterDocumentRepository
from . import compile_backend  # [jonex] P2：compile 步后端判据

logger = logging.getLogger(__name__)


class OntologyService:
    async def _reroute_compile_to_task_center(
        self, *, tenant_id: str, document_id: str, operator_id: Optional[str] = None,
    ) -> dict:
        """「重新编译 / 重跑编译」的任务中心那一半：**同一条任务、原地重跑 `compile_1`**。

        ⚠️ **它对三类库都是同一件事** —— 重跑的都是一条已有任务里的「知识编译」那一步，
        指认靠节点名（`compile_1` 三条链同名）。至于那一步到了运行方手里是「抽本体」还是
        「哪一类 Wiki 编译」，由**运行方 handler 按库类型**分流，与本方法无关。

        前提与失败语义都归 `rearm_for_rerun` / `retry_subtask_for_step`：

        · **没有接管行**（接入之前上传的老文档 / 任务被删过）→ `rearm_for_rerun` 抛
          `err.kb.task_center.no_task`（1003），**绝不偷偷新建一条任务**（用户定的口径：
          重来一次 ≠ 新起一个）；
        · **没被任务中心受理** → 交还接管行 + 抛 `err.kb.task_center.recompile_not_accepted`
          （留着那条在途行会把用户一直挡在「正在由任务中心处理」那道闸外面，而看门狗要等满
          `TC_NO_DISPATCH_SEC` 才放行）。

        ⚠️ **`strict=True` 不许走这里**：那是任务中心派回来的 `compile` 步自己在调
        `retry_extract`，它必须真去调引擎 —— 也走这里就是「任务中心重跑一步 → 这一步又叫
        任务中心重跑一步」的死循环。
        """
        # 局部 import：`task_runner_service` 反过来要在 compile handler 里调本类
        # （`retry_extract(strict=True)`），模块级互相 import 会成环。
        from .task_runner_service import (
            STEP_COMPILE,
            rearm_for_rerun,
            retry_accepted,
            retry_subtask_for_step,
        )

        # 第一段事务：校验「这份文档有没有可重跑的任务」并把接管行武装回 `managed`
        # （`current_step=compile`、清掉上次的执行号与下游任务号）。**与后面那次
        # HTTP 调用分开两次事务** —— 不让一段事务跨越一个网络调用。
        async with get_db_session() as session:
            plan = await rearm_for_rerun(
                session,
                tenant_id=tenant_id,
                document_id=document_id,
                step_code=STEP_COMPILE,
            )
            await session.commit()

        # 第二段：让任务中心把这条任务里的「知识编译」重跑一次。指认用的是节点名
        # （`compile_1`），由 `retry_subtask_for_step` 在那里翻 —— 本方法不需要知道它。
        retry_result = await retry_subtask_for_step(
            tenant_id=tenant_id,
            space_id=plan["space_id"],
            task_id=plan["task_id"],
            step_code=STEP_COMPILE,
            # 只用于任务中心的 `updated_by`（「最后是谁动的」）。可选：漏传不报错，
            # 只会静默记成空值。
            operator_id=operator_id,
        )
        if not retry_accepted(retry_result):
            # 没被受理 → **交还接管行 + 显式报错**。
            # 交还是必须的：留着那条在途行，用户会被「正在由任务中心处理」那道闸一直
            # 挡住，而看门狗要等满 `TC_NO_DISPATCH_SEC`（默认 30 分钟）才放行。
            # 报错也是必须的：用户点的是「重新编译」，不能显示成成功。
            async with get_db_session() as session:
                await TaskCenterDocumentRepository(session).mark_released(
                    document_id,
                    error_message=(
                        f"任务中心未受理重新编译：{retry_result.message}"
                    )[:2000],
                )
                await session.commit()
            err_msg = translate(
                "err.kb.task_center.recompile_not_accepted",
                params={"reason": retry_result.message or ""},
                fallback=f"任务中心未受理这次重新编译：{retry_result.message}",
            )
            logger.warning(
                "重新编译未被任务中心受理: doc=%s task=%s disposition=%s message=%s",
                document_id, plan["task_id"],
                retry_result.disposition.value, retry_result.message,
            )
            raise ResourceConflictError(
                message=err_msg,
                details={"document_id": document_id, "task_id": plan["task_id"]},
            )

        # 到此为止：**本方法不再自己调引擎**。任务中心会把 `compile_1` 派回来，
        # 那一刻 compile handler 用 `strict=True` 再进本方法，走下面那段真活。
        logger.info(
            "重新编译已交任务中心重跑: doc=%s task=%s node=%s",
            document_id, plan["task_id"], "compile_1",
        )
        async with get_db_session() as session:
            fresh = await KnowledgeDocumentRepository(session).get_by_id(
                document_id, tenant_id
            )
        # 文档在这两次会话之间被删掉（`get_by_id` 命中软删也算没了）就回空 dict ——
        # 接口本来就要一份「文档现在什么样」的快照，这时候没有可给的。
        return fresh.to_dict() if fresh is not None else {}

    async def retry_extract(
        self,
        tenant_id: str,
        document_id: str,
        knowledge_base_id: str,
        *,
        strict: bool = False,
        operator_id: Optional[str] = None,
    ) -> dict:
        """手动重抽本体（ontology-only）/ OpenKB 重新编译。

        [jonex] P0-B：统一走「领取 → 提交 → 拿到有效 task_id → EXTRACTING + retry_count++」协议：
        - 不再凭 queued 就置 READY（会导致对账不再扫、新结果永不落 Neo4j）；
        - 提交成功才写回新 `rag_task_id`、置 EXTRACTING、++retry，由对账 EXTRACTING 分支收尾；
        - 提交失败 / 未拿到 task_id：不改状态、不 ++retry，不遗留无 rag_task_id 的 EXTRACTING。

        [jonex] llm-wiki 管线：先校验文档归属，再按 doc 上的真实 KB 查 kb_type。
        openkb → KnowledgeCompilerService.recompile_openkb；jonex_wiki →
        JonexWikiService.recompile_jonex_wiki（产物形状不同，各走各的原子 claim + 异步投递）。

        ── `strict`（任务中心接入新增，**默认关**，既有调用方行为零变化）──

        默认 `False` 时，下面两处「提交失败」与「没拿到 task_id」都只打一条 warning、
        **原样返回文档**（看起来像成功）—— 那是给「用户点重试按钮」这条交互路径的：
        失败原因在服务端日志里、用户看到的是文档状态没变。

        任务中心那条路（compile 步）要的恰恰是相反的东西：它必须**当场**知道这一步有没有
        真的发出去 —— 拿到的回执是「我收下了」的意思，收下的却是一件没发出去的事，
        就会一路等到硬超时。传 `strict=True` 让那两处**抛异常**，由调用方转成显式拒绝。
        默认值不动是为了不改既有语义；只有 compile handler 传它。

        ── 三类库的「重新编译」：**改由任务中心重跑 `compile_1`** ──

        `strict=False`（= 用户点按钮那条路）时，**只要这份文档有任务中心接管行**，本方法
        就不再自己调引擎，而是让任务中心把这条任务里的「知识编译」那一步原地重跑一次
        （§4.2 ③ 的 `retry_subtask`）—— 见下面的分流块。用户定的两条：只针对已接入的库、
        而且**不是新起一个任务**。

        于是「重新编译」有两条路，判据**只有「有没有接管行」**：

        | 文档 | 走哪条 |
        | --- | --- |
        | 有接管行（接入之后上传的三类库文档） | 任务中心重跑 `compile_1`，本方法到此为止 |
        | 没有接管行（接入之前上传的老文档 / 任务被删过） | 明确报错（`err.kb.task_center.no_task`），**不偷偷新建任务** |

        **三类库共用这一条**（`lightrag` 与 `openkb` 自 2026-09-29 起，`jonex_wiki`
        自本轮起）：重跑的都是同一条任务里的「知识编译」那一步，到了运行方手里才按库类型
        分成「抽本体」与「哪一类 Wiki 编译」。

        `operator_id` 只用于任务中心的 `updated_by`（「最后是谁动的」）——它是可选参数：
        漏传不报错，只会静默记成空值（空值是合法状态：内部调用、后台循环本来就没有人）。
        """
        tenant_id = require_tenant(tenant_id)

        # ── [jonex] 先校验文档归属，再按 doc 上的真实 KB 查 pipeline_type ──
        # 不能直接信请求里的 knowledge_base_id：传错 KB id 会走错分支，或返回
        # 误导性的「无可用 compiled schema」。
        # 本块同时取出 lightrag 分支需要的 file_path / content_generation，
        # 下游复用，替代原来第二次 doc 查询。
        async with get_db_session() as session:
            repo = KnowledgeDocumentRepository(session)
            doc = await repo.get_by_id(document_id, tenant_id)
            if doc is None or doc.knowledge_base_id != knowledge_base_id:
                raise ResourceNotFoundError(
                    message=translate("err.doc.not_found", fallback="知识文档不存在"),
                )
            # ── [jonex] 任务中心接管中：**拦住用户、让他等**（§3.7）──
            # ⚠️ **只拦用户那条路**（`strict=False`）：`strict=True` 是 compile 步自己在
            # 调它（见 docstring 的 `strict` 段），而那一刻接管行正是 `managed` ——
            # 无条件拦会把链子的最后一步自己拦死。这个耦合是刻意的：`strict` 的语义
            # 本来就是「我是任务中心那条路」，两条判据同源，别给它加第二个开关。
            if not strict and await TaskCenterDocumentRepository(session).exists_active(
                document_id, tenant_id
            ):
                raise ResourceConflictError(
                    message=translate(
                        "err.doc.task_center_managed",
                        fallback="该文档正在由任务中心处理，请等当前步骤结束后再重新抽取",
                    )
                )
            real_kb_id = doc.knowledge_base_id
            file_path = doc.file_path or ""                                  # lightrag 用
            content_generation = int(getattr(doc, "content_generation", 0) or 0)  # lightrag 用（fencing）
            doc_status = doc.status                                          # openkb 互斥用
            title = doc.file_name or ""                                      # openkb 用

        # llm-wiki 的「重新编译」= 重跑 Wiki 编译，不抽本体、不写 Neo4j。
        # [jonex] 三分支显式（禁止否定式判 kb_type）：openkb 与 jonex_wiki 的
        # recompile 产物形状不同（openkb artifact vs ingest 形状），各走各的入口。
        _kb_type = await self._get_kb_type(tenant_id, real_kb_id)
        if _kb_type == "openkb":
            # ── [jonex] 接管中的 openkb 文档：「重新编译」也走任务中心重跑 `compile_1` ──
            # 与 lightrag 那条**同一条判据**（有没有接管行）、同一个错（没有就报
            # `err.kb.task_center.no_task`，绝不偷偷新建任务）—— 所以直接复用那段共用逻辑。
            # ⚠️ openkb **不要** lightrag 那道「先算 compiled schema」的前置校验（它不抽本体、
            # 没有 ontology schema），这也是它必须在这里分流、不能落到 lightrag 那段的原因。
            if not strict:
                return await self._reroute_compile_to_task_center(
                    tenant_id=tenant_id,
                    document_id=document_id,
                    operator_id=operator_id,
                )
            from .openkb_service import KnowledgeCompilerService
            return await KnowledgeCompilerService().recompile_openkb(
                tenant_id, document_id, real_kb_id,
                doc_status=doc_status, source_file_path=file_path, title=title,
                # 任务中心派回来的 compile 步（`strict=True`）：跳过「还在解析中就不让编译」
                # 那道忙校验（链上这一步只可能在解析成功之后才跑到），见该方法自己的 docstring。
                from_task_chain=True,
            )
        if _kb_type == "jonex_wiki":
            # ── [jonex] 接管中的 jonex_wiki 文档：「重新编译」也走任务中心重跑 `compile_1` ──
            # 与 openkb / lightrag 那条**同一条判据**（有没有接管行）、同一个错（没有就报
            # `err.kb.task_center.no_task`，绝不偷偷新建任务）—— 所以直接复用那段共用逻辑。
            # ⚠️ 与 openkb 同理，jonex_wiki 也**不要** lightrag 那道「先算 compiled schema」
            # 的前置校验（它不抽本体、没有 ontology schema），这也是它必须在这里分流、
            # 不能落到下面 lightrag 那段的原因。
            if not strict:
                return await self._reroute_compile_to_task_center(
                    tenant_id=tenant_id,
                    document_id=document_id,
                    operator_id=operator_id,
                )
            from .jonex_wiki_service import JonexWikiService
            return await JonexWikiService().recompile_jonex_wiki(
                tenant_id, document_id, real_kb_id,
                source_file_path=file_path, title=title,
                # 任务中心派回来的 compile 步（`strict=True`）：跳过「还在解析中就不让编译」
                # 那道忙校验（链上这一步只可能在解析成功之后才跑到），见该方法自己的 docstring。
                from_task_chain=True,
            )

        # ── 以下为 lightrag 原有逻辑 ──

        # [jonex] 手动重抽也应尊重重试上限，避免 retry_count 被无限顶高。
        # 已达上限的文档应走 force reparse（reparse 会重置计数），
        # 而非继续点「重试本体抽取」。
        from .reconciliation_service import MAX_ONTOLOGY_RETRIES

        if (doc.ontology_retry_count or 0) >= MAX_ONTOLOGY_RETRIES:
            raise InvalidParameterError(
                message=translate(
                    "err.ontology.retry_limit_reached",
                    fallback=f"本体重试次数已达上限 ({doc.ontology_retry_count}/{MAX_ONTOLOGY_RETRIES})，请对该文档执行 force reparse 以重置计数后重试",
                ),
                details={
                    "document_id": document_id,
                    "retry_count": doc.ontology_retry_count,
                    "max_retries": MAX_ONTOLOGY_RETRIES,
                },
            )

        # ontology-only 必须携带 compiled schema，否则 atomic-rag 无法归类
        schema = None
        schema_version = 0
        try:
            from .ontology_compiler import OntologyCompiler
            schema = await OntologyCompiler().get_compiled_schema(
                tenant_id, knowledge_base_id, auto_compile=True,
            )
            if schema:
                schema_version = int(schema.get("schema_version", 0) or 0)
        except Exception as e:
            logger.warning("Compiled schema check failed for doc %s: %s", document_id, e)

        if schema is None:
            raise InvalidParameterError(
                message=translate("err.ontology.no_compiled_schema", fallback="该知识库无可用 compiled schema，请先编译本体 schema 后再重试"),
                details={"knowledge_base_id": knowledge_base_id, "document_id": document_id},
            )

        # ── [jonex] 用户点「重新编译」：**改由任务中心重跑「知识编译」那一步**（§4.2 ③）──
        #
        # 走到这里已经确定是 Ontology 库（lightrag）：上面两个 `if` 把 openkb / jonex_wiki
        # 分流走了，`get_kb_type` 的默认值也是 lightrag。所以这里**不需要再判一次 kb_type**。
        #
        # 为什么放在 `schema is None` 之后：这一步是「重跑编译」，而编译的入参正是这份
        # schema。schema 都算不出来就没什么可重跑的，报同一个错（既有文案）比让任务中心
        # 跑到一半再失败更早、更准。
        #
        # ⚠️ `strict=True` 不走这里（那是任务中心派回来的 compile 步自己在调本方法）——
        # 那一趟必须真去调引擎，不然就是「任务中心重跑一步 → 这一步又叫任务中心重跑一步」
        # 的死循环。判据与 docstring 里 `strict` 那两段同源：`strict` 就是「我是任务中心
        # 那条路」的意思。
        if not strict:
            # 两类库共用这一段：重跑的都是一条已有任务里的「知识编译」那一步
            # （`compile_1` 在 lightrag 与 openkb 两条链上同名）。见该方法自己的 docstring。
            return await self._reroute_compile_to_task_center(
                tenant_id=tenant_id,
                document_id=document_id,
                operator_id=operator_id,
            )

        # ── [jonex] P2：compile 步后端判据（库级优先、env 兜底、缺省 ontology_compile；
        #    P4a 2026-10-17 起翻默认，回滚 = 设 ONTOLOGY_COMPILE_BACKEND=rag）──
        # **只有这一处读 env**；选中的值随下面那次 commit 写进文档行（粘性），查询侧
        # （对账 EXTRACTING 轮询）只读粘性 —— 见 `compile_backend` 的模块 docstring。
        # 在**提交时**当场求值：库设置 / env 改了只影响下一次提交，在途文档按旧值收尾。
        backend = compile_backend.resolve_compile_backend(
            kb_backend=await self._get_kb_compile_backend(tenant_id, real_kb_id),
        )

        # 提交 ontology-only 任务（不在此处 ++retry / 置 EXTRACTING）
        try:
            if backend == "ontology_compile":
                # 新服务：`chunks` 不传 = 容器在 worker 执行时按 `document_id`
                # 从 LightRAG 直读（设计 §4.5 ①-②「跳过灌库步」）。
                # 拿回的 `compile_id` 就落在下面那个 `task_id` 槽位
                # （与 rag 路径**同一列** `rag_task_id` —— 查询侧靠粘性值决定去问谁）。
                #
                # `content_generation` 与下面 rag 支**同源同值**（提交那一刻的文档代次）：
                # 对账收尾的代次栅栏读 `status_info["content_generation"]`，而新服务的任务
                # 状态里**不带**这个键 —— 不在这里传下去，容器就恒按 0 记，凡是被重新解析过
                # 的文档（gen≥1）必然被判「旧代次」、停在 EXTRACTING 永不 READY（不报错）。
                result = await get_ontology_compile_client().compile(
                    document_id=document_id,
                    knowledge_base_id=knowledge_base_id,
                    tenant_id=tenant_id,
                    mode="extract_only",
                    schema=schema,
                    content_generation=content_generation,
                )
                task_id = (result or {}).get("compile_id", "")
            else:
                result = await get_rag_client().retry_ontology_extract(
                    document_id=document_id,
                    knowledge_base_id=knowledge_base_id,
                    tenant_id=tenant_id,
                    file_path=file_path,
                    ontology_schema=schema,
                    schema_version=schema_version,
                    content_generation=content_generation,
                    # [jonex] 手动重抽 = 明确要求重跑，必须传 force_retry。
                    # 否则 atomic-rag 按幂等键 `ontology:{tenant}:{kb}:{doc}:{schema_version}`
                    # 命中同版本的 completed 任务时直接 return（handler 里
                    # `completed and not force_retry` 分支），抽取根本不执行——
                    # 接口却返回成功、retry_count 照常 +1，报的还是上一次的错误，
                    # 表现为「重试按钮点了没反应」。
                    force_retry=True,
                )
                task_id = (result or {}).get("task_id", "")
        except Exception as e:
            logger.warning("Ontology retry submit failed for doc %s: %s", document_id, e)
            if strict:
                # 任务中心那条路：把失败原样抛给调用方（它要据此显式拒这一步），
                # 不要在这里吞成「看起来成功」。
                raise
            async with get_db_session() as session:
                repo = KnowledgeDocumentRepository(session)
                doc = await repo.get_required(document_id, tenant_id)
                return doc.to_dict()

        if not task_id:
            logger.warning("Ontology retry did not return task_id for doc %s", document_id)
            if strict:
                raise InvalidParameterError(
                    message=translate(
                        # 复用既有键：同一条事实（投递没拿到 `task_id`）—— 不新造一个
                        # 语义重复的键，省掉「两个 locale 都得记得同步」那一步。
                        "err.ontology.submit_no_task_id",
                        fallback="本体抽取任务未能提交（atomic-rag 未返回 task_id）",
                    ),
                    details={"document_id": document_id, "knowledge_base_id": knowledge_base_id},
                )
            async with get_db_session() as session:
                repo = KnowledgeDocumentRepository(session)
                doc = await repo.get_required(document_id, tenant_id)
                return doc.to_dict()

        # 提交成功：写回新 rag_task_id + EXTRACTING + ++retry，由对账收尾后才 READY
        async with get_db_session() as session:
            repo = KnowledgeDocumentRepository(session)
            doc = await repo.get_required(document_id, tenant_id)
            await repo.set_ontology_status(doc, OntologyStatus.EXTRACTING, increment_retry=True)
            await repo.set_status(doc, doc.status, rag_task_id=task_id)
            # [jonex] 手动重抽 = 换任务：写图状态一并清零。这是
            # ontology_auto_retry_disabled 的两条人工解除入口之一（另一条是
            # force reparse），不清的话被打标的文档重抽后第一次写图失败即再达上限。
            _fm = clear_ontology_write_state(doc.extra_metadata)
            _fm["ontology_retry_task_id"] = task_id
            # ── [jonex] P2 粘性：**本次实际派给谁，就写谁** ──
            # ⚠ 必须与 `rag_task_id` 落在**同一次 commit** 里（**先后无关**，但**不许拆成两次提交**）。
            # 分开提交会留下「有 task_id、却看不出它是哪个服务的」的中间态，而查询侧
            # （对账 EXTRACTING 轮询）正要靠这个键决定去问谁 —— 那种行的 task_id 在两个
            # 后端都不存在，只能等超时判死（先例 `document_service.py:893-896`）。
            # 取值恒为 `rag` / `ontology_compile`（已由 `resolve_compile_backend` 校验）。
            # 老文档没有这个键 ⇒ 查询侧按旧链路口径回落 `rag`，行为与从前逐字一致。
            _fm["ontology_compile_backend"] = backend
            doc.extra_metadata = _fm
            await session.commit()
            return doc.to_dict()

    async def reextract_kb_documents(
        self,
        tenant_id: str,
        knowledge_base_id: str,
        *,
        document_ids: list[str] | None = None,
        only_outdated: bool = False,
        only_ready: bool = True,
    ) -> dict:
        """KB 级「按新 schema 重抽本体」（C→B 联动，走对账被动）。

        [jonex] 阶段2：只做状态重置（置 PENDING + 重置计数 + 写目标 schema 版本）；
        实际重抽交给 30s 对账 reconcile_ontology 逐个 ontology-only 跑（不重解析文件）。
        返回 {matched, reset, skipped, schema_version}。
        """
        tenant_id = require_tenant(tenant_id)

        schema = None
        schema_version = 0
        try:
            from .ontology_compiler import OntologyCompiler
            schema = await OntologyCompiler().get_compiled_schema(
                tenant_id, knowledge_base_id, auto_compile=True,
            )
            if schema:
                schema_version = int(schema.get("schema_version", 0) or 0)
        except Exception as e:
            logger.warning("Compiled schema check failed for KB %s: %s", knowledge_base_id, e)

        if schema is None:
            raise InvalidParameterError(
                message=translate("err.ontology.no_compiled_schema", fallback="该知识库无可用 compiled schema，请先编译本体 schema 后再重试"),
                details={"knowledge_base_id": knowledge_base_id},
            )

        async with get_db_session() as session:
            repo = KnowledgeDocumentRepository(session)
            stats = await repo.reset_ontology_for_kb(
                tenant_id, knowledge_base_id,
                document_ids=document_ids,
                only_ready=only_ready,
                only_outdated=only_outdated,
                target_schema_version=schema_version,
            )
            await session.commit()

        return {**stats, "schema_version": schema_version}

    # ── [jonex] kb_type 查询 ──

    async def _get_kb_type(self, tenant_id: str, kb_id: str) -> str:
        """[jonex] 查询 KB 的 kb_type（转调公共 helper）。"""
        from .kb_type_service import get_kb_type
        return await get_kb_type(tenant_id, kb_id)

    async def _get_kb_compile_backend(self, tenant_id: str, kb_id: str) -> str | None:
        """[jonex] P2：查询 KB 行的 **compile 步**库级派单值（转调 kb_type_service）。

        形状照 `DocumentService._get_kb_ingest_backend`（`document_service.py:2973`）：
        `None` 是**合法值**（= 跟随服务端开关 `ONTOLOGY_COMPILE_BACKEND`），不是「查不到」
        —— 两者对 `compile_backend.resolve_compile_backend` 是同一件事。

        ⚠ 与解析链的 `ingest_backend` 是**两把钥匙、两个语义**：那个读
        `knowledge_info.ingest_backend`，本方法读 `knowledge_info.ontology_compile_backend`。
        独立成方法（而不是内联）还有一个实务理由：单测按既有形状 patch 它即可，
        不必让桩会话去支持 `execute`。
        """
        from .kb_type_service import get_kb_ontology_compile_backend
        return await get_kb_ontology_compile_backend(tenant_id, kb_id)


__all__ = ["OntologyService"]
