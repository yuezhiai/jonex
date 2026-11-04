"""Small, public previews of actual retrieval work; no model reasoning or full payloads."""
from itertools import islice


def tool_progress_details(args, *, pages=(), views=()):
    detail = {}
    for key in ("query", "table_id", "operation", "literal", "field_id"):
        value = args.get(key)
        if isinstance(value, str) and value:
            detail[key] = value[:300]
    # table_read 的定位信息藏在 scope / raw_scope 里，顶层没有 table_id/range。
    scope = args.get("scope") or args.get("raw_scope")
    if isinstance(scope, dict):
        table_id = scope.get("table_id")
        if "table_id" not in detail and isinstance(table_id, str) and table_id:
            detail["table_id"] = table_id[:300]
        row_start, row_end = scope.get("row_start"), scope.get("row_end")
        if isinstance(row_start, int) and isinstance(row_end, int) \
                and not isinstance(row_start, bool) and not isinstance(row_end, bool):
            detail["rows"] = [row_start, row_end]
    paths = args.get("paths")
    if isinstance(paths, list):
        detail["paths"] = [path[:300] for path in islice(paths, 3) if isinstance(path, str)]
    range_ = args.get("range")
    if isinstance(range_, list) and len(range_) == 2 and all(isinstance(n, int) for n in range_):
        detail["range"] = list(range_)
    previews = []
    for page in islice(pages, 3):
        preview = {key: value[:220] if key == "excerpt" else value[:300]
                   for key in ("title", "path", "excerpt")
                   if isinstance(value := page.get(key), str) and value}
        if preview:
            previews.append(preview)
    for view in islice(views, 3):
        # Only source blocks actually admitted to this tool's final payload.
        path = view.get("article_path")
        headings = view.get("title_path") or []
        title = headings[-1].get("title") if headings else path
        blocks = view.get("blocks") or []
        body = blocks[0].get("body") if blocks else None
        preview = {key: value[:220] if key == "excerpt" else value[:300]
                   for key, value in (("path", path), ("title", title), ("excerpt", body))
                   if isinstance(value, str) and value}
        if preview:
            previews.append(preview)
    if previews:
        detail["previews"] = previews
    return detail
