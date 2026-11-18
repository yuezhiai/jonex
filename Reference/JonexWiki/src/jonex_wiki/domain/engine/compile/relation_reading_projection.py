"""Ordinary-file navigation for source claims and located relationships.

These are reading aids, not synthesized prose or verified cross-source identities.
"""
from __future__ import annotations

from collections import defaultdict
import hashlib
import json
import unicodedata
import zlib

RULE = 'agent-claim-discovery-v4-unbound-leads'


def _key(value):
    return ' '.join(unicodedata.normalize('NFKC', str(value or '')).casefold().split()).strip(' "“”')


def _label(value):
    return ' '.join(str(value or '').split()).replace('[', '\\[').replace(']', '\\]')


def _name(value):
    return hashlib.sha256(value.encode()).hexdigest()[:20]+'.md'


def _discovery_leads(record):
    """Expose compact, unconfirmed leads while keeping the full record intact."""
    names = []
    seen_names = set()

    def add_name(value):
        if not isinstance(value, str):
            return
        value = ' '.join(value.split())
        key = _key(value)
        if key and key not in seen_names:
            seen_names.add(key)
            names.append(value)

    original = record.get('original_names') or {}
    if isinstance(original, dict):
        for value in original.values():
            add_name(value)
    for value in record.get('entity_mentions') or []:
        add_name(value)

    relations = []
    seen_relations = set()
    excerpt = str(record.get('proposition') or '').casefold()
    for proposal in record.get('link_proposals') or []:
        if not isinstance(proposal, dict):
            continue
        target = proposal.get('target_mention')
        predicate = proposal.get('relation')
        subject = proposal.get('subject_mention')
        target = ' '.join(target.split()) if isinstance(target, str) else ''
        predicate = ' '.join(predicate.split()) if isinstance(predicate, str) else ''
        subject = ' '.join(subject.split()) if isinstance(subject, str) else ''
        if not target:
            continue
        if not predicate:
            add_name(target)
            continue
        key = (_key(subject), _key(predicate), _key(target))
        if key in seen_relations:
            continue
        seen_relations.add(key)
        lead = {'subject':subject, 'predicate':predicate, 'target':target,
                'scope':'excerpt' if subject and subject.casefold() in excerpt
                and target.casefold() in excerpt else 'outside_excerpt'}
        why = proposal.get('why_visit')
        if isinstance(why, str):
            why = ' '.join(why.split())
            if why and _key(why) not in {_key(predicate), 'source states this relation'}:
                lead['why_visit'] = why
        relations.append(lead)

    result = {}
    if names:
        result['name_leads'] = names
    if relations:
        result['relation_leads'] = relations
    unbound = [value for value in record.get('unbound') or []
               if isinstance(value, str) and value.strip()]
    if unbound:
        result['unbound'] = unbound
    return result


def reading_files(db):
    """Yield source-only files, retaining full claim records in bounded JSONL shards."""
    available={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type IN ('table','view')")}
    if not {'objects','units','records','record_locations','record_units','catalog_relations'} <= available:
        return
    sources={sid:(title,path) for sid,title,path in db.execute("SELECT id,title,path FROM objects WHERE kind='source'")}
    originals=set()
    for payload, in db.execute(
            "SELECT payload FROM objects WHERE kind='entity' AND json_extract(payload,'$.identity_status')='original_article_identity'"):
        original=json.loads(payload)
        for sid in original.get('source_ids') or [original.get('source_id')]:
            if sid:
                originals.add((sid,original['article_key']))
    units={}; candidates=defaultdict(list)
    for uid,sid,article_key,title,path in db.execute("SELECT u.id,u.source_id,u.article_key,o.title,o.path FROM units u "
            "JOIN objects o ON o.kind='unit' AND o.id=u.id ORDER BY u.source_id,u.start_char"):
        owner='article:'+sid+':'+article_key if (sid,article_key) in originals else 'unit:'+uid
        units[uid]=(owner,sid,title,path)
        if title and path and '未归属原文' not in title:
            candidates[_key(title)].append((owner,title,path))
    relations=defaultdict(dict);located_relations=set();located_proposals=defaultdict(set)
    sql="""SELECT r.id,r.source_id,r.predicate,r.payload,s.title,t.title,u.id
             FROM catalog_relations r
             JOIN objects s ON s.kind='entity' AND s.id=r.source
             JOIN objects t ON t.kind='entity' AND t.id=r.target
             JOIN units u ON u.source_id=r.source_id
               AND u.start_char<=json_extract(r.payload,'$.evidence.start_char')
               AND u.end_char>=json_extract(r.payload,'$.evidence.end_char')
             ORDER BY r.source_id,r.id,u.start_char"""
    for rid,sid,predicate,payload,subject,target,uid in db.execute(sql):
        if uid not in units:continue
        owner,_,title,path=units[uid]
        details=json.loads(payload)
        relations[owner].setdefault(rid,(sid,title,path,subject,predicate,target,details))
        located_proposals[owner].add((str(details.get('record_id') or ''),
                                      _key(subject),_key(predicate),_key(target)))
        located_relations.add(rid)
    unresolved=[]
    for rid,sid,predicate,payload,subject,target in db.execute(
            "SELECT r.id,r.source_id,r.predicate,r.payload,s.title,t.title FROM catalog_relations r "
            "LEFT JOIN objects s ON s.kind='entity' AND s.id=r.source "
            "LEFT JOIN objects t ON t.kind='entity' AND t.id=r.target ORDER BY r.id"):
        if rid in located_relations:continue
        unresolved.append(json.dumps({'relation_id':rid,'source_id':sid,'subject':subject,'predicate':predicate,
                                     'target':target,'source_record':json.loads(payload)},
                                    ensure_ascii=False,separators=(',',':')))
    if unresolved:
        yield 'claims/unlocated-relations.jsonl', ('\n'.join(unresolved)+'\n').encode()
    facts=defaultdict(dict); unbound_leads=defaultdict(dict)
    for uid,rid,payload in db.execute('SELECT ru.unit,r.id,r.payload FROM record_units ru '
                                       'JOIN records r ON r.id=ru.record ORDER BY ru.unit,r.ordinal'):
        if uid not in units:continue
        owner,sid,title,path=units[uid]
        record=json.loads(zlib.decompress(payload))
        facts[owner].setdefault(rid,(sid,title,path,{'topic_hint':record.get('topic_hint'),
                                                    'proposition':record.get('proposition')}))
        for lead in _discovery_leads(record).get('relation_leads',[]):
            key=(str(rid),_key(lead['subject']),_key(lead['predicate']),_key(lead['target']))
            if key not in located_proposals[owner]:
                unbound_leads[owner].setdefault(key,(path,lead))
    owners=set(relations)|set(facts)
    index=['# 来源关系阅读目录','','这些关系来自来源记录；同名文章只是候选，需结合原文判断身份。','']
    if unresolved:
        index+=['尚未定位到原文段落的关系见 [待定位关系](../../claims/unlocated-relations.jsonl)，可继续查找来源。','']
    for owner in sorted(owners):
        items=relations[owner]
        first=next(iter(items.values()),None) or next(iter(facts[owner].values()))
        sid,title,path,*_=first
        display=title or (sources.get(sid) or (sid,''))[0]
        filename=_name(owner);index.append('- ['+_label(display)+']('+filename+')')
        body=['# '+_label(display),'','本页列出来源中的关系线索，需阅读原文判断指代、时间和身份。','']
        if sid in sources and sources[sid][1]:body+=['[查看来源全文](../../'+sources[sid][1]+')','']
        if facts[owner]:
            body+=['## 来源事实与原文','']
            for rid,(_,fact_title,fact_path,record) in facts[owner].items():
                proposition=_label(record.get('proposition'))
                body+=['- '+('**'+_label(record.get('topic_hint'))+'**：' if record.get('topic_hint') else '')+
                       proposition+'；[原文](../../'+fact_path+')；`'+rid+'`。']
            body.append('')
        if unbound_leads[owner]:
            body+=['## 待核实的其他关系候选', '',
                   '以下线索未能唯一绑定关系端点；可能需要上下文。请读原文核对，不能当作已确认关系。', '']
            for (record_id,_,_,_),(lead_path,lead) in unbound_leads[owner].items():
                body+=['- '+_label(lead['subject'])+' — '+_label(lead['predicate'])+' → '+
                       _label(lead['target'])+'；'+('需补读上下文' if lead['scope']=='outside_excerpt' else '摘录内候选')+
                       '；[原文](../../'+lead_path+')；来源事实 `'+record_id+'`。']
            body.append('')
        if items:
            body+=['## 来源关系线索','','关系和同名候选都需要回到原文核对；摘录定位不等于语义或身份已确认。','']
        for rid,(sid,title,path,subject,predicate,target,payload) in items.items():
            evidence=payload.get('evidence') or {};record=payload.get('record_id')
            if payload.get('support_status') == 'LOCATED_PROPOSAL' or payload.get('semantic_verification') == 'not_assessed':
                status='原文定位的模型关系提议；尚未核实语义。'
            elif payload.get('support_status') == 'SUPPORTED':
                status='来源语句的程序匹配；仍需核对关系方向、条件和身份。'
            else:
                status='待核实的关系线索。'
            body+=['### '+_label(subject)+' — '+_label(predicate)+' → '+_label(target),'',status,'']
            # The source fact above already includes the same excerpt. Avoid
            # sending it twice while keeping standalone relationship evidence.
            if not record or record not in facts[owner]:
                body+=['> '+_label(evidence.get('excerpt')),'']
            body+=['[相关原文](../../'+path+')；关系记录 `'+rid+'`'+
                   ('；来源事实 `'+str(record)+'`，可在 `claims/index.jsonl` 查找完整记录入口。' if record else '。'),'']
            seen=set()
            for target_owner,candidate_title,candidate_path in candidates.get(_key(target),[]):
                if target_owner==owner or (target_owner,candidate_path) in seen:continue
                seen.add((target_owner,candidate_path))
                link=_name(target_owner) if target_owner in owners else '../../'+candidate_path
                body+=['相关候选：['+_label(candidate_title)+']('+link+')；同名不等于同一对象。','']
        yield 'articles/linked/'+filename, ('\n'.join(body)+'\n').encode()
    yield 'articles/linked/index.md', ('\n'.join(index)+'\n').encode()

    locations=defaultdict(list)
    sql="""SELECT l.record,l.source_id,l.start_char,l.end_char,l.status,s.path,u.path
             FROM record_locations l
             LEFT JOIN objects s ON s.kind='source' AND s.id=l.source_id
             LEFT JOIN record_units ru ON ru.record=l.record AND ru.span_no=l.span_no
             LEFT JOIN objects u ON u.kind='unit' AND u.id=ru.unit
             ORDER BY l.record,l.span_no,u.path"""
    for rid,sid,start,end,status,source_path,unit_path in db.execute(sql):
        entry={'source_id':sid,'source_path':source_path,'unit_path':unit_path,
               'start_char':start,'end_char':end,'status':status}
        if entry not in locations[rid]:locations[rid].append(entry)
    summary=[];shard=[];shard_no=0
    for rid,payload in db.execute('SELECT id,payload FROM records ORDER BY ordinal'):
        record=json.loads(zlib.decompress(payload));name='claims/records-'+str(shard_no).zfill(5)+'.jsonl'
        shard.append(json.dumps(record,ensure_ascii=False,separators=(',',':')))
        summary.append(json.dumps({'record_id':rid,'topic':record.get('topic_hint'),
            'proposition':record.get('proposition'),'qualifiers':record.get('qualifiers'),
            'support_status':record.get('support_status'),'source_locations':locations.get(rid,[]),
            'full_record_file':name,**_discovery_leads(record)},ensure_ascii=False,separators=(',',':')))
        if len(shard)==2000:
            yield name, ('\n'.join(shard)+'\n').encode();shard=[];shard_no+=1
    if shard:yield 'claims/records-'+str(shard_no).zfill(5)+'.jsonl', ('\n'.join(shard)+'\n').encode()
    yield 'claims/index.jsonl', ('\n'.join(summary)+'\n').encode()
