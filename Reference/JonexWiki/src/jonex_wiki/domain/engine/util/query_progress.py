"""Small, public previews of actual retrieval work; no model reasoning or full payloads."""
from itertools import islice


def tool_progress_details(args, *, pages=(), views=()):
    detail = {}
    for key in ("query", "table_id", "operation"):
        value = args.get(key)
        if isinstance(value, str) and value:
            detail[key] = value[:300]
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
