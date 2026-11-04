/**
 * 数据答案正文的展示裁剪。
 *
 * router 现在把答案组织成「LLM 总结 + **数据引用**（表格 + SQL）」，而
 * `DataBlocksView` 已经把同一份结果渲染成真表格（SQL 折叠可见）。两处都显示
 * 就是重复：有结构化块时，把正文里的引用段切掉，只留总结。
 *
 * - 老答案 / RAG / 本体（没有标记）⇒ 原样返回，零回归；
 * - 有标记但**没有**结构化块（老后端只发文本）⇒ 保留，否则信息直接丢。
 */
import { describe, expect, it } from 'vitest';

import { stripDataReference } from './dataAnswerText';

const WITH_REF = [
  '2026年10月12日至10月18日，#3炉吨垃圾发电量为521.3 kWh/t，达标。',
  '',
  '**数据引用**',
  '',
  '### 数据库接入',
  '```',
  'line_code | ton_garbage_power',
  'LINE-03 | 521.3',
  '```',
].join('\n');

describe('stripDataReference', () => {
  it('有结构化块 ⇒ 只留总结，表格交给 DataBlocksView', () => {
    expect(stripDataReference(WITH_REF, true)).toBe(
      '2026年10月12日至10月18日，#3炉吨垃圾发电量为521.3 kWh/t，达标。',
    );
  });

  it('没有结构化块 ⇒ 保留引用段（否则表格与 SQL 全丢）', () => {
    expect(stripDataReference(WITH_REF, false)).toBe(WITH_REF);
  });

  it('老答案没有标记 ⇒ 原样返回', () => {
    const old = '### 数据库接入\n```\na | b\n```\n\nsql：\n```\nSELECT 1\n```';
    expect(stripDataReference(old, true)).toBe(old);
  });
});
