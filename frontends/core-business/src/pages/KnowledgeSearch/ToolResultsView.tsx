import React from 'react';
import { ApiOutlined } from '@ant-design/icons';
import { Tag, Tooltip } from 'antd';
import { useTranslation } from 'react-i18next';
import type { ToolCallResult } from '@/types/knowledgeSearch';

const NS = 'knowledgeSearch.toolResults';

const STATUS_COLOR: Record<string, string> = {
  executed: 'green',
  failed: 'red',
  needs_confirmation: 'gold',
  not_allowed: 'orange',
  not_callable: 'default',
  rate_limited: 'orange',
  invalid_input: 'gold',
  no_runtime: 'default',
  tool_unavailable: 'default',
};

/** 实时数据块：本次问答调用了动作绑定的外部工具时，列出调了哪个工具、何时、结果状态。
 *
 * 工具结果没有文档出处，不进引用卡片；这里单独说明答案里有一部分来自实时接口。
 * 无调用（总闸关 / 未调用 / 旧后端）时不渲染。
 */
export default function ToolResultsView({ results }: { results?: ToolCallResult[] | null }) {
  const { t } = useTranslation();
  if (!results || results.length === 0) return null;
  return (
    <div
      style={{
        background: '#f8fafc',
        borderLeft: '3px solid #14b8a6',
        borderRadius: '0 8px 8px 0',
        padding: '10px 14px',
        marginTop: 12,
      }}
    >
      <div style={{ fontSize: 13, fontWeight: 600, color: '#0f766e', marginBottom: 6 }}>
        <ApiOutlined /> {t(`${NS}.title`)}
      </div>
      <div style={{ fontSize: 12, color: '#64748b', marginBottom: 6 }}>{t(`${NS}.hint`)}</div>
      <ul style={{ margin: 0, paddingLeft: 16 }}>
        {results.map((r) => {
          const status = r.status || 'failed';
          const label = t(`${NS}.status.${status}`, { defaultValue: status });
          const time = r.called_at ? new Date(r.called_at).toLocaleTimeString() : '';
          return (
            <li key={r.seq} style={{ fontSize: 13, color: '#334155', lineHeight: 1.9 }}>
              <span>{r.action}</span>
              <span style={{ color: '#94a3b8' }}> → </span>
              <span style={{ fontFamily: 'monospace' }}>{r.tool_label || r.tool_name || '—'}</span>
              <Tag color={STATUS_COLOR[status] || 'default'} style={{ marginLeft: 8 }}>
                {label}
              </Tag>
              {r.http_status != null && (
                <Tooltip title={t(`${NS}.httpStatus`)}>
                  <span style={{ fontSize: 12, color: '#64748b' }}>HTTP {r.http_status}</span>
                </Tooltip>
              )}
              {time && <span style={{ marginLeft: 8, fontSize: 12, color: '#94a3b8' }}>{time}</span>}
            </li>
          );
        })}
      </ul>
    </div>
  );
}
