import { useEffect } from 'react';
import { Modal, Button, Form, Input, Select, Switch, Checkbox } from 'antd';
import { EditOutlined, PlusOutlined } from '@ant-design/icons';
import { useTranslation } from 'react-i18next';
import type { DomainServiceItem, KnowledgeBaseOption } from '../../types/domainService';
import './ServiceFormModal.css';

/** 表单提交值：两页共用（domain_type 只有服务管理页展示） */
export interface ServiceFormValues {
  name: string;
  domain_type?: string;
  kb_ids: string[];
  status: boolean;
}

interface ServiceFormModalProps {
  open: boolean;
  /** null = 新建 */
  editing: DomainServiceItem | null;
  /** 所属空间名称，仅用于标题下方说明 */
  spaceName?: string;
  availableKbs: KnowledgeBaseOption[];
  submitting: boolean;
  /** 服务管理页有「服务类型」字段，领域管理页没有 */
  showType?: boolean;
  onSubmit: (values: ServiceFormValues) => void | Promise<void>;
  onCancel: () => void;
}

const TYPE_VALUES = ['retrieval', 'inference', 'analysis', 'general'] as const;

export default function ServiceFormModal({
  open,
  editing,
  spaceName,
  availableKbs,
  submitting,
  showType = false,
  onSubmit,
  onCancel,
}: ServiceFormModalProps) {
  const { t } = useTranslation();
  const [form] = Form.useForm<ServiceFormValues>();

  // 打开时按「新建 / 编辑」回填，避免上一次输入的残留
  useEffect(() => {
    if (!open) return;
    form.setFieldsValue({
      name: editing?.name ?? '',
      domain_type: editing?.domain_type ?? undefined,
      kb_ids: editing?.kb_ids ?? [],
      status: (editing?.status ?? 'active') === 'active',
    });
  }, [open, editing, form]);

  const handleOk = async () => {
    const values = await form.validateFields();
    await onSubmit({ ...values, name: values.name.trim() });
  };

  const typeOptions = TYPE_VALUES.map((value) => ({
    value,
    label: t(`domainManagementServices.type${value.charAt(0).toUpperCase()}${value.slice(1)}`),
  }));

  return (
    <Modal
      wrapClassName="sfm-modal"
      title={
        <span className="sfm-title">
          <span className="sfm-title-ico">{editing ? <EditOutlined /> : <PlusOutlined />}</span>
          {editing ? t('domainManagement.formTitleEdit') : t('domainManagement.formTitleCreate')}
        </span>
      }
      open={open}
      onCancel={onCancel}
      footer={
        <div className="sfm-footer">
          <Button autoInsertSpace={false} onClick={onCancel}>
            {t('common.cancel')}
          </Button>
          <Button type="primary" autoInsertSpace={false} loading={submitting} onClick={handleOk}>
            {editing ? t('common.save') : t('common.add')}
          </Button>
        </div>
      }
      width={760}
      maskClosable={false}
      destroyOnHidden
    >
      <p className="sfm-sub">
        {spaceName
          ? t('domainManagement.formSubWithSpace', { name: spaceName })
          : t('domainManagement.formSub')}
      </p>

      <Form form={form} layout="vertical" autoComplete="off" className="sfm-form" requiredMark={false}>
        <section className="sfm-section">
          <h4 className="sfm-section-title">{t('domainManagement.formSectionBasic')}</h4>

          <Form.Item
            name="name"
            label={t('domainManagement.name')}
            rules={[
              { required: true, whitespace: true, message: t('domainManagement.nameRequired') },
              { max: 100, message: t('domainManagement.nameTooLong') },
            ]}
          >
            <Input
              placeholder={t('domainManagement.namePlaceholder')}
              maxLength={100}
              showCount
              autoFocus
            />
          </Form.Item>

          {showType && (
            <Form.Item name="domain_type" label={t('domainManagementServices.columnType')}>
              <Select
                allowClear
                placeholder={t('domainManagementServices.typePlaceholder')}
                options={typeOptions}
              />
            </Form.Item>
          )}

          <Form.Item
            name="status"
            label={t('domainManagement.status')}
            valuePropName="checked"
            extra={t('domainManagement.statusHint')}
          >
            <Switch checkedChildren={t('status.active')} unCheckedChildren={t('status.inactive')} />
          </Form.Item>
        </section>

        <section className="sfm-section">
          <h4 className="sfm-section-title">
            {t('domainManagement.kb')}
            <span className="sfm-section-count">{availableKbs.length}</span>
          </h4>
          <p className="sfm-section-hint">{t('domainManagement.kbHint')}</p>

          {availableKbs.length === 0 ? (
            <div className="sfm-kb-empty">{t('domainManagement.noKbAvailable')}</div>
          ) : (
            <Form.Item name="kb_ids" className="sfm-kb-field">
              <Checkbox.Group className="sfm-kb-grid">
                {availableKbs.map((kb) => (
                  <Checkbox key={kb.id} value={kb.id} className="sfm-kb-item">
                    <span className="sfm-kb-name">{kb.name}</span>
                  </Checkbox>
                ))}
              </Checkbox.Group>
            </Form.Item>
          )}
        </section>
      </Form>
    </Modal>
  );
}
