import React, { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Modal, Form, Input, Select, message } from 'antd';
import {
  PROMPT_CATEGORIES,
  PROMPT_CATEGORY_LABEL_KEYS,
  checkPromptTemplateName,  
  type PromptTemplateItem,
  type CreatePromptTemplatePayload,
  type UpdatePromptTemplatePayload,
} from '../../api/promptTemplates';

const { TextArea } = Input;

interface CreateEditModalProps {
  open: boolean;
  mode: 'create' | 'edit' | 'view';
  template: PromptTemplateItem | null;
  domainSpaceId: string | null;
  onClose: () => void;
  onSubmit: (data: CreatePromptTemplatePayload | UpdatePromptTemplatePayload) => Promise<void>;
}

function getCurrentContent(item: PromptTemplateItem | null): string {
  if (!item) return '';
  const versions = item.versions_json || [];
  return versions.length > 0 ? versions[0].content : '';
}

const CreateEditModal: React.FC<CreateEditModalProps> = ({ open, mode, template, domainSpaceId, onClose, onSubmit }) => {
  const { t } = useTranslation();
  const [form] = Form.useForm();
  const [loading, setLoading] = useState(false);
  const isView = mode === 'view';

  // 名称实时查重（防抖，取消令牌版——settle 旧 Promise 避免悬挂）
  const pendingNameCheck = React.useRef<{ timer: number; resolve: () => void } | null>(null);

  useEffect(() => () => {
    if (pendingNameCheck.current) {
      window.clearTimeout(pendingNameCheck.current.timer);
      pendingNameCheck.current.resolve();
    }
  }, []);

  const validateNameUnique = (_rule: unknown, value: string): Promise<void> => {
    const name = (value ?? '').trim();
    if (!name) return Promise.resolve();
    // 作废上一次：settle 旧 Promise，避免悬挂 + 覆盖旧的校验结果
    if (pendingNameCheck.current) {
      window.clearTimeout(pendingNameCheck.current.timer);
      pendingNameCheck.current.resolve();
    }
    return new Promise<void>((resolve, reject) => {
      const timer = window.setTimeout(async () => {
        pendingNameCheck.current = null;
        try {
          const res = await checkPromptTemplateName({
            name,
            domain_space_id: domainSpaceId ?? undefined,
            template_id: mode === 'edit' ? template?.id : undefined,
          });
          if (res.exists) reject(t('promptTemplate.nameExists'));
          else resolve();
        } catch {
          resolve(); // 查重接口异常不阻断提交，最终由后端兜底
        }
      }, 350);
      pendingNameCheck.current = { timer, resolve };
    });
  };

  useEffect(() => {
    if (!open) return;
    if (mode === 'create') {
      form.resetFields();
      form.setFieldsValue({ category: '通用问答', status: '启用' });
    } else if (template) {
      form.setFieldsValue({
        name: template.name,
        category: template.category,
        description: template.description || '',
        content: getCurrentContent(template),
        status: template.status,
        version_remark: '',
      });
    }
  }, [open, mode, template, form]);

  const handleOk = async () => {
    if (isView) {
      onClose();
      return;
    }
    try {
      const values = await form.validateFields();
      setLoading(true);
      await onSubmit(values);
      onClose();
    } catch (err: unknown) {
      if (err && typeof err === 'object' && 'errorFields' in err) return; // form validation
      message.error(err instanceof Error ? err.message : t('promptTemplate.saveFailed'));
    } finally {
      setLoading(false);
    }
  };

  return (
    <Modal
      title={
        mode === 'create'
          ? t('promptTemplate.createTitle')
          : mode === 'edit'
            ? t('promptTemplate.editTitle')
            : t('promptTemplate.viewTitle')
      }
      open={open}
      onOk={handleOk}
      onCancel={onClose}
      okText={isView ? t('common.close') : t('common.save')}
      cancelText={t('common.cancel')}
      confirmLoading={loading}
      cancelButtonProps={isView ? { style: { display: 'none' } } : undefined}
      width={640}
      destroyOnClose
    >
      <Form form={form} layout="vertical" disabled={isView}>
        <Form.Item
          name="name"
          label={t('promptTemplate.name')}
          rules={[
            { required: true, message: t('promptTemplate.nameRequired') },
            { max: 255, message: t('promptTemplate.nameTooLong') },
            { validator: validateNameUnique },
          ]}
        >
          <Input placeholder={t('promptTemplate.namePlaceholder')} />
        </Form.Item>

        <Form.Item
          name="category"
          label={t('promptTemplate.category')}
          rules={[{ required: true, message: t('promptTemplate.categoryRequired') }]}
        >
          <Select placeholder={t('promptTemplate.categoryPlaceholder')}>
            {PROMPT_CATEGORIES.map((cat) => (
              <Select.Option key={cat} value={cat}>
                {t(PROMPT_CATEGORY_LABEL_KEYS[cat] || cat)}
              </Select.Option>
            ))}
          </Select>
        </Form.Item>

        {mode === 'edit' && (
          <>
            <Form.Item
              name="version_remark"
              label={t('promptTemplate.versionRemark')}
              extra={t('promptTemplate.versionRemarkHint')}
            >
              <Input placeholder={t('promptTemplate.versionRemarkPlaceholder')} maxLength={512} />
            </Form.Item>

            <Form.Item label={t('promptTemplate.currentVersion')}>
              <Input value={`v${template?.current_version || '1'}`} disabled />
            </Form.Item>
          </>
        )}

        <Form.Item name="description" label={t('promptTemplate.descriptionField')}>
          <Input placeholder={t('promptTemplate.descriptionPlaceholder')} maxLength={512} />
        </Form.Item>

        <Form.Item
          name="content"
          label={
            mode === 'edit' ? (
              <>
                {t('promptTemplate.content')}
                <span
                  style={{
                    marginLeft: 8,
                    fontSize: 12,
                    fontWeight: 'normal',
                    color: 'rgba(0, 0, 0, 0.45)',
                  }}
                >
                  {t('promptTemplate.contentVersionHintPrefix')}
                  <span style={{ color: '#ff4d4f' }}>+1</span>
                  {t('promptTemplate.contentVersionHintSuffix')}
                </span>
              </>
            ) : (
              t('promptTemplate.content')
            )
          }
          rules={[
            { required: true, message: t('promptTemplate.contentRequired') },
            { max: 5000, message: t('promptTemplate.contentTooLong') },
          ]}
          extra={t('promptTemplate.variableHint', {
            variable: '{{variable}}',
            userQuestion: '{{user_question}}',
          })}
        >
          <TextArea
            rows={8}
            showCount={{ formatter: ({ count }: { count: number }) => `${count} / 5000` }}
            placeholder={
              t('promptTemplate.contentTextareaPlaceholder', {
                variable: '{{variable}}',
              }) +
              t('promptTemplate.contentExample', {
                retrievedContent: '{{retrieved_content}}',
                userQuestion: '{{user_question}}',
              })
            }
            style={{ fontFamily: "'Courier New', monospace", lineHeight: 1.6 }}
          />
        </Form.Item>

        <Form.Item name="status" label={t('promptTemplate.status')}>
          <Select>
            <Select.Option value="启用">{t('promptTemplate.enabled')}</Select.Option>
            <Select.Option value="停用">{t('promptTemplate.disabled')}</Select.Option>
          </Select>
        </Form.Item>
      </Form>
    </Modal>
  );
};

export default React.memo(CreateEditModal);
