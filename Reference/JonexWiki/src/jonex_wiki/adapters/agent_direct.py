"""Run Agent over a pinned local Bundle through the existing model gateway.

The normal query entry uses this executor. It does not call the retired Wiki
agent loop or an MCP server. A sandbox preflight must pass before execution.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import platform
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time


SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "answer": {"type": "string"},
        "short_answer": {"type": "string"},
        "incomplete": {"type": "boolean"},
        "reason": {"type": "string"},
        "method_notes": {"type": "string"},
        "evidence": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "path": {"type": "string"},
                    "locator": {"type": "string"},
                    "quote": {"type": "string"},
                },
                "required": ["path", "locator"],
            },
        },
    },
    "required": [
        "answer",
        "short_answer",
        "incomplete",
        "reason",
        "method_notes",
        "evidence",
    ],
}


def _profile(root, bundle, work, package, python_prefix, python_base, cli=None):
    if platform.system() == "Linux":
        rules = {
            ":minimal": "read",
            "/": "deny",
            "/bin": "read",
            "/sbin": "read",
            "/usr": "read",
            "/lib": "read",
            "/lib64": "read",
            "/etc": "read",
        }
        if cli is not None:
            rules[str(Path(cli).resolve().parent.parent)] = "read"
    else:
        rules = {
            ":minimal": "read",
            "/Users": "deny",
            "/Volumes": "deny",
            "/private": "deny",
            "/bin": "read",
            "/sbin": "read",
            "/usr": "read",
            "/System": "read",
            "/Library": "read",
            "/opt/homebrew": "read",
        }
    rules.update(
        {
            str(root): "deny",
            str(package): "read",
            str(python_prefix): "read",
            str(python_base): "read",
            str(bundle): "read",
            str(work): "write",
        }
    )
    table = (
        "{"
        + ",".join(json.dumps(k) + "=" + json.dumps(v) for k, v in rules.items())
        + "}"
    )
    return [
        "-c",
        'default_permissions="wiki_direct"',
        "-c",
        "permissions.wiki_direct.filesystem=" + table,
        "-c",
        "permissions.wiki_direct.network.enabled=false",
    ]


def _process_probe_code(system, pid, marker, *, proc_root="/proc", darwin_libc=None):
    """Detect a real process outside the sandbox; inconclusive checks fail closed."""
    if system == "Linux":
        inspect = (
            f"probe_pid={int(pid)}; probe_token=bytes.fromhex({marker.encode().hex()!r})\n"
            f"probe_file=Path({str(proc_root)!r})/str(probe_pid)/'cmdline'\n"
            "try:\n"
            " data=probe_file.read_bytes()\n"
            " info='READABLE' if probe_token in data else 'UNKNOWN'\n"
            "except (FileNotFoundError,PermissionError): info='DENIED'\n"
            "except OSError: info='UNKNOWN'\n"
        )
    elif system == "Darwin":
        inspect = (
            "import ctypes,errno\n"
            f"probe_pid={int(pid)}; probe_token=bytes.fromhex({marker.encode().hex()!r})\n"
            "try:\n"
            f" lib=ctypes.CDLL({darwin_libc!r},use_errno=True)\n"
            " lib.sysctl.argtypes=[ctypes.POINTER(ctypes.c_int),ctypes.c_uint,ctypes.c_void_p,ctypes.POINTER(ctypes.c_size_t),ctypes.c_void_p,ctypes.c_size_t]\n"
            " mib=(ctypes.c_int*3)(1,49,probe_pid); size=ctypes.c_size_t()\n"
            " rc=lib.sysctl(mib,3,None,ctypes.byref(size),None,0)\n"
            " if rc==0 and 0<size.value<=1048576:\n"
            "  data=ctypes.create_string_buffer(size.value)\n"
            "  rc=lib.sysctl(mib,3,data,ctypes.byref(size),None,0)\n"
            "  info=('READABLE' if probe_token in data.raw[:size.value] else 'UNKNOWN') if rc==0 else ('DENIED' if ctypes.get_errno() in (errno.EPERM,errno.EACCES,errno.ESRCH) else 'UNKNOWN')\n"
            " elif rc!=0 and ctypes.get_errno() in (errno.EPERM,errno.EACCES,errno.ESRCH): info='DENIED'\n"
            " else: info='UNKNOWN'\n"
            "except (OSError,AttributeError): info='UNKNOWN'\n"
        )
    else:
        raise RuntimeError("AGENT_SANDBOX_PROCESS_PROBE_UNSUPPORTED")
    return inspect + (
        "try: os.kill(probe_pid,0);signal='READABLE'\n"
        "except (ProcessLookupError,PermissionError): signal='DENIED'\n"
        "except OSError: signal='UNKNOWN'\n"
        "print('process_info='+info);print('process_signal='+signal)\n"
        "result='READABLE' if 'READABLE' in (info,signal) else ('DENIED' if info==signal=='DENIED' else 'UNKNOWN')\n"
        "print('process='+result)\n"
    )


def _preflight(cli, profile, bundle, root, work, env, *, installed_command=None):
    """Fail closed on cross-workspace, link, network or process leakage."""
    outside = root / "private" / "other-tenant-sentinel"
    outside.write_text("synthetic only")
    sibling = root.parent / (root.name + "-other-tenant-sentinel")
    sibling.write_text("synthetic only")
    link = work / "outside-link"
    link.symlink_to(outside)
    listener = socket.socket()
    sentinel = None
    sentinel_alive = False
    package = installed_command.parent if installed_command is not None else root / "package"
    command_check = (
        f"p=Path({str(installed_command)!r}); print('package_command='+str(p.is_file() and os.access(p,os.R_OK|os.X_OK)))\n"
        if installed_command is not None else ""
    )
    try:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        marker = "wiki-process-probe-" + secrets.token_hex(16)
        sentinel = subprocess.Popen(
            [str(Path(sys.executable).resolve()), "-I", "-c", "import time; time.sleep(300)", marker],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            env={"PATH": "/usr/bin:/bin"},
        )
        system = platform.system()
        process_probe = _process_probe_code(system, sentinel.pid, marker)
        try:
            baseline = subprocess.run(
                [str(Path(sys.executable).resolve()), "-I", "-c",
                 "import os\nfrom pathlib import Path\n" + process_probe],
                capture_output=True, text=True, timeout=10,
            )
            baseline_lines = set(baseline.stdout.splitlines())
            observable = baseline.returncode == 0 and {
                "process_info=READABLE", "process_signal=READABLE",
            }.issubset(baseline_lines)
        except (OSError, subprocess.TimeoutExpired):
            observable = False
        if not observable or sentinel.poll() is not None:
            raise RuntimeError("AGENT_SANDBOX_PROCESS_PROBE_UNAVAILABLE")
        code = (
            "import os,socket,subprocess\nfrom pathlib import Path\n"
            f"p=Path({str(outside)!r}); link=Path({str(link)!r}); sibling=Path({str(sibling)!r})\n"
            f"print('bundle='+str(Path({str(bundle / 'indexes/manifest.json')!r}).is_file()))\n"
            "for name,target in [('outside',p),('symlink',link),('sibling',sibling)]:\n"
            " try: target.read_text();print(name+'=READABLE')\n"
            " except OSError: print(name+'=DENIED')\n"
            f"try: os.link(p,{str(work / 'outside-hardlink')!r});print('hardlink=CREATED')\n"
            "except OSError: print('hardlink=DENIED')\n"
            f"try: open({str(bundle / 'indexes/manifest.json')!r},'r+').close();print('bundle_write=ALLOWED')\n"
            "except OSError: print('bundle_write=DENIED')\n"
            f"try: open({str(package / 'write-probe')!r},'w').close();print('package_write=ALLOWED')\n"
            "except OSError: print('package_write=DENIED')\n"
            f"try: socket.create_connection(('127.0.0.1',{listener.getsockname()[1]}),timeout=2);print('network=CONNECTED')\n"
            "except OSError: print('network=DENIED')\n"
            + command_check + process_probe
        )
        proc = subprocess.run(
            [
                cli,
                *profile,
                "sandbox",
                str(Path(sys.executable).resolve()),
                "-I",
                "-c",
                code,
            ],
            cwd=work,
            env=env,
            text=True,
            capture_output=True,
            timeout=120,
        )
        sentinel_alive = sentinel.poll() is None
    finally:
        listener.close()
        if sentinel is not None and sentinel.poll() is None:
            sentinel.terminate()
            try:
                sentinel.wait(timeout=3)
            except subprocess.TimeoutExpired:
                sentinel.kill()
                sentinel.wait()
        sibling.unlink(missing_ok=True)
    required = [
        "bundle=True",
        "outside=DENIED",
        "symlink=DENIED",
        "sibling=DENIED",
        "hardlink=DENIED",
        "bundle_write=DENIED",
        "package_write=DENIED",
        "network=DENIED",
        "process_info=DENIED",
        "process_signal=DENIED",
        "process=DENIED",
    ]
    if installed_command is not None:
        required.append("package_command=True")
    observed = set(proc.stdout.splitlines())
    missing = [value for value in required if value not in observed]
    if proc.returncode or missing or not sentinel_alive:
        raise RuntimeError(
            "AGENT_SANDBOX_PREFLIGHT_FAILED:exit="
            + str(proc.returncode)
            + ":missing="
            + ",".join(missing)
            + (":sentinel_exited" if not sentinel_alive else "")
        )
    return {"passed": True, "checks": required}


class AgentDirectExecutor:
    def __init__(self, gateway, settings, *, reasoning_effort=None):
        self.gateway = gateway
        self.settings = settings
        self.reasoning_effort = reasoning_effort or getattr(
            settings, "agent_reasoning_effort", "off"
        )

    async def query(
        self,
        *,
        view,
        question,
        model,
        tenant_id,
        context_id,
        run_id,
        revision_id,
        cancel=None,
        query_context=None,
        trace_id=None,
    ):
        if platform.system() not in {"Darwin", "Linux"}:
            raise RuntimeError("AGENT_SANDBOX_BACKEND_UNAVAILABLE")
        if not model:
            raise RuntimeError("AGENT_QUERY_MODEL_NOT_CONFIGURED")
        cli = self.settings.agent_cli_path or shutil.which("codex")
        if not cli or not Path(cli).is_file():
            raise RuntimeError("AGENT_CLI_NOT_CONFIGURED")
        root_path = getattr(view, "_dir", None)
        if root_path is None:
            raise RuntimeError("AGENT_DIRECT_REQUIRES_LOCAL_DIRECTORY_BUNDLE")
        bundle = Path(root_path).resolve()
        if cancel:
            cancel.raise_if_cancelled()
        loop = asyncio.get_running_loop()
        stop = threading.Event()
        # CLI output arrives from a worker thread. Preserve the task-local
        # listener, and deliver bounded events on the event-loop thread.
        import contextvars
        from jonex_wiki.kernel.query_progress import publish_query_progress
        progress_context = contextvars.copy_context()
        def report_progress(event):
            loop.call_soon_threadsafe(progress_context.run, publish_query_progress, event)
        self._progress_callback = report_progress
        model_sequence = 0

        async def complete(chat):
            nonlocal model_sequence
            if cancel:
                cancel.raise_if_cancelled()
            final_only = chat.get("tool_choice") == "none"
            tools = None if final_only else chat.get("tools")
            model_sequence += 1
            operation_id = f"model-{model_sequence}"
            model_started = time.monotonic()
            report_progress({"type": "model", "operation_id": operation_id, "status": "running"})
            try:
                value = await self.gateway.complete(
                    chat["messages"], scene="jonex_wiki_query_traversal",
                    tenant_id=tenant_id, kb_id=context_id,
                    trace_id=(trace_id or ("query:" + str(run_id or "agent")))[:64],
                    model=model, tools=tools, json_mode=final_only,
                    schema=SCHEMA if final_only else None,
                    thinking=chat.get("thinking"),
                    reasoning_effort=chat.get("reasoning_effort"),
                    max_tokens=chat["max_tokens"], request_timeout=None,
                )
            except BaseException:
                report_progress({"type": "model", "operation_id": operation_id, "status": "failed",
                                 "duration_ms": max(0, int((time.monotonic() - model_started) * 1000))})
                raise
            report_progress({"type": "model", "operation_id": operation_id, "status": "done",
                             "duration_ms": max(0, int((time.monotonic() - model_started) * 1000))})
            message = {
                "role": "assistant",
                "content": value.get("content") or "",
                "tool_calls": value.get("tool_calls") or [],
            }
            return {
                "model": value.get("model") or model,
                "usage": value.get("usage"),
                "choices": [
                    {
                        "message": message,
                        "finish_reason": value.get("finish_reason") or "stop",
                    }
                ],
            }

        try:
            return await asyncio.to_thread(
                self._execute,
                cli,
                bundle,
                question,
                model,
                tenant_id,
                context_id,
                run_id,
                revision_id,
                view.manifest_hash,
                loop,
                complete,
                stop,
                cancel,
                **({"query_context": query_context} if query_context is not None else {}),
            )
        except asyncio.CancelledError:
            stop.set()
            raise

    def _execute(
        self,
        cli,
        bundle,
        question,
        model,
        tenant_id,
        context_id,
        run_id,
        revision_id,
        bundle_sha,
        loop,
        complete,
        stop,
        cancel,
        query_context=None,
    ):
        from jonex_wiki.adapters.agent_files_direct import execute_files

        return execute_files(
            self,
            cli,
            bundle,
            question,
            model,
            tenant_id,
            context_id,
            run_id,
            revision_id,
            bundle_sha,
            loop,
            complete,
            stop,
            cancel,
            query_context,
        )
