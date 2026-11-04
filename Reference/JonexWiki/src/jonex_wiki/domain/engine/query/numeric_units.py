"""Small, explicit unit algebra for evidence-bound calculations.

The written scale and the base unit are separate. Unknown units are preserved
as unknown, never interpreted by a permissive substring or a default factor.
"""
from decimal import Decimal
import re
from typing import Optional, Tuple

ALIASES = {
    '元': '元', '人民币': '元', 'CNY': '元', 'RMB': '元',
    '美元': '美元', 'USD': '美元', 'US dollars': '美元',
    '港元': '港元', 'HKD': '港元', '欧元': '欧元', 'EUR': '欧元',
    '日元': '日元', 'JPY': '日元', '英镑': '英镑', 'GBP': '英镑',
    '$': '$', 'dollars': '$', '¥': '¥', '€': '欧元', '£': '英镑',
    '%': '%', '％': '%', '百分点': '%', '个百分点': '%',
    '米': '米', 'm': '米', '天': '天', 'days': '天', 'day': '天',
    '家': '家', '个': '个', '股': '股', '人': '人', '吨': '吨',
}
CURRENCIES = frozenset(('元','美元','港元','欧元','日元','英镑','$','¥'))
SCALES = {'十亿': Decimal('1e9'), '亿': Decimal('1e8'), '千万': Decimal('1e7'),
          '百万': Decimal('1e6'), '十万': Decimal('1e5'), '万': Decimal('1e4'),
          '千': Decimal('1e3'), '百': Decimal('1e2'),
          'billion': Decimal('1e9'), 'million': Decimal('1e6'), 'thousand': Decimal('1e3')}

def unit_spec(text: Optional[str]) -> Optional[Tuple[str, Decimal]]:
    """Return (base identity, scale); empty means dimensionless, None unknown."""
    if not text:
        return ('', Decimal(1))
    value = str(text).strip()
    for alias, base in ALIASES.items():
        if value.casefold() == alias.casefold():
            return base, Decimal(1)
    for scale, factor in sorted(SCALES.items(), key=lambda kv:len(kv[0]), reverse=True):
        if value.casefold() == scale:
            return '', factor
        if value.casefold().startswith(scale):
            rest = value[len(scale):].strip()
            for alias, base in ALIASES.items():
                if rest.casefold()==alias.casefold():
                    return base, factor
    match = re.fullmatch(r'(dollars|USD|US dollars) in (millions|thousands|billions)',value,re.I)
    if match:
        return next(base for alias, base in ALIASES.items() if alias.casefold() == match[1].casefold()), SCALES[match[2].lower()[:-1]]
    return None

def inline_unit(text: str) -> Optional[str]:
    # Longer aliases win before generic 元; Latin names must be whole words.
    for alias in sorted(ALIASES, key=len, reverse=True):
        boundary = r'(?<![A-Za-z])' if alias[0].isascii() and alias[0].isalpha() else ''
        end = r'(?![A-Za-z])' if alias[-1].isascii() and alias[-1].isalpha() else ''
        if re.search(boundary+re.escape(alias)+end, text, re.I):
            return ALIASES[alias]
    return None

def unit_note(text: str, *, metric: Optional[str]=None) -> Optional[str]:
    match = re.search(r'单位\s*[:：]\s*([^\s，。；、）)]+)',text)
    if match:
        return match[1] if unit_spec(match[1]) is not None else None
    value = re.sub(r'\s*\(Notes?\s+[\d,\sand]+\)\s*$', '', text, flags=re.I).strip('()（） ')
    if re.search(r'\bexcept\b',value,re.I):
        parts = re.split(r',?\s*except\s+',value,flags=re.I)
        if len(parts)!=2 or not re.fullmatch(r'per share (data|amounts|figures)',parts[1],re.I) or not metric:
            return None
        if re.search(r'per[ -]share|每股',metric,re.I) or re.fullmatch(r'basic|diluted', metric.strip(), re.I):
            return None  # money/share is not implemented; do not apply millions.
        value=parts[0]
    mixed = re.fullmatch(r'dollars and shares in (millions|thousands|billions)', value, re.I)
    if mixed:
        # A metric-name whitelist cannot identify the owner of two dimensions.
        # Require a local declaration of a single unit instead.
        return None
    dollar = re.fullmatch(r'\$ in (millions|thousands|billions)', value, re.I)
    if dollar:
        value = 'dollars in ' + dollar[1].lower()
    return value if unit_spec(value) is not None else None

def number_range_valid(text: str, start: int, end: int) -> bool:
    """An explicit position may not split a signed number or its scale/unit."""
    if start<0 or end<=start or end>len(text):
        return False
    # Currency, decimal/grouping punctuation and signs are part of a numeric token.
    if start and text[start-1] in '0123456789+-−–(（':
        return False
    if start > 1 and text[start-1] in '.,' and text[start-2].isdigit():
        return False
    if end < len(text) and text[end] in '0123456789)）':
        return False
    if end + 1 < len(text) and text[end] in '.,' and text[end+1].isdigit():
        return False
    return True
