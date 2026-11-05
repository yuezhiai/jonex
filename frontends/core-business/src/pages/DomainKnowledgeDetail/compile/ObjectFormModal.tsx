import React, { useEffect, useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Modal, Form, Input, Select, Switch, Button, Alert, message } from 'antd';
import { PlusOutlined, DeleteOutlined } from '@ant-design/icons';
import type {
  ExtractionSkillRef,
  OntologyAttrType,
  OntologyObjectDef,
  SaveOntologyObjectPayload,
} from '@/types/domainKnowledge';
import { ATTR_TYPE_OPTIONS } from './constants';
import ExtractToolDeclarations, { type AttrOption } from './ExtractToolDeclarations';
import { attributeKeyOf, mergeSkills, splitSkills, type CatalogDraft } from './extractToolDraft';

interface AttrFormItem {
  id?: string;
  name?: string;
  description?: string;
  type?: OntologyAttrType;
  isPrimaryKey?: boolean;
  compiledName?: string;
}

interface ObjectFormValues {
  name: string;
  description: string;
  requirement: string;
  attributes?: AttrFormItem[];
}

interface Props {
  open: boolean;
  editing: OntologyObjectDef | null;
  submitting: boolean;
  onCancel: () => void;
  onSubmit: (payload: SaveOntologyObjectPayload) => void;
  /** 知识库 id：抽取期工具一节要拉工具列表、试跑 */
  kbId?: string;
  /** 本库「抽取期调用工具」开关是否打开：打开才显示声明编辑 */
  extractToolEnabled?: boolean;
}

const EMPTY_BUILTIN = { refine: [] as ExtractionSkillRef[], constrain: [] as ExtractionSkillRef[] };

export default function ObjectFormModal({
  open,
  editing,
  submitting,
  onCancel,
  onSubmit,
  kbId,
  extractToolEnabled = false,
}: Props) {
  const { t } = useTranslation();
  const [form] = Form.useForm<ObjectFormValues>();
  const [drafts, setDrafts] = useState<CatalogDraft[]>([]);
  const [builtin, setBuiltin] = useState(EMPTY_BUILTIN);
  const [draftsTouched, setDraftsTouched] = useState(false);
  const watchedAttrs = Form.useWatch('attributes', form) as AttrFormItem[] | undefined;

  useEffect(() => {
    if (!open) return;
    const split = splitSkills(editing?.extractionSkills);
    setDrafts(split.drafts);
    setBuiltin(split.builtin);
    setDraftsTouched(false);
    if (editing) {
      form.setFieldsValue({
        name: editing.name,
        description: editing.description,
        requirement: editing.requirement,
        attributes: editing.attributes.map((attr) => ({
          id: attr.id,
          name: attr.name,
          description: attr.description,
          type: attr.type,
          isPrimaryKey: attr.isPrimaryKey,
          compiledName: attr.compiledName,
        })),
      });
      return;
    }
    form.setFieldsValue({
      name: '',
      description: '',
      requirement: '',
      attributes: [{ type: '字符串', isPrimaryKey: false }],
    });
  }, [editing, form, open]);

  // 写入 / 入参下拉用的属性键：已有属性用编译键，新加的属性按保存时的推导规则算
  const attrOptions: AttrOption[] = useMemo(() => {
    const seen = new Set<string>();
    const out: AttrOption[] = [];
    (watchedAttrs || []).forEach((attr) => {
      const label = (attr?.name || '').trim();
      const key = attr?.compiledName || attributeKeyOf(label);
      if (!key || seen.has(key)) return;
      seen.add(key);
      out.push({ key, label: label || key });
    });
    return out;
  }, [watchedAttrs]);

  // 声明写入、但还不在属性定义里的属性：保存时自动加进来，这里先告诉用户
  const newAttrs = useMemo(() => {
    if (!extractToolEnabled) return [];
    const keys = new Set(attrOptions.map((a) => a.key));
    const out: string[] = [];
    drafts.forEach((d) => {
      if (d.group !== 'refine') return;
      d.writes.forEach((w) => {
        const attr = (w.attr || '').trim();
        if (attr && !keys.has(attr) && !out.includes(attr)) out.push(attr);
      });
    });
    return out;
  }, [attrOptions, drafts, extractToolEnabled]);

  async function handleOk() {
    const values = await form.validateFields();
    // 开关关着时不显示编辑区，也不碰声明（undefined = 保存时保留原值）
    let extractionSkills: SaveOntologyObjectPayload['extractionSkills'];
    if (extractToolEnabled && (draftsTouched || !editing)) {
      const merged = mergeSkills(builtin, drafts);
      if (merged.errors.length) {
        const first = merged.errors[0];
        message.error(
          t('compile.extractTool.decl.saveBlocked', {
            index: first.index + 1,
            reason: t(`compile.extractTool.err.${first.errors[0].code}`, first.errors[0].params || {}),
          }),
        );
        return;
      }
      extractionSkills = merged.config;
    }
    onSubmit({
      name: values.name.trim(),
      description: (values.description || '').trim(),
      requirement: (values.requirement || '').trim(),
      status: editing?.status || 'active',
      extractionSkills,
      attributes: (values.attributes || [])
        .filter((item) => (item.name || '').trim())
        .map((item) => ({
          id: item.id || '',
          name: (item.name || '').trim(),
          description: (item.description || '').trim(),
          type: item.type || '字符串',
          isPrimaryKey: Boolean(item.isPrimaryKey),
        })),
    });
  }

  const catalogCount = drafts.length;

  return (
    <Modal
      title={editing ? t('compile.objectEdit') : t('compile.objectNew')}
      open={open}
      onCancel={onCancel}
      onOk={handleOk}
      okText={t('common.confirm')}
      cancelText={t('common.cancel')}
      confirmLoading={submitting}
      width={extractToolEnabled ? 1040 : 860}
      destroyOnHidden
    >
      <Form form={form} layout="vertical" preserve={false}>
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12 }}>
          <Form.Item
            label={t('compile.object.name')}
            name="name"
            rules={[{ required: true, whitespace: true, message: t('compile.object.nameMessage') }]}
          >
            <Input placeholder={t('compile.object.namePlaceholder')} />
          </Form.Item>
          <Form.Item label={t('compile.object.description')} name="description">
            <Input placeholder={t('compile.object.descriptionPlaceholder')} />
          </Form.Item>
        </div>
        <Form.Item label={t('compile.object.requirement')} name="requirement">
          <Input.TextArea rows={2} placeholder={t('compile.object.requirementPlaceholder')} />
        </Form.Item>
        <Form.List name="attributes">
          {(fields, { add, remove }) => (
            <div>
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 8 }}>
                <label>{t('compile.object.attributes')}</label>
                <Button
                  type="dashed"
                  size="small"
                  icon={<PlusOutlined />}
                  onClick={() => add({ type: '字符串', isPrimaryKey: false })}
                >
                  {t('compile.addAttribute')}
                </Button>
              </div>
              {fields.map((field) => (
                <div
                  key={field.key}
                  style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 130px 90px 40px', gap: 8, marginBottom: 8 }}
                >
                  <Form.Item
                    name={[field.name, 'name']}
                    rules={[{ required: true, whitespace: true, message: t('compile.object.attributeNameRequired') }]}
                    style={{ marginBottom: 0 }}
                  >
                    <Input placeholder={t('compile.object.attributeNamePlaceholder')} />
                  </Form.Item>
                  <Form.Item name={[field.name, 'description']} style={{ marginBottom: 0 }}>
                    <Input placeholder={t('compile.object.attributeDescriptionPlaceholder')} />
                  </Form.Item>
                  <Form.Item name={[field.name, 'type']} style={{ marginBottom: 0 }}>
                    <Select options={ATTR_TYPE_OPTIONS} />
                  </Form.Item>
                  <Form.Item name={[field.name, 'isPrimaryKey']} valuePropName="checked" style={{ marginBottom: 0 }}>
                    <Switch
                      checkedChildren={t('compile.object.primaryKey')}
                      unCheckedChildren={t('compile.object.normal')}
                    />
                  </Form.Item>
                  <Button danger type="text" icon={<DeleteOutlined />} onClick={() => remove(field.name)} />
                </div>
              ))}
            </div>
          )}
        </Form.List>
      </Form>

      {extractToolEnabled && kbId ? (
        <>
          <ExtractToolDeclarations
            kbId={kbId}
            entityType={editing?.compiledName}
            attributes={attrOptions}
            builtin={builtin}
            value={drafts}
            onChange={(next) => {
              setDrafts(next);
              setDraftsTouched(true);
            }}
          />
          {newAttrs.length > 0 && (
            <Alert
              type="info"
              showIcon
              style={{ marginTop: 8 }}
              title={t('compile.extractTool.decl.newAttrsHint', { attrs: newAttrs.join('、') })}
            />
          )}
        </>
      ) : (
        catalogCount > 0 && (
          <Alert
            type="info"
            showIcon
            style={{ marginTop: 12 }}
            title={t('compile.extractTool.decl.switchOff', { count: catalogCount })}
          />
        )
      )}
    </Modal>
  );
}
