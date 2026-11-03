#!/usr/bin/env python3
"""Export verified Wiki media artifacts for inspection without running the platform."""
from __future__ import annotations

import argparse
import base64
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'Reference/JonexWiki/src'))
from jonex_wiki.adapters.bundle_view import DirectoryBundleView, read_revision_header
from jonex_wiki.domain.reading.media_service import read_media


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('bundle', type=Path, help='Immutable published Bundle directory')
    parser.add_argument('--output', type=Path, required=True, help='Local inspection directory outside the Bundle')
    parser.add_argument('--author', default='')
    parser.add_argument('--work', default='')
    args = parser.parse_args()
    pub, output = args.bundle.resolve(), args.output.resolve()
    if output.is_relative_to(pub):
        parser.error('Inspection output must be outside the immutable Bundle')
    revision = read_revision_header(pub)
    view = DirectoryBundleView(tenant_id=revision['tenant_id'], context_id=revision['kb_id'], revision_dir=pub)
    if 'query/media_catalog.json' not in view.list_entries():
        parser.error('This Bundle has no media catalog; recompile into a NEW publication first')
    catalog = json.loads(view.read_bytes('query/media_catalog.json'))
    bindings = [b for b in catalog['bindings'] if (not args.author or b['author'] == args.author)
                and (not args.work or b['title'] == args.work)]
    output.mkdir(parents=True, exist_ok=True)
    images = output / 'images'; images.mkdir(exist_ok=True)
    records = []
    for binding in bindings:
        asset = next(a for a in catalog['assets'] if a['asset_id'] == binding['asset_id'])
        data = read_media(view, asset['asset_id'], asset['document_id'], True)
        filename = Path(asset['path']).name
        (images / filename).write_bytes(base64.b64decode(data['content_base64'], validate=True))
        records.append({**binding, 'asset': asset, 'export_path': str(images / filename)})
    summary = {'bundle_sha': view.manifest_hash, 'source_bundle': str(pub), 'schema': catalog['schema'],
               'counts': {key: len(catalog[key]) for key in ['entities', 'relations', 'assets', 'bindings']},
               'unresolved': dict(Counter(item['reason'] for item in catalog['unresolved'])),
               'exported_bindings': len(records), 'author_filter': args.author, 'work_filter': args.work,
               'note': 'Layout associations require review. Catalog is partial; absence is not a negative fact. PDF pages are 1-based.'}
    for name, value in [('summary.json', summary), ('bindings.json', records), ('unresolved.json', catalog['unresolved'])]:
        (output / name).write_text(json.dumps(value, ensure_ascii=False, indent=2), 'utf-8')
    lines = ['# Wiki 图片产物检查', '', f"发布版本：`{view.manifest_hash}`", '',
             '这份报告直接读取发布清单并校验内容；不启动平台，不修改原 Bundle。', '',
             '目录覆盖不完整，未收录不表示不存在。布局推断与书籍文字断言是不同的证据。', '']
    for record in records:
        asset = record['asset']; refs = record['evidence']
        lines += [f"## {record['author']}《{record['title']}》", '',
                  f"PDF 第 {asset['pdf_page']} 页；位置 {asset['image_idx']}；关联规则 `{record['method']}`。", '',
                  f"![{record['title']}]({record['export_path']})", '',
                  f"图片 SHA256：`{asset['sha256']}`", '',
                  *[f"原文 `{r['path']}` 字符区间 [{r['start_char']}, {r['end_char']})：\n\n> " + r['excerpt'].replace('\n', '\n> ') + '\n' for r in refs],
                  '代表作原文证据：' + ('有，详见 bindings.json。' if record.get('representative_evidence') else '未在本目录中确认。'), '']
    (output / 'report.md').write_text('\n'.join(lines), 'utf-8')
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
