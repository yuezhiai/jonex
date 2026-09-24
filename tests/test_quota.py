"""Unit tests for jonex_core.common.quota — file classification and config validation.

These tests exercise the pure-function surface of the quota module without
requiring any external infrastructure (Redis, PostgreSQL, etc.).
"""
import json
import os

import pytest

from jonex_core.common.quota import (
    QuotaConfigError,
    SystemQuota,
    classify_quota_category,
    load_system_quota,
    size_limit_key,
)


# ── classify_quota_category ─────────────────────────────────────────


class TestClassifyQuotaCategory:
    """Verify MIME / extension → quota-category mapping."""

    # -- Image MIME prefix --
    def test_image_mime(self):
        assert classify_quota_category("image/png", "file.bin") == "image"
        assert classify_quota_category("image/jpeg", "") == "image"

    # -- Audio MIME prefix --
    def test_audio_mime(self):
        assert classify_quota_category("audio/mpeg", "song.mp3") == "audio"

    # -- Video MIME prefix --
    def test_video_mime(self):
        assert classify_quota_category("video/mp4", "clip.mp4") == "video"

    # -- Spreadsheet by exact MIME --
    def test_spreadsheet_xlsx_mime(self):
        assert classify_quota_category(
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "data.xlsx",
        ) == "spreadsheet"

    def test_spreadsheet_csv_mime(self):
        assert classify_quota_category("text/csv", "data.csv") == "spreadsheet"

    # -- Document by MIME --
    def test_pdf_mime(self):
        assert classify_quota_category("application/pdf", "report.pdf") == "document"

    def test_text_plain_mime(self):
        assert classify_quota_category("text/plain", "notes.txt") == "document"

    # -- Extension fallback when MIME is empty / generic --
    def test_extension_fallback_docx(self):
        assert classify_quota_category("", "report.docx") == "document"

    def test_extension_fallback_xlsx(self):
        assert classify_quota_category("application/octet-stream", "data.xlsx") == "spreadsheet"

    def test_extension_fallback_mp3(self):
        assert classify_quota_category("", "song.mp3") == "audio"

    def test_extension_fallback_mp4(self):
        assert classify_quota_category("", "clip.mp4") == "video"

    def test_extension_fallback_png(self):
        assert classify_quota_category("", "photo.png") == "image"

    # -- Unknown → other --
    def test_unknown_returns_other(self):
        assert classify_quota_category("", "file.xyz") == "other"

    def test_none_inputs_returns_other(self):
        assert classify_quota_category(None, None) == "other"

    # -- MIME takes priority over extension --
    def test_mime_priority_over_extension(self):
        # MIME says image, extension says document
        assert classify_quota_category("image/png", "report.pdf") == "image"


# ── size_limit_key ───────────────────────────────────────────────────


class TestSizeLimitKey:
    """Verify category → size-quota-key mapping."""

    def test_known_categories(self):
        assert size_limit_key("document") == "documentFileSizeLimitMiB"
        assert size_limit_key("spreadsheet") == "spreadsheetFileSizeLimitMiB"
        assert size_limit_key("image") == "imageFileSizeLimitMiB"
        assert size_limit_key("audio") == "audioFileSizeLimitMiB"
        assert size_limit_key("video") == "videoFileSizeLimitMiB"

    def test_unknown_returns_none(self):
        assert size_limit_key("other") is None
        assert size_limit_key("") is None


# ── load_system_quota ────────────────────────────────────────────────

_VALID_CONFIG = json.dumps({
    "tenantKnowledgeBaseLimit": 10,
    "tenantDocumentLimit": 1000,
    "tenantStorageLimitMiB": 5120,
    "knowledgeBaseDocumentLimit": 200,
    "uploadFileCountLimit": 50,
    "documentFileSizeLimitMiB": 20,
    "spreadsheetFileSizeLimitMiB": 10,
    "imageFileSizeLimitMiB": 5,
    "audioFileSizeLimitMiB": 50,
    "audioDurationLimitMinutes": 30,
    "videoFileSizeLimitMiB": 200,
    "videoDurationLimitMinutes": 10,
})


class TestLoadSystemQuota:
    """Validate configuration parsing and rejection rules."""

    def test_valid_config(self):
        sq = load_system_quota(_VALID_CONFIG)
        assert isinstance(sq, SystemQuota)
        assert sq.get("tenantDocumentLimit") == 1000

    def test_to_bytes_conversion(self):
        sq = load_system_quota(_VALID_CONFIG)
        # tenantStorageLimitMiB = 5120 → 5120 * 1024 * 1024 bytes
        assert sq.to_bytes("tenantStorageLimitMiB") == 5120 * 1024 * 1024

    def test_to_bytes_passthrough_for_count(self):
        sq = load_system_quota(_VALID_CONFIG)
        # count fields should pass through unchanged
        assert sq.to_bytes("tenantDocumentLimit") == 1000

    def test_missing_field_rejected(self):
        incomplete = json.dumps({"tenantKnowledgeBaseLimit": 10})
        with pytest.raises(QuotaConfigError, match="缺少字段"):
            load_system_quota(incomplete)

    def test_invalid_json_rejected(self):
        with pytest.raises(QuotaConfigError, match="不是合法 JSON"):
            load_system_quota("not json{{{")

    def test_non_object_rejected(self):
        with pytest.raises(QuotaConfigError, match="必须为 JSON 对象"):
            load_system_quota("[1, 2, 3]")

    def test_zero_value_rejected(self):
        bad = json.loads(_VALID_CONFIG)
        bad["tenantDocumentLimit"] = 0
        with pytest.raises(QuotaConfigError, match="必须为正整数"):
            load_system_quota(json.dumps(bad))

    def test_negative_value_rejected(self):
        bad = json.loads(_VALID_CONFIG)
        bad["tenantDocumentLimit"] = -5
        with pytest.raises(QuotaConfigError, match="必须为正整数"):
            load_system_quota(json.dumps(bad))

    def test_boolean_value_rejected(self):
        bad = json.loads(_VALID_CONFIG)
        bad["tenantDocumentLimit"] = True
        with pytest.raises(QuotaConfigError, match="必须为正整数"):
            load_system_quota(json.dumps(bad))

    def test_kb_limit_exceeding_tenant_limit_rejected(self):
        bad = json.loads(_VALID_CONFIG)
        bad["knowledgeBaseDocumentLimit"] = 2000
        bad["tenantDocumentLimit"] = 1000
        with pytest.raises(QuotaConfigError, match="不得大于"):
            load_system_quota(json.dumps(bad))

    def test_empty_string_rejected(self):
        with pytest.raises(QuotaConfigError, match="未配置"):
            load_system_quota("")

    def test_none_reads_env_var(self, monkeypatch):
        monkeypatch.setenv("JONEX_SYSTEM_QUOTA_CONFIG", _VALID_CONFIG)
        sq = load_system_quota(None)
        assert sq.get("tenantDocumentLimit") == 1000

    def test_none_missing_env_rejected(self, monkeypatch):
        monkeypatch.delenv("JONEX_SYSTEM_QUOTA_CONFIG", raising=False)
        with pytest.raises(QuotaConfigError, match="未配置"):
            load_system_quota(None)
