"""Conservative deterministic source-to-claim support checks.

The checker intentionally proves a small, auditable language instead of
pretending to solve unrestricted natural-language inference.  It accepts exact
source text and order-preserving minimal decontextualization, protects factual
atoms (dates, numbers, units, negation, and comparisons), keeps table facts on
one row, and evaluates only explicit table sums.  Anything outside those
bounded rules is ``NOT_ENOUGH_INFO``.

This module is pure: it consumes immutable ``SourceSpan`` values and never
calls a model or touches storage, a clock, randomness, or the network.
"""

from __future__ import annotations

import re
import sys
from collections import OrderedDict
from threading import RLock
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Callable, FrozenSet, List, Literal, Optional, Sequence, Tuple

from jonex_wiki.domain.engine.models.compiler_state import SourceSpan

ClaimSupportStatus = Literal["SUPPORTED", "REFUTED", "NOT_ENOUGH_INFO"]
ClaimSupportDecision = Tuple[ClaimSupportStatus, str]

#: Identity of the deterministic support rule.
#:
#: ``claim-support-v1`` was the punctuation-folded substring fast path without
#: protected-atom checks.  Bundles compiled under the old rule keep the support
#: labels frozen in ``claims.jsonl``/``claim_audit.json``. Version 3 retains
#: trailing percent units in matched windows; the version is what
#: lets the bundle codec tell "compiled by an older rule" apart from "audit
#: inconsistent with its own sources".
SUPPORT_RULE_VERSION = "claim-support-v3"

_TOKEN_RE = re.compile(
    r"[a-z]+(?:'[a-z]+)?|(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?|[\u3400-\u9fff]+",
    flags=re.IGNORECASE,
)
_NUMBER_RE = re.compile(
    r"(?<![\d.,])[-+]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?(?![\d.,])"
)
_ISO_DATE_RE = re.compile(
    r"(?<!\d)(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})(?!\d)"
)
_ZH_DATE_RE = re.compile(
    r"(?<!\d)(\d{4})年(\d{1,2})月(\d{1,2})日"
)

_STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "been",
    "being",
    "by",
    "did",
    "do",
    "does",
    "for",
    "from",
    "had",
    "has",
    "have",
    "in",
    "is",
    "it",
    "its",
    "of",
    "on",
    "or",
    "that",
    "the",
    "their",
    "this",
    "to",
    "was",
    "were",
    "with",
    "于",
    "为",
    "和",
    "的",
}

_NEGATION_WORDS = {
    "denied",
    "failed",
    "never",
    "no",
    "not",
    "without",
}
_NEGATION_FRAGMENTS = ("不", "未", "没有", "无", "否认")

_COMPARISON_PATTERNS: Tuple[Tuple[str, re.Pattern[str]], ...] = (
    ("GE", re.compile(r"\b(?:at least|no less than)\b|不少于|至少", re.I)),
    ("LE", re.compile(r"\b(?:at most|no more than)\b|不多于|至多", re.I)),
    ("GT", re.compile(r"\b(?:greater|higher|larger|more) than\b|高于|大于|超过", re.I)),
    ("LT", re.compile(r"\b(?:less|lower|smaller) than\b|低于|小于|少于", re.I)),
    ("EQ", re.compile(r"\b(?:equal to|the same as)\b|等于|相同", re.I)),
)
_COMPARISON_WORDS = {
    "above",
    "at",
    "below",
    "equal",
    "greater",
    "higher",
    "larger",
    "least",
    "less",
    "lower",
    "more",
    "most",
    "same",
    "smaller",
    "than",
    "under",
    "over",
}

_UNIT_ALIASES = {
    "%": "percent",
    "percent": "percent",
    "percentage": "percent",
    "usd": "usd",
    "dollar": "usd",
    "dollars": "usd",
    "eur": "eur",
    "euro": "eur",
    "euros": "eur",
    "cny": "cny",
    "rmb": "cny",
    "yuan": "cny",
    "kg": "kg",
    "kilogram": "kg",
    "kilograms": "kg",
    "g": "g",
    "gram": "g",
    "grams": "g",
    "km": "km",
    "kilometer": "km",
    "kilometers": "km",
    "m": "m",
    "meter": "m",
    "meters": "m",
    "cm": "cm",
    "mm": "mm",
    "ms": "ms",
    "second": "second",
    "seconds": "second",
    "minute": "minute",
    "minutes": "minute",
    "hour": "hour",
    "hours": "hour",
    "thousand": "thousand",
    "million": "million",
    "billion": "billion",
    "trillion": "trillion",
    "万": "万",
    "亿": "亿",
}

_DERIVED_SUM_RE = re.compile(
    r"\b(?:total|sum|combined|together)\b|合计|总计|总和|共",
    flags=re.IGNORECASE,
)
_DERIVED_WORDS = {"combined", "sum", "together", "total", "合计", "总计", "总和", "共"}


@dataclass(frozen=True)
class _FactFeatures:
    tokens: Tuple[str, ...]
    anchors: FrozenSet[str]
    numbers: FrozenSet[Decimal]
    dates: FrozenSet[str]
    units: FrozenSet[str]
    negated: bool
    comparisons: FrozenSet[str]


@dataclass(frozen=True)
class _EvidenceUnit:
    text: str
    table_row: bool = False


@dataclass(frozen=True)
class _TableRow:
    label: str
    text: str
    cells: Tuple[str, ...] = ()


@dataclass(frozen=True)
class _Table:
    header: str
    rows: Tuple[_TableRow, ...]


def _collapse(value: str) -> str:
    folded = value.replace("\r\n", "\n").replace("\r", "\n").casefold()
    return re.sub(r"[\W_]+", "", folded, flags=re.UNICODE)


# ──────────────────────────────────────────────
#  Protected fast path (decimal, sign, unit, negation, comparison)
# ──────────────────────────────────────────────
#
# ``_collapse`` deletes every punctuation character, so a collapsed substring
# hit alone cannot prove that the *factual atoms* of a claim survive: "收入为
# 15万元" collapses into "收入为1.5万元" and "支持离线部署" collapses into
# "官方明确否认支持离线部署".  The fast path therefore re-checks the matched
# region before it is allowed to short-circuit the feature-based comparison.
# The check is local (the clause the claim actually matched in), so an
# unrelated negation in another clause or sentence still cannot refute a
# faithful excerpt.

#: Clause/sentence boundaries used to bound one matched region.
_CLAUSE_SEPARATOR_RE = re.compile(r"[。！？；;，,\n!?]|(?<!\d)\.(?!\d)")
#: Exactly the characters ``_collapse`` keeps (``[\W_]`` is removed).
_COLLAPSE_KEEP_RE = re.compile(r"[^\W_]", re.UNICODE)
_ENGLISH_TOKEN_RE = re.compile(r"[a-z]+(?:'[a-z]+)?")
#: How far before the matched region a negator may sit and still govern it.
_NEGATION_GAP_CHARS = 16
#: A collapsed hit whose protected atoms could not be located must not be
#: reported as supported; it is not evidence of a contradiction either.
_UNVERIFIED_PROTECTION = "protected_atoms_unverified"


def _fold_with_origin(value: str) -> Tuple[str, Tuple[int, ...]]:
    """Casefolded text plus, for every folded character, its original index."""
    normalized = value.replace("\r\n", "\n").replace("\r", "\n")
    folded = normalized.casefold()
    if folded == normalized:
        return folded, tuple(range(len(normalized)))
    pieces: List[str] = []
    origin: List[int] = []
    for index, character in enumerate(normalized):
        folded_character = character.casefold()
        pieces.append(folded_character)
        origin.extend([index] * len(folded_character))
    return "".join(pieces), tuple(origin)


def _collapse_positions(value: str) -> Tuple[str, Tuple[int, ...]]:
    """``_collapse`` plus the position of every surviving character."""
    positions = tuple(match.start() for match in _COLLAPSE_KEEP_RE.finditer(value))
    return "".join(value[index] for index in positions), positions


def _clause_bounds(folded: str, start: int, end: int) -> Tuple[int, int]:
    """The clause around ``[start, end)``; the region itself when it spans one."""
    if _CLAUSE_SEPARATOR_RE.search(folded, start, end) is not None:
        return start, end
    left = 0
    for separator in _CLAUSE_SEPARATOR_RE.finditer(folded, 0, start):
        left = separator.end()
    right_match = _CLAUSE_SEPARATOR_RE.search(folded, end)
    right = right_match.start() if right_match is not None else len(folded)
    return left, right


def _matched_windows(
    text: str, surface_claim: str
) -> List[Tuple[str, int, int]]:
    """Clause-bounded views of every collapsed occurrence of a claim."""
    folded, origin = _fold_with_origin(text)
    collapsed, positions = _collapse_positions(folded)
    windows: List[Tuple[str, int, int]] = []
    cursor = collapsed.find(surface_claim)
    while cursor != -1:
        first = origin[positions[cursor]]
        last = origin[positions[cursor + len(surface_claim) - 1]] + 1
        # Punctuation folding omits the percent unit after the last digit.
        # Keep it in multi-clause windows as well as single-clause windows.
        while last < len(folded) and folded[last] == "%":
            last += 1
        left, right = _clause_bounds(folded, first, last)
        windows.append((folded[left:right], first - left, last - left))
        cursor = collapsed.find(surface_claim, cursor + 1)
    return windows


def _negation_governs(window: str, match_start: int, match_end: int) -> bool:
    """Whether a negator in the window can govern the matched region.

    A negator that only trails the region is a separate qualifier
    (``收入为15万元（未经审计）``), not the polarity of the matched value.  A
    negator that overlaps the region or immediately precedes it is the polarity
    the excerpt inherits (``官方明确否认支持离线部署``).
    """
    lowered = window.casefold()
    occurrences: List[Tuple[int, int]] = []
    for match in _ENGLISH_TOKEN_RE.finditer(lowered):
        if match.group(0) in _NEGATION_WORDS:
            occurrences.append(match.span())
    for fragment in _NEGATION_FRAGMENTS:
        start = lowered.find(fragment)
        while start != -1:
            occurrences.append((start, start + len(fragment)))
            start = lowered.find(fragment, start + 1)
    for start, end in occurrences:
        if start < match_end and end > match_start:
            return True
        if end <= match_start and match_start - end <= _NEGATION_GAP_CHARS:
            return True
    return False


def _window_conflict(
    claim: _FactFeatures, window: str, match_start: int, match_end: int
) -> Optional[str]:
    """The protected-atom conflict of one matched window, if any."""
    conflict = _protected_conflict(claim, _features(window))
    if conflict == "negation_conflict" and not _negation_governs(
        window, match_start, match_end
    ):
        return None
    return conflict


def _fast_path_conflict(
    claim_text: str,
    source_text: str,
    surface_claim: str,
    normalize: Callable[[str], str],
) -> Optional[str]:
    """``None`` when a matched window keeps every protected atom.

    Returns a conflict code when every occurrence contradicts the claim, and
    ``_UNVERIFIED_PROTECTION`` when no occurrence could be located at all.
    """
    claim = _features(normalize(claim_text))
    windows = _matched_windows(normalize(source_text), surface_claim)
    if not windows:
        return _UNVERIFIED_PROTECTION
    conflict: Optional[str] = None
    for window, match_start, match_end in windows:
        window_conflict = _window_conflict(claim, window, match_start, match_end)
        if window_conflict is None:
            return None
        conflict = conflict or window_conflict
    return conflict


def _tokens(value: str) -> Tuple[str, ...]:
    return tuple(match.group(0).casefold() for match in _TOKEN_RE.finditer(value))


def _meaningful_tokens(value: str) -> Tuple[str, ...]:
    return tuple(token for token in _tokens(value) if token not in _STOPWORDS)


def _date_atoms(value: str) -> Tuple[FrozenSet[str], Tuple[Tuple[int, int], ...]]:
    atoms = set()
    spans: List[Tuple[int, int]] = []
    for pattern in (_ISO_DATE_RE, _ZH_DATE_RE):
        for match in pattern.finditer(value):
            year, month, day = (int(part) for part in match.groups())
            if 1 <= month <= 12 and 1 <= day <= 31:
                atoms.add(f"{year:04d}-{month:02d}-{day:02d}")
                spans.append(match.span())
    return frozenset(atoms), tuple(spans)


def _inside_any_span(start: int, end: int, spans: Sequence[Tuple[int, int]]) -> bool:
    return any(start >= left and end <= right for left, right in spans)


def _number_atoms(value: str, date_spans: Sequence[Tuple[int, int]]) -> FrozenSet[Decimal]:
    atoms = set()
    for match in _NUMBER_RE.finditer(value):
        if _inside_any_span(match.start(), match.end(), date_spans):
            continue
        try:
            atoms.add(Decimal(match.group(0).replace(",", "")).normalize())
        except InvalidOperation:
            continue
    return frozenset(atoms)


def _unit_atoms(value: str) -> FrozenSet[str]:
    value_tokens = set(_tokens(value))
    atoms = {
        canonical
        for token, canonical in _UNIT_ALIASES.items()
        if (token == "%" and token in value)
        or (token != "%" and token in value_tokens)
    }
    return frozenset(atoms)


def _comparison_atoms(value: str) -> FrozenSet[str]:
    return frozenset(
        direction for direction, pattern in _COMPARISON_PATTERNS if pattern.search(value)
    )


def _is_negated(value: str) -> bool:
    tokens = set(_tokens(value))
    return bool(tokens & _NEGATION_WORDS) or any(
        fragment in value for fragment in _NEGATION_FRAGMENTS
    )


def _features(value: str) -> _FactFeatures:
    dates, date_spans = _date_atoms(value)
    tokens = _meaningful_tokens(value)
    unit_tokens = set(_UNIT_ALIASES)
    anchors = {
        token
        for token in tokens
        if token not in _NEGATION_WORDS
        and token not in _COMPARISON_WORDS
        and token not in _DERIVED_WORDS
        and token not in unit_tokens
        and not _NUMBER_RE.fullmatch(token)
    }
    return _FactFeatures(
        tokens=tokens,
        anchors=frozenset(anchors),
        numbers=_number_atoms(value, date_spans),
        dates=dates,
        units=_unit_atoms(value),
        negated=_is_negated(value),
        comparisons=_comparison_atoms(value),
    )


def _is_subsequence(needles: Sequence[str], haystack: Sequence[str]) -> bool:
    if not needles:
        return False
    cursor = 0
    for token in haystack:
        if token == needles[cursor]:
            cursor += 1
            if cursor == len(needles):
                return True
    return False


def _aligned(claim: _FactFeatures, source: _FactFeatures) -> bool:
    if not claim.anchors:
        return False
    common = len(claim.anchors & source.anchors)
    return common >= 1 and common / len(claim.anchors) >= 0.60


def _protected_conflict(
    claim: _FactFeatures, source: _FactFeatures
) -> Optional[str]:
    if claim.dates and source.dates and not claim.dates.issubset(source.dates):
        return "date_conflict"
    if claim.numbers and source.numbers and not claim.numbers.issubset(source.numbers):
        return "numeric_conflict"
    if claim.negated != source.negated:
        return "negation_conflict"
    if (
        claim.comparisons
        and source.comparisons
        and not claim.comparisons.issubset(source.comparisons)
    ):
        return "comparison_conflict"
    if claim.units and source.units and not claim.units.issubset(source.units):
        return "unit_conflict"
    return None


def _protected_contained(claim: _FactFeatures, source: _FactFeatures) -> bool:
    return (
        claim.dates.issubset(source.dates)
        and claim.numbers.issubset(source.numbers)
        and claim.units.issubset(source.units)
        and claim.negated == source.negated
        and claim.comparisons.issubset(source.comparisons)
    )


def _split_table_row(line: str) -> List[str]:
    from jonex_wiki.domain.engine.compile.source_prep.chunking import split_table_row
    return split_table_row(line)


def _is_separator_row(cells: Sequence[str]) -> bool:
    from jonex_wiki.domain.engine.compile.source_prep.chunking import is_separator_row
    return is_separator_row(cells)


def _parse_tables(value: str) -> List[_Table]:
    from jonex_wiki.domain.engine.compile.source_prep.chunking import identify_units
    tables = []
    for unit in identify_units(value):
        if unit.kind != "table":
            continue
        lines = value[unit.start:unit.end].splitlines()
        headers = _split_table_row(lines[0])
        rows = []
        for line in lines[2:]:
            cells = _split_table_row(line)
            if len(cells) != len(headers):
                continue
            # A cell carries its column label, never the other numeric cells.
            rows.append(_TableRow(label=cells[0], text=" ".join(cells),
                cells=tuple(f"{headers[0]} {cells[0]} {header} {cell}" for header, cell in zip(headers[1:], cells[1:]))))
        if rows:
            tables.append(_Table(header=" ".join(headers), rows=tuple(rows)))
    return tables


def _parse_table(value: str) -> Optional[_Table]:
    tables = _parse_tables(value)
    return tables[0] if len(tables) == 1 else None


def _evidence_units(source_spans: Sequence[SourceSpan]) -> List[_EvidenceUnit]:
    from jonex_wiki.domain.engine.compile.source_prep.chunking import identify_units
    units: List[_EvidenceUnit] = []
    has_tables = False
    for span in source_spans:
        tables = _parse_tables(span.text)
        has_tables |= bool(tables)
        for table in tables:
            for row in table.rows:
                units.extend(_EvidenceUnit(text=cell, table_row=True) for cell in row.cells)
        # Preserve prose in a mixed passage, without flattening table columns.
        prose = "".join(span.text[u.start:u.end] for u in identify_units(span.text) if u.kind != "table")
        parts = [part.strip() for part in re.split(r"(?<=[.!?。！？])\s+|[\n;]+", prose) if part.strip()]
        units.extend(_EvidenceUnit(text=part) for part in parts)
        units.extend(_EvidenceUnit(text=f"{parts[index]} {parts[index + 1]}") for index in range(len(parts) - 1))
    if len(source_spans) > 1 and not has_tables:
        units.append(_EvidenceUnit(text=" ".join(span.text for span in source_spans)))
    return list(dict.fromkeys(units))


def _derived_sum_decision(
    claim_text: str, source_spans: Sequence[SourceSpan]
) -> Optional[ClaimSupportDecision]:
    if not _DERIVED_SUM_RE.search(claim_text):
        return None
    claim_features = _features(claim_text)
    claim_tokens = set(claim_features.tokens)
    if len(claim_features.numbers) != 1:
        return "NOT_ENOUGH_INFO", "derived_fact_unproven"

    for table in [table for span in source_spans for table in _parse_tables(span.text)]:
        header_features = _features(table.header)
        if not (claim_features.anchors & header_features.anchors):
            continue
        selected: List[_TableRow] = []
        selected_label_tokens = set()
        for row in table.rows:
            label_tokens = set(_meaningful_tokens(row.label))
            if label_tokens and label_tokens.issubset(claim_tokens):
                selected.append(row)
                selected_label_tokens.update(label_tokens)
        if len(selected) < 2:
            continue
        # Every substantive claim anchor must be accounted for by either the
        # table header (metric) or an explicitly selected row label.  This
        # prevents "Atlas + Beta + Gamma" from being accepted merely because
        # the Atlas and Beta subtotal happens to equal the claimed target.
        if not claim_features.anchors.issubset(
            header_features.anchors | selected_label_tokens
        ):
            continue

        operands: List[Decimal] = []
        for row in selected:
            row_features = _features(row.text)
            if len(row_features.numbers) != 1:
                operands = []
                break
            operands.append(next(iter(row_features.numbers)))
        if len(operands) < 2:
            continue
        table_units = _features(
            f"{table.header} {' '.join(row.text for row in selected)}"
        ).units
        if not claim_features.units.issubset(table_units):
            return "NOT_ENOUGH_INFO", "derived_fact_unproven"
        target = next(iter(claim_features.numbers))
        if sum(operands, Decimal(0)) == target:
            return "SUPPORTED", "derived_sum_supported"
        return "REFUTED", "derived_sum_conflict"
    return "NOT_ENOUGH_INFO", "derived_fact_unproven"


_SUPPORT_CACHE_BYTES = 64 * 1024 * 1024
_support_cache = OrderedDict()
_support_cache_size = 0
_support_cache_lock = RLock()


def assess_claim_support(
    claim_text: str,
    source_spans: Sequence[SourceSpan],
    *,
    has_unresolved_evidence: bool,
) -> ClaimSupportDecision:
    """Reuse only the exact inputs of the unchanged deterministic checker.

    Spans are frozen, including their full text, identities, digests and ranges.
    Binding them to the current archive and global publication checks still run
    in their original callers. This cache is never a stored support certificate.
    """
    global _support_cache_size
    if has_unresolved_evidence or not source_spans or not all(isinstance(span,SourceSpan) for span in source_spans):
        return _assess_claim_support(claim_text,source_spans,has_unresolved_evidence=has_unresolved_evidence)
    key=(SUPPORT_RULE_VERSION,claim_text,tuple(source_spans))
    from jonex_wiki.kernel.telemetry import count_work
    with _support_cache_lock:
        decision=_support_cache.get(key)
        if decision is not None:
            _support_cache.move_to_end(key)
            count_work('claim_support_cache_hits')
            return decision[0]
    count_work('claim_support_cache_misses')
    result=_assess_claim_support(claim_text,source_spans,has_unresolved_evidence=False)
    # Conservative reachable-object weight; shared strings/spans count again.
    weight=512+sys.getsizeof(claim_text)+sys.getsizeof(key[2])
    for span in source_spans:
        weight+=sys.getsizeof(span)+sys.getsizeof(span.__dict__)
        weight+=sum(sys.getsizeof(value) for value in span.__dict__.values())
    if weight<=_SUPPORT_CACHE_BYTES:
        with _support_cache_lock:
            if key not in _support_cache:
                while _support_cache and (_support_cache_size+weight>_SUPPORT_CACHE_BYTES or len(_support_cache)>=32768):
                    _,old_weight=_support_cache.popitem(last=False)[1]
                    _support_cache_size-=old_weight
                _support_cache[key]=(result,weight)
                _support_cache_size+=weight
    return result


def _assess_claim_support(
    claim_text: str,
    source_spans: Sequence[SourceSpan],
    *,
    has_unresolved_evidence: bool,
) -> ClaimSupportDecision:
    """Return one conservative, auditable support decision for a claim."""
    if has_unresolved_evidence or not source_spans:
        return "NOT_ENOUGH_INFO", "evidence_span_unbound"

    def identity(text):
        return text

    # A collapsed substring hit may short-circuit the feature comparison, but
    # only for a matched region whose numbers, signs, units, negation and
    # comparison direction survive the same protection.  A definite protected
    # contradiction is remembered and reported when no exact/copula/derived
    # path supports the claim.
    refutation: Optional[str] = None

    claim_surface = _collapse(claim_text)
    if claim_surface:
        for span in source_spans:
            if claim_surface not in _collapse(span.text):
                continue
            conflict = _fast_path_conflict(claim_text, span.text, claim_surface, identity)
            if conflict is None:
                return "SUPPORTED", "exact_source_substring"
            if conflict != _UNVERIFIED_PROTECTION:
                refutation = refutation or conflict

    # Narrow Chinese copula equivalence; all values, negation and units must
    # remain exactly equal. This is not a general synonym/semantic relaxation.
    def copula(text):
        return re.sub(r"(额定功率|额定电压|安装地点|制造年份)(?:为|是)", r"\1为", text)
    normalized_claim = _collapse(copula(claim_text))
    if normalized_claim:
        for span in source_spans:
            if normalized_claim not in _collapse(copula(span.text)):
                continue
            conflict = _fast_path_conflict(claim_text, span.text, normalized_claim, copula)
            if conflict is None:
                return "SUPPORTED", "chinese_attribute_copula"
            if conflict != _UNVERIFIED_PROTECTION:
                refutation = refutation or conflict

    derived = _derived_sum_decision(claim_text, source_spans)
    if derived is not None:
        if derived[0] == "NOT_ENOUGH_INFO" and refutation is not None:
            # The derived path only reports that the sum is unproven; a
            # protected contradiction in the matched excerpt is stronger.
            return "REFUTED", refutation
        return derived

    claim = _features(claim_text)
    conflicts: List[str] = []
    for unit in _evidence_units(source_spans):
        source = _features(unit.text)
        if not _aligned(claim, source):
            continue
        conflict = _protected_conflict(claim, source)
        if conflict is not None:
            conflicts.append("table_row_conflict" if unit.table_row else conflict)
            continue
        if not _protected_contained(claim, source):
            continue
        if unit.table_row:
            supported = set(claim.tokens).issubset(set(source.tokens))
            reason = "table_row_supported"
        else:
            supported = _is_subsequence(claim.tokens, source.tokens)
            reason = "minimal_decontextualization"
        if supported:
            return "SUPPORTED", reason

    if refutation is not None:
        return "REFUTED", refutation
    if conflicts:
        return "REFUTED", conflicts[0]
    return "NOT_ENOUGH_INFO", "claim_not_entailed_by_bound_spans"


__all__ = [
    "ClaimSupportStatus",
    "ClaimSupportDecision",
    "SUPPORT_RULE_VERSION",
    "assess_claim_support",
]
