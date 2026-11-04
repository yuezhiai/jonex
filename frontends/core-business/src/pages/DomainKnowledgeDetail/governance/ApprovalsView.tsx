/** 治理 → 指标调用审批（W4.8）：列表 / 申请 / 批准 / 下线。
 *
 * 口径：`docs/superpowers/specs/2026-11-02-w4.8-approval-ui-design.md`（工作区文档，不入库）。
 * 审批/下线由后端再校验租户管理员；这里只按 `canWrite` 决定是否显示入口。
 */
import { useCallback, useEffect, useState } from 'react';
import {
  Alert, Button, Empty, Input, Modal, Segmented, Space, Spin, Table, Tag, message,
} from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { useTranslation } from 'react-i18next';
import {
  approveMetricCall,
  listMetricCallApprovals,
  requestMetricCallApproval,
  suspendMetricCall,
  type MetricCallApproval,
  type MetricCallApprovalStatus,
} from '../../../api/governance';

const STATUS_COLOR: Record<string, string> = {
  pending: 'orange', approved: 'green', suspended: 'red',
};

type StatusFilter = 'all' | MetricCallApprovalStatus;
type ActionType = 'approve' | 'suspend';

interface Props {
  kbId: string;
  canWrite?: boolean;
}

function statusLabel(t: (key: string, opts?: Record<string, unknown>) => string,
                     status: string): string {
  return t(`domainKnowledge.governance.approvalStatus.${status}`,
    { defaultValue: status });
}

export default function ApprovalsView({ kbId, canWrite = false }: Props) {
  const { t } = useTranslation();
  const [status, setStatus] = useState<StatusFilter>('all');
  const [items, setItems] = useState<MetricCallApproval[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [requestOpen, setRequestOpen] = useState(false);
  const [requestMetric, setRequestMetric] = useState('');
  const [requestReason, setRequestReason] = useState('');
  const [requesting, setRequesting] = useState(false);
  const [action, setAction] = useState<{ type: ActionType; row: MetricCallApproval } | null>(null);
  const [actionReason, setActionReason] = useState('');
  const [acting, setActing] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await listMetricCallApprovals(
        kbId, status === 'all' ? undefined : status);
      setItems(res.items || []);
      setLoaded(true);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : t('common.loadFailed'));
    } finally {
      setLoading(false);
    }
  }, [kbId, status, t]);

  useEffect(() => {
    void load();
  }, [load]);

  const submitRequest = async () => {
    const metric = requestMetric.trim();
    if (!metric) {
      message.warning(t('domainKnowledge.governance.approvalMetricRequired'));
      return;
    }
    setRequesting(true);
    try {
      await requestMetricCallApproval(kbId, metric, requestReason.trim());
      message.success(t('domainKnowledge.governance.approvalRequested'));
      setRequestOpen(false);
      setRequestMetric('');
      setRequestReason('');
      await load();
    } catch (e: unknown) {
      message.error(e instanceof Error ? e.message : t('common.error'));
    } finally {
      setRequesting(false);
    }
  };

  const submitAction = async () => {
    if (!action) return;
    setActing(true);
    try {
      if (action.type === 'approve') {
        await approveMetricCall(kbId, action.row.metric_name, actionReason.trim());
        message.success(t('domainKnowledge.governance.approvalApproved'));
      } else {
        await suspendMetricCall(kbId, action.row.metric_name, actionReason.trim());
        message.success(t('domainKnowledge.governance.approvalSuspended'));
      }
      setAction(null);
      setActionReason('');
      await load();
    } catch (e: unknown) {
      message.error(e instanceof Error ? e.message : t('common.error'));
    } finally {
      setActing(false);
    }
  };

  const columns: ColumnsType<MetricCallApproval> = [
    {
      title: t('domainKnowledge.governance.approvalColMetric'),
      dataIndex: 'metric_name',
      render: (v: string) => <code>{v}</code>,
      ellipsis: true,
    },
    {
      title: t('domainKnowledge.governance.approvalColStatus'),
      dataIndex: 'status',
      width: 110,
      render: (v: string) => (
        <Tag color={STATUS_COLOR[v] ?? 'default'}>{statusLabel(t, v)}</Tag>
      ),
    },
    {
      title: t('domainKnowledge.governance.approvalColRequestedBy'),
      dataIndex: 'requested_by',
      width: 140,
      render: (v?: string) => v || '-',
    },
    {
      title: t('domainKnowledge.governance.approvalColDecidedBy'),
      dataIndex: 'decided_by',
      width: 140,
      render: (v?: string) => v || '-',
    },
    {
      title: t('domainKnowledge.governance.approvalColReason'),
      dataIndex: 'reason',
      ellipsis: true,
      render: (v?: string) => v || '-',
    },
    {
      title: t('domainKnowledge.governance.approvalColUpdatedAt'),
      dataIndex: 'updated_at',
      width: 180,
      render: (v?: string) => (v ? v.replace('T', ' ').slice(0, 19) : '-'),
    },
    {
      title: t('domainKnowledge.governance.approvalColActions'),
      key: 'actions',
      width: 170,
      render: (_, row) => (
        <Space size={4}>
          {row.status !== 'approved' && (
            <Button size="small" type="link" disabled={!canWrite}
                    onClick={() => { setAction({ type: 'approve', row }); setActionReason(''); }}>
              {t('domainKnowledge.governance.approvalApprove')}
            </Button>
          )}
          {row.status !== 'suspended' && (
            <Button size="small" type="link" danger disabled={!canWrite}
                    onClick={() => { setAction({ type: 'suspend', row }); setActionReason(''); }}>
              {t('domainKnowledge.governance.approvalSuspend')}
            </Button>
          )}
        </Space>
      ),
    },
  ];

  return (
    <div>
      <Alert type="info" showIcon style={{ marginBottom: 12 }}
             message={t('domainKnowledge.governance.approvalHint')} />
      <Space style={{ marginBottom: 12 }} wrap>
        <Segmented
          value={status}
          onChange={(v) => setStatus(v as StatusFilter)}
          options={[
            { value: 'all', label: t('domainKnowledge.governance.approvalAll') },
            { value: 'pending', label: statusLabel(t, 'pending') },
            { value: 'approved', label: statusLabel(t, 'approved') },
            { value: 'suspended', label: statusLabel(t, 'suspended') },
          ]}
        />
        <Button loading={loading} onClick={() => void load()}>
          {t('common.refresh')}
        </Button>
        <Button type="primary" disabled={!canWrite} onClick={() => setRequestOpen(true)}>
          {t('domainKnowledge.governance.approvalRequest')}
        </Button>
      </Space>
      {!canWrite && (
        <Alert type="warning" showIcon style={{ marginBottom: 12 }}
               message={t('domainKnowledge.governance.approvalReadOnly')} />
      )}
      {error && (
        <Alert type="error" showIcon style={{ marginBottom: 12 }} message={error}
               action={<Button size="small" onClick={() => void load()}>{t('common.retry')}</Button>} />
      )}
      {loading && !loaded ? (
        <div style={{ textAlign: 'center', padding: 32 }}><Spin /></div>
      ) : items.length === 0 ? (
        <Empty description={t('domainKnowledge.governance.approvalEmpty')} />
      ) : (
        <Table<MetricCallApproval>
          rowKey="id"
          size="small"
          dataSource={items}
          columns={columns}
          scroll={{ x: 1000 }}
          pagination={{ pageSize: 20, hideOnSinglePage: true }}
        />
      )}

      <Modal
        open={requestOpen}
        title={t('domainKnowledge.governance.approvalRequest')}
        confirmLoading={requesting}
        onOk={() => void submitRequest()}
        onCancel={() => setRequestOpen(false)}
        okText={t('common.confirm')}
        cancelText={t('common.cancel')}
      >
        <Space direction="vertical" style={{ width: '100%' }}>
          <Input
            placeholder={t('domainKnowledge.governance.approvalMetricPlaceholder')}
            value={requestMetric}
            onChange={(e) => setRequestMetric(e.target.value)}
            maxLength={255}
          />
          <Input.TextArea
            placeholder={t('domainKnowledge.governance.approvalReasonPlaceholder')}
            value={requestReason}
            onChange={(e) => setRequestReason(e.target.value)}
            rows={3}
            maxLength={500}
          />
        </Space>
      </Modal>

      <Modal
        open={action !== null}
        title={action?.type === 'approve'
          ? t('domainKnowledge.governance.approvalApproveTitle')
          : t('domainKnowledge.governance.approvalSuspendTitle')}
        confirmLoading={acting}
        onOk={() => void submitAction()}
        onCancel={() => setAction(null)}
        okText={t('common.confirm')}
        cancelText={t('common.cancel')}
        okButtonProps={{ danger: action?.type === 'suspend' }}
      >
        <p>
          {t('domainKnowledge.governance.approvalTarget', {
            metric: action?.row.metric_name || '',
          })}
        </p>
        <Input.TextArea
          placeholder={t('domainKnowledge.governance.approvalReasonPlaceholder')}
          value={actionReason}
          onChange={(e) => setActionReason(e.target.value)}
          rows={3}
          maxLength={500}
        />
      </Modal>
    </div>
  );
}
