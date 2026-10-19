/**
 * [jonex] 以图问知识：上传前的图片压缩。
 *
 * 设计：docs/knowledge-base/image-query-search-execution-plan.md §5-F2、§2-D5
 *
 * 为什么必须压缩：
 * - 后端单张上限 5MB、总量 12MB，且这几个数会经 Gateway 与能力服务两道判定；
 * - 识别要经 VLM，**图越大越慢**（B0 实测 1798KB 的图 5.3s，498KB 的图 3.9s）；
 * - 报文是 base64 进 invoke，体积再涨三分之一。
 *
 * ⚠️ **原图不出浏览器**：识别与落盘用的都是这份压缩产物，用户原图不会被上传。
 *
 * 几条有意为之的规则：
 * - **所有格式都重编码**（含 GIF）：动图只取首帧。检索只要静态内容，这样也顺手
 *   解决了「GIF 最容易撞 5MB 上限」的问题（动图压不动）。
 * - **带透明通道的 PNG/WebP 输出 WebP**，其余输出 JPEG：透明区在 JPEG 上会变黑，
 *   而黑底会污染 VLM 的描述与 OCR。
 * - 解码失败（文件损坏 / 浏览器不支持）抛 `ImageDecodeError`，由调用方提示
 *   「无法读取该图片」——**不静默跳过**，否则用户以为图附上了。
 *
 * ⚠️ **依赖注入是为了可测**：本仓 vitest 跑在 `environment: 'node'`（没有 DOM、
 * 没有 canvas、没有 createImageBitmap），所以浏览器 API 全部经 `deps` 注入，
 * 默认值才是真实实现。测试注入假件，不必上 jsdom。
 */

/** 长边上限（与后端 `IMAGE_QUERY_MAX_BYTES` 配合，把「压缩后」控制在 1MB 上下） */
export const DEFAULT_MAX_EDGE = 2048;

/** 重编码质量 */
export const DEFAULT_QUALITY = 0.85;

/** 解码失败：文件损坏或浏览器不认这个格式 */
export class ImageDecodeError extends Error {
  constructor(message = '无法读取该图片') {
    super(message);
    this.name = 'ImageDecodeError';
  }
}

/**
 * 允许的图片 MIME —— 与后端 `ALLOWED_IMAGE_MIME` 同源。
 *
 * ⚠️ 前端这一份**只用于提前提示**（粘贴/拖拽进来的非图片直接挡掉并告知），
 * **不是判据**：真正的判定在服务端按 magic bytes 做，不看声明的 Content-Type。
 * 两处不一致时的正确行为是「前端放行、后端拒绝」，而不是反过来。
 */
export const ACCEPTED_IMAGE_TYPES = [
  'image/jpeg',
  'image/png',
  'image/webp',
  'image/gif',
  'image/bmp',
] as const;

/** 声明类型是否在图片白名单内（只看声明，预览用；判定以服务端 magic bytes 为准）。 */
export function isAcceptedImage(file: Blob): boolean {
  const type = (file.type || '').split(';', 1)[0].trim().toLowerCase();
  return (ACCEPTED_IMAGE_TYPES as readonly string[]).includes(type);
}

/** canvas 的最小接口（只用到这几个，便于测试替身） */
export interface CanvasLike {
  width: number;
  height: number;
  getContext(type: '2d'): {
    drawImage(source: unknown, dx: number, dy: number, dw: number, dh: number): void;
    fillStyle: string;
    fillRect(x: number, y: number, w: number, h: number): void;
  } | null;
  toBlob(callback: (blob: Blob | null) => void, type?: string, quality?: number): void;
}

export interface DecodedImage {
  width: number;
  height: number;
  /** 能直接画进 canvas 的东西（ImageBitmap / HTMLImageElement） */
  source: unknown;
  /** ImageBitmap 需要显式释放 */
  close?: () => void;
}

export interface ImageCompressDeps {
  decode?: (file: Blob) => Promise<DecodedImage>;
  createCanvas?: (width: number, height: number) => CanvasLike;
}

/** 输入 MIME → 输出 MIME。带透明通道的走 WebP，其余走 JPEG。 */
export function pickOutputType(inputType: string | undefined | null): 'image/webp' | 'image/jpeg' {
  const type = (inputType ?? '').split(';', 1)[0].trim().toLowerCase();
  if (type === 'image/png' || type === 'image/webp') return 'image/webp';
  return 'image/jpeg';
}

/** 等比缩放到长边 ≤ maxEdge；本来就够小则原样返回（**不放大**）。 */
export function computeTargetSize(
  width: number,
  height: number,
  maxEdge: number = DEFAULT_MAX_EDGE,
): { width: number; height: number } {
  if (!width || !height) throw new ImageDecodeError('图片尺寸无效');
  const longest = Math.max(width, height);
  if (longest <= maxEdge) return { width: Math.round(width), height: Math.round(height) };
  const scale = maxEdge / longest;
  return {
    width: Math.max(1, Math.round(width * scale)),
    height: Math.max(1, Math.round(height * scale)),
  };
}

/** 真实解码实现：优先 `createImageBitmap`（快、能直接关掉释放），退化到 `<img>`。 */
async function defaultDecode(file: Blob): Promise<DecodedImage> {
  const w = globalThis as unknown as {
    createImageBitmap?: (blob: Blob) => Promise<{ width: number; height: number; close(): void }>;
    Image?: new () => HTMLImageElement;
    URL?: { createObjectURL(b: Blob): string; revokeObjectURL(u: string): void };
  };

  if (typeof w.createImageBitmap === 'function') {
    try {
      const bitmap = await w.createImageBitmap(file);
      return { width: bitmap.width, height: bitmap.height, source: bitmap, close: () => bitmap.close() };
    } catch {
      // ⚠️ **不要直接放弃**：部分浏览器/格式组合下 `createImageBitmap` 会抛
      // （历史上 Safari 对某些 GIF/WebP、以及损坏文件的处理与 Chrome 不同），
      // 而 `<img>` 解码往往仍能成功。这里退化到 `<img>` 再试一次，
      // 全都失败才按「无法读取该图片」处理。
    }
  }

  if (typeof w.Image === 'function' && w.URL?.createObjectURL) {
    const url = w.URL.createObjectURL(file);
    try {
      const image = await new Promise<HTMLImageElement>((resolve, reject) => {
        const el = new w.Image!();
        el.onload = () => resolve(el);
        el.onerror = () => reject(new ImageDecodeError());
        el.src = url;
      });
      return {
        width: image.naturalWidth || image.width,
        height: image.naturalHeight || image.height,
        source: image,
      };
    } finally {
      w.URL.revokeObjectURL(url);
    }
  }

  throw new ImageDecodeError();
}

function defaultCreateCanvas(width: number, height: number): CanvasLike {
  const doc = (globalThis as unknown as { document?: Document }).document;
  if (!doc) throw new ImageDecodeError('当前环境不支持图片压缩');
  const canvas = doc.createElement('canvas');
  canvas.width = width;
  canvas.height = height;
  return canvas as unknown as CanvasLike;
}

/**
 * 压缩一张图：等比缩到长边 ≤ maxEdge，重编码为 WebP（原图带透明度）或 JPEG。
 *
 * @throws ImageDecodeError 解码失败或环境不支持 canvas
 */
export async function compressImage(
  file: Blob,
  options: { maxEdge?: number; quality?: number } = {},
  deps: ImageCompressDeps = {},
): Promise<Blob> {
  const maxEdge = options.maxEdge ?? DEFAULT_MAX_EDGE;
  const quality = options.quality ?? DEFAULT_QUALITY;
  const decode = deps.decode ?? defaultDecode;
  const createCanvas = deps.createCanvas ?? defaultCreateCanvas;

  let decoded: DecodedImage;
  try {
    decoded = await decode(file);
  } catch (error) {
    if (error instanceof ImageDecodeError) throw error;
    throw new ImageDecodeError();
  }

  try {
    const { width, height } = computeTargetSize(decoded.width, decoded.height, maxEdge);
    const canvas = createCanvas(width, height);
    const ctx = canvas.getContext('2d');
    if (!ctx) throw new ImageDecodeError('当前环境不支持图片压缩');

    const outputType = pickOutputType(file.type);
    // ⚠️ JPEG **没有透明通道**，不铺底的话透明区会被渲染成**黑色** ——
    // 而黑底会污染 VLM 的描述与 OCR（比如透明背景的截图/图标一路识别成"黑色背景"）。
    // 输出 WebP 时**不铺**：那种情况下的透明是原图的透明，应当原样保留。
    if (outputType === 'image/jpeg') {
      ctx.fillStyle = '#ffffff';
      ctx.fillRect(0, 0, width, height);
    }
    ctx.drawImage(decoded.source, 0, 0, width, height);

    const blob = await new Promise<Blob | null>((resolve) => {
      canvas.toBlob((result) => resolve(result), outputType, quality);
    });
    if (!blob) throw new ImageDecodeError('图片压缩失败');

    // 尺寸没变、重编码却**更大** → 原图本来就是"压过的"，再编一遍只掉画质。
    // （缩放过的那种即使略大也要用重编码结果：尺寸本身是我们想要的。）
    if (blob.size >= file.size && width === decoded.width && height === decoded.height) {
      return file;
    }
    return blob;
  } finally {
    decoded.close?.();
  }
}

/** 可读的体积文案（错误提示里用） */
export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}
