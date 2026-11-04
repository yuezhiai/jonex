"""Source identity hints from sealed metadata; lexical mentions are not policies."""
import json
import re


def named_documents(view, question):
    """Find filename mentions only; inclusion/exclusion is not inferred here."""
    if view is None or "query/resources.json" not in getattr(view, "list_entries", lambda: ())():return []
    resources = json.loads(view.read_bytes("query/resources.json"))
    return [o["document_id"] for o in resources["objects"] if o["kind"] == "document"
        and o.get("label") and o["label"].casefold() in question.casefold()]


def named_source_paths(view, question, page_index):
    """Resolve named files through sealed metadata, before ranking body terms.

    A filename is a source identity hint, not evidence. The source page still
    has to be read and its links followed to the requested original material.
    """
    document_ids = named_documents(view, question)
    if not document_ids or "sources/registry.json" not in view.list_entries():
        return [], document_ids
    registry = json.loads(view.read_bytes("sources/registry.json"))
    paths = []
    for source in registry["sources"]:
        if source.get("document_id") not in document_ids:
            continue
        path = str(source.get("source_page") or "").removeprefix("wiki/")
        if path in page_index and path not in paths:
            paths.append(path)
    return paths, document_ids


def source_article_navigation_alias(corpus, path):
    """Resolve an advertised archived-article URI to its published navigation.

    Never read an arbitrary storage path. Only a unique directory proven by
    source-scoped literal pages in this QueryCorpus can be returned.
    """
    if not isinstance(path,str):
        return path
    match = re.fullmatch(r"sources/articles/([A-Za-z0-9_-]+)\.md",path)
    if not match or path in corpus.files:
        return path
    source_id = match.group(1)
    candidates = {page.rsplit("/",1)[0]+"/index.md" for page,entry in corpus.page_index.items()
        if page.startswith("source-articles/") and (getattr(entry,"scope",None)or{}).get("source_id")==source_id}
    candidates = {page for page in candidates if page in corpus.files}
    return next(iter(candidates)) if len(candidates)==1 else path



def exact_table_candidate(view, registry, question, document_ids=()):
    """Deprecated: natural-language matching must not force an answer path."""
    return None


def matrix_table_candidate(view, registry, question, document_ids=()):
    """Deprecated: use normal navigation or explicit structured predicates."""
    return None
