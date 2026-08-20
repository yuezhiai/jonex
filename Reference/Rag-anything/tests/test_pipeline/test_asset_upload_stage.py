"""Unit tests for AssetUploadStage.

[jonex] §image-refs P1-2/P1-3（image-reference-chain-execution-plan.md §4.1/§5）：
- 对象键的 image_idx 与 PushChunksStage._collect_multimodal_chunks 写入
  file_source 的 image_idx 必须逐项相等——两者都取自 multimodal_results
  item["index"]（全局 multimodal 枚举，非图片单独计数），[table, image,
  equation, image] 序列中的两图键为 img_1 / img_3 而非 img_0 / img_1；
- gate（RAG_ASSET_UPLOAD_ENABLED）与 jonex_core 缺失时整体跳过；
- best-effort：单张失败只 WARNING，不阻塞其他图片上传与主链路。
"""

import sys
import pytest
from unittest import mock

from raganything.pipeline.base import PipelineContext, PipelineServices
from raganything.pipeline.stages import AssetUploadStage, PushChunksStage


@pytest.fixture(autouse=True)
def _gate_default_on(monkeypatch):
    """默认开启（生产口径），用例内再显式关闭做对照。"""
    monkeypatch.setenv("RAG_ASSET_UPLOAD_ENABLED", "true")


@pytest.fixture
def fake_obj_storage():
    """伪造 jonex_core.common.object_storage，隔离平台侧真实依赖。

    注入 sys.modules 使 AssetUploadStage 内的延迟 import 命中假模块；
    构建键/白名单的真实行为由 tests/unit/test_asset_key.py 覆盖，
    此处只关心调用参数与返回结果。
    """
    fake = mock.MagicMock(name="jonex_core.common.object_storage")
    storage = mock.AsyncMock(name="storage")
    fake.get_object_storage.return_value = storage
    fake.build_asset_key.side_effect = (
        lambda tenant, kb, doc, idx, ext: (
            f"jonex/kb/{tenant}/{kb}/{doc}/assets/img_{idx}.{ext}"
        )
    )
    fake.normalize_asset_ext.side_effect = (
        lambda ext: str(ext).lower() if ext else None
    )
    with mock.patch.dict(
        sys.modules, {"jonex_core.common.object_storage": fake}
    ):
        yield fake, storage


@pytest.fixture
def ctx(tmp_path):
    return PipelineContext(
        file_path=str(tmp_path / "test.pdf"),
        file_name="test.pdf",
        tenant_id="t1",
        kb_id="kb1",
        document_id="doc-1",
    )


@pytest.fixture
def services():
    return PipelineServices(
        config=mock.MagicMock(),
        lightrag=None,
        doc_parser=mock.MagicMock(),
        http_client=None,
        logger=mock.MagicMock(),
    )


def _image_result(idx: int, img_path: str, content_type: str = "image") -> dict:
    return {
        "description": f"desc of item {idx}",
        "content_type": content_type,
        "item_info": {"page_idx": 1},
        "index": idx,
        "original": {"img_path": str(img_path)},
    }


class TestAssetUploadStage:
    @pytest.mark.asyncio
    async def test_uploads_images_with_global_index_keys(
        self, ctx, services, fake_obj_storage, tmp_path
    ):
        """核心对齐用例：[table, image, equation, image] 的全局枚举下，
        两图对象键必须为 img_1 / img_3（复用 item["index"]），
        而非按图片单独计数的 img_0 / img_1。"""
        _, storage = fake_obj_storage
        img1 = tmp_path / "img_1.png"
        img3 = tmp_path / "img_3.jpg"
        img1.write_bytes(b"\x89PNG-fake")
        img3.write_bytes(b"\xff\xd8-fake")

        ctx.multimodal_results = [
            {"description": "表格摘要", "content_type": "table",
             "item_info": {"page_idx": 1}, "index": 0},
            _image_result(1, img1),
            {"description": "公式", "content_type": "equation",
             "item_info": {"page_idx": 1}, "index": 2},
            _image_result(3, img3),
        ]

        stage = AssetUploadStage()
        result = await stage.execute(ctx, services)

        assert result.error is None
        assert result.asset_exts == {1: "png", 3: "jpg"}
        keys = [c.args[0] for c in storage.put_bytes.call_args_list]
        assert keys == [
            "jonex/kb/t1/kb1/doc-1/assets/img_1.png",
            "jonex/kb/t1/kb1/doc-1/assets/img_3.jpg",
        ]
        # content_type 随 ext 透传
        content_types = [
            c.kwargs.get("content_type") for c in storage.put_bytes.call_args_list
        ]
        assert content_types == ["image/png", "image/jpg"]

    @pytest.mark.asyncio
    async def test_asset_keys_align_with_chunk_file_source(
        self, ctx, services, fake_obj_storage, tmp_path
    ):
        """端到端对齐：先跑 AssetUploadStage 拿 asset_exts，再喂给
        _collect_multimodal_chunks——上传键的 (image_idx, ext) 与图片
        chunk file_source 里的 image_idx/aext 必须逐项相等。"""
        _, storage = fake_obj_storage
        img1 = tmp_path / "img_1.png"
        img3 = tmp_path / "img_3.jpg"
        img1.write_bytes(b"png-bytes")
        img3.write_bytes(b"jpg-bytes")

        ctx.multimodal_results = [
            {"description": "表格摘要", "content_type": "table",
             "item_info": {"page_idx": 1}, "index": 0},
            _image_result(1, img1),
            {"description": "公式", "content_type": "equation",
             "item_info": {"page_idx": 1}, "index": 2},
            _image_result(3, img3),
        ]

        stage = AssetUploadStage()
        result = await stage.execute(ctx, services)
        ctx.asset_exts = result.asset_exts or {}

        chunks: list[dict] = []
        PushChunksStage()._collect_multimodal_chunks(
            chunks, ctx.multimodal_results,
            "t1", "kb1", "doc-1", "test.pdf", asset_exts=ctx.asset_exts,
        )
        image_chunks = [c for c in chunks if "image_idx=" in c["file_source"]]
        assert len(image_chunks) == 2  # 仅 2 张图片写 image_idx

        uploaded_keys = [c.args[0] for c in storage.put_bytes.call_args_list]
        fs_pairs = []
        for c in image_chunks:
            fs = c["file_source"]
            idx = int(fs.split("image_idx=")[1].split("|")[0])
            ext = fs.split("aext=")[1].split("|")[0]
            fs_pairs.append((idx, ext))
        assert fs_pairs == [(1, "png"), (3, "jpg")]
        # 键与 file_source 一一对应（排序后比较）
        key_suffixes = sorted(k.rsplit("img_", 1)[1] for k in uploaded_keys)
        assert key_suffixes == sorted(f"{idx}.{ext}" for idx, ext in fs_pairs)

    @pytest.mark.asyncio
    async def test_document_id_empty_falls_back_to_doc_id(
        self, ctx, services, fake_obj_storage, tmp_path
    ):
        """doc 锚点同源兜底：document_id 为空时 key 的 doc 段取 ctx.doc_id，
        与 PushChunksStage 的 `ctx.document_id or ctx.doc_id or ""` 一致，
        检索侧按 file_source 的 doc= 推导 key 才能命中。"""
        _, storage = fake_obj_storage
        img = tmp_path / "img_1.png"
        img.write_bytes(b"bytes")
        ctx.document_id = ""
        ctx.doc_id = "content-hash-abc"
        ctx.multimodal_results = [_image_result(1, img)]

        result = await AssetUploadStage().execute(ctx, services)

        assert result.asset_exts == {1: "png"}
        key = storage.put_bytes.call_args_list[0].args[0]
        assert "/content-hash-abc/assets/img_1.png" in key

    @pytest.mark.asyncio
    async def test_both_doc_anchors_empty_skips_upload(
        self, ctx, services, fake_obj_storage, tmp_path
    ):
        """两侧兜底后仍为空 → 整体跳过上传（空 doc 段的 key 无法回链检索侧，
        上传无意义），仅 WARNING。"""
        _, storage = fake_obj_storage
        img = tmp_path / "img_1.png"
        img.write_bytes(b"bytes")
        ctx.document_id = ""
        ctx.doc_id = ""
        ctx.multimodal_results = [_image_result(1, img)]

        result = await AssetUploadStage().execute(ctx, services)

        storage.put_bytes.assert_not_awaited()
        assert result.asset_exts is None
        assert services.logger.warning.called

    @pytest.mark.asyncio
    async def test_gate_disabled_skips_everything(
        self, ctx, services, fake_obj_storage, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("RAG_ASSET_UPLOAD_ENABLED", "false")
        _, storage = fake_obj_storage
        img = tmp_path / "img_0.png"
        img.write_bytes(b"bytes")
        ctx.multimodal_results = [_image_result(0, img)]

        result = await AssetUploadStage().execute(ctx, services)

        storage.put_bytes.assert_not_awaited()
        assert result.asset_exts is None

    @pytest.mark.asyncio
    async def test_empty_results_noop(self, ctx, services, fake_obj_storage):
        _, storage = fake_obj_storage
        ctx.multimodal_results = []

        result = await AssetUploadStage().execute(ctx, services)

        storage.put_bytes.assert_not_awaited()
        assert result.asset_exts is None

    @pytest.mark.asyncio
    async def test_missing_img_path_skipped(
        self, ctx, services, fake_obj_storage
    ):
        _, storage = fake_obj_storage
        ctx.multimodal_results = [
            {"description": "无文件路径的图片", "content_type": "image",
             "item_info": {"page_idx": 1}, "index": 4},
        ]

        result = await AssetUploadStage().execute(ctx, services)

        storage.put_bytes.assert_not_awaited()
        assert result.asset_exts is None

    @pytest.mark.asyncio
    async def test_single_failure_other_images_uploaded(
        self, ctx, services, fake_obj_storage, tmp_path
    ):
        """best-effort：一张上传失败只 WARNING，另一张照常成功且不阻断。"""
        _, storage = fake_obj_storage
        img1 = tmp_path / "img_1.png"
        img3 = tmp_path / "img_3.png"
        img1.write_bytes(b"ok-bytes")
        img3.write_bytes(b"ok-bytes")
        ctx.multimodal_results = [_image_result(1, img1), _image_result(3, img3)]

        async def _fail(key, *args, **kwargs):
            if "img_1." in key:
                raise OSError("storage down")
            return "ok"

        storage.put_bytes.side_effect = _fail

        result = await AssetUploadStage().execute(ctx, services)

        assert result.asset_exts == {3: "png"}  # 失败的不进结果
        assert storage.put_bytes.await_count == 2
        assert services.logger.warning.called

    @pytest.mark.asyncio
    async def test_jonex_core_unavailable_degrades(
        self, ctx, services, tmp_path, monkeypatch
    ):
        """非平台环境（jonex_core 不可导入）→ 整体跳过，仅 WARNING。"""
        monkeypatch.setitem(
            sys.modules, "jonex_core.common.object_storage", None
        )
        img = tmp_path / "img_0.png"
        img.write_bytes(b"bytes")
        ctx.multimodal_results = [_image_result(0, img)]

        result = await AssetUploadStage().execute(ctx, services)

        assert result.asset_exts is None
        assert services.logger.warning.called
