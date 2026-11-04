import { describe, expect, it } from 'vitest';

import {
  candidateChipLabel,
  candidateChipTags,
  candidateScopeHint,
  splitCandidates,
  toggleChipSelection,
} from './DataBlocksView.js';

describe('candidateChipLabel', () => {
  it('有中文 display_name ⇒ 中文名优先、原名进括号', () => {
    expect(candidateChipLabel({
      entity_name: 'LineMaster',
      display_name: '焚烧线主数据',
      source_name: 'wte_a 生产库',
      matched_term: '线名称',
    })).toBe('焚烧线主数据 · wte_a 生产库（LineMaster）');
  });

  it('无 display_name 或与原名相同 ⇒ 原名 + 命中词（原行为）', () => {
    expect(candidateChipLabel({ entity_name: 'LineMaster', matched_term: '线名称' }))
      .toBe('LineMaster（线名称）');
    expect(candidateChipLabel({ entity_name: 'LineMaster', display_name: 'LineMaster' }))
      .toBe('LineMaster');
  });

  it('指标候选 ⇒ 落点实体中文名优先，指标名进括号', () => {
    expect(candidateChipLabel({
      entity_name: 'DwdM12PlantDaily',
      entity_display_name: '集团吨发日汇总',
      kind: 'metric',
      metric: '吨垃圾发电量',
      source_name: 'wte_dw 汇总库',
      match_kind: 'aggregate',
    })).toBe('集团吨发日汇总 · wte_dw 汇总库（指标：吨垃圾发电量 · DwdM12PlantDaily）');
    // 没有落点中文名 ⇒ 指标名打头，仍带原名
    expect(candidateChipLabel({
      entity_name: 'DwdM12PlantDaily', kind: 'metric', metric: '吨垃圾发电量',
    })).toBe('吨垃圾发电量（DwdM12PlantDaily）');
  });
});

describe('candidateChipTags / candidateScopeHint', () => {
  it('指标候选 + 聚合兜底 + 源范围各出一个标签', () => {
    expect(candidateChipTags({
      entity_name: 'DwdM12PlantDaily', kind: 'metric', match_kind: 'aggregate',
      source_name: 'wte_dw 汇总库',
    })).toEqual(['指标', '聚合兜底', '汇总源']);
  });

  it('概念候选只有源范围标签；无关键词的源名不出范围', () => {
    expect(candidateChipTags({ entity_name: 'LineMaster', source_name: 'wte_a 生产库' }))
      .toEqual(['生产源']);
    expect(candidateChipTags({ entity_name: 'X', source_name: 'misc' })).toEqual([]);
  });

  it('scope hint 由源名推断', () => {
    expect(candidateScopeHint('wte_a 生产库')).toBe('生产源');
    expect(candidateScopeHint('wte_dw 汇总库')).toBe('汇总源');
    expect(candidateScopeHint('')).toBe('');
    expect(candidateScopeHint(null)).toBe('');
  });
});

describe('toggleChipSelection', () => {
  const lineA = { entity_name: 'LineMaster', data_source_id: 'a' };
  const lineADw = { entity_name: 'PlantALineMaster', data_source_id: 'a' };
  const lineB = { entity_name: 'LineMaster', data_source_id: 'b' };

  it('不同数据源可多选', () => {
    const s1 = toggleChipSelection({}, lineA);
    const s2 = toggleChipSelection(s1, lineB);
    expect(s2).toEqual({ a: 'LineMaster', b: 'LineMaster' });
  });

  it('同一数据源再点另一个实体 ⇒ 替换（pin 是 ds→entity 单值）', () => {
    expect(toggleChipSelection({ a: 'LineMaster' }, lineADw)).toEqual({ a: 'PlantALineMaster' });
  });

  it('再点已选中的实体 ⇒ 取消', () => {
    expect(toggleChipSelection({ a: 'LineMaster', b: 'LineMaster' }, lineA))
      .toEqual({ b: 'LineMaster' });
  });

  it('没有 data_source_id 的候选不参与选择', () => {
    const prev = { a: 'LineMaster' };
    expect(toggleChipSelection(prev, { entity_name: 'X' })).toBe(prev);
  });
});

describe('splitCandidates', () => {
  it('无候选段 ⇒ 原文不动', () => {
    const text = '**count** = 17\n\n引擎：NL→SQL（②）';
    expect(splitCandidates(text)).toBe(text);
  });

  it('单段候选（② 澄清块）⇒ 切掉该段（既有行为）', () => {
    expect(splitCandidates('选哪个？\n\n候选：\n- Job\n- Company')).toBe('选哪个？');
  });

  it('两段候选（DI 澄清 + ② 失败并存）⇒ 只切最后一段，DI 段与「答不了」留正文', () => {
    const text =
      '你想统计服务数量，还是内存总量？\n\n候选：\n- t_a\n- t_b\n\n答不了：命中多个实体\n\n候选：\n- Job\n- Company';
    expect(splitCandidates(text)).toBe(
      '你想统计服务数量，还是内存总量？\n\n候选：\n- t_a\n- t_b\n\n答不了：命中多个实体');
  });
});
