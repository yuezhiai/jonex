"""Compile explicit source heading references into bounded reading paths.

The source text is the only input. References are navigational candidates, not
semantic assertions; ambiguous numbers, missing headings and references whose
source scope is uncertain remain unresolved.
"""
from __future__ import annotations

from bisect import bisect_right
from copy import deepcopy
import hashlib
import re

RULE='source-heading-reference-v2'
_HEADING=re.compile(r'(?m)^(?P<marks>#{1,6})[ \t]+(?P<title>[^\n]{1,240}?)\s*$')
_LABEL=re.compile(r'(?i)^(?:(?P<kind>note|item|section|chapter|appendix|part)\s+)?'
                  r'(?P<number>\d+(?:\.\d+)*)(?:[.、:：\-]|\s|$)\s*(?P<name>.*)$')
_LABEL_CN=re.compile(r'^第\s*(?P<number>\d+(?:\.\d+)*)\s*(?P<kind>章|节|条|部分)\s*[.、:：\-]?\s*(?P<name>.*)$')
_REFERENCE=re.compile(r'(?i)\b(?:see|refer(?:red)?\s+to|included\s+in|contained\s+in|'
                      r'described\s+in|set\s+out\s+in|pursuant\s+to)\s+'
                      r'(?:the\s+)?(?P<kind>note|item|section|chapter|appendix|part)\s+'
                      r'(?P<number>\d+(?:\.\d+)*)\b')
_REFERENCE_CN=re.compile(r'(?:详见|参见|请参阅|参照)\s*(?:本(?:文|文档|文件)\s*)?第\s*'
                         r'(?P<number>\d+(?:\.\d+)*)\s*(?P<kind>章|节|条|部分)')
_CN_KIND={'章':'chapter','节':'section','条':'section','部分':'part'}


def _mentions(text):
    values=[(m,m.group('kind').casefold(),m.group('number')) for m in _REFERENCE.finditer(text)]
    values += [(m,_CN_KIND[m.group('kind')],m.group('number')) for m in _REFERENCE_CN.finditer(text)]
    return sorted(values,key=lambda item:item[0].start())


def _reference_context(text, match):
    """Keep the surrounding source clause without crossing a sentence boundary."""
    suffix=text[match.end():match.end()+350]
    boundary=re.search(r'(?:[.!?。！？](?=\s|$)|\n\s*\n)',suffix)
    end=match.end()+(boundary.start()+1 if boundary else len(suffix))
    return text[match.start():end],end


def _reference_scope(context, match_text):
    """Return local only when a competing source is not indicated."""
    suffix=context[len(match_text):]
    if re.match(r'(?i)^\s+(?:of|in|to)\s+(?:this\b|the\s+(?:current|present)\b)',suffix):
        return 'local_candidate'
    if re.search(r'(?i)\b(?:another|other|prior|previous|preceding)\s+(?:\w+\s+){0,3}'
                 r'(?:report|agreement|statement|document|filing)\b',suffix):
        return 'external_reference_unresolved'
    current=re.search(r'(?i)\b(?:of|in|to|under)\s+this\s+(?:\d{4}\s+)?'
                      r'(?:(?:annual|quarterly|current)\s+)?(?:report|form|10\s*[-–]?\s*[kq])\b',suffix)
    specific=re.search(r'(?i)\b\d{4}\b[^.!?。！？]{0,100}\b'
                       r'(?:annual\s+report|quarterly\s+report|form\s+10\s*[-–]?\s*[kq])\b',suffix)
    if current and (not specific or current.start()<=specific.start()<current.end()):
        return 'local_candidate'
    if (specific or re.search(r'(?i)\b(?:annual\s+report|quarterly\s+report|'
                              r'form\s+10\s*[-–]?\s*[kq])\b',suffix)
        or re.match(r'(?i)^\s+(?:of|in)\s+',suffix)):
        return 'source_scope_unresolved'
    return 'local_candidate'


def heading_references(text, *, source_id, source_path):
    """Retain explicit source leads; resolve only unique, plausible local headings."""
    if not isinstance(text,str):raise ValueError('SOURCE_TEXT_REQUIRED')
    headings=[];stack=[]
    for match in _HEADING.finditer(text):
        level=len(match.group('marks'));title=match.group('title').strip();label=_LABEL.match(title) or _LABEL_CN.match(title)
        while stack and headings[stack[-1]]['level']>=level:
            headings[stack.pop()]['end']=match.start()
        heading={'level':level,'start':match.start(),'end':len(text),'title':title,
                 'kind':_CN_KIND.get(label.group('kind'),(label.group('kind') or '').casefold()) if label else '',
                 'number':label.group('number') if label else '',
                 'name':label.group('name').strip() if label else ''}
        headings.append(heading);stack.append(len(headings)-1)
    by_number={}
    for heading in headings:
        if heading['number']:by_number.setdefault(heading['number'],[]).append(heading)
    starts=[h['start'] for h in headings]
    sha=hashlib.sha256(text.encode()).hexdigest();out=[]
    for match,kind,number in _mentions(text):
        before=text[max(0,match.start()-45):match.start()]
        if re.search(r'(?i)\b(?:not|never|without|cannot|do\s+not|does\s+not)\s*$',before):
            continue
        context_quote,context_end=_reference_context(text,match)
        scope=_reference_scope(context_quote,match.group())
        options=[h for h in by_number.get(number,[]) if not h['start']<=match.start()<h['end']]
        # Explicitly named heading kinds are compatible; a bare numbered
        # heading needs its full descriptive name in the nearby reference.
        context=text[max(0,match.start()-80):min(len(text),match.end()+240)].casefold()
        candidates=[h for h in options if h['kind']==kind or
                    (not h['kind'] and len(h['name'])>=8 and h['name'].casefold() in context)]
        if scope!='local_candidate' or len(candidates)!=1:
            ident=hashlib.sha256((source_id+'\0'+str(match.start())+'\0unresolved').encode()).hexdigest()[:24]
            out.append({'reference_id':'heading_ref_'+ident,'source_id':source_id,'source_path':source_path,
                'source_sha256':sha,'origin_chars':[match.start(),match.end()],'origin_quote':match.group(),
                'context_chars':[match.start(),context_end],'context_quote':context_quote,
                'target_chars':None,'target_title':None,'target_children':[],'children_truncated':0,
                'following_heading_candidates':[],'following_truncated':0,
                'status':scope if scope!='local_candidate' else 'heading_target_unresolved'})
            continue
        target=candidates[0]
        after=headings[bisect_right(starts,target['start']):]
        children=[]
        for heading in after:
            if heading['start']>=target['end']:break
            if heading['level']>target['level']:children.append(heading)
        # Some parsed documents flatten subsection headings to the same
        # Markdown level. Keep nearby unnumbered headings as candidates only;
        # do not assert they are children or extend the proven target range.
        following=[]
        for heading in after:
            if heading['start']-target['start']>64000:break
            if heading['level']<=target['level'] and heading['number']:break
            if heading['start']>=target['end']:following.append(heading)
        ident=hashlib.sha256((source_id+'\0'+str(match.start())+'\0'+str(target['start'])).encode()).hexdigest()[:24]
        out.append({'reference_id':'heading_ref_'+ident,'source_id':source_id,'source_path':source_path,
            'source_sha256':sha,'origin_chars':[match.start(),match.end()],'origin_quote':match.group(),
            'context_chars':[match.start(),context_end],'context_quote':context_quote,
            'target_chars':[target['start'],target['end']],'target_title':target['title'],
            'target_children':[{'title':h['title'],'chars':[h['start'],h['end']]} for h in children[:8]],
            'children_truncated':max(0,len(children)-8),
            'following_heading_candidates':[{'title':h['title'],'chars':[h['start'],h['end']]} for h in following[:8]],
            'following_truncated':max(0,len(following)-8),
            'status':'same_source_heading_candidate'})
    return out


def references_for_block(body, *, source_id, source_path, source_text, cache, source_range=None):
    if not _REFERENCE.search(body) and not _REFERENCE_CN.search(body):return []
    if source_id not in cache:
        cache[source_id]=heading_references(source_text,source_id=source_id,source_path=source_path)
    matches=[ref for ref in cache[source_id] if ref['origin_quote'].casefold() in body.casefold()]
    if source_range is not None:
        left,right=source_range
        return [ref for ref in matches if left<=ref['origin_chars'][0]<ref['origin_chars'][1]<=right]
    # A generated paragraph cannot choose between repeated source occurrences.
    return [ref for ref in matches if sum(other['origin_quote'].casefold()==ref['origin_quote'].casefold()
                                            for other in matches)==1]


def render_reference_navigation(refs, prefix):
    """A short human-readable projection; the full list remains in the catalog."""
    links=[]
    for ref in refs[:4]:
        target=ref.get('target_chars')
        if target:
            title=str(ref['target_title']).replace('[','\\[').replace(']','\\]').replace('\n',' ')
            start,end=target
        else:
            title=('外部来源引用，待定位' if ref.get('status')=='external_reference_unresolved' else
                   '引用来源范围待核对' if ref.get('status')=='source_scope_unresolved' else '章节引用，目标待定位')
            start,end=ref['origin_chars']
        links.append('['+title+']('+prefix+ref['source_path']+') 字符 '+str(start)+'–'+str(end))
    line='来源章节引用线索（需核对）：'+'；'.join(links)
    if len(refs)>4:line+='；另有 '+str(len(refs)-4)+' 条，可通过 references 查看。'
    return line


def attach_heading_references(articles, snapshots, source_paths):
    """Attach only source-matched references to article blocks, without QA data."""
    cache={};output=[]
    for article in articles:
        changes=[]
        for si,section in enumerate(article.get('sections') or []):
            for bi,block in enumerate(section.get('blocks') or []):
                body=block.get('text') or '';matched=[]
                if not _REFERENCE.search(body) and not _REFERENCE_CN.search(body):continue
                for sid in dict.fromkeys(block.get('source_ids') or []):
                    if sid not in snapshots or sid not in source_paths:continue
                    spans=[s for s in block.get('source_ranges') or [] if s.get('source_id')==sid]
                    source_range=None
                    if spans:
                        if len(spans)!=1:continue
                        left,right=spans[0].get('start_char'),spans[0].get('end_char')
                        if isinstance(left,int) and isinstance(right,int) and snapshots[sid]['text'][left:right]==body:
                            source_range=(left,right)
                        else:continue
                    matched.extend(references_for_block(body,source_id=sid,source_path=source_paths[sid],
                        source_text=snapshots[sid]['text'],cache=cache,source_range=source_range))
                if matched:changes.append((si,bi,matched))
        if changes:
            article=deepcopy(article)
            for si,bi,matched in changes:
                article['sections'][si]['blocks'][bi]['source_references']=matched
        output.append(article)
    return output
