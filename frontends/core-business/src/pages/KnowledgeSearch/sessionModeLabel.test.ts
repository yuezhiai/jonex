/**
 * 会话卡片档位徽标的文案映射（2026-10-27 review 修复）。
 *
 * 旧实现只认 fast / deep，其余一律回落 wikiSearch —— 「本体 + 数据」与「数据检索」
 * 在会话卡上被错标成「Wiki 检索」。这里钉死完整映射与两语言文案。
 */
import { describe, expect, it } from 'vitest';
import { SEARCH_MODE_LABEL_KEYS, searchModeLabelKey } from './sessionModeLabel';
import type { KnowledgeSearchUiMode } from '@/types/knowledgeSearch';
import zh from '@/locales/zh.json';
import en from '@/locales/en.json';

function label(locale: typeof zh, mode?: KnowledgeSearchUiMode): string {
  const key = searchModeLabelKey(mode).replace(/^knowledgeSearch\./, '');
  return (locale.knowledgeSearch as unknown as Record<string, string>)[key];
}

describe('SEARCH_MODE_LABEL_KEYS', () => {
  it('覆盖全部档位（新增档位漏配时该断言加类型检查一起报错）', () => {
    expect(SEARCH_MODE_LABEL_KEYS).toEqual({
      fast: 'knowledgeSearch.fastSearch',
      deep: 'knowledgeSearch.deepSearch',
      wiki: 'knowledgeSearch.wikiSearch',
      ontologyData: 'knowledgeSearch.ontologyDataSearch',
      data: 'knowledgeSearch.dataSearch',
    });
  });

  it('缺失/未知档位保持旧兜底（Wiki 检索），不抛错', () => {
    expect(searchModeLabelKey(undefined)).toBe('knowledgeSearch.wikiSearch');
  });
});

describe('会话卡档位徽标文案', () => {
  it('中文：ontologyData → 本体 + 数据，data → 数据检索', () => {
    expect(label(zh, 'ontologyData')).toBe('本体 + 数据');
    expect(label(zh, 'data')).toBe('数据检索');
  });

  it('英文：ontologyData → Ontology + Data，data → Data Search', () => {
    expect(label(en, 'ontologyData')).toBe('Ontology + Data');
    expect(label(en, 'data')).toBe('Data Search');
  });

  it('fast / deep / wiki 文案保持不变', () => {
    expect(label(zh, 'fast')).toBe('快速检索');
    expect(label(zh, 'deep')).toBe('深度检索');
    expect(label(zh, 'wiki')).toBe('Wiki 检索');
    expect(label(en, 'fast')).toBe('Fast Search');
    expect(label(en, 'deep')).toBe('Deep Search');
    expect(label(en, 'wiki')).toBe('Wiki Search');
  });
});
