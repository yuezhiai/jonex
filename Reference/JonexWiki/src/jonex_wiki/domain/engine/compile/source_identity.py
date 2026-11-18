"""Source-scoped article identities backed by aligned original paragraphs.

An original article owns its named subject within its source. A matching name
and role in a different source is a discovery lead, not an identity proof.
Legacy rules remain readable so old published Bundles can be verified.
"""
from __future__ import annotations
import hashlib
import re
import unicodedata
from collections import defaultdict

RULE = 'original-article-role-identity-v4-source-scoped'
_ROLES = {
    'director': r'\b(?:film\s+|television\s+)?director\b',
    'writer': r'\b(?:writer|screenwriter|novelist|author)\b',
    'composer': r'\bcomposer\b',
    'series': r'\b(?:television|TV|web|animated)\s+(?:\w+\s+){0,3}series\b',
    'ruler': r'\b(?:queen|king|emperor|empress|monarch)\b',
    'film': r'\bfilm\b',
}
_EXPECTED = {'directed_by':'director', 'written_by':'writer',
             'composed_by':'composer', 'episode_of_series':'series'}

def _fold(value):
    return ' '.join(unicodedata.normalize('NFKC', value).casefold().split()).strip(' \"“”')

def _aliases(title):
    return {_fold(title), _fold(re.sub(r'\s*\([^()]*\)\s*$', '', title))}

def _ref(sid, body, section, article_paths, end=None):
    a=section['start']; b=min(section['end'], end) if end is not None else section['end']
    return {'source_id':sid, 'path':article_paths[sid], 'start_char':a, 'end_char':b,
            'excerpt':body[a:b], 'body_sha256':hashlib.sha256(body.encode()).hexdigest(),
            'authority':section['authority'], 'article_key':section['article_key'],
            'original_title':section['title']}

def bind_article_identities(catalog, snapshots, page_index, article_paths, *, rule=RULE):
    """Derive additive identity metadata; inputs contain source material only."""
    if rule not in {RULE,'original-article-role-identity-v3','original-article-role-identity-v2','original-article-role-identity-v1'}:raise ValueError('SOURCE_IDENTITY_RULE_UNSUPPORTED')
    legacy=rule=='original-article-role-identity-v1'
    scoped=rule in {RULE,'original-article-role-identity-v3'}
    source_scoped=rule==RULE
    catalog={k:v for k,v in catalog.items() if k not in {
        'identity_rule','article_entities','identity_bindings','identity_ambiguities_unbound'}}
    groups=defaultdict(list); by_source=defaultdict(list)
    readers=defaultdict(list)
    for path, entry in page_index.items():
        scope=(entry.get('scope',{}) if isinstance(entry,dict) else entry.scope) or {}
        if scope.get('reading_role')=='article':
            for sid in scope.get('source_ids',[]):
                readers[sid].append((scope.get('article_start',0),scope.get('article_end',0),path))
    for sid,snapshot in sorted(snapshots.items()):
        body=snapshot.get('text',''); metadata=snapshot.get('source_metadata',{})
        structure=metadata.get('original_structure')
        if not structure:continue
        digest=hashlib.sha256(body.encode()).hexdigest()
        if structure.get('article_sha256')!=digest:raise ValueError('IDENTITY_SOURCE_MISMATCH')
        authority=structure.get('authority','')
        if not authority:raise ValueError('IDENTITY_AUTHORITY_REQUIRED')
        previous_end=0
        for raw in sorted(structure.get('sections',[]),key=lambda s:s['start']):
            a,b=raw['start'],raw['end']; title=raw['title']; key=raw['article_key']
            expected=hashlib.sha256((authority+'\0'+title).encode()).hexdigest()
            if key!=expected or not 0<=previous_end<=a<b<=len(body):
                raise ValueError('IDENTITY_SECTION_INVALID')
            previous_end=b
            section={**raw,'authority':authority}; text=body[a:min(b,a+900)]
            roles=set(); births=set()
            # Lead subjects must match the original title's literal name.
            lead=re.match(r'\s*[\"“]?(.{2,180}?)\s+(?:is|was)\s+(.+)',text,re.I|re.S)
            if lead:
                name=re.sub(r'\s*\([^)]*\)\s*',' ',lead[1]).strip(' \"“”')
                if _fold(name) in _aliases(title):
                    definition=re.split(r'[.;\n]',lead[2],maxsplit=1)[0]
                    definition=re.split(r'\b(?:who|which|that|of|about|by|according)\b',definition,maxsplit=1,flags=re.I)[0]
                    roles={role for role,pattern in _ROLES.items() if re.search(pattern,definition,re.I)}
                    life=re.search(r'\(\s*(?:born\s+)?(?:\d{1,2}\s+\w+\s+)?(\d{4})\b',lead[1],re.I)
                    if life:births.add(life[1])
            item={'sid':sid,'body':body,'section':section,'roles':roles,'births':births}
            group_key=(authority,key,sid) if source_scoped else (authority,key)
            groups[group_key].append(item);by_source[sid].append(item)
    if not groups:return catalog
    entities={}; compatible=defaultdict(list); rejected=set()
    for key,items in sorted(groups.items()):
        roles=set().union(*(i['roles'] for i in items)); births=set().union(*(i['births'] for i in items))
        if len(births)>1 or ('ruler' in roles and roles & {'director','writer','composer','film','series'}):
            rejected.add(key);continue
        title=items[0]['section']['title']; eid='article_'+hashlib.sha256(('\0'.join(key)).encode()).hexdigest()[:24]
        anchor=next((i for i in items if i['roles']),items[0]); s=anchor['section'];sid=anchor['sid']
        reader=next((p for lo,hi,p in readers[sid] if lo<=s['start']<hi),article_paths[sid])
        proof=_ref(sid,anchor['body'],s,article_paths,min(s['end'],s['start']+900))
        entities[key]={'entity_id':eid,'label':title,'identity_status':'original_article_identity',
                       'authority':key[0],'article_key':key[1],'roles':sorted(roles),
                       'source_id':sid,'document_id':snapshots[sid].get('document_id',sid),
                       'page_path':reader,'evidence':proof}
        if scoped:
            alternatives=[]
            for item in items:
                section=item['section'];source=item['sid']
                reference=_ref(source,item['body'],section,article_paths,min(section['end'],section['start']+900))
                path=next((p for lo,hi,p in readers[source] if lo<=section['start']<hi),article_paths[source])
                alternatives.append({'evidence':reference,'roles':sorted(item['roles']),'page_path':path})
            entities[key]['source_ids']=sorted({item['sid'] for item in items})
            entities[key]['evidence_alternatives']=alternatives
        for alias in _aliases(title):compatible[alias].append(key)
    occurrences={i['entity_id']:i for i in catalog['entities']}; bindings={}
    def bind(oid,key,method,proof,alternatives=None):
        if key not in entities:return
        prior=bindings.get(oid)
        value={'occurrence_id':oid,'entity_id':entities[key]['entity_id'],'method':method,'evidence':proof}
        if scoped:
            origin=occurrences[oid]['source_id']
            value['source_groups']=[sorted({origin,p['source_id']}) for p in (alternatives or [proof])]
        if prior and prior['entity_id']!=value['entity_id']:
            bindings.pop(oid,None);conflicts.add(oid)
        elif oid not in conflicts:bindings[oid]=value
    conflicts=set()
    owners=([occurrences[r['source']] for r in catalog['relations']] if legacy else occurrences.values())
    for owner in owners:
        sid=owner['source_id']
        for item in by_source.get(sid,[]):
            s=item['section'];key=((s['authority'],s['article_key'],sid) if source_scoped
                                  else (s['authority'],s['article_key']))
            if s['start']<=owner['start_char']<owner['end_char']<=s['end'] and _fold(owner['label']) in _aliases(s['title']):
                bind(owner['entity_id'],key,'named_subject_in_original_article',_ref(sid,item['body'],s,article_paths,min(s['end'],s['start']+900)))
    if not source_scoped:
        # Reproduce historical Bundles exactly; new compilations leave these
        # cross-source matches as reading candidates, not identity bindings.
        for relation in catalog['relations']:
            role=_EXPECTED.get(relation['predicate'])
            if not role:continue
            obj=occurrences[relation['target']]
            candidates=[k for k in compatible.get(_fold(obj['label']),[]) if role in entities[k]['roles']]
            if len(candidates)==1:
                key=candidates[0]
                proofs=[a['evidence'] for a in entities[key].get('evidence_alternatives',[]) if role in a['roles']]
                bind(obj['entity_id'],key,'exact_name_and_explicit_'+role,proofs[0] if proofs else entities[key]['evidence'],proofs or None)
    used={v['entity_id'] for v in bindings.values()}
    visible=[v for v in entities.values() if not legacy or v['entity_id'] in used]
    return {**catalog,'identity_rule':rule,'article_entities':sorted(visible,key=lambda v:v['entity_id']),
            'identity_bindings':sorted(bindings.values(),key=lambda v:v['occurrence_id']),
            'identity_ambiguities_unbound':len(rejected)+len(conflicts),
            'identity_policy':('Article-owned subjects are source-scoped; cross-source names and roles remain unbound candidates.'
                               if source_scoped else 'Article-owned subjects; cross-article objects require exact name and a unique explicit compatible lead role. Other occurrences remain unresolved.')}
