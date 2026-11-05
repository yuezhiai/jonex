/**
 * 反馈来源判定与展示辅助。
 *
 * 后端列表项没有独立的「web/mcp」字段：`source` 存的是检索管线来源，不能用来
 * 区分入口；真正的判据是 `mcp_key_id`（Web 反馈为 null，MCP 反馈为快照 id）。
 * 这与后端 source 筛选的 SQL 语义（mcp_key_id IS [NOT] NULL）保持一致。
 */
import type { AnswerFeedbackListItem } from '@/types/knowledgeSearch';

export type FeedbackSource = 'web' | 'mcp';

/** 判定反馈来源：有 MCP Key 快照即 MCP，否则 Web */
export function getFeedbackSource(item: Pick<AnswerFeedbackListItem, 'mcp_key_id'>): FeedbackSource {
  return item.mcp_key_id ? 'mcp' : 'web';
}

/** MCP Key 展示名：Key 名称优先，缺失时回落 id（两者都空则返回空串） */
export function getMcpKeyLabel(
  item: Pick<AnswerFeedbackListItem, 'mcp_key_id' | 'mcp_key_name'>,
): string {
  return item.mcp_key_name || item.mcp_key_id || '';
}

/** 问答快照文本：MCP 反馈不保存快照时为 null，供调用方渲染占位符 */
export function getQuerySnapshot(item: Pick<AnswerFeedbackListItem, 'query'>): string | null {
  const query = item.query?.trim();
  return query ? query : null;
}
