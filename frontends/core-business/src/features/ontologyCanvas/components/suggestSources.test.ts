/**
 * 字段级落点建议（node 环境，无 DOM）：候选组装 / 已落点去重 / 同三元组取最高档 / 排序 / synonym 档。
 *
 * ⚠️ `hitKind` 与 `normalizeTerm` 的用例已移到 `matchKernel.test.ts`（实现搬到 kernel）。
 */
import { describe, expect, it } from 'vitest';

import { buildSynonymIndex } from './matchKernel';
import { suggestSources, type LandingPair } from './suggestSources';
import type { Attribute } from '../domain/index.js';

const attr = (name: string, displayName?: string, sources: Attribute['sources'] = []): Attribute => ({
  id: `a:${name}`, name, type: 'string', required: false, primaryKey: false,
  ...(displayName ? { displayName } : {}), sources,
});

describe('suggestSources', () => {
  const pairs: LandingPair[] = [
    { dataSourceId: 'ds_a', entityName: 'Jobs', columns: [
      { name: 'city' }, { name: 'note' }, { name: 'city_name', displayName: '城市' },
    ] },
    { dataSourceId: 'ds_b', entityName: 'Posts', columns: [{ name: 'city' }] },
  ];

  it('跨源汇总命中，按 档位 → 数据源 → 源实体 → 列名 排序', () => {
    const out = suggestSources(attr('city'), pairs);
    expect(out.map((s) => `${s.dataSourceId}:${s.entityName}:${s.attributeName}:${s.hit}`)).toEqual([
      'ds_a:Jobs:city:exact',        // exact 打头
      'ds_b:Posts:city:exact',
      'ds_a:Jobs:city_name:token',   // token 在后
    ]);
    // displayName 从编译列带出（仅展示用，不写进落点）
    expect(out.find((s) => s.attributeName === 'city_name')!.displayName).toBe('城市');
  });

  it('已在 sources 里的三元组不再建议', () => {
    const out = suggestSources(
      attr('city', undefined, [{ dataSourceId: 'ds_a', entityName: 'Jobs', attributeName: 'city' }]),
      pairs,
    );
    expect(out.some((s) => s.dataSourceId === 'ds_a' && s.entityName === 'Jobs' && s.attributeName === 'city')).toBe(false);
    expect(out.map((s) => `${s.dataSourceId}:${s.attributeName}`)).toEqual(['ds_b:city', 'ds_a:city_name']);
  });

  it('同一三元组只保留最高档：高档在前（杀 keep-last）', () => {
    const dup: LandingPair[] = [
      { dataSourceId: 'ds_a', entityName: 'Jobs', columns: [{ name: 'cityname', displayName: 'city' }] },
      { dataSourceId: 'ds_a', entityName: 'Jobs', columns: [{ name: 'cityname' }] },
    ];
    // 属性名 city → 列 cityname 命中 token；同列显示名 city → 命中 display。
    // 高档（display）在前、低档（token）在后 → 只有「保留最高档」的实现留 display。
    const out = suggestSources(attr('city'), dup);
    expect(out).toHaveLength(1);
    expect(out[0]!.hit).toBe('display');
  });

  it('同一三元组只保留最高档：低档在前（杀 keep-first）', () => {
    const dup: LandingPair[] = [
      { dataSourceId: 'ds_a', entityName: 'Jobs', columns: [{ name: 'cityname' }] },
      { dataSourceId: 'ds_a', entityName: 'Jobs', columns: [{ name: 'cityname', displayName: 'city' }] },
    ];
    // 低档（token）在前、高档（display）在后 —— 「保留第一次见到的」实现会留 token 而变红。
    const out = suggestSources(attr('city'), dup);
    expect(out).toHaveLength(1);
    expect(out[0]!.hit).toBe('display');
  });

  it('synonym：同义词组桥接 → 命中 synonym 档', () => {
    const out = suggestSources(
      attr('职位'),
      [{ dataSourceId: 'ds_a', entityName: 'Jobs', columns: [{ name: 'job_title' }, { name: 'note' }] }],
      buildSynonymIndex([{ terms: ['职位', 'job_title'] }]),
    );
    expect(out.map((s) => `${s.attributeName}:${s.hit}`)).toEqual(['job_title:synonym']);
  });

  it('synonym：不传索引 = 不接同义词表（同输入不命中）', () => {
    const out = suggestSources(
      attr('职位'),
      [{ dataSourceId: 'ds_a', entityName: 'Jobs', columns: [{ name: 'job_title' }] }],
    );
    expect(out).toEqual([]);
  });

  it('synonym：主名 ↔ 对侧显示名 的槽位交叉（不接同义词表也命中）', () => {
    // 这是 spec §5 口径 1 的 ⓑ：`display` 档比的是「各自的最可读名」同一槽位，
    // 比不到这种交叉；`Attribute` 又没有 aliases，所以这条是**字段级** synonym 档
    // 除同义词组之外的唯一来源。标签是「synonym」而非「display」，别改错。
    const out = suggestSources(
      attr('工作地点', '地点'),
      [{ dataSourceId: 'ds_a', entityName: 'Jobs', columns: [{ name: 'jobs', displayName: '工作地点' }] }],
    );
    expect(out.map((s) => `${s.attributeName}:${s.hit}`)).toEqual(['jobs:synonym']);
  });

  it('无候选返回空数组', () => {
    expect(suggestSources(attr('工作地点'), pairs)).toEqual([]);
  });
});
