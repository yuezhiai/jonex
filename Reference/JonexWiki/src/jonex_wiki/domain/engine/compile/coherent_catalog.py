"""Portable source/record/article directory built into the normal Bundle.

This extends the existing compressed contribution index; native grids and
original text remain in their published files. Source names are recall keys,
never entity identity. Every located object has its own content revision.
"""
from __future__ import annotations

from contextlib import closing
from functools import cache
import hashlib
from itertools import chain
import json
from pathlib import Path
import sqlite3
import tempfile

from jonex_wiki.domain.engine.compile.material_aggregation import build_index, encoded,fts_text

RULE = 'coherent-catalog-v4-preview-links'
PATH = 'catalog.sqlite'
COVERAGE = 'coverage.json'
CONTEXT_RULE='located-source-context-v2'


def unique_offset(article: str, text: str) -> int | None:
    """Locate a passage only when it occurs exactly once."""
    if not text:
        return None
    first=article.find(text)
    if first < 0 or article.find(text,first+1) >= 0:
        return None
    return first


def passage_offset(source: str, text: str, metadata: dict) -> int | None:
    """Prefer a verified source span, then require a unique textual match."""
    start = metadata.get("char_start")
    end = metadata.get("char_end")
    snapshot = metadata.get("snapshot_text")
    if (
        type(start) is int and type(end) is int and isinstance(snapshot, str)
        and 0 <= start < end <= len(source) and source[start:end] == snapshot
    ):
        local = unique_offset(snapshot, text)
        if local is not None:
            return start + local
    return unique_offset(source, text)


def digest(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def object_id(kind, *parts):
    return kind+'_'+digest(parts)[:32]


def portable(value):
    """Exclude transport paths from identity, without altering source evidence."""
    if isinstance(value, dict):
        return {k: portable(v) for k, v in value.items()
                if not k.endswith('_local_path') and k not in {'local_path', 'build_directory'}}
    if isinstance(value, list):
        return [portable(v) for v in value]
    return value


def add_object(db, kind, oid, revision, title, sources, *, parent=None, path=None,
               payload=None, body='', capabilities=None, status='available',support_groups=None,search_title=None):
    payload = payload or {}
    row = db.execute('INSERT INTO objects(kind,id,revision,parent,title,path,source_ids,payload,capabilities,status) '
                     'VALUES(?,?,?,?,?,?,?,?,?,?)',
                     (kind, oid, revision, parent, title, path, encoded(sorted(set(sources))),
                      encoded(payload), encoded(capabilities or ['open']), status)).lastrowid
    # Native table parts share the search stream but are not object rows.
    # Appending prose to an enriched catalogue must allocate a search ID from
    # that stream, rather than reusing a native part's existing ID.
    search_id=db.execute('SELECT COALESCE(max(id),0)+1 FROM catalog_search_objects').fetchone()[0]
    db.execute('INSERT INTO catalog_search(rowid,title,body) VALUES(?,?,?)', (search_id, fts_text(title if search_title is None else search_title), fts_text(body)))
    db.execute('INSERT INTO catalog_search_objects VALUES(?,?,?)',(search_id,row,'object'))
    for group_no,group in enumerate(support_groups if support_groups is not None else [sources]):
        if not group:continue
        db.execute('INSERT INTO object_support_groups VALUES(?,?)',(row,group_no))
        db.executemany('INSERT INTO object_support_sources VALUES(?,?,?)',[(row,group_no,sid) for sid in sorted(set(group))])
    return row


def build_core(*, scope, snapshots, corpus, records, relations, source_paths,
               attachments, articles=(), producer=None,archive=None):
    """Build only from source-derived inputs; no questions or reviewer fields."""
    with tempfile.TemporaryDirectory(prefix='wiki-catalog-') as directory:
        file = Path(directory)/PATH
        record_values = [r.model_dump() if hasattr(r, 'model_dump') else r for r in records]
        build_index(sorted(record_values, key=lambda r: r['record_id']), file, scope=scope)
        with sqlite3.connect(file) as db:
            db.executescript('''
                CREATE TABLE objects(ordinal INTEGER PRIMARY KEY,kind TEXT,id TEXT,revision TEXT,parent TEXT,
                    title TEXT,path TEXT,source_ids TEXT,payload TEXT,capabilities TEXT,status TEXT,UNIQUE(kind,id));
                CREATE INDEX object_parent ON objects(parent,ordinal);
                CREATE INDEX original_entity_titles ON objects(title COLLATE NOCASE)
                    WHERE kind='entity' AND json_extract(payload,'$.identity_status')='original_article_identity';
                CREATE INDEX occurrence_source_ranges ON objects(json_extract(payload,'$.source_id'),
                    json_extract(payload,'$.start_char'),json_extract(payload,'$.end_char'))
                    WHERE kind='entity' AND json_extract(payload,'$.identity_status')='source_occurrence';
                CREATE TABLE object_sources(object INTEGER,source_id TEXT,PRIMARY KEY(object,source_id));
                CREATE INDEX object_source ON object_sources(source_id,object);
                CREATE TABLE object_support_groups(object INTEGER,group_no INTEGER,PRIMARY KEY(object,group_no));
                CREATE TABLE object_support_sources(object INTEGER,group_no INTEGER,source_id TEXT,
                    PRIMARY KEY(object,group_no,source_id));
                CREATE VIRTUAL TABLE catalog_search USING fts5(title,body,content='',tokenize='unicode61 remove_diacritics 2');
                CREATE TABLE catalog_search_objects(id INTEGER PRIMARY KEY,object INTEGER,origin TEXT);
                CREATE INDEX search_object ON catalog_search_objects(object,id);
                CREATE TABLE native_table_parts(id INTEGER PRIMARY KEY,object INTEGER,first_row INTEGER,first_column INTEGER,
                    last_row INTEGER,last_column INTEGER,cell_count INTEGER,body_sha256 TEXT);
                CREATE INDEX native_parts_object ON native_table_parts(object,id);
                CREATE TABLE units(id TEXT PRIMARY KEY,source_id TEXT,source_revision TEXT,start_char INTEGER,end_char INTEGER,
                    content_revision TEXT,article_key TEXT,context_refs TEXT);
                CREATE INDEX unit_source_range ON units(source_id,start_char,end_char);
                CREATE INDEX unit_original_article ON units(article_key,source_id,start_char);
                CREATE TABLE source_page_spans(source_id TEXT,start_char INTEGER,end_char INTEGER,page_number INTEGER,
                    source_revision TEXT,PRIMARY KEY(source_id,start_char,end_char,page_number));
                CREATE INDEX source_page_ranges ON source_page_spans(source_id,start_char,end_char);
                CREATE TABLE record_locations(record TEXT,span_no INTEGER,source_id TEXT,source_revision TEXT,
                    start_char INTEGER,end_char INTEGER,status TEXT,PRIMARY KEY(record,span_no));
                CREATE INDEX located_record ON record_locations(source_id,start_char,end_char);
                CREATE TABLE record_units(record TEXT,span_no INTEGER,unit TEXT,
                    PRIMARY KEY(record,span_no,unit));
                CREATE INDEX unit_records ON record_units(unit,record);
                CREATE TABLE context_tasks(id TEXT PRIMARY KEY,record TEXT,unit TEXT,kind TEXT,target TEXT,
                    required INTEGER,status TEXT,basis TEXT,payload TEXT);
                CREATE INDEX record_context ON context_tasks(record,required,status);
                CREATE TABLE context_members(record TEXT,task TEXT,reason TEXT,PRIMARY KEY(record,task));
                CREATE INDEX context_records ON context_members(task,record);
                CREATE TABLE record_topics(record TEXT,source_id TEXT,article_key TEXT,topic TEXT,
                    PRIMARY KEY(record,source_id,article_key));
                CREATE TABLE legacy_claims(claim_id TEXT,page_path TEXT,record_id TEXT,PRIMARY KEY(claim_id,page_path,record_id));
                CREATE TABLE placements(article TEXT,section TEXT,kind TEXT,material TEXT,role TEXT,reason TEXT,basis TEXT,
                    PRIMARY KEY(article,section,kind,material,role));
                CREATE TABLE prose_blocks(id TEXT PRIMARY KEY,section TEXT,ordinal INTEGER,revision TEXT,payload TEXT);
                CREATE INDEX section_blocks ON prose_blocks(section,ordinal);
                CREATE TABLE block_placements(article TEXT,section TEXT,block TEXT,kind TEXT,material TEXT,role TEXT,reason TEXT,basis TEXT,
                    PRIMARY KEY(article,section,block,kind,material,role));
                CREATE INDEX block_reference_scope ON block_placements(block,kind,material);
                CREATE TABLE dependencies(consumer_kind TEXT,consumer TEXT,input_kind TEXT,input_id TEXT,
                    input_revision TEXT,purpose TEXT,PRIMARY KEY(consumer_kind,consumer,input_kind,input_id,purpose));
                CREATE TABLE collection_predicates(article TEXT PRIMARY KEY,predicate TEXT,revision TEXT);
                CREATE TABLE resource_links(resource TEXT,unit TEXT,status TEXT,basis TEXT,
                    PRIMARY KEY(resource,unit,status));
                CREATE VIEW catalog_relations AS
                    SELECT id,json_extract(payload,'$.source') AS source,
                        json_extract(payload,'$.target') AS target,title AS predicate,
                        json_extract(payload,'$.source_id') AS source_id,payload
                    FROM objects WHERE kind='relation';
                CREATE TABLE identity_bindings(occurrence TEXT PRIMARY KEY,entity TEXT,payload TEXT);
                CREATE INDEX binding_entity ON identity_bindings(entity,occurrence);
                CREATE TABLE identity_binding_groups(occurrence TEXT,group_no INTEGER,PRIMARY KEY(occurrence,group_no));
                CREATE TABLE identity_binding_sources(occurrence TEXT,group_no INTEGER,source_id TEXT,
                    PRIMARY KEY(occurrence,group_no,source_id));
            ''')
            db.executemany('INSERT INTO meta VALUES(?,?)', [('catalog_rule', RULE), ('context_rule',CONTEXT_RULE),
                ('producer', encoded(portable(producer or {})))])
            source_hashes = {}
            for sid, snap in sorted(snapshots.items()):
                text = snap['text']; revision = hashlib.sha256(text.encode()).hexdigest()
                source_hashes[sid] = revision
                metadata = portable(snap.get('source_metadata') or {})
                title = metadata.get('source_name') or sid
                source = {k: portable(v) for k, v in snap.items() if k != 'text'}
                source.update(canonical_text_digest=revision, **{k:v for k,v in attachments.get(sid, {}).items() if k!='tables_inventory'})
                add_object(db, 'source', sid, revision, title, [sid], path=source_paths[sid], payload=source,
                           capabilities=['open', 'children', 'resources'], status='available' if text else 'partial')
                index_source_pages(db,sid,text,snap.get('source_metadata') or {},revision)
            for path, entry in sorted(corpus.page_index.items()):
                metadata = entry.scope or {}
                if metadata.get('reading_role') != 'article':
                    continue
                from jonex_wiki.domain.engine.query.retrieval import page_source_ids_from_markdown
                sids = metadata.get('source_ids') or page_source_ids_from_markdown(corpus.files[path])
                sid = next((s for s in sids if s in snapshots), None)
                if sid is None:
                    continue
                text = snapshots[sid]['text']; a, b = metadata.get('article_start'), metadata.get('article_end')
                if type(a) is not int or type(b) is not int or not 0 <= a < b <= len(text):
                    raise ValueError('CATALOG_UNIT_RANGE_INVALID:'+path)
                body = text[a:b]
                if metadata.get('body_sha256') != hashlib.sha256(body.encode()).hexdigest():
                    raise ValueError('CATALOG_UNIT_SOURCE_MISMATCH:'+path)
                owner = metadata.get('original_article_key') or path
                uid = object_id('unit', sid, path)
                # A reading page is a section, not automatically a different
                # original article. Unstructured source chapters share the
                # source document's article parent; aligned original articles
                # keep their authoritative distinct owners.
                aid = object_id('article', sid, metadata.get('original_article_key') or 'source_document')
                source_rev = source_hashes[sid]
                content_revision = digest([body, metadata.get('heading_path'), metadata.get('original_article_title')])
                revision = digest([source_rev, a, b, content_revision])
                context = list(metadata.get('reference_targets') or [])
                payload = {'source_id': sid, 'source_revision': source_rev, 'start_char': a, 'end_char': b,
                           'source_path': source_paths[sid], 'original_article_key': owner,
                           'scope': metadata, 'content_revision': content_revision, 'context_refs': context}
                add_object(db, 'unit', uid, revision, entry.title, [sid], parent=aid, path='wiki/'+path,
                           payload=payload, body=body, capabilities=['open', 'original', 'resources', 'follow'])
                db.execute('INSERT INTO units VALUES(?,?,?,?,?,?,?,?)',
                           (uid, sid, source_rev, a, b, content_revision, owner, encoded(context)))
                db.execute('INSERT INTO dependencies VALUES(?,?,?,?,?,?)', ('unit', uid, 'source', sid, source_rev, 'locator'))
                db.execute('INSERT INTO placements VALUES(?,?,?,?,?,?,?)',
                           (aid, uid, 'unit', uid, 'body', 'original continuous source section', 'exact_original_range'))
            index_context(db,record_values,snapshots,archive or {},source_hashes)
            groups = db.execute('SELECT DISTINCT parent FROM objects WHERE kind=? ORDER BY parent', ('unit',)).fetchall()
            for aid, in groups:
                rows = db.execute('SELECT id,title,source_ids,revision,payload FROM objects WHERE kind=? AND parent=? ORDER BY ordinal', ('unit', aid)).fetchall()
                sids = sorted({sid for row in rows for sid in json.loads(row[2])})
                payload = {'kind': 'source_reused', 'unit_refs': [r[0] for r in rows], 'state': 'ready_to_write',
                           'semantic_verification': 'original_reused', 'gaps': []}
                add_object(db, 'article', aid, digest([(r[0], r[3]) for r in rows]), rows[0][1], sids,
                           parent='sources', payload=payload, capabilities=['open', 'children', 'original'])
                # Recollection is source-bound here. An equal name in an added
                # source is a candidate, never an automatic identity merge.
                predicate = {'source_ids': sids, 'original_article_keys': sorted({json.loads(r[4])['original_article_key'] for r in rows}),
                             'expansion': 'located_relations_only', 'new_sources': 'reconsider_explicit_bindings'}
                db.execute('INSERT INTO collection_predicates VALUES(?,?,?)', (aid, encoded(predicate), digest(predicate)))
                for row in rows:
                    db.execute('INSERT INTO dependencies VALUES(?,?,?,?,?,?)', ('article', aid, 'unit', row[0], row[3], 'body'))
            for record in record_values:
                for display in record.get('displays') or []:
                    db.execute('INSERT OR IGNORE INTO legacy_claims VALUES(?,?,?)',
                               (display['claim_id'], display['page_path'], record['record_id']))
            occurrence_sources={n['entity_id']:n['source_id'] for n in relations.get('entities',[])}
            for binding in relations.get('identity_bindings', []):
                db.execute('INSERT INTO identity_bindings VALUES(?,?,?)', (binding['occurrence_id'], binding['entity_id'], encoded(binding)))
                fallback=sorted({binding['evidence']['source_id'],occurrence_sources.get(binding['occurrence_id'],binding['evidence']['source_id'])})
                for no,sources in enumerate(binding.get('source_groups') or [fallback]):
                    db.execute('INSERT INTO identity_binding_groups VALUES(?,?)',(binding['occurrence_id'],no))
                    db.executemany('INSERT INTO identity_binding_sources VALUES(?,?,?)',[(binding['occurrence_id'],no,sid) for sid in sources])
            for node in [*relations.get('entities', []), *relations.get('article_entities', [])]:
                sid = node['source_id']
                sources=node.get('source_ids') or [sid]
                groups=[[a['evidence']['source_id']] for a in node.get('evidence_alternatives',[])] or [sources]
                add_object(db, 'entity', node['entity_id'], digest(node), node['label'], sources,
                           payload=node, body=node['label'], capabilities=['open', 'follow'],support_groups=groups)
            for relation in relations.get('relations', []):
                sid = relation['source_id']
                if sid not in snapshots:
                    raise ValueError('CATALOG_RELATION_SOURCE_UNKNOWN')
                ref = relation['evidence']; text = snapshots[sid]['text']
                a, b = ref['start_char'], ref['end_char']
                if not 0 <= a < b <= len(text) or text[a:b] != ref['excerpt']:
                    raise ValueError('CATALOG_RELATION_RANGE_MISMATCH')
                add_object(db, 'relation', relation['relation_id'], digest(relation), relation['predicate'], relation.get('source_ids') or [sid],
                           payload=relation, body=ref['excerpt'],status=relation.get('support_status','unknown'))
            for sid, attachment in sorted(attachments.items()):
                for table in attachment.get('tables_inventory', []):
                    tid = table['table_id']; payload = {**{k:v for k,v in table.items() if k!='printed_text'}, 'source_id': sid, 'tables_path': attachment['tables'],
                        'tables_sha256':attachment.get('tables_sha256')}
                    title = str(table.get('title') or tid)
                    # Keep native title; coordinates and sheet give a useful,
                    # structurally supported label even for a title like 序号.
                    display = title+' · '+str(table.get('sheet_id', ''))+' '+str(table.get('cell_range') or '')
                    add_object(db, 'table', tid, digest(payload), display, [sid], parent=sid, payload=payload,
                               body=title+'\n'+table.get('printed_text',''), capabilities=['read_file'])
            bind_native_tables(db,snapshots,attachments,source_hashes)
            add_articles(db, articles)
            refresh_sources(db)
            coverage = coverage_summary(db)
            db.execute('INSERT INTO meta VALUES(?,?)', ('catalog_coverage', encoded(coverage)))
        return file.read_bytes(), coverage


def add_articles(db, articles):
    """Shared additive article publisher; body and components stay distinct."""
    db.execute('CREATE INDEX IF NOT EXISTS body_material_reference ON block_placements(kind,material,role,block)')
    for article in articles:
        # New synthesized prose has already passed source-only writing
        # validation; reference association is not semantic proof.
        aid = article['article_id']; payload = portable(article)
        article_groups=[]
        for section in article['sections']:
            section_groups=[];section_sources=set()
            for ordinal,block in enumerate(section['blocks']):
                block_sources=set();unit_map={}
                for uid in block.get('unit_refs') or []:
                    unit=db.execute("SELECT revision,source_ids FROM objects WHERE kind='unit' AND id=?",(uid,)).fetchone()
                    if unit is None:raise ValueError('ARTICLE_UNKNOWN_SOURCE_UNIT:'+uid)
                    if (block.get('unit_revisions') or {}).get(uid,unit[0])!=unit[0]:raise ValueError('ARTICLE_SOURCE_UNIT_REVISION_CHANGED')
                    unit_map[uid]=json.loads(unit[1]);block_sources.update(unit_map[uid])
                # Ownership includes every explicitly cited material;
                # the reader separately checks alternative support.
                block_sources.update(block.get('source_ids') or [])
                groups=[]
                for alternative in block.get('support_alternatives') or [{'all_of':list(unit_map)}]:
                    refs=alternative.get('all_of') or []
                    if not refs or not all(isinstance(ref,str) and ref in unit_map for ref in refs):
                        raise ValueError('ARTICLE_SUPPORT_GROUP_INVALID')
                    groups.append(sorted({sid for ref in refs for sid in unit_map[ref]}))
                if not groups:raise ValueError('ARTICLE_BLOCK_WITHOUT_SUPPORT')
                bid=block['block_id'];revision=digest(block)
                db.execute('INSERT INTO prose_blocks VALUES(?,?,?,?,?)',(bid,section['section_id'],ordinal,revision,encoded(block)))
                add_object(db,'block',bid,revision,section['title'],block_sources,parent=section['section_id'],
                    path='articles/'+aid+'/'+section['section_id']+'.md',
                        payload={'article_id':aid,'section_id':section['section_id'],'block_id':bid,
                                 'scope_role':block.get('scope_role','primary')},body=block['text'],
                    capabilities=['open','follow','original'],status=article['state'],support_groups=groups,search_title='')
                section_groups+=groups;section_sources.update(block_sources)
                for rid in block.get('record_refs', []):
                    db.execute('INSERT OR IGNORE INTO placements VALUES(?,?,?,?,?,?,?)',
                               (aid, section['section_id'], 'record', rid, 'body', section['goal'], 'record_referenced'))
                for kind,refs in [('record',block.get('record_refs',[])),('unit',block.get('unit_refs',[])),('resource',block.get('resource_refs',[]))]:
                    db.executemany('INSERT OR IGNORE INTO block_placements VALUES(?,?,?,?,?,?,?,?)',
                        [(aid,section['section_id'],bid,kind,ref,'body',section['goal'],'record_referenced' if kind=='record' else 'declared_source_reference') for ref in refs])
            section_payload={'article_id':aid,'goal':section['goal'],'block_ids':[b['block_id'] for b in section['blocks']],
                'block_count':len(section['blocks']),'local_issues':section.get('issues') or []}
            add_object(db,'section',section['section_id'],digest(section),section['title'],section_sources,parent=aid,
                path='articles/'+aid+'/'+section['section_id']+'.md',
                payload=section_payload,capabilities=['open','follow','original'],status=article['state'],support_groups=section_groups)
            article_groups+=section_groups
        add_object(db, 'synthesis', aid, digest(payload), article['title'], article['source_ids'],
                   path='articles/'+aid+'.md',
                   parent='articles', payload={'section_ids':[s['section_id'] for s in article['sections']],
                    'gap_count':len(article.get('gaps') or []),'component_count':len(article.get('components') or []),
                    'state':article['state'],'semantic_verification':article.get('semantic_verification','not_assessed'),
                    'composition_mode':article.get('composition_mode','generated')},
                   capabilities=['open', 'follow', 'children', 'original'], status=article['state'],support_groups=article_groups)
        for component in article.get('components', []):
            db.execute('INSERT OR IGNORE INTO placements VALUES(?,?,?,?,?,?,?)',
                       (aid, '', component['kind'], component['id'], 'component', component['reason'], component.get('basis', 'candidate')))


def index_source_pages(db,sid,text,metadata,revision):
    """Keep parser page ranges only after checking their canonical container.

    Page adjacency is a reading path, not a semantic table/paragraph binding.
    Packed prose and separate tables may occur far apart in canonical text.
    """
    for chunk in metadata.get('chunks') or []:
        meta=chunk.get('metadata') or {};body=meta.get('snapshot_text')
        if not isinstance(body,str) or not body:continue
        start=meta.get('char_start')
        if type(start) is not int or text[start:start+len(body)]!=body:
            start=unique_offset(text,body)
        if start is None:continue
        for location in meta.get('source_locations') or []:
            a,b,page=(location.get(k) for k in ('char_start','char_end','page_number'))
            if not (type(a) is int and type(b) is int and type(page) is int and 0<=a<b<=len(body) and page>0):continue
            db.execute('INSERT OR IGNORE INTO source_page_spans VALUES(?,?,?,?,?)',
                (sid,start+a,start+b,page,revision))


def article_resource_index_path(source_id):
    """Stable, filesystem-safe Markdown location for a source's resources."""
    return 'articles/resources/source-' + hashlib.sha256(source_id.encode()).hexdigest()[:20] + '.md'


def article_resource_navigation(source_ids, resource_sources, prefix):
    """Link to the source inventory without claiming a paragraph-table match."""
    links = ['[' + sid + '](' + prefix + article_resource_index_path(sid) + ')'
             for sid in sorted(set(source_ids)) if sid in resource_sources]
    return '来源表图目录：' + '；'.join(links) if links else ''


def render_article_section(section, paths, resources, prefix):
    """Render the same paragraph offsets for file reading and citation lookup."""
    from .source_heading_references import render_reference_navigation
    from .table_navigation import table_navigation_text
    body=['## '+section['title'], '', section['goal'], '']
    source_ids=[sid for block in section['blocks'] for sid in block.get('source_ids',[])]
    resource_nav=article_resource_navigation(source_ids,resources,prefix)
    if resource_nav:body.extend([resource_nav,''])
    spans={};candidates=[]
    for block in section['blocks']:
        text=block['text'];start=sum(len(part)+1 for part in body)
        if block.get('block_id'):spans[block['block_id']]=(start,start+len(text))
        body.extend([text,''])
        if block.get('nearby_tables'):
            body.append('附近表格（按来源页及顺序定位，使用前核对）：')
            body.extend('- '+table_navigation_text(table,prefix) for table in block['nearby_tables'])
            body.append('')
        body.extend(['原文：'+'；'.join('['+uid+']('+prefix+paths[uid]+')' for uid in block['unit_refs']), ''])
        candidates.extend(block.get('resource_refs') or [])
        if block.get('source_references'):
            body.extend([render_reference_navigation(block['source_references'],prefix),''])
    if candidates:
        body.extend(['## 原有未核实的表格与图片候选', '', ', '.join(dict.fromkeys(candidates)), ''])
    return body,spans


def article_files(articles, catalog):
    """Human-readable projections; the scoped catalog remains authoritative.

    These files deliberately stay outside legacy fact pages: synthesized prose
    is an orientation layer, not a new original-source citation.
    """
    with tempfile.TemporaryDirectory(prefix='wiki-article-projection-') as directory:
        path=Path(directory)/PATH;path.write_bytes(catalog)
        with sqlite3.connect(path) as db:
            from .relation_reading_projection import reading_files
            tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type IN ('table','view')")}
            has_relation_reading={'objects','units','records','record_locations','record_units','catalog_relations'} <= tables
            if has_relation_reading:
                yield from reading_files(db)
            paths={uid:target for uid,target in db.execute("SELECT id,path FROM objects WHERE kind='unit'")}
            source_rows=list(db.execute("SELECT id,title,payload FROM objects WHERE kind='source'"))
            sources={sid:json.loads(payload) for sid,_,payload in source_rows}
            titles={sid:title for sid,title,_ in source_rows}
            resources={}
            for kind,rid,title,sid,payload in db.execute(
                    "SELECT kind,id,title,parent,payload FROM objects WHERE kind IN ('table','image') ORDER BY parent,kind,title,id"):
                resources.setdefault(sid,[]).append((kind,rid,title,json.loads(payload)))
    from .table_navigation import table_directory
    tables_index=table_directory(titles,resources)
    if tables_index:
        yield 'articles/tables/index.md',tables_index
    for sid in sorted(resources):
        lines=['# '+str(titles.get(sid) or sid)+'：表格与图片', '',
               '来源 ID：`'+sid+'`', '',
               '这是同一来源的资源目录；表格链接指向完整逻辑表文件。资源与文章段落的对应关系需要结合内容判断。', '']
        from .table_navigation import table_navigation_text
        for kind,rid,title,payload in resources[sid]:
            label='表格' if kind=='table' else '图片'
            if kind=='table' and payload.get('table_id'):
                lines.append('- '+label+' '+table_navigation_text(payload,'../../'))
            else:
                lines.append('- '+label+' `'+rid+'` — '+str(title or '').replace('\n',' '))
        yield article_resource_index_path(sid), ('\n'.join(lines)+'\n').encode()
    navigation=['# 综合文章', '', '正文依据与组成材料分别保存；引用关联不等于语义已核验。', '']
    if has_relation_reading:
        navigation += ['[来源关系阅读目录](linked/index.md) · [来源事实检索索引](../claims/index.jsonl)', '']
    if tables_index:
        navigation += ['[完整逻辑表目录](tables/index.md)', '']
    for article in articles:
        aid=article['article_id']
        root=['# '+article['title'], '', '状态：'+article['state']+'；语义核验：'+article.get('semantic_verification','not_assessed')+'。', '']
        root.extend(['## 章节', ''])
        root.extend('- ['+section['title']+']('+aid+'/'+section['section_id']+'.md)' for section in article['sections'])
        root.append('')
        for section in article['sections']:
            target=aid+'/'+section['section_id']+'.md'
            root.extend(render_article_section(section,paths,resources,'../')[0])
            yield 'articles/'+target, '\n'.join(render_article_section(section,paths,resources,'../../')[0]).encode()
        root.extend(['## 来源与原件', ''])
        for sid in article['source_ids']:
            source=sources[sid]
            if source.get('original'):
                root.append('- ['+sid+' 原件](../'+source['original']+')')
            else:
                root.append('- '+sid+'：'+str((source.get('original_availability') or {}).get('status','original_not_packaged')))
        root.extend(['', '## 尚未解决的问题', '', json.dumps(article.get('gaps') or [],ensure_ascii=False,indent=2),
                     '', '## 保留的组成材料', '', json.dumps(article.get('components') or [],ensure_ascii=False,indent=2), ''])
        full_article='\n'.join(root).encode()
        if len(full_article)>250_000 and article['sections']:
            outline=['# '+article['title'], '', '文章较长，可直接阅读相关章节；完整文章与全部组成材料仍保留。',
                     '', '[完整文章](../'+aid+'.md)', '', '## 章节', '']
            outline.extend('- ['+section['title']+'](../'+aid+'/'+section['section_id']+'.md)'
                           for section in article['sections'])
            for sid in article['source_ids']:
                if sid in resources:
                    outline.append('- ['+str(titles.get(sid) or sid)+'：表格与图片](../'+
                                   article_resource_index_path(sid).removeprefix('articles/')+')')
            yield 'articles/outlines/'+aid+'.md', ('\n'.join(outline)+'\n').encode()
            navigation.append('- ['+article['title']+'](outlines/'+aid+'.md) · '+article['state'])
        else:
            navigation.append('- ['+article['title']+']('+aid+'.md) · '+article['state'])
        yield 'articles/'+aid+'.md',full_article
    if articles:
        yield 'articles/index.md','\n'.join(navigation).encode()


def index_context(db,records,snapshots,archive,source_hashes):
    """Locate once at compile time; repeated text requires verified coordinates.

    Context tasks are source-derived reading obligations, not assertions that
    pronouns/conditions have already been semantically resolved.
    """
    offsets={}
    for pid,passage in archive.items():
        if hasattr(passage,'model_dump'):passage=passage.model_dump()
        sid=passage['source_id'];text=passage['text'];source=snapshots.get(sid,{}).get('text','')
        base=passage_offset(source,text,passage.get('source_metadata') or {})
        offsets[pid]=(base,text,sid)
    empty_source_revision = hashlib.sha256(b'').hexdigest()
    passage_hashes = {}
    unit_ids_by_path = None

    @cache
    def article_lead(source_id, article_key):
        return db.execute(
            'SELECT id,start_char,end_char FROM units WHERE source_id=? AND article_key=? '
            'ORDER BY start_char LIMIT 1', (source_id,article_key)
        ).fetchone()

    for record in records:
        rid=record['record_id']
        for ordinal,span in enumerate(record.get('source_spans') or []):
            sid=span['source_id'];source=snapshots.get(sid,{}).get('text','')
            revision=source_hashes.get(sid,empty_source_revision)
            passage_id=span.get('passage_id')
            base,passage_text,owner=offsets.get(passage_id,(None,'',None))
            a,b=span.get('start_char'),span.get('end_char');quote=span.get('text') or span.get('source_excerpt')
            valid=(base is not None and owner==sid and type(a) is int and type(b) is int and
                   0<=a<b<=len(passage_text) and passage_text[a:b]==quote and source[base+a:base+b]==quote)
            if span.get('source_sha256') and span['source_sha256']!=snapshots.get(sid,{}).get('content_sha256'):
                valid=False
            if span.get('passage_text_sha256'):
                if passage_id not in passage_hashes:
                    passage_hashes[passage_id]=hashlib.sha256(passage_text.encode()).hexdigest()
                valid=valid and span['passage_text_sha256']==passage_hashes[passage_id]
            start,end=(base+a,base+b) if valid else (None,None)
            db.execute('INSERT INTO record_locations VALUES(?,?,?,?,?,?,?)',
                       (rid,ordinal,sid,revision,start,end,'located' if valid else 'unresolved'))
            if not valid:
                task=object_id('context',rid,ordinal,'unresolved_location')
                db.execute('INSERT INTO context_tasks VALUES(?,?,?,?,?,?,?,?,?)',
                    (task,rid,'','source_location','',1,'unresolved','no_unique_or_verified_passage_mapping',encoded({'source_id':sid})))
                db.execute('INSERT OR IGNORE INTO context_members VALUES(?,?,?)',(rid,task,'unresolved_source_location'))
                continue
            units=db.execute('SELECT id,article_key,context_refs FROM units '
                'WHERE source_id=? AND start_char<? AND end_char>? ORDER BY start_char',(sid,end,start)).fetchall()
            for uid,article_key,context_refs in units:
                db.execute('INSERT OR IGNORE INTO record_units VALUES(?,?,?)',(rid,ordinal,uid))
                db.execute('INSERT OR IGNORE INTO record_topics VALUES(?,?,?,?)',
                    (rid,sid,article_key,record.get('topic_hint') or ''))
                lead=article_lead(sid, article_key)
                if lead and (record.get('unbound') or start>lead[1]):
                    lead_unit,lead_start,lead_end=lead
                    intro_end=source.find('\n\n',lead_start,lead_end)
                    if intro_end<0:intro_end=lead_end
                    task=object_id('context',uid,'original_lead',lead_unit)
                    db.execute('INSERT OR IGNORE INTO context_tasks VALUES(?,?,?,?,?,?,?,?,?)',
                        (task,'',uid,'original_lead',lead_unit,1,'context_available',
                         'same_original_article_boundary',encoded({'semantic_resolution':'not_assessed',
                         'intro_range':[lead_start,intro_end]})))
                    db.execute('INSERT OR IGNORE INTO context_members VALUES(?,?,?)',(rid,task,encoded({'unbound':record.get('unbound') or [],
                        'reason':'stored gap or source hit follows its owning article/section introduction'})))
                for ref in json.loads(context_refs):
                    target=None
                    if ref.get('status')=='resolved' and ref.get('target'):
                        if unit_ids_by_path is None:
                            # Source units stay fixed for the entire context binding phase.
                            unit_ids_by_path = dict(db.execute("SELECT path,id FROM objects WHERE kind='unit'"))
                        target=unit_ids_by_path.get('wiki/'+ref['target'])
                    task=object_id('context',uid,'explicit_reference',target or ref.get('mention'),ref.get('status'))
                    db.execute('INSERT OR IGNORE INTO context_tasks VALUES(?,?,?,?,?,?,?,?,?)',
                        (task,'',uid,'explicit_reference',target or '',1,'context_available' if target else 'unresolved',
                         'explicit_reference_in_same_source',encoded(ref)))
                    db.execute('INSERT OR IGNORE INTO context_members VALUES(?,?,?)',(rid,task,encoded(ref)))


def refresh_sources(db):
    db.execute('DELETE FROM object_sources')
    db.execute('INSERT INTO object_sources SELECT o.ordinal,j.value FROM objects o,json_each(o.source_ids) j')


def bind_native_tables(db,snapshots,attachments,source_hashes):
    """Use typed parser anchors, never a title/name co-occurrence, for binding."""
    for sid,snapshot in snapshots.items():
        body=snapshot['text'];sha=source_hashes[sid]
        for chunk in snapshot.get('source_metadata',{}).get('chunks',[]):
            meta=chunk.get('metadata') or {};tid=meta.get('table_id');text=meta.get('snapshot_text')
            if not tid or not isinstance(text,str) or not text:continue
            if not db.execute("SELECT 1 FROM objects WHERE kind='table' AND id=? AND parent=?",(tid,sid)).fetchone():continue
            a,b=meta.get('char_start'),meta.get('char_end')
            if not (type(a) is int and type(b) is int and body[a:b]==text):
                a=unique_offset(body,text);b=a+len(text) if a is not None else None
            if a is None:continue
            for uid, in db.execute('SELECT id FROM units WHERE source_id=? AND start_char<? AND end_char>?',(sid,b,a)):
                basis={'kind':'trusted_parse_table_anchor','source_id':sid,'canonical_text_digest':sha,
                    'start_char':a,'end_char':b,'chunk_id':chunk.get('chunk_id'),
                    'tables_sha256':attachments.get(sid,{}).get('tables_sha256'),
                    'semantic_association':'not_assessed'}
                db.execute('INSERT OR IGNORE INTO resource_links VALUES(?,?,?,?)',(tid,uid,'located',encoded(basis)))


def coverage_summary(db):
    counts = dict(db.execute('SELECT kind,count(*) FROM objects GROUP BY kind'))
    resources = counts.get('table', 0)+counts.get('image', 0)
    bound = db.execute("SELECT count(DISTINCT resource) FROM resource_links WHERE status='located'").fetchone()[0]
    contexts=dict(db.execute('SELECT status,count(*) FROM context_tasks GROUP BY status'))
    locations=dict(db.execute('SELECT status,count(*) FROM record_locations GROUP BY status'))
    originals={}
    for payload, in db.execute("SELECT payload FROM objects WHERE kind='source'"):
        source=json.loads(payload)
        state=(source.get('original_availability') or {}).get('status') or ('packaged_unclassified' if source.get('original') else 'not_packaged')
        originals[state]=originals.get(state,0)+1
    return {'schema': RULE, 'sources': {'available_with_entry': counts.get('source', 0)},
            'record_locations':locations,'context_tasks':contexts,
            'context_memberships':db.execute('SELECT count(*) FROM context_members').fetchone()[0],
            'units': counts.get('unit', 0), 'records': db.execute('SELECT count(*) FROM records').fetchone()[0],
            'legacy_claim_mappings': db.execute('SELECT count(*) FROM legacy_claims').fetchone()[0],
            'resources': {'registered': resources, 'bound': bound, 'candidate_or_unbound': resources-bound},
            'originals':originals,
            'articles': {'source_reused': counts.get('article', 0), 'synthesized': counts.get('synthesis', 0)},
            'object_counts': counts, 'semantics': 'Range checks and record references do not prove semantic correctness.'}


def enrich_resources(publication):
    """Finish the private candidate after the existing native media builder."""
    publication = Path(publication)
    with sqlite3.connect(publication/PATH) as db:
        media = publication/'query/media_catalog.json'
        if media.is_file():
            source_texts={}
            for asset in json.loads(media.read_text()).get('assets', []):
                source_path = asset.get('source_path')
                source = db.execute("SELECT id FROM objects WHERE kind='source' AND path=?", (source_path,)).fetchone()
                if source is None:
                    continue
                sid = source[0]; payload = portable(asset)
                add_object(db, 'image', asset['asset_id'], digest(payload), asset.get('caption') or asset['asset_id'], [sid],
                           parent=sid, path=asset['path'], payload=payload, body=asset.get('caption') or asset.get('ocr_text') or '',
                           capabilities=['open', 'image_inspect'])
                span = asset.get('source_span') or {}
                a,b=(span if isinstance(span,list) and len(span)==2 else
                     (span.get('start_char'),span.get('end_char')) if isinstance(span,dict) else (None,None))
                if type(a) is int and type(b) is int:
                    for uid, in db.execute('SELECT id FROM units WHERE source_id=? AND start_char<=? AND end_char>=?', (sid, a, b)):
                        if source_path not in source_texts:
                            if len(source_texts)>=4:source_texts.pop(next(iter(source_texts)))
                            original=(publication/source_path).read_text()
                            source_texts[source_path]=(original,hashlib.sha256(original.encode()).hexdigest())
                        original,original_sha=source_texts[source_path]
                        marker=asset.get('source_marker')
                        located=bool(marker and marker in original[a:b] and
                            original_sha==asset.get('source_body_sha256'))
                        db.execute('INSERT OR IGNORE INTO resource_links VALUES(?,?,?,?)',
                                   (asset['asset_id'], uid, 'located' if located else 'candidate',
                                    'exact_source_image_marker; semantic association unverified' if located else
                                    'source image text location; semantic association unverified'))
        bind_native_observations(db,publication)
        index_native_cells(db,publication)
        refresh_sources(db)
        coverage = coverage_summary(db)
        db.execute('UPDATE meta SET value=? WHERE key=?', (encoded(coverage), 'catalog_coverage'))
    (publication/COVERAGE).write_text(encoded(coverage))
    return coverage


def index_authored_articles(path, registered_files, overlays):
    """Index reachable authored prose without changing source and table ranking."""
    from jonex_wiki.domain.reading.authored_articles import (
        _MAX_ARTICLE_BYTES, authored_link_targets, iter_authored_articles,
    )

    def read(name):
        content = overlays[name]
        if len(content) > _MAX_ARTICLE_BYTES:
            raise ValueError("ARTICLE_TOO_LARGE")
        return content.decode("utf-8")

    articles = iter_authored_articles(overlays, read)
    first = next(articles, None)
    if first is None:
        return False

    with closing(sqlite3.connect(path)) as db, db:
        db.execute("BEGIN")
        db.execute(
            "CREATE TABLE authored_articles(ordinal INTEGER PRIMARY KEY, "
            "path TEXT NOT NULL UNIQUE, title TEXT NOT NULL, source_ids TEXT NOT NULL)"
        )
        db.execute(
            "CREATE VIRTUAL TABLE authored_search USING "
            "fts5(title,body,content='',tokenize='unicode61 remove_diacritics 2')"
        )
        owners = {}
        for target, sources in db.execute(
            """SELECT CASE WHEN kind='table' THEN json_extract(payload,'$.page_path')
                           ELSE path END,source_ids
               FROM objects WHERE kind IN ('source','unit','table')"""
        ):
            if target in registered_files:
                owners.setdefault(target, set()).update(json.loads(sources))
        for target, title, body in chain((first,), articles):
            sources = sorted({
                sid
                for link in authored_link_targets(target, body)
                for sid in owners.get(link, ())
            })
            ordinal = db.execute(
                "INSERT INTO authored_articles(path,title,source_ids) VALUES(?,?,?)",
                (target, title, encoded(sources)),
            ).lastrowid
            db.execute(
                "INSERT INTO authored_search(rowid,title,body) VALUES(?,?,?)",
                (ordinal, fts_text(title), fts_text(body)),
            )
    return True


def index_native_cells(db,publication):
    """Index saved native values in bounded parts, including zero-claim XLSX.

    No table is reconstructed or interpreted here. The native reader remains
    responsible for merges, headers, formulas and actual complete delivery.
    """
    from jonex_wiki.domain.engine.query.table_reader import read_logical_table,stream_cells
    signature=digest(['native-common-search-v1',list(db.execute(
        "SELECT id,revision FROM objects WHERE kind IN ('source','table') ORDER BY kind,id"))])
    previous=db.execute("SELECT value FROM meta WHERE key='native_cells_index'").fetchone()
    if previous:
        if previous[0]==signature:return
        raise ValueError('CATALOG_NATIVE_INDEX_REQUIRES_FRESH_BUILD')
    db.execute('DELETE FROM native_table_parts')
    for source in db.execute("SELECT id,payload FROM objects WHERE kind='source' ORDER BY id").fetchall():
        sid,payload=source;path=json.loads(payload).get('tables')
        if not path:continue
        with sqlite3.connect((publication/path).resolve().as_uri()+'?mode=ro&immutable=1',uri=True) as grid:
            grid.row_factory=sqlite3.Row
            for obj in db.execute("SELECT ordinal,id FROM objects WHERE kind='table' AND parent=? ORDER BY id",(sid,)).fetchall():
                ordinal,tid=obj;table=read_logical_table(grid,tid);parts=[];size=0;first=None;last=None;count=0
                def flush():
                    nonlocal parts,size,first,last,count
                    if not parts:return
                    text='\n'.join(parts)
                    db.execute('INSERT INTO native_table_parts(object,first_row,first_column,last_row,last_column,cell_count,body_sha256) VALUES(?,?,?,?,?,?,?)',
                        (ordinal,*first,*last,count,hashlib.sha256(text.encode()).hexdigest()))
                    search_id=db.execute('INSERT INTO catalog_search_objects(object,origin) VALUES(?,?)',(ordinal,'native_cells')).lastrowid
                    db.execute('INSERT INTO catalog_search(rowid,title,body) VALUES(?,?,?)',(search_id,'',fts_text(text)))
                    parts=[];size=0;first=last=None;count=0
                for cell in stream_cells(grid,table):
                    cell=dict(cell);point=(cell['row_index'],cell['column_index'])
                    values=list(dict.fromkeys(str(cell[key]) for key in ('raw_value','cached_value','formula') if cell.get(key) is not None))
                    text=' '.join(values)
                    if not text:continue
                    # Even a very large single cell is indexed without a large
                    # accumulator. Part coordinates remain the saved cell.
                    for start in range(0,len(text),16000):
                        value=text[start:start+16000]
                        if parts and size+len(value)>16000:flush()
                        if first is None:first=point
                        last=point;parts.append(value);size+=len(value);count+=1
                flush()
    db.execute("INSERT INTO meta VALUES('native_cells_index',?)",(signature,))


def bind_native_observations(db,publication):
    """Join saved table observations to exact source ranges, not captions/names."""
    for source in db.execute("SELECT id,path,payload FROM objects WHERE kind='source'").fetchall():
        sid,source_path,payload=source;metadata=json.loads(payload);relative=metadata.get('tables')
        if not relative:continue
        text=(publication/source_path).read_text();sha=hashlib.sha256(text.encode()).hexdigest()
        with sqlite3.connect((publication/relative).resolve().as_uri()+'?mode=ro&immutable=1',uri=True) as grid:
            grid.row_factory=sqlite3.Row
            if not grid.execute("SELECT 1 FROM sqlite_master WHERE name='table_observations'").fetchone():continue
            for observation in grid.execute('SELECT table_id,raw_item_json FROM table_observations ORDER BY table_id'):
                tid=observation['table_id'];raw=json.loads(observation['raw_item_json'] or '{}')
                item=raw.get('raw_item') or {};printed=raw.get('text') or ''
                candidates=[printed,item.get('table_body') or '',printed.split('\n\n',1)[0]]
                for quote in dict.fromkeys(candidates):
                    if len(quote)<16:continue
                    start=unique_offset(text,quote)
                    if start is None:continue
                    end=start+len(quote)
                    basis={'kind':'exact_native_table_observation','source_id':sid,'canonical_text_digest':sha,
                        'start_char':start,'end_char':end,'tables_sha256':metadata.get('tables_sha256'),
                        'semantic_association':'not_assessed'}
                    for uid, in db.execute('SELECT id FROM units WHERE source_id=? AND start_char<? AND end_char>?',(sid,end,start)):
                        db.execute('INSERT OR IGNORE INTO resource_links VALUES(?,?,?,?)',(tid,uid,'located',encoded(basis)))
                    break
