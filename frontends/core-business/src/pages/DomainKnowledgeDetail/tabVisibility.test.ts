import { describe, expect, it } from 'vitest';
import { resolveActiveTab, visibleTabs } from './tabVisibility';

const TABS = [
  { key: 'datasource' }, { key: 'parse' }, { key: 'schema' },
  { key: 'synonym' }, { key: 'lineage' }, { key: 'etl' },
  { key: 'governance' }, { key: 'abstractSchema' }, { key: 'permission' },
];

describe('visibleTabs', () => {
  it('hides structured tabs when disabled', () => {
    const keys = visibleTabs(TABS, false).map((t) => t.key);
    // datasource 不受结构化开关控制（数据源接入是基础知识库能力），恒展示。
    expect(keys).toEqual(['datasource', 'parse', 'synonym', 'permission']);
  });
  it('keeps all tabs when enabled', () => {
    expect(visibleTabs(TABS, true).map((t) => t.key)).toEqual(TABS.map((t) => t.key));
  });
});

describe('resolveActiveTab', () => {
  it('falls back to the first visible tab', () => {
    expect(resolveActiveTab('schema', TABS, false)).toBe('datasource');
  });
  it('keeps the datasource tab even when structured is disabled', () => {
    expect(resolveActiveTab('datasource', TABS, false)).toBe('datasource');
  });
  it('keeps a visible active tab', () => {
    expect(resolveActiveTab('synonym', TABS, false)).toBe('synonym');
  });
});
