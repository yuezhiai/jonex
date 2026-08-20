import React, { useEffect } from 'react';
import { Form, Modal, Input, Radio, Tag, Space, Alert } from 'antd';
import { PlusOutlined, EditOutlined, LockOutlined } from '@ant-design/icons';
import { useTranslation } from 'react-i18next';
import type { DomainKnowledgeItem, KnowledgeBaseType } from '@/types/domainKnowledge';

interface CreateEditModalProps {
  open: boolean;
  editingKb: DomainKnowledgeItem | null;
  submitting: boolean;
  /** kb_type 仅在「创建」模式下回传；编辑模式不回传（类型创建后不可变） */
  onOk: (data: { name: string; description?: string; kb_type?: KnowledgeBaseType }) => void;
  onCancel: () => void;
}

/** 知识库类型选项（值与后端 VALID_KB_TYPES 一致） */
const KB_TYPE_OPTIONS: { value: KnowledgeBaseType; labelKey: string; descKey: string }[] = [
  {
    value: 'lightrag',
    labelKey: 'domainKnowledge.kbTypeLightrag',
    descKey: 'domainKnowledge.kbTypeLightragDesc',
  },
  {
    value: 'openkb',
    labelKey: 'domainKnowledge.kbTypeOpenkb',
    descKey: 'domainKnowledge.kbTypeOpenkbDesc',
  },
];

const KB_TYPE_TAG_COLOR: Record<KnowledgeBaseType, string> = {
  lightrag: 'blue',
  openkb: 'purple',
};

export default function CreateEditModal({ open, editingKb, submitting, onOk, onCancel }: CreateEditModalProps) {
  const { t } = useTranslation();
  const [form] = Form.useForm();
  const isEdit = !!editingKb;

  useEffect(() => {
    if (open) {
      form.setFieldsValue({
        name: editingKb?.name ?? '',
        description: editingKb?.description ?? '',
        // 编辑模式回填以便只读展示；创建模式默认标准检索型
        kb_type: editingKb?.kbType ?? 'lightrag',
      });
    }
  }, [open, editingKb, form]);

  const handleOk = async () => {
    try {
      const values = await form.validateFields();
      // [jonex] 编辑模式不回传 kb_type：类型创建后不可变，后端 update() 收到该字段会直接报错
      onOk({
        name: values.name,
        description: values.description,
        ...(isEdit ? {} : { kb_type: values.kb_type as KnowledgeBaseType }),
      });
    } catch {
      /* 校验失败，由 Form 提示 */
    }
  };

  const editingKbType: KnowledgeBaseType = editingKb?.kbType ?? 'lightrag';
  const editingOption = KB_TYPE_OPTIONS.find((o) => o.value === editingKbType) ?? KB_TYPE_OPTIONS[0];

  return (
    <Modal
      open={open}
      onCancel={onCancel}
      onOk={handleOk}
      confirmLoading={submitting}
      okText={isEdit ? t('common.save') : t('common.create')}
      cancelText={t('common.cancel')}
      width={560}
      destroyOnClose
      title={
        <span>
          {isEdit ? (
            <EditOutlined style={{ color: '#3b82f6', marginRight: 8 }} />
          ) : (
            <PlusOutlined style={{ color: '#3b82f6', marginRight: 8 }} />
          )}
          {isEdit ? t('domainKnowledge.editKnowledgeBase') : t('domainKnowledge.createKnowledgeBase')}
        </span>
      }
    >
      <Form form={form} layout="vertical" style={{ marginTop: 8 }}>
        <Form.Item
          name="name"
          label={t('domainKnowledge.knowledgeBaseName')}
          rules={[{ required: true, message: t('domainKnowledge.knowledgeBaseNameRequired') }]}
        >
          <Input placeholder={t('domainKnowledge.namePlaceholder')} />
        </Form.Item>

        {/* [jonex] 知识库类型：创建时可选，创建后不可变 → 编辑模式只读展示 */}
        <Form.Item
          name="kb_type"
          label={t('domainKnowledge.kbType')}
          extra={isEdit ? undefined : t('domainKnowledge.kbTypeCreateOnlyHint')}
        >
          {isEdit ? (
            <Space size={8} style={{ paddingTop: 2 }}>
              <Tag color={KB_TYPE_TAG_COLOR[editingKbType]} style={{ marginInlineEnd: 0 }}>
                {t(editingOption.labelKey)}
              </Tag>
              <span style={{ color: '#94a3b8', fontSize: 12 }}>
                <LockOutlined style={{ marginRight: 4 }} />
                {t('domainKnowledge.kbTypeImmutable')}
              </span>
            </Space>
          ) : (
            <Radio.Group style={{ width: '100%' }}>
              <Space direction="vertical" size={8} style={{ width: '100%' }}>
                {KB_TYPE_OPTIONS.map((opt) => (
                  <Radio
                    key={opt.value}
                    value={opt.value}
                    style={{
                      display: 'flex',
                      alignItems: 'flex-start',
                      width: '100%',
                      padding: '10px 12px',
                      border: '1px solid #e2e8f0',
                      borderRadius: 8,
                      marginInlineEnd: 0,
                    }}
                  >
                    <div style={{ marginLeft: 2 }}>
                      <div style={{ fontWeight: 500, color: '#0b2b5c' }}>{t(opt.labelKey)}</div>
                      <div style={{ color: '#64748b', fontSize: 12, marginTop: 2, whiteSpace: 'normal' }}>
                        {t(opt.descKey)}
                      </div>
                    </div>
                  </Radio>
                ))}
              </Space>
            </Radio.Group>
          )}
        </Form.Item>

        <Form.Item name="description" label={t('domainKnowledge.description')}>
          <Input.TextArea rows={3} placeholder={t('domainKnowledge.descPlaceholder')} />
        </Form.Item>

        {/* [jonex] 新建 KB 默认配置说明（仅创建模式） */}
        {!isEdit && (
          <Alert
            type="info"
            showIcon
            message={t('domainKnowledge.kbDefaultConfigHint')}
            style={{ marginBottom: 8 }}
          />
        )}
      </Form>
    </Modal>
  );
}
