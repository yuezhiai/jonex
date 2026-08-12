import React, { useState, useImperativeHandle, forwardRef } from 'react';
import { Drawer, Descriptions, Tag, Typography, Spin, Space } from 'antd';
import { useTranslation } from 'react-i18next';
import type { McpKeyItem } from '@/api/mcpKeys';
import { getMcpKeyDetail } from '@/api/mcpKeys';

/** 详情抽屉对外暴露的句柄 */
export type KeyDetailDrawerHandle = {
  /** 打开抽屉并渲染指定 Key 的详情 */
  open: (record: McpKeyItem) => void;
};

interface KeyDetailDrawerProps {
  /** 抽屉打开/关闭状态变化的回调（可选） */
  onOpenChange?: (open: boolean) => void;
}

const KeyDetailDrawer = forwardRef<KeyDetailDrawerHandle, KeyDetailDrawerProps>(({ onOpenChange }, ref) => {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const [record, setRecord] = useState<McpKeyItem | null>(null);
  const [loading, setLoading] = useState(false);

  // 对外暴露打开方法：先用列表数据立即渲染，再异步拉取最新详情覆盖
  useImperativeHandle(
    ref,
    () => ({
      open(rec: McpKeyItem) {
        setRecord(rec);
        setLoading(true);
        setOpen(true);
        onOpenChange?.(true);
        getMcpKeyDetail(rec.id)
          .then((detail) => setRecord(detail))
          .catch(() => {
            // 拉取详情失败时保留列表已有数据，不阻断展示
          })
          .finally(() => setLoading(false));
      },
    }),
    [onOpenChange],
  );

  // 关闭抽屉
  const handleClose = () => {
    setOpen(false);
    onOpenChange?.(false);
  };

  // 格式化时间：YYYY-MM-DD HH:mm
  const formatDate = (dateStr?: string | null): string => {
    if (!dateStr) return '-';
    const date = new Date(dateStr);
    if (Number.isNaN(date.getTime())) return '-';
    const pad = (n: number) => String(n).padStart(2, '0');
    return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
  };

  // 权限渲染：view=金色，call=蓝色，write=绿色（多分支用 switch 提升可读性）
  const renderPermissions = (permissions: string[]) => {
    if (!permissions || permissions.length === 0) return <span className="mcp-key-muted">-</span>;
    return (
      <Space size={4} wrap>
        {permissions.map((p) => {
          let tag: React.ReactNode;
          switch (p) {
            case 'view':
              tag = <Tag color="gold">{t('mcpKeyManagement.permissionView')}</Tag>;
              break;
            case 'call':
              tag = <Tag color="blue">{t('mcpKeyManagement.permissionCall')}</Tag>;
              break;
            default:
              tag = <Tag color="green">{t('mcpKeyManagement.permissionWrite')}</Tag>;
          }
          return <span key={p}>{tag}</span>;
        })}
      </Space>
    );
  };

  // 知识库范围渲染：空数组=全部知识库，有值=逐个 Tag
  const renderKbRange = (ids: string[]) => {
    if (!ids || ids.length === 0) {
      return <Tag>{t('mcpKeyManagement.allKb')}</Tag>;
    }
    return (
      <Space size={4} wrap>
        {ids.map((id) => (
          <Tag key={id}>{id}</Tag>
        ))}
      </Space>
    );
  };

  return (
    <Drawer
      title={t('mcpKeyManagement.detailTitle')}
      placement="right"
      width={480}
      open={open}
      onClose={handleClose}
    >
      {/* 加载期间在抽屉内显示 Spin */}
      <Spin spinning={loading}>
        {record && (
          <Descriptions column={1} bordered size="small">
            <Descriptions.Item label={t('mcpKeyManagement.detailLabelName')}>
              {record.name || '-'}
            </Descriptions.Item>
            <Descriptions.Item label={t('mcpKeyManagement.detailLabelPrefix')}>
              {record.key_prefix ? <Typography.Text code>yxm_{record.key_prefix}</Typography.Text> : '-'}
            </Descriptions.Item>
            <Descriptions.Item label={t('mcpKeyManagement.detailLabelPermissions')}>
              {renderPermissions(record.permissions)}
            </Descriptions.Item>
            <Descriptions.Item label={t('mcpKeyManagement.detailLabelKbRange')}>
              {renderKbRange(record.allowed_kb_ids || [])}
            </Descriptions.Item>
            <Descriptions.Item label={t('mcpKeyManagement.detailLabelCreatedBy')}>
              {record.created_by || '-'}
            </Descriptions.Item>
            <Descriptions.Item label={t('mcpKeyManagement.detailLabelCreatedAt')}>
              {formatDate(record.created_at)}
            </Descriptions.Item>
            <Descriptions.Item label={t('mcpKeyManagement.detailLabelStatus')}>
              {record.revoked_at ? (
                <Tag color="error">{t('mcpKeyManagement.revokedStatus')}</Tag>
              ) : (
                <Tag color="success">{t('mcpKeyManagement.activeStatus')}</Tag>
              )}
            </Descriptions.Item>
          </Descriptions>
        )}
      </Spin>
    </Drawer>
  );
});

KeyDetailDrawer.displayName = 'KeyDetailDrawer';

export default KeyDetailDrawer;
