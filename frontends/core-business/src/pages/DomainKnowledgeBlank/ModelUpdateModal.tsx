import React, { useCallback, useEffect, useRef, useState } from 'react';
import { Alert, Button, Modal, Space, Spin, Steps, Tag, Typography, message } from 'antd';
import { useTranslation } from 'react-i18next';
import {
  applyModelUpdate,
  getModelUpdateStatus,
  startModelUpdate,
} from '@/api/domainKnowledge';
import type { ModelUpdateJob } from '@/types/domainKnowledge';

const POLL_INTERVAL_MS = 2000;
const YAML_PREVIEW_LIMIT = 8000;

const STAGE_KEYS: Record<string, string> = {
  queued: 'stageQueued',
  normalizing: 'stageNormalizing',
  upserting: 'stageUpserting',
  projecting: 'stageProjecting',
  schema: 'stageSchema',
};

/** 进度展示用：与 Ontology Builder 的 job.status 一一对应（顺序即实际执行顺序） */
const STAGE_ORDER = ['queued', 'normalizing', 'upserting', 'projecting', 'schema'] as const;

interface ModelUpdateModalProps {
  open: boolean;
  kbId: string;
  docId: string;
  docName: string;
  onClose: () => void;
  /** 导入成功后通知父级刷新文档/模型相关数据。 */
  onApplied: () => void;
}

/**
 * 「更新模型」：发起 → 轮询 → 草稿预览 → 人工确认导入。
 *
 * 草稿**不会自动落库**：ready 之前 Apply 按钮一直不可点；invalid/failed 只展示
 * 原因，用户关掉重试即可（重新发起会开新任务）。
 */
export default function ModelUpdateModal({
  open,
  kbId,
  docId,
  docName,
  onClose,
  onApplied,
}: ModelUpdateModalProps) {
  const { t } = useTranslation();
  const [job, setJob] = useState<ModelUpdateJob | null>(null);
  const [jobId, setJobId] = useState<string | null>(null);
  const [started, setStarted] = useState(false);
  const [starting, setStarting] = useState(false);
  const [applying, setApplying] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const pollTimer = useRef<number | null>(null);
  const cancelled = useRef(false);
  /** 任务发起时刻：轮询每 2s 触发一次重渲染，用它算「已运行 N 秒」，避免用户以为卡死 */
  const startedAtRef = useRef<number | null>(null);

  const stopPolling = useCallback(() => {
    if (pollTimer.current !== null) {
      window.clearTimeout(pollTimer.current);
      pollTimer.current = null;
    }
  }, []);

  // 打开弹窗只重置状态——**不自动发起**；用户点「确认开始」才提交任务。
  useEffect(() => {
    if (!open) return;
    cancelled.current = false;
    setJob(null);
    setJobId(null);
    setStarted(false);
    setStarting(false);
    setApplying(false);
    setError(null);
    return () => {
      cancelled.current = true;
      stopPolling();
    };
  }, [open, stopPolling]);

  const handleStart = useCallback(async () => {
    setStarting(true);
    setError(null);
    try {
      const startedJob = await startModelUpdate(kbId, docId);
      if (cancelled.current) return;
      setJob(startedJob);
      setJobId(startedJob.job_id);
      setStarted(true);
      startedAtRef.current = Date.now();
    } catch (err: any) {
      if (!cancelled.current) {
        setError(err?.message || t('domainKnowledge.modelUpdate.startFailed'));
      }
    } finally {
      if (!cancelled.current) setStarting(false);
    }
  }, [kbId, docId, t]);

  useEffect(() => {
    if (!open || !jobId) return;
    let active = true;

    const poll = async () => {
      try {
        const next = await getModelUpdateStatus(kbId, jobId);
        if (!active) return;
        setJob(next);
        if (next.status === 'running') {
          pollTimer.current = window.setTimeout(poll, POLL_INTERVAL_MS);
        }
      } catch (err: any) {
        if (active) {
          setError(err?.message || t('domainKnowledge.modelUpdate.pollFailed'));
        }
      }
    };

    void poll();
    return () => {
      active = false;
      stopPolling();
    };
  }, [open, jobId, kbId, stopPolling, t]);

  const handleApply = async () => {
    if (!job || job.status !== 'ready') return;
    setApplying(true);
    try {
      const applied = await applyModelUpdate(kbId, job.job_id);
      message.success(
        t('domainKnowledge.modelUpdate.applySuccess', {
          version: applied.schema_version ?? '',
        }),
      );
      onApplied();
      onClose();
    } catch (err: any) {
      message.error(err?.message || t('domainKnowledge.modelUpdate.applyFailed'));
    } finally {
      setApplying(false);
    }
  };

  const status = job?.status ?? 'running';
  const draft = job?.draft ?? null;
  const yamlPreview =
    draft?.yaml_text && draft.yaml_text.length > YAML_PREVIEW_LIMIT
      ? `${draft.yaml_text.slice(0, YAML_PREVIEW_LIMIT)}\n…`
      : draft?.yaml_text;

  return (
    <Modal
      open={open}
      title={t('domainKnowledge.modelUpdate.title', { name: docName })}
      width={760}
      onCancel={onClose}
      destroyOnClose
      footer={
        <Space>
          <Button onClick={onClose}>
            {started
              ? t('domainKnowledge.modelUpdate.close')
              : t('common.cancel')}
          </Button>
          {started ? (
            <Button
              type="primary"
              disabled={status !== 'ready'}
              loading={applying}
              onClick={handleApply}
            >
              {t('domainKnowledge.modelUpdate.apply')}
            </Button>
          ) : (
            <Button type="primary" loading={starting} onClick={handleStart}>
              {t('domainKnowledge.modelUpdate.confirmStart')}
            </Button>
          )}
        </Space>
      }
    >
      <Typography.Paragraph type="secondary">
        {t('domainKnowledge.modelUpdate.description')}
      </Typography.Paragraph>

      {error && <Alert type="error" showIcon message={error} style={{ marginBottom: 12 }} />}

      {!started && (
        <>
          <Alert
            type="info"
            showIcon
            message={t('domainKnowledge.modelUpdate.beforeStart')}
            style={{ marginBottom: 12 }}
          />
          {starting && (
            <Space>
              <Spin size="small" />
              <span>{t('domainKnowledge.modelUpdate.starting')}</span>
            </Space>
          )}
        </>
      )}

      {started && job && (
        <>
          <Steps
            size="small"
            style={{ marginBottom: 12 }}
            status={status === 'failed' ? 'error' : status === 'running' ? 'process' : 'finish'}
            current={Math.max(0, STAGE_ORDER.indexOf((job.stage as (typeof STAGE_ORDER)[number]) ?? 'queued'))}
            items={STAGE_ORDER.map((stage) => ({
              title: t(`domainKnowledge.modelUpdate.${STAGE_KEYS[stage]}`),
            }))}
          />
          <Space style={{ marginBottom: 12 }}>
            <span>{t('domainKnowledge.modelUpdate.statusLabel')}</span>
            {status === 'running' ? (
              <Tag color="processing">
                {t(
                  `domainKnowledge.modelUpdate.${
                    STAGE_KEYS[job.stage] || 'stageRunning'
                  }`,
                )}
              </Tag>
            ) : (
              <Tag color={status === 'ready' || status === 'applied' ? 'success' : 'error'}>
                {t(`domainKnowledge.modelUpdate.status.${status}`)}
              </Tag>
            )}
            {status === 'running' && startedAtRef.current !== null && (
              <Typography.Text type="secondary">
                {t('domainKnowledge.modelUpdate.elapsed', {
                  seconds: Math.max(0, Math.round((Date.now() - startedAtRef.current) / 1000)),
                })}
              </Typography.Text>
            )}
            {job.stats && (
              <Typography.Text type="secondary">
                {t('domainKnowledge.modelUpdate.stats', {
                  entities: job.stats.entities,
                  relations: job.stats.relations,
                  constraints: job.stats.constraints,
                })}
              </Typography.Text>
            )}
          </Space>

          {job.warnings.length > 0 && (
            <Alert
              type="warning"
              showIcon
              style={{ marginBottom: 12 }}
              message={
                <ul style={{ margin: 0, paddingLeft: 18 }}>
                  {job.warnings.map((w) => (
                    <li key={w}>{w}</li>
                  ))}
                </ul>
              }
            />
          )}

          {status === 'failed' && job.error && (
            <Alert type="error" showIcon message={job.error} style={{ marginBottom: 12 }} />
          )}

          {draft && draft.errors.length > 0 && (
            <Alert
              type="error"
              showIcon
              style={{ marginBottom: 12 }}
              message={t('domainKnowledge.modelUpdate.invalidTitle')}
              description={
                <ul style={{ margin: 0, paddingLeft: 18 }}>
                  {draft.errors.map((e) => (
                    <li key={e}>{e}</li>
                  ))}
                </ul>
              }
            />
          )}

          {draft && (
            <>
              <Typography.Text strong>
                {t('domainKnowledge.modelUpdate.preview')}
              </Typography.Text>
              <pre
                style={{
                  marginTop: 8,
                  maxHeight: 320,
                  overflow: 'auto',
                  padding: 12,
                  borderRadius: 6,
                  background: '#f8fafc',
                  border: '1px solid #e2e8f0',
                  fontSize: 12,
                  whiteSpace: 'pre-wrap',
                  wordBreak: 'break-word',
                }}
              >
                {yamlPreview}
              </pre>
            </>
          )}
        </>
      )}
    </Modal>
  );
}
