/**
 * 能力 ID 释义映射与显示名搜索展开。
 *
 * 这里钉住四件事：
 *  ① 13 条能力在 zh / en 两套语言包里都有词条，且叶子键完全一致；
 *  ② 能力 ID → 安全 i18n key（不含能力 ID 里的点）→ 当前语言释义，未知值回退原值；
 *  ③ keyword 按中英文释义 / 原始对象标识做大小写不敏感子串匹配；
 *  ④ 同一展示名挂多个协议时能识别出来（用于表格追加协议标签）。
 */
import { describe, expect, it } from 'vitest';
import i18next, { type TFunction } from 'i18next';
import platformZh from '../../locales/zh.json';
import platformEn from '../../locales/en.json';
import {
  CAPABILITY_IDS,
  CAPABILITY_I18N_KEY,
  capabilityLabel,
  duplicateProtocolsByLabel,
  expandKeywordTargets,
  matchCapabilityIds,
} from './capabilityLabels';

/** 按点路径取语言包里的叶子值（测试侧独立实现，避免与生产代码共用同一份错误） */
function resolve(node: unknown, path: string): unknown {
  return path.split('.').reduce<unknown>((acc, part) => {
    if (acc === null || typeof acc !== 'object' || Array.isArray(acc)) return undefined;
    return (acc as Record<string, unknown>)[part];
  }, node);
}

type TranslateOptions = { lng?: string; defaultValue?: string };

/**
 * 最小 TFunction 桩：`t(key, {lng})` 从对应语言包取值，取不到回退 defaultValue。
 * 语言切换就是换一个桩实例 —— 与 react-i18next 在 `languageChanged` 后重渲染同构。
 */
function makeT(defaultLng: 'zh' | 'en'): TFunction {
  return ((key: string, options?: TranslateOptions) => {
    const lng = options?.lng === 'en' ? 'en' : options?.lng === 'zh' ? 'zh' : defaultLng;
    const value = resolve(lng === 'zh' ? platformZh : platformEn, key);
    if (typeof value === 'string') return value;
    return options?.defaultValue ?? key;
  }) as unknown as TFunction;
}

describe('能力释义词条', () => {
  it('13 条能力在 zh / en 都有非空词条，且叶子键完全一致', () => {
    expect(CAPABILITY_IDS).toHaveLength(13);

    const zhLeaves = Object.keys(platformZh.platformMonitor.capability).sort();
    const enLeaves = Object.keys(platformEn.platformMonitor.capability).sort();
    expect(zhLeaves).toEqual(enLeaves);
    expect(zhLeaves).toHaveLength(13);

    for (const id of CAPABILITY_IDS) {
      const key = CAPABILITY_I18N_KEY[id];
      expect(key).toMatch(/^platformMonitor\.capability\.[a-z0-9_]+$/);

      for (const locale of [platformZh, platformEn]) {
        const label = resolve(locale, key);
        expect(typeof label).toBe('string');
        expect((label as string).trim()).not.toBe('');
      }
    }
  });

  it('中文界面显示中文短名，英文界面显示英文名', () => {
    expect(capabilityLabel(makeT('zh'), 'business.task_center.v1')).toBe('任务中心');
    expect(capabilityLabel(makeT('zh'), 'atomic.rag.lightrag.v1')).toBe('知识检索');

    expect(capabilityLabel(makeT('en'), 'business.task_center.v1')).toBe('Task Center');
    expect(capabilityLabel(makeT('en'), 'atomic.rag.lightrag.v1')).toBe('LightRAG Retrieval');
  });

  it('真实 i18next 实例下切换语言即时生效（安全 key 不被 keySeparator 拆坏）', async () => {
    const instance = i18next.createInstance();
    await instance.init({
      lng: 'zh',
      fallbackLng: 'en',
      resources: {
        zh: { translation: platformZh },
        en: { translation: platformEn },
      },
      interpolation: { escapeValue: false },
    });
    const t = instance.t.bind(instance);

    expect(capabilityLabel(t, 'business.task_center.v1')).toBe('任务中心');
    // 与 `switchLocale()` 一样只改语言，不重建实例 / 不刷新页面
    await instance.changeLanguage('en');
    expect(capabilityLabel(t, 'business.task_center.v1')).toBe('Task Center');
  });

  it('切换语言（换 TFunction 实例）后释义即时变化，无需刷新', () => {
    const id = 'business.business_domain.v1';
    const zhLabel = capabilityLabel(makeT('zh'), id);
    const enLabel = capabilityLabel(makeT('en'), id);

    expect(zhLabel).toBe('业务领域');
    expect(enLabel).toBe('Business Domain');
    expect(zhLabel).not.toBe(enLabel);
  });

  it('未知能力 / 模型名 / MCP 工具名回退原值，null 显示占位符', () => {
    const t = makeT('zh');
    expect(capabilityLabel(t, 'business.unknown_capability.v9')).toBe('business.unknown_capability.v9');
    expect(capabilityLabel(t, 'deepseek-v4-flash-202605')).toBe('deepseek-v4-flash-202605');
    expect(capabilityLabel(t, 'search_ontology')).toBe('search_ontology');
    expect(capabilityLabel(t, null)).toBe('-');
    expect(capabilityLabel(t, '')).toBe('-');
  });
});

describe('keyword 显示名展开', () => {
  it('中文释义子串命中能力 ID', () => {
    expect(matchCapabilityIds('任务中心', makeT('zh'))).toEqual(['business.task_center.v1']);
    // 子串匹配会同时命中「知识库」与「知识检索」，这正是显示名搜索要的「输入部分词也能出结果」
    expect(matchCapabilityIds('知识', makeT('zh'))).toEqual([
      'business.knowledge_base.v1',
      'atomic.rag.lightrag.v1',
    ]);
    expect(matchCapabilityIds('知识库', makeT('zh'))).toEqual(['business.knowledge_base.v1']);
    expect(matchCapabilityIds('本体', makeT('zh'))).toEqual(['atomic.ontology_compile.v1']);
  });

  it('英文释义大小写不敏感，且子串即可命中', () => {
    const t = makeT('zh');
    expect(matchCapabilityIds('task center', t)).toEqual(['business.task_center.v1']);
    expect(matchCapabilityIds('TASK', t)).toEqual(['business.task_center.v1']);
    expect(matchCapabilityIds('lightrag', t)).toEqual(['atomic.rag.lightrag.v1']);
    expect(matchCapabilityIds('embedding', t)).toEqual([]);
  });

  it('空关键字 / 未命中返回空数组', () => {
    expect(matchCapabilityIds('   ', makeT('zh'))).toEqual([]);
    expect(matchCapabilityIds('能力不存在xyz', makeT('zh'))).toEqual([]);
  });

  it('同时匹配能力释义与 meta.callTargets 原始值，并去重', () => {
    const t = makeT('zh');
    const callTargets = ['text-embedding-v3', 'deepseek-v4-flash-202605', 'business.task_center.v1'];

    expect(expandKeywordTargets('embedding', callTargets, t)).toEqual(['text-embedding-v3']);
    expect(expandKeywordTargets('DeepSeek', callTargets, t)).toEqual(['deepseek-v4-flash-202605']);
    // 能力释义命中的原始 ID 与 callTargets 里的同一条合并，不重复下发
    expect(expandKeywordTargets('任务中心', callTargets, t)).toEqual(['business.task_center.v1']);
  });

  it('callTargets 缺失时仍能按能力释义展开', () => {
    expect(expandKeywordTargets('语音识别', [], makeT('zh'))).toEqual(['atomic.audio.asr.v1']);
  });
});

describe('同名不同协议', () => {
  it('同一展示名挂多个协议时返回协议列表；单协议不返回', () => {
    const rows = [
      { name: 'business.knowledge_base.v1', protocol: 'invoke' as const },
      { name: 'business.knowledge_base.v1', protocol: 'mcp' as const },
      { name: 'deepseek-v4-flash-202605', protocol: 'openai' as const },
    ];

    const map = duplicateProtocolsByLabel(rows, makeT('zh'));

    expect(map.get('知识库')).toEqual(['invoke', 'mcp']);
    expect(map.has('deepseek-v4-flash-202605')).toBe(false);
  });

  it('空名不参与聚合', () => {
    const rows = [
      { name: null, protocol: 'invoke' as const },
      { name: null, protocol: 'mcp' as const },
    ];

    expect(duplicateProtocolsByLabel(rows, makeT('zh')).size).toBe(0);
  });
});
