/** 新增禁止同查表对：两端表名 + 备注；前端只挡空/同表，存在性由后端判。 */
import { Form, Input, Modal } from 'antd';
import { useEffect } from 'react';
import { useTranslation } from 'react-i18next';

export interface ForbiddenPairForm {
  table_a: string;
  table_b: string;
  note?: string;
}

interface Props {
  open: boolean;
  submitting?: boolean;
  onCancel: () => void;
  onSubmit: (values: ForbiddenPairForm) => void;
}

export default function LineageForbiddenModal({ open, submitting, onCancel, onSubmit }: Props) {
  const { t } = useTranslation();
  const [form] = Form.useForm<ForbiddenPairForm>();

  useEffect(() => {
    if (open) form.resetFields();
  }, [open, form]);

  return (
    <Modal
      open={open}
      title={t('domainKnowledge.lineage.forbidden.addTitle')}
      okText={t('common.confirm')}
      cancelText={t('common.cancel')}
      confirmLoading={submitting}
      onCancel={onCancel}
      onOk={() => form.submit()}
      destroyOnHidden
    >
      <Form form={form} layout="vertical" onFinish={onSubmit}>
        <Form.Item
          name="table_a"
          label={t('domainKnowledge.lineage.forbidden.tableA')}
          rules={[{ required: true, whitespace: true, message: t('domainKnowledge.lineage.requiredField') }]}
        >
          <Input placeholder="dbo.jobs" />
        </Form.Item>
        <Form.Item
          name="table_b"
          label={t('domainKnowledge.lineage.forbidden.tableB')}
          dependencies={['table_a']}
          rules={[
            { required: true, whitespace: true, message: t('domainKnowledge.lineage.requiredField') },
            ({ getFieldValue }) => ({
              validator(_, value) {
                const a = String(getFieldValue('table_a') || '').trim().toLowerCase();
                const b = String(value || '').trim().toLowerCase();
                return !a || !b || a !== b
                  ? Promise.resolve()
                  : Promise.reject(new Error(t('domainKnowledge.lineage.forbidden.sameTable')));
              },
            }),
          ]}
        >
          <Input placeholder="dbo.job_forward" />
        </Form.Item>
        <Form.Item name="note" label={t('domainKnowledge.lineage.noteOptional')}>
          <Input.TextArea rows={2} maxLength={500} showCount />
        </Form.Item>
      </Form>
    </Modal>
  );
}
