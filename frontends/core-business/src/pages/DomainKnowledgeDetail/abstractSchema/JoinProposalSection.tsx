/**
 * 概念对齐页 →「join 边提案」区块（join-agent 线）。
 *
 * 与指标提案同一形态：agent 只提案，人在这里确认或驳回；确认前不写关系边。
 * 五态齐全：loading / empty / error / success / refresh。
 */
import { useCallback, useEffect, useState } from 'react';
import {
  Alert, Button, Empty, Input, List, Modal, Space, Spin, Tag, Typography, message,
} from 'antd';
import { useTranslation } from 'react-i18next';
import {
  decideJoinProposal,
  listJoinProposals,
  runJoinAgent,
  type JoinProposal,
} from '@/api/joinProposal';

const { Text } = Typography;
const { TextArea } = Input;

interface Props {
  kbId: string;
  canWrite: boolean;
}

const STATUS_COLOR: Record<string, string> = {
  pending: 'orange',
  confirmed: 'green',
  rejected: 'default',
};

const CONFIDENCE_COLOR: Record<string, string> = {
  high: 'green',
  medium: 'blue',
  low: 'red',
};

export default function JoinProposalSection({ kbId, canWrite }: Props) {
  const { t } = useTranslation();

  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [proposals, setProposals] = useState<JoinProposal[]>([]);
  const [running, setRunning] = useState(false);
  const [deciding, setDeciding] = useState<JoinProposal | null>(null);
  const [decision, setDecision] = useState<'confirmed' | 'rejected'>('confirmed');
  const [note, setNote] = useState('');
  const [submitting, setSubmitting] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      setProposals(await listJoinProposals(kbId));
    } catch (e: any) {
      setError(e?.message || t('joinProposal.loadFailed'));
    } finally {
      setLoading(false);
    }
  }, [kbId, t]);

  useEffect(() => { void load(); }, [load]);

  const doRun = async () => {
    setRunning(true);
    try {
      const out = await runJoinAgent(kbId);
      if (!out.ok) {
        message.warning(out.message || t('joinProposal.runFailed'));
      } else if (out.proposals.length === 0) {
        message.info(t('joinProposal.noCandidate', { count: out.candidates ?? 0 }));
      } else {
        message.success(t('joinProposal.proposed', { count: out.proposals.length }));
      }
      await load();
    } catch (e: any) {
      message.error(e?.message || t('joinProposal.runFailed'));
    } finally {
      setRunning(false);
    }
  };

  const doDecide = async () => {
    if (!deciding) return;
    setSubmitting(true);
    try {
      const out = await decideJoinProposal(kbId, deciding.proposalId, decision, note);
      if (!out.changed) {
        message.info(t('joinProposal.alreadyDecided'));
      } else {
        message.success(decision === 'confirmed'
          ? t('joinProposal.confirmed')
          : t('joinProposal.rejected'));
      }
      setDeciding(null);
      setNote('');
      await load();
    } catch (e: any) {
      message.error(e?.message || t('joinProposal.decideFailed'));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Space direction="vertical" size={12} style={{ width: '100%' }}>
      <Space wrap>
        <Text strong>{t('joinProposal.title')}</Text>
        <Button size="small" onClick={() => void load()} loading={loading}>
          {t('common.refresh')}
        </Button>
        <Button
          type="primary"
          size="small"
          disabled={!canWrite}
          loading={running}
          onClick={() => void doRun()}
        >
          {t('joinProposal.run')}
        </Button>
        <Text type="secondary">{t('joinProposal.hint')}</Text>
      </Space>

      {error && (
        <Alert
          type="error"
          showIcon
          message={error}
          action={<Button size="small" onClick={() => void load()}>{t('common.retry')}</Button>}
        />
      )}

      {loading && !proposals.length ? (
        <Spin />
      ) : proposals.length === 0 ? (
        <Empty
          image={Empty.PRESENTED_IMAGE_SIMPLE}
          description={t('joinProposal.empty')}
        />
      ) : (
        <List
          size="small"
          dataSource={proposals}
          renderItem={(proposal) => (
            <List.Item
              actions={proposal.status === 'pending' ? [
                <Button
                  key="confirm"
                  size="small"
                  type="primary"
                  disabled={!canWrite}
                  onClick={() => {
                    setDeciding(proposal);
                    setDecision('confirmed');
                    setNote('');
                  }}
                >
                  {t('joinProposal.confirm')}
                </Button>,
                <Button
                  key="reject"
                  size="small"
                  danger
                  disabled={!canWrite}
                  onClick={() => {
                    setDeciding(proposal);
                    setDecision('rejected');
                    setNote('');
                  }}
                >
                  {t('joinProposal.reject')}
                </Button>,
              ] : [
                <Text key="decided" type="secondary">
                  {proposal.decidedAt
                    ? t('joinProposal.decidedAt', { at: proposal.decidedAt })
                    : ''}
                </Text>,
              ]}
            >
              <Space direction="vertical" size={6} style={{ width: '100%' }}>
                <Space wrap>
                  <Text code>{proposal.sourceEntity}.{proposal.sourceColumn}</Text>
                  <Text>→</Text>
                  <Text code>{proposal.targetEntity}.{proposal.targetColumn}</Text>
                  <Tag color={STATUS_COLOR[proposal.status]}>
                    {t(`joinProposal.status.${proposal.status}`)}
                  </Tag>
                  <Tag color={CONFIDENCE_COLOR[proposal.confidence]}>
                    {t(`joinProposal.confidence.${proposal.confidence}`)}
                  </Tag>
                </Space>
                {proposal.rationale && (
                  <Text type="secondary">{proposal.rationale}</Text>
                )}
              </Space>
            </List.Item>
          )}
        />
      )}

      <Modal
        title={decision === 'confirmed'
          ? t('joinProposal.confirm')
          : t('joinProposal.reject')}
        open={Boolean(deciding)}
        onCancel={() => setDeciding(null)}
        onOk={() => void doDecide()}
        okButtonProps={{ loading: submitting }}
        cancelText={t('common.cancel')}
        destroyOnClose
      >
        <Space direction="vertical" size={8} style={{ width: '100%' }}>
          <Text>
            {deciding?.sourceEntity}.{deciding?.sourceColumn} →{' '}
            {deciding?.targetEntity}.{deciding?.targetColumn}
          </Text>
          <TextArea
            rows={3}
            value={note}
            maxLength={500}
            onChange={(event) => setNote(event.target.value)}
            placeholder={t('joinProposal.notePlaceholder')}
          />
        </Space>
      </Modal>
    </Space>
  );
}
