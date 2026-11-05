// 本库工具新建 / 编辑弹窗：基础信息 + 入参 / 出参 Schema + 运行时（与平台工具共用 ApiRuntimeFields）。
import React, { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Checkbox, Form, Input, Modal, Switch, message } from 'antd';
import {
  ApiRuntimeFields,
  formValuesToRuntime,
  runtimeToFormValues,
  type ApiRuntimeFormValues,
} from '@jonex/shared-lib';
import type { KbTool } from '@/types/domainKnowledge';
import type { SaveKbToolPayload } from '@/api/kbTool';

const { TextArea } = Input;
const TOOL_NAME_RE = /^[a-z][a-z0-9_]{1,127}$/;
const DEFAULT_INPUT_SCHEMA = '{\n  "type": "object",\n  "properties": {},\n  "required": []\n}';

interface FormValues extends ApiRuntimeFormValues {
  tool_name: string;
  name: string;
  description?: string;
  input_schema: string;
  output_schema?: string;
  enabled: boolean;
  read_only: boolean;
}

interface Props {
  open: boolean;
  /** 为 null 时是新建 */
  tool: KbTool | null;
  onClose: () => void;
  onSubmit: (payload: SaveKbToolPayload) => Promise<void>;
}

function pretty(schema: Record<string, unknown> | undefined): string {
  if (!schema || !Object.keys(schema).length) return '';
  return JSON.stringify(schema, null, 2);
}

function parseObject(text?: string): Record<string, unknown> | undefined {
  const raw = (text ?? '').trim();
  if (!raw) return undefined;
  const v: unknown = JSON.parse(raw);
  if (!v || typeof v !== 'object' || Array.isArray(v)) throw new Error('not an object');
  return v as Record<string, unknown>;
}

export default function KbToolFormModal({ open, tool, onClose, onSubmit }: Props) {
  const { t } = useTranslation();
  const [form] = Form.useForm<FormValues>();
  const [saving, setSaving] = useState(false);
  const isEdit = !!tool;

  useEffect(() => {
    if (!open) return;
    form.resetFields();
    if (tool) {
      form.setFieldsValue({
        tool_name: tool.toolName,
        name: tool.name,
        description: tool.description,
        input_schema: pretty(tool.inputSchema) || DEFAULT_INPUT_SCHEMA,
        output_schema: pretty(tool.outputSchema),
        enabled: tool.enabled,
        read_only: tool.readOnly,
        ...runtimeToFormValues(tool.runtime),
      });
    } else {
      form.setFieldsValue({
        input_schema: DEFAULT_INPUT_SCHEMA,
        output_schema: '',
        enabled: true,
        read_only: false,
        ...runtimeToFormValues({ type: 'api', method: 'POST', auth: { type: 'none' } }),
      });
    }
  }, [form, open, tool]);

  const schemaRule = (required: boolean) => ({
    validator: (_: unknown, value?: string) => {
      if (!(value ?? '').trim()) {
        return required ? Promise.reject(new Error(t('kbTool.schemaRequired'))) : Promise.resolve();
      }
      try {
        parseObject(value);
        return Promise.resolve();
      } catch {
        return Promise.reject(new Error(t('kbTool.schemaInvalid')));
      }
    },
  });

  const handleOk = async () => {
    let values: FormValues;
    try {
      values = await form.validateFields();
    } catch {
      return;
    }
    const runtimePart = formValuesToRuntime(values);
    const payload: SaveKbToolPayload = {
      name: values.name.trim(),
      description: (values.description ?? '').trim() || null,
      input_schema: parseObject(values.input_schema) ?? {},
      output_schema: parseObject(values.output_schema) ?? {},
      runtime: runtimePart.runtime,
      enabled: values.enabled,
      read_only: !!values.read_only,
    };
    if (runtimePart.secret) payload.secret = runtimePart.secret;
    if (runtimePart.clear_secret) payload.clear_secret = true;
    if (!isEdit) payload.tool_name = values.tool_name.trim();
    setSaving(true);
    try {
      await onSubmit(payload);
      onClose();
    } catch (err: any) {
      message.error(err?.message || t('common.saveFailed'));
    } finally {
      setSaving(false);
    }
  };

  return (
    <Modal
      title={isEdit ? t('kbTool.editTitle') : t('kbTool.createTitle')}
      open={open}
      onOk={handleOk}
      onCancel={onClose}
      okText={t('common.save')}
      cancelText={t('common.cancel')}
      confirmLoading={saving}
      width={760}
      destroyOnHidden
    >
      <Form form={form} layout="vertical">
        <Form.Item
          name="tool_name"
          label={t('kbTool.toolName')}
          extra={isEdit ? t('kbTool.toolNameImmutable') : t('kbTool.toolNameHint')}
          rules={[
            { required: true, message: t('kbTool.toolNameRequired') },
            { pattern: TOOL_NAME_RE, message: t('kbTool.toolNameInvalid') },
          ]}
        >
          <Input disabled={isEdit} placeholder="query_card_status" maxLength={128} />
        </Form.Item>
        <Form.Item
          name="name"
          label={t('kbTool.name')}
          rules={[{ required: true, whitespace: true, message: t('kbTool.nameRequired') }, { max: 255 }]}
        >
          <Input maxLength={255} />
        </Form.Item>
        <Form.Item name="description" label={t('kbTool.description')} extra={t('kbTool.descriptionHint')}>
          <TextArea rows={2} maxLength={1000} />
        </Form.Item>
        <Form.Item
          name="input_schema"
          label={t('kbTool.inputSchema')}
          extra={t('kbTool.inputSchemaHint')}
          rules={[schemaRule(true)]}
        >
          <TextArea rows={7} style={{ fontFamily: 'monospace', fontSize: 12 }} />
        </Form.Item>
        <Form.Item name="output_schema" label={t('kbTool.outputSchema')} rules={[schemaRule(false)]}>
          <TextArea rows={3} style={{ fontFamily: 'monospace', fontSize: 12 }} placeholder="{}" />
        </Form.Item>

        <ApiRuntimeFields allowNone={false} hasSecret={!!tool?.hasSecret} />

        <Form.Item
          name="read_only"
          label={t('toolRuntime.readOnly')}
          valuePropName="checked"
          extra={t('toolRuntime.readOnlyHint')}
        >
          <Checkbox>{t('toolRuntime.readOnlyCheckbox')}</Checkbox>
        </Form.Item>

        <Form.Item name="enabled" label={t('kbTool.enabled')} valuePropName="checked" extra={t('kbTool.enabledHint')}>
          <Switch />
        </Form.Item>
      </Form>
    </Modal>
  );
}
