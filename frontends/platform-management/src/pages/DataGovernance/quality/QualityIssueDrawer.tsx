import { useCallback, useEffect, useRef, useState } from 'react';
import {
  Alert, Button, Drawer, Input, Select, Space, Spin, Tag, Timeline, message,
} from 'antd';
import { useTranslation } from 'react-i18next';

import {
  assignQualityIssue,
  commentQualityIssue,
  getQualityIssue,
  updateQualityIssueStatus,
  verifyQualityIssue,
} from '@/api/quality';
import { listUsers } from '@/api/users';
import type { DgQualityIssueDetail, DgQualityIssueStatus } from '../types';
import {
  isIssueActionAllowed,
  issueEventLabelKey,
  issueStatusTransitions,
  severityColor,
  severityLabelKey,
  statusColor,
  statusLabelKey,
} from '../qualityUtils';

interface Props {
  open: boolean;
  issueId: string | null;
  userId: string | null;
  isAdmin: boolean;
  onClose: () => void;
  onChanged: () => void;
}

export default function QualityIssueDrawer({
  open, issueId, userId, isAdmin, onClose, onChanged,
}: Props) {
  const { t } = useTranslation();
  const [detail, setDetail] = useState<DgQualityIssueDetail | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [assignee, setAssignee] = useState('');
  const [userOptions, setUserOptions] = useState<{ value: string; label: string }[]>([]);
  const [note, setNote] = useState('');
  const [targetStatus, setTargetStatus] = useState<DgQualityIssueStatus | undefined>();
  const [comment, setComment] = useState('');
  const [busy, setBusy] = useState(false);
  // 快速 A→B 切换时，旧请求的响应不得覆盖新详情。
  const requestSeq = useRef(0);

  const load = useCallback(async () => {
    if (!issueId) return;
    const seq = ++requestSeq.current;
    setLoading(true);
    setError(null);
    try {
      const next = await getQualityIssue(issueId);
      if (seq !== requestSeq.current) return;
      setDetail(next);
      setTargetStatus(issueStatusTransitions(next.status)[0]);
    } catch (err) {
      if (seq !== requestSeq.current) return;
      setError(err instanceof Error ? err.message : t('dataGovernance.quality.loadFailed'));
    } finally {
      if (seq === requestSeq.current) setLoading(false);
    }
  }, [issueId, t]);

  useEffect(() => {
    if (!open) return;
    setDetail(null);
    setAssignee('');
    setNote('');
    setComment('');
    setError(null);
    void load();
    return () => {
      requestSeq.current += 1;
    };
  }, [open, load]);

  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    void listUsers(1, 100)
      .then((result) => {
        if (cancelled) return;
        setUserOptions(result.items.map((user) => ({
          value: String(user.id),
          label: user.display_name
            ? `${user.display_name} (${user.username})`
            : user.username,
        })));
      })
      .catch(() => {
        if (!cancelled) setUserOptions([]);
      });
    return () => { cancelled = true; };
  }, [open]);

  const run = async (action: () => Promise<unknown>, successKey: string) => {
    setBusy(true);
    try {
      await action();
      message.success(t(successKey));
      setNote('');
      setComment('');
      await load();
      onChanged();
    } catch (err) {
      message.error(err instanceof Error ? err.message : t('dataGovernance.quality.issue.actionFailed'));
    } finally {
      setBusy(false);
    }
  };

  const canOperate = detail
    ? isIssueActionAllowed(detail, userId, isAdmin)
    : false;

  const statusText = (value: string | null) => (value ? t(statusLabelKey(value)) : '-');

  return (
    <Drawer
      open={open}
      width={680}
      title={t('dataGovernance.quality.issue.detailTitle')}
      onClose={onClose}
      destroyOnClose
    >
      {loading ? <Spin /> : null}
      {error ? (
        <Alert
          type="error"
          showIcon
          message={error}
          action={<Button size="small" onClick={() => void load()}>{t('common.retry')}</Button>}
        />
      ) : null}
      {detail ? (
        <Space direction="vertical" style={{ width: '100%' }} size="middle">
          <Space wrap>
            <Tag color={statusColor(detail.status)}>{t(statusLabelKey(detail.status))}</Tag>
            <Tag color={severityColor(detail.severity)}>{t(severityLabelKey(detail.severity))}</Tag>
            <strong>{detail.title}</strong>
          </Space>
          <div>{detail.assetId}</div>
          {detail.detail ? <pre className="yx-dg-quality-detail">{detail.detail}</pre> : null}

          {isAdmin && detail.status !== 'closed' ? (
            <Space.Compact style={{ width: '100%' }}>
              <Select
                showSearch
                allowClear
                optionFilterProp="label"
                value={assignee || undefined}
                style={{ width: '100%' }}
                placeholder={t('dataGovernance.quality.issue.assigneePlaceholder')}
                options={userOptions}
                onChange={(value) => setAssignee(value ?? '')}
              />
              <Button
                loading={busy}
                disabled={!assignee}
                onClick={() => void run(
                  () => assignQualityIssue(detail.id, assignee, detail.revision),
                  'dataGovernance.quality.issue.assigned',
                )}
              >
                {t('dataGovernance.quality.issue.assign')}
              </Button>
            </Space.Compact>
          ) : null}

          {canOperate && issueStatusTransitions(detail.status).length > 0 ? (
            <Space wrap>
              <Select<DgQualityIssueStatus>
                value={targetStatus}
                style={{ width: 160 }}
                options={issueStatusTransitions(detail.status).map((value) => ({
                  value, label: t(statusLabelKey(value)),
                }))}
                onChange={setTargetStatus}
              />
              <Input
                value={note}
                style={{ width: 260 }}
                placeholder={t('dataGovernance.quality.issue.notePlaceholder')}
                onChange={(event) => setNote(event.target.value)}
              />
              <Button
                loading={busy}
                disabled={!targetStatus}
                onClick={() => void run(
                  () => updateQualityIssueStatus(detail.id, {
                    toStatus: targetStatus as DgQualityIssueStatus,
                    expectedRevision: detail.revision,
                    ...(note.trim() ? { note: note.trim() } : {}),
                  }),
                  'dataGovernance.quality.issue.statusChanged',
                )}
              >
                {t('dataGovernance.quality.issue.changeStatus')}
              </Button>
            </Space>
          ) : null}

          {isAdmin && detail.status === 'pending_verify' ? (
            <Space>
              <Button
                type="primary"
                loading={busy}
                onClick={() => void run(
                  () => verifyQualityIssue(detail.id, {
                    passed: true, note: note.trim() || undefined,
                    expectedRevision: detail.revision,
                  }),
                  'dataGovernance.quality.issue.verified',
                )}
              >
                {t('dataGovernance.quality.issue.verifyPass')}
              </Button>
              <Button
                danger
                loading={busy}
                onClick={() => void run(
                  () => verifyQualityIssue(detail.id, {
                    passed: false, note: note.trim() || t('dataGovernance.quality.issue.reopenedNote'),
                    expectedRevision: detail.revision,
                  }),
                  'dataGovernance.quality.issue.verifyFail',
                )}
              >
                {t('dataGovernance.quality.issue.verifyFail')}
              </Button>
            </Space>
          ) : null}

          {canOperate && detail.status !== 'closed' ? (
            <Space.Compact style={{ width: '100%' }}>
              <Input
                value={comment}
                placeholder={t('dataGovernance.quality.issue.commentPlaceholder')}
                onChange={(event) => setComment(event.target.value)}
              />
              <Button
                loading={busy}
                disabled={!comment.trim()}
                onClick={() => void run(
                  () => commentQualityIssue(detail.id, comment.trim()),
                  'dataGovernance.quality.issue.commented',
                )}
              >
                {t('dataGovernance.quality.issue.comment')}
              </Button>
            </Space.Compact>
          ) : null}

          <Timeline
            items={detail.events.map((event) => ({
              key: event.id,
              children: (
                <div>
                  <div>
                    <strong>
                      {t(issueEventLabelKey(event.eventType), { defaultValue: event.eventType })}
                    </strong>
                    {event.fromStatus || event.toStatus
                      ? ` (${statusText(event.fromStatus)} → ${statusText(event.toStatus)})`
                      : ''}
                  </div>
                  {event.note ? <div>{event.note}</div> : null}
                  <div className="yx-dg-quality-muted">{event.createdAt}</div>
                </div>
              ),
            }))}
          />
        </Space>
      ) : null}
    </Drawer>
  );
}
