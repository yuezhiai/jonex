"""Source-only table labels and nearby-text navigation for compiled articles."""
from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
import hashlib
import json
import re
import sqlite3


def full_table_path(source_id, table_id):
    source = hashlib.sha256(source_id.encode()).hexdigest()[:20]
    name = table_id if re.fullmatch(r'[A-Za-z0-9_-]+', table_id) else hashlib.sha256(table_id.encode()).hexdigest()
    return 'full-tables/' + source + '/' + name + '.md'


def full_table_file(db, source_id, table_id, database_path):
    """Export the existing logical table as a normal, directly readable file."""
    from jonex_wiki.domain.engine.query.table_reader import full_table_body
    metadata = {'kind': 'full_logical_table', 'source_ids': [source_id], 'table_id': table_id,
                'native_database': database_path}
    header = '---\n' + ''.join(k + ': ' + json.dumps(v, ensure_ascii=False) + '\n'
                                  for k, v in metadata.items()) + '---\n\n'
    return (header + full_table_body(db, source_id, table_id) + '\n').encode()


def read_inventory(db):
    """Read available table labels once; the native grid remains authoritative."""
    db.row_factory = sqlite3.Row
    tables = [dict(row) for row in db.execute('SELECT * FROM logical_tables ORDER BY ordinal,table_id')]
    available = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    sheets = ({row['sheet_id']: row['name'] for row in db.execute('SELECT sheet_id,name FROM sheets')}
              if 'sheets' in available else {})
    fields = defaultdict(list)
    if {'interpretations', 'field_map'} <= available:
        for row in db.execute('SELECT i.table_id,f.label FROM interpretations i '
                'JOIN field_map f ON f.interpretation_id=i.interpretation_id '
                'WHERE i.version=(SELECT max(j.version) FROM interpretations j WHERE j.table_id=i.table_id) '
                'ORDER BY i.table_id,f.column_index'):
            if row['label'] and row['label'] not in fields[row['table_id']]:
                fields[row['table_id']].append(row['label'])
    observations = ({row['table_id']: dict(row) for row in db.execute('SELECT * FROM table_observations')}
                    if 'table_observations' in available else {})
    for table in tables:
        observation = observations.get(table['table_id']) or {}
        raw = json.loads(observation.get('raw_item_json') or '{}')
        table['printed_text'] = raw.get('text') or ''
        table['page_number'] = observation.get('page_number')
        table['content_index'] = (raw.get('raw_item') or {}).get('content_index')
        table['sheet_name'] = sheets.get(table.get('sheet_id'))
        table['field_labels'] = fields[table['table_id']]
    return tables


def _clean(value, limit=220):
    value = re.sub(r'\s+', ' ', str(value or '')).strip()
    return value if len(value) <= limit else value[:limit].rstrip() + '…'


def prepare_table_navigation(articles, snapshots, attachments):
    """Attach only uniquely located source-assembled paragraphs to nearby tables.

    Same-page parser order is a navigation hint, not a semantic table binding.
    No question, answer or evaluation result is an input to this function.
    """
    articles = deepcopy(articles)
    blocks = defaultdict(list)
    for article in articles:
        for section in article.get('sections', []):
            for block in section.get('blocks', []):
                block.pop('nearby_tables', None)
                owners = block.get('source_ids') or []
                if len(owners) == 1 and block.get('composition_mode') == 'source_assembled':
                    blocks[owners[0]].append(block)
    for sid, attachment in attachments.items():
        tables = attachment.get('tables_inventory') or []
        pages = defaultdict(dict)
        metadata = (snapshots.get(sid) or {}).get('source_metadata') or {}
        for chunk in metadata.get('chunks') or []:
            for location in (chunk.get('metadata') or {}).get('source_locations') or []:
                raw = location.get('raw_item') or {}
                page = location.get('page_number')
                index = raw.get('content_index', location.get('position'))
                if type(page) is int and type(index) is int and isinstance(raw.get('text'), str) and raw['text'].strip():
                    pages[page][index] = (raw['text'].strip(), raw.get('text_level'))
        positions = defaultdict(list)
        for table in tables:
            if type(table.get('page_number')) is int and type(table.get('content_index')) is int:
                positions[table['page_number']].append(table['content_index'])
        for table in tables:
            page, index = table.get('page_number'), table.get('content_index')
            if type(page) is not int or type(index) is not int:
                continue
            previous = max((n for n in positions[page] if n < index), default=-1)
            near = [value for n, value in sorted(pages.get(page, {}).items()) if previous < n < index]
            heading = next((text for text, level in reversed(near) if level), '')
            paragraphs = [text for text, level in near if not level]
            table['nearby_heading'] = _clean(heading)
            table['nearby_text'] = [_clean(text) for text in paragraphs[-2:]]
            anchor = next((text for text in reversed(paragraphs) if len(text) >= 24), '')
            matches = [block for block in blocks[sid] if anchor and anchor in (block.get('text') or '')]
            if len(matches) == 1:
                entry = {key: table.get(key) for key in ('table_id', 'title', 'sheet_name', 'field_labels',
                         'nearby_heading', 'nearby_text', 'page_number', 'status', 'page_path')}
                matches[0].setdefault('nearby_tables', []).append(entry)
    return articles


def table_navigation_text(table, prefix):
    """Render labels as navigation; a preview never replaces a logical table."""
    tid = table['table_id']
    title = _clean(table.get('title') or tid, 120)
    page = table.get('page_path')
    label = '[' + title + '](' + prefix + page + ')' if page else title
    parts = [label, '`' + tid + '`']
    if table.get('sheet_name') and table['sheet_name'] != table.get('title'):
        parts.append('工作表：' + _clean(table['sheet_name'], 100))
    fields = table.get('field_labels') or []
    if fields:
        parts.append('表头：' + '；'.join(_clean(v, 100) for v in fields[:12]) + ('；…' if len(fields) > 12 else ''))
    context = [table.get('nearby_heading') or '', *(table.get('nearby_text') or [])]
    if any(context): parts.append('附近原文：' + '；'.join(x for x in context if x))
    if table.get('page_number'): parts.append('来源页：' + str(table['page_number']))
    if table.get('status') not in (None, 'accepted'): parts.append('结构状态：' + str(table['status']))
    return '；'.join(parts)


def table_directory(titles, resources):
    """Make table headings and complete-file links searchable in one place."""
    lines = ['# 完整逻辑表目录', '',
             '按来源、标题、表头和附近文字寻找表格；链接指向完整逻辑表。', '']
    for source_id, entries in sorted(resources.items(), key=lambda item: str(titles.get(item[0]) or item[0])):
        for kind, _, _, table in entries:
            if kind != 'table' or not table.get('table_id') or not table.get('page_path'):
                continue
            lines.append('- ' + _clean(titles.get(source_id) or source_id, 200) + '；' +
                         table_navigation_text(table, '../../'))
    return ('\n'.join(lines) + '\n').encode() if len(lines) > 4 else b''
