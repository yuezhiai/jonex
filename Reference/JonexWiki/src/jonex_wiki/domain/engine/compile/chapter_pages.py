"""Source-bound continuous reading pages; fine claims retain their own identity.

The source is partitioned exactly once. Complete sibling sections are packed
under their original parent; oversized sections descend to subsections, then
complete paragraph/table units. No model rewrites or cross-source merges occur.
"""
from __future__ import annotations

import hashlib
import re
from bisect import bisect_right
from collections import defaultdict
from typing import Any, Iterable

from jonex_wiki.domain.engine.compile.publication import _source_intervals
from jonex_wiki.domain.engine.compile.source_prep.reading_materials import _heading_scan
from jonex_wiki.domain.engine.models.query_models import RawWikiPage

READING_RULE = 'source-chapters-v2'
PAGE_TARGET = 16000
_REFERENCE = re.compile(r'\b(Note|Item|Section|Chapter|Article|Appendix|Annex)\s+(\d+(?:\.\d+)*[A-Za-z]?|[A-Za-z])(?=\W|$)', re.I)
_NUMBERED_HEADING = re.compile(r'^(\d+(?:\.\d+)*)(?:[.)]?\s+)')
_CHINESE_REFERENCE = re.compile(r'(?:第\s*([0-9一二三四五六七八九十百零〇]+)\s*([章节条款])|附注\s*([0-9一二三四五六七八九十百零〇]+))')


def _heading_only(text: str) -> bool:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return bool(lines) and all(line.startswith('#') for line in lines)


def chapter_ranges(text: str, target: int = PAGE_TARGET) -> tuple[list[tuple[int, int]], list[dict[str, Any]]]:
    """Return contiguous original ranges and the full (subtree) outline."""
    sections = _heading_scan(text)
    children = defaultdict(list)
    for section in sections:
        children[section['parent']].append(section)

    def partition(start, end, descendants):
        if end-start <= target:
            return [(start, end)]
        if not descendants:
            units = [(start+a, start+b) for a,b,_ in _source_intervals(text[start:end], target)]
        else:
            units = [(start, descendants[0]['start'])] if start < descendants[0]['start'] else []
            for child in descendants:
                units.extend(partition(child['start'], child['end'], children[child['start']]))
        prefix_end = descendants[0]['start'] if descendants else start
        result, begin, stop = [], start, start
        for a,b in units:
            # Substantive original sections stay independent. Only short
            # siblings share a reading page; this is a display budget, not a
            # rule declaring neighbouring facts to be the same topic/entity.
            standalone = b-a >= target//4
            # A cover/title or parent introduction belongs with its first
            # child. Do not create a nearly empty page before a large child.
            carry_prefix = stop-begin < target//4 and (
                (begin == start and stop <= prefix_end) or not result)
            if (b-begin > target or standalone) and stop>begin and not carry_prefix and not _heading_only(text[begin:stop]):
                result.append((begin,stop)); begin=a
            stop=b
            if standalone:
                result.append((begin,stop)); begin=stop
        if stop>begin:
            result.append((begin,stop))
        return result

    return (partition(0,len(text),children[None]) if text else []), sections


def _chinese_key(match: re.Match) -> str:
    # Match the identifier actually printed in this source. Different written
    # forms are not silently equated (e.g. 十 and 10 require disambiguation).
    return ('zh:' + match.group(2) + ':' + match.group(1)) if match.group(1) else ('zh:附注:' + match.group(3))


def _references(text: str) -> Iterable[tuple[int, str, str]]:
    matches = [(m.start(), m.group(), m.group(1).casefold()+':'+m.group(2).casefold())
               for m in _REFERENCE.finditer(text)]
    matches.extend((m.start(), m.group(), _chinese_key(m)) for m in _CHINESE_REFERENCE.finditer(text))
    return sorted(matches)


def _reference_keys(title: str) -> list[str]:
    chinese = _CHINESE_REFERENCE.match(title.strip())
    if chinese:
        return [_chinese_key(chinese)]
    match = _REFERENCE.match(title.strip())
    if match:
        return [match.group(1).casefold()+':'+match.group(2).casefold()]
    match = _NUMBERED_HEADING.match(title.strip())
    return ['section:'+match.group(1)] if match else []


def article_pages(source_id: str, text: str, source_name: str = '', *, original_structure=None) -> list[RawWikiPage]:
    if original_structure:
        return structured_article_pages(source_id,text,source_name,original_structure)
    ranges, headings = chapter_ranges(text)
    slug = hashlib.sha256(source_id.encode()).hexdigest()[:16]
    article_sha = hashlib.sha256(text.encode()).hexdigest()
    paths = [f'source-articles/{slug}/{i:04d}.md' for i in range(len(ranges))]
    targets = defaultdict(list)
    for heading in headings:
        for key in _reference_keys(heading['title']):
            for i,(start,end) in enumerate(ranges):
                if start<=heading['start']<end:
                    targets[key].append(paths[i]); break
    result=[]
    for i,(start,end) in enumerate(ranges):
        body=text[start:end]
        if not body.strip():
            continue
        ancestors=[h for h in headings if h['start']<=start<h['end']]
        contained=[h for h in headings if start<=h['start']<end]
        if contained:
            level=min(h['level'] for h in contained)
            titles=[h['title'] for h in contained if h['level']==level]
        else:
            titles=[ancestors[-1]['title']] if ancestors else ['正文']
        name=source_name or source_id
        title=name[:80]+' · '
        for heading_title in titles:
            addition=heading_title if title.endswith(' · ') else ' / '+heading_title
            if len(title+addition)>197:
                title+=' …';break
            title+=addition
        references=[]
        for position, mention, key in _references(body):
            candidates=targets.get(key,[])
            references.append({'mention':mention, 'status':'resolved' if len(candidates)==1 else ('ambiguous' if candidates else 'unresolved'),
                'target':candidates[0] if len(candidates)==1 else None,
                'basis':'unique_heading_in_same_source', 'source_start':start+position})
            if len(references)>=64:break
        related=([paths[i-1]] if i else [])+([paths[i+1]] if i+1<len(paths) else [])
        related+= [r['target'] for r in references if r['target'] and r['target']!=paths[i]]
        result.append(RawWikiPage(path=paths[i], body=body, content_kind='source', source_id=source_id,
            title=title, aliases=list(dict.fromkeys([name,*[h['title'] for h in contained]]))[:20],
            tags=['article','chapter','source'], related_pages=list(dict.fromkeys(related)),
            scope={'reading_role':'article', 'reading_rule':READING_RULE,
                'source_ids':[source_id],
                'article_start':start, 'article_end':end, 'article_sha256':article_sha,
                'body_sha256':hashlib.sha256(body.encode()).hexdigest(),
                'heading_path':[h['title'] for h in ancestors],
                'section_titles':[h['title'] for h in contained],
                'source_name':name, 'reference_targets':references}))
    return result


def structured_article_pages(source_id, text, source_name, structure):
    """Project supplied original paragraph metadata without changing one byte.

    The importer must provide exact, non-overlapping source coordinates and a
    matching whole-article hash. Unassigned ranges remain readable, explicitly
    without an inferred article identity. No adjacent topics are merged.
    """
    digest=hashlib.sha256(text.encode()).hexdigest()
    if structure.get('article_sha256')!=digest or not structure.get('authority'):
        raise ValueError('ORIGINAL_STRUCTURE_SOURCE_MISMATCH')
    sections=sorted(structure.get('sections',[]),key=lambda x:(x['start'],x['end']))
    spans=[];cursor=0
    for section in sections:
        a,b=section['start'],section['end']
        if (type(a) is not int or type(b) is not int or a<cursor or b<=a or b>len(text)
                or not str(section.get('title','')).strip() or not section.get('paragraph_id')):
            raise ValueError('ORIGINAL_STRUCTURE_RANGE_INVALID')
        if a>cursor:spans.append((cursor,a,None))
        spans.append((a,b,section));cursor=b
    if cursor<len(text):spans.append((cursor,len(text),None))
    normalized=[];leading=None
    for a,b,section in spans:
        if not text[a:b].strip():
            if normalized:
                lo,_,owner=normalized[-1];normalized[-1]=(lo,b,owner)
            elif leading is None:leading=a
            continue
        normalized.append((a if leading is None else leading,b,section));leading=None
    slug=hashlib.sha256(source_id.encode()).hexdigest()[:16];pages=[]
    for begin,end,section in normalized:
        ranges,_=chapter_ranges(text[begin:end])
        for lo,hi in ranges:
            a,b=begin+lo,begin+hi;body=text[a:b]
            title=section['title'] if section else (source_name or source_id)+' · 未归属原文'
            path=f'source-articles/{slug}/{len(pages):04d}.md'
            scope={'reading_role':'article','reading_rule':READING_RULE,'article_start':a,'article_end':b,
                'article_sha256':digest,'body_sha256':hashlib.sha256(body.encode()).hexdigest(),
                'source_name':source_name or source_id,'heading_path':[],'section_titles':[title],
                'structure_status':'original_paragraph' if section else 'unassigned_original',
                'structure_authority':structure['authority'],'source_ids':[source_id]}
            if section:scope.update(original_article_title=section['title'],original_paragraph_id=section['paragraph_id'],
                 original_article_key=section.get('article_key') or section['paragraph_id'])
            pages.append(RawWikiPage(path=path,body=body,content_kind='source',source_id=source_id,
                 title=title,aliases=list(dict.fromkeys([title,source_name or source_id]))[:20],
                 tags=['article','source','original-structure'],scope=scope))
    if text.strip() and ''.join(p.body for p in pages) != text:raise ValueError('ORIGINAL_STRUCTURE_COVERAGE_GAP')
    return pages


def attach_article_catalog(pages, raw_pages):
    """Group only authoritative original article identities, as navigation.

    Fine facts retain their paths; source paragraphs retain their ranges. The
    article overview contains read links, never a synthetic merged fact body.
    """
    from jonex_wiki.domain.engine.models.compiler_state import PageIndexEntry,WikiPageSnapshot
    groups={}
    for raw in raw_pages:
        key=raw.scope.get('original_article_key')
        if key:groups.setdefault(key,[]).append(raw)
    for key,items in sorted(groups.items()):
        title=items[0].scope['original_article_title'];path='articles/'+hashlib.sha256(key.encode()).hexdigest()[:20]+'.md'
        targets=list(dict.fromkeys(p.path for p in items))
        body='Read the original paragraphs below; each retains its source, version and exact range.\n\n'
        body+='\n'.join('- [['+p.path+'|'+p.title+']] · '+p.source_id+' · '+str(p.scope['article_start'])+':'+str(p.scope['article_end']) for p in items)
        scope={'content_kind':'navigation','reading_role':'article_index','reading_targets':targets,
               'original_article_key':key,'source_ids':sorted({p.source_id for p in items})}
        entry=PageIndexEntry(path=path,title=title[:200],aliases=[title[:200]],tags=['article','navigation-only'],scope=scope)
        pages[path]=WikiPageSnapshot(index=entry,content=body,related_pages=targets,source_ids=scope['source_ids'])


def attach_reading_targets(pages, claims, raw_pages, archive, source_records, source_locations):
    """Keep fact/evidence IDs as indexes; resolve their exact original ranges.

    Locate each archived passage once in its own source, then use saved span
    offsets for all of its claims and fragments. Repeated/unlocatable passages
    retain their old exact read path; no guessed paragraph is substituted.
    """
    chapters=defaultdict(list)
    for raw in raw_pages:
        if raw.scope.get('reading_rule')==READING_RULE:
            chapters[raw.source_id].append(raw)
    texts, ends, offsets = {}, {}, {}
    for source_id, items in chapters.items():
        items.sort(key=lambda item: item.scope['article_start'])
        texts[source_id] = ''.join(item.body for item in items)
        ends[source_id] = [item.scope['article_end'] for item in items]
    for passage_id, passage in archive.items():
        text = texts.get(passage.source_id, '')
        start = text.find(passage.text) if passage.text else -1
        if start >= 0 and text.find(passage.text, start+1) < 0:
            offsets[passage_id] = start

    def locate(passage_id, source_id, start, end, text):
        passage = archive.get(passage_id)
        if (passage_id not in offsets or passage is None or passage.source_id != source_id
                or not isinstance(start, int) or not isinstance(end, int)
                or start < 0 or end <= start or end > len(passage.text)
                or passage.text[start:end] != text):
            return []
        begin, stop = offsets[passage_id]+start, offsets[passage_id]+end
        first = bisect_right(ends[source_id], begin)
        result = []
        for item in chapters[source_id][first:]:
            if item.scope['article_start'] >= stop:
                break
            result.append(item.path)
        return result

    by_page=defaultdict(list)
    for claim in claims:
        by_page[claim.page_path].append(claim)
    for path,snapshot in list(pages.items()):
        generated = snapshot.index.scope.get('content_kind') == 'generated'
        source_fragment = path in source_records and source_locations.get(path, {}).get('unit_kind') == 'text'
        if not generated and not source_fragment:
            continue
        owners=[sid for sid in snapshot.source_ids if sid in chapters]
        if not owners:continue
        targets=[]
        for claim in by_page.get(path,[]):
            for span in claim.source_spans:
                targets.extend(locate(span.passage_id, span.source_id,
                                      span.start_char, span.end_char, span.text))
        location = source_locations.get(path, {})
        for record in source_records.get(path, []):
            targets.extend(locate(record['passage_id'], record['source_id'],
                location.get('start_char'), location.get('end_char'), record['excerpt']))
        targets = list(dict.fromkeys(targets))
        entry=snapshot.index.model_copy(update={'scope':{**snapshot.index.scope,
            'reading_role':'fact_index' if generated else 'evidence_index','reading_targets':targets,
            'reading_sources':[f'source-articles/{hashlib.sha256(sid.encode()).hexdigest()[:16]}/index.md' for sid in owners]}})
        related=list(dict.fromkeys([*snapshot.related_pages,*targets]))
        pages[path]=snapshot.model_copy(update={'index':entry,'related_pages':related})
