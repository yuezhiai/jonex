"""Validate numeric conclusions against delivered source values or named outputs.

Text markers discover claims; they never prove that a disclosed value requires
arithmetic. Computation identity, output field, units and rounding are checked
at consumption. No separate model/classification call is made here.
"""
from __future__ import annotations
import json
import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Literal
from pydantic import BaseModel, ConfigDict, Field
from .numeric_operands import parse_number, declared_unit, handle_claim_ids, _ground_text, resolve_operand, OperandRef, OperandError, scale_multiplier
from .numeric_units import unit_spec

CONSISTENCY_RULE_VERSION = 'numeric-consistency-v3'
DERIVED_MARKERS = re.compile(r'百分点|百分比|占比|比例|比率|增长率|同比|环比|合计|总计|共计|平均|均值|差额|差值|倍数|percentage|percent|ratio|average|mean|total|difference|%|％', re.I)
PERCENT_MARKERS = ('%', '％', '百分点', '个百分点', 'percentage', 'percent', 'ratio', '占比', '比例')
_NUMBER = re.compile(r'[-+−]?\d[\d,]*(?:\.\d+)?')
_BRACKET = re.compile(r'\[[^\]]*\]')
_HANDLE = re.compile(r'\[(E[1-9][0-9]*)\]|(table_(?:cell|note)_[0-9a-f]{16,})')
_UP = re.compile(r'上升|增加|增长|提高|rise|rose|increase|grew', re.I)
_DOWN = re.compile(r'下降|减少|降低|下跌|decline|decrease|fell|drop', re.I)
_JUDGMENT = re.compile(r'改善|恶化|improv|worsen|better|worse', re.I)
_QUALIFIED = re.compile(r'未(?:确认|证明|核实)|缺少|不确定|unknown|unverified|not (?:verified|established)|missing', re.I)

# ── locators: numbers that point at material instead of asserting a value ────
# An answer says "the 10-K on page 52 reports FY2018 revenue of 12,645": only
# the last number is a financial assertion; 10, 52 and 2018 locate it.  Treating
# every literal as a value made a correct answer look unsupported, so each
# literal is classified first and a locator never enters the financial check.
_MONTH_NAME = re.compile(
    r'(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|'
    r'aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?\s*$',
    re.I)
_REFERENCE_BEFORE = re.compile(
    r'(?:page|pages|p|pp|note|item|section|table|figure|exhibit|appendix|schedule|part|'
    r'form|asc|ifrs|ias|sfas|asu|fasb|topic|no)\.?\s*$', re.I)
_REFERENCE_AFTER = re.compile(
    r'\s*(?:页|頁|页码|页数|节|節|条|條|项|項|款|章|表|图|圖|注|号|號|部分|paragraph|clause)')
_PAGE_AFTER = re.compile(r'\s*(?:页|頁)')
_FORM_SUFFIX = re.compile(r'\s*-\s*[A-Z]{1,3}(?:\b|$)')
_CURRENCY_BEFORE = re.compile(r'(?:[$￥€£¥]|USD|RMB|CNY|HKD|EUR|美元|元|人民币|港元|欧元|日元)\s*$', re.I)
_SCALE_AFTER = re.compile(
    r'\s*(?:%|％|万|萬|千|百|亿|億|十亿|百万|千万|美元|元|港元|欧元|日元|倍|个|家|人|吨|股|'
    r'thousand|million|billion|dollars|percent|people|employees|shares|units|basis points)',
    re.I)
_ISO_DATE_AFTER = re.compile(r'\s*[-/]\s*\d{1,2}\s*[-/]\s*\d{1,4}')
_ISO_DATE_BEFORE = re.compile(r'\d{1,4}\s*[-/]\s*\d{1,2}\s*[-/]\s*$')
_DAY_COMMA_AFTER = re.compile(r'\s*,\s*\d{4}')
_DAY_COMMA_BEFORE = re.compile(r'\d{1,2}\s*,\s*$')
_CN_YEAR_BEFORE = re.compile(r'(?:19|20)\d{2}\s*年\s*$')
_CN_MONTH_BEFORE = re.compile(r'\d{1,2}\s*月\s*$')
_CN_MONTH_AFTER = re.compile(r'\s*月\s*\d{1,2}\s*日')
_FISCAL_AFTER = re.compile(r'\s*(?:年|年度|财年|財年|fiscal year|fy)', re.I)
_FISCAL_BEFORE = re.compile(r'(?:FY|Fiscal|fiscal year)\s*$', re.I)
# English temporal context around a four-digit literal.  "in 2018", "for FY2018"
# and "the 2018 fiscal year" type the literal as a period; "the balance is
# 2018" does not, and a number is only a locator once something says so.
_ENGLISH_PERIOD_BEFORE = re.compile(
    r'(?:\b(?:in|during|for|since|by|through|throughout|at|as of|as at|ends?|ended|'
    r'fiscal|fy|year|period|quarter|q[1-4])\s*)$', re.I)
_ENGLISH_PERIOD_AFTER = re.compile(
    r'\s*(?:fiscal year|fy|year ended|year|period|quarter|q[1-4]|ended)\b', re.I)


def locator_kind(literal: str, text: str, start: int, end: int) -> Optional[str]:
    """Classify a literal that locates material rather than asserting a value.

    Returns ``document_id``, ``reference_number``, ``date``, ``period`` or
    ``None`` when the literal is a value that must still be checked.  The
    classification is deliberately narrow and local: it only fires on explicit
    identifiers (``10-K``, ``Form 8-K``, ``ASC 606``), explicit page/section
    markers (``第 52 页``, ``page 52``, ``Note 5``), date components and bare
    calendar years.  A number carrying a unit, a scale or a currency stays a
    value even when it is a plausible year ("2022 百万美元" is money).  A bare
    four-digit literal in the year range is *not* a locator on its own: only an
    explicit date/period label (``2018 年``, ``FY2018``, ``in 2018``, a date
    form) types it as one. An industry metric name alone cannot establish that
    an adjacent four-digit number is a date rather than a measured value.
    """
    text = text or ""
    # The claim scanner keeps a trailing grouping comma in the literal
    # ("2," in "February 2, 2019"); it is punctuation, not part of the number.
    literal = (literal or "").rstrip(",，")
    before = text[max(0, start - 24):start]
    after = text[end:end + 24]
    if _FORM_SUFFIX.match(after) and re.fullmatch(r'\d{1,2}(?:\.\d+)?', literal or ""):
        return 'document_id'
    if _REFERENCE_BEFORE.search(before) and re.fullmatch(r'\d{1,4}(?:\.\d+)?', literal or ""):
        return 'reference_number'
    if before.endswith('第') or re.search(r'第\s*$', before):
        if _REFERENCE_AFTER.match(after):
            return 'reference_number'
    if _PAGE_AFTER.match(after):
        return 'reference_number'
    if _MONTH_NAME.search(before) and re.fullmatch(r'\d{1,2}(?:st|nd|rd|th)?', literal or "", re.I):
        return 'date'
    if _DAY_COMMA_BEFORE.search(before) and re.fullmatch(r'(?:19|20)\d{2}', literal or ""):
        return 'date'
    if _DAY_COMMA_AFTER.match(after) and re.fullmatch(r'\d{1,2}', literal or ""):
        return 'date'
    if _ISO_DATE_AFTER.match(after) or _ISO_DATE_BEFORE.search(before):
        return 'date'
    if _CN_YEAR_BEFORE.search(before) and re.fullmatch(r'\d{1,2}', literal or ""):
        return 'date'
    if _CN_MONTH_BEFORE.search(before) and re.fullmatch(r'\d{1,2}', literal or ""):
        return 'date'
    if _CN_MONTH_AFTER.match(after) and re.fullmatch(r'\d{1,2}', literal or ""):
        return 'date'
    if re.fullmatch(r'(?:19|20)\d{2}', literal or ""):
        if _SCALE_AFTER.match(after) or _CURRENCY_BEFORE.search(before):
            return None
        if (_FISCAL_AFTER.match(after) or _FISCAL_BEFORE.search(before)
                or _ENGLISH_PERIOD_AFTER.match(after)
                or _ENGLISH_PERIOD_BEFORE.search(before)):
            return 'period'
        # Being a four-digit number in the year range is a shape, not a period.
        # "The balance is 2018 [E99]." states a value and has to be checked;
        # calling it a period by shape is how an unsupported number slipped
        # through.  An explicit date/period context above is what makes it a
        # locator, and when the asked fact *is* a year or a date the value check
        # below (and the answer binding's stated-year conflict) still applies.
        return None
    return None


class ComputedClaimRef(BaseModel):
    """Bind one unique phrase in the answer to a tool output, not any scalar."""
    text: str = Field(min_length=1, description='Exact unique phrase from this answer, including the numeric conclusion.')
    computation_id: str = Field(pattern=r'^C[1-9][0-9]*$')
    field: Literal['result','absolute_change','percentage_points','relative_percent'] = 'result'
    step_id: Optional[str] = None
    model_config = ConfigDict(extra='forbid')

def _decimal(value):
    if value is None or isinstance(value, bool): return None
    try: return Decimal(str(value).replace(',','').replace('−','-'))
    except (InvalidOperation, ValueError): return None

def answer_handles(answer: str) -> List[str]:
    return list(dict.fromkeys('['+a+']' if a else b for a,b in _HANDLE.findall(answer or '')))

def _all_claims(answer: str) -> List[Dict[str, Any]]:
    # Keep offsets in the original answer, including across sentence boundaries.
    masked = _BRACKET.sub(lambda m:' '*len(m[0]), answer or '')
    result=[]
    for segment in re.finditer(r'[^。；;\n]+',masked):
        text=segment[0]
        original=(answer or '')[segment.start():segment.end()]
        for number in re.finditer(r'[（(]\s*\d[\d,]*(?:\.\d+)?\s*[）)]|[-+−]?\d[\d,]*(?:\.\d+)?', text):
            start,end=number.span()
            # Explicit years are context, not a computed scalar claim.
            if (re.fullmatch(r'(?:19|20)\d{2}',number[0])
                    and (text[end:end+1]=='年' or re.search(r'(?:FY|Fiscal)\s*$',text[:start],re.I))):
                continue
            suffix=text[end:]
            unit_match=re.match(r'\s*(个百分点|百分点|%|％|(?:[万千百亿]+)?(?:美元|港元|欧元|日元|元)|(?:million|thousand|billion) dollars\b|dollars\b|USD\b|天|days?\b|percent\b|家|个|米|m\b)',suffix,re.I)
            unit=unit_match[1] if unit_match else None
            if unit and unit.lower()=='percent': unit='%'
            if unit and unit.lower() in ('day','days'): unit='天'
            marks=[m for m in DERIVED_MARKERS.finditer(text,max(0,start-14),min(len(text),end+14))
                   if (m.end()<=start and not re.search(r'[，,、]',text[m.end():start]))
                   or (m.start()>=end and m[0] in ('%','％','百分点')
                       and not re.search(r'[，,、]',text[end:m.start()]))]
            marker=min(marks,key=lambda m:min(abs(m.end()-start),abs(m.start()-end)))[0].lower() if marks else ''
            # Use the same accounting-sign parser as delivered operands: (473)
            # is -473, not an unparseable Decimal that later enters arithmetic.
            result.append({'value':parse_number(number[0])[0],'literal':number[0], 'unit':unit,
                           'marker':marker,'text':original.strip(),'position':segment.start()+start,
                           'end':segment.start()+end, 'handles':answer_handles(original),
                           'locator':locator_kind(number[0], text, start, end)})
    return result

def numeric_claims(answer: str, *, limit: int=64) -> List[Dict[str, Any]]:
    """Legacy discovery helper; no cross-sentence offset deduplication."""
    return [c for c in _all_claims(answer) if c['marker']][:limit]

def _candidate(value, *, label, operation, payload, computation_id, step_id=None):
    number=_decimal(value)
    if number is None: return None
    return {'value':number,'label':label,'operation':operation,'unit':payload.get('unit'),
            'table_id':payload.get('table_id'),'citations':list(payload.get('citations') or payload.get('evidence_handles') or []),
            'unproven':list(payload.get('unproven') or payload.get('missing') or []),
            'display_decimals':payload.get('display_decimals'), 'computation_id':computation_id,
            'step_id':step_id,'direction':payload.get('direction'), 'operands':payload.get('operands') or []}

def program_values(computations: Iterable[Mapping[str,Any]]) -> List[Dict[str,Any]]:
    values=[]
    for index, comp in enumerate(computations or ()):
        payload=comp.get('payload') or {}
        if payload.get('status')!='complete': continue
        cid=payload.get('computation_id') or 'C'+str(index+1)
        steps=payload.get('steps') or []
        # Intermediate steps are addressable only by an explicit binding.
        records=[(None,{**(steps[-1] if steps else {}),**payload},'result')]
        records += [(step.get('step_id'),{**payload,**step,
                       'citations':step.get('evidence_handles') or payload.get('citations'),
                       'unproven':step.get('missing',[])},'display') for step in steps]
        for step_id,record,value_key in records:
            candidate=_candidate(record.get('exact_result', record.get('exact', record.get(value_key))), label='result',operation=record.get('operation'),
                                 payload=record,computation_id=cid,step_id=step_id)
            if candidate: values.append(candidate)
            comparison=record.get('comparison') or record
            for key in ('absolute_change','percentage_points','relative_percent'):
                if comparison.get(key) is None: continue
                units={'percentage_points':'百分点','relative_percent':'%'}
                detail={**record,**comparison,'unit':units.get(key,record.get('unit'))}
                if key in ('percentage_points','relative_percent'):detail['display_decimals']=2
                candidate=_candidate(comparison[key],label=key,operation='compare',payload=detail,
                                     computation_id=cid,step_id=step_id)
                if candidate:values.append(candidate)
    return values

def _matches_claim(claim,candidate):
    left,right=unit_spec(claim.get('unit')),unit_spec(candidate.get('unit'))
    if left is None or right is None:return False
    value=candidate['value']
    if left[0]==right[0]: value=value*right[1]/left[1]
    elif left[0]=='%' and not right[0]:value*=100
    elif not left[0] and right[0]=='%':return False
    else:return False
    wanted=claim['value']
    if (candidate.get('direction')=='下降' and _DOWN.search(claim['text'])
            and candidate['label'] in ('result','absolute_change','percentage_points','relative_percent')):
        wanted=-abs(wanted)
    # Check the exact result at the answer's precision, after unit conversion.
    # A rounded fraction is not the basis for checking a displayed percentage.
    places=max(0,-claim['value'].as_tuple().exponent)
    quantum=Decimal(1).scaleb(-int(places))
    with localcontext() as context:
        context.prec = max(28, len(value.as_tuple().digits) + abs(value.adjusted()) + int(places) + 2)
        return wanted == value.quantize(quantum, rounding=ROUND_HALF_UP)

def _claim_years(text: str) -> List[str]:
    return list(dict.fromkeys(re.findall(r'(?<!\d)(?:19|20)\d{2}(?!\d)', text or '')))


def _period_year(value: Any) -> Optional[str]:
    years = re.findall(r'(?<!\d)(?:19|20)\d{2}(?!\d)', str(value or ''))
    return years[0] if len(set(years)) == 1 else None


def _period_conflict(claim, operand) -> bool:
    """Whether a disclosed value sits in a period its own sentence contradicts.

    Only a directly disclosed value is checked (a derived one is the job of the
    comparison rules), only when the sentence names exactly one year and the
    cited cell proves exactly one year: anything less certain keeps the gap open
    instead of inventing either support or a conflict.
    """
    if claim.get('marker') or claim.get('locator'):
        return False
    claimed = _claim_years(claim.get('text'))
    stated = _period_year(getattr(operand, 'period', None))
    if len(claimed) != 1 or stated is None:
        return False
    return claimed[0] != stated


def _source_support(claim,state,external_handles=()):
    if state is None:return False
    delivered=handle_claim_ids(state.get('query_state'))
    for handle in claim['handles'] or external_handles:
        text=_ground_text(state,delivered,handle)
        if not text:continue
        provenance = (state.get('table_evidence_refs') or {}).get(delivered.get(handle)) or {}
        if provenance.get('kind') == 'table_cell':
            try:
                operand = resolve_operand(OperandRef(kind='table_cell',handle=handle),state)
            except OperandError:
                continue
            left,right=unit_spec(claim.get('unit')),unit_spec(operand.unit)
            if left is not None and right is not None and left[0]==right[0]:
                if claim['value']*left[1]==operand.value*right[1] and not _period_conflict(claim,operand):
                    return True
            continue  # do not lose a financial sign by scanning inside the cell
        for number in re.finditer(r'[（(]\s*\d[\d,]*(?:\.\d+)?\s*[）)]|[-+−]?\d[\d,]*(?:\.\d+)?', text):
            # Match a complete numeric literal; compare exact units, not answer precision.
            tail=re.match(r'\s*[%％\u3400-\u9fffA-Za-z]+',text[number.end():])
            raw=number[0]+(tail[0] if tail else '')
            value,_,_=parse_number(number[0])
            _,scale,_=parse_number(raw)
            if value is None:continue
            unit=declared_unit(raw) or ('%' if scale in ('%','％') else None)
            left,right=unit_spec(claim.get('unit')),unit_spec(unit)
            if left is not None and right is not None and left[0]==right[0] and claim['value']*left[1]==value*scale_multiplier(scale)*right[1]:return True
    return False

def _comparison_conflict(claim, candidates):
    for candidate in candidates:
        direction = candidate.get('direction')
        if not direction:
            continue
        if (_UP.search(claim['text']) and direction != '上升') or (_DOWN.search(claim['text']) and direction != '下降'):
            return 'comparison_direction_conflict'
        if _JUDGMENT.search(claim['text']) and not re.search(r'不能|无法|不足以|cannot|not established',claim['text'],re.I):
            return 'comparison_judgment_unproven'
        baseline = re.search(r'(?:相较于?|相对于?|比|compared (?:with|to)|relative to|versus)\s*((?:19|20)\d{2})',claim['text'],re.I)
        if baseline:
            operands = candidate.get('operands') or []
            period = str(operands[0].get('period') or '') if operands else ''
            if not period or baseline[1] not in period:
                return 'comparison_baseline_unproven_or_conflict'
    return None


def _failure_reasons(failures):
    return [{k:v for k,v in f.items() if k in ('reason','failed_step_id')} for f in failures or () if f.get('reason')]

def _conflict(reason,claim,candidates,failures):
    return {'reason':reason,'rule_version':CONSISTENCY_RULE_VERSION,'claim':claim['text'],
            'declared':str(claim['value']),'marker':claim.get('marker'),
            'program_results':[{k:(str(v) if isinstance(v,Decimal) else v) for k,v in c.items()
                                if k in ('value','label','operation','unit','citations','computation_id','step_id')} for c in candidates[:6]],
            'citations_to_use':list(dict.fromkeys(h for c in candidates for h in c['citations'])),
            'rejected_calculations':_failure_reasons(failures)}

def computed_answer_conflict(answer,computations,*,failures=(),state=None,bindings=(),citation_handles=()):
    candidates=program_values(computations)
    if not candidates:return None
    refs=[ref.model_dump() if isinstance(ref,ComputedClaimRef) else ref for ref in bindings]
    for ref in refs:
        if answer.count(ref['text'])!=1:
            return {'reason':'computed_claim_binding_invalid','rule_version':CONSISTENCY_RULE_VERSION,'claim':ref['text']}
    for claim in _all_claims(answer):
        if claim.get('locator') and not any(
                answer.index(ref['text'])<=claim['position']<answer.index(ref['text'])+len(ref['text'])
                for ref in refs):
            # A document number, a page reference, a date or a bare year locates
            # material; it is not a financial assertion and cannot conflict with
            # a source value.
            continue
        matches_ref=[ref for ref in refs if answer.index(ref['text'])<=claim['position']<answer.index(ref['text'])+len(ref['text'])]
        if not matches_ref and _source_support(claim,state,citation_handles):
            # Copying a current value cannot prove the direction of change.
            cited = set(claim['handles'] or citation_handles)
            related = [c for c in candidates if c.get('direction') and cited.intersection(
                operand.get('evidence_handles',[None])[0] for operand in c.get('operands',[])
                if operand.get('evidence_handles'))]
            reason = _comparison_conflict(claim, related)
            if reason:
                return _conflict(reason,claim,related,failures)
            continue
        selected=candidates
        if matches_ref:
            if len(matches_ref)!=1:return _conflict('ambiguous_result_binding',claim,[],failures)
            ref=matches_ref[0]
            selected=[c for c in candidates if c['computation_id']==ref['computation_id']
                      and c['step_id']==ref.get('step_id') and c['label']==ref.get('field','result')]
        else:
            selected=[c for c in selected if c['step_id'] is None]
        matched=[c for c in selected if _matches_claim(claim,c)]
        if not matched:return _conflict('not_a_program_result',claim,selected,failures)
        ids={c['computation_id'] for c in matched}
        if len(ids)>1 and not matches_ref:return _conflict('ambiguous_result_binding',claim,matched,failures)
        handles=set(claim['handles'] or citation_handles)
        matched=[c for c in matched if handles.intersection(c['citations'])]
        if not matched:return _conflict('uncited_program_result',claim,selected,failures)
        for c in matched:
            reason = _comparison_conflict(claim,[c])
            if reason:
                return _conflict(reason,claim,[c],failures)
            if state is not None and c['unproven'] and not _QUALIFIED.search(claim['text']):
                return _conflict('computation_semantics_unproven',claim,[c],failures)
    return None

def computed_claim_without_program(answer,computations,*,failures=(),state=None,citation_handles=()):
    if program_values(computations):return None
    claims=_all_claims(answer) if state is not None else numeric_claims(answer)
    # Locators (``10-K``, ``第 52 页``, dates, bare years) are not financial
    # assertions: they are reported in the trace by ``_all_claims`` but they
    # never make an answer unsupported.
    unresolved=[c for c in claims if not c.get('locator') and not _source_support(c,state,citation_handles)]
    if not unresolved:return None
    locators=[{'value':str(c['value']),'kind':c['locator'],'literal':c['literal']}
              for c in claims if c.get('locator')]
    return {'rule_version':CONSISTENCY_RULE_VERSION,
            'claims':[{'value':str(c['value']),'marker':c['marker'],'text':c['text']} for c in unresolved],
            'locators_skipped':locators[:16],
            'rejected_calculations':_failure_reasons(failures),
            'note':'这些数值尚无本次已交付的来源或计算结果支持。直接披露值请读取并引用原文；派生值请用 table_compute 的受支持操作与已读输入计算；缺少依据时说明缺口。'}

__all__=['CONSISTENCY_RULE_VERSION','ComputedClaimRef','DERIVED_MARKERS','PERCENT_MARKERS',
         'answer_handles','computed_answer_conflict','computed_claim_without_program',
         'locator_kind','numeric_claims','program_values']
