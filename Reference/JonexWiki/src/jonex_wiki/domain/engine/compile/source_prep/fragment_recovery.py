"""Recover complete source paragraphs across fragments using source-only metadata.

Original title/text metadata proposes identity; every published word must be
covered by unchanged source ranges. Partial coverage, incompatible titles,
known-section overlaps, and non-overlapping guesses are not published.
"""
from __future__ import annotations

from collections import defaultdict
import hashlib
import re
import unicodedata


def unicode_tokens(text):
    chars, offsets = [], []
    for at, char in enumerate(text):
        for value in unicodedata.normalize('NFKD', char).casefold():
            if not unicodedata.combining(value):
                chars.append(value)
                offsets.append(at)
    normalized = ''.join(chars)
    return [(m.group(), offsets[m.start()], offsets[m.end()-1]+1)
            for m in re.finditer(r'[\u3400-\u9fff]|[^\W_\u3400-\u9fff]+', normalized)]


def render_recovered(sources, recovered):
    pieces = []
    for part in recovered['parts']:
        if part['source_id'] not in sources or not 0 <= part['start'] < part['end'] <= len(sources[part['source_id']]):
            raise ValueError('RECOVERED_SOURCE_RANGE_INVALID')
        text = sources[part['source_id']][part['start']:part['end']]
        if hashlib.sha256(text.encode()).hexdigest() != part['sha256']:
            raise ValueError('RECOVERED_SOURCE_RANGE_CHANGED')
        pieces.append(text)
    text = ' '.join(pieces)
    if hashlib.sha256(text.encode()).hexdigest() != recovered['text_sha256']:
        raise ValueError('RECOVERED_PARAGRAPH_CHANGED')
    return text


def validate_recovered_manifest(sources, structures, recovered):
    """Check metadata before it can reserve or relabel source ranges."""
    required={'title','paragraph_id','article_key','authority','parts','text_sha256','token_count','normalization'}
    part_keys={'source_id','start','end','sha256','original_token_start','original_token_end','matched_start','matched_end'}
    spans=defaultdict(list);ids=set()
    for item in recovered:
        if set(item)!=required or item['paragraph_id'] in ids or not 2<=len(item['parts'])<=32:
            raise ValueError('RECOVERY_MANIFEST_FIELDS_OR_ID_INVALID')
        ids.add(item['paragraph_id'])
        expected=hashlib.sha256((item['authority']+'\0'+item['title']).encode()).hexdigest()
        if not item['authority'] or item['article_key']!=expected:
            raise ValueError('RECOVERY_MANIFEST_ARTICLE_ID_INVALID')
        position=0
        for part in item['parts']:
            if set(part)!=part_keys or part['source_id'] not in sources:
                raise ValueError('RECOVERY_MANIFEST_PART_INVALID')
            sid=part['source_id'];a,b=part['matched_start'],part['matched_end']
            if not 0<=a<=part['start']<part['end']<=len(sources[sid]) or not a<b<=len(sources[sid]) or b>part['end']:
                raise ValueError('RECOVERY_MANIFEST_RESERVED_RANGE_INVALID')
            if part['original_token_start']!=position or part['original_token_end']<=position:
                raise ValueError('RECOVERY_MANIFEST_TOKEN_GAP')
            position=part['original_token_end']
            structure=structures.get(sid,{})
            if structure.get('authority') not in (None,item['authority']):
                raise ValueError('RECOVERY_MANIFEST_AUTHORITY_MISMATCH')
            if any(a<s['end'] and b>s['start'] and s.get('paragraph_id')!=item['paragraph_id'] for s in structure.get('sections',[])):
                raise ValueError('RECOVERY_MANIFEST_KNOWN_SECTION_COLLISION')
            for left,right,key in spans[sid]:
                if a<right and b>left and key!=item['article_key']:
                    raise ValueError('RECOVERY_MANIFEST_ARTICLE_COLLISION')
            spans[sid].append((a,b,item['article_key']))
        text=render_recovered(sources,item)
        if position!=item['token_count'] or len(unicode_tokens(text))!=position:
            raise ValueError('RECOVERY_MANIFEST_TOKEN_COUNT')


def recover_paragraphs(sources, structures, paragraphs, *, authority, anchor_words=12, overlap_words=8):
    """Return only fully supported composite paragraphs, never missing text.

    ``paragraphs`` accepts exactly title/text. It is a source catalog, not QA
    records. Multiple fragments must overlap in original token coordinates.
    No source ordering is inferred from filenames or question associations.
    """
    if not authority or anchor_words < 8 or not 1 <= overlap_words <= anchor_words:
        raise ValueError('RECOVERY_AUTHORITY_OR_ANCHOR_INVALID')
    for sid, structure in structures.items():
        if sid not in sources or structure.get('article_sha256') != hashlib.sha256(sources[sid].encode()).hexdigest():
            raise ValueError('RECOVERY_STRUCTURE_HASH_MISMATCH')
    already = {s['paragraph_id'] for d in structures.values() for s in d.get('sections',[]) if s.get('paragraph_id')}
    catalog, titles_for, seen = {}, defaultdict(set), set()
    for p in paragraphs:
        if set(p) != {'title','text'}:
            raise ValueError('RECOVERY_ACCEPTS_SOURCE_TITLE_TEXT_ONLY')
        title, body = str(p['title']).strip(), str(p['text'])
        values = tuple(t[0] for t in unicode_tokens(body))
        if not title or len(values) < anchor_words:
            continue
        titles_for[values].add(title)
        pid = hashlib.sha256((title+'\0'+body).encode()).hexdigest()
        if pid in already or (title,values) in seen:
            continue
        seen.add((title,values))
        catalog[pid] = {'title':title,'words':values,'paragraph_id':pid,
            'article_key':hashlib.sha256((authority+'\0'+title).encode()).hexdigest()}
    ambiguous = {p for p,d in catalog.items() if len(titles_for[d['words']])>1}
    catalog = {p:d for p,d in catalog.items() if p not in ambiguous}
    anchors = defaultdict(list)
    for pid, item in catalog.items():
        values = item['words']
        positions = sorted(set([0,len(values)-anchor_words,*range(0,len(values)-anchor_words+1,32)]))
        for at in positions:anchors[values[at:at+anchor_words]].append((pid,at))
    matched, source_tokens = defaultdict(dict), {}
    for sid, text in sorted(sources.items()):
        tokens = unicode_tokens(text);source_tokens[sid]=tokens;values=[t[0] for t in tokens]
        sections = structures.get(sid,{}).get('sections',[])
        for at in range(max(0,len(tokens)-anchor_words+1)):
            for pid, origin in anchors.get(tuple(values[at:at+anchor_words]),[]):
                expected = catalog[pid]['words'];left,right,ol,orr=at,at+anchor_words,origin,origin+anchor_words
                while left>0 and ol>0 and values[left-1]==expected[ol-1]:left-=1;ol-=1
                while right<len(values) and orr<len(expected) and values[right]==expected[orr]:right+=1;orr+=1
                a,b=tokens[left][1],tokens[right-1][2]
                if any(a<s['end'] and b>s['start'] and s.get('paragraph_id')!=pid for s in sections):
                    continue
                key=(sid,left,right,ol,orr)
                matched[pid][key]={'source_id':sid,'token_start':left,'token_end':right,'original_start':ol,'original_end':orr}
    recovered=[];incomplete=0
    for pid, candidates in matched.items():
        item=catalog[pid];count=len(item['words']);position=0;chosen=[]
        while position<count:
            eligible=[c for c in candidates.values() if c['original_end']>position and
                      (c['original_start']==0 if position==0 else c['original_start']<=position-overlap_words)]
            if not eligible:break
            part=min(eligible,key=lambda c:(-c['original_end'],c['source_id'],c['token_start']))
            sid=part['source_id'];tokens=source_tokens[sid];start_token=part['token_start']+position-part['original_start']
            a=tokens[start_token][1];b=tokens[part['token_end']-1][2]
            while b<len(sources[sid]) and sources[sid][b] in '.,;:!?。！？\"”’':b+=1
            body=sources[sid][a:b]
            chosen.append({'source_id':sid,'start':a,'end':b,'sha256':hashlib.sha256(body.encode()).hexdigest(),
                           'original_token_start':position,'original_token_end':part['original_end'],
                           'matched_start':tokens[part['token_start']][1],
                           'matched_end':tokens[part['token_end']-1][2]})
            position=part['original_end']
        if position!=count:
            incomplete+=1;continue
        if len(chosen)<2:
            continue
        text=' '.join(sources[p['source_id']][p['start']:p['end']] for p in chosen)
        if tuple(t[0] for t in unicode_tokens(text))!=item['words']:
            incomplete+=1;continue
        recovered.append({k:v for k,v in item.items() if k!='words'} | {
            'authority':authority,'parts':chosen,'text_sha256':hashlib.sha256(text.encode()).hexdigest(),
            'token_count':count,'normalization':'Overlap deduplicated; inter-fragment whitespace joined; source bytes unchanged.'})
    # Two complete reconstructions with incompatible article identities must
    # not both claim the same previously-unassigned source region.
    ranges=defaultdict(list);rejected=set()
    for at,item in enumerate(recovered):
        for part in item['parts']:
            ranges[part['source_id']].append((part['matched_start'],part['matched_end'],at,item['article_key']))
    for spans in ranges.values():
        spans.sort()
        for i,(a,b,left,key) in enumerate(spans):
            for c,d,right,other in spans[i+1:]:
                if c>=b:break
                if key!=other:rejected.update((left,right))
    recovered=sorted((p for i,p in enumerate(recovered) if i not in rejected),key=lambda p:p['paragraph_id'])
    return recovered, {'source_catalog_records':len(paragraphs),'already_assigned_paragraphs':len(already),
        'recovered_paragraphs':len(recovered),'recovered_source_ranges':sum(len(p['parts']) for p in recovered),
        'ambiguous_catalog_identities_rejected':len(ambiguous),'incomplete_candidates_unpublished':incomplete,
        'ambiguous_recovery_overlaps_unpublished':len(rejected),
        'question_or_answer_fields_accepted':False}
