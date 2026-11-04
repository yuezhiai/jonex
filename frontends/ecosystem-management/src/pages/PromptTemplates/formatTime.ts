import dayjs from 'dayjs';

/** 模板更新时间展示：解析失败时退回原字符串，避免把合法时间吞成 '—'。 */
export function formatUpdatedAt(value?: string | null): string {
  if (!value) return '—';
  const parsed = dayjs(value);
  return parsed.isValid() ? parsed.format('YYYY-MM-DD HH:mm') : value;
}
