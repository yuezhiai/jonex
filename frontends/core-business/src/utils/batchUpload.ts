import {
  ACCEPT_EXTENSIONS,
  BATCH_UPLOAD_MAX_INFLIGHT_BYTES,
  BATCH_UPLOAD_MAX_PARALLEL,
  MAX_FILE_SIZE_BYTES,
} from '@/constants/upload';

/**
 * 批量上传的纯函数集中处（便于单测，见 batchUpload.test.ts）。
 * 组件内的 uploadOne / patchItem 依赖组件作用域（useRef / t / onSuccess），不在此列。
 */

export type FileUploadStatus =
  | 'pending'      // 排队中
  | 'uploading'    // 上传中
  | 'success'      // 上传成功（已提交异步解析）
  | 'error'        // 上传/入库失败
  | 'duplicate'    // 内容去重冲突（后端 code=1003）
  | 'skipped'      // 前置校验未通过（类型/大小/同批重复）
  | 'canceled';    // 用户关闭弹窗导致中断

export interface UploadItem {
  uid: string;          // 前端本地唯一 id（非后端 doc_id）
  file: File;
  status: FileUploadStatus;
  progress: number;     // 0–100，单文件上传字节进度
  error?: string;       // 失败原因（含去重/跳过提示）
}

export interface BatchUploadSummary {
  total: number;
  success: number;
  failed: number;       // error 计数（duplicate/skipped 不计入"失败"，单独展示）
  duplicate: number;
  skipped: number;
}

/** 前置校验的跳过原因（稳定 key，UI 层再翻译） */
export type SkipReason = 'fileEmpty' | 'fileTooLarge' | 'extNotAllowed' | 'dupInBatch';

/** ResourceConflictError 的业务码（通用"资源冲突"，非重复文档专属，作兜底用） */
export const BIZ_CODE_CONFLICT = 1003;

const ALLOWED_EXTS = new Set(
  ACCEPT_EXTENSIONS.split(',').map((s) => s.trim().toLowerCase()).filter(Boolean),
);

/**
 * 字节预算并发池（双闸门：并发个数上限 + 在途字节预算上限）。
 * - worker 内部必须自行吞掉异常（见 UploadModal 的 uploadOne）；此处额外 catch 兜底，
 *   避免 Promise.race 因单个失败中断整批。
 * - 当前无任务在途时，无论文件多大都放行一个，保证单个超大文件不被饿死。
 */
export async function runWithByteBudget(
  items: UploadItem[],
  worker: (item: UploadItem) => Promise<void>,
): Promise<void> {
  let cursor = 0;
  let inflightBytes = 0;
  const running = new Set<Promise<void>>();

  while (cursor < items.length || running.size > 0) {
    while (
      cursor < items.length &&
      running.size < BATCH_UPLOAD_MAX_PARALLEL &&
      (running.size === 0 ||
        inflightBytes + items[cursor].file.size <= BATCH_UPLOAD_MAX_INFLIGHT_BYTES)
    ) {
      const item = items[cursor++];
      const size = item.file.size;
      inflightBytes += size;
      const p = Promise.resolve()
        .then(() => worker(item))
        .catch(() => {})
        .finally(() => {
          inflightBytes -= size;
          running.delete(p);
        });
      running.add(p);
    }
    if (running.size > 0) {
      await Promise.race(running);
    }
  }
}

/** 轻量文件指纹（同批去重用，不适用于内容去重） */
export function fingerprint(f: File): string {
  return `${f.name}::${f.size}::${f.lastModified}`;
}

/** 总进度按字节加权；skipped 不进分母，duplicate 按满字节计入（字节已传完） */
export function computeTotalProgress(items: UploadItem[]): number {
  const counted = items.filter((it) => it.status !== 'skipped');
  const totalBytes = counted.reduce((s, it) => s + it.file.size, 0);
  if (!totalBytes) return 0;
  const loadedBytes = counted.reduce((s, it) => {
    if (it.status === 'success' || it.status === 'duplicate') return s + it.file.size;
    if (it.status === 'uploading') return s + (it.file.size * it.progress) / 100;
    return s;
  }, 0);
  return Math.round((loadedBytes / totalBytes) * 100);
}

/** 取文件名后缀（小写、含点）；无后缀 / 隐藏文件 / 末尾是点 都返回 null */
export function extOf(name: string): string | null {
  const i = name.lastIndexOf('.');
  if (i <= 0 || i === name.length - 1) return null;
  return name.slice(i).toLowerCase();
}

/** 判断扩展名是否在允许列表内 */
export function isExtAllowed(name: string): boolean {
  const ext = extOf(name);
  return ext != null && ALLOWED_EXTS.has(ext);
}

/** 前置校验：返回跳过原因 key 或 null（通过）。seen 为已入队指纹集合 */
export function validate(file: File, seen: Set<string>): SkipReason | null {
  if (file.size === 0) return 'fileEmpty';
  if (file.size > MAX_FILE_SIZE_BYTES) return 'fileTooLarge';
  if (!isExtAllowed(file.name)) return 'extNotAllowed';
  if (seen.has(fingerprint(file))) return 'dupInBatch';
  return null;
}

/**
 * 去重判定：优先看语义字段，bizCode 仅作兜底。
 * 后端在去重分支显式带 duplicate_document_id（error_details），经能力层透传
 * 一路到浏览器，是可靠信号；bizCode=1003 是通用冲突码，仅作兜底。
 */
export function isDuplicateError(err: unknown): boolean {
  const e = err as { errorDetails?: Record<string, unknown>; bizCode?: number } | undefined;
  if (e?.errorDetails?.duplicate_document_id) return true;
  return e?.bizCode === BIZ_CODE_CONFLICT;
}
