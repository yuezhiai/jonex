"""Observable cell-quality clues, separate from rectangle/header validity."""
import re

_AMOUNT = r"[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?"
_NUMBERS = re.compile(_AMOUNT)


def cell_quality_warnings(cells):
    warnings = []
    for cell in cells:
        value = str(cell.get("raw_value") or "").strip()
        # Require a currency delimiter and multiple distinct number tokens.
        # This is a review clue, never permission to split or realign cells.
        if (any(symbol in value for symbol in "$€£¥")
                and len(_NUMBERS.findall(value)) >= 2
                and not re.sub(r"[\s$€£¥]", "", _NUMBERS.sub("", value))):
            warnings.append({"code": "MULTIPLE_AMOUNTS_IN_CELL",
                "row": cell["row_index"], "column": cell["column_index"],
                "coordinate": cell.get("coordinate"), "value": value,
                "effect": "Check source layout before assigning amounts to fields or periods."})
    return warnings
