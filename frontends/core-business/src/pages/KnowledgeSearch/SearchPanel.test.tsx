/**
 * 「本体 + 数据」检索模式（2026-10-27 恢复，与纯「数据检索」并存）。
 *
 * 钉死三件事：
 * - 模式列表顺序：ontologyData（本体 + 数据）必须紧排在 data（数据检索）**之前**；
 * - ontologyData 档复用 fast 的默认严格配置、且可配置（齿轮可见）；
 * - 中英文 i18n 文案都存在，且原「数据检索」文案保持不变（两档并存）。
 */
import { describe, expect, it } from 'vitest';
import { DEFAULT_CONFIG_BY_MODE, MODE_META } from './SearchPanel';
import { DEFAULT_FAST_STRICT_CONFIG } from '@/api/knowledgeSearch';
import zh from '@/locales/zh.json';
import en from '@/locales/en.json';

describe('SearchPanel 模式列表', () => {
  it('顺序为 fast → deep → wiki → ontologyData → data（本体 + 数据在数据检索上方）', () => {
    expect(MODE_META.map((item) => item.mode)).toEqual(['fast', 'deep', 'wiki', 'ontologyData', 'data']);

    const ontologyIndex = MODE_META.findIndex((item) => item.mode === 'ontologyData');
    const dataIndex = MODE_META.findIndex((item) => item.mode === 'data');
    expect(ontologyIndex).toBeGreaterThan(-1);
    expect(ontologyIndex).toBe(dataIndex - 1);
  });

  it('ontologyData 档的文案 key 与可配置标记正确', () => {
    const meta = MODE_META.find((item) => item.mode === 'ontologyData');
    expect(meta).toEqual({
      mode: 'ontologyData',
      labelKey: 'knowledgeSearch.ontologyDataSearch',
      descKey: 'knowledgeSearch.ontologyDataSearchDesc',
      configurable: true,
    });
  });

  it('ontologyData 档复用 fast 的默认严格配置', () => {
    expect(DEFAULT_CONFIG_BY_MODE.ontologyData).toBe(DEFAULT_FAST_STRICT_CONFIG);
  });
});

describe('i18n：本体 + 数据文案', () => {
  it('中文文案存在，且数据检索文案保留', () => {
    expect(zh.knowledgeSearch.ontologyDataSearch).toBe('本体 + 数据');
    expect(zh.knowledgeSearch.ontologyDataSearchDesc).toBe('本体检索后追加数据库查询结果');
    expect(zh.knowledgeSearch.dataSearch).toBe('数据检索');
  });

  it('英文文案存在，且数据检索文案保留', () => {
    expect(en.knowledgeSearch.ontologyDataSearch).toBe('Ontology + Data');
    expect(en.knowledgeSearch.ontologyDataSearchDesc).toBe('Ontology search with database results appended');
    expect(en.knowledgeSearch.dataSearch).toBe('Data Search');
  });
});
