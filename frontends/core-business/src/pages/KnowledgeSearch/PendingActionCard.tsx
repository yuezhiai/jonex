import React, { useMemo, useState } from 'react';
import { Alert, Button, Checkbox, Input, Space, Tag, Typography } from 'antd';
import { SafetyCertificateOutlined } from '@ant-design/icons';
import { useTranslation } from 'react-i18next';
import {
  executeOntologyAction,
  type ExecuteResult,
  type ExecutionChecklistItem,
} from '@/api/ontologyAction';
import type { ActionExecutionRecord, PendingActionView } from '@/types/knowledgeSearch';

const CONFIRM_KEY: Record<string, string> = {
  authorization: 'authorization',
  precondition: 'preconditions',
};

interface Props {
  item: PendingActionView;
  actionResults?: ActionExecutionRecord[] | null;
}

function asText(value: unknown): string {
  if (value == null) return '';
  if (typeof value === 'string') return value;
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}

/** 工具返回内容：能解析成 JSON 就格式化，解析不了原样展示（历史里可能是截断文本）。 */
function prettyOutput(value: unknown): string {
  const text = asText(value);
  try {
    return JSON.stringify(JSON.parse(text), null, 2).slice(0, 4000);
  } catch {
    return text.slice(0, 4000);
  }
}

/**
 * 问答动作提案卡片：服务端待办（pending_id）+ 强制预检 + 执行。
 *
 * 卡片本身不执行任何东西：预检/执行都调现有 `executeOntologyAction`，
 * 由执行器按知识库写权限判定并原子认领服务端待办；结果由执行器回写历史。
 */
export default function PendingActionCard({ item, actionResults }: Props) {
  const { t } = useTranslation();
  const historyResult = useMemo(
    () => (actionResults || []).find((r) => r.pending_id && r.pending_id === item.pending_id),
    [actionResults, item.pending_id],
  );
  const [inputs, setInputs] = useState<Record<string, string>>(() => {
    const out: Record<string, string> = {};
    Object.entries(item.inputs || {}).forEach(([key, value]) => {
      out[key] = asText(value);
    });
    return out;
  });
  const [confirmations, setConfirmations] = useState<Record<string, boolean>>({});
  const [checklist, setChecklist] = useState<ExecutionChecklistItem[]>(item.checklist || []);
  const [preflight, setPreflight] = useState<ExecuteResult | null>(null);
  const [result, setResult] = useState<ExecuteResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [outputOpen, setOutputOpen] = useState(false);
  const [busy, setBusy] = useState<'dry' | 'run' | null>(null);

  const providedKeys = new Set(item.provided_input_keys || []);
  const missingRequired = (item.required_inputs || []).filter(
    (field) => field.required && !providedKeys.has(field.name),
  );
  const allConfirmed = checklist.every(
    (c) => confirmations[CONFIRM_KEY[c.kind] || c.kind] === true,
  );
  const preflightOk = preflight?.status === 'planned';
  const terminal =
    Boolean(historyResult) ||
    ['executed', 'failed'].includes(result?.status || '') ||
    ['executed', 'failed'].includes(item.status || '');
  const executable = Boolean(item.executable && item.can_execute && item.pending_id) && !terminal;

  const invalidate = () => {
    setPreflight(null);
  };

  const submit = async (dryRun: boolean) => {
    if (!item.pending_id) return;
    setBusy(dryRun ? 'dry' : 'run');
    setError(null);
    try {
      const payload = {
        knowledgeBaseId: item.kb_id || '',
        actionName: item.action || '',
        inputs: Object.fromEntries(
          Object.entries(inputs).filter(([, value]) => value !== ''),
        ),
        confirmations,
        dryRun,
        qaPendingId: item.pending_id,
      };
      const out = await executeOntologyAction(payload);
      if (dryRun) {
        setPreflight(out);
        return;
      }
      setResult(out);
      if (out.status === 'needs_confirmation' && out.contract_changed) {
        // 契约在提问后变过：换成新 checklist，要求重新确认；待办仍在
        setChecklist(out.decision?.checklist || out.pending_confirmations || []);
        setConfirmations({});
        setPreflight(null);
      }
    } catch (e: any) {
      setError(e?.message || t('pendingAction.failed'));
    } finally {
      setBusy(null);
    }
  };

  const settledResult = historyResult || result;
  const settledStatus = settledResult?.status || item.status;

  return (
    <div
      style={{
        border: '1px solid #dbeafe',
        borderLeft: '3px solid #2563eb',
        borderRadius: '0 8px 8px 0',
        background: 'var(--yx-bg-subtle)',
        padding: '12px 14px',
        marginTop: 12,
      }}
    >
      <Space size={6} wrap style={{ marginBottom: 6 }}>
        <SafetyCertificateOutlined style={{ color: '#2563eb' }} />
        <strong style={{ fontSize: 13 }}>{t('pendingAction.title')}</strong>
        {item.action && <Tag color="blue">{item.action}</Tag>}
        {item.tool_label && <Tag>{item.tool_label}</Tag>}
        {settledStatus && <Tag color={settledStatus === 'executed' ? 'green' : settledStatus === 'failed' ? 'red' : 'gold'}>{settledStatus}</Tag>}
      </Space>
      <div style={{ fontSize: 12, color: 'var(--yx-text-secondary)', marginBottom: 8 }}>
        {t('pendingAction.hint')}
      </div>

      {item.contract_executable === false && (
        <Alert
          type="warning"
          showIcon
          message={t('pendingAction.notExecutable')}
          description={(item.reasons || []).join('；') || undefined}
          style={{ marginBottom: 8 }}
        />
      )}
      {item.contract_executable !== false && !item.can_execute && (
        <Alert
          type="info"
          showIcon
          message={t('pendingAction.needManager')}
          style={{ marginBottom: 8 }}
        />
      )}
      {item.contract_executable !== false && item.can_execute && !item.executable && !terminal && (
        <Alert
          type="info"
          showIcon
          message={t('pendingAction.pendingUnavailable')}
          style={{ marginBottom: 8 }}
        />
      )}

      {settledResult && (
        <Alert
          type={settledResult.status === 'executed' ? 'success' : settledResult.status === 'failed' ? 'warning' : 'info'}
          showIcon
          message={
            settledResult.status === 'failed'
              ? t('pendingAction.failedUnknown')
              : settledResult.status === 'executed'
                ? t('pendingAction.executed')
                : settledResult.status
          }
          description={settledResult.error || undefined}
          style={{ marginBottom: 8 }}
        />
      )}
      {settledResult?.output != null && (
        <div style={{ marginBottom: 8 }}>
          <Button
            type="link"
            size="small"
            style={{ paddingInline: 0 }}
            onClick={() => setOutputOpen((prev) => !prev)}
          >
            {outputOpen ? t('pendingAction.hideOutput') : t('pendingAction.showOutput')}
          </Button>
          {outputOpen && (
            <pre
              style={{
                margin: 0,
                maxHeight: 320,
                overflow: 'auto',
                padding: '8px 10px',
                borderRadius: 6,
                background: 'var(--yx-bg-subtle)',
                border: '1px solid #e5e7eb',
                fontSize: 12,
                lineHeight: 1.6,
                whiteSpace: 'pre-wrap',
                wordBreak: 'break-all',
              }}
            >
              {prettyOutput(settledResult.output)}
            </pre>
          )}
        </div>
      )}
      {item.status && !result && !historyResult && ['executed', 'failed'].includes(item.status) && (
        <Alert
          type={item.status === 'executed' ? 'success' : 'warning'}
          showIcon
          message={item.status === 'failed' ? t('pendingAction.failedUnknown') : t('pendingAction.executed')}
          style={{ marginBottom: 8 }}
        />
      )}

      {!terminal && item.executable && item.can_execute && (
        <>
          {missingRequired.length > 0 && (
            <div style={{ marginBottom: 8 }}>
              <div style={{ fontSize: 12, fontWeight: 600, marginBottom: 4 }}>{t('pendingAction.inputsTitle')}</div>
              {missingRequired.map((field) => (
                <div key={field.name} style={{ marginBottom: 6 }}>
                  <div style={{ fontSize: 12, color: 'var(--yx-text-secondary)' }}>
                    {field.name}{field.required ? ' *' : ''}{field.description ? ` · ${field.description}` : ''}
                  </div>
                  <Input
                    size="small"
                    value={inputs[field.name] || ''}
                    onChange={(e) => {
                      setInputs((prev) => ({ ...prev, [field.name]: e.target.value }));
                      invalidate();
                    }}
                  />
                </div>
              ))}
            </div>
          )}
          {providedKeys.size > 0 && (
            <div style={{ fontSize: 12, color: 'var(--yx-text-muted)', marginBottom: 8 }}>
              {t('pendingAction.providedByQa', { keys: Array.from(providedKeys).join('、') })}
            </div>
          )}

          {checklist.length > 0 && (
            <div style={{ marginBottom: 8 }}>
              <div style={{ fontSize: 12, fontWeight: 600, marginBottom: 4 }}>{t('pendingAction.checklistTitle')}</div>
              {checklist.map((c) => {
                const key = CONFIRM_KEY[c.kind] || c.kind;
                return (
                  <div key={key} style={{ marginBottom: 6 }}>
                    <ul style={{ margin: '0 0 4px', paddingLeft: 18, fontSize: 13 }}>
                      {c.items.map((entry) => <li key={entry}>{entry}</li>)}
                    </ul>
                    <Checkbox
                      checked={confirmations[key] === true}
                      disabled={busy != null}
                      onChange={(e) => {
                        setConfirmations((prev) => ({ ...prev, [key]: e.target.checked }));
                        invalidate();
                      }}
                    >
                      {c.kind === 'authorization'
                        ? t('pendingAction.confirmAuthorization')
                        : t('pendingAction.confirmPreconditions')}
                    </Checkbox>
                  </div>
                );
              })}
            </div>
          )}

          {preflight?.arguments_view?.length ? (
            <div style={{ marginBottom: 8 }}>
              <div style={{ fontSize: 12, fontWeight: 600, marginBottom: 4 }}>{t('pendingAction.argumentsTitle')}</div>
              <ul style={{ margin: 0, paddingLeft: 18, fontSize: 12 }}>
                {preflight.arguments_view.map((arg) => (
                  <li key={arg.key}>
                    <code>{arg.key}</code> = <span>{asText(arg.value).slice(0, 160)}</span>{' '}
                    <Tag>{arg.source}</Tag>
                  </li>
                ))}
              </ul>
            </div>
          ) : null}

          {error && <Alert type="error" showIcon message={error} style={{ marginBottom: 8 }} />}

          <Space>
            <Button
              size="small"
              loading={busy === 'dry'}
              disabled={!allConfirmed || busy != null}
              onClick={() => submit(true)}
            >
              {t('pendingAction.preflight')}
            </Button>
            <Button
              size="small"
              type="primary"
              loading={busy === 'run'}
              disabled={!allConfirmed || !preflightOk || busy != null}
              onClick={() => submit(false)}
            >
              {t('pendingAction.execute')}
            </Button>
          </Space>
          {!preflightOk && (
            <Typography.Text type="secondary" style={{ fontSize: 12, marginLeft: 8 }}>
              {preflight
                ? t('pendingAction.preflightNotPassed', { status: preflight.status })
                : t('pendingAction.preflightRequired')}
            </Typography.Text>
          )}
        </>
      )}
    </div>
  );
}
