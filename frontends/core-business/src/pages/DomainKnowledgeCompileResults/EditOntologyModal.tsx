import { useState, useEffect } from 'react';
import { useTranslation } from 'react-i18next';
import { Form, Modal, Button, Input, Select, message } from 'antd';
import { SaveOutlined, CloseOutlined } from '@ant-design/icons';

interface EditOntologyModalProps {
  open: boolean;
  mode?: 'create' | 'edit';
  /** 编辑模式时传入实例数据 */
  record: {
    name: string;
    type: string;
    aliases?: string[];
    description?: string;
    attributes?: Record<string, unknown> | null;
  } | null;
  ontologyOptions: Array<{ value: string; label: string }>;
  onSave: (data: {
    name: string;
    type: string;
    aliases?: string[];
    description?: string;
    attributes?: Record<string, unknown>;
  }) => void;
  onCancel: () => void;
}

export default function EditOntologyModal({
  open,
  mode = 'edit',
  record,
  ontologyOptions,
  onSave,
  onCancel,
}: EditOntologyModalProps) {
  const { t } = useTranslation();
  const isCreate = mode === 'create';
  const [form] = Form.useForm();

  const [aliases, setAliases] = useState<string[]>([]);
  const [aliasInput, setAliasInput] = useState('');
  const [attributes, setAttributes] = useState<Record<string, string>>({});
  const [attrKey, setAttrKey] = useState('');
  const [attrValue, setAttrValue] = useState('');

  useEffect(() => {
    if (open) {
      if (isCreate) {
        form.setFieldsValue({ type: undefined, name: '', description: '' });
        setAliases([]);
        setAttributes({});
      } else if (record) {
        form.setFieldsValue({
          type: record.type,
          name: record.name,
          description: record.description ?? '',
        });
        setAliases(record.aliases ?? []);
        setAttributes(
          record.attributes
            ? Object.fromEntries(
                Object.entries(record.attributes).map(([k, v]) => [k, typeof v === 'string' ? v : JSON.stringify(v)]),
              )
            : {},
        );
      }
      setAliasInput('');
      setAttrKey('');
      setAttrValue('');
    }
  }, [open, record, isCreate, form]);

  const handleSave = async () => {
    try {
      const values = await form.validateFields();
      const payload = {
        name: values.name.trim(),
        type: values.type,
        aliases: aliases.length > 0 ? aliases : undefined,
        description: values.description?.trim() || undefined,
        attributes:
          Object.keys(attributes).length > 0
            ? Object.fromEntries(Object.entries(attributes).filter(([, v]) => v !== ''))
            : undefined,
      };
      onSave(payload);
    } catch {
      /* 校验失败，由 Form 提示 */
    }
  };

  const addAlias = () => {
    const val = aliasInput.trim();
    if (val && !aliases.includes(val)) {
      setAliases([...aliases, val]);
    }
    setAliasInput('');
  };

  const removeAlias = (alias: string) => {
    setAliases(aliases.filter((a) => a !== alias));
  };

  const addAttributeItem = () => {
    const key = attrKey.trim();
    if (key && !(key in attributes)) {
      setAttributes({ ...attributes, [key]: attrValue });
    }
    setAttrKey('');
    setAttrValue('');
  };

  const removeAttributeItem = (key: string) => {
    const next = { ...attributes };
    delete next[key];
    setAttributes(next);
  };

  return (
    <Modal
      title={
        <span style={{ fontSize: 18, fontWeight: 700, color: 'var(--yx-brand-dark)' }}>
          {isCreate ? t('compile.createInstance') : t('compile.editInstance', { name: record?.name || '' })}
        </span>
      }
      open={open}
      onCancel={onCancel}
      width={560}
      centered
      footer={
        <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 12 }}>
          <Button size="middle" onClick={onCancel}>
            {t('common.cancel')}
          </Button>
          <Button type="primary" size="middle" icon={<SaveOutlined />} onClick={handleSave}>
            {t('common.save')}
          </Button>
        </div>
      }
      styles={{ body: { padding: '24px 24px 12px' } }}
      closeIcon={<CloseOutlined style={{ color: 'var(--yx-text-secondary)' }} />}
    >
      <Form form={form} layout="vertical">
        <Form.Item
          name="type"
          label={t('domainKnowledge.entityType')}
          rules={[{ required: true, message: t('common.required') }]}
        >
          <Select options={ontologyOptions} placeholder={t('compile.ontologyDefPlaceholder')} size="middle" />
        </Form.Item>

        <Form.Item
          name="name"
          label={t('compile.instanceName')}
          rules={[{ required: true, message: t('common.nameRequired') }]}
        >
          <Input placeholder={t('compile.instanceNamePlaceholder')} size="middle" />
        </Form.Item>

        {/* 别名 */}
        <div style={{ marginBottom: 24 }}>
          <div
            style={{
              fontSize: 14,
              fontWeight: 600,
              color: 'var(--yx-brand-dark)',
              marginBottom: 8,
            }}
          >
            {t('domainKnowledge.alias')}
          </div>
          <div style={{ display: 'flex', gap: 8, marginBottom: 8 }}>
            <Input
              placeholder={t('compile.aliasPlaceholder')}
              value={aliasInput}
              onChange={(e) => setAliasInput(e.target.value)}
              onPressEnter={(e) => {
                e.preventDefault();
                addAlias();
              }}
              style={{ flex: 1 }}
              size="middle"
            />
            <Button onClick={addAlias}>{t('common.add')}</Button>
          </div>
          {aliases.length > 0 && (
            <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
              {aliases.map((alias) => (
                <span
                  key={alias}
                  style={{
                    display: 'inline-flex',
                    alignItems: 'center',
                    gap: 4,
                    padding: '2px 8px',
                    borderRadius: 6,
                    background: 'var(--yx-info-bg)',
                    color: 'var(--yx-accent)',
                    fontSize: 12,
                  }}
                >
                  {alias}
                  <CloseOutlined style={{ fontSize: 10, cursor: 'pointer' }} onClick={() => removeAlias(alias)} />
                </span>
              ))}
            </div>
          )}
        </div>

        <Form.Item name="description" label={t('common.description')}>
          <Input.TextArea rows={2} placeholder={t('compile.createInstanceDescPlaceholder')} />
        </Form.Item>

        {/* 自定义属性 */}
        <Form.Item label={t('compile.customAttributes')}>
          <div
            style={{
              background: 'var(--yx-bg-subtle)',
              borderRadius: 12,
              padding: 16,
              border: '1px solid var(--yx-border-light)',
            }}
          >
            <div style={{ display: 'flex', gap: 8, marginBottom: 12 }}>
              <Input
                placeholder={t('compile.attrKeyPlaceholder')}
                value={attrKey}
                onChange={(e) => setAttrKey(e.target.value)}
                style={{ width: 160 }}
                size="middle"
              />
              <Input
                placeholder={t('compile.attrValuePlaceholder')}
                value={attrValue}
                onChange={(e) => setAttrValue(e.target.value)}
                onPressEnter={(e) => {
                  e.preventDefault();
                  addAttributeItem();
                }}
                style={{ flex: 1 }}
                size="middle"
              />
              <Button onClick={addAttributeItem}>{t('common.add')}</Button>
            </div>
            {Object.keys(attributes).length > 0 && (
              <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
                {Object.entries(attributes).map(([k, v]) => (
                  <div
                    key={k}
                    style={{
                      display: 'flex',
                      alignItems: 'center',
                      justifyContent: 'space-between',
                      padding: '4px 10px',
                      borderRadius: 6,
                      background: '#fff',
                      border: '1px solid var(--yx-border)',
                      fontSize: 13,
                    }}
                  >
                    <span style={{ fontWeight: 500, color: 'var(--yx-brand-dark)' }}>{k}</span>
                    <span
                      style={{
                        display: 'flex',
                        alignItems: 'center',
                        gap: 8,
                      }}
                    >
                      <span style={{ color: 'var(--yx-text-secondary)' }}>{v}</span>
                      <CloseOutlined
                        style={{
                          fontSize: 10,
                          cursor: 'pointer',
                          color: 'var(--yx-text-muted)',
                        }}
                        onClick={() => removeAttributeItem(k)}
                      />
                    </span>
                  </div>
                ))}
              </div>
            )}
          </div>
        </Form.Item>
      </Form>
    </Modal>
  );
}
