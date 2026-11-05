import React from 'react';
import { Button, Empty, Space, Table, Tag, Tooltip, message } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import copyToClipboard from 'copy-to-clipboard';
import { useTranslation } from 'react-i18next';
import type { SemanticVersion } from '../../../api/evolution';

/** 后端 `_version_dict` 给 `ds`；accept both（简报里写作 `data_source_id`）。 */
export const versionDs = (v: SemanticVersion): string | null =>
  v.data_source_id ?? v.ds ?? null;

/** `legacy` 后端给 int(0/1)，测试夹具可能给布尔。 */
export const isLegacyVersion = (v: SemanticVersion): boolean =>
  v.legacy === true || v.legacy === 1;

export const isArchivedVersion = (v: SemanticVersion): boolean => v.archived === true;

const statusColor = (status: string): string => {
  if (status === 'active') return 'success';
  if (status === 'candidate') return 'processing';
  if (status === 'rejected') return 'error';
  return 'default';
};

const fmtTime = (value?: string | null): string =>
  value ? value.replace('T', ' ').slice(0, 19) : '-';

interface VersionListProps {
  items: SemanticVersion[];
  /** 行内「回滚到此版本」；未给则不渲染动作列。 */
  onRollback?: (version: SemanticVersion) => void;
  rollbackDisabled?: boolean;
  /** 所在 run 的状态：只有 `adopted` 的 run 才有可回滚的线上版本。 */
  runStatus?: string;
}

/** 行内回滚是否可用：run 必须是 adopted，且目标版本已生效过（active/superseded）。 */
export const canRollbackTo = (
  runStatus: string | undefined,
  version: SemanticVersion,
): boolean => runStatus === 'adopted' && ['active', 'superseded'].includes(version.status);

/** 版本链表格：id / 状态 / scope / ds / legacy·archived / 时间 / hash（截断 + Tooltip）/ 回滚入口。
 * 数据来自 `RunBundle.versions`（该 run 相关版本）；空列表显式 Empty，不伪装成「没有历史」。 */
export default function VersionList({ items, onRollback, rollbackDisabled, runStatus }: VersionListProps) {
  const { t } = useTranslation();

  const copyId = async (versionId: string) => {
    // copy-to-clipboard 自带 execCommand 降级（http 非安全上下文没有 clipboard API）。
    const ok = await copyToClipboard(versionId);
    if (ok) message.success(t('common.copySuccess'));
    else message.error(t('common.copyFailed'));
  };

  const columns: ColumnsType<SemanticVersion> = [
    {
      title: t('evolutionConsole.versionId'),
      dataIndex: 'version_id',
      width: 250,
      render: (value: string) => (
        <Space size={4}>
          <Tooltip title={value}>
            <code style={{ fontSize: 12 }}>{value.length > 20 ? `${value.slice(0, 20)}…` : value}</code>
          </Tooltip>
          <Button type="link" size="small" style={{ padding: 0 }}
                  onClick={() => void copyId(value)}>
            {t('evolutionConsole.versionCopy')}
          </Button>
        </Space>
      ),
    },
    {
      title: t('evolutionConsole.versionStatus'),
      dataIndex: 'status',
      width: 110,
      render: (value: string) => (
        <Tag color={statusColor(value)}>
          {t(`evolutionConsole.versionStatusLabel.${value}`, { defaultValue: value })}
        </Tag>
      ),
    },
    {
      title: t('evolutionConsole.versionScope'),
      dataIndex: 'scope',
      width: 110,
      render: (value: string) => (
        <span>{t(`evolutionConsole.versionScopeLabel.${value}`, { defaultValue: value })}</span>
      ),
    },
    {
      title: t('evolutionConsole.versionDs'),
      key: 'ds',
      width: 150,
      ellipsis: true,
      render: (_: unknown, row) => versionDs(row) || '-',
    },
    {
      title: t('evolutionConsole.versionFlags'),
      key: 'flags',
      width: 150,
      render: (_: unknown, row) => (
        <Space size={4} wrap>
          {isLegacyVersion(row) && <Tag color="warning">{t('evolutionConsole.versionLegacyTag')}</Tag>}
          {isArchivedVersion(row) && <Tag>{t('evolutionConsole.versionArchivedTag')}</Tag>}
          {!isLegacyVersion(row) && !isArchivedVersion(row) && <span style={{ color: '#8c8c8c' }}>-</span>}
        </Space>
      ),
    },
    {
      title: t('evolutionConsole.versionCreated'),
      dataIndex: 'created_at',
      width: 170,
      render: (value: string | null) => fmtTime(value),
    },
    {
      title: t('evolutionConsole.versionHash'),
      dataIndex: 'snapshot_hash',
      width: 150,
      render: (value: string | null) => (
        value
          ? <Tooltip title={value}><code style={{ fontSize: 12 }}>{value.slice(0, 12)}…</code></Tooltip>
          : '-'
      ),
    },
  ];

  if (onRollback) {
    columns.push({
      title: t('evolutionConsole.versionAction'),
      key: 'action',
      width: 130,
      render: (_: unknown, row) => {
        const allowed = canRollbackTo(runStatus, row);
        const disabled = Boolean(rollbackDisabled) || !allowed;
        const reason = rollbackDisabled
          ? t('evolutionConsole.rollbackDryRunBlocked')
          : !allowed
            ? t('evolutionConsole.versionRollbackUnavailable')
            : '';
        return (
          <Tooltip title={reason}>
            {/* span 让 disabled 按钮也能触发 Tooltip */}
            <span>
              <Button size="small" disabled={disabled}
                      onClick={() => onRollback(row)}>
                {t('evolutionConsole.versionRollback')}
              </Button>
            </span>
          </Tooltip>
        );
      },
    });
  }

  return (
    <Table<SemanticVersion>
      rowKey="version_id"
      size="small"
      columns={columns}
      dataSource={items}
      pagination={false}
      locale={{ emptyText: <Empty description={t('evolutionConsole.versionsEmpty')} /> }}
    />
  );
}
