/** 治理策略新增/编辑弹窗。 */
import { Form, Input, InputNumber, Modal, Select, Switch } from 'antd';
import { useEffect } from 'react';
import { useTranslation } from 'react-i18next';
import { LEVEL_ORDER } from './governanceModel';
import type { GovernancePolicy } from '@/api/governance';

export interface GovernancePolicyForm {
  name: string;
  description?: string;
  effect: string;
  priority: number;
  resource_type: string;
  table_pattern?: string;
  column_pattern?: string;
  tag_name?: string;
  min_clearance: string;
  mask_type?: string;
  row_filter?: string;
  valid_from?: string;
  valid_to?: string;
  enabled: boolean;
  note?: string;
}

interface Props {
  open: boolean;
  policy?: GovernancePolicy | null;
  submitting?: boolean;
  onCancel: () => void;
  onSubmit: (values: GovernancePolicyForm) => void;
}

const levelOptions = (t: (k: string) => string) => LEVEL_ORDER.map((lv) => ({
  value: lv,
  label: t(`domainKnowledge.governance.level${lv[0].toUpperCase()}${lv.slice(1)}`),
}));

export default function PolicyModal({
  open, policy, submitting, onCancel, onSubmit,
}: Props) {
  const { t } = useTranslation();
  const [form] = Form.useForm<GovernancePolicyForm>();
  const editing = !!policy;

  useEffect(() => {
    if (!open) return;
    form.resetFields();
    form.setFieldsValue(policy ? {
      name: policy.name,
      description: policy.description ?? undefined,
      effect: policy.effect,
      priority: policy.priority,
      resource_type: policy.resource_type,
      table_pattern: policy.table_pattern ?? undefined,
      column_pattern: policy.column_pattern ?? undefined,
      tag_name: policy.tag_name ?? undefined,
      min_clearance: policy.min_clearance,
      mask_type: policy.mask_type ?? undefined,
      row_filter: policy.row_filter ?? undefined,
      valid_from: policy.valid_from ?? undefined,
      valid_to: policy.valid_to ?? undefined,
      enabled: !!policy.enabled,
      note: policy.note ?? undefined,
    } : {
      effect: 'deny', priority: 100, resource_type: 'table',
      min_clearance: 'public', enabled: true,
    });
  }, [open, policy, form]);

  return (
    <Modal
      open={open}
      title={t(editing
        ? 'domainKnowledge.governance.editPolicy'
        : 'domainKnowledge.governance.addPolicy')}
      okText={t('common.confirm')}
      cancelText={t('common.cancel')}
      confirmLoading={submitting}
      onCancel={onCancel}
      onOk={() => form.submit()}
      width={640}
      destroyOnHidden
    >
      <Form form={form} layout="vertical" onFinish={onSubmit}>
        <Form.Item name="name" label={t('domainKnowledge.governance.policyName')}
                   rules={[{ required: true, whitespace: true }]}>
          <Input />
        </Form.Item>
        <Form.Item name="effect" label={t('domainKnowledge.governance.effect')}
                   rules={[{ required: true }]}>
          <Select options={['allow', 'deny', 'mask', 'row_filter'].map((e) => ({
            value: e,
            label: t(`domainKnowledge.governance.effect${e === 'row_filter' ? 'RowFilter' : e[0].toUpperCase() + e.slice(1)}`),
          }))} />
        </Form.Item>
        <Form.Item name="resource_type" label={t('domainKnowledge.governance.resourceType')}
                   rules={[{ required: true }]}>
          <Select options={['table', 'column', 'tag'].map((r) => ({
            value: r,
            label: t(`domainKnowledge.governance.resource${r[0].toUpperCase()}${r.slice(1)}`),
          }))} />
        </Form.Item>
        <Form.Item name="table_pattern" label={t('domainKnowledge.governance.tablePattern')}>
          <Input placeholder="public.orders 或 *" />
        </Form.Item>
        <Form.Item name="column_pattern" label={t('domainKnowledge.governance.columnPattern')}>
          <Input placeholder="amount 或 *" />
        </Form.Item>
        <Form.Item name="tag_name" label={t('domainKnowledge.governance.tagName')}>
          <Input placeholder="PII" />
        </Form.Item>
        <Form.Item name="min_clearance" label={t('domainKnowledge.governance.minClearance')}
                   rules={[{ required: true }]}>
          <Select options={levelOptions(t)} />
        </Form.Item>
        <Form.Item name="mask_type" label={t('domainKnowledge.governance.maskType')}>
          <Select allowClear options={[{ value: 'null', label: 'NULL' }, { value: 'redact', label: '***' }]} />
        </Form.Item>
        <Form.Item name="row_filter" label={t('domainKnowledge.governance.rowFilter')}
                   rules={[({ getFieldValue }) => ({
                     validator(_, value) {
                       return getFieldValue('effect') !== 'row_filter' || String(value || '').trim()
                         ? Promise.resolve()
                         : Promise.reject(new Error(t('domainKnowledge.lineage.requiredField')));
                     },
                   })]}>
          <Input.TextArea rows={2} maxLength={1000}
                          placeholder="tenant_id = 'x'" />
        </Form.Item>
        <Form.Item name="priority" label={t('domainKnowledge.governance.priority')}>
          <InputNumber min={0} max={10000} style={{ width: 160 }} />
        </Form.Item>
        <Form.Item name="valid_from" label={t('domainKnowledge.governance.validFrom')}>
          <Input placeholder="2026-10-01T00:00:00+08:00" />
        </Form.Item>
        <Form.Item name="valid_to" label={t('domainKnowledge.governance.validTo')}>
          <Input placeholder="2026-12-31T23:59:59+08:00" />
        </Form.Item>
        <Form.Item name="enabled" label={t('domainKnowledge.governance.enabled')}
                   valuePropName="checked">
          <Switch />
        </Form.Item>
        <Form.Item name="note" label={t('domainKnowledge.governance.note')}>
          <Input.TextArea rows={2} maxLength={500} showCount />
        </Form.Item>
      </Form>
    </Modal>
  );
}
