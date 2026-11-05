// 本体动作试执行抽屉：预览执行计划 → 填入参 → 预检（dry_run，不调用接口）→ 逐项确认 → 执行。
// 执行会真实调用对方接口；权限只认调用人对该知识库的写权限（后端判定）。
import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import {
  Alert,
  Button,
  Checkbox,
  Descriptions,
  Drawer,
  Empty,
  Input,
  Modal,
  Space,
  Spin,
  Switch,
  Table,
  Tag,
  Typography,
} from 'antd';
import {
  executeOntologyAction,
  resolveActionExecution,
  type ExecuteResult,
  type ExecuteStatus,
  type ExecutionPlan,
  type ExecutionVerdict,
} from '@/api/ontologyAction';

const { Text, Paragraph } = Typography;

interface Props {
  open: boolean;
  kbId: string;
  actionName: string;
  onClose: () => void;
}

type ParamSource = 'ontology' | 'attr_empty' | 'input' | 'optional';

interface ParamRow {
  /** 工具入参名 */
  param: string;
  /** 调用方输入键（取值来源是 $input.* 时与入参名不同） */
  inputKey: string;
  source: ParamSource;
  required: boolean;
  type?: string;
  value?: unknown;
}

const VERDICT_COLOR: Record<ExecutionVerdict, string> = {
  allow: 'green',
  confirm: 'gold',
  manual: 'orange',
  refuse: 'red',
};

const STATUS_COLOR: Record<ExecuteStatus, string> = {
  executed: 'green',
  planned: 'blue',
  blocked: 'red',
  manual: 'orange',
  needs_confirmation: 'gold',
  invalid_input: 'gold',
  no_runtime: 'default',
  failed: 'red',
};

const STATUS_KEY: Record<ExecuteStatus, string> = {
  executed: 'statusExecuted',
  planned: 'statusPlanned',
  blocked: 'statusBlocked',
  manual: 'statusManual',
  needs_confirmation: 'statusNeedsConfirmation',
  invalid_input: 'statusInvalidInput',
  no_runtime: 'statusNoRuntime',
  failed: 'statusFailed',
};

const CONFIRM_KEY: Record<string, string> = { authorization: 'authorization', precondition: 'preconditions' };

/** 按入参 schema 声明的类型转换输入值；转换不了的保留原文（由对方接口或必填校验兜底）。 */
function coerce(raw: unknown, type?: string): unknown {
  if (typeof raw !== 'string') return raw;
  const text = raw.trim();
  if (!text) return undefined;
  if (type === 'integer' || type === 'number') {
    const n = Number(text);
    return Number.isFinite(n) ? n : text;
  }
  if (type === 'object' || type === 'array') {
    try {
      return JSON.parse(text);
    } catch {
      return text;
    }
  }
  return text;
}

/** 由执行计划推出入参表：取自本体的只展示；$input / 本体属性为空 / schema 声明但未映射的需要填写。 */
function buildParamRows(plan: ExecutionPlan | null): ParamRow[] {
  if (!plan?.binding) return [];
  const schema = plan.binding.input_schema || {};
  const props = (schema.properties || {}) as Record<string, { type?: string }>;
  const required = new Set<string>((schema.required || []) as string[]);
  const { resolved = {}, input_refs = {}, unresolved_attrs = [] } = plan.parameters || ({} as any);
  const rows: ParamRow[] = [];
  const seen = new Set<string>();
  const push = (row: ParamRow) => {
    if (seen.has(row.param)) return;
    seen.add(row.param);
    rows.push(row);
  };
  Object.entries(resolved).forEach(([param, value]) =>
    push({ param, inputKey: param, source: 'ontology', required: required.has(param), type: props[param]?.type, value }),
  );
  Object.entries(input_refs).forEach(([param, key]) =>
    push({ param, inputKey: String(key), source: 'input', required: required.has(param), type: props[param]?.type }),
  );
  (unresolved_attrs as string[]).forEach((param) =>
    push({ param, inputKey: param, source: 'attr_empty', required: required.has(param), type: props[param]?.type }),
  );
  Object.keys(props).forEach((param) =>
    push({ param, inputKey: param, source: 'optional', required: required.has(param), type: props[param]?.type }),
  );
  return rows;
}

function JsonBlock({ value }: { value: unknown }) {
  const text = typeof value === 'string' ? value : JSON.stringify(value, null, 2);
  return (
    <pre
      style={{
        margin: 0,
        maxHeight: 280,
        overflow: 'auto',
        fontSize: 12,
        background: '#f8fafc',
        border: '1px solid #e2e8f0',
        borderRadius: 6,
        padding: 8,
        whiteSpace: 'pre-wrap',
        wordBreak: 'break-all',
      }}
    >
      {text}
    </pre>
  );
}

export default function ActionExecuteDrawer({ open, kbId, actionName, onClose }: Props) {
  const { t } = useTranslation();
  const [plan, setPlan] = useState<ExecutionPlan | null>(null);
  const [loading, setLoading] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [values, setValues] = useState<Record<string, unknown>>({});
  const [confirmations, setConfirmations] = useState<Record<string, boolean>>({});
  const [dryResult, setDryResult] = useState<ExecuteResult | null>(null);
  const [execResult, setExecResult] = useState<ExecuteResult | null>(null);
  const [runError, setRunError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  // 防重复提交：按钮 loading 之外再加一道同步闸，连点也只发一次
  const inFlight = useRef(false);

  const load = useCallback(async () => {
    setLoading(true);
    setLoadError(null);
    try {
      setPlan(await resolveActionExecution(kbId, actionName));
    } catch (e: any) {
      setPlan(null);
      setLoadError(e?.message || t('actionExecute.loadFailed'));
    } finally {
      setLoading(false);
    }
  }, [actionName, kbId, t]);

  useEffect(() => {
    if (!open) return;
    setValues({});
    setConfirmations({});
    setDryResult(null);
    setExecResult(null);
    setRunError(null);
    load();
  }, [load, open]);

  const rows = useMemo(() => buildParamRows(plan), [plan]);
  const verdict = plan?.decision?.verdict;
  const blocked = verdict === 'manual' || verdict === 'refuse' || !plan?.binding;
  const checklist = plan?.decision?.checklist || [];
  const allConfirmed = checklist.every((c) => confirmations[CONFIRM_KEY[c.kind] || c.kind] === true);
  const toolLabel = plan?.binding?.tool_label || plan?.binding?.tool_name || '';

  // 入参或确认变了，上一次预检作废：必须对「将要执行的这组值」预检通过才能执行
  const invalidateDryRun = () => {
    setDryResult(null);
    setExecResult(null);
  };

  const inputs = useMemo(() => {
    const out: Record<string, unknown> = {};
    rows.forEach((r) => {
      if (r.source === 'ontology') return;
      const v = coerce(values[r.inputKey], r.type);
      if (v !== undefined) out[r.inputKey] = v;
    });
    return out;
  }, [rows, values]);

  const run = async (dryRun: boolean) => {
    if (inFlight.current) return;
    inFlight.current = true;
    setSubmitting(true);
    setRunError(null);
    try {
      const res = await executeOntologyAction({
        knowledgeBaseId: kbId,
        actionName,
        inputs,
        confirmations,
        dryRun,
      });
      if (dryRun) {
        setDryResult(res);
        setExecResult(null);
      } else {
        setExecResult(res);
      }
    } catch (e: any) {
      setRunError(e?.message || t('common.operationFailed'));
    } finally {
      inFlight.current = false;
      setSubmitting(false);
    }
  };

  const confirmAndExecute = () => {
    Modal.confirm({
      title: t('actionExecute.executeConfirmTitle'),
      content: t('actionExecute.executeConfirmDesc', { tool: toolLabel }),
      okText: t('actionExecute.execute'),
      okButtonProps: { danger: true },
      cancelText: t('common.cancel'),
      onOk: () => run(false),
    });
  };

  const dryPassed = dryResult?.status === 'planned';

  const renderInput = (r: ParamRow) => {
    if (r.source === 'ontology') {
      return <Text code>{typeof r.value === 'string' ? r.value : JSON.stringify(r.value)}</Text>;
    }
    const onChange = (v: unknown) => {
      setValues((prev) => ({ ...prev, [r.inputKey]: v }));
      invalidateDryRun();
    };
    if (r.type === 'boolean') {
      return (
        <Switch
          size="small"
          checked={values[r.inputKey] === true}
          disabled={blocked || submitting}
          onChange={(c) => onChange(c)}
        />
      );
    }
    return (
      <Input
        size="small"
        value={(values[r.inputKey] as string) ?? ''}
        disabled={blocked || submitting}
        placeholder={r.type ? t('actionExecute.jsonValueHint', { type: r.type }) : undefined}
        onChange={(e) => onChange(e.target.value)}
      />
    );
  };

  const sourceTag = (r: ParamRow) => {
    switch (r.source) {
      case 'ontology':
        return <Tag color="blue">{t('actionExecute.paramFromOntology')}</Tag>;
      case 'attr_empty':
        return <Tag color="orange">{t('actionExecute.paramAttrEmpty')}</Tag>;
      case 'input':
        return <Tag color="geekblue">{t('actionExecute.paramFromInput')}</Tag>;
      default:
        return <Tag>{t('actionExecute.paramOptional')}</Tag>;
    }
  };

  const renderResult = (res: ExecuteResult, isDry: boolean) => (
    <div style={{ marginTop: 16 }}>
      <Typography.Title level={5} style={{ marginBottom: 8 }}>
        {isDry ? t('actionExecute.resultDryRun') : t('actionExecute.result')}
      </Typography.Title>
      <Descriptions size="small" column={1} bordered>
        <Descriptions.Item label={t('actionExecute.status')}>
          <Space size={4} wrap>
            <Tag color={STATUS_COLOR[res.status] || 'default'}>
              {t(`actionExecute.${STATUS_KEY[res.status] || 'statusFailed'}`)}
            </Tag>
            {res.duration_ms != null && (
              <Text type="secondary">{t('actionExecute.duration', { ms: res.duration_ms })}</Text>
            )}
          </Space>
        </Descriptions.Item>
        {res.http_status != null && (
          <Descriptions.Item label={t('actionExecute.httpStatus')}>{res.http_status}</Descriptions.Item>
        )}
        {res.error && <Descriptions.Item label={t('actionExecute.error')}>{res.error}</Descriptions.Item>}
        {!!res.missing_inputs?.length && (
          <Descriptions.Item label={t('actionExecute.missingInputs')}>
            <Space size={4} wrap>
              {res.missing_inputs.map((m) => (
                <Tag key={m} color="gold">
                  {m}
                </Tag>
              ))}
            </Space>
          </Descriptions.Item>
        )}
        {!!res.pending_confirmations?.length && (
          <Descriptions.Item label={t('actionExecute.pendingConfirmations')}>
            {res.pending_confirmations.map((c) => c.items.join('；')).join('；')}
          </Descriptions.Item>
        )}
        {isDry && res.arguments && (
          <Descriptions.Item label={t('actionExecute.arguments')}>
            <JsonBlock value={res.arguments} />
          </Descriptions.Item>
        )}
        {!isDry && res.output !== undefined && res.output !== null && (
          <Descriptions.Item label={t('actionExecute.output')}>
            {res.output_truncated && (
              <Tag color="orange" style={{ marginBottom: 6 }}>
                {t('actionExecute.truncated')}
              </Tag>
            )}
            <JsonBlock value={res.output} />
          </Descriptions.Item>
        )}
        {!isDry && !!res.post_actions?.follow_ups?.length && (
          <Descriptions.Item label={t('actionExecute.followUps')}>
            {res.post_actions.follow_ups.join('；')}
          </Descriptions.Item>
        )}
        {!isDry && !!res.post_actions?.failure_policy && (
          <Descriptions.Item label={t('actionExecute.failurePolicy')}>
            {res.post_actions.failure_policy}
          </Descriptions.Item>
        )}
      </Descriptions>
      <Text type="secondary" style={{ fontSize: 12, display: 'block', marginTop: 6 }}>
        {t('actionExecute.audited')}
      </Text>
    </div>
  );

  return (
    <Drawer
      title={t('actionExecute.title', { name: actionName })}
      open={open}
      onClose={onClose}
      width={640}
      destroyOnHidden
      maskClosable={!submitting}
      footer={
        <Space style={{ display: 'flex', justifyContent: 'flex-end' }}>
          <Button onClick={onClose} disabled={submitting}>
            {t('common.cancel')}
          </Button>
          <Button
            disabled={blocked || loading || !allConfirmed}
            loading={submitting && !dryPassed}
            onClick={() => run(true)}
          >
            {t('actionExecute.dryRun')}
          </Button>
          <Button
            type="primary"
            danger
            disabled={blocked || loading || !allConfirmed || !dryPassed}
            loading={submitting && dryPassed}
            title={dryPassed ? undefined : t('actionExecute.executeNeedDryRun')}
            onClick={confirmAndExecute}
          >
            {t('actionExecute.execute')}
          </Button>
        </Space>
      }
    >
      {loading && (
        <div style={{ textAlign: 'center', padding: 48 }}>
          <Spin />
        </div>
      )}
      {!loading && loadError && (
        <Alert
          type="error"
          showIcon
          message={loadError}
          action={<a onClick={load}>{t('common.retry')}</a>}
        />
      )}
      {!loading && !loadError && !plan && <Empty />}
      {!loading && !loadError && plan && (
        <>
          <Alert
            type="warning"
            showIcon
            style={{ marginBottom: 12 }}
            message={t('actionExecute.realCallWarning')}
            description={t('actionExecute.realCallWarningDesc')}
          />

          <Descriptions size="small" column={1} bordered style={{ marginBottom: 12 }}>
            <Descriptions.Item label={t('actionExecute.verdict')}>
              {verdict && (
                <Tag color={VERDICT_COLOR[verdict]}>
                  {t(`actionExecute.verdict${verdict[0].toUpperCase()}${verdict.slice(1)}`)}
                </Tag>
              )}
            </Descriptions.Item>
            {!!plan.decision?.reasons?.length && (
              <Descriptions.Item label={t('actionExecute.reasons')}>
                <ul style={{ margin: 0, paddingLeft: 18 }}>
                  {plan.decision.reasons.map((r) => (
                    <li key={r.code}>{r.message}</li>
                  ))}
                </ul>
              </Descriptions.Item>
            )}
            {plan.binding && (
              <Descriptions.Item label={t('actionExecute.tool')}>
                <Space size={4} wrap>
                  <Tag color={plan.binding.tool_source === 'kb' ? 'cyan' : 'geekblue'}>
                    {plan.binding.tool_source === 'kb' ? t('toolBinding.sourceKb') : t('toolBinding.sourcePlatform')}
                  </Tag>
                  <span>{toolLabel}</span>
                  {plan.binding.read_only ? (
                    <Tag color="cyan">{t('toolRuntime.readOnlyTag')}</Tag>
                  ) : (
                    <Tag color="volcano">{t('toolRuntime.writeTag')}</Tag>
                  )}
                </Space>
              </Descriptions.Item>
            )}
          </Descriptions>

          {blocked && (
            <Alert
              type="error"
              showIcon
              style={{ marginBottom: 12 }}
              message={verdict === 'refuse' ? t('actionExecute.blockedRefuse') : t('actionExecute.blockedManual')}
            />
          )}

          <Typography.Title level={5}>{t('actionExecute.params')}</Typography.Title>
          {rows.length ? (
            <Table<ParamRow>
              size="small"
              rowKey="param"
              pagination={false}
              dataSource={rows}
              style={{ marginBottom: 12 }}
              columns={[
                {
                  title: t('actionExecute.params'),
                  key: 'param',
                  width: 200,
                  render: (_, r) => (
                    <Space direction="vertical" size={2}>
                      <Text strong>
                        {r.param}
                        {r.required && <Text type="danger"> *</Text>}
                      </Text>
                      {r.inputKey !== r.param && (
                        <Text type="secondary" style={{ fontSize: 12 }}>
                          $input.{r.inputKey}
                        </Text>
                      )}
                      {sourceTag(r)}
                    </Space>
                  ),
                },
                { title: '', key: 'value', render: (_, r) => renderInput(r) },
              ]}
            />
          ) : (
            <Paragraph type="secondary">{t('actionExecute.noParams')}</Paragraph>
          )}

          {!!checklist.length && (
            <>
              <Typography.Title level={5}>{t('actionExecute.checklist')}</Typography.Title>
              <Paragraph type="secondary" style={{ marginBottom: 6 }}>
                {t('actionExecute.checklistHint')}
              </Paragraph>
              {checklist.map((c) => {
                const key = CONFIRM_KEY[c.kind] || c.kind;
                return (
                  <div key={key} style={{ marginBottom: 8 }}>
                    <ul style={{ margin: '0 0 4px', paddingLeft: 18 }}>
                      {c.items.map((item) => (
                        <li key={item}>{item}</li>
                      ))}
                    </ul>
                    <Checkbox
                      checked={confirmations[key] === true}
                      disabled={blocked || submitting}
                      onChange={(e) => {
                        setConfirmations((prev) => ({ ...prev, [key]: e.target.checked }));
                        invalidateDryRun();
                      }}
                    >
                      {c.kind === 'authorization'
                        ? t('actionExecute.confirmAuthorization')
                        : t('actionExecute.confirmPreconditions')}
                    </Checkbox>
                  </div>
                );
              })}
            </>
          )}

          {runError && <Alert type="error" showIcon style={{ marginTop: 12 }} message={runError} />}
          {dryResult && renderResult(dryResult, true)}
          {execResult && renderResult(execResult, false)}
        </>
      )}
    </Drawer>
  );
}
