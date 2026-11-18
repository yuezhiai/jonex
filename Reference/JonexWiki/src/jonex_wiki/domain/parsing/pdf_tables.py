"""Conservative native-PDF table recovery from words and their physical boxes.

The saved parser observation is retained. Only a bounded, aligned region with
unambiguous word assignment becomes a replacement grid. No model invents cell
values, blank-to-zero conversion, or signs. Scans remain explicit gaps.
"""
from __future__ import annotations

import copy
import hashlib
import html
import json
import re
import shutil
import statistics
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

VERSION = 'pdf-native-grid-v1'
NUMBER = re.compile(r'^(?:[$€£¥]?\(?[+−-]?\d[\d,.]*%?\)?%?|[—–−-])$')
CURRENCY = {'$', '€', '£', '¥'}
NS = {'x': 'http://www.w3.org/1999/xhtml'}


def extract_page_words(original: Path, pages, *, max_pages=128):
    """Poppler bbox coordinates in PDF points, origin at top-left; no OCR."""
    executable = shutil.which('pdftotext')
    if not executable:
        raise ValueError('PDF_NATIVE_TOOL_UNAVAILABLE')
    numbers=sorted({p for p in pages if isinstance(p,int) and p>=1})
    if not numbers:return {}
    first=numbers[0];last=min(numbers[-1],first+max_pages-1)
    with tempfile.TemporaryDirectory(prefix='wiki-pdf-') as directory:
        target=Path(directory)/'pages.xml'
        proc=subprocess.run([executable,'-bbox-layout','-enc','UTF-8','-f',str(first),'-l',str(last),str(original),str(target)],capture_output=True,timeout=90)
        if proc.returncode or not target.is_file() or target.stat().st_size>64*1024*1024:
            raise ValueError('PDF_NATIVE_PAGE_EXTRACTION_FAILED')
        output={}
        for number,page in enumerate(ET.fromstring(target.read_bytes()).findall('.//x:page',NS),first):
            if number not in numbers:continue
            words=[{'id':i,'text':w.text or '', 'bbox':[float(w.get(k)) for k in ('xMin','yMin','xMax','yMax')]}
                   for i,w in enumerate(page.findall('.//x:word',NS))]
            output[number]={'page_number':number,'width':float(page.get('width')),'height':float(page.get('height')),'words':words}
        return output


def _lines(words):
    lines = []
    for word in sorted(words, key=lambda w: (w['bbox'][1], w['bbox'][0])):
        if lines and abs(statistics.median(w['bbox'][1] for w in lines[-1]) - word['bbox'][1]) <= 2.5:
            lines[-1].append(word)
        else:
            lines.append([word])
    return [sorted(line, key=lambda w: w['bbox'][0]) for line in lines]


def _segments(line):
    result = []
    for word in line:
        if result and word['bbox'][0] - result[-1][-1]['bbox'][2] < 7:
            result[-1].append(word)
        else:
            result.append([word])
    return result


def _text(words):
    return ' '.join(w['text'] for w in words)


def recover_region(page, bbox, *, bbox_space='normalized_1000'):
    """Return a source-backed projection or an explicit refusal.

    Numeric right edges establish column tracks. Repeated group headings may
    cover equal contiguous groups only when their centers agree with that
    geometry. Every body word is assigned exactly once. All source boxes and
    header composition remain in the receipt for inspection.
    """
    def reject(reason): return {'status': 'rejected', 'reason': reason, 'processor': VERSION}
    if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
        return reject('PDF_TABLE_BBOX_REQUIRED')
    if bbox_space != 'normalized_1000':
        return reject('PDF_TABLE_BBOX_SPACE_UNSUPPORTED')
    try:
        x0,y0,x1,y1 = [float(x) for x in bbox]
    except (TypeError, ValueError, OverflowError):
        return reject('PDF_TABLE_BBOX_INVALID')
    if not 0 <= x0 < x1 <= 1000 or not 0 <= y0 < y1 <= 1000:
        return reject('PDF_TABLE_BBOX_INVALID')
    crop = [x0*page['width']/1000,y0*page['height']/1000,x1*page['width']/1000,y1*page['height']/1000]
    words = [w for w in page['words'] if crop[0]-2 <= (w['bbox'][0]+w['bbox'][2])/2 <= crop[2]+2
             and crop[1]-2 <= (w['bbox'][1]+w['bbox'][3])/2 <= crop[3]+2]
    if not words or len(words) > 20000:
        return reject('PDF_NATIVE_REGION_EMPTY_OR_TOO_LARGE')
    lines = _lines(words)
    clusters = []
    body_height = statistics.median(w['bbox'][3]-w['bbox'][1] for w in words)
    aligned_ids = {w['id'] for line in lines if sum(bool(NUMBER.fullmatch(q['text'])) for q in line) >= 3 for w in line}
    for w in sorted([w for w in words if w['id'] in aligned_ids and w['bbox'][3]-w['bbox'][1] >= body_height*.85 and NUMBER.fullmatch(w['text'])], key=lambda w:w['bbox'][2]):
        if clusters and abs(statistics.mean(q['bbox'][2] for q in clusters[-1])-w['bbox'][2]) <= 5:
            clusters[-1].append(w)
        else:
            clusters.append([w])
    clusters = [c for c in clusters if len(c) >= 3]
    if not 2 <= len(clusters) <= 32:
        return reject('PDF_NATIVE_COLUMN_TRACKS_UNPROVEN')
    edges = [statistics.mean(w['bbox'][2] for w in c) for c in clusters]
    spacing = statistics.median(b-a for a,b in zip(edges,edges[1:]))
    if min(b-a for a,b in zip(edges,edges[1:])) < 14:
        return {**reject('PDF_NATIVE_COLUMNS_TOO_CLOSE'),'edges':edges}
    first_value = min(w['bbox'][0] for w in clusters[0])
    def slot(w):
        distances = [abs(w['bbox'][2]-x) for x in edges]
        i = min(range(len(edges)),key=lambda j: distances[j])
        return i if distances[i] <= 5 else None
    data_start = next((i for i,line in enumerate(lines) if sum(NUMBER.fullmatch(w['text']) is not None
                      and slot(w) is not None for w in line) >= max(2,(len(edges)+1)//2)
                      and any(w['bbox'][2] < first_value-5 for w in line)), None)
    if data_start is None or data_start == 0:
        return reject('PDF_NATIVE_HEADER_BOUNDARY_UNPROVEN')
    # A source label can be longer than the first value's minimum x, but it
    # cannot cross the left edge of the first numeric token in that same row.
    header_lines = lines[:data_start]
    labels = [[] for _ in edges]; label_words = [[] for _ in edges]; stub = []
    for header_number,line in enumerate(header_lines):
        segs = _segments(line)
        right = [s for s in segs if s[-1]['bbox'][2] >= first_value-12]
        left = [s for s in segs if s not in right]
        stub.extend(w for seg in left for w in seg)
        if not right:
            continue
        group = header_number == 0 and len(right) < len(edges) and len(edges) % len(right) == 0
        if group:
            count = len(edges)//len(right)
            # Equal group geometry is required; no wording/industry rule.
            group = all(abs((seg[0]['bbox'][0]+seg[-1]['bbox'][2])/2
                        - (statistics.mean(edges[i*count:(i+1)*count])-10)) <= spacing*.65
                        for i,seg in enumerate(right))
        for i,seg in enumerate(right):
            center = (seg[0]['bbox'][0]+seg[-1]['bbox'][2])/2
            targets = range(i*count,(i+1)*count) if group else [min(range(len(edges)),key=lambda j:abs((edges[j]-10)-center))]
            for col in targets:
                labels[col].append(_text(seg));label_words[col].extend(w['id'] for w in seg)
    headings = [' / '.join(parts) for parts in labels]
    if any(not x for x in headings) or len(set(headings)) != len(headings):
        return {**reject('PDF_NATIVE_HEADER_FIELDS_AMBIGUOUS'),'headings':headings,'data_start':data_start,'edges':edges}
    # Wrapped labels may straddle a centered numeric baseline. Coalesce only
    # adjacent label-only lines within one font height, retaining every word.
    body_lines=lines[data_start:]
    numeric_flags=[any(NUMBER.fullmatch(w['text']) and slot(w) is not None for w in line) for line in body_lines]
    consumed=set(); additions={}
    for index,line in enumerate(body_lines):
        if numeric_flags[index]:continue
        if any(w['bbox'][2] >= first_value+4 for w in line):continue
        nearby=[j for j in (index-1,index+1) if 0<=j<len(body_lines) and numeric_flags[j]
                and abs(statistics.median(w['bbox'][1] for w in line)-statistics.median(w['bbox'][1] for w in body_lines[j])) < body_height*.9]
        if len(nearby)==1:
            additions.setdefault(nearby[0],[]).extend(line);consumed.add(index)
    body_lines=[sorted(line+additions.get(index,[]),key=lambda w:(w['bbox'][1],w['bbox'][0]))
                for index,line in enumerate(body_lines) if index not in consumed]
    rows = [['row_label', *headings]]
    coordinates = []; body_word_ids = []; numeric_rows=0
    for line in body_lines:
        assigned = [[] for _ in range(len(edges)+1)]
        numeric = [w for w in line if NUMBER.fullmatch(w['text']) and slot(w) is not None]
        numeric_left = min((w['bbox'][0] for w in numeric),default=first_value)
        for w in line:
            col = slot(w) if NUMBER.fullmatch(w['text']) else None
            if col is not None:
                assigned[col+1].append(w)
            elif w['text'] in CURRENCY:
                following = [q for q in numeric if q['bbox'][0] >= w['bbox'][2] and q['bbox'][0]-w['bbox'][2] < spacing*.8]
                if not following:
                    return reject('PDF_NATIVE_CURRENCY_UNASSIGNED')
                assigned[slot(following[0])+1].append(w)
            elif not numeric or w['bbox'][2] < numeric_left:
                assigned[0].append(w)
            else:
                return {**reject('PDF_NATIVE_BODY_WORD_UNASSIGNED'), 'word':w,'line':_text(line),'data_start':data_start}
        if not assigned[0]:
            return {**reject('PDF_NATIVE_ROW_LABEL_UNPROVEN'),'line':_text(line),'data_start':data_start}
        if numeric:numeric_rows+=1
        rows.append([_text(sorted(cell,key=lambda w:(round(w['bbox'][1]/3),w['bbox'][0]))) for cell in assigned])
        for col,cell in enumerate(assigned,1):
            body_word_ids.extend(w['id'] for w in cell)
            coordinates.append({'row':len(rows),'column':col,'word_ids':[w['id'] for w in cell],
                'bbox': [min(w['bbox'][0] for w in cell),min(w['bbox'][1] for w in cell),
                         max(w['bbox'][2] for w in cell),max(w['bbox'][3] for w in cell)] if cell else None})
    if numeric_rows < 3 or len(body_word_ids) != len(set(body_word_ids)):
        return reject('PDF_NATIVE_BODY_COVERAGE_UNPROVEN')
    expected = {w['id'] for line in lines[data_start:] for w in line}
    if expected != set(body_word_ids):
        return reject('PDF_NATIVE_BODY_COVERAGE_MISMATCH')
    digest = hashlib.sha256(json.dumps({'rows':rows,'coordinates':coordinates,'header_word_ids':label_words},
        ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    return {'status':'accepted','processor':VERSION,'rows':rows,'cell_locations':coordinates,
        'header_stub_text':_text(stub),'header_word_ids':label_words,'header_projection':'composed_native_word_paths',
        'page_number':page['page_number'],'bbox_points':crop,'coordinate_space':'pdf_points_top_left',
        'body_word_coverage':'exact_once','body_word_count':len(expected),'projection_sha256':digest,
        'row_roles':{str(i+1):('layout_text_only' if i and not any(row[1:]) else 'values_present') for i,row in enumerate(rows)},
        'source_fidelity':'native_words_verified;layout_inferred','column_right_edges':edges,
        'words':words}


def supplement_pdf(items, original: Path, output_dir: Path, *, max_pages=128):
    """Copy observations, recovering only explicit page/bbox regions.

    Original parser input and full decisions are saved independently. Callers
    publish a new generation/Bundle; shared parser results are never changed.
    """
    candidates = [(i,item) for i,item in enumerate(items) if item.get('type') in {'table','complex_table'}
                  and isinstance(item.get('page_idx'),int) and item.get('bbox')]
    report={'schema':'wiki.pdf_table_repair.v1','processor':VERSION,
        'original_sha256':hashlib.sha256(original.read_bytes()).hexdigest(),'decisions':[],
        'page_budget':max_pages,'not_ocr':True}
    output=copy.deepcopy(items)
    try:
        pages=extract_page_words(original,[item['page_idx']+1 for _,item in candidates],max_pages=max_pages)
    except (ValueError,OSError,subprocess.SubprocessError,ET.ParseError) as exc:
        report['error']=type(exc).__name__
        pages={}
    for index,item in candidates:
        page=pages.get(item['page_idx']+1)
        repaired=recover_region(page,item['bbox']) if page else {'status':'rejected','reason':'PDF_NATIVE_PAGE_UNAVAILABLE'}
        decision={'item_index':index,**repaired}
        report['decisions'].append(decision)
        if repaired['status']!='accepted':continue
        markup='<table>'+''.join('<tr>'+''.join('<td>'+html.escape(value)+'</td>' for value in row)+'</tr>' for row in repaired['rows'])+'</table>'
        out=output[index]
        out['table_body']=markup
        out['processor']=VERSION
        out['pdf_native_repair']={k:v for k,v in repaired.items() if k not in {'words','rows','cell_locations'}}
        out['pdf_native_repair']['original_observation']=item
        out['pdf_native_repair']['cell_locations']=repaired['cell_locations']
    output_dir.mkdir(parents=True,exist_ok=True)
    (output_dir/'pdf-table-repair.json').write_text(json.dumps(report,ensure_ascii=False),'utf-8')
    return output,report
