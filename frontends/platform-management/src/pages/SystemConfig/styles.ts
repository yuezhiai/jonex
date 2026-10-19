/**
 * 系统配置页两个作用域（平台设置 / 租户设置）**共用**的样式常量。
 *
 * 为什么单独一个文件：这些值原先在 `index.tsx` 与 `TenantSecuritySection.tsx` 里各写一份，
 * 于是同一页里出现「两种卡片内边距 / 两种标题分隔线 / 两种只读提示色」只是时间问题。
 * 抽到这里，两边改一次同时生效。
 *
 * ⚠ 只抽**取值相同**的样式。保存行的位置与文案两侧是**故意不同**的（平台侧跨两张卡片，
 * 操作行放卡片外；租户侧一张卡片，操作行放卡片内），不要为了"看起来一致"再合并它们。
 */

/** 卡片外框：两处卡片必须长得一样 */
export const CARD_STYLE = { borderRadius: 12, border: '1px solid #e2e8f0', marginBottom: 20 } as const;

export const CARD_BODY_STYLE = { padding: 24 } as const;

/** 卡片标题（h3）：带分隔线，右侧可放刷新按钮（`marginLeft: 'auto'`） */
export const HEADING_STYLE = {
  margin: '0 0 16px',
  fontSize: 16,
  display: 'flex',
  alignItems: 'center',
  gap: 8,
  paddingBottom: 12,
  borderBottom: '1px solid #e2e8f0',
} as const;

/** 作用域说明（灰色）；与下面的只读提示区分——一个是「这是什么」，一个是「你能改吗」 */
export const SUBTITLE_STYLE = { margin: '0 0 16px', color: '#64748b', fontSize: 13 } as const;

export const READONLY_HINT_STYLE = { margin: '0 0 16px', color: '#f59e0b', fontSize: 13 } as const;

export const CENTERED_STYLE = {
  display: 'flex',
  justifyContent: 'center',
  alignItems: 'center',
  minHeight: 180,
} as const;

export const GRID_STYLE = { display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16 } as const;

/** 同一卡片里的第二行字段（第一行不留上边距，紧跟标题） */
export const GRID_TOP_STYLE = { ...GRID_STYLE, marginTop: 14 } as const;

/** 已修改但未保存的字段高亮 */
export const CHANGED_FIELD_STYLE = {
  borderColor: '#f59e0b',
  boxShadow: '0 0 0 2px rgba(245,158,11,0.15)',
} as const;
