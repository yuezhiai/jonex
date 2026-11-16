/**
 * 单次检索 Token 用量（总量）。
 *
 * 产品口径：
 * - 只展示 `total_tokens` 总量，不展示估算值、不拆分输入/输出；
 * - **数据未就绪整块不渲染**：undefined / 0 / NaN / 负数都不渲染，
 *   不显示「统计中」「-」「0」，也不补显；
 * - 只有新发起的检索会带值；历史回放不带该字段，自然不展示。
 *
 * 展示形态：图标 + 短单位数字（`181.9k`）+ 单位小字，紧跟反馈按钮排在行左；
 * 不做浮窗，`aria-label` 给读屏的是千分位精确值（`本次消耗 181,935 Token`）。
 *
 * 拆成独立组件是为了让「不渲染」与「短单位换算」可被单测直接钉死，
 * 不必挂载整张 SessionCard（依赖大量 API / antd 子组件）。
 */
import { useTranslation } from 'react-i18next';
import { ThunderboltOutlined } from '@ant-design/icons';
import { formatExactTokenUsage, formatTokenUsage } from './tokenUsageFormat';

export function TokenUsage({ totalTokens }: { totalTokens?: number | null }) {
  const { t } = useTranslation();
  const formatted = formatTokenUsage(totalTokens);
  if (!formatted) return null;

  // 精确值只进 aria-label：屏幕上用短单位（181.9k），读屏拿到完整数字
  // count 传字符串：千分位已在外层完成；i18next 对字符串 count 不做复数查询
  const exact = formatExactTokenUsage(totalTokens) ?? formatted;
  const label = t('knowledgeSearch.tokenUsage', { count: exact });

  return (
    <span
      aria-label={label}
      style={{
        display: 'inline-flex',
        alignItems: 'center',
        gap: 4,
        height: 24,
        padding: '0 9px 0 7px',
        borderRadius: 999,
        background: 'var(--yx-bg-muted)',
        color: 'var(--yx-text-secondary)',
        fontSize: 12,
        fontWeight: 600,
        lineHeight: '24px',
        // 等宽数字：多次检索上下排列时位数对齐，不会因 1 比 8 窄而抖动
        fontVariantNumeric: 'tabular-nums',
        whiteSpace: 'nowrap',
      }}
    >
      <ThunderboltOutlined style={{ fontSize: 12, color: 'var(--yx-accent)' }} />
      {formatted}
      <span style={{ fontSize: 11, fontWeight: 400, color: 'var(--yx-text-muted)' }}>
        {t('knowledgeSearch.tokenUnit')}
      </span>
    </span>
  );
}

export default TokenUsage;
