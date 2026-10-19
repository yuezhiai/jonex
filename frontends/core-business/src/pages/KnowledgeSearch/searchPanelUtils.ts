/**
 * 提交判据：有文字**或**有图片即可检索。
 *
 * 「仅图片无文字」必须放行 —— 后端以图问知识链路本来就支持只传图片，
 * 这里若按文本判空会把那条路径堵死。
 */
export function canSubmitSearch(query: string, imageCount: number): boolean {
  return query.trim().length > 0 || imageCount > 0;
}
