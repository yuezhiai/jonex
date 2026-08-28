import { forwardRef, useImperativeHandle, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Drawer, Descriptions, Tag, Space, Typography } from 'antd';
import type { McpKeyItem, WriteGrant } from '@/api/mcpKeys';

export type McpKeyDetailDrawerHandle = {
  open: (record: McpKeyItem) => void;
};

interface McpKeyDetailDrawerProps {
  /** KB id → 名称（写入范围展示） */
  kbNameMap: Map<string, string>;
}

const McpKeyDetailDrawer = forwardRef<McpKeyDetailDrawerHandle, McpKeyDetailDrawerProps>(
  ({ kbNameMap }, ref) => {
    const { t } = useTranslation();
    const [open, setOpen] = useState(false);
    const [record, setRecord] = useState<McpKeyItem | null>(null);

    useImperativeHandle(ref, () => ({
      open: (r) => {
        setRecord(r);
        setOpen(true);
      },
    }));

    /** 4 态派生：revoked（不可逆终态）> expired > disabled > active（与 McpKeysTab 对齐） */
    const deriveStatus = (r: McpKeyItem): 'active' | 'disabled' | 'expired' | 'revoked' => {
      if (r.status === 'disabled' || r.status === 'expired' || r.status === 'revoked') return r.status;
      if (r.revoked_at) return 'revoked';
      if (r.expires_at && new Date(r.expires_at).getTime() < Date.now()) return 'expired';
      if (r.disabled_at) return 'disabled';
      return 'active';
    };

    const statusTag = (r: McpKeyItem) => {
      const map: Record<string, React.ReactNode> = {
        revoked: <Tag color="error">{t('mcpKeyManagement.revokedStatus')}</Tag>,
        expired: <Tag color="orange">{t('mcpKeyManagement.expiredStatus')}</Tag>,
        disabled: <Tag>{t('mcpKeyManagement.disabledStatus')}</Tag>,
        active: <Tag color="success">{t('mcpKeyManagement.activeStatus')}</Tag>,
      };
      return map[deriveStatus(r)] || map.active;
    };

    const grantText = (g: WriteGrant) => {
      const kbName = kbNameMap.get(g.kb) ?? g.kb;
      return g.mode === 'all'
        ? `${kbName}(${t('mcpKeyManagement.writeAll')})`
        : `${kbName}(${g.directories?.length ?? 0} ${t('mcpKeyManagement.directoryUnit')})`;
    };

    return (
      <Drawer
        title={t('mcpKeyManagement.detailTitle')}
        placement="right"
        width={520}
        open={open}
        onClose={() => setOpen(false)}
      >
        {record && (
          <Descriptions column={1} size="small" colon={false} layout="vertical">
            <Descriptions.Item label={t('mcpKeyManagement.nameLabel')}>
              {record.name || '-'}
            </Descriptions.Item>
            <Descriptions.Item label={t('mcpKeyManagement.keyPrefix')}>
              {record.key_prefix ? <Typography.Text code>{record.key_prefix}</Typography.Text> : '-'}
            </Descriptions.Item>
            <Descriptions.Item label={t('mcpKeyManagement.status')}>
              {statusTag(record)}
            </Descriptions.Item>
            <Descriptions.Item label={t('mcpKeyManagement.colCapability')}>
              {(record.service_permissions || []).length > 0 || (record.write_grants || []).length > 0 ? (
                <Space size={4} wrap>
                  {(record.service_permissions || []).map((s) => (
                    <Tag
                      key={s.service_id}
                      color={s.permission_level === 'call' ? 'blue' : s.permission_level === 'view' ? 'gold' : 'green'}
                    >
                      {s.permission_level === 'call'
                        ? t('mcpKeyManagement.permissionCall')
                        : s.permission_level === 'view'
                          ? t('mcpKeyManagement.permissionView')
                          : t('mcpKeyManagement.writeCapability')}
                    </Tag>
                  ))}
                  {(record.write_grants || []).length > 0 && (
                    <Tag color="green">{t('mcpKeyManagement.writeCapability')}</Tag>
                  )}
                </Space>
              ) : (
                '-'
              )}
            </Descriptions.Item>
            <Descriptions.Item label={t('mcpKeyManagement.colWriteScope')}>
              {(record.write_grants || []).length > 0 ? (
                <Space size={4} wrap>
                  {(record.write_grants || []).map((g, i) => (
                    <Tag key={i}>{grantText(g)}</Tag>
                  ))}
                </Space>
              ) : (
                '-'
              )}
            </Descriptions.Item>
            <Descriptions.Item label={t('mcpKeyManagement.expiryLabel')}>
              {record.expires_at ? record.expires_at.slice(0, 10) : t('mcpKeyManagement.expiryForever')}
            </Descriptions.Item>
            <Descriptions.Item label={t('mcpKeyManagement.noteLabel')}>
              {record.note || '-'}
            </Descriptions.Item>
            <Descriptions.Item label={t('mcpKeyManagement.colCreatedAt')}>
              {record.created_at ? record.created_at.slice(0, 10) : '-'}
            </Descriptions.Item>
          </Descriptions>
        )}
      </Drawer>
    );
  },
);
McpKeyDetailDrawer.displayName = 'McpKeyDetailDrawer';

export default McpKeyDetailDrawer;
