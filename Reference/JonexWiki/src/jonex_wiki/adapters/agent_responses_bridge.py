"""Loopback-only Agent Responses to the configured Chat gateway adapter."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import threading
import time
import uuid


_NUMBERED_MATCH = re.compile(r"^[^:\r\n]+\.(?:md|jsonl?|txt|csv|tsv):[1-9][0-9]*:")


def _text(content):
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        raise ValueError("UNSUPPORTED_MESSAGE_CONTENT")
    out = []
    for part in content:
        if not isinstance(part, dict) or part.get("type") not in {
            "input_text",
            "output_text",
            "text",
        }:
            raise ValueError("UNSUPPORTED_NON_TEXT_INPUT")
        out.append(str(part.get("text") or ""))
    return "\n".join(out)


def _deduplicate_identical_shell_results(messages, exact_repeats):
    """Keep the first complete result when a command repeats it.

    The command still runs normally. The first complete result and any
    changed output remain visible. Numbered file matches may arrive in a
    different order; their source positions are part of the comparison.
    """
    calls = {}
    seen = set()
    read_outputs = set()
    repeated = False
    last_tool_index = next(
        (
            i
            for i in range(len(messages) - 1, -1, -1)
            if messages[i].get("role") == "tool"
        ),
        -1,
    )
    for index, message in enumerate(messages):
        for call in message.get("tool_calls") or []:
            function = call.get("function") or {}
            if function.get("name") == "exec_command":
                calls[call.get("id")] = function.get("arguments")
        if message.get("role") != "tool":
            continue
        command = calls.get(message.get("tool_call_id"))
        content = message.get("content")
        if not command or not isinstance(content, str):
            continue
        output = content
        status = ""
        if "\nOutput:\n" in content:
            output = content.split("\nOutput:\n", 1)[1]
            exit_status = re.search(
                r"Process exited with code (\d+)", content.split("\nOutput:\n", 1)[0]
            )
            status = exit_status.group(1) if exit_status else ""
            # Agent's patch shim wraps a shell output once more.
            nested = re.match(
                r"Exit code: ([^\n]*)\nWall time: [^\n]*\nOutput:\n", output
            )
            if nested:
                status = nested.group(1)
                output = output[nested.end() :]
        try:
            arguments = json.loads(command)
            shell = arguments.get("cmd", "")
            # Equivalent JSON objects can have different whitespace or key
            # order. Retain every argument (including workdir and shell) so
            # distinct execution conditions are not conflated.
            command_key = json.dumps(arguments, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        except (ValueError, TypeError, AttributeError):
            shell = ""
            command_key = command
        simple_read = (len(output) >= 256 and isinstance(shell, str)
                       and re.match(r"^\s*(?:cat|head|tail|sed|nl|rg|grep)\b", shell)
                       and not re.search(r"[\n\r;&<>]", shell))
        ordered = output
        if (exact_repeats is not None and status == "0" and isinstance(shell, str)
                and re.match(r"^\s*(?:rg|grep)\b", shell)):
            lines = output.splitlines()
            if len(lines) > 1 and all(_NUMBERED_MATCH.match(line) for line in lines):
                # Parallel searches can emit the same file-and-line hits in
                # different orders. Keep the first complete result verbatim.
                ordered = "\n".join(sorted(lines))
        digest = hashlib.sha256(ordered.encode()).digest()
        key = (command_key, status, digest)
        output_key = (status, digest)
        exact_repeat = key in seen
        seen.add(key)
        if (exact_repeats is not None and exact_repeat and
                (status.isdigit() or content.startswith("exec_command failed:"))):
            exact_repeats.add(message["tool_call_id"])
        if exact_repeat or (simple_read and output_key in read_outputs):
            message["content"] = (
                "This command returned the same printed output already shown above. "
                "If it failed, inspect the error and working directory; if it edited a file, inspect the current "
                "file before repeating it. Otherwise use the earlier result."
            )
            repeated = index == last_tool_index
        else:
            if simple_read:
                read_outputs.add(output_key)
    if repeated:
        messages.append(
            {
                "role": "system",
                "content": "An identical shell command and printed result have repeated. The original result remains in this "
                "conversation. Inspect current files if the command made changes; otherwise continue with a "
                "different useful action, or answer if the evidence is sufficient. "
                "All ordinary tools remain available.",
            }
        )
    return repeated


def _deduplicate_search_hits(messages):
    """Keep the first copy of identical file-and-line hits across shell searches."""
    calls = {}
    seen = set()
    for message in messages:
        for call in message.get("tool_calls") or []:
            function = call.get("function") or {}
            if function.get("name") == "exec_command":
                try:
                    arguments = json.loads(function.get("arguments") or "{}")
                    if isinstance(arguments, dict) and isinstance(arguments.get("cmd"), str):
                        calls[call.get("id")] = arguments["cmd"]
                except (ValueError, TypeError):
                    pass
        command = (
            calls.get(message.get("tool_call_id"))
            if message.get("role") == "tool"
            else None
        )
        content = message.get("content")
        if (
            not command
            or not isinstance(content, str)
            or not re.search(r"\b(?:rg|grep)\b", command)
        ):
            continue
        kept = []
        removed = 0
        for line in content.split("\n"):
            if re.search(r"\.(?:md|jsonl?):\d+[:\-]", line):
                if line in seen:
                    removed += 1
                    continue
                seen.add(line)
            kept.append(line)
        if removed:
            kept.append(
                f"[{removed} identical file-and-line search hits were already shown above.]"
            )
            message["content"] = "\n".join(kept)


def to_chat(request, model, *, exact_repeats=None):
    if request.get("model") != model:
        raise ValueError("MODEL_MISMATCH")
    messages = []
    if request.get("instructions"):
        messages.append({"role": "system", "content": request["instructions"]})
    inputs = request.get("input") or []
    if isinstance(inputs, str):
        inputs = [{"type": "message", "role": "user", "content": inputs}]
    for item in inputs:
        kind = item.get("type", "message")
        if kind == "reasoning":
            continue
        if kind == "message":
            role = item.get("role")
            if role not in {"developer", "system", "user", "assistant"}:
                raise ValueError("UNSUPPORTED_MESSAGE_ROLE")
            messages.append(
                {
                    "role": "system" if role == "developer" else role,
                    "content": _text(item.get("content") or ""),
                }
            )
        elif kind == "function_call":
            name = item.get("name")
            call_id = item.get("call_id")
            arguments = item.get("arguments")
            if (
                not isinstance(name, str)
                or not isinstance(call_id, str)
                or not isinstance(arguments, str)
            ):
                raise ValueError("INVALID_FUNCTION_CALL")
            call = {
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": arguments},
            }
            if (
                messages
                and messages[-1].get("role") == "assistant"
                and messages[-1].get("tool_calls") is not None
            ):
                messages[-1]["tool_calls"].append(call)
            elif (
                messages
                and messages[-1].get("role") == "assistant"
                and not messages[-1].get("tool_calls")
            ):
                messages[-1]["tool_calls"] = [call]
            else:
                messages.append(
                    {"role": "assistant", "content": None, "tool_calls": [call]}
                )
        elif kind == "function_call_output":
            output = item.get("output")
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": item["call_id"],
                    "content": output
                    if isinstance(output, str)
                    else json.dumps(output, ensure_ascii=False),
                }
            )
        else:
            raise ValueError("UNSUPPORTED_RESPONSES_ITEM:" + str(kind))
    repeated = _deduplicate_identical_shell_results(messages, exact_repeats)
    _deduplicate_search_hits(messages)
    tools = []
    for tool in request.get("tools") or []:
        # The read-only experiment needs shell execution and continuation only.
        if tool.get("type") != "function" or tool.get("name") not in {
            "exec_command",
            "write_stdin",
        }:
            continue
        fn = {
            "name": tool["name"],
            "description": tool.get("description", ""),
            "parameters": tool.get("parameters")
            or {"type": "object", "properties": {}},
        }
        tools.append({"type": "function", "function": fn})
    schema = ((request.get("text") or {}).get("format") or {}).get("schema")
    if schema:
        messages.insert(
            0,
            {
                "role": "system",
                "content": "For the final answer only, return a single JSON object matching this schema: "
                + json.dumps(schema, ensure_ascii=False, separators=(",", ":")),
            },
        )
    # Leave output capacity to the configured model provider. The direct path
    # has no per-answer token budget; provider/context limits still apply.
    value = {
        "model": model,
        "messages": messages,
        "stream": False,
        "temperature": 0,
        "max_tokens": None,
        "thinking": {"type": "disabled"},
    }
    if repeated:
        # The provider may otherwise repeat a successful command indefinitely.
        # Ask it to reason about the situation, without stopping any tools.
        value.update(thinking={"type": "enabled"}, reasoning_effort="xhigh")
    if tools:
        value.update(tools=tools, tool_choice="auto")
    return value


def events_from_chat(value, response_id=None):
    choice = (value.get("choices") or [{}])[0]
    if choice.get("finish_reason") == "length":
        raise ValueError("UPSTREAM_OUTPUT_TRUNCATED")
    msg = choice.get("message") or {}
    rid = response_id or ("resp_" + uuid.uuid4().hex)
    now = int(time.time())
    output = []
    events = []
    sequence = 0

    def send(kind, **kwargs):
        nonlocal sequence
        events.append({"type": kind, "sequence_number": sequence, **kwargs})
        sequence += 1

    base = {
        "id": rid,
        "object": "response",
        "created_at": now,
        "model": value.get("model") or "",
        "status": "in_progress",
        "output": [],
        "error": None,
        "metadata": {},
    }
    send("response.created", response=base.copy())
    send("response.in_progress", response=base.copy())
    text = msg.get("content") or ""
    if text:
        mid = "msg_" + uuid.uuid4().hex
        idx = len(output)
        phase = "commentary" if msg.get("tool_calls") else "final_answer"
        started = {
            "id": mid,
            "type": "message",
            "status": "in_progress",
            "role": "assistant",
            "phase": phase,
            "content": [],
        }
        send("response.output_item.added", output_index=idx, item=started)
        part = {"type": "output_text", "text": "", "annotations": []}
        send(
            "response.content_part.added",
            output_index=idx,
            item_id=mid,
            content_index=0,
            part=part,
        )
        send(
            "response.output_text.delta",
            output_index=idx,
            item_id=mid,
            content_index=0,
            delta=text,
        )
        send(
            "response.output_text.done",
            output_index=idx,
            item_id=mid,
            content_index=0,
            text=text,
        )
        part = {"type": "output_text", "text": text, "annotations": []}
        send(
            "response.content_part.done",
            output_index=idx,
            item_id=mid,
            content_index=0,
            part=part,
        )
        item = {**started, "status": "completed", "content": [part]}
        output.append(item)
        send("response.output_item.done", output_index=idx, item=item)
    for call in msg.get("tool_calls") or []:
        fn = call.get("function") or {}
        name = fn.get("name")
        args = fn.get("arguments")
        if name not in {"exec_command", "write_stdin"} or not isinstance(args, str):
            raise ValueError("UNSUPPORTED_UPSTREAM_TOOL")
        ident = "fc_" + uuid.uuid4().hex
        idx = len(output)
        call_id = call.get("id") or ("call_" + uuid.uuid4().hex)
        item = {
            "id": ident,
            "type": "function_call",
            "status": "in_progress",
            "name": name,
            "call_id": call_id,
            "arguments": "",
        }
        send("response.output_item.added", output_index=idx, item=item)
        send(
            "response.function_call_arguments.delta",
            output_index=idx,
            item_id=ident,
            delta=args,
        )
        send(
            "response.function_call_arguments.done",
            output_index=idx,
            item_id=ident,
            arguments=args,
        )
        item = {**item, "status": "completed", "arguments": args}
        output.append(item)
        send("response.output_item.done", output_index=idx, item=item)
    if not output:
        raise ValueError("EMPTY_UPSTREAM_OUTPUT")
    old = value.get("usage") or {}
    incoming = old.get("prompt_tokens")
    outgoing = old.get("completion_tokens")
    measured = isinstance(incoming, int) and isinstance(outgoing, int)
    usage = (
        {
            "input_tokens": incoming,
            "output_tokens": outgoing,
            "total_tokens": incoming + outgoing,
            "input_tokens_details": {
                "cached_tokens": (old.get("prompt_tokens_details") or {}).get(
                    "cached_tokens", old.get("prompt_cache_hit_tokens", 0)
                )
                or 0
            },
            "output_tokens_details": {
                "reasoning_tokens": (old.get("completion_tokens_details") or {}).get(
                    "reasoning_tokens", 0
                )
                or 0
            },
        }
        if measured
        else None
    )
    final = {
        **base,
        "status": "completed",
        "completed_at": int(time.time()),
        "output": output,
        "usage": usage,
    }
    send("response.completed", response=final)
    return events, usage


@contextmanager
def serve(
    *,
    model,
    loop,
    complete,
    ledger,
    record_dir=None,
    on_delivery=None,
    search_progress=None,
    request_size_guard=None,
    record_payloads=True,
    on_error=None,
):
    """Bridge protocol only; ``complete`` calls the existing model gateway.

    Optional raw recording must be explicitly configured by the operator and
    is never needed to make citations. No credentials or hidden reasoning are
    recorded. The ledger is updated only after an upstream call succeeds.
    """
    directory = Path(record_dir) if record_dir else None
    if directory:
        directory.mkdir(parents=True, exist_ok=False, mode=0o700)
    secret = uuid.uuid4().hex
    lock = threading.Lock()
    records = []
    pending = set()
    closing = False

    def save(name, value, *, receipt=False):
        if directory and (record_payloads or receipt):
            path = directory / name
            with path.open("x", encoding="utf-8") as stream:
                path.chmod(0o600)
                json.dump(value, stream, ensure_ascii=False)

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"

        def log_message(self, *args):
            pass

        def do_POST(self):
            if (
                self.path != "/v1/responses"
                or self.headers.get("Authorization") != "Bearer " + secret
            ):
                self.send_error(403)
                return
            try:
                n = int(self.headers.get("Content-Length", "0"))
                if n <= 0:
                    raise ValueError("REQUEST_SIZE")
                if request_size_guard is not None:
                    request_size_guard(n)
                raw = self.rfile.read(n)
                request = json.loads(raw)
                with lock:
                    record = {
                        "id": len(records) + 1,
                        "status": "started",
                        "usage": None,
                        "started": time.time(),
                    }
                    records.append(record)
                ident = f"{record['id']:03d}"
                if directory and record_payloads:
                    recorded = dict(request)
                    if isinstance(recorded.get("input"), list):
                        visible = [
                            item
                            for item in recorded["input"]
                            if item.get("type") != "reasoning"
                        ]
                        recorded["reasoning_items_redacted"] = len(recorded["input"]) - len(
                            visible
                        )
                        recorded["input"] = visible
                    save(ident + ".responses-request.json", recorded)
                exact_repeats = set() if search_progress is not None else None
                chat = to_chat(request, model, exact_repeats=exact_repeats)
                if request_size_guard is not None:
                    record["client_request_bytes"] = n
                if search_progress is not None:
                    with lock:
                        progress_action = search_progress.apply(request, chat, exact_repeats=exact_repeats)
                    if progress_action:
                        record["search_progress"] = progress_action
                save(ident + ".chat-request.json", chat)
                with lock:
                    if closing:
                        raise RuntimeError("AGENT_BRIDGE_CLOSED")
                    future = asyncio.run_coroutine_threadsafe(complete(chat), loop)
                    pending.add(future)
                try:
                    value = future.result()
                except BaseException:
                    future.cancel()
                    raise
                finally:
                    with lock:
                        pending.discard(future)
                for c in value.get("choices") or []:
                    message = c.get("message") or {}
                    reasoning = message.pop("reasoning_content", None)
                    if reasoning:
                        record["reasoning_sha256"] = hashlib.sha256(
                            reasoning.encode()
                        ).hexdigest()
                save(ident + ".chat-response.json", value)
                ev, usage = events_from_chat(value)
                record.update(
                    status="complete",
                    usage=usage,
                    returned_model=value.get("model"),
                    seconds=time.time() - record["started"],
                )
                with lock:
                    ledger.observe_successful_request(chat["messages"])
                    if on_delivery is not None:
                        on_delivery(ledger)
                data = "".join(
                    "event: "
                    + e["type"]
                    + "\ndata: "
                    + json.dumps(e, ensure_ascii=False, separators=(",", ":"))
                    + "\n\n"
                    for e in ev
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except Exception as error:
                if on_error is not None:
                    on_error(error)
                if "record" in locals():
                    record.update(status="bridge_error", error=type(error).__name__,
                                  seconds=time.time() - record["started"])
                try:
                    self.send_error(500, "Protocol adapter failed")
                except (BrokenPipeError, ConnectionResetError):
                    pass
            finally:
                if "record" in locals():
                    save(f"{record['id']:03d}" + ".receipt.json", record, receipt=True)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", secret, records
    finally:
        with lock:
            closing = True
            futures = list(pending)
        for future in futures:
            future.cancel()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
