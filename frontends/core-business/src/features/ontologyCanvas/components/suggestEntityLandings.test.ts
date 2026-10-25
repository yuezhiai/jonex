/**
 * 实体级落点建议（node 环境，无 DOM）：排除已登记 / 同实体取最高档 / 强弱分档 / 按源分组。
 */
import { describe, expect, it } from 'vitest';

import { buildSynonymIndex } from './matchKernel';
import { suggestEntityLandings, type CandidateEntity } from './suggestEntityLandings';
import type { EntityType } from '../domain/index.js';

const entity = (over: Partial<EntityType> = {}): EntityType => ({
  id: 'e:职位', name: '职位', aliases: [], attributes: [], landings: [], ...over,
});

const cand = (over: Partial<CandidateEntity> & { dataSourceId: string; name: string }): CandidateEntity => ({
  ...over,
});

describe('suggestEntityLandings', () => {
  it('按源分组，组内按 档 → 实体名 排序；强档进 strong', () => {
    const out = suggestEntityLandings(entity(), [
      // 入参故意乱序：组序靠 sort，不靠插入序
      cand({ dataSourceId: 'ds_b', sourceName: '锦聘', name: 'Jobs', displayName: '职位' }),
      cand({ dataSourceId: 'ds_a', sourceName: 'genesis', name: 'job_logs' }),
      cand({ dataSourceId: 'ds_a', sourceName: 'genesis', name: 'Jobs', displayName: '职位' }),
      cand({ dataSourceId: 'ds_a', sourceName: 'genesis', name: '职位' }),
      cand({ dataSourceId: 'ds_a', sourceName: 'genesis', name: 'note' }),
    ]);

    expect(out.map((g) => g.dataSourceId)).toEqual(['ds_a', 'ds_b']);   // 组按源 id 排序
    expect(out[0]!.sourceName).toBe('genesis');
    // 同组内 exact（职位）排在 display（Jobs）之前；job_logs / note 不命中
    expect(out[0]!.strong.map((s) => `${s.entityName}:${s.hit}`)).toEqual(['职位:exact', 'Jobs:display']);
    expect(out[0]!.weak).toEqual([]);
    expect(out[1]!.sourceName).toBe('锦聘');
    expect(out[1]!.strong.map((s) => `${s.entityName}:${s.hit}`)).toEqual(['Jobs:display']);
  });

  it('token 档进 weak（不与强档混在一起）', () => {
    const out = suggestEntityLandings(entity({ name: 'job' }), [
      cand({ dataSourceId: 'ds_a', name: 'job_title' }),
      cand({ dataSourceId: 'ds_a', name: 'job' }),
    ]);
    expect(out).toHaveLength(1);
    expect(out[0]!.strong.map((s) => `${s.entityName}:${s.hit}`)).toEqual(['job:exact']);
    expect(out[0]!.weak.map((s) => `${s.entityName}:${s.hit}`)).toEqual(['job_title:token']);
  });

  it('排除已登记的 (源, 源实体) —— 含 included:false 的也排除', () => {
    const out = suggestEntityLandings(
      entity({ landings: [
        { dataSourceId: 'ds_a', entityName: '职位', included: true, stale: false },
        { dataSourceId: 'ds_b', entityName: '职位', included: false, stale: false },
      ] }),
      [
        cand({ dataSourceId: 'ds_a', name: '职位' }),
        cand({ dataSourceId: 'ds_b', name: '职位' }),
        cand({ dataSourceId: 'ds_c', name: '职位' }),
      ],
    );
    expect(out.map((g) => g.dataSourceId)).toEqual(['ds_c']);
  });

  it('同一 (源, 实体) 出现两次 → 取最高档（两个方向都杀）', () => {
    // 抽象实体 job_title_v2：cand 名字 job_title 命中 token；带上显示名后命中 display。
    // 两个方向各来一次 —— keep-first 会在方向 A 变红，无条件覆盖会在方向 B 变红。
    const ent = entity({ name: 'job_title_v2' });

    const tokenFirst = suggestEntityLandings(ent, [
      cand({ dataSourceId: 'ds_a', name: 'job_title' }),                              // token
      cand({ dataSourceId: 'ds_a', name: 'job_title', displayName: 'job_title_v2' }),  // display
    ]);
    expect(tokenFirst[0]!.strong.map((s) => `${s.entityName}:${s.hit}`)).toEqual(['job_title:display']);
    expect(tokenFirst[0]!.weak).toEqual([]);

    const displayFirst = suggestEntityLandings(ent, [
      cand({ dataSourceId: 'ds_a', name: 'job_title', displayName: 'job_title_v2' }),  // display
      cand({ dataSourceId: 'ds_a', name: 'job_title' }),                              // token
    ]);
    expect(displayFirst[0]!.strong.map((s) => `${s.entityName}:${s.hit}`)).toEqual(['job_title:display']);
    expect(displayFirst[0]!.weak).toEqual([]);
  });

  it('同义词组桥接 + 别名交叉', () => {
    const out = suggestEntityLandings(
      entity({ name: '职位', aliases: ['Job'] }),
      [
        cand({ dataSourceId: 'ds_a', name: 'post' }),
        cand({ dataSourceId: 'ds_a', name: 'Job' }),
      ],
      buildSynonymIndex([{ terms: ['职位', 'post'] }]),
    );
    const got = out[0]!.strong.map((s) => `${s.entityName}:${s.hit}`);
    expect(got).toHaveLength(2);
    expect(got).toContain('post:synonym');   // 同义词组桥
    expect(got).toContain('Job:synonym');    // 别名交叉
  });

  it('不传索引 = 不接同义词表（synonym 只剩别名交叉）', () => {
    const out = suggestEntityLandings(entity({ name: '职位' }), [
      cand({ dataSourceId: 'ds_a', name: 'post' }),
      cand({ dataSourceId: 'ds_a', name: 'Job' }),
    ]);
    expect(out).toEqual([]);
  });

  it('零候选 / 全部不命中 → 空数组', () => {
    expect(suggestEntityLandings(entity(), [])).toEqual([]);
    expect(suggestEntityLandings(entity(), [cand({ dataSourceId: 'ds_a', name: 'note' })])).toEqual([]);
  });
});
