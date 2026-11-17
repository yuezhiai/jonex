/** 已验证查询：新建 / 编辑弹窗。 */
import { useEffect } from 'react';
import {
  Button, Form, Input, Modal, Select, Space, Switch, Typography,
} from 'antd';
import { MinusCircleOutlined, PlusOutlined } from '@ant-design/icons';
import { useTranslation } from 'react-i18next';
import type {
  VerifiedParamType, VerifiedQuery, VerifiedQueryParam, VerifiedQuerySavePayload,
} from '@/api/verifiedQuery';

export interface VerifiedQueryFormValues {
  name: string;
  description?: string;
  data_source_id: string;
  questions: string;
  sql_template: string;
  params: VerifiedQueryParam[];
}

interface Props {
  open: boolean;
  editing: VerifiedQuery | null;
  dataSources: { id: string; name: string }[];
  submitting: boolean;
  onSubmit: (payload: Omit<VerifiedQuerySavePayload, 'knowledge_base_id'>) => void;
  onCancel: () => void;
}

const PARAM_TYPES: VerifiedParamType[] = ['string', 'number', 'date', 'boolean', 'enum'];

export default function VerifiedQueryFormModal({
  open, editing, dataSources, submitting, onSubmit, onCancel,
}: Props) {
  const { t } = useTranslation();
  const [form] = Form.useForm<VerifiedQueryFormValues>();

  useEffect(() => {
    if (!open) return;
    form.setFieldsValue({
      name: editing?.name || '',
      description: editing?.description || '',
      data_source_id: editing?.data_source_id || dataSources[0]?.id || '',
      questions: (editing?.questions || []).join('\n'),
      sql_template: editing?.sql_template || '',
      params: editing?.params || [],
    });
  }, [open, editing, dataSources, form]);

  const submit = async () => {
    const values = await form.validateFields();
    const questions = values.questions
      .split('\n').map((s) => s.trim()).filter(Boolean);
    onSubmit({
      data_source_id: values.data_source_id,
      name: values.name.trim(),
      description: values.description?.trim() || '',
      questions,
      sql_template: values.sql_template.trim(),
      params: (values.params || []).map((p) => ({
        name: p.name?.trim(),
        type: p.type,
        required: p.required !== false,
        description: p.description?.trim() || '',
        extract: p.extract?.trim(),
        ...(p.type === 'enum'
          ? { enum: (p.enum || []).map((v) => String(v).trim()).filter(Boolean) }
          : {}),
      })),
      ...(editing ? { verified_query_id: editing.id, expected_version: editing.version } : {}),
    });
  };

  return (
    <Modal
      open={open}
      title={editing
        ? t('domainKnowledge.verifiedQuery.editTitle')
        : t('domainKnowledge.verifiedQuery.createTitle')}
      onCancel={onCancel}
      onOk={() => void submit()}
      okText={t('common.save')}
      cancelText={t('common.cancel')}
      confirmLoading={submitting}
      width={760}
      destroyOnClose
    >
      <Form form={form} layout="vertical">
        <Form.Item name="name" label={t('domainKnowledge.verifiedQuery.name')}
                   rules={[{ required: true, whitespace: true, max: 128 }]}>
          <Input maxLength={128} />
        </Form.Item>
        <Form.Item name="description" label={t('domainKnowledge.verifiedQuery.description')}>
          <Input maxLength={500} />
        </Form.Item>
        <Form.Item name="data_source_id" label={t('domainKnowledge.verifiedQuery.dataSource')}
                   rules={[{ required: true }]}>
          <Select options={dataSources.map((d) => ({ value: d.id, label: d.name }))} />
        </Form.Item>
        <Form.Item
          name="questions"
          label={t('domainKnowledge.verifiedQuery.questions')}
          extra={t('domainKnowledge.verifiedQuery.questionsHint')}
          rules={[{ required: true, whitespace: true }]}
        >
          <Input.TextArea rows={4} />
        </Form.Item>
        <Form.Item
          name="sql_template"
          label={t('domainKnowledge.verifiedQuery.sqlTemplate')}
          extra={t('domainKnowledge.verifiedQuery.sqlHint')}
          rules={[{ required: true, whitespace: true, max: 10000 }]}
        >
          <Input.TextArea rows={5} className="yx-kb-vq-sql" />
        </Form.Item>

        <Typography.Text strong>
          {t('domainKnowledge.verifiedQuery.params')}
        </Typography.Text>
        <Typography.Paragraph type="secondary" style={{ fontSize: 12, marginTop: 4 }}>
          {t('domainKnowledge.verifiedQuery.paramsHint')}
        </Typography.Paragraph>
        <Form.List name="params">
          {(fields, { add, remove }) => (
            <div>
              {fields.map((field) => (
                <Space key={field.key} align="start" style={{ display: 'flex', marginBottom: 8 }}>
                  <Form.Item name={[field.name, 'name']} rules={[{ required: true }]} noStyle>
                    <Input placeholder={t('domainKnowledge.verifiedQuery.paramName')} style={{ width: 120 }} />
                  </Form.Item>
                  <Form.Item name={[field.name, 'type']} initialValue="string" noStyle>
                    <Select style={{ width: 110 }}
                            options={PARAM_TYPES.map((v) => ({ value: v, label: v }))} />
                  </Form.Item>
                  <Form.Item name={[field.name, 'extract']} rules={[{ required: true }]} noStyle>
                    <Input placeholder={t('domainKnowledge.verifiedQuery.paramExtract')} style={{ width: 260 }} />
                  </Form.Item>
                  <Form.Item name={[field.name, 'enum']} noStyle>
                    <Select mode="tags" placeholder={t('domainKnowledge.verifiedQuery.paramEnum')}
                            style={{ width: 160 }} />
                  </Form.Item>
                  <Form.Item name={[field.name, 'required']} valuePropName="checked" noStyle>
                    <Switch size="small" defaultChecked />
                  </Form.Item>
                  <Button type="text" danger icon={<MinusCircleOutlined />}
                          onClick={() => remove(field.name)} />
                </Space>
              ))}
              <Button type="dashed" block icon={<PlusOutlined />} onClick={() => add()}>
                {t('domainKnowledge.verifiedQuery.addParam')}
              </Button>
            </div>
          )}
        </Form.List>
      </Form>
    </Modal>
  );
}
