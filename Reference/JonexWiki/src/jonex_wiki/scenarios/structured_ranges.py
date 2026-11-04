"""Keep source offsets while excluding explicit table grids from relation extraction."""

import re
from html.parser import HTMLParser

SEPARATOR = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*$")


def prose_ranges(body):
    lines = body.splitlines(keepends=True)
    offsets, position = [], 0
    for line in lines:
        offsets.append(position)
        position += len(line)
    excluded = []
    for index, line in enumerate(lines):
        if index and SEPARATOR.fullmatch(line.rstrip()) and "|" in lines[index - 1]:
            end = index + 1
            while end < len(lines) and lines[end].strip() and "|" in lines[end]:
                end += 1
            excluded.append(
                (offsets[index - 1], offsets[end] if end < len(lines) else len(body))
            )

    class Tables(HTMLParser):
        depth = 0
        start = None

        def absolute(self):
            row, column = self.getpos()
            return offsets[row - 1] + column

        def handle_starttag(self, tag, attrs):
            if tag == "table":
                if not self.depth:
                    self.start = self.absolute()
                self.depth += 1

        def handle_endtag(self, tag):
            if tag == "table" and self.depth:
                self.depth -= 1
                if not self.depth:
                    excluded.append((self.start, self.absolute() + len("</table>")))

    parser = Tables(convert_charrefs=False)
    parser.feed(body)
    if parser.depth:
        excluded.append((parser.start, len(body)))
    merged = []
    for start, end in sorted(excluded):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    included, cursor = [], 0
    for start, end in merged:
        if body[cursor:start].strip():
            included.append((cursor, start))
        cursor = end
    if body[cursor:].strip():
        included.append((cursor, len(body)))
    return included, merged
