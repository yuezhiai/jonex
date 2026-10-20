/**
 * 四个钻取抽屉共用的纯展示工具。
 *
 * 与 `fields.tsx`（只放组件）分开，是为了不触发 react-refresh 的
 * only-export-components —— 一个文件里既有组件又有普通函数会让热更新整块失效。
 */

/** 四个抽屉统一的空值占位 —— 与页面各表的 `-` 区分开：这里是「信息缺失」，不是「0」 */
export const DASH = '—';

/** 后端给的是字节数，展示统一走这里，免得四个抽屉各写一套换算 */
export function formatFileSize(bytes: number | null): string {
  if (bytes == null) return DASH;
  if (bytes >= 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  if (bytes >= 1024) return `${(bytes / 1024).toFixed(0)} KB`;
  return `${bytes} B`;
}

/**
 * 落库的**原始码** → 展示文案。
 *
 * 未知取值一律回落成原始码本身，不漏 i18n key：这几个状态列都不是数据库枚举
 * （如 `services.status` 可被改成任意串），写死一张表就会在遇到新值时显示成
 * `dataOperations.xxx.yyy` 这种原始 key。
 */
export function codeLabel(map: Record<string, string>, code: string | null): React.ReactNode {
  if (!code) return DASH;
  return map[code] ?? code;
}

/**
 * 码表里存的是 **i18n key** 而不是译文时的查询 —— 命中取 key 交给 `t()`，
 * 未知取值回落原始码，空值给 `DASH`。
 *
 * 与 `codeLabel` 分成两个函数，是因为两张表的内容确实不同：`statusLabels()` 产出的
 * 表里已经是译文（再 `t()` 一次就是二次翻译），而 API 层导出的常量表
 * （`KB_TYPE_KEYS` / `ASSET_SOURCE_KEYS`）只有 key —— 那里在模块作用域，拿不到 `t`。
 * 拿 `codeLabel` 读后一种表，界面上会直接出现 `dataOperations.sourceType.file`。
 */
export function translateCode(
  t: (key: string) => string,
  map: Record<string, string>,
  code: string | null,
): string {
  if (!code) return DASH;
  return t(map[code] ?? code);
}

/** 按 code 清单把 i18n 文案收成一张表 —— 三种处理状态各用一份，别在 JSX 里一个个 `t()` */
export function statusLabels(
  t: (key: string) => string,
  prefix: string,
  codes: readonly string[],
): Record<string, string> {
  return Object.fromEntries(codes.map((code) => [code, t(`${prefix}.${code}`)]));
}
