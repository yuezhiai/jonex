// 工具目录新建 / 编辑弹窗（平台管理员）：基础信息 + 入参 / 出参 Schema + 运行时配置。
// 运行时表单段与知识库工具共用 @jonex/shared-lib 的 ApiRuntimeFields（echo / API）。
import React, { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Alert, Checkbox, Form, Input, Modal, Select, message } from 'antd';
import {
  ApiRuntimeFields,
  formValuesToRuntime,
  runtimeToFormValues,
  type ApiRuntimeFormValues,
} from '@jonex/shared-lib';
import type { CatalogItem, SaveCatalogPayload } from '../../api/skills';
import { CATEGORY_LABEL_KEYS, formatJsonSchema } from './shared';

const { TextArea } = Input;

const CATEGORY_OPTIONS = ['image', 'voice', 'document', 'video', 'fusion', 'custom'] as const;
const TOOL_NAME_RE = /^[a-z][a-z0-9_]{1,127}$/;
const DEFAULT_INPUT_SCHEMA = '{\n  "type": "object",\n  "properties": {},\n  "required": []\n}';

interface FormValues extends ApiRuntimeFormValues {
  tool_name: string;
  name: string;
  category: string;
  status: 'published' | 'disabled';
  description?: string;
  instruction?: string;
  tags?: string[];
  input_schema: string;
  output_schema?: string;
  read_only: boolean;
}

interface CatalogFormModalProps {
  open: boolean;
  /** 为 null 时是新建 */
  item: CatalogItem | null;
  onClose: () => void;
  onSubmit: (data: SaveCatalogPayload) => Promise<void>;
}

function parseJsonObject(text: string | undefined): Record<string, unknown> | undefined {
  const raw = (text ?? '').trim();
  if (!raw) return undefined;
  const value: unknown = JSON.parse(raw);
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('not an object');
  return value as Record<string, unknown>;
}

const CatalogFormModal: React.FC<CatalogFormModalProps> = ({ open, item, onClose, onSubmit }) => {
  const { t } = useTranslation();
  const [form] = Form.useForm<FormValues>();
  const [saving, setSaving] = useState(false);
  const isEdit = !!item;

  useEffect(() => {
    if (!open) return;
    form.resetFields();
    if (item) {
      form.setFieldsValue({
        tool_name: item.tool_name,
        name: item.name,
        category: item.category,
        status: item.status,
        description: item.description ?? '',
        instruction: item.instruction ?? '',
        tags: item.tags ?? [],
        input_schema: formatJsonSchema(item.input_schema ?? {}),
        output_schema: Object.keys(item.output_schema ?? {}).length ? formatJsonSchema(item.output_schema) : '',
        read_only: item.read_only === true,
        ...runtimeToFormValues(item.runtime as Record<string, any> | null),
      });
    } else {
      form.setFieldsValue({
        category: 'custom',
        status: 'published',
        tags: [],
        input_schema: DEFAULT_INPUT_SCHEMA,
        output_schema: '',
        read_only: false,
        ...runtimeToFormValues({ type: 'echo' }),
      });
    }
  }, [open, item, form]);

  const jsonValidator = (required: boolean) => (_rule: unknown, value: string | undefined) => {
    if (!(value ?? '').trim()) {
      return required ? Promise.reject(t('skills.catalog.schemaRequired')) : Promise.resolve();
    }
    try {
      parseJsonObject(value);
      return Promise.resolve();
    } catch {
      return Promise.reject(t('skills.catalog.schemaInvalidJson'));
    }
  };

  const handleOk = async () => {
    let values: FormValues;
    try {
      values = await form.validateFields();
    } catch {
      return; // 表单校验失败，错误已在字段下展示
    }
    const runtimePart = formValuesToRuntime(values);
    const payload: SaveCatalogPayload = {
      name: values.name.trim(),
      category: values.category,
      status: values.status,
      description: (values.description ?? '').trim() || null,
      tags: values.tags ?? [],
      input_schema: parseJsonObject(values.input_schema) ?? {},
      output_schema: parseJsonObject(values.output_schema) ?? {},
      runtime: runtimePart.runtime,
      read_only: !!values.read_only,
    };
    if (runtimePart.secret) payload.secret = runtimePart.secret;
    if (runtimePart.clear_secret) payload.clear_secret = true;
    const instruction = (values.instruction ?? '').trim();
    if (instruction || isEdit) payload.instruction = instruction;
    if (!isEdit) payload.tool_name = values.tool_name.trim();
    setSaving(true);
    try {
      await onSubmit(payload);
      onClose();
    } catch (err: unknown) {
      message.error(err instanceof Error ? err.message : t('common.saveFailed'));
    } finally {
      setSaving(false);
    }
  };

  return (
    <Modal
      title={isEdit ? t('skills.catalog.editTitle') : t('skills.catalog.createTitle')}
      open={open}
      onOk={handleOk}
      onCancel={onClose}
      okText={t('common.save')}
      cancelText={t('common.cancel')}
      confirmLoading={saving}
      width={760}
      destroyOnHidden
    >
      <Alert type="info" showIcon style={{ marginBottom: 16 }} message={t('skills.catalog.sharedHint')} />
      <Form form={form} layout="vertical">
        <Form.Item
          name="tool_name"
          label={t('skills.toolName')}
          extra={isEdit ? t('skills.catalog.toolNameImmutable') : t('skills.catalog.toolNameHint')}
          rules={[
            { required: true, message: t('skills.catalog.toolNameRequired') },
            { pattern: TOOL_NAME_RE, message: t('skills.catalog.toolNameInvalid') },
          ]}
        >
          <Input disabled={isEdit} placeholder="query_card_status" maxLength={128} />
        </Form.Item>

        <Form.Item
          name="name"
          label={t('common.name')}
          rules={[
            { required: true, whitespace: true, message: t('common.nameRequired') },
            { max: 255, message: t('skills.catalog.nameTooLong') },
          ]}
        >
          <Input maxLength={255} />
        </Form.Item>

        <div style={{ display: 'flex', gap: 16 }}>
          <Form.Item name="category" label={t('skills.categoryLabel')} style={{ flex: 1 }} rules={[{ required: true }]}>
            <Select options={CATEGORY_OPTIONS.map((c) => ({ label: t(CATEGORY_LABEL_KEYS[c]), value: c }))} />
          </Form.Item>
          <Form.Item
            name="status"
            label={t('common.status')}
            style={{ flex: 1 }}
            extra={t('skills.catalog.statusHint')}
            rules={[{ required: true }]}
          >
            <Select
              options={[
                { label: t('skills.catalog.statusPublished'), value: 'published' },
                { label: t('skills.catalog.statusDisabled'), value: 'disabled' },
              ]}
            />
          </Form.Item>
        </div>

        <Form.Item name="description" label={t('common.description')}>
          <TextArea rows={2} maxLength={1000} />
        </Form.Item>

        <Form.Item
          name="instruction"
          label={t('skills.instruction')}
          extra={t('skills.catalog.instructionHint')}
          rules={isEdit ? [{ required: true, whitespace: true, message: t('skills.catalog.instructionRequired') }] : []}
        >
          <TextArea rows={3} maxLength={4000} />
        </Form.Item>

        <Form.Item name="tags" label={t('skills.catalog.tags')} extra={t('skills.catalog.tagsHint')}>
          <Select mode="tags" tokenSeparators={[',', '，']} open={false} />
        </Form.Item>

        <Form.Item
          name="input_schema"
          label={t('skills.inputSchema')}
          extra={t('skills.catalog.inputSchemaHint')}
          rules={[{ validator: jsonValidator(true) }]}
        >
          <TextArea rows={8} style={{ fontFamily: 'monospace', fontSize: 12 }} />
        </Form.Item>

        <Form.Item
          name="output_schema"
          label={t('skills.outputSchema')}
          rules={[{ validator: jsonValidator(false) }]}
        >
          <TextArea rows={4} style={{ fontFamily: 'monospace', fontSize: 12 }} placeholder="{}" />
        </Form.Item>

        <ApiRuntimeFields allowNone hasSecret={!!item?.has_secret} />

        <Form.Item
          name="read_only"
          label={t('toolRuntime.readOnly')}
          valuePropName="checked"
          extra={t('toolRuntime.readOnlyHint')}
        >
          <Checkbox>{t('toolRuntime.readOnlyCheckbox')}</Checkbox>
        </Form.Item>
      </Form>
    </Modal>
  );
};

export default CatalogFormModal;
