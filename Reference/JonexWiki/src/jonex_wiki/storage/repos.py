from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from jonex_wiki.kernel.task import TaskKind, TaskStatus
from jonex_wiki.storage.models import DocumentRow, OutlineRow, QueryTaskRow, TaskRow, _now


class DocumentRepo:
    async def add(self, session: AsyncSession, *, tenant_id, kb_id, source_name,
                  source_sha256, route, parser_id, parser_version,
                  document_id: str | None = None) -> DocumentRow:
        # document_id 透传平台 id(方案:覆盖引擎默认 doc-* 前缀);缺省由模型 default 生成
        row = DocumentRow(tenant_id=tenant_id, kb_id=kb_id, source_name=source_name,
                          source_sha256=source_sha256, route=route, parser_id=parser_id,
                          parser_version=parser_version)
        if document_id is not None:
            row.document_id = document_id
        session.add(row)
        await session.flush()
        return row

    async def get(self, session: AsyncSession, tenant_id, kb_id, document_id) -> DocumentRow | None:
        return (await session.execute(
            select(DocumentRow).where(
                DocumentRow.tenant_id == tenant_id,
                DocumentRow.kb_id == kb_id,
                DocumentRow.document_id == document_id,
            ))).scalar_one_or_none()

    async def list_parsed_for_compile(self, session: AsyncSession, tenant_id, kb_id) -> list[DocumentRow]:
        return list((await session.execute(
            select(DocumentRow).where(
                DocumentRow.tenant_id == tenant_id,
                DocumentRow.kb_id == kb_id,
                DocumentRow.status == "parsed",
                DocumentRow.deleted_at.is_(None),
            ))).scalars())

    async def list_deleted(self, session: AsyncSession, tenant_id, kb_id) -> list[DocumentRow]:
        """已逻辑删除的文档(compile 级联清理输入)。"""
        return list((await session.execute(
            select(DocumentRow).where(
                DocumentRow.tenant_id == tenant_id,
                DocumentRow.kb_id == kb_id,
                DocumentRow.deleted_at.isnot(None),
            ))).scalars())

    async def count_active(self, session: AsyncSession, tenant_id, kb_id) -> int:
        return (await session.execute(
            select(func.count()).select_from(DocumentRow).where(
                DocumentRow.tenant_id == tenant_id,
                DocumentRow.kb_id == kb_id,
                DocumentRow.deleted_at.is_(None),
            ))).scalar_one()

    async def soft_delete(self, session: AsyncSession, row: DocumentRow) -> None:
        row.deleted_at = _now()
        await session.flush()


class TaskRepo:
    async def create(self, session: AsyncSession, *, tenant_id, kb_id, kind: TaskKind,
                     document_id: str | None = None, compile_model: str = "") -> TaskRow:
        """建任务。``compile_model`` = **任务建立时**该 KB 的编译模型快照。

        [jonex] P3b（方案 B）：模型不进引擎侧的 KB 表，只进任务。任务跑的时候
        读自己这一行，所以「改配置影响之后的编译、不影响在途任务」是结构保证的，
        不需要比对或同步。空串 = 这次调用没带模型 ⇒ 引擎走部署默认（env）。
        """
        summary = {"compile_model": compile_model} if compile_model else None
        row = TaskRow(tenant_id=tenant_id, kb_id=kb_id, kind=kind.value,
                      document_id=document_id, summary=summary)
        session.add(row)
        await session.flush()
        return row

    @staticmethod
    def restamp_compile_model(task: TaskRow | None, compile_model: str) -> None:
        """给复用的**已排队**任务换上本次请求的模型（未开跑 ⇒ 换模型该生效）。

        平台保存配置后会排一次重编；如果此时已有一条 queued 编译任务（比如刚上传
        文档触发的），直接沿用就会用旧模型编完这次 —— 用户看到的是「改了没生效」。
        这里只改还没开跑的任务；compiling 状态一律不动（在途任务保持自己的快照）。
        """
        if task is None or not compile_model:
            return
        if task.status != TaskStatus.queued.value:
            return
        summary = dict(task.summary or {})
        if summary.get("compile_model") == compile_model:
            return
        summary["compile_model"] = compile_model
        task.summary = summary

    async def get(self, session: AsyncSession, tenant_id, task_id) -> TaskRow | None:
        return (await session.execute(
            select(TaskRow).where(TaskRow.tenant_id == tenant_id, TaskRow.task_id == task_id)
        )).scalar_one_or_none()

    async def queued_compile_for_kb(self, session: AsyncSession, tenant_id, kb_id) -> TaskRow | None:
        return (await session.execute(
            select(TaskRow).where(
                TaskRow.tenant_id == tenant_id,
                TaskRow.kb_id == kb_id,
                TaskRow.kind == TaskKind.compile.value,
                TaskRow.status == TaskStatus.queued.value,
            ).order_by(TaskRow.created_at))).scalars().first()

    async def list_for_kb(self, session: AsyncSession, tenant_id, kb_id, kind=None,
                          limit=50, offset=0) -> list[TaskRow]:
        stmt = select(TaskRow).where(TaskRow.tenant_id == tenant_id, TaskRow.kb_id == kb_id)
        if kind:
            stmt = stmt.where(TaskRow.kind == kind)
        return list((await session.execute(
            stmt.order_by(TaskRow.created_at.desc()).limit(limit).offset(offset))).scalars())


class QueryTaskRepo:
    async def create(self, session: AsyncSession, *, tenant_id, kb_id, question,
                     bundle_sha: str | None = None) -> QueryTaskRow:
        row = QueryTaskRow(tenant_id=tenant_id, kb_id=kb_id, question=question,
                           bundle_sha=bundle_sha)
        session.add(row)
        await session.flush()
        return row

    async def get(self, session: AsyncSession, tenant_id, query_id) -> QueryTaskRow | None:
        return (await session.execute(
            select(QueryTaskRow).where(QueryTaskRow.tenant_id == tenant_id,
                                       QueryTaskRow.query_id == query_id)
        )).scalar_one_or_none()

    async def list_expired(self, session: AsyncSession, *, before, limit=500) -> list[QueryTaskRow]:
        """done/failed/cancelled 且 finished_at 早于 before 的行(TTL 清理)。"""
        from jonex_wiki.storage.models import QueryTaskRow as Q
        stmt = (select(Q)
                .where(Q.status.in_([TaskStatus.done.value, TaskStatus.failed.value,
                                     TaskStatus.cancelled.value]))
                .where(Q.finished_at.is_not(None), Q.finished_at < before)
                .limit(limit))
        return list((await session.execute(stmt)).scalars())

    async def delete_expired(self, session: AsyncSession, *, before, limit=500) -> int:
        """删除过期 query_tasks 行,返回删除数(启动 GC 用)。"""
        rows = await self.list_expired(session, before=before, limit=limit)
        for row in rows:
            await session.delete(row)
        await session.flush()
        return len(rows)


class OutlineRepo:
    """Wiki 阅读目录留档 repository(目录功能下沉)。

    语义平移自平台 `WikiOutlineRepository`,按引擎约定改造:接收 `session`(调用方
    开事务)、不 commit 只 flush(commit 由 service 层决定)、`kb_id` 命名。
    """

    async def get_active(self, session: AsyncSession, tenant_id, kb_id) -> OutlineRow | None:
        """读当前 active 目录行;无则 None(首次由编译后自动触发或人工 draft 生成)。"""
        return (await session.execute(
            select(OutlineRow).where(
                OutlineRow.tenant_id == tenant_id,
                OutlineRow.kb_id == kb_id,
                OutlineRow.status == "active",
            ))).scalar_one_or_none()

    async def save_new_active(
        self, session: AsyncSession, *, tenant_id, kb_id,
        source: str, model: str | None, instruction: str | None,
        page_count: int, catalog_fingerprint: str,
        outline: dict, catalog_snapshot: list,
        edited_by: str | None = None,
    ) -> OutlineRow:
        """留档新 active 目录:旧 active 转 archived → INSERT 新行(同事务,只 flush)。

        版本号由库内 MAX(outline_version)+1 决定,避免并发下版本号错乱;并发第二个
        事务会被 partial unique index 挡住 → flush 抛 IntegrityError,由 service 层
        捕获转 ConflictError(引擎分层:repo 不做业务异常转换)。
        """
        await session.execute(
            update(OutlineRow)
            .where(OutlineRow.tenant_id == tenant_id,
                   OutlineRow.kb_id == kb_id,
                   OutlineRow.status == "active")
            .values(status="archived", updated_at=_now()))
        next_version = (await session.execute(
            select(func.coalesce(func.max(OutlineRow.outline_version), 0) + 1)
            .where(OutlineRow.tenant_id == tenant_id,
                   OutlineRow.kb_id == kb_id)
        )).scalar_one()
        row = OutlineRow(
            tenant_id=tenant_id, kb_id=kb_id,
            outline_version=next_version,
            status="active", source=source, model=model, instruction=instruction,
            page_count=page_count, catalog_fingerprint=catalog_fingerprint,
            outline=outline, catalog_snapshot=catalog_snapshot,
            edited_by=edited_by, generated_at=_now())
        session.add(row)
        await session.flush()
        return row
