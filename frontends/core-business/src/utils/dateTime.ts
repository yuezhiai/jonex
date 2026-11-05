/** 后端时间（UTC ISO，如 `2026-10-22T02:36:24+00:00`）→ 浏览器本地时区 `YYYY-MM-DD HH:mm:ss`。
 *
 * 不能直接 `iso.replace('T',' ').slice(0,19)`：后端 `timestamptz` 序列化成 UTC，
 * 直接切片就把 UTC 当本地时间显示（东八区会差 8 小时，真机：10:36 的 ETL 推送
 * 显示成 02:36）。空值回 `—`；解析失败回退「原串切片」，不吞掉原值。
 */
export function formatLocalDateTime(value?: string | null): string {
  if (!value) return '—';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return String(value).replace('T', ' ').slice(0, 19);
  }
  const pad = (n: number) => String(n).padStart(2, '0');
  return (
    `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ` +
    `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`
  );
}
