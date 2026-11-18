"""Locate directed relationship candidates in source text, without name merges.

Names identify occurrences, not global people. A film's unnamed director with
the same spelling as a historical ruler therefore cannot become that ruler.
Only clauses with two literal names and an explicit predicate are admitted.
Pronouns, inferred relations, negation and unresolved spans remain unbound.
"""
from __future__ import annotations
import hashlib
import re

from .coherent_catalog import passage_offset

RULE = 'source-clause-relations-v7'
FILE = 'query/source_relations.json'
_GENERIC = {'he','she','it','they','his','her','their','the film','the band','the series','the city','the company','film','series','city','father','mother','daughter','son'}
_UNSUPPORTED = re.compile(r"\b(?:not|never|whether|may|might|could|would|will|shall|scheduled|planned|intended|expected|rumou?r(?:ed)?|alleged|reportedly|supposed|hypothetical|if|claimed|unsubstantiated|disputed|contested|denied|discredited)\b|[?？]|并非|不是|可能|假设|是否|未曾",re.I)
# Surface grammars, not dataset/entity dictionaries. Full source statements,
# dates, conditions and units are retained beside each admitted predicate.
_PREDICATES = (
 ('directed_by',r'\bdirected\s+by\s*["\s]*$'),
 ('written_by',r'\bwritten\s+by\s*["\s]*$'),
 ('composed_by',r'\b(?:composed\s+by|musical\s+score\s+by|music\s+by)\s*["\s]*$'),
 ('daughter_of',r'\b(?:is|was)\s+(?:a|the)\s+daughter\s+of\s+(?:(?:king|queen)\s+)?["\s]*$'),
 ('son_of',r'\b(?:is|was)\s+(?:(?:a|the|younger|elder)\s+){0,2}son\s+of\s+(?:(?:king|queen)\s+)?["\s]*$'),
 ('father_of',r'\b(?:is|was)\s+(?:a|the)\s+father\s+of\s*["\s]*$'),
 ('mother_of',r'\b(?:is|was)\s+(?:a|the)\s+mother\s+of\s*["\s]*$'),
 ('married',r'\bmarried\s*["\s]*$'),
 ('born_in',r'\b(?:was\s+)?born\s+in\s*["\s]*$'),
 ('died_in',r'\bdied\s+in\s*["\s]*$'),
 ('buried_in',r'\b(?:was\s+)?buried\s+in\s*["\s]*$'),
 ('headquartered_in',r'\b(?:headquartered|based)\s+in\s*["\s]*$'),
 ('located_in',r'\b(?:located|situated)\s+in\s*["\s]*$'),
 ('capital_of',r'\b(?:is|was|been)\s+(?:the\s+)?capital(?:\s+city)?\s+of\s*["\s]*$'),
 ('member_of',r'\b(?:is|was)\s+(?:a\s+)?member\s+of\s*["\s]*$'),
 ('part_of',r'\b(?:is|was)\s+(?:a\s+)?part\s+of\s*["\s]*$'),
 ('episode_of_series',r'\b(?:pilot\s+)?episode\s+of\s+[^.;\n]{0,90}\bseries\s*["\s]*$'),
)

def _get(value,key,default=None):
    return value.get(key,default) if isinstance(value,dict) else getattr(value,key,default)

def _key(*parts):
    return hashlib.sha256('\0'.join(map(str,parts)).encode()).hexdigest()[:24]

def literal_occurrences(statement,names,*,allow_generic=False):
    """Located names are navigable even when no predicate can be established."""
    names=sorted({str(n).strip() for n in names if 2<=len(str(n).strip())<=160 and (allow_generic or str(n).casefold().strip() not in _GENERIC)},key=lambda n:(-len(n),n))
    occurrences=[];occupied=[]
    for name in names:
        left=r'(?<!\w)' if name[0].isascii() and name[0].isalnum() else ''
        right=r'(?!\w)' if name[-1].isascii() and name[-1].isalnum() else ''
        for match in re.finditer(left+re.escape(name)+right,statement,re.I):
            a,b=match.span()
            if any(a<y and x<b for x,y in occupied):continue
            occurrences.append((a,b,statement[a:b]));occupied.append((a,b))
    occurrences.sort()
    return occurrences


def clause_relations(statement, names):
    """Return named occurrence offsets; never carry a previous clause's subject."""
    if len(statement)>4000 or _UNSUPPORTED.search(statement):return []
    occurrences=literal_occurrences(statement,names)
    if not occurrences:return []
    subject=occurrences[0]
    if statement[:subject[0]].strip(' \t\n\"“”‘’\''):return []
    result=[]
    for obj in occurrences[1:]:
        between=statement[subject[1]:obj[0]]
        if between.lstrip().startswith(("'s",'’s')):continue
        # Parenthetical life dates belong to the same subject, but another
        # sentence, semicolon or quoted assertion must not inherit it.
        syntax=re.sub(r'\([^)]*\)',' ',between)
        if re.search(r'[;\n]|\.(?:\s|$)',syntax):continue
        for predicate,pattern in _PREDICATES:
            match=re.search(pattern,between,re.I)
            prefix=between[:match.start()] if match else ''
            nested=bool(re.search(r'\b(?:of|by|about|based|according|who|whom|whose|which|that|said|says|reported|claims|claim|alleges|denies)\b',re.sub(r'\([^)]*\)',' ',prefix),re.I))
            if match and not nested and len(prefix)<240 and not any(
                    middle[0]>=subject[1] and middle[1]<=subject[1]+match.start()
                    for middle in occurrences[1:] if middle!=obj):
                result.append((subject,predicate,obj));break
    return result

def build_source_relations(contributions, archive, snapshots, page_index, *, article_paths=None):
    entities={};relations={};unbound=0;article_hashes={};passage_offsets={}
    readers={}
    for path,entry in page_index.items():
        scope=_get(entry,'scope',{}) or {}
        if scope.get('reading_role')=='article':
            for sid in scope.get('source_ids',[]) or [_get(entry,'source_id','')]:
                if sid:readers.setdefault(sid,[]).append((scope.get('article_start',0),scope.get('article_end',0),path))
    for record in _get(contributions,'records',[]):
        if _get(record,'support_status')!='SUPPORTED' or _get(record,'unresolved_evidence',[]):continue
        original=_get(record,'original_names',{})
        names=list(original)+list(original.values())
        origin_pages=[_get(d,'page_path') for d in _get(record,'displays',[]) if _get(d,'page_path') in page_index]
        for span in _get(record,'source_spans',[]):
            sid=_get(span,'source_id');text=_get(span,'text','');snapshot=snapshots.get(sid,{})
            article=_get(snapshot,'text','');passage=archive.get(_get(span,'passage_id'))
            if not article or not passage or _get(passage,'source_id',sid)!=sid:continue
            passage_text=_get(passage,'text','');passage_key=(sid,_get(span,'passage_id'))
            if passage_key not in passage_offsets:
                passage_offsets[passage_key]=passage_offset(article,passage_text,_get(passage,'source_metadata',{}) or {})
            base=passage_offsets[passage_key]
            a,b=_get(span,'start_char',0),_get(span,'end_char',0)
            if (base is None or type(a) is not int or type(b) is not int
                    or not 0<=a<b<=len(passage_text) or passage_text[a:b]!=text):
                unbound+=1;continue
            start=base+a;end=base+b
            if article[start:end]!=text:unbound+=1;continue
            if sid not in article_hashes:article_hashes[sid]=hashlib.sha256(article.encode()).hexdigest()
            reader=next((p for lo,hi,p in readers.get(sid,[]) if lo<=start<hi),origin_pages[0] if origin_pages else '')
            for lo,hi,label in literal_occurrences(text,names):
                oid='mention_'+_key(sid,article_hashes[sid],start+lo,start+hi)
                entities[oid]={'entity_id':oid,'label':label,'identity_status':'source_occurrence',
                    'source_id':sid,'document_id':_get(span,'document_id',sid),'start_char':start+lo,'end_char':start+hi,
                    'page_path':reader,'origin_page_paths':origin_pages}
            for subject,predicate,obj in clause_relations(text,names):
                ids=[]
                for occurrence in [subject,obj]:
                    lo,hi,label=occurrence
                    identity='mention_'+_key(sid,article_hashes[sid],start+lo,start+hi)
                    entities[identity]={'entity_id':identity,'label':label,'identity_status':'source_occurrence',
                        'source_id':sid,'document_id':_get(span,'document_id',sid),'start_char':start+lo,'end_char':start+hi,
                        'page_path':reader,'origin_page_paths':origin_pages}
                    ids.append(identity)
                rid='relation_'+_key(*ids,predicate,start,end)
                relations[rid]={'relation_id':rid,'source':ids[0],'target':ids[1],'predicate':predicate,
                    'support_status':'LOCATED_PROPOSAL','semantic_verification':'not_assessed',
                    'origin_kind':'named_clause_pattern','identity_status':'source_occurrences_not_global_identity',
                    'origin_page_paths':origin_pages,'source_id':sid,'document_id':_get(span,'document_id',sid),
                    'evidence':{'path':(article_paths or {}).get(sid,'sources/articles/'+sid+'.md'),'start_char':start,'end_char':end,'excerpt':text,
                                'body_sha256':article_hashes[sid]},
                    'qualifiers':{'source_statement':text,'identity_scope':'literal named occurrences only'}}
            # Arbitrary source-understanding predicates remain discoverable.
            # Exact excerpt/endpoints establish a located proposal, not its
            # semantic truth. Negation, conditions and forecasts stay attached.
            for proposal in _get(record,'link_proposals',[]) or []:
                predicate=proposal.get('relation');subject=proposal.get('subject_mention');target=proposal.get('target_mention')
                if not all(isinstance(v,str) and v.strip() for v in [predicate,subject,target]):continue
                refs=proposal.get('evidence_refs') or []
                if not any(_get(ref,'source_id')==sid and _get(ref,'passage_id')==_get(span,'passage_id')
                           and _get(ref,'excerpt')==text for ref in refs):continue
                endpoints=[]
                for label in [subject,target]:
                    # A proposal may point to "She" or "the company" as an
                    # unresolved occurrence. This retains its source context;
                    # it neither creates a trusted clause nor binds identity.
                    matches=literal_occurrences(text,[label],allow_generic=True)
                    if len(matches)!=1:break
                    lo,hi,raw=matches[0];oid='mention_'+_key(sid,article_hashes[sid],start+lo,start+hi)
                    entities[oid]={'entity_id':oid,'label':raw,'identity_status':'source_occurrence','source_id':sid,
                        'document_id':_get(span,'document_id',sid),'start_char':start+lo,'end_char':start+hi,
                        'page_path':reader,'origin_page_paths':origin_pages}
                    endpoints.append(oid)
                if len(endpoints)!=2 or endpoints[0]==endpoints[1]:continue
                rid='relation_'+_key(*endpoints,predicate,start,end,'source_proposal')
                relations[rid]={'relation_id':rid,'source':endpoints[0],'target':endpoints[1],'predicate':predicate,
                    'support_status':'LOCATED_PROPOSAL','semantic_verification':'not_assessed',
                    'identity_status':'source_occurrences_not_global_identity','record_id':_get(record,'record_id'),
                    'source_id':sid,'source_ids':sorted({_get(s,'source_id') for s in _get(record,'source_spans',[])}),
                    'document_id':_get(span,'document_id',sid),'origin_page_paths':origin_pages,
                    'evidence':{'path':(article_paths or {}).get(sid,'sources/articles/'+sid+'.md'),'start_char':start,'end_char':end,
                        'excerpt':text,'body_sha256':article_hashes[sid]},
                    'qualifiers':{'source_statement':text,'record_qualifiers':_get(record,'qualifiers',[]),
                        'support_alternatives':_get(record,'support_alternatives'),
                        'meaning':'Located source-understanding proposal; open its full record support and original context before asserting the relation.'}}
    catalog = {'schema_version':1,'rule':RULE,'entities':sorted(entities.values(),key=lambda x:x['entity_id']),
            'relations':sorted(relations.values(),key=lambda x:(x['source_id'],x['evidence']['start_char'],x['relation_id'])),
            'unbound_span_count':unbound,'identity_policy':'No global same-name merges; pronouns and unstated relations are not inferred.'}
    if any(s.get('source_metadata',{}).get('original_structure') for s in snapshots.values()):
        from .source_identity import bind_article_identities
        paths=article_paths or {sid:'sources/articles/'+sid+'.md' for sid in snapshots}
        return bind_article_identities(catalog,snapshots,page_index,paths)
    return catalog
