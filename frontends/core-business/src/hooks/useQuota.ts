import { useCallback, useEffect, useState } from 'react';
import { getQuotaView, type QuotaItem } from '@/api/domainKnowledge';

/** MiB → Byte（后端 bytes 类配额以 Byte 下发，展示用除除数） */
export const MIB = 1024 * 1024;

/** 达限判断：used + reserved >= limit（一期 reserved=0，保留判断式兼容二期预占） */
export function isQuotaReached(item: QuotaItem | null | undefined): boolean {
  if (!item) return false;
  return item.used + item.reserved >= item.limit;
}

/** 文件类别 → 大小配额 key（对标后端 classify_quota_category 的扩展名兜底） */
const CATEGORY_BY_EXT: Record<string, string> = {
  pdf: 'document', doc: 'document', docx: 'document', ppt: 'document', pptx: 'document',
  txt: 'document', md: 'document', markdown: 'document',
  xls: 'spreadsheet', xlsx: 'spreadsheet', csv: 'spreadsheet', ods: 'spreadsheet',
  png: 'image', jpg: 'image', jpeg: 'image', gif: 'image', bmp: 'image',
  tiff: 'image', tif: 'image', webp: 'image',
  mp3: 'audio', wav: 'audio', flac: 'audio', aac: 'audio', m4a: 'audio', ogg: 'audio',
  wma: 'audio', opus: 'audio', amr: 'audio',
  mp4: 'video', avi: 'video', mov: 'video', mkv: 'video', flv: 'video', wmv: 'video',
  webm: 'video', m4v: 'video', mpg: 'video', mpeg: 'video', '3gp': 'video',
};

const SIZE_LIMIT_KEY_BY_CATEGORY: Record<string, string> = {
  document: 'documentFileSizeLimitMiB',
  spreadsheet: 'spreadsheetFileSizeLimitMiB',
  image: 'imageFileSizeLimitMiB',
  audio: 'audioFileSizeLimitMiB',
  video: 'videoFileSizeLimitMiB',
};

/** 按文件扩展名粗分类型（无后缀 / 未知扩展归入 document，仅用于大小预检提示） */
export function categoryOfFile(name: string): string {
  const i = name.lastIndexOf('.');
  if (i <= 0 || i === name.length - 1) return 'document';
  return CATEGORY_BY_EXT[name.slice(i + 1).toLowerCase()] ?? 'document';
}

/** 某文件的单文件大小上限（Byte）；配额未就绪或无对应项返回 null（不预检，交后端阻断） */
export function fileSizeLimitBytes(
  quotas: QuotaItem[] | null | undefined,
  name: string,
): number | null {
  if (!quotas) return null;
  const key = SIZE_LIMIT_KEY_BY_CATEGORY[categoryOfFile(name)];
  const item = quotas.find((q) => q.quotaKey === key);
  return item ? item.limit : null;
}

/** 格式化 Byte 为「xxx MB」（保留 0 位小数，向下取整展示） */
export function formatMiB(bytes: number): string {
  return `${Math.floor(bytes / MIB)}MB`;
}

export interface UseQuotaResult {
  /** 配额项数组；加载失败或未就绪为 null（前端不展示本地默认数量） */
  quotas: QuotaItem[] | null;
  loading: boolean;
  /** 配额加载失败（提示刷新重试，不做本地兜底） */
  error: boolean;
  reload: () => void;
  getQuota: (key: string) => QuotaItem | null;
  isReached: (key: string) => boolean;
}

/** 拉取租户配额视图；kbId 为空时 knowledgeBaseDocumentLimit.used=0（租户级）。 */
export function useQuota(kbId?: string): UseQuotaResult {
  const [quotas, setQuotas] = useState<QuotaItem[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(false);
  const [nonce, setNonce] = useState(0);

  useEffect(() => {
    let alive = true;
    setLoading(true);
    setError(false);
    getQuotaView(kbId)
      .then((res) => {
        if (!alive) return;
        setQuotas(res.quotas ?? []);
      })
      .catch(() => {
        if (!alive) return;
        setQuotas(null);
        setError(true);
      })
      .finally(() => {
        if (alive) setLoading(false);
      });
    return () => {
      alive = false;
    };
  }, [kbId, nonce]);

  const getQuota = useCallback(
    (key: string) => quotas?.find((q) => q.quotaKey === key) ?? null,
    [quotas],
  );
  const isReached = useCallback((key: string) => isQuotaReached(getQuota(key)), [getQuota]);
  const reload = useCallback(() => setNonce((n) => n + 1), []);

  return { quotas, loading, error, reload, getQuota, isReached };
}
