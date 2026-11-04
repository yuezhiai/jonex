/**
 * 对象 / 来源选择器的「展示与筛选分离」。
 *
 * 三种选项都必须满足：`label` 给人看（能力释义 / 后端下发名称），`value` 给接口用（原始标识）。
 * 拼错任何一侧都会表现为「筛选没结果」或「页面又出现裸 ID」。
 */
import { describe, expect, it } from 'vitest';
import type { TFunction } from 'i18next';
import platformZh from '../../locales/zh.json';
import platformEn from '../../locales/en.json';
import { PM_ALL } from '../../constants/platformMonitor';
import { CAPABILITY_IDS } from './capabilityLabels';
import {
  buildCallTargetOptions,
  buildScopeObjectOptions,
  buildSourceOptions,
  resolveTargetInput,
  targetDisplayText,
} from './identifierOptions';

function resolve(node: unknown, path: string): unknown {
  return path.split('.').reduce<unknown>((acc, part) => {
    if (acc === null || typeof acc !== 'object' || Array.isArray(acc)) return undefined;
    return (acc as Record<string, unknown>)[part];
  }, node);
}

function makeT(lng: 'zh' | 'en'): TFunction {
  return ((key: string, options?: { lng?: string; defaultValue?: string }) => {
    const active = options?.lng === 'zh' || options?.lng === 'en' ? options.lng : lng;
    const value = resolve(active === 'zh' ? platformZh : platformEn, key);
    return typeof value === 'string' ? value : (options?.defaultValue ?? key);
  }) as unknown as TFunction;
}

describe('趋势对象下拉', () => {
  it('value 保持原始标识，label 用能力释义', () => {
    const options = buildScopeObjectOptions(makeT('zh'), ['business.task_center.v1', 'deepseek-v4'], '全部对象');

    expect(options[0]).toEqual({ value: PM_ALL, label: '全部对象' });
    expect(options[1]).toEqual({ value: 'business.task_center.v1', label: '任务中心' });
    // 模型名不在能力表里，原样展示
    expect(options[2]).toEqual({ value: 'deepseek-v4', label: 'deepseek-v4' });
  });

  it('英文界面下 label 切英文，value 不变', () => {
    const zh = buildScopeObjectOptions(makeT('zh'), ['business.knowledge_base.v1'], '全部对象');
    const en = buildScopeObjectOptions(makeT('en'), ['business.knowledge_base.v1'], '全部对象');

    expect(zh[1]).toEqual({ value: 'business.knowledge_base.v1', label: '知识库' });
    expect(en[1]).toEqual({ value: 'business.knowledge_base.v1', label: 'Knowledge Base' });
  });
});

describe('目标对象选择器', () => {
  it('优先用 meta.callTargets；value 原始值、label 释义或原值', () => {
    const options = buildCallTargetOptions(makeT('zh'), {
      callTargets: ['business.task_center.v1', 'deepseek-v4-flash-202605', 'search_ontology'],
    });

    expect(options[0]).toEqual({ value: '', label: '全部对象' });
    expect(options[1]).toEqual({ value: 'business.task_center.v1', label: '任务中心' });
    expect(options[2]).toEqual({ value: 'deepseek-v4-flash-202605', label: 'deepseek-v4-flash-202605' });
    expect(options[3]).toEqual({ value: 'search_ontology', label: 'search_ontology' });
  });

  it('meta.callTargets 缺失时回退 13 条能力映射', () => {
    const options = buildCallTargetOptions(makeT('zh'), null);

    expect(options).toHaveLength(CAPABILITY_IDS.length + 1);
    expect(options.slice(1).every((item) => (CAPABILITY_IDS as readonly string[]).includes(item.value))).toBe(true);
  });

  it('回显文本按选项 label，未知值 / 空值回退原值 / 空串', () => {
    const options = buildCallTargetOptions(makeT('zh'), {
      callTargets: ['atomic.ontology_compile.v1', 'deepseek-v4-flash-202605'],
    });

    // 选中「本体编译」回显释义，而不是 atomic.ontology_compile.v1
    expect(targetDisplayText(options, 'atomic.ontology_compile.v1')).toBe('本体编译');
    // 模型名不在能力表里，label 与 value 同值
    expect(targetDisplayText(options, 'deepseek-v4-flash-202605')).toBe('deepseek-v4-flash-202605');
    // URL 带来的未知值原样回退，不吞成空
    expect(targetDisplayText(options, 'unknown-target')).toBe('unknown-target');
    expect(targetDisplayText(options, '')).toBe('');
    expect(targetDisplayText(options, null)).toBe('');
  });

  it('输入文本命中 label / value 时收敛为原始标识，未命中按自由输入原样下发', () => {
    const options = buildCallTargetOptions(makeT('zh'), {
      callTargets: ['atomic.ontology_compile.v1', 'search_ontology'],
    });

    // 点选项：AutoComplete 给的就是 value
    expect(resolveTargetInput(options, 'atomic.ontology_compile.v1')).toBe('atomic.ontology_compile.v1');
    // 手输展示名：还原成接口认的原始标识
    expect(resolveTargetInput(options, '本体编译')).toBe('atomic.ontology_compile.v1');
    // 自由输入（MCP 工具名等）原样下发，去首尾空白
    expect(resolveTargetInput(options, '  custom_tool  ')).toBe('custom_tool');
    // 清空 = 不筛
    expect(resolveTargetInput(options, '')).toBe('');
  });
});

describe('来源筛选选项', () => {
  it('优先用 call_source_options，value 原始 source、label 展示名', () => {
    const options = buildSourceOptions({
      callSourceOptions: [{ value: '1', label: 'admin' }],
      callSources: ['1'],
    });

    expect(options).toEqual([{ value: '1', label: 'admin' }]);
  });

  it('旧 call_sources 归一为 value/label 同值', () => {
    expect(buildSourceOptions({ callSources: ['task-center', 'apikey'] })).toEqual([
      { value: 'task-center', label: 'task-center' },
      { value: 'apikey', label: 'apikey' },
    ]);
  });

  it('两者都缺失时返回空数组（调用方回退自由输入）', () => {
    expect(buildSourceOptions(null)).toEqual([]);
  });
});
