import { useCallback, useEffect, useRef, useState } from 'react';
import {
  Alert,
  Button,
  Descriptions,
  Empty,
  Form,
  InputNumber,
  Modal,
  Segmented,
  Select,
  Skeleton,
  Space,
  Tag,
  message,
} from 'antd';
import { ReloadOutlined } from '@ant-design/icons';
import { useTranslation } from 'react-i18next';
import { getRetentionPolicy, setRetentionPolicy } from '@/api/dataGovernance';
import { listKbOptions } from '@/api/dataOperations';
import type { DgRetentionPolicy } from '../types';
import {
  actionErrorMessage,
  buildRetentionPatch,
  errorMeta,
  retentionScopeDraft,
  retentionSourceLabelKey,
  retentionValueText,
} from '../utils';
import type { RetentionDraft } from '../utils';

interface Props {
  open: boolean;
  onClose: () => void;
  onSaved?: () => void;
}

type PolicyScope = 'tenant' | 'kb';

const EMPTY_DRAFT: RetentionDraft = { retentionDays: null, graceDays: null };

/**
 * 保留策略弹窗：编辑租户默认或某个知识库的覆盖值。
 *
 * 两级都只是「覆盖」——留空继承上级（租户 → 系统默认），已生效值在上方展示，
 * 避免用户把留空误读成「不保留」。
 */
export default function RetentionPolicyModal({ open, onClose, onSaved }: Props) {
  const { t } = useTranslation();
  const tRef = useRef(t);
  useEffect(() => {
    tRef.current = t;
  }, [t]);

  const [scope, setScope] = useState<PolicyScope>('tenant');
  const [kbId, setKbId] = useState<string | undefined>();
  const [kbs, setKbs] = useState<Array<{ id: string; name: string }>>([]);
  const [kbLoading, setKbLoading] = useState(false);
  const [kbError, setKbError] = useState<string | null>(null);
  const [policy, setPolicy] = useState<DgRetentionPolicy | null>(null);
  const [draft, setDraft] = useState<RetentionDraft>(EMPTY_DRAFT);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // 作用域/KB 快速切换时，只有最后一次请求允许回写。
  const loadSeq = useRef(0);
  const saveSeq = useRef(0);
  const kbSeq = useRef(0);
  // 保存期间输入被禁用，仍以 ref 校验「回写前作用域未变」。
  const scopeRef = useRef(scope);
  const kbRef = useRef(kbId);
  useEffect(() => {
    scopeRef.current = scope;
  }, [scope]);
  useEffect(() => {
    kbRef.current = kbId;
  }, [kbId]);

  const loadPolicy = useCallback(async () => {
    if (!open) return;
    // 知识库作用域还没选 KB：不请求（scope_id 必填），交给空态提示。
    if (scope === 'kb' && !kbId) {
      loadSeq.current += 1;
      setPolicy(null);
      setDraft(EMPTY_DRAFT);
      setError(null);
      setLoading(false);
      return;
    }
    const my = ++loadSeq.current;
    setLoading(true);
    setError(null);
    try {
      const next = await getRetentionPolicy(scope, scope === 'kb' ? kbId : undefined);
      if (my !== loadSeq.current) return;
      setPolicy(next);
      setDraft(retentionScopeDraft(next, scope));
    } catch (err: unknown) {
      if (my !== loadSeq.current) return;
      const meta = errorMeta(err);
      setPolicy(null);
      setError(
        meta.status === 403
          ? tRef.current('dataGovernance.policy.viewForbidden')
          : meta.status !== undefined && meta.message
            ? meta.message
            : tRef.current('dataGovernance.policy.loadFailed'),
      );
    } finally {
      if (my === loadSeq.current) setLoading(false);
    }
  }, [open, scope, kbId]);

  useEffect(() => {
    void loadPolicy();
  }, [loadPolicy]);

  // 关闭时让在途请求失效，清掉上一轮展示，并回到租户默认作用域。
  useEffect(() => {
    if (open) return;
    loadSeq.current += 1;
    saveSeq.current += 1;
    kbSeq.current += 1;
    setScope('tenant');
    setKbId(undefined);
    setKbs([]);
    setKbLoading(false);
    setKbError(null);
    setPolicy(null);
    setDraft(EMPTY_DRAFT);
    setError(null);
    setLoading(false);
    setSaving(false);
  }, [open]);

  const loadKbs = useCallback(async () => {
    if (!open) return;
    const my = ++kbSeq.current;
    setKbLoading(true);
    setKbError(null);
    try {
      const next = await listKbOptions();
      if (my !== kbSeq.current) return;
      setKbs(next);
    } catch {
      if (my !== kbSeq.current) return;
      setKbs([]);
      setKbError(tRef.current('dataGovernance.policy.kbLoadFailed'));
    } finally {
      if (my === kbSeq.current) setKbLoading(false);
    }
  }, [open]);

  useEffect(() => {
    if (!open) return;
    void loadKbs();
  }, [open, loadKbs]);

  const save = async () => {
    if (saving) return;
    if (scope === 'kb' && !kbId) {
      message.info(t('dataGovernance.policy.selectKb'));
      return;
    }
    if (!policy) return;
    const patch = buildRetentionPatch(retentionScopeDraft(policy, scope), draft);
    if (!Object.keys(patch).length) {
      message.info(t('dataGovernance.drawer.noChange'));
      return;
    }
    const originScope = scope;
    const originKb = kbId;
    const my = ++saveSeq.current;
    setSaving(true);
    try {
      // 重载签名把 tenant 与 kb 分开，避免 scope 联合类型直接透传。
      const next =
        originScope === 'kb'
          ? await setRetentionPolicy('kb', originKb as string, patch)
          : await setRetentionPolicy('tenant', undefined, patch);
      // 服务端写入已完成：即使弹窗已切走，也让父级刷新。
      onSaved?.();
      if (
        my !== saveSeq.current ||
        scopeRef.current !== originScope ||
        kbRef.current !== originKb
      ) {
        return;
      }
      setPolicy(next);
      setDraft(retentionScopeDraft(next, scope));
      message.success(tRef.current('dataGovernance.policy.saved'));
      onClose();
    } catch (err: unknown) {
      if (my !== saveSeq.current) return;
      const text = actionErrorMessage(err, {
        forbiddenKey: 'dataGovernance.policy.forbidden',
        fallbackKey: 'dataGovernance.policy.saveFailed',
      });
      message.error('key' in text ? tRef.current(text.key) : text.message);
    } finally {
      if (my === saveSeq.current) setSaving(false);
    }
  };

  const knob = (
    key: 'retentionDays' | 'graceDays',
    label: string,
    inputId: string,
  ) => (
    <Form.Item label={label} htmlFor={inputId}>
      <InputNumber
        id={inputId}
        min={0}
        precision={0}
        style={{ width: '100%' }}
        disabled={saving}
        value={draft[key] ?? undefined}
        placeholder={t('dataGovernance.lifecycle.inherit')}
        addonAfter={t('dataGovernance.retention.daysUnit')}
        onChange={(value) =>
          setDraft((current) => ({
            ...current,
            [key]: typeof value === 'number' ? value : null,
          }))
        }
      />
    </Form.Item>
  );

  const showEmpty = scope === 'kb' && !kbId;

  return (
    <Modal
      open={open}
      title={t('dataGovernance.policy.title')}
      onCancel={onClose}
      onOk={() => void save()}
      okText={t('dataGovernance.policy.save')}
      cancelText={t('common.cancel')}
      confirmLoading={saving}
      okButtonProps={{ disabled: showEmpty || loading || saving || Boolean(error) }}
      destroyOnHidden
      width={560}
    >
      <Space direction="vertical" size={16} style={{ width: '100%' }}>
        <Segmented
          value={scope}
          disabled={saving}
          options={[
            { value: 'tenant', label: t('dataGovernance.policy.tenant') },
            { value: 'kb', label: t('dataGovernance.policy.kb') },
          ]}
          onChange={(value) => setScope(value as PolicyScope)}
        />

        {scope === 'kb' && (
          <>
            <Select
              showSearch
              allowClear
              disabled={saving}
              loading={kbLoading}
              optionFilterProp="label"
              aria-label={t('dataGovernance.policy.selectKb')}
              placeholder={t('dataGovernance.policy.selectKb')}
              value={kbId}
              options={kbs.map((kb) => ({ value: kb.id, label: kb.name }))}
              onChange={(value) => setKbId(value)}
              style={{ width: '100%' }}
            />
            {kbError && (
              <Alert
                type="warning"
                showIcon
                title={kbError}
                action={
                  <Button
                    size="small"
                    icon={<ReloadOutlined />}
                    onClick={() => void loadKbs()}
                  >
                    {t('common.retry')}
                  </Button>
                }
              />
            )}
          </>
        )}

        {loading ? (
          <Skeleton active paragraph={{ rows: 4 }} />
        ) : error ? (
          <Alert
            type="error"
            showIcon
            title={error}
            action={
              <Button size="small" icon={<ReloadOutlined />} onClick={() => void loadPolicy()}>
                {t('common.retry')}
              </Button>
            }
          />
        ) : showEmpty ? (
          <Empty description={t('dataGovernance.policy.empty')} />
        ) : policy ? (
          <>
            <Descriptions title={t('dataGovernance.policy.effective')} column={2} size="small">
              <Descriptions.Item label={t('dataGovernance.lifecycle.retentionDays')}>
                {retentionValueText(t, policy.retentionDays)}
              </Descriptions.Item>
              <Descriptions.Item label={t('dataGovernance.lifecycle.graceDays')}>
                {t('dataGovernance.retention.days', { count: policy.graceDays })}
              </Descriptions.Item>
              <Descriptions.Item label={t('dataGovernance.lifecycle.effectiveFrom')}>
                <Tag color="blue">{t(retentionSourceLabelKey(policy.effectiveFrom))}</Tag>
              </Descriptions.Item>
              <Descriptions.Item label={t('dataGovernance.lifecycle.source')}>
                {t(retentionSourceLabelKey(policy.sources.retentionDays))} /{' '}
                {t(retentionSourceLabelKey(policy.sources.graceDays))}
              </Descriptions.Item>
            </Descriptions>

            <Form layout="vertical">
              <div className="yx-dg-lifecycle-fields">
                {knob(
                  'retentionDays',
                  t('dataGovernance.lifecycle.retentionDays'),
                  'yx-dg-policy-retention-days',
                )}
                {knob(
                  'graceDays',
                  t('dataGovernance.lifecycle.graceDays'),
                  'yx-dg-policy-grace-days',
                )}
              </div>
            </Form>
            <div className="yx-dg-muted">{t('dataGovernance.policy.inheritHint')}</div>
            <Alert
              type="info"
              showIcon
              description={
                <ul className="yx-dg-policy-hints">
                  <li>{t('dataGovernance.policy.hintRetention')}</li>
                  <li>{t('dataGovernance.policy.hintGrace')}</li>
                  <li>{t('dataGovernance.policy.hintDestroy')}</li>
                </ul>
              }
            />
          </>
        ) : null}
      </Space>
    </Modal>
  );
}
