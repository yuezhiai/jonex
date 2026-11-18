"""Search and read progress observation for the direct-file Agent."""

from collections import deque
import hashlib
import json
from pathlib import Path
import re
import shlex


_ADVICE = {
    'batch_search': (
        'The same filter is returning no matches on successive individual files. Use a single batch '
        'search across the relevant originals with file names and line numbers, or a ranked lookup '
        'scoped to the subject. Open the strongest candidates and check the actual missing relation. '
        'Keep searching if the batch reveals new leads.'
    ),
    'exhausted_pages': (
        'Successive later windows of the same search stream are empty. Do not request more pages of '
        'that stream. Check the available evidence for each hop. If needed, run a different search '
        'using an established name or a different source, then give the supported findings and any '
        'remaining gap with file locations.'
    ),
    'replan': (
        'Several searches or reads returned material already seen. Briefly identify the relations '
        'established with file locations, the missing relation, and what has already been checked. '
        'Change what you are looking for, such as using a term from the confirmed passage '
        'to look for the missing relation in another original. Changing only the output window '
        'repeats the same search. Do not guess. If the gap remains, put the confirmed facts, missing '
        'hop, checked locations and useful next leads in the final answer and method_notes so another '
        'attempt can resume.'
    ),
    'repeat_command': (
        'The same search or read command has repeatedly returned the same output or error. '
        'Inspect its earlier result and any error, then change the search, source or working '
        'directory if that can resolve the missing evidence. Do not repeat the identical command. '
        'If no useful next step remains, answer with the supported facts and the specific gap.'
    ),
    'stop': (
        'After replanning, searches are still returning the same material, empty results or errors. End this '
        'attempt without further searches. Return the confirmed facts with file locations, the '
        'specific missing relation, checked locations and possible next leads in answer and '
        'method_notes. Mark incomplete unless every required relation is supported. Do not guess, and '
        'do not claim the whole publication has no answer.'
    ),
}


_PAGED_SEARCH = re.compile(
    r"^(?P<stream>\s*(?:rg|grep)\b[^\r\n;&]*?)\s*\|\s*sed\s+-n\s+"
    r"(?P<quote>['\"])(?P<start>[1-9][0-9]{0,11})(?:,[1-9][0-9]{0,11})?p(?P=quote)\s*$"
)
_OUTPUT_WINDOW = re.compile(
    r"(?:head|tail)(?:\s+-(?:[nc]\s*)?[-+]?\d+)?"
    r"|cut\s+-[bc]\s*[-,0-9]+"
    r"|sed\s+-n\s+(?P<quote>['\"]?)\d+(?:,\d+)?p(?P=quote)"
)


def _shell_result(output):
    header, sep, body = output.partition("\nOutput:\n")
    if not sep:
        return None, output
    status = re.search(r"Process exited with code ([0-9]+)", header)
    wrapped = re.match(r"Exit code: ([0-9]+)\nWall time: [^\n]*\nOutput:\n", body)
    if wrapped:
        return int(wrapped[1]), body[wrapped.end() :]
    return (int(status[1]) if status else None), body


def _empty_search_family(command, status):
    """Detect operational repetition while keeping distinct search predicates distinct."""
    page = _PAGED_SEARCH.fullmatch(command) if status == 0 else None
    if page:
        return "page", page["stream"].strip(), int(page["start"])
    if status != 1 or not re.match(r"^\s*(?:rg|grep)\b", command) or re.search(r"[\r\n]", command):
        return None
    try:
        args = shlex.split(command)
    except ValueError:
        return None
    paths = []
    while len(args) > 2 and re.fullmatch(r"(?:[\w./-]+/)[\w.-]+\.(?:md|txt|jsonl?)", args[-1]):
        paths.append(Path(args.pop()).parent.as_posix())
    if paths and len(set(paths)) == 1 and not any(arg in {"|", ";", "&", "&&", "||", ">", "<"} for arg in args):
        return "files", (tuple(args), paths[0]), None
    return None


def _empty_query_key(command, workdir, output):
    status, body = _shell_result(output)
    if status not in {0, 1} or body.strip() or "\n" in command:
        return command, workdir
    lexer = shlex.shlex(command, posix=False, punctuation_chars="|&;<>")
    lexer.whitespace_split = True
    lexer.commenters = ""
    try:
        tokens = list(lexer)
    except ValueError:
        return command, workdir
    pipe = tokens.index("|") if "|" in tokens else len(tokens)
    if any(token in {"&", "&&", "||", ";", "<", ">", ">>", "|&"} for token in tokens[:pipe]):
        return command, workdir
    # An empty output window on the same search cannot reveal a new predicate
    # or source. Keep changed search filters and working directories distinct.
    if (tokens and tokens[0] in {"rg", "grep"} and
            (pipe == len(tokens) or _OUTPUT_WINDOW.fullmatch(" ".join(tokens[pipe + 1:])))):
        return tuple(tokens[:pipe]), workdir
    return command, workdir


def _observable_command(command):
    """Limit identical-turn termination to commands used for search and reading.

    The publication is immutable, but scripts may use scratch state, so this
    is only a progress heuristic: warn first, and report an unfinished answer
    if support is still missing. A changed command, working directory, output,
    or continuation resets the separate identical-turn counter.
    """
    try:
        args = shlex.split(command)
        if (len(args) == 3 and Path(args[0]).name in {"bash", "sh"}
                and args[1] in {"-c", "-lc"}):
            command = args[2]
        if re.search(r"[\r\n`$]", command):
            return False
        lexer = shlex.shlex(command, posix=True, punctuation_chars="|;&<>")
        lexer.whitespace_split = True
        lexer.commenters = ""
        args = list(lexer)
    except ValueError:
        return False
    if not args or any(token != "|" and token and all(char in "|;&<>" for char in token)
                       for token in args):
        return False
    stages = []
    while "|" in args:
        at = args.index("|")
        stages.append(args[:at])
        args = args[at + 1:]
    stages.append(args)
    if not stages[0] or any(not stage or not _OUTPUT_WINDOW.fullmatch(" ".join(stage))
                            for stage in stages[1:]):
        return False
    args = stages[0]
    name = Path(args[0]).name
    if name == "find" and any(arg.startswith(("-exec", "-ok", "-delete", "-fprint", "-fls"))
                              for arg in args[1:]):
        return False
    if name in {"rg", "grep"} and any(arg.startswith("--pre") for arg in args[1:]):
        return False
    if name == "sed" and (len(args) < 3 or args[1] != "-n" or
                          not re.fullmatch(r"[0-9]+(?:,[0-9]+)?p", args[2])):
        return False
    return bool(re.fullmatch(r"python(?:3(?:\.\d+)?)?", name)) or name in {
        "wiki-find", "rg", "grep", "find", "cat", "head", "tail", "sed",
        "nl", "jq",
    }


class SearchProgress:
    """Notice repeated material, then allow one replanning phase before stopping.

    This is deliberately an observable-output heuristic, not a claim that a
    relation does or does not exist. Repeated scans of different files get
    batching advice because those files may be useful. Identical search/read
    commands with identical output get a separate warning and, only after
    more identical turns, a recoverable stop.
    """

    def __init__(self, *, reasoning_effort="off"):
        self.reasoning_effort = reasoning_effort
        self.calls = {}
        self.processed = set()
        self.seen = set()
        self.empty_queries = set()
        self.window = deque(maxlen=12)
        self.warned = False
        self.stopped = False
        self.warnings = 0
        self.fresh_streak = 0
        self.identical_turns = 0
        self.empty_run = None
        self.advised_searches = set()
        self.latest_chat = None

    @staticmethod
    def _fingerprints(command, output):
        if not re.search(r"\b(rg|grep|cat|sed|head|tail|jq|python|nl|wiki-find)\b", command):
            return None
        if re.search(r"\brg\s+--files\b", command):
            return None
        _, output = _shell_result(output)
        if re.search(r"\bwiki-find\b", command):
            try:
                ranked = json.loads(output)
            except ValueError:
                ranked = None
            if isinstance(ranked, dict) and isinstance(ranked.get("hits"), list):
                groups = (ranked.get(key) for key in ("hits", "authored_articles", "independent_sources",
                                                     "source_sections", "contextual_claims"))
                return {
                    hashlib.sha256(json.dumps(hit, sort_keys=True).encode()).digest()
                    for group in groups if isinstance(group, list)
                    for hit in group
                    if isinstance(hit, dict) and hit.get("path")
                }
        hashes = set()
        has_content = False
        for line in output.splitlines():
            line = line.strip()
            if not line or re.search(
                r"(^|\s)(?:error|no such file|permission denied|unmatched|argument list too long)",
                line,
                re.I,
            ):
                continue
            match = re.match(r"^(.*?\.(?:md|jsonl?|txt|csv|tsv)):\d+[:-]", line, re.I)
            if match:
                hashes.add(hashlib.sha256(("path:" + match.group(1)).encode()).digest())
                line = line[match.end() :]
            line = re.sub(r"^\s*\d+[:\t ]+", "", line)
            line = re.sub(r"\s+", " ", line).strip().lower()
            if line:
                has_content = True
                hashes.add(hashlib.sha256(line.encode()).digest())
        if has_content:
            for path in re.findall(
                r"(?<![\w.-])(?:[\w.-]+/)*[\w.-]+\.(?:md|jsonl?|txt|csv|tsv)\b", command
            ):
                hashes.add(hashlib.sha256(("path:" + path).encode()).digest())
        return hashes

    def _empty_run_action(self, command, workdir, output):
        status, body = _shell_result(output)
        family = _empty_search_family(command, status) if not body.strip() else None
        if family is None:
            self.empty_run = None
            return None
        kind, stream, start = family
        key = (kind, stream, workdir)
        previous = self.empty_run
        same = previous and previous[0] == key and (start is None or start >= previous[1])
        count = previous[2] + 1 if same else 1
        self.empty_run = (key, start, count)
        if count < 6:
            return None
        if key in self.advised_searches:
            return "stop" if kind == "page" else None
        self.advised_searches.add(key)
        self.empty_run = None
        return "exhausted_pages" if kind == "page" else "batch_search"

    def _apply_action(self, request, action):
        self.window.clear()
        self.fresh_streak = 0
        if action == "stop":
            self.stopped = True
        else:
            self.warnings += 1
            # Outputs already delivered together must not count as attempts
            # made after the model received the advice.
            self.processed.update(
                x.get("call_id")
                for x in request.get("input") or []
                if isinstance(x, dict) and x.get("type") == "function_call_output"
            )
        return action

    def observe(self, request, *, exact_repeats=()):
        new_results = set()
        action = None
        for item in request.get("input") or []:
            if not isinstance(item, dict):
                continue
            kind = item.get("type")
            ident = item.get("call_id")
            if kind == "function_call" and item.get("name") in {
                "exec_command",
                "write_stdin",
            }:
                try:
                    arguments = json.loads(item.get("arguments") or "{}")
                except (ValueError, TypeError):
                    arguments = {}
                if not isinstance(arguments, dict):
                    arguments = {}
                command, workdir = arguments.get("cmd"), arguments.get("workdir")
                self.calls[ident] = (
                    item.get("name"), command if isinstance(command, str) else "",
                    workdir if isinstance(workdir, str) else "",
                )
            elif (
                kind == "function_call_output"
                and ident not in self.processed
                and ident in self.calls
            ):
                self.processed.add(ident)
                new_results.add(ident)
                name, command, workdir = self.calls[ident]
                if name == "write_stdin":
                    # A continuation may contain new evidence; do not infer a stall from it.
                    self.window.clear()
                    self.warned = False
                    self.fresh_streak = 0
                    self.empty_run = None
                    continue
                output = item.get("output")
                output = (
                    output
                    if isinstance(output, str)
                    else json.dumps(output, ensure_ascii=False)
                )
                if action := self._empty_run_action(command, workdir, output):
                    break
                values = self._fingerprints(command, output)
                if values is None:
                    continue
                if values:
                    overlap = len(values & self.seen) / len(values)
                    no_new = values.issubset(self.seen)
                    self.seen.update(values)
                    repeated = overlap >= 0.9
                    self.fresh_streak = self.fresh_streak + 1 if overlap < 0.5 else 0
                    self.window.append((repeated, True, no_new))
                elif re.search(r"\b(rg|grep|jq)\b", command):
                    # A distinct negative search can help rule out a candidate.
                    key = _empty_query_key(command, workdir, output)
                    no_new = key in self.empty_queries
                    self.empty_queries.add(key)
                    self.window.append((no_new, False, no_new))
                    self.fresh_streak = 0 if no_new else self.fresh_streak + 1
                else:
                    continue
                if self.warned and self.fresh_streak >= 4:
                    self.warned = False
                    self.window.clear()
                first_warning = (
                    not self.warned
                    and sum(repeated for repeated, _, _ in self.window) >= 8
                )
                continued_repeat = self.warned and all(
                    no_new for _, _, no_new in self.window
                )
                if (
                    len(self.window) == 12
                    and (first_warning or continued_repeat)
                    and sum(
                        repeated and nonempty for repeated, nonempty, _ in self.window
                    )
                    >= 3
                ):
                    action = "stop" if self.warned else "replan"
                    self.warned = True
                    break
        if action:
            self.identical_turns = 0
            return self._apply_action(request, action)
        if new_results:
            # Count model turns with only repeated completed commands. One
            # tool batch is one attempt and any new result resets the count.
            identical = new_results.issubset(exact_repeats) and all(
                self.calls[ident][0] == "exec_command"
                and _observable_command(self.calls[ident][1])
                for ident in new_results
            )
            self.identical_turns = self.identical_turns + 1 if identical else 0
            if self.identical_turns >= 4:
                if self.warned:
                    return self._apply_action(request, "stop")
                self.warned = True
                self.identical_turns = 0
                self.empty_run = None
                return self._apply_action(request, "repeat_command")
        return None

    def apply(self, request, chat, *, exact_repeats=()):
        """Apply the current query's advice to its next gateway request."""
        if self.reasoning_effort in {"high", "xhigh"}:
            chat["thinking"] = {"type": "enabled"}
            chat["reasoning_effort"] = self.reasoning_effort
        else:
            chat["thinking"] = {"type": "disabled"}
            chat.pop("reasoning_effort", None)
        action = self.observe(request, exact_repeats=exact_repeats)
        if action is not None:
            chat["messages"].append({"role": "system", "content": _ADVICE[action]})
        if action == "stop":
            chat.pop("tools", None)
            chat["tool_choice"] = "none"
        self.latest_chat = chat
        return action
