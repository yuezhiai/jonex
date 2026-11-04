"""Independent page text for targeted corroboration, never an invented PDF cell grid."""
import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path


def extract_text_layer(original: Path, output_dir: Path, *, max_pages=128, max_bytes=16 * 1024 * 1024):
    with original.open("rb") as stream:
        original_sha = hashlib.file_digest(stream, "sha256").hexdigest()
    report = {"schema": "wiki.pdf_text_layer.v1", "processor": "poppler.pdftotext.layout",
        "original_sha256": original_sha, "pages": [], "gaps": []}
    executable = shutil.which("pdftotext")
    if not executable:
        report["gaps"].append("pdf_native_text_tool_unavailable")
    else:
        temporary = output_dir / ".native-pdf-text.tmp"
        try:
            process = subprocess.run([executable, "-layout", "-enc", "UTF-8", "-f", "1", "-l", str(max_pages), str(original), str(temporary)],
                capture_output=True, timeout=60)
            if process.returncode or not temporary.is_file():
                report["gaps"].append("pdf_native_text_extraction_failed")
            elif temporary.stat().st_size > max_bytes:
                report["gaps"].append("pdf_native_text_byte_budget_exceeded")
            else:
                pages = temporary.read_text("utf-8").split("\f")
                if pages and not pages[-1].strip():pages.pop()
                for number, text in enumerate(pages, 1):
                    report["pages"].append({"page_number": number, "text": text,
                        "text_sha256": hashlib.sha256(text.encode()).hexdigest(), "characters": len(text)})
                if not any(p["text"].strip() for p in report["pages"]):
                    report["gaps"].append("pdf_has_no_usable_native_text; OCR_values_remain_unverified")
                if len(pages) >= max_pages:report["gaps"].append("pdf_native_text_page_budget_reached")
        except (OSError, subprocess.SubprocessError, UnicodeError):
            report["gaps"].append("pdf_native_text_extraction_unavailable")
        finally:
            temporary.unlink(missing_ok=True)
    (output_dir / "pdf-text-layer.json").write_text(json.dumps(report, ensure_ascii=False), "utf-8")
    return report
