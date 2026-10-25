/**
 * 源调色板：按下标取色，超出后回绕。色值取编辑画布同族（蓝/绿/琥珀/紫/红/青）。
 * 多源视图（区域框/徽章）与抽象编辑器的节点来源徽章**共用这一份** —— 改色只改这里。
 */
export const SOURCE_COLORS = ['#2563eb', '#16a34a', '#d97706', '#9333ea', '#dc2626', '#0891b2'];

export const sourceColor = (index: number): string => SOURCE_COLORS[index % SOURCE_COLORS.length];
