import { forwardRef, useImperativeHandle, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Button, Drawer, Typography } from 'antd';
import { KeyOutlined } from '@ant-design/icons';
import type { McpKeyItem, WriteGrant } from '@/api/mcpKeys';
import './index.css';

export type McpKeyDetailDrawerHandle = {
  open: (record: McpKeyItem) => void;
};

interface McpKeyDetailDrawerProps {
  /** KB id → 名称（写入范围展示） */
  kbNameMap: Map<string, string>;
}

/** 4 态 → 文案 key / 圆点样式（与 McpKeysTab 列表一列对齐） */
const STATUS_META: Record<string, { labelKey: string; cls: string }> = {
  active: { labelKey: 'mcpKeyManagement.activeStatus', cls: 'is-active' },
  disabled: { labelKey: 'mcpKeyManagement.disabledStatus', cls: 'is-off' },
  expired: { labelKey: 'mcpKeyManagement.expiredStatus', cls: 'is-expired' },
  revoked: { labelKey: 'mcpKeyManagement.revokedStatus', cls: 'is-revoked' },
};

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

    /** 写入范围：按知识库展示（整库授权，不再区分目录级） */
    const grantText = (g: WriteGrant) => kbNameMap.get(g.kb) ?? g.kb;

    const statusMeta = record ? (STATUS_META[deriveStatus(record)] ?? STATUS_META.active) : STATUS_META.active;
    const grants = record?.write_grants ?? [];
    /** Key 级知识库白名单（上界）：空数组 = 不限（全部知识库） */
    const allowedKbIds = record?.allowed_kb_ids ?? [];
    const servicePermissions = record?.service_permissions ?? [];
    const callCount = servicePermissions.filter((s) => s.permission_level === 'call').length;
    const viewCount = servicePermissions.filter((s) => s.permission_level === 'view').length;
    // 旧写 Key 并入统一 mcp-keys 后可能带 write/* 级权限，与列表口径一致：视同知识写入
    const legacyWriteCount = servicePermissions.filter(
      (s) => s.permission_level !== 'call' && s.permission_level !== 'view',
    ).length;
    const writeCount = grants.length + legacyWriteCount;
    const hasAuth = callCount + viewCount + writeCount > 0;

    return (
      <Drawer
        className="mcp-key-detail"
        placement="right"
        size={560}
        open={open}
        onClose={() => setOpen(false)}
        title={
          <div className="mcp-drawer-title">
            <span className="mcp-drawer-title-icon">
              <KeyOutlined />
            </span>
            <span className="mcp-drawer-title-text">{t('mcpKeyManagement.detailTitle')}</span>
          </div>
        }
        footer={
          <div className="mcp-drawer-footer">
            <Button onClick={() => setOpen(false)}>{t('common.close')}</Button>
          </div>
        }
      >
        {record && (
          <div className="mcp-kd">
            {/* 头部：名称 + 状态 + 前缀，只用一条分隔线，不做卡片 */}
            <div className="mcp-kd-head">
              <div className="mcp-kd-name">{record.name || '-'}</div>
              <div className="mcp-kd-meta">
                <span className={`mcp-status ${statusMeta.cls}`}>{t(statusMeta.labelKey)}</span>
                {record.key_prefix && (
                  <Typography.Text code copyable={{ text: record.key_prefix }}>
                    mcp_{record.key_prefix}
                  </Typography.Text>
                )}
              </div>
            </div>

            {/* 基础信息：两列事实行，无边框底板 */}
            <section className="mcp-kd-section">
              <div className="mcp-kd-section-title">{t('mcpKeyManagement.detailSectionBasic')}</div>
              <div className="mcp-kd-facts">
                <div className="mcp-kd-fact">
                  <span className="k">{t('mcpKeyManagement.expiryLabel')}</span>
                  <span className="v">
                    {record.expires_at ? record.expires_at.slice(0, 10) : t('mcpKeyManagement.expiryForever')}
                  </span>
                </div>
                <div className="mcp-kd-fact">
                  <span className="k">{t('mcpKeyManagement.colCreatedAt')}</span>
                  <span className="v">{record.created_at ? record.created_at.slice(0, 10) : '-'}</span>
                </div>
              </div>
            </section>

            {/* 授权能力：胶囊横排，写入范围单独一行 */}
            <section className="mcp-kd-section">
              <div className="mcp-kd-section-title">{t('mcpKeyManagement.colCapability')}</div>
              {hasAuth ? (
                <>
                  <div className="mcp-kd-chips">
                    {callCount > 0 && (
                      <span className="mcp-chip is-call">{t('mcpKeyManagement.callCount', { n: callCount })}</span>
                    )}
                    {viewCount > 0 && (
                      <span className="mcp-chip is-view">{t('mcpKeyManagement.viewCount', { n: viewCount })}</span>
                    )}
                    {writeCount > 0 && (
                      <span className="mcp-chip is-write">
                        {t('mcpKeyManagement.writeCapability')} · {writeCount}
                      </span>
                    )}
                  </div>
                  {grants.length > 0 && (
                    <div className="mcp-kd-line">
                      <span className="mcp-kd-line-label">{t('mcpKeyManagement.colWriteScope')}</span>
                      <div className="mcp-kd-chips">
                        {grants.map((g, i) => (
                          <span className="mcp-chip" key={`${g.kb}-${i}`}>
                            {grantText(g)}
                          </span>
                        ))}
                      </div>
                    </div>
                  )}
                </>
              ) : (
                <span className="mcp-kd-empty">-</span>
              )}
              {/* 知识库范围（上界）：空 = 不限；有值 = 只能访问这些库 */}
              <div className="mcp-kd-line">
                <span className="mcp-kd-line-label">{t('mcpKeyManagement.kbScopeLabel')}</span>
                <div className="mcp-kd-chips">
                  {allowedKbIds.length === 0 ? (
                    <span className="mcp-chip is-more">{t('mcpKeyManagement.kbScopeAllTag')}</span>
                  ) : (
                    allowedKbIds.map((id) => (
                      <span className="mcp-chip" key={id}>
                        {kbNameMap.get(id) ?? id}
                      </span>
                    ))
                  )}
                </div>
              </div>
            </section>

            {/* 备注：纯文本，不加底框 */}
            <section className="mcp-kd-section">
              <div className="mcp-kd-section-title">{t('mcpKeyManagement.noteLabel')}</div>
              <p className="mcp-kd-text">{record.note || '-'}</p>
            </section>
          </div>
        )}
      </Drawer>
    );
  },
);
McpKeyDetailDrawer.displayName = 'McpKeyDetailDrawer';

export default McpKeyDetailDrawer;
