"""R1.2: the smallest answer-evidence binding the run can verify by program.

An ``[E#]`` handle only says a span was delivered; it does not say the span
supports the sentence that cites it.  This module adds a bounded, structured
binding to the answer the model already submits (no extra unconditional call)
and verifies exactly three things by program:

1. **delivery** - every bound handle belongs to this run's delivered-material
   ledger, and the quote really occurs inside that handle's delivered text
   (an existence check over the run's own delivery, *not* an entailment proof);
2. **derivation** - a binding declared ``derived`` must name a computation the
   run actually produced (``table_compute`` result / ``C<n>`` id), so a number
   worked out by hand cannot be presented as a program result;
3. **stated identity** - when both the answer phrase and the cited text state a
   single year, they must agree; a disagreement is a concrete conflict, not a
   semantic judgement.

Everything else stays explicitly uncertain: the conflict payload says which of
the checks failed and never claims that the remaining bindings were *entailed*.

F4 adds three bounded rules to the same final answer entry point:

4. **declared propositions** - an answer declared ``complete`` must declare a
   binding for every assertive proposition it states, in that very answer.  "No
   bindings at all" is not a way to opt out of the contract: it is one bounded
   correction, and a second silent answer is carried as uncertainty.  A binding
   covers a proposition when its exact declared spans collectively cover all
   content except punctuation and explicit conjunctions; a name alone cannot
   cover a sentence;
5. **the quote is required** - a ``direct``/``bridge`` binding without a
   verbatim quote from one of its own handles proves only that a handle was
   read, which the citation gate already knew.  ``derived`` bindings are
   exempt: their program-computation id is the check;
6. **determinable scope** - when the claim and the quoted text each state one
   period/year, unit base, metric or direction, they must agree.  The payload
   names which dimension conflicted.  Subject identity, alias equivalence and
   arbitrary entailment stay *undetermined* on purpose: this module never
   performs a semantic judgement, and says so instead of implying one.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from pydantic import BaseModel, ConfigDict, Field, field_validator
from .numeric_consistency import answer_handles

# The bindings ride on the final answer turn that already happens, so the cap
# only has to fit inside one answer: 8 claims x (1..4 handles + one quote).
MAX_ANSWER_BINDINGS = 8
MAX_BINDING_QUOTE = 400
MAX_BINDING_CLAIM = 300

_YEAR = re.compile(r'(?<!\d)(?:19|20)\d{2}(?!\d)')

#: Bound on the propositions one answer may declare (same order as the binding
#: cap: retain one overflow sentinel so a longer answer is explicitly rejected
#: rather than silently checking a prefix).
MAX_DECLARED_PROPOSITIONS = 8
MAX_REPORTED_PROPOSITIONS = 6
#: Bound on how many failed claims the final answer may rewrite.  Same spirit
#: as the note bound: mark what can be read, never grow without limit.
MAX_UNVERIFIED_MARKS = 3

_PROPOSITION_SPLIT = re.compile(r'(?<=[。；;!?！？])\s*|(?<=\.)\s+(?=[A-Z0-9(\[])')
_CONTENT = re.compile(r'[^\s\W_]', re.UNICODE)
_UNIT_TOKEN = re.compile(
    r'\s*(%|％|百分点|个百分点|万|萬|千|百|亿|億|百万|千万|十亿|美元|港元|欧元|日元|元|'
    r'thousand|million|billion|dollars|USD|RMB|CNY|HKD|EUR|percent|people|employees|shares)',
    re.I)
_UP = re.compile(r'上升|增加|增长|提高|增长至|rise|rises|rose|increase[sd]?|grew|grow(?:th)?\b', re.I)
_DOWN = re.compile(r'下降|减少|降低|下跌|减少至|decline[sd]?|decrease[sd]?|fell|drop(?:ped)?|lower\b', re.I)


def declared_propositions(answer: str) -> List[str]:
    """The assertive propositions of one answer, bounded and in order.

    A proposition is a sentence (Chinese/ASCII terminators) that carries
    content.  Splitting is deliberately shallow: it exists so coverage can be
    checked sentence by sentence, not to parse the answer.
    """
    found: List[str] = []
    for segment in _PROPOSITION_SPLIT.split(answer or ""):
        text = (segment or "").strip()
        if text and _CONTENT.search(text) and not _HANDLE_ONLY.match(text):
            found.append(text)
        if len(found) > MAX_DECLARED_PROPOSITIONS:
            break
    return found


_HANDLE_ONLY = re.compile(r'^(?:\[E[1-9][0-9]*\][\s,，、]*)+$')


def _normalized(text: str) -> str:
    return re.sub(r'\s+', '', text or '')


# These are formatting/coordination tokens only. Any unbound entity, number,
# negation, comparison or other content remains a coverage conflict. This is
# syntactic declaration coverage, never proof of the relationship's entailment.
_BINDING_JOIN = re.compile(r"(?:[\s.,，、;；:：!?！？。()（）]|and|or|而|并且|以及|和|与|因此|所以)*", re.I)


def _covered_by_claims(proposition: str, claims: Sequence[str]) -> bool:
    """Union exact declared spans; a compound sentence may have two bindings."""
    def content(text: str) -> str:
        return _normalized(re.sub(r"\[E[1-9][0-9]*\]", "", text))
    text = content(proposition)
    covered = [False] * len(text)
    for claim in claims:
        fragment = content(claim).strip("。；;!?！？. ")
        if not fragment:
            continue
        if text.strip("。；;!?！？. ") in fragment:
            return True
        start = 0
        while True:
            start = text.find(fragment, start)
            if start < 0:
                break
            covered[start:start + len(fragment)] = [True] * len(fragment)
            start += len(fragment)
    if not any(covered):
        return False
    gaps = re.findall(r"[^\0]+", "".join("\0" if hit else char
                       for char, hit in zip(text, covered)))
    return all(_BINDING_JOIN.fullmatch(gap) for gap in gaps)


def _metric_scope(text: str, labels: Sequence[str] = ()) -> set:
    """Match only literal labels explicitly supplied from source metadata.

    No built-in domain aliases: equivalence and absent labels are undetermined.
    """
    lowered = (text or "").casefold()
    found = set()
    for label in labels:
        if not isinstance(label, str) or not label.strip():
            continue
        name = label.strip().casefold()
        pattern = (r"(?<![a-z0-9_])" + re.escape(name) + r"(?![a-z0-9_])"
                   if name.isascii() else re.escape(name))
        if re.search(pattern, lowered):
            found.add(label)
    return found


def _unit_bases(text: str) -> set:
    from .numeric_operands import declared_unit
    from .numeric_units import unit_spec

    bases = set()
    for number in re.finditer(r'[-+−]?\d[\d,]*(?:\.\d+)?', text or ""):
        tail = _UNIT_TOKEN.match((text or "")[number.end():])
        if not tail:
            continue
        unit = declared_unit(number[0] + tail[0])
        spec = unit_spec(unit)
        if spec and spec[0]:
            bases.add(spec[0])
    return bases


def _directions(text: str) -> set:
    found = set()
    if _UP.search(text or ""):
        found.add("up")
    if _DOWN.search(text or ""):
        found.add("down")
    return found


def scope_conflicts(claim: str, quote_text: str, *,
                    metric_labels: Sequence[str] = ()) -> List[Dict[str, Any]]:
    """Program-checkable scope disagreements between a claim and its quote.

    Only *determinable* dimensions are judged: a dimension is skipped when
    either side states more (or fewer) than one value for it, because then the
    comparison itself is undetermined.  Subject identity and semantic
    equivalence are never judged here.
    """
    conflicts: List[Dict[str, Any]] = []
    claimed_years, quoted_years = _delivered_years(claim), _delivered_years(quote_text)
    if len(claimed_years) == 1 and quoted_years and claimed_years[0] not in quoted_years:
        conflicts.append({"dimension": "period", "claim": claimed_years[0],
                          "quote": sorted(quoted_years)[:4]})
    claimed_units, quoted_units = _unit_bases(claim), _unit_bases(quote_text)
    if len(claimed_units) == 1 and len(quoted_units) == 1 and claimed_units != quoted_units:
        conflicts.append({"dimension": "unit", "claim": sorted(claimed_units)[0],
                          "quote": sorted(quoted_units)[0]})
    claimed_metrics, quoted_metrics = _metric_scope(claim, metric_labels), _metric_scope(quote_text, metric_labels)
    if len(claimed_metrics) == 1 and len(quoted_metrics) == 1 \
            and claimed_metrics != quoted_metrics:
        conflicts.append({"dimension": "metric", "claim": sorted(claimed_metrics)[0],
                          "quote": sorted(quoted_metrics)[0]})
    claimed_directions, quoted_directions = _directions(claim), _directions(quote_text)
    if len(claimed_directions) == 1 and len(quoted_directions) == 1 \
            and claimed_directions != quoted_directions:
        conflicts.append({"dimension": "direction", "claim": sorted(claimed_directions)[0],
                          "quote": sorted(quoted_directions)[0]})
    return conflicts


def _binding_handles(values: Any) -> List[str]:
    """Canonicalize an evidence ID, never invent or loosely search for one."""
    found = []
    for value in ([values] if isinstance(values, str) else (values or ())):
        match = re.fullmatch(r"(E[1-9][0-9]*)|\[(E[1-9][0-9]*)\]", str(value).strip())
        if match:
            handle = "[{}]".format(match[1] or match[2])
            if handle not in found:
                found.append(handle)
    return found


class AnswerBinding(BaseModel):
    """One sentence of the answer, the handles it rests on and how."""

    claim: str = Field(min_length=1, max_length=MAX_BINDING_CLAIM,
                       description="Copy a complete assertion clause exactly from answer, including its numbers and entities. All clauses must be declared; keep the answer concise. A compound sentence may use several bindings covering all its content.")
    handles: List[str] = Field(default_factory=list, max_items=4,
                               description="The [E#] handles this phrase rests on, copied from the evidence you read.")
    relation: str = Field(default="direct", description="direct | derived | bridge")
    source_quote: str = Field(default="", max_length=MAX_BINDING_QUOTE,
                              description="Text copied verbatim from one bound handle that states this phrase.")
    derivation: str = Field(default="", description="For relation=derived: the table_compute computation_id (C1) or step_id that produced the value.")

    model_config = ConfigDict(extra="forbid")

    @field_validator("handles")
    @classmethod
    def canonical_handles(cls, values: List[str]) -> List[str]:
        for value in values:
            if not _binding_handles([value]):
                raise ValueError("binding handle must be E<number> or [E<number>]")
        return _binding_handles(values)

    def handle_list(self) -> List[str]:
        return _binding_handles(self.handles)


def _ledger_text(state: Mapping[str, Any], handle: str) -> Optional[str]:
    """The delivered text of one handle, or ``None`` when it was never delivered."""
    from .numeric_operands import _excerpt_for

    excerpt = _excerpt_for(state, handle)
    if not excerpt:
        return None
    parts = [excerpt]
    query_state = state.get("query_state")
    for evidence_id in getattr(query_state, "seen_evidence_order", ()) or ():
        if query_state.evidence_handle(evidence_id) != handle:
            continue
        record = query_state.seen_evidence_by_id.get(evidence_id)
        claim_id = str(getattr(record, "claim_id", ""))
        ref = (state.get("table_evidence_refs") or {}).get(claim_id) or {}
        if ref.get("kind") != "source_span":
            break
        for view in state.get("read_views") or ():
            if any(view.get(left) != ref.get(right) for left, right in (
                    ("source_id", "source_id"), ("article_path", "path"),
                    ("revision_id", "revision_id"), ("spans", "spans"))):
                continue
            blocks = [str(block.get("body") or "") for block in view.get("blocks") or ()]
            body = "\n".join(blocks)
            if hashlib.sha256(body.encode("utf-8")).hexdigest() == ref.get("body_sha256"):
                # Keep disjoint blocks separate: no fabricated quote may bridge
                # a range that the read never delivered.
                parts.extend(blocks)
        break
    try:
        payload = json.loads(excerpt)
    except (ValueError, TypeError):
        payload = None
    if isinstance(payload, Mapping):
        parts.extend(str(value) for value in payload.values()
                     if isinstance(value, str) and value)
    return "\n".join(parts)


def _delivered_years(text: str) -> List[str]:
    return list(dict.fromkeys(_YEAR.findall(text or "")))


def _derivation_ids(state: Mapping[str, Any]) -> set:
    found = set()
    for entry in state.get("table_compute_results") or ():
        payload = entry.get("payload") if isinstance(entry, Mapping) else None
        payload = payload if isinstance(payload, Mapping) else (entry if isinstance(entry, Mapping) else {})
        for key in ("computation_id", "step_id"):
            if payload.get(key):
                found.add(str(payload[key]))
        for step in payload.get("steps") or ():
            if isinstance(step, Mapping) and step.get("step_id"):
                found.add(str(step["step_id"]))
    return found


def verify_answer_bindings(
    answer: str,
    bindings: Sequence[Any],
    state: Mapping[str, Any],
    *,
    require_declared_propositions: bool = True,
) -> Optional[Dict[str, Any]]:
    """Return the first concrete binding conflict, or ``None`` when all pass.

    ``None`` means every *declared* proposition was declared, its handles were
    delivered this run, its quote (or program computation) checks out, and the
    determinable scope dimensions agree.  It never means the prose was entailed:
    subject identity and semantic equivalence stay undetermined, and the payload
    says so.

    An answer that declares no binding for its propositions is a conflict here,
    not a pass: the contract is bounded, but it is not optional.  The coverage
    requirement (and only that requirement) is skipped for an answer the model
    itself declared incomplete: that answer is already carried as uncertainty,
    and the contract governs complete verdicts.
    """
    answer = answer or ""
    ledger = state.get("query_state")
    derivations = _derivation_ids(state)
    reported: List[Dict[str, Any]] = []
    seen_claims = set()
    for binding in bindings:
        item = binding.model_dump() if isinstance(binding, BaseModel) else dict(binding)
        claim = str(item.get("claim") or "")
        relation = str(item.get("relation") or "direct")
        handles = _binding_handles(item.get("handles"))
        if relation not in ("direct", "derived", "bridge"):
            reported.append({"reason": "binding_relation_unknown", "claim": claim,
                             "relation": relation})
            continue
        if not claim or answer.count(claim) != 1:
            reported.append({"reason": "binding_claim_not_a_unique_answer_phrase",
                             "claim": claim})
            continue
        if claim in seen_claims:
            reported.append({"reason": "binding_claim_duplicated", "claim": claim})
            continue
        seen_claims.add(claim)
        if not handles:
            reported.append({"reason": "binding_handles_empty", "claim": claim})
            continue
        quote = str(item.get("source_quote") or "").strip()
        delivered = {}
        for handle in handles:
            text = _ledger_text(state, handle)
            if text is None:
                reported.append({"reason": "binding_handle_not_delivered",
                                 "claim": claim, "handle": handle})
                continue
            delivered[handle] = text
        if not delivered:
            continue
        if relation == "derived":
            wanted = {str(value) for value in
                      re.findall(r'C[1-9][0-9]*|\S+', str(item.get("derivation") or ""))}
            if not (wanted & derivations):
                reported.append({"reason": "binding_derivation_not_a_run_result",
                                 "claim": claim, "derivation": item.get("derivation") or "",
                                 "known": sorted(derivations)[:8]})
                continue
        if relation != "derived" and not quote:
            # A handle alone says a span was read, which the citation gate
            # already checked.  Without the verbatim supporting text, a
            # "direct" binding proves nothing about the sentence.
            reported.append({"reason": "binding_quote_missing",
                             "claim": claim, "handles": sorted(delivered)})
            continue
        if quote:
            if not any(quote in text for text in delivered.values()):
                reported.append({"reason": "binding_quote_not_in_bound_handle",
                                 "claim": claim, "handles": sorted(delivered)})
                continue
        else:
            # Without a quote the only program-checkable link left is the year
            # the phrase states: a single stated year that no bound handle
            # states at all is a concrete identity conflict.
            stated = _delivered_years(claim)
            if len(stated) == 1 and delivered:
                if not any(stated[0] in _delivered_years(text)
                           for text in delivered.values()):
                    reported.append({"reason": "binding_period_conflict",
                                     "claim": claim, "year": stated[0],
                                     "handles": sorted(delivered)})
                    continue
        if quote:
            matching = [text for text in delivered.values() if quote in text]
            stated = _delivered_years(claim)
            # A structured read may deliver the value and its heading in
            # separate bound units. Period context belongs to the whole
            # declared binding, not only the unit containing source_quote.
            # An explicit conflicting year in the quote is still rejected
            # independently by scope_conflicts below.
            if len(stated) == 1 and matching and \
                    not any(stated[0] in _delivered_years(text)
                            for text in delivered.values()):
                reported.append({"reason": "binding_period_conflict",
                                 "claim": claim, "year": stated[0],
                                 "handles": sorted(delivered)})
                continue
            # Determinable scope: period, unit base, metric and direction.  A
            # dimension either side states more than once is skipped -- the
            # comparison would be undetermined, and this check never guesses.
            scopes = scope_conflicts(claim, quote, metric_labels=state.get("source_metric_labels") or ())
            if scopes and matching:
                reported.append({"reason": "binding_scope_conflict",
                                 "claim": claim, "conflicts": scopes,
                                 "handles": sorted(delivered)})
                continue
    propositions = (declared_propositions(answer)
                    if require_declared_propositions else [])
    claims = [str((binding.model_dump() if isinstance(binding, BaseModel)
                   else dict(binding)).get("claim") or "") for binding in bindings]
    uncovered = [proposition for proposition in propositions
                 if not _covered_by_claims(proposition, claims)]
    if len(propositions) > MAX_DECLARED_PROPOSITIONS:
        reported.append({"reason": "binding_proposition_limit_exceeded",
                         "claim": propositions[-1][:MAX_BINDING_CLAIM],
                         "proposition_cap": MAX_DECLARED_PROPOSITIONS})
    if uncovered:
        reported.append({
            "reason": "binding_declared_propositions_uncovered",
            "declared_bindings": len(bindings),
            "propositions": [item[:MAX_BINDING_CLAIM] for item in
                             uncovered[:MAX_REPORTED_PROPOSITIONS]],
            "uncovered_count": len(uncovered),
            "proposition_cap": MAX_DECLARED_PROPOSITIONS,
        })
    if not reported:
        return None
    return {
        "rule_version": "answer-binding-v2",
        "reason": reported[0]["reason"],
        "conflicts": reported[:MAX_ANSWER_BINDINGS],
        "scope_dimensions": ["period", "unit", "direction"],
        "literal_metric_labels": list(state.get("source_metric_labels") or ()),
        "undetermined_dimensions": ["subject_identity", "metric_semantics", "alias_equivalence",
                                    "semantic_entailment"],
        "note": ("这些绑定只做了可程序验证的事：每条已声明结论都有绑定、句柄属于本次已交付文本、"
                 "直接引用有落在该句柄内的原文（派生值对应本次程序结果）、以及可判定的"
                 "期间/单位/方向范围；指标只比较显式提供的来源原始标签。主体同一性、指标语义、别名等价与语义蕴含仍未判定："
                 "请补齐引文或程序结果，或把该部分明确标为待核实。"),
        "message": _correction_message(reported[:MAX_ANSWER_BINDINGS], state),
    }


def _correction_message(conflicts: Sequence[Mapping[str, Any]],
                        state: Mapping[str, Any]) -> str:
    feedback = []
    for item in conflicts:
        detail = "{} ({})".format(item.get("reason"), item.get("claim") or "")
        if item.get("propositions"):
            detail += " 未绑定的完整句子：" + json.dumps(
                item["propositions"], ensure_ascii=False)
        if item.get("reason") in ("binding_quote_missing", "binding_quote_not_in_bound_handle"):
            examples = []
            for handle in (item.get("handles") or ())[:4]:
                text = _ledger_text(state, handle)
                if not text:
                    continue
                try:
                    cell = json.loads(text.split("\n", 1)[0])
                except (ValueError, TypeError):
                    cell = None
                if isinstance(cell, Mapping):
                    value = cell.get("value", cell.get("text"))
                    if isinstance(value, str) and value:
                        text = value
                examples.append({"handle": handle, "delivered_excerpt": text[:MAX_BINDING_QUOTE]})
            if examples:
                detail += " 已交付引文片段：" + json.dumps(examples, ensure_ascii=False)
        feedback.append(detail)
    return ("ANSWER_BINDING_CONFLICT: " + "; ".join(feedback)
        + "。请只保留回答问题必需的结论，删除未建立依据的背景、重复句和来源评论。"
          "answer_bindings.claim 应逐字复制完整断言短句（可省略句尾引文标记），"
          "一句含多条断言时可分别声明，合起来须覆盖全部内容；一个名字不能代替整句。"
          "最多八句，删除未绑定的总结句。handles 用 [E1] 这样的已交付句柄；"
          "source_quote 逐字复制该句柄的交付文本，表格不要把不同格子的文字拼成一条引文。"
          "派生值使用本次 table_compute 结果；无法建立依据的部分请标 incomplete。")


__all__ = ["AnswerBinding", "MAX_ANSWER_BINDINGS", "MAX_DECLARED_PROPOSITIONS",
           "MAX_UNVERIFIED_MARKS", "declared_propositions", "scope_conflicts",
           "verify_answer_bindings"]
