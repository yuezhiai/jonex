/**
 * 对象 / 来源选择器的候选项（展示层共用）。
 *
 * 统一约定：`value` 永远是接口认的原始值，`label` 才是给人看的释义。页面把控件里的
 * value 原样下发给筛选接口，不做「显示名 → 原值」的二次反查。
 */
import type { TFunction } from 'i18next';
import { PM_ALL } from '../../constants/platformMonitor';
import { CAPABILITY_IDS, capabilityLabel } from './capabilityLabels';

export interface PmSelectOption {
  value: string;
  label: string;
}

/**
 * 趋势卡的「全部对象 / 全部能力」+ 对象下拉。
 *
 * `names` 是原始对象标识；label 用能力释义，模型名 / MCP 工具名原样（C8）。
 */
export function buildScopeObjectOptions(
  t: TFunction,
  names: readonly string[],
  allLabel: string,
): PmSelectOption[] {
  return [
    { value: PM_ALL, label: allLabel },
    ...names.map((name) => ({ value: name, label: capabilityLabel(t, name) })),
  ];
}

/**
 * 目标对象选择器。
 *
 * 优先用元信息接口下发的 `callTargets`（对外对象标识 = `COALESCE(mcp_tool, target)`）；
 * 字段缺失或为空时回退到能力映射表，保证至少能按能力筛选。空字符串代表「全部」，
 * 与记录页「清空筛选 = 不筛」的既有语义一致。
 */
export function buildCallTargetOptions(
  t: TFunction,
  meta: { callTargets?: string[] } | null | undefined,
): PmSelectOption[] {
  const candidates = [...new Set((meta?.callTargets ?? []).filter(Boolean))];
  const values = candidates.length ? candidates : CAPABILITY_IDS;
  return [
    { value: '', label: t('platformMonitor.records.allObjects') },
    ...values.map((value) => ({ value, label: capabilityLabel(t, value) })),
  ];
}

/**
 * 目标对象选择器的回显文本：原始标识 → 选项 label。
 *
 * 输入框不能直接回显原始标识（选中「本体编译」后显示 `atomic.ontology_compile.v1`
 * 反直觉）；命中选项时显示释义，未命中（自由输入的模型名等）原样回退。
 * 空值返回空串，交给 placeholder「全部对象」。
 */
export function targetDisplayText(
  options: readonly PmSelectOption[],
  value: string | null | undefined,
): string {
  if (!value) return '';
  return options.find((option) => option.value === value)?.label ?? value;
}

/**
 * 选择器输入文本 → 下发给接口的原始值。
 *
 * 选中选项时 AutoComplete 给的是 option.value，直接可用；但用户也可能敲出
 * 选项的展示名，或自由输入模型名 / MCP 工具名。命中 label / value 时统一收敛成
 * 原始标识，未命中才原样下发（保持自由输入能力）。
 */
export function resolveTargetInput(options: readonly PmSelectOption[], text: string): string {
  const needle = text.trim();
  if (!needle) return '';
  return (
    options.find((option) => option.value === needle)?.value ??
    options.find((option) => option.label === needle)?.value ??
    needle
  );
}

/**
 * 来源筛选候选。
 *
 * 新契约 `call_source_options` 已带可读名（value=原始 source，label=账号名 / 认证类型）；
 * 旧后端只有 `call_sources` 时归一成 value/label 同值。两者都没有时返回空数组，
 * 页面回退成自由输入（而不是给一个空下拉）。
 */
export function buildSourceOptions(
  meta: { callSources?: string[]; callSourceOptions?: { value: string; label: string }[] } | null | undefined,
): PmSelectOption[] {
  if (meta?.callSourceOptions?.length) {
    // label 缺失时至少回退原始值，绝不渲染空选项
    return meta.callSourceOptions.map((item) => ({ value: item.value, label: item.label || item.value }));
  }
  return (meta?.callSources ?? []).map((source) => ({ value: source, label: source }));
}
