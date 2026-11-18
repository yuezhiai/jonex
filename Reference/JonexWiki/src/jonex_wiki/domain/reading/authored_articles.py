"""Expose indexed Agent-written articles without changing immutable Bundles.

Only links reachable from the registered authored index become pages. Source
membership is derived from explicit links to registered source material; it is
not a claim that every sentence has been verified against that source.
"""
from __future__ import annotations

from collections import deque
from functools import lru_cache
import logging
import posixpath
import re
import unicodedata
from pathlib import Path
from jonex_wiki.domain.reading.article_links import (
    MARKDOWN_LINK as _LINK,
    authored_link_targets,
    resolve_link as _target,
)
from jonex_wiki.domain.reading.direct_file_evidence import _BundleEvidenceAccess, _metadata

log = logging.getLogger(__name__)
_ROOT_INDEX = 'articles/authored/index.md'
_MAX_ARTICLES = 10000
_MAX_ARTICLE_BYTES = 4 * 1024 * 1024
_TABLE_INDEX = "articles/tables/index.md"


def check_authored_table_links(view, files):
    """Keep registered table links and flag links that name a different table."""
    registered = set(view.list_entries("full-tables/"))
    aliases = {}

    def label_key(label):
        return " ".join(unicodedata.normalize("NFKC", label).casefold().split())

    has_index = _TABLE_INDEX in view.list_entries(_TABLE_INDEX)
    if has_index:
        for line in view.read_bytes(_TABLE_INDEX).decode("utf-8").splitlines():
            sheet = re.search(r"；工作表：([^；]+)", line)
            for match in _LINK.finditer(line):
                target = _target(_TABLE_INDEX, match.group(2))
                if target in registered:
                    aliases.setdefault(label_key(match.group(1)), set()).add(target)
                    if sheet:
                        aliases.setdefault(label_key(sheet.group(1)), set()).add(target)

    checked = dict(files)
    issues = []
    for article, data in files.items():
        body = data.decode("utf-8", errors="replace")

        def check(match):
            label = match.group(1)
            target = _target(article, match.group(2))
            if not target or not target.startswith("full-tables/"):
                return match.group()
            named = aliases.get(label_key(label), set())
            if target in registered and (not named or target in named):
                return match.group()
            issues.append({"article": article, "label": label, "target": target})
            recovery = ""
            if has_index:
                index_link = posixpath.relpath(_TABLE_INDEX, posixpath.dirname(article))
                recovery = f"，见[完整表格目录]({index_link})"
            return f"{label}（表链接待核对{recovery}）"

        adjusted = _LINK.sub(check, body)
        if adjusted != body:
            checked[article] = adjusted.encode("utf-8")
    return checked, issues


def _title(body: str, fallback: str) -> str:
    match = re.search(r'^#\s+(.+?)\s*$', body, re.MULTILINE)
    return (match.group(1).strip() if match else fallback)[:500]


def _description(body: str) -> str:
    text = re.sub(r'^#\s+.*$', '', body, count=1, flags=re.MULTILINE)
    for paragraph in re.split(r'\n\s*\n', text):
        paragraph = paragraph.strip()
        if paragraph and not paragraph.startswith(('#', '|', '```', '- ', '* ')):
            return re.sub(r'\s+', ' ', paragraph)[:500]
    return ''


def _safe_read(access: _BundleEvidenceAccess, path: str) -> str:
    target = access.path(path)
    if target.stat().st_size > _MAX_ARTICLE_BYTES:
        raise ValueError('ARTICLE_TOO_LARGE')
    return target.read_text(encoding='utf-8')


def _source_paths(access: _BundleEvidenceAccess) -> dict[str, tuple[str, str]]:
    from jonex_wiki.domain.engine.compile.table_navigation import full_table_path
    owners = {}
    for source_id, source in access.by_source.items():
        document_id = source.get('document_id')
        if not document_id:
            continue
        paths = [access.article(source_id)]
        paths.extend(full_table_path(source_id, table_id) for table_id in source.get('table_ids') or [])
        for path in paths:
            if path and path in access.files:
                owners.setdefault(path, set()).add((source_id, str(document_id)))
    return {path: next(iter(values)) for path, values in owners.items() if len(values) == 1}


def iter_authored_articles(files, read):
    """Read only Markdown articles reachable from the registered contents."""
    if _ROOT_INDEX not in files:
        return
    indexes = deque([_ROOT_INDEX])
    seen = set()
    while indexes and len(seen) < _MAX_ARTICLES:
        index = indexes.popleft()
        if index in seen:
            continue
        seen.add(index)
        for match in _LINK.finditer(read(index)):
            if len(seen) >= _MAX_ARTICLES:
                break
            target = _target(index, match.group(2))
            if not target or not target.startswith('articles/authored/') or not target.endswith('.md'):
                continue
            if target not in files or target in seen:
                continue
            if Path(target).name == 'index.md':
                indexes.append(target)
                continue
            try:
                article = read(target)
            except (OSError, ValueError, UnicodeError):
                log.warning('Skipping invalid registered authored article %s', target)
                continue
            seen.add(target)
            yield target, _title(article, match.group(1).strip() or Path(target).stem), article


def _linked_source_owners(access: _BundleEvidenceAccess, path: str | None) -> set[tuple[str, str]]:
    """Resolve an explicit link to a registered source-bearing Wiki page.

    An article-to-article navigation link is deliberately not followed: it
    does not establish that the first article used the second one's sources.
    """
    prefixes = ('wiki/source-evidence/', 'wiki/source-articles/', 'wiki/sources/', 'wiki/tables/')
    if not path or not path.startswith(prefixes) or path not in access.files or not path.endswith('.md'):
        return set()
    try:
        metadata = _metadata(_safe_read(access, path))
        if metadata.get('kind') != 'page' or metadata.get('path') != path.removeprefix('wiki/'):
            return set()
        scope = metadata.get('scope') if isinstance(metadata.get('scope'), dict) else {}
        declared = metadata.get('source_ids')
        scoped = scope.get('source_ids')
        if not isinstance(declared, list) or not declared or (scoped is not None and
                (not isinstance(scoped, list) or set(scoped) != set(declared))):
            return set()
        result = set()
        for source_id in declared:
            source = access.by_source.get(source_id)
            if not isinstance(source, dict) or not source.get('document_id'):
                return set()
            result.add((source_id, str(source['document_id'])))
        return result
    except (OSError, ValueError, TypeError, UnicodeError):
        return set()


@lru_cache(maxsize=4)
def _catalog(root: str, stamp: tuple[int, int]) -> tuple[dict, ...]:
    del stamp
    try:
        access = _BundleEvidenceAccess(Path(root))
        if _ROOT_INDEX not in access.files:
            return ()
        owners = _source_paths(access)
        articles = {}
        linked_owners = {}
        for target, title, article in iter_authored_articles(access.files, lambda path: _safe_read(access, path)):
            targets = list(dict.fromkeys(authored_link_targets(target, article)))[:512]
            linked = set()
            for path in targets:
                direct = owners.get(path)
                if direct:
                    linked.add(direct)
                if path not in linked_owners:
                    linked_owners[path] = _linked_source_owners(access, path)
                linked.update(linked_owners[path])
            articles[target] = {
                'path': target, 'title': title, 'type': 'article',
                'description': _description(article),
                'source_ids': sorted({source for source, _ in linked}),
                'document_ids': sorted({document for _, document in linked}),
                '_link_candidates': targets,
            }
        # Article-to-article links are navigation, not evidence inheritance.
        for path, article in articles.items():
            article['links'] = [{'target_id': target, 'kind': 'navigation'}
                                for target in article.pop('_link_candidates')
                                if target in articles and target != path]
        return tuple(articles[path] for path in sorted(articles))
    except (OSError, ValueError, KeyError, TypeError, UnicodeError):
        log.warning('Cannot read the registered authored article index', exc_info=True)
        return ()


def authored_articles(pub: Path) -> list[dict]:
    manifest = pub / 'indexes/manifest.json'
    try:
        stat = manifest.stat()
    except OSError:
        return []
    return [dict(item) for item in _catalog(str(pub.resolve()), (stat.st_mtime_ns, stat.st_size))]


def read_authored(pub: Path, page_id: str) -> dict | None:
    item = next((row for row in authored_articles(pub) if row['path'] == page_id), None)
    if item is None:
        return None
    access = _BundleEvidenceAccess(pub)
    body = _safe_read(access, page_id)
    # Convert only registered source-article links to platform document links.
    # The document viewer still performs the platform's authorization check.
    from urllib.parse import quote
    owners = _source_paths(access)
    source_documents = {path: doc for path, (_, doc) in owners.items()
                        if path.startswith('sources/articles/') and doc in item['document_ids']}
    # Existing table pages provide the bounded human-readable table projection.
    # Only map a registered full-table file when its source and table identity
    # uniquely agree with a page in this same publication.
    table_pages = {}
    navigation_file = pub / 'query/navigation.json'
    if navigation_file.is_file():
        from jonex_wiki.domain.reading.bundle_reader import load_navigation
        from jonex_wiki.domain.engine.compile.table_navigation import full_table_path
        table_sources = {}
        for source_id, source in access.by_source.items():
            for table_id in source.get('table_ids') or []:
                table_sources.setdefault(table_id, []).append((source_id, source.get('document_id')))
        for page, entry in (load_navigation(pub).get('page_index') or {}).items():
            # A raw table may have many chunk pages with the same object_id.
            # They are not competing human-readable table destinations.
            if not page.startswith('tables/') or not isinstance(entry, dict):
                continue
            scope = entry.get('scope') if isinstance(entry.get('scope'), dict) else {}
            table_id = entry.get('object_id') or scope.get('table_id')
            for source_id, document_id in table_sources.get(table_id, []):
                if scope.get('source_id') and scope['source_id'] not in {source_id, document_id}:
                    continue
                if scope.get('source_ids') and source_id not in scope['source_ids']:
                    continue
                table = full_table_path(source_id, table_id)
                if table in owners:
                    table_pages.setdefault(table, []).append(page)

    def source_link(match):
        target = _target(page_id, match.group(2))
        document = source_documents.get(target)
        if document:
            return f"[{match.group(1)}](#wiki-source/{quote(document, safe='')})"
        pages = table_pages.get(target) or []
        if len(pages) == 1:
            return f"[{match.group(1)}](#wiki/{quote(pages[0], safe='')})"
        if _linked_source_owners(access, target):
            return f"[{match.group(1)}](#wiki/{quote(target.removeprefix('wiki/'), safe='')})"
        return match.group(0)

    body = _LINK.sub(source_link, body)
    return {
        'path': page_id, 'type': 'article',
        'index': {'title': item['title'], 'description': item['description']},
        'page': {'version': 0, 'related_pages': [], 'links': item['links'], 'source_ids': item['source_ids']},
        'body': body, 'content': body,
        'references': [{'document_id': doc, 'excerpts': [], 'claim_count': 0} for doc in item['document_ids']],
    }


def bind_article_images(view, page_id: str, body: str, document_ids: list[str], limit: int = 6):
    """Bind explicit authored image choices to unambiguous registered assets.

    An author's link is an illustration choice, not visual evidence. Other
    source images remain discoverable through the separate picture search.
    """
    import json
    from contextlib import closing
    from jonex_wiki.domain.reading.image_service import ASSET_ID, INDEX, _validate_index, open_index

    if INDEX not in view.list_entries() or not document_ids:
        return body, [], 0
    pattern = re.compile(r'!\[([^\]\n]*)\]\(([^\s)]+)(?:\s+"[^"]*")?\)')
    selected = []
    seen = set()
    with closing(open_index(view)) as db:
        _validate_index(db)

        def bind(path, method):
            if len(selected) >= limit:
                return None
            if not path or not path.startswith('media/assets/') or path not in view.list_entries():
                return None
            rows = db.execute(
                "SELECT payload FROM images WHERE json_extract(payload, '$.path') = ? "
                "AND document_id IN (SELECT value FROM json_each(?)) LIMIT 2",
                (path, json.dumps(document_ids)),
            ).fetchall()
            matches = [json.loads(row[0]) for row in rows]
            if (len(matches) != 1 or not ASSET_ID.fullmatch(matches[0].get('asset_id') or '')
                    or matches[0]['asset_id'] in seen):
                return None
            asset = matches[0]
            seen.add(asset['asset_id'])
            selected.append({**asset, 'bundle_sha': view.manifest_hash, 'placement': 'inline',
                             'association_method': method,
                             'source_page': page_id, 'usage': 'illustration'})
            return asset

        def replace(match):
            asset = bind(_target(page_id, match.group(2)), 'authored_explicit_image_link')
            if not asset:
                return match.group(0)
            return f"![{match.group(1)}](#wiki-image/{asset['asset_id']})"

        body = pattern.sub(replace, body)
        # Some writers explicitly identify one asset in prose without using
        # Markdown image syntax. Render that single unambiguous choice; never
        # infer a choice from proximity or insert all pictures. Code spans and
        # existing Markdown destinations remain literal.
        parts = re.split(r'(```[\s\S]*?```|`[^`\n]+`)', body)
        plain = re.compile(r'(?<![\w/(])media/assets/[0-9a-f]{64}\.(?:png|jpe?g|webp)(?![\w/])', re.I)
        choices = {match.group(0) for part in parts[::2] for match in plain.finditer(part)}
        if len(choices) == 1:
            path = next(iter(choices))
            asset = bind(path, 'authored_explicit_asset_mention')
            if asset:
                for index in range(0, len(parts), 2):
                    parts[index], count = plain.subn(
                        lambda match: f"![Source illustration](#wiki-image/{asset['asset_id']})"
                        if match.group(0) == path else match.group(0), parts[index], count=1)
                    if count:
                        break
                body = ''.join(parts)
    return body, selected, len(selected)
