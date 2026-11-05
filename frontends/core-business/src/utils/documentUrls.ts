/** 文档原文 / 图片资产的**同源代理** URL 构造器。
 *
 * 背景：S3/MinIO 部署下，检索引用里的 `asset_url` / `raw_url` 是**预签名直链**，
 * 签的是对象存储的部署内地址（如 `http://minio:9000/...`）—— 浏览器解析不了，
 * 相关图片全部加载失败。`/documents/{id}/raw` 与 `/documents/{id}/assets/{idx}`
 * 都支持 `proxy=1`（网关在服务端取字节、同源返回），前端拿到短时 view-ticket 的
 * token 后改走这两个地址。
 */
const API_BASE = (import.meta as any).env?.VITE_API_BASE_URL || '/api/v1';

/** 文档原文（图片/音视频/PDF）同源代理 URL。 */
export function buildDocumentRawProxyUrl(docId: string, token: string): string {
  return `${API_BASE}/knowledge-base/documents/${encodeURIComponent(docId)}/raw`
    + `?token=${encodeURIComponent(token)}&proxy=1`;
}

/** 文档内嵌图片资产同源代理 URL（`ext` = 检索侧的 `asset_ext`，缺省不带）。 */
export function buildDocumentAssetProxyUrl(
  docId: string, token: string, imageIdx: number, ext?: string | null,
): string {
  const base = `${API_BASE}/knowledge-base/documents/${encodeURIComponent(docId)}`
    + `/assets/${imageIdx}?token=${encodeURIComponent(token)}&proxy=1`;
  return ext ? `${base}&ext=${encodeURIComponent(ext)}` : base;
}
