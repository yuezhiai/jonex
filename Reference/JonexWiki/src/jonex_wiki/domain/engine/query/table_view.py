"""Deterministic display of Markdown cells; never an answer or new citation.

Column indexes, source-order data rows and explicit empty cells are retained.
A Markdown display cannot prove extra header tiers or spreadsheet date epochs.
"""
from __future__ import annotations

import re
import hashlib
from collections import Counter
from typing import Any, Iterable


def _cells(line: str) -> list[str]:
    return [cell.strip().replace(r'\|', '|') for cell in
            re.split(r'(?<!\\)\|', line.strip()[1:-1])]


def table_view(text: str) -> list[dict]:
    sections = []
    heading = ''
    rows = []

    def flush():
        if len(rows) < 2:
            rows.clear()
            return
        separator = next((i for i, row in enumerate(rows)
                          if all(re.fullmatch(r':?-+:?', cell.replace(' ', '')) for cell in row)), None)
        if separator is None or separator == 0:
            rows.clear()
            return
        header = rows[:separator]
        data = rows[separator + 1:]
        # GFM declares headers before its separator. Data values cannot prove
        # extra header tiers; native formats provide that structure separately.
        width = max(map(len, rows))
        labels = []
        for column in range(width):
            parts = list(dict.fromkeys(row[column] for row in header if column < len(row) and row[column]))
            labels.append({'index': column, 'labels': parts})
        raw_header = [list(row) for row in header]
        header_key = hashlib.sha256(repr(raw_header).encode('utf-8')).hexdigest()[:16]
        base_header_key = hashlib.sha256(repr(raw_header[:1]).encode('utf-8')).hexdigest()[:16]
        sections.append({'section': heading, 'columns': labels,
                         'header_rows': raw_header, 'header_key': header_key,
                         'base_header_key': base_header_key,
                         'rows': [{'row': i+1, 'cells': row} for i, row in enumerate(data)],
                         'row_count': len(data),
                         'width_mismatch': any(len(row) != width for row in data)})
        rows.clear()

    for line in text.splitlines():
        if line.strip().startswith('|') and line.strip().endswith('|'):
            rows.append(_cells(line))
        else:
            flush()
            if line.startswith('#'):
                heading = line.lstrip('# ').strip()
    flush()
    return sections


_QUESTION_NOISE = {
    '哪些', '哪个', '多少', '几个', '是否', '怎么', '什么', '分别', '一共',
    '事项', '权限', '要求', '情况', '处理', '里面', '其中',
}


def _question_terms(question: str) -> set[str]:
    compact = question.casefold()
    chunks = re.findall(r'[A-Za-z]+\d*|\d+(?:\.\d+)?%?|[\u4e00-\u9fff]+', compact)
    terms = set()
    for chunk in chunks:
        if re.fullmatch(r'[\u4e00-\u9fff]+', chunk):
            for size in range(2, min(6, len(chunk)) + 1):
                terms.update(chunk[i:i + size] for i in range(len(chunk) - size + 1))
        elif len(chunk) >= 2:
            terms.add(chunk)
    return terms - _QUESTION_NOISE


def _maximal_hits(terms: set[str], text: str) -> list[str]:
    matches = {term for term in terms if term in text}
    return sorted(
        (term for term in matches if not any(term != other and term in other for other in matches)),
        key=lambda value: (-len(value), value),
    )


def table_analysis_records(
    records: Iterable[Any], question: str, *, max_documents: int = 2,
    max_columns: int = 4, max_values: int = 32,
) -> list[dict]:
    """Bounded statistics for each literal display fragment, never merged tables.

    Matching column positions or labels cannot establish table identity or
    prove that two passages contain disjoint rows. Native whole-table execution
    belongs to the verified structured executor instead.
    """
    terms = _question_terms(question)
    candidates = []
    for record in records:
        document = str(getattr(record, 'document_id', ''))
        passage = str(getattr(record, 'passage_id', ''))
        for ordinal, view in enumerate(table_view(str(getattr(record, 'excerpt', '')))):
            scored = []
            for column in view['columns']:
                index = column['index']
                values = [row['cells'][index] if index < len(row['cells']) else '' for row in view['rows']]
                hits = _maximal_hits(terms, '/'.join(column['labels']).casefold())
                value_hits = _maximal_hits(terms, '\n'.join(values).casefold())
                score = sum(len(term) ** 2 for term in hits) * 4 + sum(len(term) ** 2 for term in value_hits)
                if score:
                    scored.append((score, index, column, values, hits + value_hits))
            chosen = sorted(scored, key=lambda item: (-item[0], item[1]))[:max_columns]
            if not chosen:
                continue
            profiles = []
            for score, index, column, values, hits in chosen:
                counts = Counter(value for value in values if value)
                profiles.append({'index': index, 'labels': column['labels'],
                    'matched_question_terms': sorted(set(hits), key=lambda value: (-len(value), value))[:12],
                    'non_empty_count': sum(counts.values()), 'distinct_count': len(counts),
                    'value_counts': [{'value': value[:160], 'count': count} for value, count in counts.most_common(max_values)]})
            indexes = [item[1] for item in chosen]
            joint = Counter(tuple(row['cells'][i] if i < len(row['cells']) else '' for i in indexes) for row in view['rows'])
            candidates.append((sum(item[0] for item in chosen), {
                'document_id': document, 'passage_id': passage, 'table_ordinal': ordinal,
                'aggregation_scope': 'this_display_fragment_only', 'row_count': len(view['rows']),
                'question_conditions': 'not_interpreted', 'column_profiles': profiles,
                'joint_counts': [{'values': [{'index': i, 'value': value[:160]} for i, value in zip(indexes, key)], 'count': count}
                    for key, count in joint.most_common(max_values)]}))
    selected_documents = []
    output = []
    for _, item in sorted(candidates, key=lambda item: -item[0]):
        document = item['document_id']
        if document not in selected_documents:
            if len(selected_documents) >= max_documents:
                continue
            selected_documents.append(document)
        output.append(item)
        if len(output) >= max(1, max_documents) * 4:
            break
    return output


def table_projection(text: str, question: str, *, max_rows: int = 64,
                     max_columns: int = 32) -> list[dict]:
    """Display bounded source-order cells without interpreting question filters.

    Both row and column omissions are measured against the original grid.
    Display coverage is not question-condition coverage.
    """
    if (type(max_rows) is not int or type(max_columns) is not int
            or max_rows < 1 or max_columns < 1):
        raise ValueError("TABLE_PROJECTION_LIMIT_INVALID")
    output = []
    for view in table_view(text):
        columns, rows = view['columns'], view['rows']
        if not columns or not rows:
            continue
        selected_columns = columns[:max_columns]
        selected_rows = rows[:max_rows]
        output.append({
            'section': view['section'],
            'columns': [
                {'index': column['index'], 'header_sequence': column['labels']}
                for column in selected_columns
            ],
            'rows': [
                {'row': row['row'], 'cells': [
                    {'index': column['index'], 'value': (
                        row['cells'][column['index']] if column['index'] < len(row['cells']) else '')[:240]}
                    for column in selected_columns
                ]} for row in selected_rows
            ],
            'selection': 'source_order_no_question_filter',
            'source_row_count': len(rows), 'source_column_count': len(columns),
            'rows_omitted': len(rows) - len(selected_rows),
            'columns_omitted': len(columns) - len(selected_columns),
            'values_truncated': any(len(value) > 240 for row in selected_rows
                for value in row['cells'][:max_columns]),
            'question_conditions': 'not_interpreted',
        })
    return output


def table_analysis(text: str, question: str, *, max_columns: int = 4) -> list[dict]:
    """Return bounded, deterministic statistics for question-relevant columns.

    This projection never decides the answer. It exposes exact per-column and
    joint counts, plus matching row keys, so synthesis does not have to count
    dozens of Markdown rows by eye.
    """
    views = table_view(text)
    terms = _question_terms(question)
    output = []
    for view in views:
        rows = view['rows']
        if not rows:
            continue
        scored = []
        for column in view['columns']:
            index = column['index']
            label = '/'.join(column['labels']).casefold()
            values = [row['cells'][index] if index < len(row['cells']) else '' for row in rows]
            value_text = '\n'.join(values).casefold()
            label_hits = _maximal_hits(terms, label)
            value_hits = _maximal_hits(terms, value_text)
            score = sum(len(term) ** 2 for term in label_hits) * 4 + sum(
                len(term) ** 2 for term in value_hits
            )
            if score:
                scored.append((score, index, column, values, label_hits, value_hits))
        chosen = sorted(scored, key=lambda item: (-item[0], item[1]))[:max_columns]
        if not chosen:
            continue
        profiles = []
        for _score, index, column, values, label_hits, value_hits in chosen:
            counts = Counter(value for value in values if value)
            profiles.append({
                'index': index,
                'labels': column['labels'],
                'matched_question_terms': sorted(set(label_hits + value_hits), key=lambda x: (-len(x), x))[:12],
                'non_empty_count': sum(bool(value) for value in values),
                'distinct_count': len(counts),
                'value_counts': [
                    {'value': value, 'count': count}
                    for value, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:64]
                ],
            })
        joint = []
        indexes = [item[1] for item in chosen]
        if len(indexes) >= 2:
            groups = {}
            for ordinal, row in enumerate(rows, start=1):
                cells = row['cells']
                key = tuple(cells[index] if index < len(cells) else '' for index in indexes)
                row_key = cells[0] if cells else str(ordinal)
                group = groups.setdefault(key, {'count': 0, 'row_keys': []})
                group['count'] += 1
                if len(group['row_keys']) < 64:
                    group['row_keys'].append(row_key or str(ordinal))
            joint = [
                {'values': [{'index': index, 'value': value} for index, value in zip(indexes, key)], **group}
                for key, group in sorted(groups.items(), key=lambda item: (-item[1]['count'], item[0]))[:64]
            ]
        output.append({
            'section': view['section'],
            'row_count': view['row_count'],
            'column_profiles': profiles,
            'joint_counts': joint,
        })
    return output
