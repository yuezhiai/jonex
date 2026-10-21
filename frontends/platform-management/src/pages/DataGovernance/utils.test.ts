import { describe, expect, it } from 'vitest';
import {
  actionErrorMessage,
  batchDraftError,
  buildBatchPayload,
  buildCategoryOptions,
  buildClassificationDefaultPatch,
  buildLevelOptions,
  buildMetadataPatch,
  buildRetentionPatch,
  clampPan,
  clampScale,
  classificationCodeError,
  classificationNameError,
  classificationOverrideDraft,
  classificationRankError,
  classificationSourceLabelKey,
  computeFitView,
  formatFileSize,
  formatRetentionValue,
  layoutLineage,
  levelColor,
  levelLabel,
  lineageEdgePath,
  LINEAGE_PAN_MARGIN,
  qualityColor,
  qualityLabelKey,
  retentionDaysLabelKey,
  retentionScopeDraft,
  retentionSourceLabelKey,
  retentionValueText,
  sensitivityLabelKey,
  statusColor,
  statusLabelKey,
  zoomAt,
} from './utils';
import { LINEAGE_LAYOUT } from './constant';
import type {
  DgAssetDetail,
  DgClassificationLevel,
  DgClassificationSource,
  DgLineage,
  DgRetentionPolicy,
} from './types';
import type { BatchDraft, MetadataDraft } from './utils';

describe('展示映射', () => {
  it('质量档位：null 是未评估，suspect 保留', () => {
    expect(qualityLabelKey('ok')).toBe('dataGovernance.quality.ok');
    expect(qualityLabelKey('suspect')).toBe('dataGovernance.quality.suspect');
    expect(qualityLabelKey('bad')).toBe('dataGovernance.quality.bad');
    expect(qualityLabelKey(null)).toBe('dataGovernance.quality.unknown');
  });
  it('密级 null 显示未分级', () => {
    expect(sensitivityLabelKey(null)).toBe('dataGovernance.sensitivity.unclassified');
    expect(sensitivityLabelKey('internal')).toBe('dataGovernance.sensitivity.internal');
  });
  it('状态按资产类型取词表，未知值原样返回', () => {
    expect(statusLabelKey('document', 'ready')).toBe('dataGovernance.status.document.ready');
    expect(statusLabelKey('document', 'archived')).toBe(
      'dataGovernance.status.document.archived',
    );
    expect(statusLabelKey('data_source', 'active')).toBe('dataGovernance.status.dataSource.active');
    expect(statusLabelKey('data_source', 'unknown-x')).toBe('unknown-x');
  });
  it('状态颜色按语义分组，未知值回落 default', () => {
    expect(statusColor('ready')).toBe('green');
    expect(statusColor('active')).toBe('green');
    expect(statusColor('failed')).toBe('red');
    expect(statusColor('error')).toBe('red');
    for (const status of ['parsing', 'ingesting', 'pending', 'deleting']) {
      expect(statusColor(status)).toBe('blue');
    }
    expect(statusColor('archived')).toBe('gold');
    expect(statusColor('deleted')).toBe('default');
    expect(statusColor('unknown-x')).toBe('default');
  });
  it('保留策略来源映射到 i18n key', () => {
    expect(retentionSourceLabelKey('document')).toBe(
      'dataGovernance.retention.source.document',
    );
    expect(retentionSourceLabelKey('kb')).toBe('dataGovernance.retention.source.kb');
    expect(retentionSourceLabelKey('tenant')).toBe('dataGovernance.retention.source.tenant');
    expect(retentionSourceLabelKey('system')).toBe('dataGovernance.retention.source.system');
  });
  it('保留天数 0 表示永不自动到期，其余带 count 参数', () => {
    expect(retentionDaysLabelKey(0)).toEqual({ key: 'dataGovernance.retention.never' });
    expect(retentionDaysLabelKey(30)).toEqual({
      key: 'dataGovernance.retention.days',
      params: { count: 30 },
    });
  });
});

const policy: DgRetentionPolicy = {
  scope: 'document',
  effectiveFrom: 'document',
  retentionDays: 30,
  graceDays: 7,
  sources: { retentionDays: 'document', graceDays: 'kb' },
};

describe('保留策略草稿', () => {
  it('formatRetentionValue：null/undefined 是未设置（继承），0 是不自动到期', () => {
    expect(formatRetentionValue(null)).toBeNull();
    expect(formatRetentionValue(undefined)).toBeNull();
    expect(formatRetentionValue(0)).toEqual({ key: 'dataGovernance.retention.never' });
    expect(formatRetentionValue(30)).toEqual({
      key: 'dataGovernance.retention.days',
      params: { count: 30 },
    });
  });

  it('retentionScopeDraft：只回填当前作用域自己的覆盖，其余留空继承', () => {
    expect(retentionScopeDraft(null, 'document')).toEqual({
      retentionDays: null,
      graceDays: null,
    });
    // 两个字段来源可能不同：retentionDays 是文档级、graceDays 是知识库级。
    expect(retentionScopeDraft(policy, 'document')).toEqual({
      retentionDays: 30,
      graceDays: null,
    });
    expect(retentionScopeDraft(policy, 'kb')).toEqual({
      retentionDays: null,
      graceDays: 7,
    });
    expect(retentionScopeDraft(policy, 'tenant')).toEqual({
      retentionDays: null,
      graceDays: null,
    });
  });

  it('buildRetentionPatch：只发变化字段，清空覆盖发显式 null', () => {
    expect(
      buildRetentionPatch(
        { retentionDays: 30, graceDays: null },
        { retentionDays: 30, graceDays: null },
      ),
    ).toEqual({});
    expect(
      buildRetentionPatch(
        { retentionDays: 30, graceDays: null },
        { retentionDays: null, graceDays: null },
      ),
    ).toEqual({ retention_days: null });
    expect(
      buildRetentionPatch(
        { retentionDays: null, graceDays: null },
        { retentionDays: 0, graceDays: 7 },
      ),
    ).toEqual({ retention_days: 0, grace_days: 7 });
  });

  it('retentionValueText：统一「继承 / 永不 / 天数」三种展示', () => {
    const t = ((key: string, options?: { count: number }) =>
      options?.count === undefined ? key : `${key}:${options.count}`) as unknown as Parameters<
      typeof retentionValueText
    >[0];
    expect(retentionValueText(t, null)).toBe('dataGovernance.lifecycle.inherit');
    expect(retentionValueText(t, 0)).toBe('dataGovernance.retention.never');
    expect(retentionValueText(t, 30)).toBe('dataGovernance.retention.days:30');
  });
});

describe('actionErrorMessage', () => {
  const options = { forbiddenKey: 'forbidden.key', fallbackKey: 'fallback.key' };

  it('403 与业务码 3006 统一走权限文案', () => {
    expect(actionErrorMessage({ status: 403, message: 'denied' }, options)).toEqual({
      key: 'forbidden.key',
    });
    expect(actionErrorMessage({ status: 400, bizCode: 3006 }, options)).toEqual({
      key: 'forbidden.key',
    });
  });

  it('拿到后端 message 时原样透出（不本地化污染英文界面）', () => {
    expect(actionErrorMessage({ status: 400, message: 'bad request' }, options)).toEqual({
      message: 'bad request',
    });
  });

  it('网络错误与未知形态回落通用文案', () => {
    expect(actionErrorMessage(new Error('network down'), options)).toEqual({
      key: 'fallback.key',
    });
    expect(actionErrorMessage(null, options)).toEqual({ key: 'fallback.key' });
    expect(actionErrorMessage('boom', options)).toEqual({ key: 'fallback.key' });
  });
});

describe('buildMetadataPatch', () => {
  const original = {
    owner: '张三',
    domain: '财务',
    sensitivity: 'internal' as const,
    categories: null,
  };

  it('没改返回空对象', () => {
    expect(buildMetadataPatch(original, { ...original })).toEqual({});
  });

  it('只发脏字段并 trim', () => {
    expect(buildMetadataPatch(original, { ...original, owner: ' 李四 ' })).toEqual({ owner: '李四' });
  });

  it('空串归一为 null', () => {
    expect(buildMetadataPatch(original, { ...original, domain: '   ' })).toEqual({ domain: null });
  });

  it('显式 null 清空密级', () => {
    expect(buildMetadataPatch(original, { ...original, sensitivity: null })).toEqual({ sensitivity: null });
  });

  it('原值带空白、草稿 trim 后相同则不发', () => {
    expect(
      buildMetadataPatch(
        { owner: ' 张三 ', domain: ' 财务 ', sensitivity: null, categories: null },
        { owner: '张三', domain: '财务', sensitivity: null, categories: null },
      ),
    ).toEqual({});
  });

  it('owner 与 domain 同时脏时都发送', () => {
    expect(
      buildMetadataPatch(original, {
        owner: ' 李四 ',
        domain: '法务',
        sensitivity: 'internal',
        categories: null,
      }),
    ).toEqual({ owner: '李四', domain: '法务' });
  });

  it('原值 null → 有值', () => {
    expect(
      buildMetadataPatch(
        { owner: null, domain: null, sensitivity: null, categories: null },
        { owner: '张三', domain: null, sensitivity: null, categories: null },
      ),
    ).toEqual({ owner: '张三' });
  });

  it('sensitivity 保持 null 不发', () => {
    expect(
      buildMetadataPatch(
        { owner: '张三', domain: '财务', sensitivity: null, categories: null },
        { owner: '张三', domain: '财务', sensitivity: null, categories: null },
      ),
    ).toEqual({});
  });
});

describe('buildMetadataPatch · categories', () => {
  const base = { owner: null, domain: null, sensitivity: null, categories: null };

  it('原 categories=null、新 [] 视为修改（空数组 ≠ 未设置）', () => {
    expect(
      buildMetadataPatch({ ...base, categories: null }, { ...base, categories: [] }),
    ).toEqual({ categories: [] });
  });

  it('原有值、新 null 表示清空', () => {
    expect(
      buildMetadataPatch({ ...base, categories: ['pii'] }, { ...base, categories: null }),
    ).toEqual({ categories: null });
  });

  it('相同数组不产生 categories 键（顺序也参与比较）', () => {
    expect(
      buildMetadataPatch({ ...base, categories: ['pii'] }, { ...base, categories: ['pii'] }),
    ).toEqual({});
    expect(
      buildMetadataPatch(
        { ...base, categories: ['pii', 'financial'] },
        { ...base, categories: ['financial', 'pii'] },
      ),
    ).toEqual({ categories: ['financial', 'pii'] });
  });

  it('草案未托管 categories（undefined）时即便原值非空也不发键', () => {
    // 类型上 categories 已必填；这里刻意绕过类型，验证运行时防御分支仍然有效
    // （旧调用方/半截数据不会误清空分类）。
    const unmanaged = { owner: null, domain: null, sensitivity: null } as unknown as MetadataDraft;
    expect(buildMetadataPatch({ ...base, categories: ['pii'] }, unmanaged)).toEqual({});
    expect(buildMetadataPatch({ ...base, categories: null }, unmanaged)).toEqual({});
  });
});

const batchAssets = [
  { assetType: 'document' as const, assetId: 'doc-1' },
  { assetType: 'data_source' as const, assetId: 'ds-1' },
];

describe('buildBatchPayload', () => {
  it('资产列表转 snake_case，keep/keep 时不发任何分类键', () => {
    const payload = buildBatchPayload(batchAssets, {
      levelMode: 'keep',
      level: null,
      categoriesMode: 'keep',
      categories: [],
    });

    expect(payload).toEqual({
      assets: [
        { asset_type: 'document', asset_id: 'doc-1' },
        { asset_type: 'data_source', asset_id: 'ds-1' },
      ],
    });
    expect('sensitivity' in payload).toBe(false);
    expect('categories' in payload).toBe(false);
  });

  it('inherit 两级都发显式 null（清除资产级覆盖）', () => {
    expect(
      buildBatchPayload(batchAssets, {
        levelMode: 'inherit',
        level: null,
        categoriesMode: 'inherit',
        categories: [],
      }),
    ).toEqual({
      assets: [
        { asset_type: 'document', asset_id: 'doc-1' },
        { asset_type: 'data_source', asset_id: 'ds-1' },
      ],
      sensitivity: null,
      categories: null,
    });
  });

  it('set 两级发送所选值', () => {
    expect(
      buildBatchPayload(batchAssets, {
        levelMode: 'set',
        level: 'confidential',
        categoriesMode: 'set',
        categories: ['pii'],
      }),
    ).toEqual({
      assets: [
        { asset_type: 'document', asset_id: 'doc-1' },
        { asset_type: 'data_source', asset_id: 'ds-1' },
      ],
      sensitivity: 'confidential',
      categories: ['pii'],
    });
  });

  it('categories=empty 发空数组，level 保持 keep', () => {
    const payload = buildBatchPayload(batchAssets, {
      levelMode: 'keep',
      level: 'internal',
      categoriesMode: 'empty',
      categories: ['pii'],
    });
    expect(payload.categories).toEqual([]);
    expect('sensitivity' in payload).toBe(false);
  });

  it('set 模式缺级别编码直接抛错（不能退化成 sensitivity:null 清除覆盖）', () => {
    expect(() =>
      buildBatchPayload(batchAssets, {
        levelMode: 'set',
        level: null,
        categoriesMode: 'keep',
        categories: [],
      }),
    ).toThrow();
    expect(() =>
      buildBatchPayload(batchAssets, {
        levelMode: 'set',
        level: '   ',
        categoriesMode: 'keep',
        categories: [],
      }),
    ).toThrow();
  });

  it('categories=set 且空数组时按「设为未分类」发 []（与 empty 等价）', () => {
    const payload = buildBatchPayload(batchAssets, {
      levelMode: 'keep',
      level: null,
      categoriesMode: 'set',
      categories: [],
    });
    expect(payload).toEqual({
      assets: [
        { asset_type: 'document', asset_id: 'doc-1' },
        { asset_type: 'data_source', asset_id: 'ds-1' },
      ],
      categories: [],
    });
  });
});

const detailBase: DgAssetDetail = {
  assetType: 'document',
  assetId: 'doc-1',
  name: 'a.pdf',
  kbId: 'kb-1',
  kbName: 'KB',
  spaceId: 'space-1',
  status: 'ready',
  updatedAt: '2026-10-01T00:00:00Z',
  owner: null,
  domain: null,
  sensitivity: 'confidential',
  classificationCategories: ['pii'],
  classificationSources: { sensitivity: 'asset', classificationCategories: 'asset' },
  qualityVerdict: 'ok',
  fileSize: 1024,
  mimeType: 'application/pdf',
  dataSourceType: null,
  folderId: null,
  contentGeneration: 1,
  ontologyStatus: null,
  accessType: null,
  syncMode: null,
  lastSyncAt: null,
  lastSyncStatus: null,
  documentCount: null,
  quality: { verdict: 'ok', reasons: [] },
  createdAt: '2026-10-01T00:00:00Z',
  archivedAt: null,
  retentionPolicy: null,
};

const detailWithSources = (
  sensitivity: DgClassificationSource,
  classificationCategories: DgClassificationSource,
): DgAssetDetail => ({
  ...detailBase,
  classificationSources: { sensitivity, classificationCategories },
});

describe('classificationOverrideDraft', () => {
  it('asset 来源：回填资产级覆盖值', () => {
    expect(classificationOverrideDraft(detailWithSources('asset', 'asset'))).toEqual({
      sensitivity: 'confidential',
      categories: ['pii'],
    });
  });

  it('tenant / kb / none 来源：两个字段都不算资产级覆盖（null）', () => {
    expect(classificationOverrideDraft(detailWithSources('tenant', 'tenant'))).toEqual({
      sensitivity: null,
      categories: null,
    });
    expect(classificationOverrideDraft(detailWithSources('kb', 'kb'))).toEqual({
      sensitivity: null,
      categories: null,
    });
    expect(classificationOverrideDraft(detailWithSources('none', 'none'))).toEqual({
      sensitivity: null,
      categories: null,
    });
  });

  it('两个来源可以不同，必须分别判断', () => {
    expect(classificationOverrideDraft(detailWithSources('tenant', 'asset'))).toEqual({
      sensitivity: null,
      categories: ['pii'],
    });
    expect(classificationOverrideDraft(detailWithSources('asset', 'kb'))).toEqual({
      sensitivity: 'confidential',
      categories: null,
    });
  });
});

describe('batchDraftError', () => {
  const keep: BatchDraft = {
    levelMode: 'keep',
    level: null,
    categoriesMode: 'keep',
    categories: [],
  };

  it('keep/keep 是空补丁（前后端都会拒绝），必须提示无变更', () => {
    expect(batchDraftError(keep)).toBe('dataGovernance.classification.noBatchChange');
  });

  it('inherit 两级都不需要字段值；任一级有变更即合法', () => {
    expect(
      batchDraftError({ ...keep, levelMode: 'inherit', categoriesMode: 'inherit' }),
    ).toBeNull();
    expect(
      batchDraftError({ ...keep, levelMode: 'inherit', categoriesMode: 'keep' }),
    ).toBeNull();
    expect(
      batchDraftError({ ...keep, levelMode: 'keep', categoriesMode: 'inherit' }),
    ).toBeNull();
  });

  it('set 等级为空（null / 空白）时要求选择等级', () => {
    expect(batchDraftError({ ...keep, levelMode: 'set', level: null })).toBe(
      'dataGovernance.classification.levelRequired',
    );
    expect(batchDraftError({ ...keep, levelMode: 'set', level: '   ' })).toBe(
      'dataGovernance.classification.levelRequired',
    );
  });

  it('set 等级非空时通过', () => {
    expect(batchDraftError({ ...keep, levelMode: 'set', level: 'confidential' })).toBeNull();
  });

  it('categories=set 空数组时要求选择分类；非空通过', () => {
    expect(
      batchDraftError({ ...keep, categoriesMode: 'set', categories: [] }),
    ).toBe('dataGovernance.classification.categoryRequired');
    expect(
      batchDraftError({ ...keep, categoriesMode: 'set', categories: ['pii'] }),
    ).toBeNull();
  });

  it('categories=empty 的显式空数组是合法值，不算缺失', () => {
    expect(
      batchDraftError({ ...keep, categoriesMode: 'empty', categories: [] }),
    ).toBeNull();
  });

  it('两级同时非法时优先返回等级错误', () => {
    expect(
      batchDraftError({
        levelMode: 'set',
        level: null,
        categoriesMode: 'set',
        categories: [],
      }),
    ).toBe('dataGovernance.classification.levelRequired');
  });
});

const levels: DgClassificationLevel[] = [
  { code: 'public', name: '公开', rank: 1, isActive: true },
  { code: 'internal', name: '内部', rank: 2, isActive: true },
  { code: 'confidential', name: '机密', rank: 3, isActive: true },
  { code: 'l4', name: '评审级', rank: 4, isActive: true },
];

describe('levelLabel / levelColor', () => {
  const t = (key: string) => `T:${key}`;

  it('内置级别且名称未改 → 走 i18n key', () => {
    expect(levelLabel(t, levels[0], 'public')).toBe('T:dataGovernance.sensitivity.public');
    expect(levelLabel(t, levels[2], 'confidential')).toBe(
      'T:dataGovernance.sensitivity.confidential',
    );
  });

  it('自定义级别 → 用后端名称；code 为空 → 未分级 key', () => {
    expect(levelLabel(t, levels[3], 'l4')).toBe('评审级');
    expect(levelLabel(t, undefined, null)).toBe(
      'T:dataGovernance.classification.unclassified',
    );
    expect(levelLabel(t, undefined, 'l9')).toBe('l9');
  });

  it('内置 code 但名称被改过 → 用后端名称（不猜 i18n 文案）', () => {
    expect(
      levelLabel(t, { code: 'public', name: '对外公开', rank: 1, isActive: true }, 'public'),
    ).toBe('对外公开');
  });

  it('配色：最高 rank red、中间 gold、最低 green、未分级/未知 default', () => {
    expect(levelColor('l4', levels)).toBe('red');
    expect(levelColor('internal', levels)).toBe('gold');
    expect(levelColor('public', levels)).toBe('green');
    expect(levelColor(null, levels)).toBe('default');
    expect(levelColor('unknown', levels)).toBe('default');
  });
});

describe('classificationSourceLabelKey', () => {
  it('四种来源各有 i18n key', () => {
    expect(classificationSourceLabelKey('asset')).toBe(
      'dataGovernance.classification.source.asset',
    );
    expect(classificationSourceLabelKey('kb')).toBe('dataGovernance.classification.source.kb');
    expect(classificationSourceLabelKey('tenant')).toBe(
      'dataGovernance.classification.source.tenant',
    );
    expect(classificationSourceLabelKey('none')).toBe(
      'dataGovernance.classification.source.none',
    );
  });
});

describe('分级选项 helper', () => {
  const t = (key: string) => `T:${key}`;
  const categories = [
    { code: 'pii', name: '个人信息', isActive: true },
    { code: 'financial', name: '财务', isActive: false },
  ];

  it('等级选项：内置级别走 i18n、停用项 disabled', () => {
    expect(buildLevelOptions(t, levels)).toEqual([
      { value: 'public', label: 'T:dataGovernance.sensitivity.public', disabled: false },
      { value: 'internal', label: 'T:dataGovernance.sensitivity.internal', disabled: false },
      {
        value: 'confidential',
        label: 'T:dataGovernance.sensitivity.confidential',
        disabled: false,
      },
      { value: 'l4', label: '评审级', disabled: false },
    ]);
    expect(
      buildLevelOptions(t, [{ code: 'l9', name: '停用级', rank: 9, isActive: false }]),
    ).toEqual([{ value: 'l9', label: '停用级', disabled: true }]);
  });

  it('筛选场景可让停用等级保持可选', () => {
    expect(
      buildLevelOptions(t, [{ code: 'l9', name: '停用级', rank: 9, isActive: false }], {
        disableInactive: false,
      }),
    ).toEqual([{ value: 'l9', label: '停用级', disabled: false }]);
  });

  it('分类选项：停用项带 (No) 后缀并禁用；筛选场景可选且不加后缀', () => {
    expect(buildCategoryOptions(categories, t)).toEqual([
      { value: 'pii', label: '个人信息', disabled: false },
      { value: 'financial', label: '财务 (T:common.no)', disabled: true },
    ]);
    expect(
      buildCategoryOptions(categories, t, { disableInactive: false }),
    ).toEqual([
      { value: 'pii', label: '个人信息', disabled: false },
      { value: 'financial', label: '财务', disabled: false },
    ]);
  });

  it('无翻译函数时不拼接停用后缀', () => {
    expect(buildCategoryOptions(categories)).toEqual([
      { value: 'pii', label: '个人信息', disabled: false },
      { value: 'financial', label: '财务', disabled: true },
    ]);
  });
});

describe('buildClassificationDefaultPatch', () => {
  const original = { levelCode: 'internal', categories: ['pii'] };

  it('无变更返回空对象', () => {
    expect(buildClassificationDefaultPatch(original, { ...original })).toEqual({});
    // 数组内容相同但引用不同，也视为无变更。
    expect(
      buildClassificationDefaultPatch(original, {
        levelCode: 'internal',
        categories: ['pii'],
      }),
    ).toEqual({});
  });

  it('只发生变化的键（某字段停用后只改另一个字段时不会带上未改值）', () => {
    expect(
      buildClassificationDefaultPatch(original, {
        levelCode: 'internal',
        categories: ['financial'],
      }),
    ).toEqual({ categories: ['financial'] });
    expect(
      buildClassificationDefaultPatch(original, {
        levelCode: 'confidential',
        categories: ['pii'],
      }),
    ).toEqual({ level_code: 'confidential' });
  });

  it('显式清空/无分类都按变化发送', () => {
    expect(
      buildClassificationDefaultPatch(original, { levelCode: null, categories: ['pii'] }),
    ).toEqual({ level_code: null });
    expect(
      buildClassificationDefaultPatch(original, { levelCode: 'internal', categories: [] }),
    ).toEqual({ categories: [] });
    expect(
      buildClassificationDefaultPatch(original, { levelCode: 'internal', categories: null }),
    ).toEqual({ categories: null });
  });
});

describe('词表表单校验', () => {
  it('编码：小写字母开头、2-32 位小写字母/数字/下划线', () => {
    expect(classificationCodeError('pii')).toBeNull();
    expect(classificationCodeError('l4')).toBeNull();
    expect(classificationCodeError('financial_level_2')).toBeNull();
    expect(classificationCodeError('a1')).toBeNull();
    expect(classificationCodeError('')).toBe(
      'dataGovernance.classification.codeInvalid',
    );
    expect(classificationCodeError('PII')).toBe(
      'dataGovernance.classification.codeInvalid',
    );
    expect(classificationCodeError('1pii')).toBe(
      'dataGovernance.classification.codeInvalid',
    );
    expect(classificationCodeError('pii-v2')).toBe(
      'dataGovernance.classification.codeInvalid',
    );
    expect(classificationCodeError('a')).toBe(
      'dataGovernance.classification.codeInvalid',
    );
    expect(classificationCodeError(`a${'b'.repeat(32)}`)).toBe(
      'dataGovernance.classification.codeInvalid',
    );
  });

  it('unclassified 是保留编码', () => {
    expect(classificationCodeError('unclassified')).toBe(
      'dataGovernance.classification.codeReserved',
    );
    expect(classificationCodeError(' unclassified ')).toBe(
      'dataGovernance.classification.codeReserved',
    );
  });

  it('名称：trim 后 1..64', () => {
    expect(classificationNameError('个人信息')).toBeNull();
    expect(classificationNameError('   ')).toBe(
      'dataGovernance.classification.nameRequired',
    );
    expect(classificationNameError(`名${'称'.repeat(64)}`)).toBe(
      'dataGovernance.classification.nameTooLong',
    );
    expect(classificationNameError('称'.repeat(64))).toBeNull();
  });

  it('等级序：1..1000 的整数', () => {
    expect(classificationRankError(1)).toBeNull();
    expect(classificationRankError(1000)).toBeNull();
    expect(classificationRankError(0)).toBe(
      'dataGovernance.classification.rankInvalid',
    );
    expect(classificationRankError(1001)).toBe(
      'dataGovernance.classification.rankInvalid',
    );
    expect(classificationRankError(1.5)).toBe(
      'dataGovernance.classification.rankInvalid',
    );
    expect(classificationRankError(null)).toBe(
      'dataGovernance.classification.rankInvalid',
    );
    expect(classificationRankError(undefined)).toBe(
      'dataGovernance.classification.rankInvalid',
    );
    expect(classificationRankError(Number.NaN)).toBe(
      'dataGovernance.classification.rankInvalid',
    );
  });
});

describe('qualityColor', () => {
  it('ok/suspect/bad/未评估各自配色', () => {
    expect(qualityColor('ok')).toBe('green');
    expect(qualityColor('suspect')).toBe('gold');
    expect(qualityColor('bad')).toBe('red');
    expect(qualityColor(null)).toBe('default');
  });
});

describe('formatFileSize', () => {
  it('null 显示占位符', () => {
    expect(formatFileSize(null)).toBe('—');
  });
  it('B 取整、KB 起保留 1 位小数', () => {
    expect(formatFileSize(0)).toBe('0 B');
    expect(formatFileSize(512)).toBe('512 B');
    expect(formatFileSize(1024)).toBe('1.0 KB');
    expect(formatFileSize(1536)).toBe('1.5 KB');
    expect(formatFileSize(1024 * 1024)).toBe('1.0 MB');
    expect(formatFileSize(5 * 1024 ** 3)).toBe('5.0 GB');
  });
});

const graph: DgLineage = {
  asset: { assetType: 'document', assetId: 'd-1', name: 'a.pdf' },
  nodes: [
    { id: 'document:d-1', type: 'document', label: 'a.pdf', meta: {} },
    { id: 'data_source:s-1', type: 'data_source', label: '文件上传', meta: {} },
    { id: 'entity:Alpha', type: 'ontology_entity', label: 'Alpha', meta: {} },
    { id: 'citation:d-1', type: 'search_citation', label: '检索引用', meta: {} },
    { id: 'chunk:c-1', type: 'chunk', label: 'c-1', meta: {} },
  ],
  edges: [
    { source: 'data_source:s-1', target: 'document:d-1', type: 'ingested_from', meta: {} },
    { source: 'document:d-1', target: 'entity:Alpha', type: 'produces_entity', meta: {} },
  ],
  counts: { chunks: 1, entities: 1, relations: 0, citations: 1 },
  truncated: { entities: false, relations: false, chunks: false },
};

describe('layoutLineage', () => {
  it('按来源/文档/下游/切片分列并给出画布尺寸', () => {
    const layout = layoutLineage(graph);
    expect(layout.positions['data_source:s-1'].column).toBe(0);
    expect(layout.positions['document:d-1'].column).toBe(1);
    expect(layout.positions['entity:Alpha'].column).toBe(2);
    expect(layout.positions['citation:d-1'].column).toBe(2);
    expect(layout.positions['chunk:c-1'].column).toBe(3);
    expect(layout.width).toBeGreaterThan(0);
    expect(layout.height).toBeGreaterThan(0);
  });
  it('超过上限折叠并返回 hiddenCount', () => {
    const many: DgLineage = {
      ...graph,
      nodes: Array.from({ length: 70 }, (_, i) => ({
        id: `entity:E${i}`, type: 'ontology_entity', label: `E${i}`, meta: {},
      })),
    };
    const layout = layoutLineage(many);
    expect(layout.visibleNodes.length).toBe(60);
    expect(layout.hiddenCount).toBe(10);
  });
  it('恰好 60 个节点不折叠', () => {
    const exactly: DgLineage = {
      ...graph,
      nodes: Array.from({ length: 60 }, (_, i) => ({
        id: `entity:E${i}`, type: 'ontology_entity', label: `E${i}`, meta: {},
      })),
    };
    const layout = layoutLineage(exactly);
    expect(layout.visibleNodes.length).toBe(60);
    expect(layout.hiddenCount).toBe(0);
  });
  it('文档节点前置，即使输入顺序靠后', () => {
    const [doc, ...rest] = graph.nodes;
    const layout = layoutLineage({ ...graph, nodes: [...rest, doc] });
    expect(layout.visibleNodes[0].id).toBe('document:d-1');
  });
  it('未知节点类型落在下游列（column=2）', () => {
    const layout = layoutLineage({
      ...graph,
      nodes: [{ id: 'future:x', type: 'future_kind', label: 'x', meta: {} }],
      edges: [],
    });
    expect(layout.positions['future:x'].column).toBe(2);
  });
  it('空图不抛错', () => {
    const layout = layoutLineage({ ...graph, nodes: [], edges: [] });
    expect(layout.visibleNodes).toEqual([]);
    expect(layout.hiddenCount).toBe(0);
  });
  it('nodes 为 null 时按空图处理', () => {
    const layout = layoutLineage({ ...graph, nodes: null } as unknown as DgLineage);
    expect(layout.visibleNodes).toEqual([]);
    expect(layout.hiddenCount).toBe(0);
  });
  it('列内垂直居中：单个文档与中间实体同 y', () => {
    const nodes = [
      { id: 'document:d-1', type: 'document', label: 'a.pdf', meta: {} },
      ...Array.from({ length: 3 }, (_, index) => ({
        id: `entity:E${index}`,
        type: 'ontology_entity',
        label: `E${index}`,
        meta: {},
      })),
    ];
    const layout = layoutLineage({ ...graph, nodes, edges: [] });
    // 实体列 3 个节点占满整列，文档列 1 个节点下移到中间行。
    expect(layout.positions['entity:E0'].y).toBe(LINEAGE_LAYOUT.top);
    expect(layout.positions['entity:E1'].y).toBe(LINEAGE_LAYOUT.top + LINEAGE_LAYOUT.nodeGap);
    expect(layout.positions['entity:E2'].y).toBe(
      LINEAGE_LAYOUT.top + 2 * LINEAGE_LAYOUT.nodeGap,
    );
    expect(layout.positions['document:d-1'].y).toBe(layout.positions['entity:E1'].y);
  });
  it('两列数量不同时各自围绕中心居中', () => {
    const nodes = [
      { id: 'data_source:s-1', type: 'data_source', label: 'src', meta: {} },
      ...Array.from({ length: 3 }, (_, index) => ({
        id: `entity:E${index}`,
        type: 'ontology_entity',
        label: `E${index}`,
        meta: {},
      })),
    ];
    const layout = layoutLineage({ ...graph, nodes, edges: [] });
    const offset = ((3 - 1) * LINEAGE_LAYOUT.nodeGap) / 2;
    expect(layout.positions['data_source:s-1'].y).toBe(LINEAGE_LAYOUT.top + offset);
    expect(layout.positions['data_source:s-1'].y).toBe(layout.positions['entity:E1'].y);
    expect(layout.positions['entity:E0'].y).toBe(LINEAGE_LAYOUT.top);
  });
  it('1 文档 + 2 实体：文档落在两实体之间且等于 top + gap/2', () => {
    const nodes = [
      { id: 'document:d-1', type: 'document', label: 'a.pdf', meta: {} },
      ...Array.from({ length: 2 }, (_, index) => ({
        id: `entity:E${index}`,
        type: 'ontology_entity',
        label: `E${index}`,
        meta: {},
      })),
    ];
    const layout = layoutLineage({ ...graph, nodes, edges: [] });
    const docY = layout.positions['document:d-1'].y;
    expect(docY).toBe(LINEAGE_LAYOUT.top + LINEAGE_LAYOUT.nodeGap / 2);
    expect(docY).toBeGreaterThan(layout.positions['entity:E0'].y);
    expect(docY).toBeLessThan(layout.positions['entity:E1'].y);
  });
  it('不变量：每列节点的平均 y 恒为 top + (maxRows - 1) * gap / 2', () => {
    const layout = layoutLineage(graph);
    const perColumn = new Map<number, number[]>();
    Object.values(layout.positions).forEach(({ column, y }) => {
      const bucket = perColumn.get(column) ?? [];
      bucket.push(y);
      perColumn.set(column, bucket);
    });
    const maxRows = Math.max(...Array.from(perColumn.values(), (ys) => ys.length));
    const center = LINEAGE_LAYOUT.top + ((maxRows - 1) * LINEAGE_LAYOUT.nodeGap) / 2;
    perColumn.forEach((ys) => {
      const average = ys.reduce((sum, y) => sum + y, 0) / ys.length;
      expect(average).toBe(center);
    });
  });
});

describe('computeFitView', () => {
  it('宽图按宽度比例缩放并居中', () => {
    const view = computeFitView({ width: 1000, height: 200 }, { width: 400, height: 420 });
    expect(view.scale).toBeCloseTo(0.352);
    expect(view.tx).toBeCloseTo(24);
    expect(view.ty).toBeCloseTo(174.8);
    // 布局四边都落在视口内（留 24px 内边距）。
    expect(view.tx).toBeGreaterThanOrEqual(24);
    expect(view.tx + 1000 * view.scale).toBeCloseTo(400 - 24);
    expect(view.ty).toBeGreaterThanOrEqual(24);
    expect(view.ty + 200 * view.scale).toBeLessThanOrEqual(420 - 24);
  });

  it('高图按高度比例缩放并居中', () => {
    const view = computeFitView({ width: 200, height: 1000 }, { width: 400, height: 420 });
    expect(view.scale).toBeCloseTo(0.372);
    expect(view.tx).toBeCloseTo(162.8);
    expect(view.ty).toBeCloseTo(24);
    expect(view.ty + 1000 * view.scale).toBeCloseTo(420 - 24);
  });

  it('小图不放大（scale 上限 1）', () => {
    const view = computeFitView({ width: 100, height: 50 }, { width: 400, height: 420 });
    expect(view.scale).toBe(1);
    expect(view.tx).toBe(150);
    expect(view.ty).toBe(185);
  });

  it('缩放范围夹取在 0.1 ~ 2', () => {
    const huge = computeFitView({ width: 100000, height: 100000 }, { width: 400, height: 420 });
    expect(huge.scale).toBe(0.1);
    const tinyViewport = computeFitView({ width: 100, height: 100 }, { width: 1, height: 1 });
    expect(tinyViewport.scale).toBe(0.1);
    const zeroLayout = computeFitView({ width: 0, height: 0 }, { width: 400, height: 420 });
    expect(zeroLayout.scale).toBe(1);
    expect(zeroLayout.tx).toBe(200);
    expect(zeroLayout.ty).toBe(210);
  });
});

describe('clampScale / zoomAt', () => {
  it('clampScale 夹取到 0.1 ~ 2', () => {
    expect(clampScale(0.01)).toBe(0.1);
    expect(clampScale(5)).toBe(2);
    expect(clampScale(1.25)).toBe(1.25);
  });

  it('同一屏幕锚点缩放前后世界坐标不变', () => {
    const view = { scale: 1.5, tx: 40, ty: -20 };
    const anchorX = 130;
    const anchorY = 90;
    const worldX = (anchorX - view.tx) / view.scale;
    const worldY = (anchorY - view.ty) / view.scale;
    for (const nextScale of [1.9, 1.2, 0.4]) {
      const zoomed = zoomAt(view, nextScale, anchorX, anchorY);
      expect((anchorX - zoomed.tx) / zoomed.scale).toBeCloseTo(worldX);
      expect((anchorY - zoomed.ty) / zoomed.scale).toBeCloseTo(worldY);
    }
  });

  it('已在上限/下限时连续缩放，tx/ty 不漂移', () => {
    let maxed = { scale: 2, tx: 123.4, ty: -56.7 };
    for (let index = 0; index < 10; index += 1) {
      maxed = zoomAt(maxed, maxed.scale * 2, 100, 100);
      expect(maxed.scale).toBe(2);
      expect(maxed.tx).toBeCloseTo(123.4);
      expect(maxed.ty).toBeCloseTo(-56.7);
    }

    let minned = { scale: 0.1, tx: -12.5, ty: 33.3 };
    for (let index = 0; index < 10; index += 1) {
      minned = zoomAt(minned, minned.scale * 0.5, 250, 180);
      expect(minned.scale).toBe(0.1);
      expect(minned.tx).toBeCloseTo(-12.5);
      expect(minned.ty).toBeCloseTo(33.3);
    }
  });
});

describe('clampPan', () => {
  const layout = { width: 800, height: 400 };
  const viewport = { width: 500, height: 420 };
  const view = { scale: 1, tx: 0, ty: 0 };

  it('拖动超出边界时至少保留 40px 图面在视口内', () => {
    const right = clampPan({ ...view, tx: 1000 }, layout, viewport);
    expect(right.tx).toBe(viewport.width - LINEAGE_PAN_MARGIN);
    expect(right.ty).toBe(0);

    const left = clampPan({ ...view, tx: -1000 }, layout, viewport);
    expect(left.tx).toBe(LINEAGE_PAN_MARGIN - layout.width);

    const up = clampPan({ ...view, ty: -1000 }, layout, viewport);
    expect(up.ty).toBe(LINEAGE_PAN_MARGIN - layout.height);

    const down = clampPan({ ...view, ty: 1000 }, layout, viewport);
    expect(down.ty).toBe(viewport.height - LINEAGE_PAN_MARGIN);
  });

  it('合法范围内的平移原样返回，scale 不变', () => {
    const inside = { scale: 1, tx: 60, ty: 80 };
    expect(clampPan(inside, layout, viewport)).toEqual(inside);
  });
});

describe('lineageEdgePath', () => {
  const layout = layoutLineage(graph);
  const edge = (source: string, target: string) => ({ source, target, type: 'x', meta: {} });

  it('相邻列走直线（源右缘 → 目标左缘）', () => {
    // 两列都只有 1 个节点，且都居中对齐（maxRows=2）。
    expect(lineageEdgePath(layout, edge('data_source:s-1', 'document:d-1'))).toBe(
      'M 240 85 L 320 85',
    );
  });
  it('同列走右侧贝塞尔弧线', () => {
    expect(lineageEdgePath(layout, edge('entity:Alpha', 'citation:d-1'))).toBe(
      'M 820 57 C 870 57, 870 113, 820 113',
    );
  });
  it('跨多列走顶部总线正交折线', () => {
    expect(lineageEdgePath(layout, edge('document:d-1', 'chunk:c-1'))).toBe(
      'M 420 68 L 420 20 L 1020 20 L 1020 68',
    );
  });
  it('端点缺失返回 null', () => {
    expect(lineageEdgePath(layout, edge('entity:missing', 'document:d-1'))).toBeNull();
    expect(lineageEdgePath(layout, edge('document:d-1', 'entity:missing'))).toBeNull();
  });
});
