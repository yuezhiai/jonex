"""MinerU parsers — local CLI and online API."""

import base64  # [jonex] MineruSelfHostParser._dump_images
import copy
import hashlib
import http.client
import json
import logging
import os
import platform
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import shutil
import zipfile
from pathlib import Path
from queue import Empty, Queue
from typing import Any, Dict, List, Optional, Tuple, Union

from raganything.asset_urls import attach_public_media_urls
from raganything.parsers.base import Parser
from raganything.parsers.conversion import convert_office_to_pdf, convert_text_to_pdf

try:
    from PIL import Image
except ImportError:
    Image = None  # type: ignore[assignment]

_IS_WINDOWS = platform.system() == "Windows"


class MineruExecutionError(Exception):
    """catch mineru error"""

    def __init__(self, return_code, error_msg):
        self.return_code = return_code
        self.error_msg = error_msg
        super().__init__(
            f"Mineru command failed with return code {return_code}: {error_msg}"
        )


class MineruParser(Parser):
    """MinerU 2.0 document parsing utility class.

    Supports parsing PDF and image documents, converting the content into structured data
    and generating markdown and JSON output.

    Note: Office documents are no longer directly supported. Please convert them to PDF first.
    """

    __slots__ = ()
    logger = logging.getLogger(__name__)

    # [jonex] 字段别名（供 _read_output_files / _read_any_output_files /
    # 子类 MineruSelfHostParser._build_content_list 共享）。
    _FIELD_ALIASES = {
        "img_caption": "image_caption",
        "img_footnote": "image_footnote",
    }

    def __init__(self) -> None:
        super().__init__()

    @classmethod
    def _run_mineru_command(
        cls,
        input_path: Union[str, Path],
        output_dir: Union[str, Path],
        method: str = "auto",
        lang: Optional[str] = None,
        backend: Optional[str] = None,
        start_page: Optional[int] = None,
        end_page: Optional[int] = None,
        formula: bool = True,
        table: bool = True,
        device: Optional[str] = None,
        source: Optional[str] = None,
        vlm_url: Optional[str] = None,
        timeout: Optional[int] = None,
        **kwargs,
    ) -> None:
        cmd = [
            "mineru",
            "-p",
            str(input_path),
            "-o",
            str(output_dir),
            "-m",
            method,
        ]
        if backend:
            cmd.extend(["-b", backend])
        if source is None:
            source = os.environ.get("MINERU_SOURCE")
        if source:
            cmd.extend(["--source", source])
        if lang:
            cmd.extend(["-l", lang])
        if start_page is not None:
            cmd.extend(["-s", str(start_page)])
        if end_page is not None:
            cmd.extend(["-e", str(end_page)])
        if not formula:
            cmd.extend(["-f", "false"])
        if not table:
            cmd.extend(["-t", "false"])
        if device:
            cmd.extend(["-d", device])
        if vlm_url:
            cmd.extend(["-u", vlm_url])

        output_lines = []
        error_lines = []
        custom_env = kwargs.pop("env", None)

        if custom_env is not None:
            if not isinstance(custom_env, dict):
                raise TypeError(
                    f"env must be a dictionary, got {type(custom_env).__name__}"
                )
            for k, v in custom_env.items():
                if not isinstance(k, str) or not isinstance(v, str):
                    raise TypeError("env keys and values must be strings")

        if kwargs:
            unsupported = ", ".join(kwargs.keys())
            raise TypeError(
                f"MineruParser._run_mineru_command received unexpected keyword argument(s): {unsupported}"
            )

        try:
            cls.logger.info(f"Executing mineru command: {' '.join(cmd)}")

            env = None
            if custom_env:
                env = os.environ.copy()
                env.update(custom_env)

            subprocess_kwargs = {
                "stdout": subprocess.PIPE,
                "stderr": subprocess.PIPE,
                "text": True,
                "encoding": "utf-8",
                "errors": "ignore",
                "bufsize": 1,
                "env": env,
            }
            if _IS_WINDOWS:
                subprocess_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

            def enqueue_output(pipe, queue, prefix):
                try:
                    for line in iter(pipe.readline, ""):
                        if line.strip():
                            queue.put((prefix, line.strip()))
                    pipe.close()
                except Exception as e:
                    queue.put((prefix, f"Error reading {prefix}: {e}"))

            process = subprocess.Popen(cmd, **subprocess_kwargs)
            stdout_queue = Queue()
            stderr_queue = Queue()
            stdout_thread = threading.Thread(
                target=enqueue_output, args=(process.stdout, stdout_queue, "STDOUT")
            )
            stderr_thread = threading.Thread(
                target=enqueue_output, args=(process.stderr, stderr_queue, "STDERR")
            )
            stdout_thread.daemon = True
            stderr_thread.daemon = True
            stdout_thread.start()
            stderr_thread.start()

            start_time = time.monotonic()
            while process.poll() is None:
                try:
                    while True:
                        prefix, line = stdout_queue.get_nowait()
                        output_lines.append(line)
                        cls.logger.info(f"[MinerU] {line}")
                except Empty:
                    pass
                try:
                    while True:
                        prefix, line = stderr_queue.get_nowait()
                        if "warning" in line.lower():
                            cls.logger.warning(f"[MinerU] {line}")
                        elif "error" in line.lower():
                            cls.logger.error(f"[MinerU] {line}")
                            error_message = line.split("\n")[0]
                            error_lines.append(error_message)
                        else:
                            cls.logger.info(f"[MinerU] {line}")
                except Empty:
                    pass

                if timeout is not None and (time.monotonic() - start_time) > timeout:
                    process.kill()
                    process.wait()
                    stdout_thread.join(timeout=1)
                    stderr_thread.join(timeout=1)
                    raise TimeoutError(
                        f"MinerU did not finish within {timeout}s. "
                        "This often means a model download is stuck due to network issues. "
                        "Check your internet connection or pre-download the required models."
                    )
                time.sleep(0.1)

            try:
                while True:
                    prefix, line = stdout_queue.get_nowait()
                    output_lines.append(line)
                    cls.logger.info(f"[MinerU] {line}")
            except Empty:
                pass
            try:
                while True:
                    prefix, line = stderr_queue.get_nowait()
                    if "warning" in line.lower():
                        cls.logger.warning(f"[MinerU] {line}")
                    elif "error" in line.lower():
                        cls.logger.error(f"[MinerU] {line}")
                        error_message = line.split("\n")[0]
                        error_lines.append(error_message)
                    else:
                        cls.logger.info(f"[MinerU] {line}")
            except Empty:
                pass

            return_code = process.wait()
            stdout_thread.join(timeout=5)
            stderr_thread.join(timeout=5)

            if return_code != 0 or error_lines:
                cls.logger.info("[MinerU] Command executed failed")
                raise MineruExecutionError(return_code, error_lines)
            else:
                cls.logger.info("[MinerU] Command executed successfully")

        except MineruExecutionError:
            raise
        except subprocess.CalledProcessError as e:
            cls.logger.error(f"Error running mineru subprocess command: {e}")
            cls.logger.error(f"Command: {' '.join(cmd)}")
            cls.logger.error(f"Return code: {e.returncode}")
            raise
        except FileNotFoundError:
            raise RuntimeError(
                "mineru command not found. Please ensure MinerU 2.0 is properly installed:\n"
                "pip install -U 'mineru[core]' or uv pip install -U 'mineru[core]'"
            )
        except Exception as e:
            error_message = f"Unexpected error running mineru command: {e}"
            cls.logger.error(error_message)
            raise RuntimeError(error_message) from e

    @classmethod
    def _read_output_files(
        cls, output_dir: Path, file_stem: str, method: str = "auto"
    ) -> Tuple[List[Dict[str, Any]], str]:
        md_file = output_dir / f"{file_stem}.md"
        json_file = output_dir / f"{file_stem}_content_list.json"
        images_base_dir = output_dir

        file_stem_subdir = output_dir / file_stem
        if file_stem_subdir.is_dir():
            found = False
            for subdir in file_stem_subdir.iterdir():
                if not subdir.is_dir():
                    continue
                candidate_json = subdir / f"{file_stem}_content_list.json"
                if candidate_json.exists():
                    md_file = subdir / f"{file_stem}.md"
                    json_file = candidate_json
                    images_base_dir = subdir
                    found = True
                    cls.logger.info(
                        f"Found MinerU output in subdirectory: {subdir.name}"
                    )
                    break

            if not found:
                cls.logger.debug(
                    f"No output found by scanning, falling back to method-based path: {method}"
                )
                md_file = file_stem_subdir / method / f"{file_stem}.md"
                json_file = file_stem_subdir / method / f"{file_stem}_content_list.json"
                images_base_dir = file_stem_subdir / method

        md_content = ""
        if md_file.exists():
            try:
                with open(md_file, "r", encoding="utf-8") as f:
                    md_content = f.read()
            except Exception as e:
                cls.logger.warning(f"Could not read markdown file {md_file}: {e}")

        content_list = []
        if json_file.exists():
            try:
                with open(json_file, "r", encoding="utf-8") as f:
                    content_list = json.load(f)

                for item in content_list:
                    if isinstance(item, dict):
                        for old_name, new_name in cls._FIELD_ALIASES.items():
                            if old_name in item and new_name not in item:
                                item[new_name] = item[old_name]
                            elif new_name in item and old_name not in item:
                                item[old_name] = item[new_name]

                cls.logger.info(
                    f"Fixing image paths in {json_file} with base directory: {images_base_dir}"
                )
                for item in content_list:
                    if isinstance(item, dict):
                        for field_name in [
                            "img_path",
                            "table_img_path",
                            "equation_img_path",
                        ]:
                            if field_name in item and item[field_name]:
                                img_path = item[field_name]
                                absolute_img_path = (
                                    images_base_dir / img_path
                                ).resolve()
                                resolved_base = images_base_dir.resolve()
                                if not absolute_img_path.is_relative_to(resolved_base):
                                    cls.logger.warning(
                                        f"Potential path traversal detected in {field_name}: {img_path}. Skipping."
                                    )
                                    item[field_name] = ""
                                    continue
                                item[field_name] = str(absolute_img_path)

                        attach_public_media_urls(item)

            except Exception as e:
                cls.logger.warning(f"Could not read JSON file {json_file}: {e}")

        return content_list, md_content

    def parse_pdf(
        self,
        pdf_path: Union[str, Path],
        output_dir: Optional[str] = None,
        method: str = "auto",
        lang: Optional[str] = None,
        **kwargs,
    ) -> List[Dict[str, Any]]:
        try:
            pdf_path = Path(pdf_path)
            if not pdf_path.exists():
                raise FileNotFoundError(f"PDF file does not exist: {pdf_path}")

            name_without_suff = pdf_path.stem
            if output_dir:
                base_output_dir = self._unique_output_dir(output_dir, pdf_path)
            else:
                base_output_dir = pdf_path.parent / "mineru_output"
            base_output_dir.mkdir(parents=True, exist_ok=True)

            self._run_mineru_command(
                input_path=pdf_path,
                output_dir=base_output_dir,
                method=method,
                lang=lang,
                **kwargs,
            )

            backend = kwargs.get("backend") or ""
            if backend.startswith("vlm-"):
                method = "vlm"
            elif backend.startswith("hybrid-"):
                method = "hybrid_auto"

            content_list, _ = self._read_output_files(
                base_output_dir, name_without_suff, method=method
            )
            return content_list

        except MineruExecutionError:
            raise
        except Exception as e:
            self.logger.error(f"Error in parse_pdf: {str(e)}")
            raise

    def parse_image(
        self,
        image_path: Union[str, Path],
        output_dir: Optional[str] = None,
        lang: Optional[str] = None,
        **kwargs,
    ) -> List[Dict[str, Any]]:
        try:
            image_path = Path(image_path)
            if not image_path.exists():
                raise FileNotFoundError(f"Image file does not exist: {image_path}")

            mineru_supported_formats = {".png", ".jpeg", ".jpg"}
            all_supported_formats = {
                ".png", ".jpeg", ".jpg", ".bmp", ".tiff", ".tif", ".gif", ".webp",
            }
            ext = image_path.suffix.lower()
            if ext not in all_supported_formats:
                raise ValueError(
                    f"Unsupported image format: {ext}. Supported formats: {', '.join(all_supported_formats)}"
                )

            actual_image_path = image_path
            temp_converted_file = None

            if ext not in mineru_supported_formats:
                self.logger.info(
                    f"Converting {ext} image to PNG for MinerU compatibility..."
                )
                if Image is None:
                    raise RuntimeError(
                        "PIL/Pillow is required for image format conversion. "
                        "Please install it using: pip install Pillow"
                    )

                temp_dir = Path(tempfile.mkdtemp())
                temp_converted_file = temp_dir / f"{image_path.stem}_converted.png"

                try:
                    with Image.open(image_path) as img:
                        if img.mode in ("RGBA", "LA", "P"):
                            if img.mode == "P":
                                img = img.convert("RGBA")
                            background = Image.new("RGB", img.size, (255, 255, 255))
                            if img.mode == "RGBA":
                                background.paste(img, mask=img.split()[-1])
                            else:
                                background.paste(img)
                            img = background
                        elif img.mode not in ("RGB", "L"):
                            img = img.convert("RGB")
                        img.save(temp_converted_file, "PNG", optimize=True)
                        self.logger.info(
                            f"Successfully converted {image_path.name} to PNG "
                            f"({temp_converted_file.stat().st_size / 1024:.1f} KB)"
                        )
                        actual_image_path = temp_converted_file

                except Exception as e:
                    if temp_converted_file and temp_converted_file.exists():
                        temp_converted_file.unlink()
                    raise RuntimeError(
                        f"Failed to convert image {image_path.name}: {str(e)}"
                    )

            name_without_suff = image_path.stem
            if output_dir:
                base_output_dir = self._unique_output_dir(output_dir, image_path)
            else:
                base_output_dir = image_path.parent / "mineru_output"
            base_output_dir.mkdir(parents=True, exist_ok=True)

            try:
                self._run_mineru_command(
                    input_path=actual_image_path,
                    output_dir=base_output_dir,
                    method="ocr",
                    lang=lang,
                    **kwargs,
                )
                content_list, _ = self._read_output_files(
                    base_output_dir, name_without_suff, method="ocr"
                )
                return content_list
            except MineruExecutionError:
                raise
            finally:
                if temp_converted_file and temp_converted_file.exists():
                    try:
                        temp_converted_file.unlink()
                        temp_converted_file.parent.rmdir()
                    except Exception:
                        pass

        except Exception as e:
            self.logger.error(f"Error in parse_image: {str(e)}")
            raise

    def parse_office_doc(
        self,
        doc_path: Union[str, Path],
        output_dir: Optional[str] = None,
        lang: Optional[str] = None,
        **kwargs,
    ) -> List[Dict[str, Any]]:
        try:
            pdf_path = convert_office_to_pdf(doc_path, output_dir)
            return self.parse_pdf(
                pdf_path=pdf_path, output_dir=output_dir, lang=lang, **kwargs
            )
        except Exception as e:
            self.logger.error(f"Error in parse_office_doc: {str(e)}")
            raise

    def parse_text_file(
        self,
        text_path: Union[str, Path],
        output_dir: Optional[str] = None,
        lang: Optional[str] = None,
        **kwargs,
    ) -> List[Dict[str, Any]]:
        try:
            pdf_path = convert_text_to_pdf(text_path, output_dir)
            return self.parse_pdf(
                pdf_path=pdf_path, output_dir=output_dir, lang=lang, **kwargs
            )
        except Exception as e:
            self.logger.error(f"Error in parse_text_file: {str(e)}")
            raise

    def parse_document(
        self,
        file_path: Union[str, Path],
        method: str = "auto",
        output_dir: Optional[str] = None,
        lang: Optional[str] = None,
        **kwargs,
    ) -> List[Dict[str, Any]]:
        file_path = Path(file_path)
        if not file_path.exists():
            raise FileNotFoundError(f"File does not exist: {file_path}")

        ext = file_path.suffix.lower()
        if ext == ".pdf":
            return self.parse_pdf(file_path, output_dir, method, lang, **kwargs)
        elif ext in self.IMAGE_FORMATS:
            return self.parse_image(file_path, output_dir, lang, **kwargs)
        elif ext in self.OFFICE_FORMATS:
            self.logger.warning(
                f"Warning: Office document detected ({ext}). "
                f"MinerU 2.0 requires conversion to PDF first."
            )
            return self.parse_office_doc(file_path, output_dir, lang, **kwargs)
        elif ext in self.TEXT_FORMATS:
            return self.parse_text_file(file_path, output_dir, lang, **kwargs)
        else:
            self.logger.warning(
                f"Warning: Unsupported file extension '{ext}', "
                f"attempting to parse as PDF"
            )
            return self.parse_pdf(file_path, output_dir, method, lang, **kwargs)

    def check_installation(self) -> bool:
        try:
            subprocess_kwargs = {
                "capture_output": True,
                "text": True,
                "check": True,
                "encoding": "utf-8",
                "errors": "ignore",
            }
            if _IS_WINDOWS:
                subprocess_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
            result = subprocess.run(["mineru", "--version"], **subprocess_kwargs)
            self.logger.debug(f"MinerU version: {result.stdout.strip()}")
            return True
        except (subprocess.CalledProcessError, FileNotFoundError):
            self.logger.debug(
                "MinerU 2.0 is not properly installed. "
                "Please install it using: pip install -U 'mineru[core]'"
            )
            return False


class MineruOnlineParser(MineruParser):
    """MinerU online API parser.

    This parser uses MinerU's token-protected v4 Precision Extract API instead of
    the local ``mineru`` command. It uploads local files through signed URLs,
    polls the batch result endpoint, downloads the result zip, and then reuses
    the same content-list normalization as the local MinerU parser.
    """

    logger = logging.getLogger(__name__)

    API_BASE_ENV = "MINERU_API_BASE_URL"
    API_TOKEN_ENV = "MINERU_API_TOKEN"
    MODEL_VERSION_ENV = "MINERU_MODEL_VERSION"
    POLL_INTERVAL_ENV = "MINERU_POLL_INTERVAL"
    POLL_TIMEOUT_ENV = "MINERU_POLL_TIMEOUT"

    def __init__(self) -> None:
        super().__init__()
        self.api_base_url = os.environ.get(
            self.API_BASE_ENV, "https://mineru.net"
        ).rstrip("/")
        self.api_token = os.environ.get(self.API_TOKEN_ENV, "").strip()
        self.model_version = os.environ.get(self.MODEL_VERSION_ENV, "vlm").strip()
        self.poll_interval = float(os.environ.get(self.POLL_INTERVAL_ENV, "5"))
        self.poll_timeout = int(os.environ.get(self.POLL_TIMEOUT_ENV, "1800"))

    def configure(self, **kwargs) -> None:
        """Override API settings from config (e.g. preset YAML or env vars).

        Priority: explicit kwargs > environment variables > defaults.
        This allows per-task token switching without restarting the service.

        Accepts:
            mineru_api_token, mineru_api_base_url, mineru_model_version,
            mineru_poll_interval, mineru_poll_timeout
        """
        if kwargs.get("mineru_api_token"):
            self.api_token = kwargs["mineru_api_token"]
        if kwargs.get("mineru_api_base_url"):
            self.api_base_url = kwargs["mineru_api_base_url"].rstrip("/")
        if kwargs.get("mineru_model_version"):
            self.model_version = kwargs["mineru_model_version"]
        if kwargs.get("mineru_poll_interval"):
            self.poll_interval = float(kwargs["mineru_poll_interval"])
        if kwargs.get("mineru_poll_timeout"):
            self.poll_timeout = int(kwargs["mineru_poll_timeout"])

    def _authorization_headers(self) -> Dict[str, str]:
        if not self.api_token:
            raise RuntimeError(
                f"{self.API_TOKEN_ENV} is required when parser='mineru_online'. "
                "Create a MinerU API token on mineru.net and set it as an "
                "environment variable."
            )
        return {"Authorization": f"Bearer {self.api_token}"}

    def _api_json_request(
        self,
        method: str,
        path: str,
        payload: Optional[Dict[str, Any]] = None,
        timeout: int = 60,
    ) -> Dict[str, Any]:
        url = f"{self.api_base_url}{path}"
        data = None
        headers = {"Accept": "*/*", **self._authorization_headers()}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"

        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                body = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="ignore")
            raise RuntimeError(
                f"MinerU API request failed: {method} {path} "
                f"HTTP {exc.code}: {body}"
            ) from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(
                f"MinerU API request failed: {method} {path}: {exc}"
            ) from exc

        try:
            result = json.loads(body)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"MinerU API returned non-JSON response for {method} {path}: {body[:500]}"
            ) from exc

        if result.get("code") != 0:
            raise RuntimeError(
                f"MinerU API returned error for {method} {path}: "
                f"{result.get('msg') or result}"
            )
        return result

    def _request_upload_url(
        self,
        file_path: Path,
        method: str,
        lang: Optional[str],
        **kwargs,
    ) -> Tuple[str, str]:
        data_id = kwargs.get("data_id") or hashlib.md5(
            str(file_path.resolve()).encode("utf-8")
        ).hexdigest()

        file_payload: Dict[str, Any] = {
            "name": file_path.name,
            "data_id": data_id,
        }
        if method == "ocr":
            file_payload["is_ocr"] = True
        elif method == "txt":
            file_payload["is_ocr"] = False

        page_ranges = kwargs.get("page_ranges") or kwargs.get("page_range")
        if page_ranges:
            file_payload["page_ranges"] = str(page_ranges)
        else:
            start_page = kwargs.get("start_page")
            end_page = kwargs.get("end_page")
            if start_page is not None or end_page is not None:
                start = 1 if start_page is None else int(start_page) + 1
                end = "" if end_page is None else str(int(end_page) + 1)
                file_payload["page_ranges"] = f"{start}-{end}" if end else str(start)

        model_version = kwargs.get("model_version") or self.model_version
        if file_path.suffix.lower() in {".html", ".htm", ".xhtml"}:
            model_version = "MinerU-HTML"

        payload: Dict[str, Any] = {
            "files": [file_payload],
            "model_version": model_version,
            "enable_formula": bool(kwargs.get("formula", True)),
            "enable_table": bool(kwargs.get("table", True)),
        }
        if lang:
            payload["language"] = lang
        if kwargs.get("callback"):
            payload["callback"] = kwargs["callback"]
        if kwargs.get("seed"):
            payload["seed"] = kwargs["seed"]
        if kwargs.get("extra_formats"):
            payload["extra_formats"] = kwargs["extra_formats"]
        if kwargs.get("no_cache") is not None:
            payload["no_cache"] = bool(kwargs["no_cache"])
        if kwargs.get("cache_tolerance") is not None:
            payload["cache_tolerance"] = int(kwargs["cache_tolerance"])

        result = self._api_json_request("POST", "/api/v4/file-urls/batch", payload)
        data = result.get("data") or {}
        batch_id = data.get("batch_id")
        file_urls = data.get("file_urls") or []
        if not batch_id or not file_urls:
            raise RuntimeError(f"MinerU API did not return upload URL: {result}")
        return batch_id, file_urls[0]

    def _upload_file(self, upload_url: str, file_path: Path) -> None:
        with open(file_path, "rb") as f:
            data = f.read()

        parsed = urllib.parse.urlparse(upload_url)
        path = urllib.parse.urlunparse(("", "", parsed.path or "/", "", parsed.query, ""))
        port = parsed.port
        if parsed.scheme == "https":
            conn = http.client.HTTPSConnection(parsed.hostname, port, timeout=300)
        elif parsed.scheme == "http":
            conn = http.client.HTTPConnection(parsed.hostname, port, timeout=300)
        else:
            raise RuntimeError(f"Unsupported MinerU signed upload URL: {upload_url}")

        try:
            conn.request("PUT", path, body=data, headers={"Content-Length": str(len(data))})
            response = conn.getresponse()
            status = response.status
            body = response.read().decode("utf-8", errors="ignore")
        except (OSError, http.client.HTTPException) as exc:
            raise RuntimeError(f"MinerU signed upload failed: {exc}") from exc
        finally:
            conn.close()

        if status not in (200, 201, 204):
            raise RuntimeError(f"MinerU signed upload failed with HTTP {status}: {body}")

    def _poll_result(self, batch_id: str, timeout: Optional[int] = None) -> Dict[str, Any]:
        timeout = self.poll_timeout if timeout is None else int(timeout)
        deadline = time.monotonic() + timeout
        last_state = ""

        while time.monotonic() < deadline:
            result = self._api_json_request(
                "GET", f"/api/v4/extract-results/batch/{batch_id}", timeout=60
            )
            data = result.get("data") or {}
            extract_result = data.get("extract_result") or []
            if isinstance(extract_result, dict):
                extract_result = [extract_result]

            if extract_result:
                item = extract_result[0]
                state = item.get("state", "")
                if state != last_state:
                    self.logger.info(
                        "MinerU online batch %s state: %s", batch_id, state
                    )
                    last_state = state
                if state == "done":
                    return item
                if state == "failed":
                    raise RuntimeError(
                        f"MinerU online parsing failed: {item.get('err_msg') or item}"
                    )

            time.sleep(self.poll_interval)

        raise TimeoutError(
            f"MinerU online parsing did not finish within {timeout}s. "
            f"batch_id={batch_id}"
        )

    def _download_and_extract_result(
        self, result_item: Dict[str, Any], output_dir: Path
    ) -> Path:
        zip_url = (
            result_item.get("full_zip_url")
            or result_item.get("zip_url")
            or result_item.get("download_url")
            or result_item.get("result_url")
        )
        if not zip_url:
            raise RuntimeError(f"MinerU online result has no zip URL: {result_item}")

        zip_path = output_dir / "mineru_online_result.zip"
        extract_dir = output_dir / "mineru_online"
        extract_dir.mkdir(parents=True, exist_ok=True)

        req = urllib.request.Request(
            zip_url,
            headers={
                "User-Agent": "RAGAnything/MineruOnlineParser",
                "Accept": "*/*",
            },
        )
        # MinerU 结果 zip 托管在对象存储 CDN 上，偶发 SSL EOF / 连接中断，
        # 属瞬时错误，需重试；批次已 done，URL 在有效期内可重复下载。
        try:
            max_attempts = int(os.getenv("MINERU_DOWNLOAD_RETRIES", "4"))
        except ValueError:
            max_attempts = 4
        if max_attempts < 1:
            max_attempts = 1

        last_exc: Optional[Exception] = None
        for attempt in range(1, max_attempts + 1):
            try:
                with urllib.request.urlopen(req, timeout=300) as response:
                    with open(zip_path, "wb") as f:
                        shutil.copyfileobj(response, f)
                last_exc = None
                break
            except Exception as exc:  # SSL EOF / URLError / timeout 等瞬时错误
                last_exc = exc
                if attempt < max_attempts:
                    backoff = min(2.0 ** attempt, 30.0)  # 2s,4s,8s,16s(≤30)
                    self.logger.warning(
                        "MinerU result zip download attempt %s/%s failed: %s. "
                        "Retrying in %ss",
                        attempt, max_attempts, exc, backoff,
                    )
                    time.sleep(backoff)
        if last_exc is not None:
            raise RuntimeError(
                f"Failed to download MinerU result zip after {max_attempts} attempts: {last_exc}"
            ) from last_exc

        try:
            with zipfile.ZipFile(zip_path, "r") as archive:
                for member in archive.infolist():
                    member_path = (extract_dir / member.filename).resolve()
                    if not member_path.is_relative_to(extract_dir.resolve()):
                        self.logger.warning(
                            "Skipping unsafe path in MinerU result zip: %s",
                            member.filename,
                        )
                        continue
                    archive.extract(member, extract_dir)
        except zipfile.BadZipFile as exc:
            raise RuntimeError(
                f"MinerU result is not a valid zip file: {zip_path}"
            ) from exc

        return extract_dir

    @classmethod
    def _read_any_output_files(
        cls, output_dir: Path, file_stem: str, method: str = "auto"
    ) -> Tuple[List[Dict[str, Any]], str]:
        content_list, md_content = cls._read_output_files(output_dir, file_stem, method)
        if content_list:
            return content_list, md_content

        json_candidates = sorted(output_dir.rglob(f"{file_stem}_content_list.json"))
        if not json_candidates:
            json_candidates = sorted(output_dir.rglob("*_content_list.json"))
        if not json_candidates:
            json_candidates = sorted(output_dir.rglob("content_list.json"))

        md_candidates = sorted(output_dir.rglob(f"{file_stem}.md"))
        if not md_candidates:
            md_candidates = sorted(output_dir.rglob("full.md"))
        if not md_candidates:
            md_candidates = sorted(output_dir.rglob("*.md"))

        md_content = ""
        if md_candidates:
            try:
                md_content = md_candidates[0].read_text(encoding="utf-8")
            except Exception as e:
                cls.logger.warning(
                    f"Could not read markdown file {md_candidates[0]}: {e}"
                )

        if not json_candidates:
            if md_content:
                return [{"type": "text", "text": md_content, "page_idx": 0}], md_content
            return [], md_content

        json_file = json_candidates[0]
        images_base_dir = json_file.parent
        try:
            with open(json_file, "r", encoding="utf-8") as f:
                content_list = json.load(f)
        except Exception as e:
            cls.logger.warning(f"Could not read JSON file {json_file}: {e}")
            return [], md_content

        for item in content_list:
            if not isinstance(item, dict):
                continue
            for old_name, new_name in cls._FIELD_ALIASES.items():
                if old_name in item and new_name not in item:
                    item[new_name] = item[old_name]
                elif new_name in item and old_name not in item:
                    item[old_name] = item[new_name]

            for field_name in ["img_path", "table_img_path", "equation_img_path"]:
                if field_name not in item or not item[field_name]:
                    continue
                media_path = Path(str(item[field_name]))
                if media_path.is_absolute():
                    absolute_media_path = media_path.resolve()
                else:
                    absolute_media_path = (images_base_dir / media_path).resolve()

                resolved_base = images_base_dir.resolve()
                if not absolute_media_path.is_relative_to(resolved_base):
                    cls.logger.warning(
                        "Potential path traversal detected in %s: %s. Skipping.",
                        field_name,
                        item[field_name],
                    )
                    item[field_name] = ""
                    continue
                item[field_name] = str(absolute_media_path)

            attach_public_media_urls(item)

        return content_list, md_content

    def parse_pdf(
        self,
        pdf_path: Union[str, Path],
        output_dir: Optional[str] = None,
        method: str = "auto",
        lang: Optional[str] = None,
        **kwargs,
    ) -> List[Dict[str, Any]]:
        pdf_path = Path(pdf_path)
        if not pdf_path.exists():
            raise FileNotFoundError(f"PDF file does not exist: {pdf_path}")

        if output_dir:
            base_output_dir = self._unique_output_dir(output_dir, pdf_path)
        else:
            base_output_dir = pdf_path.parent / "mineru_online_output"
        base_output_dir.mkdir(parents=True, exist_ok=True)

        batch_id, upload_url = self._request_upload_url(
            pdf_path, method, lang, **kwargs
        )
        self.logger.info(
            "Uploading %s to MinerU online batch %s", pdf_path.name, batch_id
        )
        self._upload_file(upload_url, pdf_path)
        result_item = self._poll_result(batch_id, timeout=kwargs.get("timeout"))
        extract_dir = self._download_and_extract_result(result_item, base_output_dir)
        content_list, _ = self._read_any_output_files(
            extract_dir, pdf_path.stem, method=method
        )
        return content_list

    def parse_image(
        self,
        image_path: Union[str, Path],
        output_dir: Optional[str] = None,
        lang: Optional[str] = None,
        **kwargs,
    ) -> List[Dict[str, Any]]:
        kwargs.pop("method", None)
        return self._parse_online_file(
            image_path, output_dir=output_dir, method="ocr", lang=lang, **kwargs
        )

    def _parse_online_file(
        self,
        file_path: Union[str, Path],
        output_dir: Optional[str] = None,
        method: str = "auto",
        lang: Optional[str] = None,
        **kwargs,
    ) -> List[Dict[str, Any]]:
        file_path = Path(file_path)
        if not file_path.exists():
            raise FileNotFoundError(f"File does not exist: {file_path}")

        if output_dir:
            base_output_dir = self._unique_output_dir(output_dir, file_path)
        else:
            base_output_dir = file_path.parent / "mineru_online_output"
        base_output_dir.mkdir(parents=True, exist_ok=True)

        batch_id, upload_url = self._request_upload_url(
            file_path, method, lang, **kwargs
        )
        self.logger.info(
            "Uploading %s to MinerU online batch %s", file_path.name, batch_id
        )
        self._upload_file(upload_url, file_path)
        result_item = self._poll_result(batch_id, timeout=kwargs.get("timeout"))
        extract_dir = self._download_and_extract_result(result_item, base_output_dir)
        content_list, _ = self._read_any_output_files(
            extract_dir, file_path.stem, method=method
        )
        return content_list

    def parse_office_doc(
        self,
        doc_path: Union[str, Path],
        output_dir: Optional[str] = None,
        lang: Optional[str] = None,
        **kwargs,
    ) -> List[Dict[str, Any]]:
        method = kwargs.pop("method", "auto")
        return self._parse_online_file(
            doc_path, output_dir=output_dir, method=method, lang=lang, **kwargs
        )

    def parse_text_file(
        self,
        text_path: Union[str, Path],
        output_dir: Optional[str] = None,
        lang: Optional[str] = None,
        **kwargs,
    ) -> List[Dict[str, Any]]:
        method = kwargs.pop("method", "auto")
        return self._parse_online_file(
            text_path, output_dir=output_dir, method=method, lang=lang, **kwargs
        )

    def parse_document(
        self,
        file_path: Union[str, Path],
        method: str = "auto",
        output_dir: Optional[str] = None,
        lang: Optional[str] = None,
        **kwargs,
    ) -> List[Dict[str, Any]]:
        file_path = Path(file_path)
        if not file_path.exists():
            raise FileNotFoundError(f"File does not exist: {file_path}")

        ext = file_path.suffix.lower()
        if ext == ".pdf":
            return self.parse_pdf(file_path, output_dir, method, lang, **kwargs)
        if ext in self.IMAGE_FORMATS:
            return self.parse_image(file_path, output_dir, lang, **kwargs)
        if ext in self.OFFICE_FORMATS:
            return self._parse_online_file(
                file_path, output_dir=output_dir, method=method, lang=lang, **kwargs
            )
        if ext in self.TEXT_FORMATS:
            # [jonex] 纯文本（txt/md/json）本地解析：MinerU online API 服务端
            # 不支持这些类型（v4/file-urls/batch 返回 unsupported file type）。
            # base.Parser.parse_text 按空行分段切块，无需 VLM。
            return self.parse_text(file_path, output_dir=output_dir, **kwargs)

        self.logger.warning(
            "Warning: Unsupported file extension '%s', attempting MinerU online parse",
            ext,
        )
        return self._parse_online_file(
            file_path, output_dir=output_dir, method=method, lang=lang, **kwargs
        )

    def check_installation(self) -> bool:
        return bool(self.api_token)


# ── [jonex] MineruSelfHostParser ─────────────────────────────────────

class MineruSelfHostParser(MineruParser):
    """MinerU self-hosted (intranet) API parser.  # [jonex]

    对接**内网自建的 MinerU 官方 ``mineru-api`` (FastAPI) 服务**，而非
    mineru.net 云 API。与本地 ``mineru`` CLI 不同：本 parser 只做 HTTP
    客户端，解析算力全部落在内网服务上，atomic-rag 侧无需 torch / mineru
    模型 / GPU。

    契约（实测 mineru-api 3.4.x，详见
    docs/mineru-selfhost-parser-execution-plan.md）：

    - ``POST /tasks`` — multipart 上传文件，返回 task_id
    - ``GET  /tasks/{id}`` — 轮询状态 pending→processing→completed/failed
    - ``GET  /tasks/{id}/result`` — 取解析结果

    仅使用标准库（urllib），不引入第三方依赖，也不依赖本地 mineru 包。
    """

    logger = logging.getLogger(__name__)

    BASE_URL_ENV = "MINERU_SELFHOST_BASE_URL"
    BACKEND_ENV = "MINERU_SELFHOST_BACKEND"
    LANG_ENV = "MINERU_SELFHOST_LANG"
    POLL_INTERVAL_ENV = "MINERU_SELFHOST_POLL_INTERVAL"
    POLL_TIMEOUT_ENV = "MINERU_SELFHOST_POLL_TIMEOUT"
    RETURN_IMAGES_ENV = "MINERU_SELFHOST_RETURN_IMAGES"

    def __init__(self) -> None:
        super().__init__()
        self.base_url = os.environ.get(
            self.BASE_URL_ENV, "http://127.0.0.1:8000"
        ).rstrip("/")
        self.backend = (
            os.environ.get(self.BACKEND_ENV, "pipeline").strip() or "pipeline"
        )
        self.default_lang = (
            os.environ.get(self.LANG_ENV, "ch").strip() or "ch"
        )
        self.poll_interval = float(
            os.environ.get(self.POLL_INTERVAL_ENV, "5")
        )
        self.poll_timeout = int(
            os.environ.get(self.POLL_TIMEOUT_ENV, "1800")
        )
        self.return_images = os.environ.get(
            self.RETURN_IMAGES_ENV, "false"
        ).strip().lower() in ("1", "true", "yes", "on")

    # ── HTTP helpers ─────────────────────────────────────────────

    @staticmethod
    def _ascii_safe_filename(name: str) -> str:
        """ASCII-safe filename for multipart upload.

        MinerU detects the type by extension; the stem (sans extension)
        becomes the result key.  Non-ASCII chars in the stem are replaced
        with ``_`` to avoid encoding ambiguity across the multipart
        boundary.  Falls back to ``upload`` when the stem is empty or
        entirely non-ASCII.
        """
        safe = "".join(
            c
            if (c.isascii() and (c.isalnum() or c in "._-"))
            else "_"
            for c in name
        )
        stem, dot, ext = safe.rpartition(".")
        if dot:
            if not stem.strip("_"):
                stem = "upload"
            return f"{stem}.{ext}"
        if not safe.strip("_"):
            return "upload"
        return safe

    def _post_multipart(
        self,
        path: str,
        file_path: Path,
        fields: Dict[str, Any],
        timeout: int = 300,
    ) -> Dict[str, Any]:
        """Build a multipart/form-data request (stdlib only).

        List values in *fields* are expanded as repeated form fields
        (matching FastAPI ``List[...]`` Form behavior).
        """
        boundary = "----RAGAnythingBoundary" + hashlib.md5(
            os.urandom(16)
        ).hexdigest()
        crlf = b"\r\n"
        body = bytearray()

        def _add_field(name: str, value: Any) -> None:
            body.extend(b"--" + boundary.encode("utf-8") + crlf)
            body.extend(
                f'Content-Disposition: form-data; name="{name}"'.encode(
                    "utf-8"
                )
                + crlf
            )
            body.extend(crlf)
            body.extend(str(value).encode("utf-8") + crlf)

        for key, value in fields.items():
            if isinstance(value, (list, tuple)):
                for item in value:
                    _add_field(key, item)
            elif isinstance(value, bool):
                _add_field(key, "true" if value else "false")
            else:
                _add_field(key, value)

        with open(file_path, "rb") as f:
            file_bytes = f.read()

        safe_filename = self._ascii_safe_filename(file_path.name)
        body.extend(b"--" + boundary.encode("utf-8") + crlf)
        body.extend(
            (
                'Content-Disposition: form-data; name="files"; '
                f'filename="{safe_filename}"'
            ).encode("utf-8")
            + crlf
        )
        body.extend(b"Content-Type: application/octet-stream" + crlf)
        body.extend(crlf)
        body.extend(file_bytes + crlf)
        body.extend(b"--" + boundary.encode("utf-8") + b"--" + crlf)

        url = f"{self.base_url}{path}"
        headers = {
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Accept": "application/json",
        }
        req = urllib.request.Request(
            url, data=bytes(body), headers=headers, method="POST"
        )
        return self._read_json_response(req, f"POST {path}", timeout)

    def _get_json(
        self, path: str, timeout: int = 60
    ) -> Dict[str, Any]:
        url = f"{self.base_url}{path}"
        req = urllib.request.Request(
            url, headers={"Accept": "application/json"}, method="GET"
        )
        return self._read_json_response(req, f"GET {path}", timeout)

    @staticmethod
    def _read_json_response(
        req: "urllib.request.Request", label: str, timeout: int
    ) -> Dict[str, Any]:
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="ignore")
            raise RuntimeError(
                f"MinerU self-host request failed: {label} "
                f"HTTP {exc.code}: {detail}"
            ) from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(
                f"MinerU self-host request failed: {label}: {exc}"
            ) from exc
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"MinerU self-host returned non-JSON for {label}: "
                f"{raw[:500]}"
            ) from exc

    # ── task lifecycle ──────────────────────────────────────────

    def _submit_task(
        self,
        file_path: Path,
        method: str,
        lang: Optional[str],
        **kwargs,
    ) -> str:
        backend = str(kwargs.get("selfhost_backend") or self.backend)
        fields: Dict[str, Any] = {
            "backend": backend,
            "return_content_list": True,
            "return_md": True,
            "return_images": bool(self.return_images),
            "formula_enable": bool(kwargs.get("formula", True)),
            "table_enable": bool(kwargs.get("table", True)),
        }
        if backend in (
            "pipeline",
            "hybrid-engine",
            "hybrid-http-client",
        ):
            fields["lang_list"] = [lang or self.default_lang]
            if method in ("auto", "txt", "ocr"):
                fields["parse_method"] = method
        if kwargs.get("effort") in ("medium", "high"):
            fields["effort"] = kwargs["effort"]
        start_page = kwargs.get("start_page")
        end_page = kwargs.get("end_page")
        if start_page is not None:
            fields["start_page_id"] = int(start_page)
        if end_page is not None:
            fields["end_page_id"] = int(end_page)

        result = self._post_multipart(
            "/tasks", file_path, fields, timeout=300
        )
        task_id = result.get("task_id")
        if not task_id:
            raise RuntimeError(
                f"MinerU self-host did not return task_id: {result}"
            )
        self.logger.info(
            "MinerU self-host task submitted: %s (backend=%s, file=%s)",
            task_id,
            backend,
            file_path.name,
        )
        return task_id

    def _poll_task(
        self, task_id: str, timeout: Optional[int] = None
    ) -> None:
        timeout = self.poll_timeout if timeout is None else int(timeout)
        deadline = time.monotonic() + timeout
        last_state = ""
        while time.monotonic() < deadline:
            status = self._get_json(f"/tasks/{task_id}", timeout=60)
            state = status.get("status", "")
            if state != last_state:
                self.logger.info(
                    "MinerU self-host task %s: %s", task_id, state
                )
                last_state = state
            if state == "completed":
                return
            if state == "failed":
                raise RuntimeError(
                    f"MinerU self-host parsing failed: "
                    f"{status.get('error') or status}"
                )
            time.sleep(self.poll_interval)
        raise TimeoutError(
            f"MinerU self-host parsing did not finish within {timeout}s. "
            f"task_id={task_id}"
        )

    def _fetch_result_item(
        self, task_id: str, file_stem: str
    ) -> Dict[str, Any]:
        payload = self._get_json(
            f"/tasks/{task_id}/result", timeout=120
        )
        results = payload.get("results") or {}
        if not results:
            raise RuntimeError(
                f"MinerU self-host result is empty for task {task_id}: "
                f"{payload}"
            )
        if file_stem in results:
            return results[file_stem]
        # Single-file task – fall back to the first result entry
        return next(iter(results.values()))

    def _build_content_list(
        self, result_item: Dict[str, Any], output_dir: Path
    ) -> List[Dict[str, Any]]:
        raw = result_item.get("content_list")
        if raw is None:
            md = result_item.get("md_content") or ""
            return (
                [{"type": "text", "text": md, "page_idx": 0}]
                if md
                else []
            )
        if isinstance(raw, str):
            try:
                content_list = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise RuntimeError(
                    f"MinerU self-host content_list is not valid JSON: "
                    f"{raw[:500]}"
                ) from exc
        else:
            content_list = raw
        if not isinstance(content_list, list):
            raise RuntimeError(
                f"MinerU self-host content_list has unexpected type: "
                f"{type(content_list)}"
            )

        images_base_dir = output_dir
        images = result_item.get("images")
        if self.return_images and isinstance(images, dict) and images:
            images_base_dir = output_dir / "images"
            images_base_dir.mkdir(parents=True, exist_ok=True)
            self._dump_images(images, images_base_dir)

        resolved_base = images_base_dir.resolve()
        for item in content_list:
            if not isinstance(item, dict):
                continue
            for old_name, new_name in self._FIELD_ALIASES.items():
                if old_name in item and new_name not in item:
                    item[new_name] = item[old_name]
                elif new_name in item and old_name not in item:
                    item[old_name] = item[new_name]

            for field_name in (
                "img_path",
                "table_img_path",
                "equation_img_path",
            ):
                if not item.get(field_name):
                    continue
                if not self.return_images:
                    item[field_name] = ""
                    continue
                candidate = (
                    images_base_dir
                    / Path(str(item[field_name])).name
                ).resolve()
                if (
                    not candidate.is_relative_to(resolved_base)
                    or not candidate.exists()
                ):
                    item[field_name] = ""
                    continue
                item[field_name] = str(candidate)

            attach_public_media_urls(item)

        return content_list

    @staticmethod
    def _dump_images(images: Dict[str, Any], out_dir: Path) -> None:
        """Write base64 / data-URL images from the response to *out_dir*."""
        for name, value in images.items():
            if not isinstance(value, str) or not value:
                continue
            data = value
            if data.startswith("data:"):
                comma = data.find(",")
                if comma != -1:
                    data = data[comma + 1 :]
            try:
                blob = base64.b64decode(data)
            except Exception:
                continue
            target = out_dir / Path(name).name
            try:
                with open(target, "wb") as f:
                    f.write(blob)
            except OSError:
                continue

    # ── unified parse flow ──────────────────────────────────────

    def _parse_via_selfhost(
        self,
        file_path: Union[str, Path],
        output_dir: Optional[str] = None,
        method: str = "auto",
        lang: Optional[str] = None,
        **kwargs,
    ) -> List[Dict[str, Any]]:
        file_path = Path(file_path)
        if not file_path.exists():
            raise FileNotFoundError(f"File does not exist: {file_path}")

        if output_dir:
            base_output_dir = self._unique_output_dir(output_dir, file_path)
        else:
            base_output_dir = (
                file_path.parent / "mineru_selfhost_output"
            )
        base_output_dir.mkdir(parents=True, exist_ok=True)

        task_id = self._submit_task(file_path, method, lang, **kwargs)
        self._poll_task(task_id, timeout=kwargs.get("timeout"))
        result_item = self._fetch_result_item(task_id, file_path.stem)
        return self._build_content_list(result_item, base_output_dir)

    def parse_pdf(
        self,
        pdf_path: Union[str, Path],
        output_dir: Optional[str] = None,
        method: str = "auto",
        lang: Optional[str] = None,
        **kwargs,
    ) -> List[Dict[str, Any]]:
        return self._parse_via_selfhost(
            pdf_path,
            output_dir=output_dir,
            method=method,
            lang=lang,
            **kwargs,
        )

    def parse_image(
        self,
        image_path: Union[str, Path],
        output_dir: Optional[str] = None,
        lang: Optional[str] = None,
        **kwargs,
    ) -> List[Dict[str, Any]]:
        kwargs.pop("method", None)
        return self._parse_via_selfhost(
            image_path,
            output_dir=output_dir,
            method="ocr",
            lang=lang,
            **kwargs,
        )

    def parse_office_doc(
        self,
        doc_path: Union[str, Path],
        output_dir: Optional[str] = None,
        lang: Optional[str] = None,
        **kwargs,
    ) -> List[Dict[str, Any]]:
        method = kwargs.pop("method", "auto")
        return self._parse_via_selfhost(
            doc_path,
            output_dir=output_dir,
            method=method,
            lang=lang,
            **kwargs,
        )

    def parse_document(
        self,
        file_path: Union[str, Path],
        method: str = "auto",
        output_dir: Optional[str] = None,
        lang: Optional[str] = None,
        **kwargs,
    ) -> List[Dict[str, Any]]:
        return self._parse_via_selfhost(
            file_path,
            output_dir=output_dir,
            method=method,
            lang=lang,
            **kwargs,
        )

    def check_installation(self) -> bool:
        """Probe ``GET /health``.  Always returns ``True`` — network
        failures are a runtime concern, not an installation one."""
        try:
            health = self._get_json("/health", timeout=10)
            if health.get("status") == "healthy":
                return True
            self.logger.warning(
                "MinerU self-host /health status=%s",
                health.get("status"),
            )
        except Exception as exc:
            self.logger.warning(
                "MinerU self-host health check failed (%s): %s",
                self.base_url,
                exc,
            )
        return True


# ── [jonex] Path normalization ─────────────────────────────────────────

def normalize_asset_paths(
    content_list: list[dict],
    tenant_id: str,
    doc_id: str,
    timestamp: str,
    asset_base_url: str,
) -> list[dict]:
    """Rewrite local asset paths in content_list to normalized relative paths.

    Original content_list is deep-copied before modification.
    Each asset gets both a relative local path and an external URL.
    """
    result = copy.deepcopy(content_list)
    relative_base = f"{tenant_id}/{doc_id}/versions/{timestamp}/mineru"
    url_base = f"{asset_base_url}/{relative_base}"

    for item in result:
        # Rewrite img_path
        if "img_path" in item and item["img_path"]:
            fname = os.path.basename(str(item["img_path"]))
            item["img_path"] = f"{relative_base}/images/{fname}"
            item["img_url"] = f"{url_base}/images/{fname}"
        # Rewrite video_path
        if "video_path" in item and item["video_path"]:
            fname = os.path.basename(str(item["video_path"]))
            item["video_path"] = f"{relative_base}/{fname}"
            item["video_url"] = f"{url_base}/{fname}"
    return result
