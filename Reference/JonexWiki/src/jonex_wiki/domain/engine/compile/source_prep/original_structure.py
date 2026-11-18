"""Align supplied original paragraph metadata to unchanged imported text.

Input is title/text only, never QA records, answers or supporting-fact labels.
Case, accents and punctuation spacing are normalized solely for alignment;
all published ranges refer to the unchanged original source characters.
Partial, overlapping or differently titled matches stay unassigned.
"""
from __future__ import annotations
import hashlib
import re
import unicodedata


def tokens_with_ranges(text):
    characters=[];offsets=[]
    for i,c in enumerate(text):
        for n in unicodedata.normalize('NFKD',c).casefold():
            if unicodedata.combining(n):continue
            characters.append(n);offsets.append(i)
    normalized=''.join(characters);tokens=[]
    for match in re.finditer(r'[a-z0-9]+|[\u3400-\u9fff]',normalized):
        tokens.append((match.group(),offsets[match.start()],offsets[match.end()-1]+1))
    return tokens


def align_original_structure(sources, paragraphs, *, authority):
    if not authority:raise ValueError('ORIGINAL_STRUCTURE_AUTHORITY_REQUIRED')
    anchors={};unique={}
    for paragraph in paragraphs:
        if set(paragraph)-{'title','text'}:raise ValueError('ONLY_ORIGINAL_TITLE_AND_TEXT_ALLOWED')
        title=str(paragraph['title']).strip();text=str(paragraph['text'])
        if not title:continue
        words=tuple(t[0] for t in tokens_with_ranges(text))
        if len(words)<6:continue
        key=(title,words)
        if key in unique:continue
        item={'title':title,'tokens':words,'paragraph_id':hashlib.sha256((title+'\0'+text).encode()).hexdigest(),
              'article_key':hashlib.sha256((authority+'\0'+title).encode()).hexdigest()}
        unique[key]=item;anchors.setdefault(words[:6],[]).append(item)
    result={};matched=0;ambiguous=0
    for sid,text in sources.items():
        tokens=tokens_with_ranges(text);words=[x[0] for x in tokens];candidates={}
        for i in range(max(0,len(words)-5)):
            for paragraph in anchors.get(tuple(words[i:i+6]),[]):
                n=len(paragraph['tokens'])
                if tuple(words[i:i+n])!=paragraph['tokens']:continue
                a,b=tokens[i][1],tokens[i+n-1][2]
                # Keep punctuation immediately belonging to this paragraph,
                # without absorbing another article's substantive text.
                while b<len(text) and text[b] in '.!?。！？\"”’':b+=1
                key=(a,b,paragraph['title'])
                candidates[key]={'start':a,'end':b,**{k:v for k,v in paragraph.items() if k!='tokens'}}
        ordered=sorted(candidates.values(),key=lambda x:(x['start'],-x['end'],x['title']))
        rejected=set()
        for i,left in enumerate(ordered):
            for j in range(i+1,len(ordered)):
                right=ordered[j]
                if right['start']>=left['end']:break
                # Full text with incompatible source titles is not sufficient
                # to choose an article. Even a longer overlap cannot guess.
                if left['title']!=right['title']:
                    rejected.update((i,j))
                elif left['end']>=right['end']:rejected.add(j)
                else:rejected.update((i,j))
        sections=[v for i,v in enumerate(ordered) if i not in rejected]
        ambiguous+=len(rejected);matched+=len(sections)
        result[sid]={'article_sha256':hashlib.sha256(text.encode()).hexdigest(),
                     'authority':authority,'sections':sections}
    return result,{'paragraphs_provided':len(paragraphs),'unique_alignable_paragraphs':len(unique),
                  'full_source_matches':matched,'overlap_or_title_ambiguities_unassigned':ambiguous,
                  'source_count':len(sources),'question_or_answer_fields_accepted':False}
