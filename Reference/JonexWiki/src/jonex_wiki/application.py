"""进程内应用门面：引擎运行时接线 + 领域动作。

双用途：
- ``main.lifespan`` 用 ``start()/stop()`` 接线（独立引擎 FastAPI 服务 + 引擎自身测试）。
- ``JonexWikiCapability``（平台原子能力）进程内 ``import jonex_wiki`` 直接调用这些领域动作，
  不再经过 FastAPI 路由 / HTTP 转调（B1 单容器单进程方案，对齐 OpenKB 进程内 import 模式）。

所有动作方法都以 ``DomainError`` 子类表达业务失败（与 router 层一致），
由调用方（router 的异常处理器 / 能力的错误映射）翻译成各自信封。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import shutil
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from sqlalchemy import delete, select, text

from jonex_wiki.domain.engine.compile.source_prep.blocks import InputRoute
from jonex_wiki.domain.engine.compile.source_prep.chunking import PackingOptions
from jonex_wiki.domain.engine.compile.source_prep.classify import MINERU_EXTENSIONS
from jonex_wiki.domain.engine.compile.source_prep.direct_ingest import blocks_from_direct_text
from jonex_wiki.domain.engine.compile.source_prep.inspect import (
    InspectionPolicy, InspectionReport)
from jonex_wiki.domain.engine.compile.source_prep.source_archive import write_source_archive
from jonex_wiki.domain.engine.compile.source_prep.raw_manifest import (
    MANIFEST_FILE, SOURCE_TEXT_FILE, build_manifest, write_manifest)
from jonex_wiki.domain.engine.compile.source_prep.reading_materials import prepare_reading_materials
from jonex_wiki.domain.engine.compile.source_prep.spreadsheet_view import parse_spreadsheet
from jonex_wiki.domain.engine.compile.source_prep.table_artifact import persist_table_observations
from jonex_wiki.domain.parsing.mineru.common import normalize_inline_content_list
from jonex_wiki.domain.prompts import (PUBLIC_PROMPT_KEYS, PromptRepo, resolve_prompt,
                                       template_sha, validate_template)
from jonex_wiki.kernel.errors import ConflictError, DomainError, NotFoundError, ValidationError
from jonex_wiki.kernel.task import TaskKind, TaskStatus
from jonex_wiki.storage.layout import Layout
from jonex_wiki.storage.models import (DocumentRow, PromptTemplateRow, QueryTaskRow, TaskRow, _now)
from jonex_wiki.storage.repos import DocumentRepo, QueryTaskRepo, TaskRepo

log = logging.getLogger(__name__)

_LOCK_KEY = 730203

_WIKI_PREFIX = "wiki/"

_IMAGES_DIR = "images"  # 共享卷解析产物的图片子目录(content.md 同级)


def _contract_warnings(artifact: str, source_name: str, route: InputRoute) -> list[str]:
    """产物形状契约：源文件**期望**结构化产物，却拿到的文本产物时发声。

    为什么需要这条断言：``kb_type`` 透传丢失（RAG 侧旧进程）不会报错——解析产物从
    ``content_list.json`` 静默退回 ``content.md``，表格资产 / caption / footnote 全部不可用，
    而编译照常成功、wiki 照常发布（真实事故）。判据只用一个既有的权威集合
    ``MINERU_EXTENSIONS``：该类源文件本来就该由 MinerU 产出结构化产物。

    **断言，不是日志**：形状符合预期就返回空——所以告警列空 = 一切正常，有值 = 一定有问题。
    ``.md``/``.txt`` 这类源本来就是文本产物，静默。
    """
    if Path(source_name).suffix.lower() not in MINERU_EXTENSIONS:
        return []
    if route in (InputRoute.MINERU, InputRoute.SPREADSHEET_NATIVE):
        return []
    return [
        f"source_contract:artifact={artifact} route={route.value} shape=text —— "
        f"源文件 {source_name} 期望结构化产物(MinerU content_list.json)；"
        "文本产物下 table_observation/caption/footnote 不可用，应重跑解析并检查 "
        "kb_type 透传（RAG 侧进程是否加载了 jonex_wiki 分支）",
    ]


class WikiApplication:
    """引擎运行时 + 领域动作门面（进程内）。"""

    def __init__(self, settings, session_factory=None, executor=None):
        self.settings = settings
        self.session_factory = session_factory
        self.executor = executor
        self.layout: Layout | None = None
        self.gw = None
        self.mineru = None
        self._lock_engine = None
        self._lock_conn = None
        self._engine = None
        self._outline = None

    # ── 生命周期（自 main.lifespan 提取）──

    async def start(self) -> None:
        settings = self.settings
        if settings is None:
            return
        if settings.database_url.startswith("postgresql"):
            # 单实例强制(spec §8):专用连接全程持有 session 级 advisory lock。
            from sqlalchemy.ext.asyncio import create_async_engine
            from sqlalchemy.pool import NullPool
            self._lock_engine = create_async_engine(settings.database_url, poolclass=NullPool)
            conn = await self._lock_engine.connect()
            # [jonex] AUTOCOMMIT：session 级 advisory lock 不依赖事务即可持续持有，
            # 避免锁连接长期 idle in transaction 阻塞同库 CREATE INDEX CONCURRENTLY
            # （lightrag 全新库首启卡死），也不受 idle_in_transaction_session_timeout 误杀。
            conn = await conn.execution_options(isolation_level="AUTOCOMMIT")
            got = (await conn.execute(text(f"SELECT pg_try_advisory_lock({_LOCK_KEY})"))).scalar()
            if not got:
                await conn.close()
                await self._lock_engine.dispose()
                raise SystemExit("另一个实例已持有执行锁,拒绝启动(spec §8 单实例强制)")
            self._lock_conn = conn
        if self.session_factory is None:
            from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
            from jonex_wiki.storage.db import db_connect_args
            # [jonex] pool_pre_ping + pool_recycle：asyncpg 连接可能被 PG/网络层在空闲期
            # 回收，池子无探活会在下次取用时才报 "connection is closed"（outline 接口实测
            # 偶发）。取连接前先 ping，失效自动丢弃重建；30min 主动回收对齐平台侧
            # jonex_core.common.database 的 DB_POOL_RECYCLE 语义。
            self._engine = create_async_engine(
                settings.database_url,
                connect_args=db_connect_args(settings),
                pool_pre_ping=True,
                pool_recycle=1800,
            )
            self.session_factory = async_sessionmaker(self._engine, expire_on_commit=False)
        self.layout = Layout(settings.data_root)
        from jonex_wiki.infra.gateway import GwClient
        from jonex_wiki.infra.logging import setup_logging
        from jonex_wiki.tasks.executor import TaskExecutor
        setup_logging()
        from jonex_wiki.infra.metering import metered
        self.gw = metered(self.gateway_factory(settings) if hasattr(self, "gateway_factory") else GwClient(settings))
        mineru = None
        if settings.mineru_api_token:
            from jonex_wiki.domain.parsing.mineru import MineruCloudBackend
            mineru = MineruCloudBackend(
                base_url=settings.mineru_api_base_url,
                token=settings.mineru_api_token,
                poll_interval=settings.mineru_poll_interval,
                poll_timeout=int(settings.mineru_poll_timeout),
                model_version=settings.mineru_model_version,
            )
        self.mineru = mineru
        self.executor = self.executor or TaskExecutor(self.session_factory, settings)
        from jonex_wiki.domain.parsing.parse_task import create_parse_runner
        from jonex_wiki.domain.compile_task import create_compile_runner
        from jonex_wiki.domain.query.query_task import create_query_runner
        from jonex_wiki.domain.outline.task import build_outline_runner
        self.executor.parse_runner = self.executor.parse_runner or create_parse_runner(
            self.session_factory, settings, self.layout, mineru, executor=self.executor)
        self.executor.compile_runner = self.executor.compile_runner or create_compile_runner(
            self.session_factory, settings, self.layout, self.gw, executor=self.executor)
        self.executor.query_runner = self.executor.query_runner or create_query_runner(
            self.session_factory, settings, self.layout, self.gw, executor=self.executor)
        self.executor.outline_runner = self.executor.outline_runner or build_outline_runner(
            self.session_factory, settings, self.layout, self.gw)
        # 启动恢复(spec §3.3)
        async with self.session_factory() as s:
            await self.executor.recover(s)
            await self.executor.recover_queries(s)
            await self.executor.purge_expired_queries(s, ttl_seconds=settings.query_task_ttl_seconds)
            await s.commit()
        # 孤儿 staging 清理 + bundle GC
        for orphan in self.layout.root.rglob("compile/staging/*"):
            if orphan.is_dir() and not (orphan.parent.parent / "PAUSED").is_file():
                shutil.rmtree(orphan, ignore_errors=True)
        for kb_compile in self.layout.root.rglob("compile"):
            if not kb_compile.is_dir():
                continue
            kb_id, tenant_id = kb_compile.parent.name, kb_compile.parent.parent.name
            async with self.session_factory() as s:
                pins = set((await s.execute(select(QueryTaskRow.bundle_sha).where(
                    QueryTaskRow.tenant_id == tenant_id, QueryTaskRow.kb_id == kb_id,
                    QueryTaskRow.status.in_([TaskStatus.queued.value, TaskStatus.running.value]),
                    QueryTaskRow.bundle_sha.isnot(None),
                ))).scalars())
            self.layout.prune_versions(tenant_id, kb_id, settings.keep_bundles, pins)
        # FTS5 启动自检(spec §13)
        import sqlite3 as _sq
        try:
            probe = _sq.connect(":memory:")
            probe.execute("CREATE VIRTUAL TABLE t USING fts5(x)")
            probe.close()
        except _sq.OperationalError as e:  # pragma: no cover
            raise SystemExit(f"SQLite 未编译 FTS5,拒绝启动: {e}")
        loop = asyncio.get_running_loop()
        loop.set_default_executor(ThreadPoolExecutor(max_workers=settings.blocking_pool_size))

    async def stop(self) -> None:
        if hasattr(self, "_scenarios"):
            await self._scenarios.stop()
        if self.gw is not None:
            await self.gw.aclose()
        if self._lock_conn is not None:
            await self._lock_conn.close()
        if self._lock_engine is not None:
            await self._lock_engine.dispose()
        if self._engine is not None:
            await self._engine.dispose()

    # ── 领域动作（自 routers 提取）──

    async def ingest_parsed_document(self, tenant_id: str, kb_id: str,
                                     source_path: str, source_name: str,
                                     document_id: str | None = None,
                                     *, compile_model: str = "") -> dict:
        async with self.layout.version_lock(tenant_id, kb_id):
            return await self._ingest_parsed_document_locked(
                tenant_id, kb_id, source_path, source_name, document_id,
                compile_model=compile_model)

    async def ingest_parsed_documents(self, tenant_id: str, kb_id: str, documents: list[dict],
                                      *, compile_model: str = "") -> dict:
        """Stage a drained platform upload window before submitting one KB compile.

        The version lock also excludes compile claiming. Parsed generations
        remain durable if a later item fails; retries can re-ingest them.
        """
        if not documents or len(documents) > self.settings.compile_max_documents:
            raise ValidationError("入库批次为空或超过文档上限")
        if len({d.get("document_id") for d in documents}) != len(documents):
            raise ValidationError("入库批次 document_id 必须非空且唯一")
        for d in documents:
            if not d.get("document_id") or not d.get("source_path") or not d.get("source_name"):
                raise ValidationError("入库批次缺少文档标识或来源")
            rel = Path(d["source_path"])
            if rel.is_absolute() or ".." in rel.parts or not (Path(self.settings.input_root) / rel).is_file():
                raise ValidationError(f"入库批次来源路径不可用: {rel}")
        async with self.layout.version_lock(tenant_id, kb_id):
            results = []
            for d in documents:
                results.append(await self._ingest_parsed_document_locked(
                    tenant_id, kb_id, d["source_path"], d["source_name"], d["document_id"],
                    submit=False, compile_model=compile_model))
            async with self.session_factory() as session:
                task = await TaskRepo().get(session, tenant_id, results[-1]["task_id"])
            self.executor.submit(task)
        return {"task_id": task.task_id, "status": task.status, "documents": results}

    async def _ingest_parsed_document_locked(self, tenant_id: str, kb_id: str,
                                             source_path: str, source_name: str,
                                             document_id: str | None = None, *, submit: bool = True,
                                             compile_model: str = "") -> dict:
        """平台已解析产物入库:写 gen-1/source.txt + source_artifacts.jsonl 后触发编译,跳过 parse task。

        与 parse 的 direct 分支对齐:现场跑 blocks_from_direct_text + prepare_reading_materials
        再投影落 source_artifacts.jsonl(compile 的 source_port 只读 JSONL,不再从 raw 现场重建)。
        """
        settings = self.settings
        # 路径安全:仅相对路径、禁止穿越
        rel = Path(source_path)
        if rel.is_absolute() or ".." in rel.parts:
            raise ValidationError(f"source_path {source_path!r} 必须是共享卷内的相对路径")
        full = (Path(settings.input_root) / rel).resolve()
        if not full.is_relative_to(Path(settings.input_root).resolve()):
            raise ValidationError("source_path escapes the input root")
        if not full.is_file():
            raise ValidationError(f"共享卷解析产物不存在: {source_path}")
        data = await asyncio.to_thread(full.read_bytes)
        from jonex_wiki.domain.parsing.consumer_input import read_consumer_receipt
        try:
            receipt = await asyncio.to_thread(read_consumer_receipt, full, tenant_id=tenant_id, kb_id=kb_id,
                                              document_id=document_id or "")
        except (ValueError, KeyError) as exc:
            raise ValidationError(str(exc)) from exc
        if receipt:
            expected_input = receipt["original_sha256"] if receipt["input_path"] == receipt["original_path"] else receipt["parsed_sha256"]
            if hashlib.sha256(data).hexdigest() != expected_input:
                raise ValidationError("WIKI_INPUT_CHANGED_DURING_READ")
        # [jonex] 原生表格(.xlsx/.xlsm)走 core 的确定性 OOXML 解析;MinerU 结构化产物
        # (content_list.json)走引擎的 MinerU 归一化路由(与独立部署 parse task 的 MINERU
        # 分支一致,不再把 markdown 重组版当纯文本);其余保持平台已解析文本。
        #
        # 判据只看**文件名**(与共享卷生产端写出的名字一一对应),刻意不看内容形状:
        # 名字对得上却不是合法 content_list 时,MINERU 分支会抛 ValidationError(响亮失败),
        # 而"形状对不上就退回 TEXT_DIRECT"会让它静默走成 .json 记录切分(JSON 语法汤)。
        suffix = rel.suffix.lower()
        if suffix in {".xlsx", ".xlsm"}:
            route = InputRoute.SPREADSHEET_NATIVE
            parser_id = "spreadsheet.native.ooxml"
            parser_version = "1"
        elif rel.name == "content_list.json" or rel.name.endswith("_content_list.json"):
            route = InputRoute.MINERU
            parser_id = "mineru.cloud.v1"
            parser_version = "2.x+structure-pack-2"
        else:
            route = InputRoute.TEXT_DIRECT
            parser_id = "text_direct_v1"
            parser_version = "2.0.0"

        async with self.session_factory() as s:
            repo = DocumentRepo()
            dup = (await s.execute(select(DocumentRow).where(
                DocumentRow.tenant_id == tenant_id, DocumentRow.kb_id == kb_id,
                DocumentRow.source_name == source_name, DocumentRow.deleted_at.is_(None)))).scalars().first()
            if dup is not None and dup.document_id != document_id:
                raise ConflictError(f"同知识库已存在同名文档 {source_name}(document_id={dup.document_id})")
            if dup is None and await repo.count_active(s, tenant_id, kb_id) >= settings.compile_max_documents:
                raise ValidationError(f"该知识库文档数已达上限 {settings.compile_max_documents}")
            input_sha = hashlib.sha256(data).hexdigest()
            sha = receipt["original_sha256"] if receipt else input_sha
            if dup is not None:
                previous = self.layout.raw_dir(tenant_id, kb_id, dup.document_id, dup.content_generation) / "wiki-input.json"
                if receipt and previous.is_file():
                    previous_receipt = json.loads(previous.read_text("utf-8"))
                    if receipt["content_generation"] < previous_receipt["content_generation"]:
                        raise ConflictError("WIKI_INPUT_STALE_GENERATION")
                    if (receipt["content_generation"] == previous_receipt["content_generation"]
                            and sha != previous_receipt["original_sha256"]):
                        raise ConflictError("WIKI_INPUT_GENERATION_REUSED_FOR_DIFFERENT_SOURCE")
                    from jonex_wiki.domain.parsing.identity import parse_input_signature
                    previous_artifacts = self.layout.artifacts_path(tenant_id, kb_id, dup.document_id, dup.content_generation)
                    first = {}
                    if previous_artifacts.is_file():
                        with previous_artifacts.open(encoding="utf-8") as stream:
                            line = stream.readline()
                            first = json.loads(line) if line.strip() else {}
                    if (previous_receipt.get("receipt_sha256") == receipt.get("receipt_sha256")
                            and dup.status == "parsed"
                            and (first.get("source_metadata") or {}).get("parse_input_signature") == parse_input_signature(settings)):
                        dup.recompile_generation += 1
                        task = (await s.execute(select(TaskRow).where(TaskRow.tenant_id == tenant_id,
                            TaskRow.kb_id == kb_id, TaskRow.kind == TaskKind.compile.value,
                            TaskRow.status == TaskStatus.queued.value))).scalars().first()
                        fresh_task = task is None
                        if fresh_task:task = await TaskRepo().create(s, tenant_id=tenant_id, kb_id=kb_id, kind=TaskKind.compile, compile_model=compile_model)
                        else:TaskRepo.restamp_compile_model(task, compile_model)
                        await s.commit()
                        if fresh_task and submit:self.executor.submit(task)
                        return {"task_id": task.task_id, "document_id": dup.document_id,
                            "status": task.status, "content_generation": dup.content_generation,
                            "reused_parse": True, "warnings": [], "contract": {"route": route.value}}
                # [jonex] 同名**且同 document_id** 的再次入库不是冲突，而是平台侧
                # reparse(force) 的重放：同一文档的新一代内容。沿用 parse_task 的既有语义
                # 升一代（content_generation+1），source_port 只读增量代，编译产出新 revision。
                row = dup
                new_gen = dup.content_generation + 1
                row.source_sha256 = sha
            else:
                row = await repo.add(s, tenant_id=tenant_id, kb_id=kb_id, source_name=source_name,
                                     source_sha256=sha, route=route.value,
                                     parser_id=parser_id, parser_version=parser_version,
                                     document_id=document_id)
                new_gen = 1
            raw_dir = self.layout.raw_dir(tenant_id, kb_id, row.document_id, new_gen)
            raw_dir.mkdir(parents=True, exist_ok=True)
            if receipt:
                await asyncio.to_thread(shutil.copyfile, full.parent / receipt["original_path"], raw_dir / receipt["original_path"])
                with (raw_dir / receipt["original_path"]).open("rb") as original_stream:
                    if (await asyncio.to_thread(hashlib.file_digest, original_stream, "sha256")).hexdigest() != sha:
                        raise ValidationError("WIKI_INPUT_ORIGINAL_CHANGED_DURING_COPY")
                (raw_dir / "wiki-input.json").write_text(json.dumps(receipt, ensure_ascii=False), "utf-8")
                if settings.pdf_text_layer_enabled and Path(receipt["original_path"]).suffix.lower() == ".pdf":
                    from jonex_wiki.domain.parsing.pdf_text_layer import extract_text_layer
                    await asyncio.to_thread(extract_text_layer, raw_dir / receipt["original_path"], raw_dir,
                        max_pages=settings.pdf_text_layer_max_pages)
                if (settings.media_keyframes_enabled and Path(receipt["original_path"]).suffix.lower()
                        in {".mp4", ".mov", ".mkv", ".webm"}):
                    from jonex_wiki.domain.parsing.media_timeline import supplement_video
                    await asyncio.to_thread(supplement_video, raw_dir / receipt["original_path"], raw_dir,
                        max_frames=settings.media_keyframes_max_frames,
                        interval_seconds=settings.media_keyframes_interval_seconds)
            # [jonex] 图片随源搬入 data_root(共享卷 images/ → raw/images/)
            source_images = full.parent / _IMAGES_DIR
            if source_images.is_dir():
                try:
                    await asyncio.to_thread(
                        shutil.copytree, source_images, raw_dir / _IMAGES_DIR, dirs_exist_ok=True)
                except Exception:
                    if receipt:
                        raise ValidationError("WIKI_INPUT_ASSET_COPY_FAILED")
                    log.warning("[jonex] 复制共享卷 images 失败,继续编译: %s", source_images,
                                exc_info=True)
            packing = PackingOptions.from_settings(settings)
            # [jonex] 编译前体检:单元级缺陷(超限/空表/复读)→ 隔离该单元 + 留档,
            # 而不是让整份文档因一个坏点失败;比例超闸门仍整份失败(fail closed)。
            inspection = InspectionPolicy.from_settings(settings)
            report = InspectionReport(source_name=source_name)
            if route is InputRoute.SPREADSHEET_NATIVE:
                # core 原生 OOXML 解析:tables.sqlite + 工作簿级阅读材料。
                result = await asyncio.to_thread(
                    parse_spreadsheet, raw_dir / receipt["original_path"] if receipt else full, output_dir=raw_dir,
                    source_id=row.document_id, document_id=row.document_id)
                # 保留平台侧原始文件名:parse_spreadsheet 以暂存文件名作为 source_name,
                # 保留原始来源身份，避免暂存文件名取代用户文件名。
                result.source_name = source_name
                for block in result.blocks:
                    if block.metadata and "source_name" in block.metadata:
                        block.metadata["source_name"] = source_name
                await asyncio.to_thread(lambda: (raw_dir / SOURCE_TEXT_FILE).write_text(
                    result.markdown, "utf-8"))
            elif route is InputRoute.MINERU:
                # [jonex] 平台已把 MinerU 结构化产物落盘为 content_list.json + images/;
                # 读回 items 走 normalize_inline_content_list(与独立部署 parse task 的
                # MINERU 分支同路径),避免 markdown 重组丢失 table/image/bbox 结构。
                try:
                    items = json.loads(data.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise ValidationError(f"content_list.json 无法解析: {source_path}") from exc
                if not isinstance(items, list):
                    raise ValidationError(f"content_list.json 必须是数组: {source_path}")
                if receipt and Path(receipt["original_path"]).suffix == ".docx":
                    from jonex_wiki.domain.engine.compile.source_prep.docx_native import supplement_docx
                    items, native_inventory = await asyncio.to_thread(supplement_docx, items,
                        full.parent / receipt["original_path"], raw_dir)
                result = await asyncio.to_thread(
                    normalize_inline_content_list, items, output_dir=raw_dir,
                    source_name=source_name, parser_id=parser_id,
                    source_sha256=sha, source_id=row.document_id,
                    document_id=row.document_id, markdown="", packing=packing,
                    inspection=inspection, report=report)
                if receipt and Path(receipt["original_path"]).suffix == ".docx" and result.blocks:
                    result.blocks[0].metadata["native_inventory"] = {
                        "objects": native_inventory["objects"], "gaps": native_inventory["gaps"],
                        "warnings": native_inventory["warnings"], "alignment": native_inventory.get("alignment", [])}
                # 表资产后处理:table_observation → tables.sqlite(对齐 parse_task MINERU 分支)
                table_dir = self.layout.gen_dir(
                    tenant_id, kb_id, row.document_id, new_gen) / "tables"
                await asyncio.to_thread(persist_table_observations, result, table_dir)
            else:
                text = data.decode("utf-8")
                await asyncio.to_thread(lambda: (raw_dir / SOURCE_TEXT_FILE).write_text(
                    text, "utf-8"))
                # [jonex] 内容格式按 source_path 判,不按原始文件名 source_name——
                # 否则 .mp4/.pdf 原始名会被误当 direct 格式抛 unsupported direct format。
                content_suffix = suffix or ".txt"
                result = await asyncio.to_thread(
                    blocks_from_direct_text, text, suffix=content_suffix,
                    source_name=source_name, source_id=row.document_id, document_id=row.document_id,
                    source_sha256=sha, parser_id=parser_id, parser_version=parser_version,
                    packing=packing, inspection=inspection, report=report)
            # [jonex] 源侧留档:被体检摘出编译面的原文切片落 gen-N/source-archive/(与 raw/ 平级),
            # 让判据可复核、后续接 VLM 判官时能只对被摘除的部分重跑;没有缺陷就不建目录。
            archive_path = await asyncio.to_thread(
                write_source_archive,
                self.layout.gen_dir(tenant_id, kb_id, row.document_id, new_gen),
                report, data.decode("utf-8", errors="replace"), source_name=source_name)
            if archive_path is not None:
                log.warning("[jonex] 源侧体检摘除 %d 个单元(%d 字符,%.2f%%):%s",
                            len(report.defects), report.isolated_chars,
                            report.isolated_ratio * 100, archive_path)
            result.reading_materials = await asyncio.to_thread(prepare_reading_materials, result)
            artifacts = result.to_source_artifacts(content_generation=new_gen)
            from jonex_wiki.domain.parsing.identity import (
                parse_input_components, parse_input_signature)
            parse_signature = parse_input_signature(settings)
            # 与 domain/parsing/parse_task.py 同口径：分量摘要一并落盘，产物过期时
            # parse_status 才能说清「哪一项变了」。两处都是产物的写入方，缺一处就会
            # 让那条预检对这类产物只能回 "unknown"。
            parse_components = parse_input_components(settings)
            for artifact in artifacts:
                artifact.source_metadata["parse_input_signature"] = parse_signature
                artifact.source_metadata["parse_input_components"] = parse_components
                artifact.source_metadata["parsed_sha256"] = receipt["parsed_sha256"] if receipt else input_sha
                artifact.source_metadata["source_sha256_kind"] = "original_bytes" if receipt or route is InputRoute.SPREADSHEET_NATIVE else "legacy_parser_input"
                if receipt:
                    artifact.source_metadata["consumer_receipt"] = receipt
                    artifact.source_metadata["platform_content_generation"] = receipt["content_generation"]
                    for key, value in receipt.get("source_context", {}).items():
                        artifact.source_metadata[key] = value
            art_path = self.layout.artifacts_path(tenant_id, kb_id, row.document_id, new_gen)
            art_lines = [json.dumps(a.model_dump(mode="json"), ensure_ascii=False)
                         for a in artifacts]
            await asyncio.to_thread(self.layout.atomic_write, art_path,
                                    ("\n".join(art_lines) + "\n").encode("utf-8")
                                    if art_lines else b"")
            manifest = build_manifest(document_id=row.document_id, source_name=source_name,
                                      source_sha256=sha, route=route.value,
                                      parser_id=parser_id, parser_version=parser_version,
                                      content_generation=new_gen)
            await asyncio.to_thread(write_manifest, raw_dir / MANIFEST_FILE, manifest)
            row.content_generation = new_gen
            row.status = "parsed"
            task = (await s.execute(select(TaskRow).where(
                TaskRow.tenant_id == tenant_id, TaskRow.kb_id == kb_id,
                TaskRow.kind == TaskKind.compile.value,
                TaskRow.status == TaskStatus.queued.value,
            ))).scalars().first()
            new_task = task is None
            if new_task:
                task = await TaskRepo().create(
                    s, tenant_id=tenant_id, kb_id=kb_id, kind=TaskKind.compile,
                    compile_model=compile_model)
            else:
                TaskRepo.restamp_compile_model(task, compile_model)
            await s.commit()
            if new_task and submit:
                self.executor.submit(task)
            unrepresented = sum(len(block.metadata.get("unrepresented_items") or [])
                                for block in result.blocks)
            structured = route in (InputRoute.MINERU, InputRoute.SPREADSHEET_NATIVE)
            return {"task_id": task.task_id, "document_id": row.document_id,
                    "status": task.status,
                    # 体检结论 + 产物形状契约随 ingest 响应回平台(写
                    # llm_wiki_compile_warnings);体检结论同时已写进 ParseResult.warnings
                    # → artifacts metadata。
                    "warnings": list(result.warnings)
                                + _contract_warnings(rel.name, source_name, route),
                    "inspection": report.summary(),
                    "contract": {"route": route.value,
                                 "artifact": rel.name,
                                 "shape": "structured" if structured else "text",
                                 "unrepresented_items": unrepresented}}

    async def remove_document(self, tenant_id: str, kb_id: str, document_id: str,
                              *, compile_model: str = "") -> dict:
        """逻辑删除 + 自动触发编译(合并触发);删除豁免成本闸门。"""
        async with self.layout.version_lock(tenant_id, kb_id):
            return await self._remove_document_locked(
                tenant_id, kb_id, document_id, compile_model=compile_model)

    async def _remove_document_locked(self, tenant_id: str, kb_id: str,
                                      document_id: str, *, compile_model: str = "") -> dict:
        async with self.session_factory() as s:
            repo = DocumentRepo()
            doc = await repo.get(s, tenant_id, kb_id, document_id)
            if doc is None or doc.deleted_at is not None:
                raise NotFoundError(f"文档 {document_id} 不存在")
            parsing = (await s.execute(select(TaskRow).where(
                TaskRow.tenant_id == tenant_id, TaskRow.kb_id == kb_id,
                TaskRow.document_id == document_id, TaskRow.kind == TaskKind.parse.value,
                TaskRow.status.in_([TaskStatus.queued.value, TaskStatus.parsing.value])))).scalars().first()
            if parsing is not None:
                raise ConflictError("该文档正在解析,请先取消解析任务")
            compile_task = (await s.execute(select(TaskRow).where(
                TaskRow.tenant_id == tenant_id, TaskRow.kb_id == kb_id,
                TaskRow.kind == TaskKind.compile.value,
                TaskRow.status == TaskStatus.queued.value))).scalars().first()
            active_compile = (await s.execute(select(TaskRow.task_id).where(
                TaskRow.tenant_id == tenant_id, TaskRow.kb_id == kb_id,
                TaskRow.kind == TaskKind.compile.value,
                TaskRow.status.in_([TaskStatus.compiling.value, TaskStatus.paused.value])))).first()
            await repo.soft_delete(s, doc)
            if compile_task is None:
                compile_task = await TaskRepo().create(s, tenant_id=tenant_id, kb_id=kb_id,
                                                       kind=TaskKind.compile,
                                                       compile_model=compile_model)
            else:
                TaskRepo.restamp_compile_model(compile_task, compile_model)
            await s.commit()
            # 物理删除该文档的全部解析产物(parse/{document_id}/ 所有代次)。软删只标
            # deleted_at 并靠重编译的 removed_ids 清理 bundle 页面,解析产物目录需在
            # 此显式清掉,否则同名重传冲突解除后仍残留旧代产物。
            doc_parse_dir = self.layout.kb_dir(tenant_id, kb_id) / "parse" / document_id
            # A pinned attempt may still be reading this generation. Its
            # publication check restarts after deletion; retain those bytes
            # until the attempt no longer depends on them.
            if active_compile is None:
                await asyncio.to_thread(shutil.rmtree, doc_parse_dir, True)
            await self.executor.cancel_kb_queries(tenant_id, kb_id)
            if compile_task.status == TaskStatus.queued.value:
                self.executor.submit(compile_task)
            return {"document_id": document_id, "compile_task_id": compile_task.task_id,
                    "status": compile_task.status}

    async def reparse_document(self, tenant_id: str, kb_id: str, document_id: str,
                               *, compile_model: str = "") -> dict:
        """重新解析原上传文件。"""
        async with self.session_factory() as s:
            doc = await DocumentRepo().get(s, tenant_id, kb_id, document_id)
            if doc is None or doc.deleted_at is not None:
                raise NotFoundError(f"文档 {document_id} 不存在")
            running = (await s.execute(select(TaskRow).where(
                TaskRow.tenant_id == tenant_id, TaskRow.kb_id == kb_id,
                TaskRow.document_id == document_id, TaskRow.kind == TaskKind.parse.value,
                TaskRow.status.in_([TaskStatus.queued.value, TaskStatus.parsing.value])))).scalars().first()
            if running is not None:
                raise ConflictError("该文档已有进行中的解析任务")
            task = await TaskRepo().create(s, tenant_id=tenant_id, kb_id=kb_id,
                                           kind=TaskKind.parse, document_id=document_id,
                                           compile_model=compile_model)
            await s.commit()
            self.executor.submit(task)
            return {"task_id": task.task_id, "document_id": document_id, "status": task.status}

    async def get_document_chunks(self, tenant_id: str, kb_id: str, document_id: str) -> dict:
        """Read the current saved compile inputs, without requiring publication."""
        from jonex_wiki.kernel.contracts import SourceArtifact

        async with self.session_factory() as s:
            doc = await DocumentRepo().get(s, tenant_id, kb_id, document_id)
            if doc is None or doc.deleted_at is not None:
                raise NotFoundError(f"文档 {document_id} 不存在")
        chunks = []
        if doc.content_generation:
            path = self.layout.artifacts_path(tenant_id, kb_id, document_id, doc.content_generation)

            def read():
                with path.open(encoding="utf-8") as handle:
                    artifacts = [SourceArtifact.model_validate_json(line)
                                 for line in handle if line.strip()]
                artifacts.sort(key=lambda a: a.position if a.position is not None else 10**12)
                return [{
                    "chunk_id": a.chunk_id, "content": a.text,
                    "content_length": len(a.text), "file_name": doc.source_name,
                    "chunk_order_index": a.position, "chunk_index": a.position,
                    "page_no": a.page_number, "file_path": f"doc={document_id}",
                    "tokens": None, "full_doc_id": None,
                    "time_start": None, "time_end": None,
                    "char_start": None, "char_end": None,
                } for a in artifacts]

            chunks = await asyncio.to_thread(read)
        return {"doc_id": document_id, "total": len(chunks), "chunks": chunks,
                "kb_type": "jonex_wiki"}

    async def parse_status(self, tenant_id: str, kb_id: str) -> dict:
        """Read-only inventory of documents needing reparse before compilation.

        ``drift`` 说明这份产物**为什么**过期（identity 的哪个分量变了）：
        ``["unknown"]`` = 旧产物没记分量；``[]`` = 各项一致。
        """
        from jonex_wiki.domain.parsing.identity import (
            identity_drift, parse_input_components, parse_input_signature)
        expected = parse_input_signature(self.settings)
        expected_components = parse_input_components(self.settings)
        async with self.session_factory() as s:
            docs = await DocumentRepo().list_parsed_for_compile(s, tenant_id, kb_id)
        items = []
        for doc in docs:
            path = self.layout.artifacts_path(
                tenant_id, kb_id, doc.document_id, doc.content_generation)
            actual = None
            drift = ["unknown"]
            status = "missing"
            if path.is_file():
                with path.open("r", encoding="utf-8") as handle:
                    first = next((line for line in handle if line.strip()), None)
                if first is not None:
                    try:
                        metadata = (json.loads(first).get("source_metadata") or {})
                        actual = metadata.get("parse_input_signature")
                        status = ("current" if actual == expected else
                                  "stale" if actual else "legacy")
                        if actual != expected:
                            drift = identity_drift(
                                metadata.get("parse_input_components"),
                                expected_components)
                        else:
                            drift = []
                    except (ValueError, TypeError, AttributeError):
                        status = "invalid"
            items.append({"document_id": doc.document_id,
                          "source_name": doc.source_name,
                          "content_generation": doc.content_generation,
                          "status": status, "needs_reparse": status != "current",
                          "drift": drift,
                          "refresh_method": (
                              "reparse" if (self.layout.kb_dir(tenant_id, kb_id)
                                            / "uploads" / doc.document_id).is_dir()
                              else "ingest")})
        return {"kb_id": kb_id, "documents": items,
                "needs_reparse_count": sum(item["needs_reparse"] for item in items)}

    async def recompile_document(self, tenant_id: str, kb_id: str, document_id: str,
                                 *, compile_model: str = "") -> dict:
        """单文档重新编译:recompile_generation +1 → 增量编译。"""
        async with self.layout.version_lock(tenant_id, kb_id):
            return await self._recompile_document_locked(
                tenant_id, kb_id, document_id, compile_model=compile_model)

    async def _recompile_document_locked(self, tenant_id: str, kb_id: str,
                                         document_id: str, *, compile_model: str = "") -> dict:
        async with self.session_factory() as s:
            doc = await DocumentRepo().get(s, tenant_id, kb_id, document_id)
            if doc is None or doc.deleted_at is not None:
                raise NotFoundError(f"文档 {document_id} 不存在")
            if doc.status != "parsed":
                raise ConflictError(f"文档尚未解析完成(当前 {doc.status}),无法重编译")
            compile_task = (await s.execute(select(TaskRow).where(
                TaskRow.tenant_id == tenant_id, TaskRow.kb_id == kb_id,
                TaskRow.kind == TaskKind.compile.value,
                TaskRow.status == TaskStatus.queued.value))).scalars().first()
            doc.recompile_generation += 1
            if compile_task is None:
                compile_task = await TaskRepo().create(s, tenant_id=tenant_id, kb_id=kb_id,
                                                       kind=TaskKind.compile,
                                                       compile_model=compile_model)
            else:
                TaskRepo.restamp_compile_model(compile_task, compile_model)
            await s.commit()
            if compile_task.status == TaskStatus.queued.value:
                self.executor.submit(compile_task)
            return {"document_id": document_id, "compile_task_id": compile_task.task_id,
                    "recompile_generation": doc.recompile_generation,
                    "status": compile_task.status}

    async def rename_document(self, tenant_id: str, kb_id: str,
                              document_id: str, source_name: str,
                              *, compile_model: str = "") -> dict:
        """Change only the display name, retaining document/source identity."""
        async with self.layout.version_lock(tenant_id, kb_id):
            return await self._rename_document_locked(
                tenant_id, kb_id, document_id, source_name, compile_model=compile_model)

    async def _rename_document_locked(self, tenant_id: str, kb_id: str,
                                      document_id: str, source_name: str,
                                      *, compile_model: str = "") -> dict:
        name = source_name.strip()
        normalized = name.replace("\\", "/")
        if (not name or len(name) > 255 or normalized != Path(normalized).name
                or any(ord(char) < 32 for char in name)):
            raise ValidationError("source_name 必须是 1-255 字符的文件名，不能含路径或控制字符")
        async with self.session_factory() as s:
            doc = await DocumentRepo().get(s, tenant_id, kb_id, document_id)
            if doc is None or doc.deleted_at is not None:
                raise NotFoundError(f"文档 {document_id} 不存在")
            if doc.status != "parsed":
                raise ConflictError("文档尚未解析完成，不能更改显示名称")
            old_name = doc.source_name
            if name == old_name:
                return {"document_id": document_id, "source_name": name,
                        "compile_task_id": None, "status": "unchanged"}
            if Path(name).suffix.lower() != Path(old_name).suffix.lower():
                raise ValidationError("显示改名不能改变扩展名；格式变化请重新上传并解析")
            conflict = (await s.execute(select(DocumentRow.document_id).where(
                DocumentRow.tenant_id == tenant_id, DocumentRow.kb_id == kb_id,
                DocumentRow.source_name == name, DocumentRow.deleted_at.is_(None),
                DocumentRow.document_id != document_id,
            ))).first()
            if conflict is not None:
                raise ConflictError(f"同知识库已存在同名文档 {name}")
            parsing = (await s.execute(select(TaskRow.task_id).where(
                TaskRow.tenant_id == tenant_id, TaskRow.kb_id == kb_id,
                TaskRow.document_id == document_id, TaskRow.kind == TaskKind.parse.value,
                TaskRow.status.in_([TaskStatus.queued.value, TaskStatus.parsing.value]),
            ))).first()
            if parsing is not None:
                raise ConflictError("文档正在解析，不能更改显示名称")
            compile_task = (await s.execute(select(TaskRow).where(
                TaskRow.tenant_id == tenant_id, TaskRow.kb_id == kb_id,
                TaskRow.kind == TaskKind.compile.value,
                TaskRow.status.in_([TaskStatus.queued.value, TaskStatus.compiling.value]),
            ))).scalars().first()
            if compile_task is not None and compile_task.status == TaskStatus.compiling.value:
                raise ConflictError("编译进行中，不能更改显示名称")
            doc.source_name = name
            if compile_task is None:
                compile_task = await TaskRepo().create(
                    s, tenant_id=tenant_id, kb_id=kb_id, kind=TaskKind.compile,
                    compile_model=compile_model)
            else:
                TaskRepo.restamp_compile_model(compile_task, compile_model)
            await s.commit()
        if compile_task.status == TaskStatus.queued.value:
            self.executor.submit(compile_task)
        return {"document_id": document_id, "source_name": name,
                "previous_source_name": old_name,
                "compile_task_id": compile_task.task_id, "status": compile_task.status}

    async def query(self, tenant_id: str, kb_id: str, question: str,
                    *, thinking_mode: str | None = None, retrieval_strategy: str | None = None,
                    query_context: dict | None = None) -> dict:
        """提交查询任务(异步,返回 task_id)。"""
        question = (question or "").strip()
        from jonex_wiki.kernel.query_images import normalized_context
        query_context = normalized_context(query_context)
        if not question or len(question) > 4096:
            raise ValidationError("question 必须为 1-4096 字符")
        if thinking_mode is not None:
            from jonex_wiki.domain.query.thinking import thinking_overrides
            thinking_overrides(thinking_mode)
        if retrieval_strategy is not None and retrieval_strategy not in {"tree", "direct", "structure", "semantic"}:
            raise ValidationError("QUERY_RETRIEVAL_STRATEGY_INVALID")
        async with self.layout.version_lock(tenant_id, kb_id):
            published = self.layout.resolve_published(tenant_id, kb_id)
            if published is None or not published.is_dir():
                raise NotFoundError(f"知识库 {kb_id} 不存在或无可查询的已发布 bundle")
            async with self.session_factory() as s:
                from jonex_wiki.domain.source_visibility import ensure_sources_visible
                await ensure_sources_visible(s, tenant_id, kb_id, published)
                row = await QueryTaskRepo().create(s, tenant_id=tenant_id, kb_id=kb_id,
                                                   question=question, bundle_sha=published.name)
                query_id = row.query_id
                if retrieval_strategy is not None or query_context is not None:
                    self.layout.atomic_write(self.layout.kb_dir(tenant_id,kb_id) / "query-options" / (query_id+".json"),
                        json.dumps({**({'retrieval_strategy':retrieval_strategy} if retrieval_strategy else {}),
                            **({'query_context':query_context} if query_context else {})}, ensure_ascii=False).encode())
                await s.commit()
        if thinking_mode is None:
            self.executor.submit_query(row)
        else:
            self.executor.submit_query(row, thinking_mode=thinking_mode)
        return {"task_id": query_id, "kind": "query"}

    async def get_task(self, tenant_id: str, task_id: str) -> dict:
        """读任务状态(compile/parse 或 query)。"""
        if task_id.startswith("qry-"):
            async with self.session_factory() as s:
                row = await QueryTaskRepo().get(s, tenant_id, task_id)
                if row is None:
                    raise NotFoundError(f"任务 {task_id} 不存在")
                return {"task_id": row.query_id, "kind": TaskKind.query.value, "status": row.status,
                        "stage": row.stage, "error": row.error, "result": row.result,
                        "progress": row.progress, "kb_id": row.kb_id}
        async with self.session_factory() as s:
            row = await TaskRepo().get(s, tenant_id, task_id)
            if row is None:
                raise NotFoundError(f"任务 {task_id} 不存在")
            return {"task_id": row.task_id, "kind": row.kind, "status": row.status,
                    "stage": row.stage, "error": row.error, "summary": row.summary}

    async def list_tasks(self, tenant_id: str, kb_id: str, kind: str | None = None,
                         limit: int = 50, offset: int = 0) -> dict:
        async with self.session_factory() as s:
            rows = await TaskRepo().list_for_kb(s, tenant_id, kb_id, kind=kind, limit=limit,
                                                offset=offset)
            return {"tasks": [
                {"task_id": r.task_id, "kind": r.kind, "status": r.status,
                 "stage": r.stage, "error": r.error}
                for r in rows]}

    async def cancel_task(self, tenant_id: str, task_id: str) -> dict:
        async with self.session_factory() as s:
            if task_id.startswith("qry-"):
                row = await self.executor.cancel_query(s, tenant_id, task_id)
                return {"task_id": task_id, "status": row.status}
            row = await self.executor.cancel(s, tenant_id, task_id)
            if row.kind == TaskKind.parse.value and row.status == "cancelled" and row.document_id:
                doc = await DocumentRepo().get(s, tenant_id, row.kb_id, row.document_id)
                if doc is not None and doc.status == "parsing":
                    doc.status = "failed"
                    doc.error = "cancelled"
                await s.commit()
            status = "cancelling" if row.status != "cancelled" else "cancelled"
            return {"task_id": task_id, "status": status}

    async def set_compilation_paused(self, tenant_id: str, kb_id: str, paused: bool) -> dict:
        async with self.layout.version_lock(tenant_id, kb_id):
            await self.executor.set_compilation_paused(tenant_id, kb_id, paused)
        return await self.compilation_state(tenant_id, kb_id)

    async def compilation_state(self, tenant_id: str, kb_id: str) -> dict:
        from jonex_wiki.domain.compile_task import _published_source_count
        async with self.session_factory() as s:
            docs = await DocumentRepo().list_parsed_for_compile(s, tenant_id, kb_id)
            tasks = (await s.execute(select(TaskRow).where(
                TaskRow.tenant_id == tenant_id, TaskRow.kb_id == kb_id,
                TaskRow.kind == TaskKind.compile.value,
                TaskRow.status.in_([TaskStatus.queued.value, TaskStatus.compiling.value,
                                   TaskStatus.paused.value])))).scalars().all()
            latest = (await s.execute(select(TaskRow).where(
                TaskRow.tenant_id == tenant_id, TaskRow.kb_id == kb_id,
                TaskRow.kind == TaskKind.compile.value).order_by(TaskRow.created_at.desc()).limit(1))).scalars().first()
            parses = (await s.execute(select(TaskRow.task_id).where(
                TaskRow.tenant_id == tenant_id, TaskRow.kb_id == kb_id,
                TaskRow.kind == TaskKind.parse.value,
                TaskRow.status.in_([TaskStatus.queued.value, TaskStatus.parsing.value])))).all()
        return {"kb_id": kb_id, "paused": self.layout.compilation_paused(tenant_id, kb_id),
                "pending_source_count": len(parses),
                "last_task_status": latest.status if latest else None,
                "last_task_error": latest.error if latest else None,
                "parsed_source_count": len(docs),
                "published_source_count": _published_source_count(self.layout, tenant_id, kb_id),
                "tasks": [{"task_id": t.task_id, "status": t.status, "stage": t.stage,
                           "source_names": [item[3] for item in (t.summary or {}).get("source_snapshot", [])]}
                          for t in tasks]}

    async def delete_kb(self, tenant_id: str, kb_id: str) -> dict:
        """删除 KB:软删全部文档 + 清任务/提示/查询任务 + 删目录(幂等)。"""
        async with self.session_factory() as s:
            docs = (await s.execute(select(DocumentRow).where(
                DocumentRow.tenant_id == tenant_id, DocumentRow.kb_id == kb_id,
                DocumentRow.deleted_at.is_(None)))).scalars().all()
            for d in docs:
                d.deleted_at = _now()
            await s.execute(delete(TaskRow).where(
                TaskRow.tenant_id == tenant_id, TaskRow.kb_id == kb_id))
            await s.execute(delete(PromptTemplateRow).where(
                PromptTemplateRow.tenant_id == tenant_id, PromptTemplateRow.kb_id == kb_id))
            await s.execute(delete(QueryTaskRow).where(
                QueryTaskRow.tenant_id == tenant_id, QueryTaskRow.kb_id == kb_id))
            await s.commit()
        await asyncio.to_thread(shutil.rmtree, self.layout.kb_dir(tenant_id, kb_id), True)
        return {"kb_id": kb_id, "status": "deleted"}

    # ── 只读页面(自 pages.py 提取)──

    def _published_dir(self, tenant_id: str, kb_id: str) -> Path:
        d = self.layout.resolve_published(tenant_id, kb_id)
        if d is None:
            raise NotFoundError(f"知识库 {kb_id} 尚未编译发布")
        return d

    async def list_pages(self, tenant_id: str, kb_id: str) -> dict:
        from jonex_wiki.domain.reading import list_pages as _list_pages
        pub = self.layout.resolve_published(tenant_id, kb_id)
        if pub is None:
            return {"kb_id": kb_id, "pages": [], "published": False}
        return {"kb_id": kb_id, "pages": await asyncio.to_thread(_list_pages, pub)}

    async def count_pages(self, tenant_id: str, kb_id: str) -> dict:
        """[jonex] 只要页面数量（按引擎 page_type 分桶），不构造页面对象。

        给「编译资产 tab 的三类页数卡片 + tab 计数」这类只关心数字的调用方用；
        口径与 ``list_pages`` 完全一致（同样剔除 raw 页、同样做同名归并）。
        """
        from jonex_wiki.domain.reading import count_pages as _count_pages
        pub = self._published_dir(tenant_id, kb_id)
        return {"kb_id": kb_id, "counts": await asyncio.to_thread(_count_pages, pub)}

    async def read_page(self, tenant_id: str, kb_id: str, page_id: str) -> dict:
        async with self.layout.version_lock(tenant_id, kb_id):
            return await self._read_page_content(tenant_id, kb_id, page_id)

    async def _read_page_content(self, tenant_id: str, kb_id: str, page_id: str) -> dict:
        from jonex_wiki.domain.reading import read_page as _read_page
        pub = self._published_dir(tenant_id, kb_id)
        result = await asyncio.to_thread(_read_page, pub, page_id)
        from jonex_wiki.adapters.bundle_view import DirectoryBundleView
        from jonex_wiki.domain.reading.image_service import page_images
        image_view = DirectoryBundleView(tenant_id=tenant_id, context_id=kb_id, revision_dir=pub)
        try:
            body, images, total = await asyncio.to_thread(page_images, image_view,
                result.get("merged_paths") or [result["path"]], result["body"])
            result.update(body=body, content=body, images=images, images_total=total,
                          bundle_sha=image_view.manifest_hash)
        except (OSError, ValueError, sqlite3.Error, DomainError) as exc:
            result.update(images=[], image_error=type(exc).__name__)
        result["kb_id"] = kb_id
        return result

    async def get_page_backlinks(self, tenant_id: str, kb_id: str, path: str) -> dict:
        from jonex_wiki.domain.reading import get_backlinks as _get_backlinks
        pub = self._published_dir(tenant_id, kb_id)
        result = await asyncio.to_thread(_get_backlinks, pub, path)
        result["kb_id"] = kb_id
        return result

    async def get_page_claims(self, tenant_id: str, kb_id: str, *,
                              path: str = "", document_id: str = "",
                              limit: int = 500) -> dict:
        from jonex_wiki.domain.reading import get_claims as _get_claims
        pub = self._published_dir(tenant_id, kb_id)
        return await asyncio.to_thread(_get_claims, pub, page_path=path, document_id=document_id, limit=limit)

    async def list_page_documents(self, tenant_id: str, kb_id: str) -> dict:
        from jonex_wiki.domain.reading import list_page_documents as _list_page_documents
        pub = self.layout.resolve_published(tenant_id, kb_id)
        if pub is None:
            return {"kb_id": kb_id, "documents": {}}
        return {"kb_id": kb_id, "documents": await asyncio.to_thread(_list_page_documents, pub)}

    async def query_graph(self, tenant_id: str, kb_id: str, operation: str, **options) -> dict:
        """Graph reads share the same immutable publication across UI and MCP."""
        from jonex_wiki.domain.reading.graph_service import query_graph
        return await query_graph(self.layout, tenant_id, kb_id, operation, options)

    def scenario_service(self):
        if not hasattr(self, "_scenarios"):
            from jonex_wiki.scenarios.service import ScenarioService
            self._scenarios = ScenarioService(self)
        return self._scenarios

    async def query_resources(self, tenant_id: str, kb_id: str, operation: str,
                              options: dict, *, actor: str = "") -> dict:
        if operation in {"find_images", "get_image"}:
            from jonex_wiki.domain.reading.image_requests import query_images
            return await query_images(self.layout, tenant_id, kb_id, operation, options)
        if operation == "inspect_image":
            from jonex_wiki.domain.reading.image_inspection import inspect_image
            if not hasattr(self, "_visual_inspection_semaphore"):
                self._visual_inspection_semaphore = asyncio.Semaphore(2)
            return await inspect_image(self.layout, self.settings, self.gw, tenant_id, kb_id,
                options, self._visual_inspection_semaphore)
        if operation == "get_question":
            # Old frozen publications remain readable. New bases never build
            # these objects; their replacements are gated by the scene host.
            view=self.scenario_service().view(tenant_id,kb_id,str(options.get("revision")or""))
            if json.loads(view.read_bytes("query/resources.json")).get("schema")!="wiki.resources.v1":
                raise ValidationError("SCENARIO_OPERATION_REQUIRED; use enabled scenario resources")
        if operation == "inspect_visual":
            from jonex_wiki.domain.reading.visual_inspection import inspect_visual
            if not hasattr(self, "_visual_inspection_semaphore"):
                self._visual_inspection_semaphore = asyncio.Semaphore(2)
            return await inspect_visual(self.layout, self.settings, self.gw, tenant_id, kb_id,
                options, self._visual_inspection_semaphore)
        from jonex_wiki.domain.reading.resource_service import query_resources
        return await query_resources(self.layout, tenant_id, kb_id, operation, options, actor=actor)

    # ── Wiki 阅读目录(目录功能下沉)──

    def _outline_service(self):
        if self._outline is None:
            from jonex_wiki.domain.outline import OutlineService
            self._outline = OutlineService(
                session_factory=self.session_factory, gw=self.gw,
                layout=self.layout, settings=self.settings)
        return self._outline

    async def get_outline(self, tenant_id: str, kb_id: str, document_id: str = "") -> dict:
        """读目录 + 页面清单（阅读页首屏一次拿全；document_id 非空 = 文档级投影）。"""
        return await self._outline_service().get_view(tenant_id, kb_id, document_id)

    async def draft_outline(self, tenant_id: str, kb_id: str, request: dict,
                            *, user_id=None) -> dict:
        """LLM 起草目录并落库（人工「重新生成」无条件覆盖旧 active 行）。"""
        return await self._outline_service().draft(tenant_id, kb_id, request, user_id=user_id)

    async def adjust_outline(self, tenant_id: str, kb_id: str, request: dict,
                             *, user_id=None) -> dict:
        """按自然语言指令调整现有目录（覆盖率护栏不过则整次失败）。"""
        return await self._outline_service().adjust(tenant_id, kb_id, request, user_id=user_id)

    # ── 提示词(自 prompts.py 提取)──

    async def get_prompt(self, tenant_id: str, kb_id: str, prompt_key: str) -> dict:
        """读单个 prompt 的解析结果(source/version/template_sha/template)。

        供平台 apply_schema 投影前判断 expected_version:引擎侧尚未建立自定义行时
        source="default"、version=None,首次写入应传 expected_version=0(PromptRepo.upsert
        的 CAS 语义——新建传 0,更新传当前 version)。
        """
        if prompt_key not in PUBLIC_PROMPT_KEYS:
            raise ValidationError(f"未知 prompt_key: {prompt_key}")
        async with self.session_factory() as s:
            rp = await resolve_prompt(s, tenant_id, kb_id, prompt_key)
        return {"prompt_key": prompt_key, "source": rp.source,
                "version": rp.version, "template_sha": rp.template_sha,
                "template": rp.template}

    async def put_prompt(self, tenant_id: str, kb_id: str, prompt_key: str,
                         template: str, expected_version: int,
                         *, compile_model: str = "") -> dict:
        async with self.layout.version_lock(tenant_id, kb_id):
            return await self._put_prompt_locked(
                tenant_id, kb_id, prompt_key, template, expected_version,
                compile_model=compile_model)

    async def _put_prompt_locked(self, tenant_id: str, kb_id: str, prompt_key: str,
                                 template: str, expected_version: int,
                                 *, compile_model: str = "") -> dict:
        if prompt_key not in PUBLIC_PROMPT_KEYS:
            raise ValidationError(f"未知 prompt_key: {prompt_key}")
        validate_template(prompt_key, template)
        async with self.session_factory() as s:
            previous = await resolve_prompt(s, tenant_id, kb_id, prompt_key)
            row = await PromptRepo().upsert(s, tenant_id=tenant_id, kb_id=kb_id,
                                            prompt_key=prompt_key, template=template,
                                            expected_version=expected_version)
            task, created = (None, False)
            if previous.template.strip() != template.strip():
                task, created = await self._queue_compile_after_prompt_edit(
                    s, tenant_id, kb_id, compile_model)
            await s.commit()
        if created and self.executor is not None:
            self.executor.submit(task)
        return {"prompt_key": prompt_key, "version": row.version,
                "template_sha": template_sha(row.template),
                "compile_task_id": task.task_id if task is not None else None}

    async def delete_prompt(self, tenant_id: str, kb_id: str, prompt_key: str,
                            *, compile_model: str = "") -> None:
        async with self.layout.version_lock(tenant_id, kb_id):
            await self._delete_prompt_locked(tenant_id, kb_id, prompt_key,
                                             compile_model=compile_model)

    async def _delete_prompt_locked(self, tenant_id: str, kb_id: str, prompt_key: str,
                                    *, compile_model: str = "") -> None:
        if prompt_key not in PUBLIC_PROMPT_KEYS:
            raise ValidationError(f"未知 prompt_key: {prompt_key}")
        async with self.session_factory() as s:
            previous = await resolve_prompt(s, tenant_id, kb_id, prompt_key)
            await PromptRepo().delete(s, tenant_id, kb_id, prompt_key)
            task, created = (None, False)
            if previous.template.strip():
                task, created = await self._queue_compile_after_prompt_edit(
                    s, tenant_id, kb_id, compile_model)
            await s.commit()
        if created and self.executor is not None:
            self.executor.submit(task)

    async def _queue_compile_after_prompt_edit(self, session, tenant_id: str,
                                               kb_id: str,
                                               compile_model: str = "") -> tuple[TaskRow | None, bool]:
        """确保该 KB 有一条排队中的编译任务（prompt/模型变化后调用）。

        [jonex] P3b（方案 B）：``compile_model`` 是本次请求带来的模型快照。
        已有排队任务时**刷新**它的快照（还没开跑 ⇒ 换模型该生效）；复用而不刷新
        会出现「平台刚保存了新模型、这条 queued 任务仍用旧模型编完」。
        """
        if not await DocumentRepo().list_parsed_for_compile(session, tenant_id, kb_id):
            return None, False
        queued = await TaskRepo().queued_compile_for_kb(session, tenant_id, kb_id)
        if queued is not None:
            TaskRepo.restamp_compile_model(queued, compile_model)
            return queued, False
        task = await TaskRepo().create(
            session, tenant_id=tenant_id, kb_id=kb_id, kind=TaskKind.compile,
            compile_model=compile_model)
        return task, True
