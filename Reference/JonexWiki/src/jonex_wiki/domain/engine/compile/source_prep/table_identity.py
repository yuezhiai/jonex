"""Human table identity comes from a caption or source/sheet/range, not a field."""
from pathlib import Path


def range_label(bounds):
    def column(n):
        s=''
        while n:n,r=divmod(n-1,26);s=chr(65+r)+s
        return s
    r1,c1,r2,c2=bounds
    return f'{column(c1)}{r1}:{column(c2)}{r2}'


def spreadsheet_title(source_name, sheet_name, bounds, *, caption=None):
    if caption and str(caption).strip():
        return str(caption).strip()
    parts=[Path(source_name).stem if source_name else '',sheet_name,range_label(bounds)]
    return ' · '.join(str(value) for value in parts if value)
