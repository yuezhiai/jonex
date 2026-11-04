/** 治理标签新增/编辑弹窗。 */
import { Form, Input, Modal, Select } from 'antd';
import { useEffect } from 'react';
import { useTranslation } from 'react-i18next';
import { LEVEL_ORDER } from './governanceModel';
import type { GovernanceLabel } from '@/api/governance';
import type { DataSourceConfig } from '@/types/domainKnowledge';

export interface GovernanceLabelForm {
  data_source_id?: string;
  table_name?: string;
  column_name?: string;
  level: string;
  tags?: string[];
  note?: string;
}

interface Props {
  open: boolean;
  label?: GovernanceLabel | null;
  sources: DataSourceConfig[];
  submitting?: boolean;
  onCancel: () => void;
  onSubmit: (values: GovernanceLabelForm) => void;
}

export default function LabelModal({
  open, label, sources, submitting, onCancel, onSubmit,
}: Props) {
  const { t } = useTranslation();
  const [form] = Form.useForm<GovernanceLabelForm>();
  const editing = !!label;

  useEffect(() => {
    if (!open) return;
    form.resetFields();
    if (label) {
      form.setFieldsValue({
        data_source_id: label.data_source_id,
        table_name: label.table_name,
        column_name: label.column_name || undefined,
        level: label.level,
        tags: label.tags,
        note: label.note || undefined,
      });
    } else {
      form.setFieldsValue({ level: 'internal', tags: [] });
    }
  }, [open, label, form]);

  return (
    <Modal
      open={open}
      title={t(editing
        ? 'domainKnowledge.governance.editLabel'
        : 'domainKnowledge.governance.addLabel')}
      okText={t('common.confirm')}
      cancelText={t('common.cancel')}
      confirmLoading={submitting}
      onCancel={onCancel}
      onOk={() => form.submit()}
      destroyOnHidden
    >
      <Form form={form} layout="vertical" onFinish={onSubmit}>
        <Form.Item
          name="data_source_id"
          label={t('domainKnowledge.governance.dataSource')}
          rules={[{ required: true, message: t('domainKnowledge.lineage.requiredField') }]}
        >
          <Select
            disabled={editing}
            showSearch
            optionFilterProp="label"
            options={sources.map((s) => ({ value: s.id, label: s.name }))}
          />
        </Form.Item>
        <Form.Item
          name="table_name"
          label={t('domainKnowledge.governance.tableName')}
          rules={[{ required: true, whitespace: true }]}
        >
          <Input disabled={editing} placeholder="public.orders" />
        </Form.Item>
        <Form.Item
          name="column_name"
          label={t('domainKnowledge.governance.columnName')}
        >
          <Input disabled={editing}
                 placeholder={t('domainKnowledge.governance.columnPlaceholder')} />
        </Form.Item>
        <Form.Item
          name="level"
          label={t('domainKnowledge.governance.level')}
          rules={[{ required: true }]}
        >
          <Select options={LEVEL_ORDER.map((lv) => ({
            value: lv, label: t(`domainKnowledge.governance.level${lv[0].toUpperCase()}${lv.slice(1)}`),
          }))} />
        </Form.Item>
        <Form.Item name="tags" label={t('domainKnowledge.governance.tags')}>
          <Select mode="tags" tokenSeparators={[',']}
                  placeholder={t('domainKnowledge.governance.tagsPlaceholder')} />
        </Form.Item>
        <Form.Item name="note" label={t('domainKnowledge.governance.note')}>
          <Input.TextArea rows={2} maxLength={500} showCount />
        </Form.Item>
      </Form>
    </Modal>
  );
}
