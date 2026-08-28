/**
 * 批量触发文档下载：逐个拉取下载 URL，用隐藏 <a> 触发浏览器下载。
 * 相邻两次触发间隔 delayMs（默认 300ms），降低浏览器多下载拦截概率。
 */

export interface BatchDownloadOptions {
  /** 拉取单个文档下载 URL（返回 /raw?token=... 形式） */
  fetchTicket: (docId: string) => Promise<{ url: string }>;
  /** 完成进度回调（done: 已处理数, total: 总数） */
  onProgress?: (done: number, total: number) => void;
  /** 触发间隔（毫秒），默认 300；测试传 0 跳过等待 */
  delayMs?: number;
  /** 创建下载链接，默认 document.createElement('a')；测试注入用 */
  createLink?: (url: string) => { click: () => void };
}

export interface BatchDownloadResult {
  ok: number;
  failed: number;
}

const sleep = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms));

function defaultCreateLink(url: string): { click: () => void } {
  const a = document.createElement('a');
  a.href = url;
  a.style.display = 'none';
  document.body.appendChild(a);
  return {
    click: () => {
      a.click();
      a.remove();
    },
  };
}

export async function batchDownloadDocuments(
  docIds: string[],
  options: BatchDownloadOptions,
): Promise<BatchDownloadResult> {
  const { fetchTicket, onProgress, delayMs = 300, createLink } = options;
  let ok = 0;
  let failed = 0;
  const total = docIds.length;
  for (let i = 0; i < docIds.length; i += 1) {
    try {
      const { url } = await fetchTicket(docIds[i]);
      const link = createLink ? createLink(`${url}&download=1`) : defaultCreateLink(`${url}&download=1`);
      link.click();
      ok += 1;
    } catch {
      failed += 1;
    }
    onProgress?.(i + 1, total);
    if (delayMs > 0 && i < docIds.length - 1) {
      await sleep(delayMs);
    }
  }
  return { ok, failed };
}
