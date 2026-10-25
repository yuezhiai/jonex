/**
 * 落点建议匹配 kernel（node 环境，无 DOM）：归一 / 四档优先级 / 同义词组桥 / 别名交叉。
 * 断言用归一后的字符串与档名，不锁中文文案。
 */
import { describe, expect, it } from 'vitest';

import {
  EMPTY_SYNONYM_INDEX, HIT_ORDER, buildSynonymIndex, hitKind, normalizeTerm, termsOf,
} from './matchKernel';

describe('normalizeTerm', () => {
  it('小写化并去掉下划线/空格/连字符', () => {
    expect(normalizeTerm('Work_Address City-Name')).toBe('workaddresscityname');
  });

  it('全角转半角后再归一（对齐后端 _to_halfwidth 口径）', () => {
    expect(normalizeTerm('ＷＯＲＫ＿ＡＤＤＲＥＳＳ')).toBe('workaddress');
    // 全角空格（U+3000）按空白处理
    expect(normalizeTerm('工作　地点')).toBe('工作地点');
  });

  it('空串 / 纯分隔符 → 空串', () => {
    expect(normalizeTerm('')).toBe('');
    expect(normalizeTerm(' _- ')).toBe('');
  });
});

describe('termsOf', () => {
  it('合并 name/displayName/aliases，归一后去空去重', () => {
    // 'JOBS' 归一后与 'Jobs' 撞键被去重；'job' 是另一个词，必须保留
    expect(termsOf({ name: 'Jobs', displayName: '职位', aliases: ['job', 'JOBS'] }))
      .toEqual(['jobs', '职位', 'job']);
  });

  it('无 aliases / 无 displayName / 全空时不炸', () => {
    expect(termsOf({ name: 'Jobs' })).toEqual(['jobs']);
    expect(termsOf({ name: '', aliases: [''] })).toEqual([]);
  });
});

describe('buildSynonymIndex', () => {
  it('归一化入索引；空组 / 空词 / undefined 跳过', () => {
    const idx = buildSynonymIndex([{ terms: ['职位', 'job_title'] }]);
    expect(idx.groupOf.get('jobtitle')).toBeDefined();
    expect(buildSynonymIndex([{ terms: [' ', ''] }]).groupOf.size).toBe(0);
    expect(buildSynonymIndex(undefined).groupOf.size).toBe(0);
  });

  it('脏数据（同词多组）取先出现的组，不抛错', () => {
    const dup = buildSynonymIndex([{ terms: ['职位', 'post'] }, { terms: ['职位', 'job'] }]);
    expect(dup.groupOf.get('职位')).toBe('0');
    expect(dup.groupOf.get('post')).toBe('0');
    expect(dup.groupOf.get('job')).toBe('1');
    expect(dup.groupOf.size).toBe(3);
  });
});

describe('hitKind', () => {
  const idx = buildSynonymIndex([{ terms: ['职位', 'job_title'] }]);

  it('exact：主名归一后全等（最高档）', () => {
    expect(hitKind({ name: 'work_address_city_name' }, { name: 'WorkAddressCityName' }, EMPTY_SYNONYM_INDEX))
      .toBe('exact');
  });

  it('display：显示名归一后全等', () => {
    expect(hitKind({ name: 'city_name', displayName: '工作城市' },
                    { name: '工作地点', displayName: '工作城市' }, EMPTY_SYNONYM_INDEX)).toBe('display');
  });

  it('display：单侧缺显示名时回退到自己的名字（对称回退）', () => {
    // 列无 display_name → 回退到列名；属性显示名 == 列名 → display
    expect(hitKind({ name: 'city' }, { name: 'city_name', displayName: 'city' }, EMPTY_SYNONYM_INDEX))
      .toBe('display');
    // 反向：属性无 displayName → 回退到属性名；列名 == 属性显示名 → display
    expect(hitKind({ name: 'city_name', displayName: 'city' }, { name: 'city' }, EMPTY_SYNONYM_INDEX))
      .toBe('display');
  });

  it('synonym：属同一同义词组', () => {
    // '职位' 与 'job_title' 同组；注意两侧都必须真的在 `idx` 那个组里
    expect(hitKind({ name: '职位' }, { name: 'job_title' }, idx)).toBe('synonym');
  });

  it('synonym：不同组不命中（回落到 null）', () => {
    const two = buildSynonymIndex([{ terms: ['岗位', 'post'] }, { terms: ['职务', 'job_title'] }]);
    expect(hitKind({ name: '岗位' }, { name: 'job_title' }, two)).toBeNull();
  });

  it('synonym：别名交叉全等（走词集交集这条路）', () => {
    // 主名不等（jobs vs job）、无 displayName → exact/display 都不中，靠 alias 命中
    expect(hitKind({ name: 'Jobs', aliases: ['Job'] }, { name: 'Job' }, EMPTY_SYNONYM_INDEX))
      .toBe('synonym');
  });

  it('优先级：同时满足更高档时取更高档', () => {
    expect(HIT_ORDER.exact).toBeLessThan(HIT_ORDER.display);
    expect(HIT_ORDER.display).toBeLessThan(HIT_ORDER.synonym);
    expect(HIT_ORDER.synonym).toBeLessThan(HIT_ORDER.token);

    // 同组（synonym）同时显示名全等（display）→ display 胜
    expect(hitKind({ name: '岗位', displayName: '职位' }, { name: 'job', displayName: '职位' }, idx))
      .toBe('display');
    // 同组（synonym）同时主名互为子串（token）→ synonym 胜
    expect(hitKind({ name: 'job_title' }, { name: 'job_title_v2' },
                    buildSynonymIndex([{ terms: ['job_title', 'job_title_v2'] }]))).toBe('synonym');
  });

  it('token：主名互为子串且短串 ≥3', () => {
    expect(hitKind({ name: 'work_address_city_name' }, { name: 'city' }, EMPTY_SYNONYM_INDEX)).toBe('token');
    expect(hitKind({ name: 'city' }, { name: 'work_address_city_name' }, EMPTY_SYNONYM_INDEX)).toBe('token');
  });

  it('token：短串 <3 不命中（防 id 噪声）', () => {
    expect(hitKind({ name: 'work_address_province_id' }, { name: 'id' }, EMPTY_SYNONYM_INDEX)).toBeNull();
  });

  it('不命中返回 null', () => {
    expect(hitKind({ name: 'note' }, { name: '工作地点' }, EMPTY_SYNONYM_INDEX)).toBeNull();
  });

  it('第三参缺省 = 空索引（不接同义词表）', () => {
    expect(hitKind({ name: '岗位' }, { name: 'job_title' })).toBeNull();
  });
});
