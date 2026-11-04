"""解析任务：跑 parser → 投影 SourceArtifact → 落 ``gen-N/source_artifacts.jsonl``。

⚠️ 本文件是**编排**，不在 ``identity.PARSE_RULE_SOURCES``（解析规则清单）里 ——
所以它改了不会让历史产物失效。因此：**凡是改动到产物内容/shape 的修改（往
``source_metadata`` 加字段、改序列化、改组装顺序），必须把
``identity.PARSE_OUTPUT_VERSION`` +1**，否则旧产物会被当成新的用。规则见
``identity.py`` 文件头。
"""

import asyncio
import json
import logging
from pathlib import Path
from types import SimpleNamespace

from jonex_wiki.domain.engine.compile.source_prep.blocks import InputRoute
from jonex_wiki.domain.engine.compile.source_prep.chunking import PackingOptions
from jonex_wiki.domain.engine.compile.source_prep.classify import classify_input, validate_content
from jonex_wiki.domain.engine.compile.source_prep.direct_ingest import blocks_from_direct_text
from jonex_wiki.domain.engine.compile.source_prep.inspect import (
    InspectionPolicy, InspectionReport)
from jonex_wiki.domain.engine.compile.source_prep.source_archive import write_source_archive
from jonex_wiki.domain.engine.compile.source_prep.raw_manifest import (
    CONTENT_LIST_FILE, MANIFEST_FILE, PARSE_RESULT_FILE, SOURCE_TEXT_FILE,
    build_manifest, write_manifest)
from jonex_wiki.domain.engine.compile.source_prep.reading_materials import prepare_reading_materials
from jonex_wiki.domain.engine.compile.source_prep.spreadsheet_view import parse_spreadsheet
from jonex_wiki.domain.engine.compile.source_prep.table_artifact import persist_table_observations
from jonex_wiki.domain.parsing.direct import _read_text
from jonex_wiki.domain.parsing.mineru.common import normalize_inline_content_list
from jonex_wiki.kernel.errors import ValidationError
from jonex_wiki.kernel.runtime_env import runtime_environment
from jonex_wiki.storage.layout import Layout
from jonex_wiki.storage.repos import DocumentRepo

log = logging.getLogger(__name__)

# parser_version 随**产物生成逻辑**变化。解析期已恢复 core 的归一化/结构打包/投影，
# 故 direct 保持 core 同款 "2.0.0"；mineru 从 raw-only 的 "+raw1" 回到
# "+structure-pack-2"；spreadsheet_native 沿用 core parse_spreadsheet 的版本号。
_PARSER_VERSIONS = {
    "text_direct_v1": "2.0.0",
    "structured_direct_v1": "2.0.0",
    "mineru.cloud.v1": "2.x+structure-pack-2",
    "spreadsheet.native.ooxml": "1",
}


def _write_artifacts(layout: Layout, tenant_id: str, kb_id: str, document_id: str,
                     generation: int, artifacts: list) -> Path:
    """Write one ``SourceArtifact`` per line as JSONL (compile side reads only)."""
    path = layout.artifacts_path(tenant_id, kb_id, document_id, generation)
    lines = [json.dumps(artifact.model_dump(mode="json"), ensure_ascii=False)
             for artifact in artifacts]
    content = ("\n".join(lines) + "\n").encode("utf-8") if lines else b""
    layout.atomic_write(path, content)
    return path


def create_parse_runner(session_factory, settings, layout: Layout, mineru, executor=None):
    """spec §4.6 编排。关键约束:
    - 解析期间不持 DB session(MinerU 轮询可达 1800s,长持 session 会打满连接池)
    - 失败原子性:首次失败 → status=failed;重解析失败 → 保持 parsed 与旧代次,只写 error
    - 取消检查点:MinerU 轮询之间(core backend 的 parse_raw 在轮询间隔调用 cancel_check)
    - 解析期写 source_artifacts.jsonl + raw manifest;compile source_port 只读 JSONL。
    """

    async def _write_doc_error(tenant_id, kb_id, document_id, error: str, *, cancelled: bool) -> None:
        async with session_factory() as s:
            doc = await DocumentRepo().get(s, tenant_id, kb_id, document_id)
            if doc is None:
                return
            if cancelled or doc.status != "parsed":
                doc.status = "failed"          # 首次解析失败/取消 → failed
                doc.error = error
            else:
                doc.error = error              # 重解析失败:保持 parsed 与旧代次(spec §4.6)
            await s.commit()

    async def runner(row) -> None:
        tenant_id, kb_id = row.tenant_id, row.kb_id
        # 1) 短 session 读文档元数据
        async with session_factory() as s:
            doc = await DocumentRepo().get(s, tenant_id, kb_id, row.document_id)
            if doc is None:
                raise ValidationError(f"文档 {row.document_id} 不存在")
            meta = SimpleNamespace(document_id=doc.document_id, source_name=doc.source_name,
                                   source_sha256=doc.source_sha256, route=doc.route,
                                   parser_id=doc.parser_id, parser_version=doc.parser_version,
                                   content_generation=doc.content_generation)
            await s.commit()
        try:
            # 2) 原样采集(无 session)。分类用 core classify_input;后缀取自 ref.extension
            #    (无合法后缀/未知后缀已在 upload 层以 422 拒绝,此处 fail-closed 再兜底)。
            ref, route = classify_input(meta.source_name)
            suffix = ref.extension
            source_path = layout.upload_path(tenant_id, kb_id, meta.document_id, suffix)
            new_gen = meta.content_generation + 1   # DB 默认 0 → 首次解析写 gen-1
            raw_dir = layout.raw_dir(tenant_id, kb_id, meta.document_id, new_gen)
            raw_dir.mkdir(parents=True, exist_ok=True)
            packing = PackingOptions.from_settings(settings)
            # [jonex] 编译前体检(与平台 ingest 路径同一套判据):单元级缺陷隔离 +
            # 留档,比例超闸门整份失败。见 source_prep/inspect.py / screening.py。
            inspection = InspectionPolicy.from_settings(settings)
            report = InspectionReport(source_name=meta.source_name)

            if route is InputRoute.TEXT_DIRECT or route is InputRoute.STRUCTURED_DIRECT:
                # 二进制嗅探(validate_content 收 path,先写文件再 validate)+ 直读(_read_text)
                await asyncio.to_thread(validate_content, source_path)
                text = await asyncio.to_thread(_read_text, source_path, settings.text_encoding)
                await asyncio.to_thread((raw_dir / SOURCE_TEXT_FILE).write_text, text, "utf-8")
                parser_id = ("text_direct_v1" if route is InputRoute.TEXT_DIRECT
                             else "structured_direct_v1")
                result = await asyncio.to_thread(
                    blocks_from_direct_text,
                    text,
                    suffix=suffix,
                    source_name=meta.source_name,
                    source_id=meta.document_id,
                    document_id=meta.document_id,
                    source_sha256=meta.source_sha256,
                    parser_id=parser_id,
                    parser_version=_PARSER_VERSIONS[parser_id],
                    packing=packing,
                    inspection=inspection, report=report,
                )
            elif route is InputRoute.SPREADSHEET_NATIVE:
                # 确定性表格解析:tables.sqlite + markdown 原文(解析期即产出
                # table_reference 块,含 tables_sqlite_local_path)。
                result = await asyncio.to_thread(
                    parse_spreadsheet, source_path, output_dir=raw_dir,
                    source_id=meta.document_id, document_id=meta.document_id)
                await asyncio.to_thread(
                    (raw_dir / PARSE_RESULT_FILE).write_text,
                    json.dumps(result.to_dict(), ensure_ascii=False), "utf-8")
                await asyncio.to_thread((raw_dir / SOURCE_TEXT_FILE).write_text,
                                        result.markdown, "utf-8")
                parser_id = "spreadsheet.native.ooxml"
            else:
                if mineru is None:
                    raise ValidationError("MinerU 客户端未配置")
                cancel_check = (lambda: executor.check_cancel(row.task_id)) if executor else None
                with runtime_environment(settings.runtime_env()):
                    items = await asyncio.to_thread(
                        mineru.parse_raw, source_path,
                        output_dir=raw_dir / "_mineru",
                        source_id=meta.document_id, document_id=meta.document_id,
                        cancel_check=cancel_check)
                await asyncio.to_thread((raw_dir / CONTENT_LIST_FILE).write_text,
                                        json.dumps(items, ensure_ascii=False), "utf-8")
                parser_id = "mineru.cloud.v1"
                result = await asyncio.to_thread(
                    normalize_inline_content_list, items, output_dir=raw_dir,
                    source_name=meta.source_name, parser_id=parser_id,
                    source_sha256=meta.source_sha256, source_id=meta.document_id,
                    document_id=meta.document_id, markdown="", packing=packing,
                    inspection=inspection, report=report)
                # 表资产后处理:把 table_observation 落成 tables.sqlite,并给
                # 无法可靠展开为网格的表打 ambiguous(只允许原始阅读,禁止结构化
                # 计算)。必须在 prepare_reading_materials 之前,后者依赖
                # tables_sqlite_local_path 元数据。
                table_dir = layout.gen_dir(tenant_id, kb_id, meta.document_id, new_gen) / "tables"
                await asyncio.to_thread(persist_table_observations, result, table_dir)

            # 3) 解析后处理:source-level reading_materials 目录 → 首条 artifact 的
            #    source_metadata.source_reading_materials;投影为 SourceArtifact 并落 JSONL。
            parser_version = result.parser_version or _PARSER_VERSIONS[parser_id]
            result.reading_materials = await asyncio.to_thread(prepare_reading_materials, result)
            artifacts = result.to_source_artifacts(content_generation=new_gen)
            from jonex_wiki.domain.parsing.identity import (
                parse_input_components, parse_input_signature)
            parse_signature = parse_input_signature(settings)
            # 分量摘要一并落盘：产物过期时 parse_status 才能说清「哪一项变了」，
            # 而不是笼统的「解析规则已变化」。
            parse_components = parse_input_components(settings)
            for artifact in artifacts:
                artifact.source_metadata["parse_input_signature"] = parse_signature
                artifact.source_metadata["parse_input_components"] = parse_components
            await asyncio.to_thread(_write_artifacts, layout, tenant_id, kb_id,
                                    meta.document_id, new_gen, artifacts)

            # 3.5) 源侧留档:被体检摘出编译面的原文切片落 gen-N/source-archive/。
            archive_text = result.markdown or ""
            if archive_text:
                archive_path = await asyncio.to_thread(
                    write_source_archive,
                    layout.gen_dir(tenant_id, kb_id, meta.document_id, new_gen),
                    report, archive_text, source_name=meta.source_name)
                if archive_path is not None:
                    log.warning("源侧体检摘除 %d 个单元(%d 字符):%s",
                                len(report.defects), report.isolated_chars,
                                archive_path)

            # 4) raw manifest 仍写 raw 目录(原始采集可归因)。
            manifest = build_manifest(
                document_id=meta.document_id, source_name=meta.source_name,
                source_sha256=meta.source_sha256, route=route.value, parser_id=parser_id,
                parser_version=parser_version, content_generation=new_gen)
            await asyncio.to_thread(write_manifest, raw_dir / MANIFEST_FILE, manifest)
            log.info("解析完成 %s: route=%s blocks=%d gen=%d parser=%s@%s",
                     meta.document_id, route.value, len(result.blocks), new_gen,
                     parser_id, parser_version)

            # Serialize the successful generation switch and follow-up task
            # against publication's final input check.
            async with layout.version_lock(tenant_id, kb_id):
                # 5) 短 session 写 DB(dev 模型无 chunk_count/char_count 列)。
                async with session_factory() as s:
                    doc2 = await DocumentRepo().get(s, tenant_id, kb_id, meta.document_id)
                    doc2.content_generation = new_gen
                    doc2.status = "parsed"
                    doc2.error = None
                    doc2.parser_id = parser_id
                    doc2.parser_version = parser_version
                    await s.commit()
                # 6) 自动触发编译。queued 可合并；已有 compiling 的输入快照
                #    已固定，必须另排一个 follow-up。
                if executor is not None:
                    from sqlalchemy import select
                    from jonex_wiki.kernel.task import TaskKind
                    from jonex_wiki.storage.models import TaskRow
                    from jonex_wiki.storage.repos import TaskRepo
                    async with session_factory() as s:
                        existing = (await s.execute(select(TaskRow).where(
                            TaskRow.tenant_id == tenant_id, TaskRow.kb_id == kb_id,
                            TaskRow.kind == TaskKind.compile.value,
                            TaskRow.status == "queued"))).scalars().first()
                        if existing is None:
                            # [jonex] P3b（方案 B）：解析任务带着编译模型下来
                            # （平台发起解析时给的值），原样沿给编译任务；没带就是空
                            # ⇒ 编译走部署 env。
                            task = await TaskRepo().create(
                                s, tenant_id=tenant_id, kb_id=kb_id, kind=TaskKind.compile,
                                compile_model=str((row.summary or {}).get("compile_model") or ""))
                            await s.commit()
                            executor.submit(task)
                        else:
                            await s.commit()
        except asyncio.CancelledError:
            await _write_doc_error(tenant_id, kb_id, meta.document_id, "cancelled", cancelled=True)
            raise
        except Exception as e:  # noqa: BLE001
            await _write_doc_error(tenant_id, kb_id, meta.document_id,
                                   f"{type(e).__name__}: {e}"[:512], cancelled=False)
            raise  # executor 转 task failed
    return runner
