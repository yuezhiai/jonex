/**
 * 同义词索引取数 + 缓存（node 环境）：命中缓存 / 按 kbId 分离 / 失败降级且不缓存。
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { loadSynonymIndex } from './synonymIndexCache';
import type { SynonymIndex } from './matchKernel';

const getAllSynonyms = vi.hoisted(() => vi.fn());
vi.mock('@/api/ontologySynonym', () => ({ exportAllSynonyms: getAllSynonyms }));

beforeEach(() => {
  getAllSynonyms.mockReset();
});

describe('loadSynonymIndex', () => {
  it('首次拉取并写缓存；第二次命中缓存不再请求', async () => {
    getAllSynonyms.mockResolvedValue([{ id: 's1', terms: ['职位', 'job_title'] }]);
    const cache = new Map<string, SynonymIndex>();
    const onFallback = vi.fn();

    const first = await loadSynonymIndex(cache, 'kb_1', onFallback);
    expect(first.groupOf.get('jobtitle')).toBeDefined();

    const second = await loadSynonymIndex(cache, 'kb_1', onFallback);
    expect(second).toBe(first);                 // 同一份缓存对象
    expect(getAllSynonyms).toHaveBeenCalledTimes(1);
    expect(onFallback).not.toHaveBeenCalled();
  });

  it('不同 kbId 各自缓存', async () => {
    getAllSynonyms.mockResolvedValue([]);
    const cache = new Map<string, SynonymIndex>();

    await loadSynonymIndex(cache, 'kb_1', vi.fn());
    await loadSynonymIndex(cache, 'kb_2', vi.fn());

    expect(getAllSynonyms).toHaveBeenCalledTimes(2);
    expect(cache.size).toBe(2);
  });

  it('取数失败 → 回调降级 + 返回空索引 + 不写缓存（下次可重试）', async () => {
    getAllSynonyms.mockRejectedValue(new Error('down'));
    const cache = new Map<string, SynonymIndex>();
    const onFallback = vi.fn();

    const idx = await loadSynonymIndex(cache, 'kb_1', onFallback);
    expect(idx.groupOf.size).toBe(0);
    expect(onFallback).toHaveBeenCalledTimes(1);
    expect(cache.size).toBe(0);

    await loadSynonymIndex(cache, 'kb_1', onFallback);
    expect(getAllSynonyms).toHaveBeenCalledTimes(2);   // 失败没被缓存钉死
  });
});
