import { forwardRef, useImperativeHandle, useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Drawer, Descriptions, Tag, Space, Spin, Typography } from 'antd';
import { WRITE_KEY_PREFIX, type WriteKeyItem } from '@/api/mcpWriteKeys';
import { listKbFolders, type KnowledgeBaseBrief } from '@/api/spaces';
import './index.css';

/** 详情抽屉对外暴露的句柄 */
export type McpWriteKeyDetailDrawerHandle = {
  open: (record: WriteKeyItem) => void;
};

interface McpWriteKeyDetailDrawerProps {
  /** 知识库列表（名称映射） */
  kbList: KnowledgeBaseBrief[];
  /** 领域空间 ID → 名称 */
  spaceNameMap?: Record<string, string>;
}

/** 格式化时间：YYYY-MM-DD HH:mm */
const formatDate = (dateStr?: string | null): string => {
  if (!dateStr) return '-';
  const date = new Date(dateStr);
  if (Number.isNaN(date.getTime())) return '-';
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
};

/** 4 态状态 Tag（revoked 最优先，后端 status 已是派生结果） */
const renderStatus = (record: WriteKeyItem, t: (k: string) => string) => {
  switch (record.status) {
    case 'revoked':
      return <Tag color="error">{t('mcpWriteKeys.statusRevoked')}</Tag>;
    case 'expired':
      return <Tag color="orange">{t('mcpWriteKeys.statusExpired')}</Tag>;
    case 'disabled':
      return <Tag>{t('mcpWriteKeys.statusDisabled')}</Tag>;
    default:
      return <Tag color="success">{t('mcpWriteKeys.statusActive')}</Tag>;
  }
};

/** 知识写入 Key 详情抽屉 */
export const McpWriteKeyDetailDrawer = forwardRef<McpWriteKeyDetailDrawerHandle, McpWriteKeyDetailDrawerProps>(
  ({ kbList, spaceNameMap }, ref) => {
    const { t } = useTranslation();
    const [open, setOpen] = useState(false);
    const [record, setRecord] = useState<WriteKeyItem | null>(null);
    const [folderNameMap, setFolderNameMap] = useState<Record<string, string>>({});
    const [loadingFolders, setLoadingFolders] = useState(false);

    const kbNameMap = new Map<string, string>();
    kbList.forEach((k) => kbNameMap.set(k.id, k.name));

    useImperativeHandle(
      ref,
      () => ({
        open(rec) {
          setRecord(rec);
          setOpen(true);
          // 加载 grants 中 specified 模式 KB 的目录，用于目录名展示
          const kbIds = (rec.grants || [])
            .filter((g) => g.mode === 'specified' && g.kb)
            .map((g) => g.kb);
          setLoadingFolders(kbIds.length > 0);
          setFolderNameMap({});
          const all: Record<string, string> = {};
          Promise.all(
            kbIds.map((kb) =>
              listKbFolders(kb)
                .then((items) => {
                  items.forEach((f) => {
                    all[f.id] = f.name;
                  });
                })
                .catch(() => {
                  // 失败静默，目录名缺失时展示原始 id
                }),
            ),
          ).finally(() => {
            setFolderNameMap(all);
            setLoadingFolders(false);
          });
        },
      }),
      [],
    );

    return (
      <Drawer
        className="mcp-svc-detail"
        title={t('mcpWriteKeys.detailTitle')}
        placement="right"
        width={680}
        open={open}
        onClose={() => setOpen(false)}
      >
        {record && (
          <Spin spinning={loadingFolders}>
            <Descriptions column={2} size="small" colon={false} layout="vertical" bordered>
              <Descriptions.Item label={t('mcpWriteKeys.nameLabel')} span={2}>
                {record.name || '-'}
              </Descriptions.Item>
              <Descriptions.Item label={t('mcpWriteKeys.keyPrefix')}>
                {record.key_prefix ? (
                  <Typography.Text code>
                    {WRITE_KEY_PREFIX}
                    {record.key_prefix}
                  </Typography.Text>
                ) : (
                  '-'
                )}
              </Descriptions.Item>
              <Descriptions.Item label={t('mcpWriteKeys.status')}>
                {renderStatus(record, t)}
              </Descriptions.Item>
              <Descriptions.Item label={t('mcpWriteKeys.grantsLabel')} span={2}>
                {(record.grants || []).length === 0 ? (
                  <span style={{ color: '#94a3b8' }}>-</span>
                ) : (
                  <Space direction="vertical" size={8}>
                    {(record.grants || []).map((g, i) => {
                      const kbName = g.kb ? kbNameMap.get(g.kb) ?? g.kb : '-';
                      if (g.mode === 'all') {
                        return (
                          <div key={i}>
                            <Tag color="success">{t('mcpWriteKeys.modeAll')}</Tag>
                            <span>{kbName}</span>
                          </div>
                        );
                      }
                      const dirs = g.directories || [];
                      return (
                        <div key={i}>
                          <Tag color="processing">{t('mcpWriteKeys.modeSpecified')}</Tag>
                          <span>{kbName}</span>
                          <span style={{ color: '#94a3b8', marginLeft: 8 }}>
                            {t('mcpWriteKeys.directoryCount', { count: dirs.length })}
                          </span>
                          {dirs.length > 0 && (
                            <div style={{ marginTop: 4 }}>
                              <Space size={4} wrap>
                                {dirs.map((fid) => (
                                  <Tag key={fid} style={{ fontSize: 12 }}>
                                    {folderNameMap[fid] ?? fid}
                                  </Tag>
                                ))}
                              </Space>
                            </div>
                          )}
                        </div>
                      );
                    })}
                  </Space>
                )}
              </Descriptions.Item>
              <Descriptions.Item label={t('mcpWriteKeys.detailSpace')}>
                {record.space_id ? spaceNameMap?.[record.space_id] ?? record.space_id : '-'}
              </Descriptions.Item>
              <Descriptions.Item label={t('mcpWriteKeys.detailKb')}>
                {record.kb_id ? kbNameMap.get(record.kb_id) ?? record.kb_id : '-'}
              </Descriptions.Item>
              <Descriptions.Item label={t('mcpWriteKeys.detailCreatedAt')}>
                {formatDate(record.created_at)}
              </Descriptions.Item>
              <Descriptions.Item label={t('mcpWriteKeys.detailUpdatedAt')}>
                {formatDate(record.updated_at)}
              </Descriptions.Item>
              <Descriptions.Item label={t('mcpWriteKeys.expiryLabel')}>
                {record.expires_at ? formatDate(record.expires_at) : t('mcpWriteKeys.expiryForever')}
              </Descriptions.Item>
              <Descriptions.Item label={t('mcpWriteKeys.detailDisabledAt')}>
                {formatDate(record.disabled_at)}
              </Descriptions.Item>
              <Descriptions.Item label={t('mcpWriteKeys.detailRevokedAt')}>
                {formatDate(record.revoked_at)}
              </Descriptions.Item>
            </Descriptions>
          </Spin>
        )}
      </Drawer>
    );
  },
);
McpWriteKeyDetailDrawer.displayName = 'McpWriteKeyDetailDrawer';
