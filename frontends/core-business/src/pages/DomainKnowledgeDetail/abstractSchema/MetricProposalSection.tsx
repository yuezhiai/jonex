/**
 * 概念对齐页 →「指标提案」区块（metric-agent 线）。
 *
 * 定位：agent 只**提案**，人在这里确认或驳回。确认前不写抽象 schema、不影响问数。
 * 五态齐全：loading / empty / error / success / refresh（顶部按钮重拉）。
 *
 * 为什么把复核结果摆在卡片上：模型自述「验证过了」不可信（2026-11-14 两次误判），
 * 平台复核（执行 / 值域 / 近重复列 / 空值覆盖）才是可直接看的证据。
 */
import { useCallback, useEffect, useState } from 'react';
import {
  Alert, Button, Empty, Input, List, Modal, Space, Spin, Tag, Typography, message,
} from 'antd';
import { useTranslation } from 'react-i18next';
import {
  buildMetricProposal,
  decideMetricProposal,
  listMetricProposals,
  type MetricProposal,
} from '@/api/metricProposal';

const { Text, Paragraph } = Typography;
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

export default function MetricProposalSection({ kbId, canWrite }: Props) {
  const { t } = useTranslation();

  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [proposals, setProposals] = useState<MetricProposal[]>([]);
  const [buildOpen, setBuildOpen] = useState(false);
  const [task, setTask] = useState('');
  const [building, setBuilding] = useState(false);
  const [deciding, setDeciding] = useState<MetricProposal | null>(null);
  const [decision, setDecision] = useState<'confirmed' | 'rejected'>('confirmed');
  const [note, setNote] = useState('');
  const [submitting, setSubmitting] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      setProposals(await listMetricProposals(kbId));
    } catch (e: any) {
      setError(e?.message || t('metricProposal.loadFailed'));
    } finally {
      setLoading(false);
    }
  }, [kbId, t]);

  useEffect(() => { void load(); }, [load]);

  const doBuild = async () => {
    const text = task.trim();
    if (!text) return;
    setBuilding(true);
    try {
      const out = await buildMetricProposal(kbId, text);
      if (!out.ok) {
        message.warning(out.message || t('metricProposal.buildFailed'));
      } else {
        message.success(t('metricProposal.built'));
      }
      setBuildOpen(false);
      setTask('');
      await load();
    } catch (e: any) {
      message.error(e?.message || t('metricProposal.buildFailed'));
    } finally {
      setBuilding(false);
    }
  };

  const doDecide = async () => {
    if (!deciding) return;
    setSubmitting(true);
    try {
      const out = await decideMetricProposal(kbId, deciding.proposalId, decision, note);
      if (!out.changed) {
        message.info(t('metricProposal.alreadyDecided'));
      } else {
        message.success(decision === 'confirmed'
          ? t('metricProposal.confirmed')
          : t('metricProposal.rejected'));
      }
      setDeciding(null);
      setNote('');
      await load();
    } catch (e: any) {
      message.error(e?.message || t('metricProposal.decideFailed'));
    } finally {
      setSubmitting(false);
    }
  };

  const renderVerification = (proposal: MetricProposal) => {
    const verification = proposal.verification;
    if (!verification) return null;
    const failed = verification.ok === false;
    const warnings = verification.warnings || [];
    if (!failed && warnings.length === 0) {
      return (
        <Text type="success">
          {t('metricProposal.verifyPassed', {
            value: verification.value ?? '-',
          })}
        </Text>
      );
    }
    return (
      <Alert
        type={failed ? 'error' : 'warning'}
        showIcon
        message={failed
          ? t('metricProposal.verifyFailed')
          : t('metricProposal.verifyWarnings', { count: warnings.length })}
        description={(
          <ul style={{ margin: 0, paddingLeft: 18 }}>
            {warnings.map((item, index) => (
              <li key={index}><Text>{item}</Text></li>
            ))}
          </ul>
        )}
      />
    );
  };

  return (
    <Space direction="vertical" size={12} style={{ width: '100%' }}>
      <Space wrap>
        <Text strong>{t('metricProposal.title')}</Text>
        <Button size="small" onClick={() => void load()} loading={loading}>
          {t('common.refresh')}
        </Button>
        <Button
          type="primary"
          size="small"
          disabled={!canWrite}
          onClick={() => setBuildOpen(true)}
        >
          {t('metricProposal.generate')}
        </Button>
        <Text type="secondary">{t('metricProposal.hint')}</Text>
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
          description={t('metricProposal.empty')}
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
                  {t('metricProposal.confirm')}
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
                  {t('metricProposal.reject')}
                </Button>,
              ] : [
                <Text key="decided" type="secondary">
                  {proposal.decidedAt
                    ? t('metricProposal.decidedAt', { at: proposal.decidedAt })
                    : ''}
                </Text>,
              ]}
            >
              <Space direction="vertical" size={6} style={{ width: '100%' }}>
                <Space wrap>
                  <Text strong>{proposal.name}</Text>
                  <Tag color={STATUS_COLOR[proposal.status]}>
                    {t(`metricProposal.status.${proposal.status}`)}
                  </Tag>
                  <Tag color={CONFIDENCE_COLOR[proposal.confidence]}>
                    {t(`metricProposal.confidence.${proposal.confidence}`)}
                  </Tag>
                  {proposal.grain && (
                    <Text type="secondary">{proposal.grain}</Text>
                  )}
                </Space>
                <Paragraph
                  code
                  style={{ marginBottom: 0, whiteSpace: 'pre-wrap' }}
                >
                  {proposal.formulaSql}
                </Paragraph>
                {proposal.filters && (
                  <Text type="secondary">
                    {t('metricProposal.filters')}: {proposal.filters}
                  </Text>
                )}
                {proposal.rationale && (
                  <Paragraph style={{ marginBottom: 0 }}>{proposal.rationale}</Paragraph>
                )}
                {renderVerification(proposal)}
              </Space>
            </List.Item>
          )}
        />
      )}

      <Modal
        title={t('metricProposal.buildTitle')}
        open={buildOpen}
        onCancel={() => setBuildOpen(false)}
        onOk={() => void doBuild()}
        okButtonProps={{ loading: building, disabled: !task.trim() }}
        okText={t('metricProposal.start')}
        cancelText={t('common.cancel')}
        destroyOnClose
      >
        <Space direction="vertical" size={8} style={{ width: '100%' }}>
          <Text type="secondary">{t('metricProposal.buildHint')}</Text>
          <TextArea
            rows={4}
            value={task}
            maxLength={500}
            onChange={(event) => setTask(event.target.value)}
            placeholder={t('metricProposal.taskPlaceholder')}
          />
        </Space>
      </Modal>

      <Modal
        title={decision === 'confirmed'
          ? t('metricProposal.confirm')
          : t('metricProposal.reject')}
        open={Boolean(deciding)}
        onCancel={() => setDeciding(null)}
        onOk={() => void doDecide()}
        okButtonProps={{ loading: submitting }}
        cancelText={t('common.cancel')}
        destroyOnClose
      >
        <Space direction="vertical" size={8} style={{ width: '100%' }}>
          <Text>{deciding?.name}</Text>
          <TextArea
            rows={3}
            value={note}
            maxLength={500}
            onChange={(event) => setNote(event.target.value)}
            placeholder={t('metricProposal.notePlaceholder')}
          />
        </Space>
      </Modal>
    </Space>
  );
}
