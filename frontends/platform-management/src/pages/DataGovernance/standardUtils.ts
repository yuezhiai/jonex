/**
 * 数据治理第 5 期「标准目录」展示纯函数。
 *
 * 这里的判定只回答展示问题（是否不一致 / 是否未发布 / 文档统计如何分桶），
 * 不臆造后端未提供的语义：
 * - 只有**两侧都有值**时才可能「不一致」（任一侧缺失 = 无可比较对象）；
 * - `published_at` 为空即未发布（目录展示的是工作副本，不代表没有可看内容）；
 * - `docStatsParts` 只映射表格展示的四桶，`ontologyReady` 由调用方单独展示。
 */
import type { DgDocStats } from './standardTypes';
import { errorMeta } from './utils';

export function hasScenarioMismatch(row: {
  bindingScenarioId: string | null;
  compiledScenarioId: string | null;
}): boolean {
  return Boolean(
    row.bindingScenarioId &&
    row.compiledScenarioId &&
    row.bindingScenarioId !== row.compiledScenarioId,
  );
}

export function isUnpublished(value: string | null): boolean {
  return !value;
}

/** 切回「模板标准」Tab 时该做什么：重载详情 / 补取领域列表 / 不动。 */
export function templateEntryAction(args: {
  selectedDomainId: string | null;
  domainCount: number;
  domainsError: boolean;
}): 'detail' | 'domains' | 'none' {
  if (args.selectedDomainId) return 'detail';
  if (args.domainCount === 0 && !args.domainsError) return 'domains';
  return 'none';
}

/** 从 KB 行跳场景时，领域详情的加载方式（避免与 selectedDomainId effect 重复请求）。 */
export function kbEntryDomainAction(
  selectedDomainId: string | null | undefined,
  domainId: string | null | undefined,
): 'list' | 'explicit' | 'effect' {
  if (!domainId) return 'list';
  return selectedDomainId === domainId ? 'explicit' : 'effect';
}

/** 已有内容才静默刷新；初载/错误态走阻塞刷新，避免永久 Skeleton 或吞错。 */
export function scenarioRefreshMode(hasData: boolean): 'silent' | 'blocking' {
  return hasData ? 'silent' : 'blocking';
}

/** 静默刷新失败时，只有旧结果仍属于当前 scope 才能保留。 */
export function shouldKeepImpact(
  impact: { scopeType: string; scopeId: string } | null | undefined,
  scope: { type: string; id: string },
): boolean {
  return Boolean(
    impact &&
    impact.scopeType === scope.type &&
    impact.scopeId === scope.id,
  );
}

/** 改每页条数时回到第 1 页；只翻页则用目标页。 */
export function pageAfterSizeChange(
  currentSize: number,
  nextPage: number,
  nextSize: number,
): number {
  return nextSize !== currentSize ? 1 : nextPage;
}

/** 名称优先；名称为空/全空白时回退 id；两者皆无才占位。 */
export function displayNameOrId(
  name: string | null | undefined,
  id: string | null | undefined,
): string {
  const trimmedName = name?.trim();
  if (trimmedName) return trimmedName;
  const trimmedId = id?.trim();
  return trimmedId || '—';
}

export function docStatsParts(stats: DgDocStats) {
  return {
    total: stats.docTotal,
    noTarget: stats.noTarget,
    notApplied: stats.notApplied,
    appliedCurrent: stats.appliedCurrent,
  };
}

/**
 * 标准目录统一的错误文案（纯函数）：403/404 走本地化专用文案，
 * 其余有后端 message 才透出（英文界面不串中文），否则回落调用方给的通用 key。
 *
 * `translate` 由调用方传入（通常是 react-i18next 的 `t`），便于单测与复用。
 */
export function loadErrorText(
  err: unknown,
  fallbackKey: string,
  translate: (key: string) => string,
): string {
  const meta = errorMeta(err);
  if (meta.status === 403) return translate('dataGovernance.standards.errorForbidden');
  if (meta.status === 404) return translate('dataGovernance.standards.errorNotFound');
  return meta.status !== undefined && meta.message
    ? meta.message
    : translate(fallbackKey);
}
