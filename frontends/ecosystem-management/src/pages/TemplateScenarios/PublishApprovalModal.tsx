import { useCallback, useEffect, useRef, useState } from 'react';
import {
  Alert,
  Button,
  Empty,
  Input,
  Modal,
  Skeleton,
  Space,
  Table,
  Tag,
  message,
} from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { ReloadOutlined } from '@ant-design/icons';
import { useTranslation } from 'react-i18next';
import { getErrorMessage } from '@jonex/shared-lib';
import {
  approvePublishRequest,
  cancelPublishRequest,
  listPublishRequests,
  rejectPublishRequest,
} from '../../api/templateScenarios';
import type { PublishRequestItem, PublishRequestStatus } from '../../api/templateScenarios';

interface Props {
  open: boolean;
  scenarioId: string | null;
  scenarioName?: string;
  /** 租户管理员可审批；普通编辑只看到自己提交的申请（后端同口径）。 */
  canApprove: boolean;
  onClose: () => void;
  onChanged: () => void;
}

type Decision = { requestId: string; action: 'approve' | 'reject' } | null;

const STATUS_COLOR: Record<PublishRequestStatus, string> = {
  pending: 'processing',
  approved: 'success',
  rejected: 'error',
  cancelled: 'default',
};

function formatTime(value?: string | null): string {
  if (!value) return '—';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

export default function PublishApprovalModal({
  open,
  scenarioId,
  scenarioName,
  canApprove,
  onClose,
  onChanged,
}: Props) {
  const { t } = useTranslation();
  const [items, setItems] = useState<PublishRequestItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [decision, setDecision] = useState<Decision>(null);
  const [comment, setComment] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const seq = useRef(0);

  const load = useCallback(async (id: string) => {
    const my = ++seq.current;
    setLoading(true);
    setError(null);
    try {
      const next = await listPublishRequests(id);
      if (my !== seq.current) return;
      setItems(next.items ?? []);
    } catch (err: unknown) {
      if (my !== seq.current) return;
      setItems([]);
      setError(getErrorMessage(err, t('templateScenarios.publish.loadFailed')));
    } finally {
      if (my === seq.current) setLoading(false);
    }
  }, [t]);

  useEffect(() => {
    seq.current += 1;
    setItems([]);
    setError(null);
    setDecision(null);
    setComment('');
    if (!open || !scenarioId) {
      setLoading(false);
      return;
    }
    void load(scenarioId);
  }, [open, scenarioId, load]);

  const decide = async () => {
    if (!decision || !scenarioId) return;
    setSubmitting(true);
    try {
      if (decision.action === 'approve') {
        const result = await approvePublishRequest(decision.requestId, comment || undefined);
        message.success(
          t('templateScenarios.publish.approvedWithVersion', {
            version: result.published.version,
          }),
        );
      } else {
        await rejectPublishRequest(decision.requestId, comment || undefined);
        message.success(t('templateScenarios.publish.rejected'));
      }
      setDecision(null);
      setComment('');
      await load(scenarioId);
      onChanged();
    } catch (err: unknown) {
      message.error(getErrorMessage(err, t('templateScenarios.publish.decideFailed')));
    } finally {
      setSubmitting(false);
    }
  };

  const cancelRequest = async (requestId: string) => {
    if (!scenarioId) return;
    try {
      await cancelPublishRequest(requestId);
      message.success(t('templateScenarios.publish.cancelled'));
      await load(scenarioId);
      onChanged();
    } catch (err: unknown) {
      message.error(getErrorMessage(err, t('templateScenarios.publish.decideFailed')));
    }
  };

  const columns: ColumnsType<PublishRequestItem> = [
    {
      title: t('templateScenarios.publish.status'),
      dataIndex: 'status',
      width: 100,
      render: (status: PublishRequestStatus) => (
        <Tag color={STATUS_COLOR[status] ?? 'default'}>
          {t(`templateScenarios.publish.statuses.${status}`)}
        </Tag>
      ),
    },
    {
      title: t('templateScenarios.publish.submittedBy'),
      dataIndex: 'submitted_by',
      width: 130,
      render: (value: string | null) => value || '—',
    },
    {
      title: t('templateScenarios.publish.submittedAt'),
      dataIndex: 'submitted_at',
      width: 180,
      render: formatTime,
    },
    {
      title: t('templateScenarios.publish.decidedBy'),
      dataIndex: 'decided_by',
      width: 130,
      render: (value: string | null) => value || '—',
    },
    {
      title: t('templateScenarios.publish.decidedAt'),
      dataIndex: 'decided_at',
      width: 180,
      render: formatTime,
    },
    {
      title: t('templateScenarios.publish.publishedVersion'),
      dataIndex: 'published_version',
      width: 100,
      render: (value: number | null) => (value ? `v${value}` : '—'),
    },
    {
      title: t('templateScenarios.publish.comment'),
      dataIndex: 'comment',
      ellipsis: true,
      render: (value: string | null) => value || '—',
    },
    {
      title: '',
      key: 'actions',
      width: 170,
      render: (_: unknown, row) =>
        row.status === 'pending' ? (
          <Space size={4}>
            {canApprove && (
              <>
                <Button
                  size="small"
                  type="primary"
                  onClick={() => setDecision({ requestId: row.id, action: 'approve' })}
                >
                  {t('templateScenarios.publish.approve')}
                </Button>
                <Button
                  size="small"
                  danger
                  onClick={() => setDecision({ requestId: row.id, action: 'reject' })}
                >
                  {t('templateScenarios.publish.reject')}
                </Button>
              </>
            )}
            {!canApprove && (
              <Button size="small" onClick={() => void cancelRequest(row.id)}>
                {t('templateScenarios.publish.cancel')}
              </Button>
            )}
          </Space>
        ) : null,
    },
  ];

  return (
    <>
      <Modal
        open={open}
        onCancel={onClose}
        footer={null}
        width={980}
        destroyOnHidden
        title={
          <Space size={8}>
            <span>{t('templateScenarios.publish.title')}</span>
            {scenarioName && <Tag>{scenarioName}</Tag>}
          </Space>
        }
      >
        <Space direction="vertical" size={12} style={{ width: '100%' }}>
          <Space>
            <Button
              icon={<ReloadOutlined />}
              loading={loading}
              onClick={() => {
                if (scenarioId) void load(scenarioId);
              }}
            >
              {t('templateScenarios.refresh')}
            </Button>
          </Space>
          {loading ? (
            <Skeleton active paragraph={{ rows: 4 }} />
          ) : error ? (
            <Alert
              type="error"
              showIcon
              title={error}
              action={
                <Button size="small" onClick={() => scenarioId && void load(scenarioId)}>
                  {t('templateScenarios.refresh')}
                </Button>
              }
            />
          ) : (
            <Table<PublishRequestItem>
              rowKey="id"
              size="small"
              columns={columns}
              dataSource={items}
              pagination={false}
              scroll={{ x: 1100 }}
              locale={{
                emptyText: <Empty description={t('templateScenarios.publish.empty')} />,
              }}
            />
          )}
        </Space>
      </Modal>

      <Modal
        open={decision !== null}
        title={
          decision?.action === 'approve'
            ? t('templateScenarios.publish.approveTitle')
            : t('templateScenarios.publish.rejectTitle')
        }
        onCancel={() => {
          setDecision(null);
          setComment('');
        }}
        onOk={() => void decide()}
        confirmLoading={submitting}
        okText={
          decision?.action === 'approve'
            ? t('templateScenarios.publish.approve')
            : t('templateScenarios.publish.reject')
        }
        okButtonProps={{ danger: decision?.action === 'reject' }}
        destroyOnHidden
      >
        <Input.TextArea
          rows={3}
          value={comment}
          placeholder={t('templateScenarios.publish.commentPlaceholder')}
          onChange={(event) => setComment(event.target.value)}
        />
      </Modal>
    </>
  );
}
