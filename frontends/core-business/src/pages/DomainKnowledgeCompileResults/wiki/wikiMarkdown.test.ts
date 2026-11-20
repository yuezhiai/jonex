import { describe, expect, it } from 'vitest';
import { autoLinkMentions, hideFragmentLinks, resolveWikiLinks, splitWikiBody } from './wikiMarkdown';
import type { WikiReaderPage } from '@/types/domainKnowledge';

describe('Agent article reading', () => {
  it('hides fragment links while keeping article, table and original-document links', () => {
    const text = '[A](#wiki/articles%2Fa.md) [T](#wiki/tables%2Ft.md) [C](#wiki/concepts%2Fc.md) [原文](#wiki-source/doc)';
    expect(hideFragmentLinks(text, new Set(['articles/a.md', 'tables/t.md'])))
      .toBe('[A](#wiki/articles%2Fa.md) [T](#wiki/tables%2Ft.md) C [原文](#wiki-source/doc)');
  });
  it('retains the introduction before the first section', () => {
    const parts = splitWikiBody('# Article\n\nA substantive introduction.\n\n## History\n\nMore details.');
    expect(parts.lead).toContain('A substantive introduction.');
    expect(parts.body).toContain('## History');
    expect(parts.body).toContain('More details.');
  });

  it('keeps registered source-document links for the authorized viewer', () => {
    const result = resolveWikiLinks('[Original](#wiki-source/doc-one)', () => null);
    expect(result.markdown).toBe('[Original](#wiki-source/doc-one)');
    expect(result.unresolved).toEqual([]);
  });

  it('keeps Markdown table syntax even when a page alias matches its header', () => {
    const table = '| 年度 | 北区 | 南区 |\n| --- | ---: | ---: |\n| 2024 | 720 | 580 |';
    const page: WikiReaderPage = {
      path: 'tables/energy.md', title: '用电数据', type: 'table', category: 'tables',
      description: '', aliases: ['| 年度 | 北区 | 南区 |'], tags: [], whyVisit: '', referenceCount: 1,
    };
    const result = autoLinkMentions(`${table}\n\n查看用电数据。`, {
      pages: [page], currentPath: 'articles/report.md',
    });
    expect(result.markdown).toContain(table);
    expect(result.markdown).toContain('[用电数据](#wiki/tables%2Fenergy.md)');
    expect(result.linked).toEqual(['tables/energy.md']);
    const withoutOuterPipes = '年度 | 北区 | 南区\n--- | ---: | ---:\n2024 | 720 | 580';
    const other = { ...page, aliases: ['年度 | 北区 | 南区'] };
    expect(autoLinkMentions(withoutOuterPipes, {
      pages: [other], currentPath: 'articles/report.md',
    }).markdown).toBe(withoutOuterPipes);
  });
});
