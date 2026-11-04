"""Numeric operands bound to evidence the run actually delivered.

S3.1.  A number a model wants to compute with is only usable if it can be read
back from a position the run delivered: a table cell that carries its table
identity, revision and coordinates, or a character range inside a delivered
excerpt.  The number is parsed *from that position* - a value the model types is
never taken - and every interpretation the arithmetic depends on (metric,
period, unit, subject scope) is either proven by the run's own records or listed
as explicitly missing.  Nothing is defaulted: not the current year, not a total
row, not a unit.

Two separations are deliberate and are what the S2 review asked to keep:

* a *raw* coordinate read proves its stored content and nothing else - the
  period, the unit and the metric need their own grounds;
* one number inside a longer excerpt needs its position: when the same literal
  occurs more than once, an operand that names no range is refused instead of
  silently taking the first occurrence.
"""
from __future__ import annotations

import json
import re
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .numeric_units import inline_unit, unit_note, unit_spec, number_range_valid

OPERAND_RULE_VERSION = "numeric-operands-v2"

# Scales a *source* may declare in its own text.  A scale is read from the text
# next to the number (or from an explicit rule the operation itself defines);
# it is never supplied as a bare multiplier by a caller.
SCALE_FACTORS: Dict[str, Decimal] = {
    "%": Decimal("0.01"), "％": Decimal("0.01"),
    "千": Decimal("1000"), "百": Decimal("100"), "万": Decimal("10000"),
    "十万": Decimal("100000"), "百万": Decimal("1000000"), "千万": Decimal("10000000"),
    "亿": Decimal("100000000"), "十亿": Decimal("1000000000"),
    "thousand": Decimal("1000"), "million": Decimal("1000000"),
    "billion": Decimal("1000000000"), "k": Decimal("1000"),
}
# Only these may be used as a ``rule`` constant: the operation's own fixed
# definitions, never a financial coefficient a model chose.
RULE_CONSTANTS: Dict[str, Decimal] = {
    "percent": Decimal("100"), "percentage_points": Decimal("100"),
    "permille": Decimal("1000"), "unity": Decimal("1"),
}
CURRENCY_UNITS = ("元", "人民币", "美元", "港元", "欧元", "日元", "CNY", "RMB", "USD",
                  "HKD", "EUR", "JPY", "$", "¥", "€", "£")
NUMBER = re.compile(r"[-+]?\d[\d,]*(?:\.\d+)?")
MISSING_KEYS = ("metric", "period", "unit", "subject_scope")


class OperandError(ValueError):
    """A refusal with the code the caller reports; never a silent fallback."""


class OperandRef(BaseModel):
    """One requested operand, in exactly one of the three supported forms.

    ``table_cell``/``text_span`` name delivered evidence; ``constant`` must be
    text that really occurs in the question (or a fixed rule of the operation).
    Grounds are handles for the interpretations the caller claims (for example
    the handle that states the period); a claimed ground that this run never
    delivered is refused, and an absent one becomes an explicit gap.
    """

    kind: str
    handle: Optional[str] = None
    char_start: Optional[int] = None
    char_end: Optional[int] = None
    source_text: Optional[str] = None
    rule: Optional[str] = None
    grounds: Dict[str, str] = Field(default_factory=dict)
    label: Optional[str] = None

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def _one_form(self) -> "OperandRef":
        if self.handle and re.fullmatch(r"E[0-9]+", self.handle):
            self.handle = "[" + self.handle + "]"
        self.grounds = {key: "[" + value + "]" if re.fullmatch(r"E[0-9]+", value)
                        else value for key, value in self.grounds.items()}
        if self.kind not in ("table_cell", "text_span", "constant"):
            raise OperandError("OPERAND_KIND_UNSUPPORTED:" + str(self.kind))
        if self.kind == "constant":
            if not (self.source_text or self.rule):
                raise OperandError("OPERAND_CONSTANT_SOURCE_REQUIRED")
        else:
            if not (self.handle or "").strip():
                raise OperandError("OPERAND_HANDLE_REQUIRED")
        if self.kind == "text_span":
            if self.char_start is None or self.char_end is None:
                raise OperandError("OPERAND_SPAN_REQUIRED")
            if int(self.char_start) < 0 or int(self.char_end) <= int(self.char_start):
                raise OperandError("OPERAND_SPAN_INVALID")
        for key in self.grounds:
            if key not in MISSING_KEYS:
                raise OperandError("OPERAND_GROUND_UNSUPPORTED:" + str(key))
        return self


class ResolvedOperand(BaseModel):
    """What the run could prove about one operand, and what it could not."""

    label: str = ""
    kind: str
    value: Optional[Decimal] = None
    text: str = ""
    scale: Optional[str] = None
    metric: Optional[str] = None
    unit: Optional[str] = None
    period: Optional[str] = None
    subject_scope: Optional[str] = None
    missing: Tuple[str, ...] = ()
    evidence_handles: Tuple[str, ...] = ()
    table_id: Optional[str] = None
    revision_id: Optional[str] = None
    interpretation_id: Optional[str] = None
    row: Optional[int] = None
    column: Optional[int] = None
    char_start: Optional[int] = None
    char_end: Optional[int] = None
    reason: Optional[str] = None

    model_config = ConfigDict(extra="forbid")

    @property
    def usable(self) -> bool:
        return self.value is not None

    def as_payload(self) -> Dict[str, Any]:
        payload = self.model_dump(mode="json")
        payload["value"] = None if self.value is None else format_decimal(self.value)
        payload["usable"] = self.usable
        return payload


def format_decimal(value: Decimal, decimals: Optional[int] = None) -> str:
    """Canonical decimal text; rounding happens only here, at display time.

    Without ``decimals`` the normalized value is kept exactly: a computed 2.20
    is shown as 2.2 and never re-rounded by a financial convention.
    """
    if decimals is None:
        text = format(value.normalize(), "f")
        return "0" if text in ("-0", "") else text
    quantum = Decimal(1).scaleb(-int(decimals))
    from decimal import ROUND_HALF_UP
    return format(value.quantize(quantum, rounding=ROUND_HALF_UP), "f")


def parse_number(text: str) -> Tuple[Optional[Decimal], Optional[str], Optional[str]]:
    """Parse the single number in ``text``; return (value, scale, reason).

    More than one number, or none at all, is a refusal: the caller must name a
    narrower position instead of letting the engine pick one.
    """
    raw = str(text or "").strip().replace("−", "-").replace("–", "-")
    if not raw:
        return None, None, "OPERAND_EMPTY_TEXT"
    matches = list(NUMBER.finditer(raw))
    if not matches:
        return None, None, "OPERAND_NOT_NUMERIC"
    if len(matches) > 1:
        return None, None, "OPERAND_MULTIPLE_NUMBERS"
    match = matches[0]
    try:
        value = Decimal(match.group(0).replace(",", ""))
    except InvalidOperation:
        return None, None, "OPERAND_NOT_NUMERIC"
    # Financial parentheses belong to the number even when a written unit
    # follows the closing bracket, as in (23) million dollars.
    tail = raw[match.end():]
    if (re.fullmatch(r"[$¥€£]?\s*[（(]\s*[$¥€£]?\s*", raw[:match.start()])
            and re.match(r"\s*[）)]", tail)):
        value = -value
        tail = re.sub(r"^\s*[）)]", "", tail, count=1)
    scale = None
    for token in sorted(SCALE_FACTORS, key=len, reverse=True):
        # A boundary only makes sense after a word character: "10%" and "10%"-
        # style suffixes must match, and "10kg" must not match the "k" scale.
        boundary = (r"\b" if token[-1:].isascii() and token[-1:].isalnum() else "")
        if re.match(r"\s*" + re.escape(token) + boundary, tail, re.IGNORECASE):
            scale = token
            break
    return value, scale, None


# A percent sign is a *unit*, not a multiplier: 36.8% stays 36.8 with unit "%",
# so a difference between two percentages is measured in percentage points and a
# relative change is still computed against the base.  Magnitude words (万/千/
# 百万/亿/million) are multipliers and are applied to the value.
PERCENT_UNITS = ("%", "％", "百分点", "个百分点")


def scale_factor(scale: Optional[str]) -> Decimal:
    if not scale:
        return Decimal("1")
    return SCALE_FACTORS.get(str(scale).lower(), SCALE_FACTORS.get(scale, Decimal("1")))


def scale_multiplier(scale: Optional[str]) -> Decimal:
    """The multiplier a scale word contributes; a percent contributes none."""
    if scale in PERCENT_UNITS:
        return Decimal("1")
    return scale_factor(scale)


def percent_unit(scale: Optional[str]) -> Optional[str]:
    return "%" if scale in PERCENT_UNITS else None


_UNIT_NOTE = re.compile(r"单位\s*[:：]\s*([^\s，。；、）)]+)")


def declared_unit(text: str) -> Optional[str]:
    """The unit the source itself wrote: a unit note, or the word beside a number.

    A note that says ``单位：万元`` states 万元; a bare number written as
    ``1,000万元`` states 元.  Nothing is inferred from the number's magnitude.
    """
    return unit_note(str(text or "")) or inline_unit(str(text or ""))


def handle_claim_ids(query_state: Any) -> Dict[str, str]:
    """Map every delivered ``[E#]`` handle to the claim id it names."""
    by_handle: Dict[str, str] = {}
    for evidence_id in getattr(query_state, "seen_evidence_order", ()) or ():
        record = query_state.seen_evidence_by_id.get(evidence_id)
        if record is None:
            continue
        by_handle[query_state.evidence_handle(evidence_id)] = str(record.claim_id)
    return by_handle


def _excerpt_for(state: Mapping[str, Any], handle: str) -> Optional[str]:
    query_state = state.get("query_state")
    if query_state is None:
        return None
    for evidence_id in getattr(query_state, "seen_evidence_order", ()) or ():
        record = query_state.seen_evidence_by_id.get(evidence_id)
        if record is not None and query_state.evidence_handle(evidence_id) == handle:
            return str(record.excerpt)
    return None


def _grounds(ref: OperandRef, state: Mapping[str, Any],
             delivered: Mapping[str, str]) -> Tuple[Dict[str, str], List[str]]:
    proven, missing = {}, []
    target = state.get("table_evidence_refs", {}).get(delivered.get(str(ref.handle))) or {}
    for key in MISSING_KEYS:
        handle = str(ref.grounds.get(key) or "").strip()
        if not handle:
            continue
        if handle not in delivered:
            raise OperandError("OPERAND_GROUND_NOT_DELIVERED:{}:{}".format(key, handle))
        provenance = state.get("table_evidence_refs", {}).get(delivered[handle]) or {}
        if provenance and not _proves_interpretation(provenance):
            raise OperandError("OPERAND_GROUND_NOT_INTERPRETATION:{}".format(key))
        for identity in ("table_id", "revision_id", "source_id", "document_id"):
            if target.get(identity) and provenance.get(identity) != target[identity]:
                raise OperandError("OPERAND_GROUND_SCOPE_MISMATCH:" + key)
        # Free prose may only interpret itself unless a source-bound relation is available.
        if not target and handle != ref.handle:
            missing.append(key)
            continue
        text = _ground_text(state, delivered, handle) or ""
        if _semantic_ground(key, text) is None:
            missing.append(key)
            continue
        proven[key] = handle
    return proven, missing


def _semantic_ground(key: str, text: str) -> Optional[str]:
    if key == "unit":
        return unit_note(text)
    names = {"metric": "指标|metric", "period": "期间|年度|period",
             "subject_scope": "主体|口径|subject|scope"}
    match = re.search(r"(?:" + names[key] + r")\s*[:：]\s*([^;；。\n]+)", text, re.I)
    if match:
        return match[1].strip() or None
    if key == "period" and re.fullmatch(r"(?:Fiscal\s+|FY\s*)?(?:19|20)\d{2}年?", text.strip(), re.I):
        return text.strip()
    return None


def _proves_interpretation(ref: Mapping[str, Any]) -> bool:
    """A cell proves content; a note/caption may state a metric, period or unit.

    Delivered header/footnote text is exactly where a unit or a period is
    written, so a note handle can carry that interpretation.  A plain data cell
    cannot: it would only repeat the number it is supposed to explain.
    """
    kind = str(ref.get("kind") or "")
    if kind in ("table_note", "table_caption"):
        return True
    return bool(ref.get("note_id") or ref.get("is_note"))


def _check_unit_context(written: Optional[str], context: Optional[str]) -> None:
    if not written or not context:
        return
    left, right = unit_spec(written), unit_spec(context)
    if left is None or right is None or left[0] != right[0]:
        raise OperandError("OPERAND_UNIT_CONTEXT_CONFLICT")


def _unit_from_ground(state: Mapping[str, Any], delivered: Mapping[str, str],
                      handle: Optional[str]) -> Optional[str]:
    """The unit a delivered note ground states, if it states one."""
    if not handle:
        return None
    text = _ground_text(state, delivered, handle)
    if not text:
        return None
    return unit_note(text)


def _ground_text(state: Mapping[str, Any], delivered: Mapping[str, str],
                 handle: str) -> Optional[str]:
    """The text a delivered note handle states, for a claimed interpretation."""
    excerpt = _excerpt_for(state, handle)
    if not excerpt:
        return None
    try:
        payload = json.loads(excerpt)
    except (ValueError, TypeError):
        return excerpt
    if isinstance(payload, Mapping):
        return str(payload.get("text") or payload.get("value") or "") or None
    return excerpt


def _complete_reads(state: Mapping[str, Any], table_id: str) -> List[Mapping[str, Any]]:
    return [entry for entry in (state.get("table_reads_by_request") or {}).values()
            if entry.get("status") == "complete"
            and str(entry.get("table_id") or "") == str(table_id)]


def _cell_metric(state: Mapping[str, Any], table_id: str,
                 revision_id: Optional[str], row: int,
                 column: int) -> Tuple[Optional[str], Optional[str]]:
    """The delivered column label of a cell, from a complete structured read.

    A raw page keeps no ledger entry, so the same cell read raw has no proved
    metric - which is the point of the separation, not a gap in this code.
    """
    for entry in _complete_reads(state, table_id):
        if revision_id is not None and entry.get("source_version") not in (
                None, revision_id):
            continue
        cells = entry.get("seen_cells") or {}
        if (row, column) not in cells and "{}:{}".format(row, column) not in cells:
            continue
        for column_entry in entry.get("columns") or []:
            if int(column_entry.get("col", -1)) == column:
                label = str(column_entry.get("label") or "")
                return (label or None), str(entry.get("request_id") or "")
    return None, None


def resolve_operand(ref: OperandRef, state: Mapping[str, Any], *,
                    question: str = "") -> ResolvedOperand:
    """Resolve one operand against the run's own delivered evidence."""
    if ref.kind == "constant":
        return _resolve_constant(ref, question)
    if ref.kind == "text_span":
        return _resolve_text_span(ref, state)
    return _resolve_table_cell(ref, state)


def _resolve_constant(ref: OperandRef, question: str) -> ResolvedOperand:
    if ref.rule:
        value = RULE_CONSTANTS.get(str(ref.rule))
        if value is None:
            raise OperandError("OPERAND_RULE_UNKNOWN:" + str(ref.rule))
        return ResolvedOperand(label=ref.label or str(ref.rule), kind="constant",
                               value=value, text=str(ref.rule), scale=str(ref.rule),
                               reason="operation_rule")
    text = str(ref.source_text or "")
    positions = [m.start() for m in re.finditer(re.escape(text), question)] if text else []
    if not any(number_range_valid(question, pos, pos + len(text)) for pos in positions):
        raise OperandError("OPERAND_CONSTANT_NOT_IN_QUESTION")
    value, scale, reason = parse_number(text)
    if value is None:
        raise OperandError(str(reason))
    return ResolvedOperand(label=ref.label or text, kind="constant",
                           value=value * scale_multiplier(scale), text=text, scale=scale,
                           unit=declared_unit(text) or percent_unit(scale),
                           reason="question_text")


def _resolve_text_span(ref: OperandRef, state: Mapping[str, Any]) -> ResolvedOperand:
    handle = str(ref.handle or "")
    excerpt = _excerpt_for(state, handle)
    if excerpt is None:
        raise OperandError("OPERAND_HANDLE_NOT_DELIVERED:" + handle)
    start, end = int(ref.char_start), int(ref.char_end)
    if not number_range_valid(excerpt, start, end):
        raise OperandError("OPERAND_SPAN_OUT_OF_RANGE_OR_SPLIT_NUMBER")
    span = excerpt[start:end]
    value, scale, reason = parse_number(span)
    missing: List[str] = []
    if value is None and reason == "OPERAND_MULTIPLE_NUMBERS":
        raise OperandError("OPERAND_SPAN_AMBIGUOUS")
    if value is None:
        raise OperandError(str(reason))
    # The same number elsewhere in the same excerpt is a different position:
    # without a range the operand is refused rather than taking the first one.
    delivered = handle_claim_ids(state.get("query_state"))
    grounds, _ = _grounds(ref, state, delivered)
    # Include the adjacent written unit, but never another number or clause.
    suffix = re.match(r"\s*([%％\u3400-\u9fffA-Za-z]+)", excerpt[end:])
    inline = span + (suffix[0] if suffix else "")
    _v, adjacent_scale, _reason = parse_number(inline)
    scale = adjacent_scale or scale
    # An inline scale has already been applied to the value. A caption's
    # scaled unit must not multiply that value a second time.
    written_unit = declared_unit(inline) or percent_unit(scale)
    ground_unit = _unit_from_ground(state, delivered, grounds.get("unit"))
    _check_unit_context(written_unit, ground_unit)
    resolved_unit = written_unit or ground_unit
    for key in MISSING_KEYS:
        if key not in grounds and not (key == "unit" and resolved_unit):
            missing.append(key)
    return ResolvedOperand(label=ref.label or span, kind="text_span",
                           value=value * scale_multiplier(scale),
                           text=span, scale=scale,
                           unit=resolved_unit,
                           metric=_semantic_ground("metric", _ground_text(state, delivered, grounds["metric"]) or "") if grounds.get("metric") else None,
                           period=_semantic_ground("period", _ground_text(state, delivered, grounds["period"]) or "") if grounds.get("period") else None,
                           subject_scope=_semantic_ground("subject_scope", _ground_text(state, delivered, grounds["subject_scope"]) or "") if grounds.get("subject_scope") else None,
                           missing=tuple(missing), evidence_handles=tuple(dict.fromkeys((handle, *grounds.values()))),
                           char_start=start, char_end=end)


def _resolve_table_cell(ref: OperandRef, state: Mapping[str, Any]) -> ResolvedOperand:
    handle = str(ref.handle or "")
    delivered = handle_claim_ids(state.get("query_state"))
    claim_id = delivered.get(handle)
    if claim_id is None:
        raise OperandError("OPERAND_HANDLE_NOT_DELIVERED:" + handle)
    provenance = (state.get("table_evidence_refs") or {}).get(claim_id)
    if not provenance or str(provenance.get("kind")) != "table_cell":
        raise OperandError("OPERAND_HANDLE_NOT_A_TABLE_CELL:" + handle)
    table_id = str(provenance.get("table_id") or "")
    revision_id = provenance.get("revision_id")
    row = int(provenance.get("row"))
    column = int(provenance.get("column"))
    # A cell delivered by a complete structured read is the strongest form: its
    # column label proves the metric.  A cell that only a raw page (or a partial
    # read) delivered still carries its stored content, so it stays usable as
    # content while reporting the metric, period and unit it cannot prove.
    cell = _stored_cell(state, table_id, revision_id, row, column)
    structured = cell is not None
    if cell is None:
        cell = provenance.get("cell_snapshot")
    if cell is None:
        raise OperandError("OPERAND_CELL_NOT_DELIVERED:{}:{}:{}".format(
            table_id, row, column))
    text = str(cell.get("cached_value") if cell.get("formula") and cell.get(
        "cached_value") is not None else cell.get("raw_value") or "")
    value, scale, reason = parse_number(text)
    if value is None:
        raise OperandError(str(reason))
    metric, period = None, None
    label = None
    if structured:
        label, _request_id = _cell_metric(state, table_id, revision_id, row, column)
        if label:
            years = re.findall(r"(?<!\d)(?:19|20)\d{2}年?(?!\d)", label)
            if len(years) == 1:
                period = years[0]
            elif not years:
                metric = label
        # A numeric column label (often a mistaken header candidate) is not a
        # financial metric. Read the delivered row label before interpreting a
        # caption such as "millions, except per share amounts".
        if metric and not re.search(r"[A-Za-z\u3400-\u9fff]", metric):
            metric = None
        if period or metric is None:
            for entry in _complete_reads(state, table_id):
                cells = list((entry.get("seen_cells") or {}).values()) + list((entry.get("seen_context") or {}).values())
                labels = [str(c.get("raw_value") or "") for c in cells
                          if c.get("row_index") == row and c.get("column_index") == 1]
                if len(set(labels)) == 1 and labels and re.search(r"[A-Za-z\u3400-\u9fff]", labels[0]):
                    metric = labels[0]
                    break
    # Native full-table reads carry the same saved row labels and unit notes.
    # They do not need a second structured read to make that context usable.
    native_context = []
    if not structured and table_id in (state.get("delivered_tables") or set()):
        for h, cid in delivered.items():
            context_ref = (state.get("table_evidence_refs") or {}).get(cid) or {}
            if (context_ref.get("table_id") != table_id
                    or context_ref.get("revision_id") != revision_id):
                continue
            snapshot = context_ref.get("cell_snapshot") or {}
            text_value = str(snapshot.get("raw_value") or "")
            if (context_ref.get("kind") == "table_cell"
                    and context_ref.get("row") == row and context_ref.get("column") == 1
                    and re.search(r"[A-Za-z\u3400-\u9fff]", text_value)):
                metric = text_value
            if (context_ref.get("kind") == "table_note"
                    or (context_ref.get("kind") == "table_cell"
                        and int(context_ref.get("row") or 0) < row)):
                native_context.append((h, text_value or (_ground_text(state, delivered, h) or "")))
    grounds, _ = _grounds(ref, state, delivered)
    if metric is None and grounds.get("metric"):
        metric = _semantic_ground("metric", _ground_text(state, delivered, grounds["metric"]) or "")
    missing: List[str] = []
    if metric is None:
        missing.append("metric")
    inferred_units = set()
    inferred_unit_handles = []
    if structured:
        for entry in _complete_reads(state, table_id):
            if entry.get("source_version") != revision_id:
                continue
            for context in (entry.get("seen_context") or {}).values():
                if context.get("kind") not in ("header", "caption", "table_note", "footnote"):
                    continue
                context_text = str(context.get("text") or context.get("raw_value") or "")
                candidate_unit = unit_note(context_text, metric=metric)
                if candidate_unit:
                    inferred_units.add(candidate_unit)
                    for h, cid in delivered.items():
                        cref = (state.get("table_evidence_refs") or {}).get(cid) or {}
                        if cref.get("table_id") != table_id or cref.get("revision_id") != revision_id:
                            continue
                        if ((context.get("row_index") is not None and
                             (cref.get("row"), cref.get("column")) == (context.get("row_index"), context.get("column_index")))
                                or _ground_text(state, delivered, h) == context_text):
                            inferred_unit_handles.append(h)
    for h, context_text in native_context:
        candidate_unit = unit_note(context_text, metric=metric)
        if candidate_unit:
            inferred_units.add(candidate_unit)
            inferred_unit_handles.append(h)
    inferred_unit = next(iter(inferred_units)) if len(inferred_units) == 1 else None
    inline_unit = declared_unit(text) or percent_unit(scale)
    ground_unit = _unit_from_ground(state, delivered, grounds.get("unit"))
    _check_unit_context(inline_unit, ground_unit)
    _check_unit_context(inline_unit, inferred_unit)
    _check_unit_context(ground_unit, inferred_unit)
    resolved_unit = (inline_unit if scale else None) or ground_unit or inferred_unit or inline_unit
    for key in ("period", "unit", "subject_scope"):
        if key not in grounds and not (key == "period" and period) and not (key == "unit" and resolved_unit):
            missing.append(key)
    context_handles = []
    for evidence_handle, evidence_id in delivered.items():
        context_ref = (state.get("table_evidence_refs") or {}).get(evidence_id) or {}
        if (context_ref.get("table_id") == table_id
                and context_ref.get("revision_id") == revision_id
                and context_ref.get("kind") == "table_cell"
                and ((context_ref.get("row") == row and context_ref.get("column") == 1 and metric)
                     or [context_ref.get("row"), context_ref.get("column")] in provenance.get("context_cells", []))):
            context_handles.append(evidence_handle)
    return ResolvedOperand(
        label=ref.label or metric or "{}R{}C{}".format(table_id, row, column),
        kind="table_cell", value=value * scale_multiplier(scale), text=text,
        scale=scale, metric=metric,
        unit=resolved_unit,
        period=(_semantic_ground("period", _ground_text(state, delivered, grounds["period"]) or "")
                if grounds.get("period") else period),
        subject_scope=(_semantic_ground("subject_scope", _ground_text(state, delivered, grounds["subject_scope"]) or "")
                       if grounds.get("subject_scope") else None),
        missing=tuple(missing), evidence_handles=tuple(dict.fromkeys((handle, *grounds.values(), *context_handles, *inferred_unit_handles))), table_id=table_id,
        revision_id=revision_id, interpretation_id=provenance.get("interpretation_id"),
        row=row, column=column)


def _stored_cell(state: Mapping[str, Any], table_id: str,
                 revision_id: Optional[str], row: int,
                 column: int) -> Optional[Mapping[str, Any]]:
    for entry in _complete_reads(state, table_id):
        if revision_id is not None and entry.get("source_version") not in (None, revision_id):
            continue
        cells = entry.get("seen_cells") or {}
        for key in ((row, column), "{}:{}".format(row, column)):
            if key in cells:
                return cells[key]
    return None


def resolve_operands(refs: Sequence[OperandRef], state: Mapping[str, Any], *,
                     question: str = "") -> Tuple[List[ResolvedOperand], List[str]]:
    """Resolve a list of operands; unresolved ones are reported, never invented."""
    resolved: List[ResolvedOperand] = []
    problems: List[str] = []
    for index, ref in enumerate(refs, start=1):
        try:
            resolved.append(resolve_operand(ref, state, question=question))
        except OperandError as error:
            problems.append("{}:{}".format(index, error))
    return resolved, problems


__all__ = ["OPERAND_RULE_VERSION", "SCALE_FACTORS", "RULE_CONSTANTS", "MISSING_KEYS",
           "PERCENT_UNITS", "OperandError", "OperandRef", "ResolvedOperand",
           "declared_unit", "format_decimal", "handle_claim_ids", "parse_number",
           "percent_unit", "resolve_operand", "resolve_operands", "scale_factor",
           "scale_multiplier"]
