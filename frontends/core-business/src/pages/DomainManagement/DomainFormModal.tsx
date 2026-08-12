import React from 'react';
import { Modal, Input, Form, Checkbox, Switch } from 'antd';
import { PlusCircleOutlined, EditOutlined } from '@ant-design/icons';
import { useTranslation } from 'react-i18next';
import type { DomainServiceItem, KnowledgeBaseOption } from '../../types/domainService';

interface DomainFormValues {
  name: string;
  kb_ids: string[];
  status: boolean;
}

interface DomainFormModalProps {
  open: boolean;
  editing: DomainServiceItem | null;
  availableKbs: KnowledgeBaseOption[];
  submitting: boolean;
  onSave: (values: DomainFormValues) => void;
  onCancel: () => void;
}

export default function DomainFormModal({
  open,
  editing,
  availableKbs,
  submitting,
  onSave,
  onCancel,
}: DomainFormModalProps) {
  const { t } = useTranslation();
  const [form] = Form.useForm<DomainFormValues>();

  // 打开时初始化表单
  React.useEffect(() => {
    if (open) {
      form.setFieldsValue({
        name: editing?.name || '',
        kb_ids: editing?.kb_ids || [],
        status: (editing?.status ?? 'active') === 'active',
      });
    }
  }, [open, editing, form]);

  const handleOk = async () => {
    try {
      const values = await form.validateFields();
      onSave(values);
    } catch {
      // 表单校验失败，Ant Design 会自动显示错误信息
    }
  };

  return (
    <Modal
      wrapClassName="yx-domain-space-modal"
      title={
        <span>
          {editing ? (
            <EditOutlined style={{ color: '#3b82f6', marginRight: 8 }} />
          ) : (
            <PlusCircleOutlined style={{ color: '#3b82f6', marginRight: 8 }} />
          )}
          {editing ? t('domainManagement.edit') : t('domainManagement.create')}
        </span>
      }
      open={open}
      onCancel={onCancel}
      onOk={handleOk}
      confirmLoading={submitting}
      okText={editing ? t('common.save') : t('common.add')}
      cancelText={t('common.cancel')}
      width={600}
      destroyOnHidden
    >
      <Form form={form} layout="vertical" autoComplete="off">
        <Form.Item
          name="name"
          label={
            <span>
              {t('domainManagement.name')} <span style={{ color: '#ef4444' }}>*</span>
            </span>
          }
          rules={[{ required: true, message: t('domainManagement.nameRequired') }]}
        >
          <Input placeholder={t('rules.placeholder')} />
        </Form.Item>
        <Form.Item name="kb_ids" label={t('domainManagement.kb')} extra={t('domainManagement.kbHint')}>
          <Checkbox.Group style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 6 }}>
            {availableKbs.map((kb) => (
              <Checkbox key={kb.id} value={kb.id}>
                {kb.name}
              </Checkbox>
            ))}
          </Checkbox.Group>
        </Form.Item>
        <Form.Item name="status" label={t('domainManagement.status')} valuePropName="checked">
          <Switch checkedChildren={t('status.active')} unCheckedChildren={t('status.inactive')} />
        </Form.Item>
      </Form>
    </Modal>
  );
}
