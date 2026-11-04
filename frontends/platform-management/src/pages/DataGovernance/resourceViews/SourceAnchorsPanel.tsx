/**
 * W2.6 原文锚点视图：表/列资产 → active 对齐 → KB 本体实体 → 来源文档。
 *
 * 锚点口径是文档 + 实体名/属性，**不含页码**；上游失败（degraded）与
 * 无对齐（unresolved）都必须显式提示，不能伪装成「没有原文」。
 */
import { useCallback, useEffect, useRef, useState } from 'react';
import {
  Alert, Button, Card, Descriptions, Empty, List, Space, Spin, Tag,
} from 'antd';
import { ReloadOutlined } from '@ant-design/icons';
import { useTranslation } from 'react-i18next';

import { getSourceAnchors } from '@/api/resourceViews';
import { statusLabelKey } from '../utils';
import { anchorState } from '../resourceViewUtils';
import type { DgSourceAnchorLink, DgSourceAnchorResult } from '../types';
import {
  AssetPicker,
  EMPTY_ASSET_SELECTION,
  type AssetSelection,
} from './BindingDrawer';

function attributeText(value: unknown): string {
  if (value === null || value === undefined) return '—';
  if (typeof value === 'string') return value;
  if (typeof value === 'number' || typeof value === 'boolean') return String(value);
  try {
    return JSON.stringify(value);
  } catch {
    return '—';
  }
}

export default function SourceAnchorsPanel() {
  const { t } = useTranslation();
  const [selection, setSelection] = useState<AssetSelection>(EMPTY_ASSET_SELECTION);
  const [result, setResult] = useState<DgSourceAnchorResult | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const requestSeq = useRef(0);

  const load = useCallback(async () => {
    if (!selection.assetId || !selection.assetType) {
      requestSeq.current += 1;
      setResult(null);
      setError(null);
      setLoading(false);
      return;
    }
    const seq = ++requestSeq.current;
    setLoading(true);
    setError(null);
    try {
      const next = await getSourceAnchors(selection.assetType, selection.assetId);
      if (seq !== requestSeq.current) return;
      setResult(next);
    } catch (err) {
      if (seq !== requestSeq.current) return;
      setResult(null);
      setError(err instanceof Error && err.message
        ? err.message
        : t('dataGovernance.resourceViews.anchors.loadFailed'));
    } finally {
      if (seq === requestSeq.current) setLoading(false);
    }
  }, [selection.assetId, selection.assetType, t]);

  useEffect(() => {
    void load();
  }, [load]);

  const state = anchorState(result);

  const renderLink = (link: DgSourceAnchorLink) => {
    const attributeEntries = Object.entries(link.attributes ?? {});
    return (
      <Card
        key={`${link.knowledgeBaseId}:${link.docType}:${link.entityName}`}
        size="small"
        title={(
          <Space wrap>
            <span>{link.entityName || link.docType || '—'}</span>
            <Tag>{link.docType || '—'}</Tag>
            <Tag color="blue">
              {t('dataGovernance.resourceViews.anchors.entityCount', {
                count: link.entityCount,
              })}
            </Tag>
            {link.truncated ? (
              <Tag color="orange">{t('dataGovernance.resourceViews.anchors.truncated')}</Tag>
            ) : null}
          </Space>
        )}
        style={{ marginBottom: 12 }}
      >
        <Space direction="vertical" style={{ width: '100%' }} size="small">
          {attributeEntries.length ? (
            <Descriptions size="small" column={2} bordered>
              {attributeEntries.slice(0, 12).map(([key, value]) => (
                <Descriptions.Item key={key} label={key}>
                  {attributeText(value)}
                </Descriptions.Item>
              ))}
            </Descriptions>
          ) : (
            <span className="yx-dg-muted">
              {t('dataGovernance.resourceViews.anchors.noAttributes')}
            </span>
          )}
          {link.documents.length ? (
            <List
              size="small"
              bordered
              dataSource={link.documents}
              renderItem={(doc) => (
                <List.Item>
                  <Space wrap>
                    <span>{doc.name || doc.id}</span>
                    <Tag>{t(statusLabelKey('document', doc.status))}</Tag>
                  </Space>
                </List.Item>
              )}
            />
          ) : (
            <span className="yx-dg-muted">
              {t('dataGovernance.resourceViews.anchors.noDocuments')}
            </span>
          )}
        </Space>
      </Card>
    );
  };

  return (
    <div className="yx-dg-page">
      <Space direction="vertical" style={{ width: '100%' }} size="middle">
        <Space align="start" wrap>
          <div style={{ width: 520 }}>
            <AssetPicker value={selection} onChange={setSelection} />
          </div>
          <Button
            icon={<ReloadOutlined />}
            disabled={!selection.assetId}
            loading={loading}
            onClick={() => void load()}
          >
            {t('common.refresh')}
          </Button>
        </Space>

        <Alert
          type="info"
          showIcon
          message={t('dataGovernance.resourceViews.anchors.hint')}
        />

        {error ? (
          <Alert
            type="error"
            showIcon
            message={error}
            action={(
              <Button size="small" onClick={() => void load()}>
                {t('common.retry')}
              </Button>
            )}
          />
        ) : !selection.assetId ? (
          <Empty description={t('dataGovernance.resourceViews.anchors.needSelection')} />
        ) : loading ? (
          <div style={{ textAlign: 'center', padding: 32 }}>
            <Spin />
          </div>
        ) : result ? (
          <Space direction="vertical" style={{ width: '100%' }} size="middle">
            {result.degraded ? (
              <Alert
                type="error"
                showIcon
                message={t('dataGovernance.resourceViews.anchors.degraded')}
              />
            ) : null}
            {result.unresolved ? (
              <Alert
                type={state === 'degraded' ? 'warning' : 'info'}
                showIcon
                message={t('dataGovernance.resourceViews.anchors.unresolved')}
              />
            ) : null}
            {result.linked.length ? (
              <Space direction="vertical" style={{ width: '100%' }}>
                {result.linked.map(renderLink)}
              </Space>
            ) : (
              <Empty description={t('dataGovernance.resourceViews.anchors.empty')} />
            )}
          </Space>
        ) : null}
      </Space>
    </div>
  );
}
