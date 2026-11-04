import React, { useState, useCallback, forwardRef, useImperativeHandle } from 'react';
import { useTranslation } from 'react-i18next';
import { Button, Checkbox, Form, Input, Modal, Select, Tooltip, message } from 'antd';
import { DeleteOutlined, PlusOutlined } from '@ant-design/icons';
import { createTemplateObject, updateTemplateObject } from '../../api/templateScenarios';
import type { TemplateObject } from '../../api/templateScenarios';
import { getErrorMessage } from '@jonex/shared-lib';
import './ObjectModal.css';

const ATTRIBUTE_TYPE_VALUES = ['字符串', '数值', '日期', '枚举', '布尔', '文本'] as const;

type AttributeFormItem = {
  id?: string;
  name?: string;
  desc?: string;
  type?: string;
  isPrimary?: boolean;
};

type ObjectFormValues = {
  name: string;
  desc?: string;
  attrs?: AttributeFormItem[];
};

function createDraftId(prefix: string) {
  return `${prefix}_${Date.now()}_${Math.random().toString(36).slice(2, 7)}`;
}

function draftAttr(): AttributeFormItem {
  return { id: createDraftId('attr'), name: '', desc: '', type: ATTRIBUTE_TYPE_VALUES[0], isPrimary: false };
}

function isPrimaryKey(value: boolean | number | undefined) {
  return value === true || value === 1;
}

export interface ObjectModalHandle {
  openCreate: () => void;
  openEdit: (item: TemplateObject) => void;
}

interface ObjectModalProps {
  selectedSceneId: string | null;
  attributeTypeOptions: Array<{ label: string; value: string }>;
  onSaved: () => void;
}

const ObjectModal = forwardRef<ObjectModalHandle, ObjectModalProps>(function ObjectModal(
  { selectedSceneId, attributeTypeOptions, onSaved },
  ref,
) {
  const { t } = useTranslation();
  const [modalOpen, setModalOpen] = useState(false);
  const [editingObject, setEditingObject] = useState<TemplateObject | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [objectForm] = Form.useForm<ObjectFormValues>();

  useImperativeHandle(
    ref,
    () => ({
      openCreate: () => {
        setEditingObject(null);
        objectForm.setFieldsValue({
          name: '',
          desc: '',
          attrs: [{ id: createDraftId('attr'), name: '', desc: '', type: ATTRIBUTE_TYPE_VALUES[0], isPrimary: false }],
        });
        setModalOpen(true);
      },
      openEdit: (item: TemplateObject) => {
        setEditingObject(item);
        objectForm.setFieldsValue({
          name: item.name,
          desc: item.description || '',
          attrs: item.attributes.map((attr) => ({
            id: attr.id,
            name: attr.attr_name,
            desc: attr.description || '',
            type: attr.attr_type,
            isPrimary: isPrimaryKey(attr.is_primary_key),
          })),
        });
        setModalOpen(true);
      },
    }),
    [objectForm],
  );

  const handleSave = useCallback(async () => {
    if (!selectedSceneId) return;
    const values = await objectForm.validateFields();
    const attributes = (values.attrs || []).map((attr, index) => ({
      attr_name: attr.name?.trim() || '',
      description: attr.desc?.trim() || '',
      attr_type: attr.type || ATTRIBUTE_TYPE_VALUES[0],
      is_primary_key: Boolean(attr.isPrimary),
      sort_order: index,
    }));

    setSubmitting(true);
    try {
      if (editingObject) {
        await updateTemplateObject(editingObject.id, {
          name: values.name.trim(),
          description: values.desc?.trim() || '',
          attributes,
        });
        message.success(t('templateScenarios.objectUpdated'));
      } else {
        await createTemplateObject(selectedSceneId, {
          name: values.name.trim(),
          description: values.desc?.trim() || '',
          attributes,
        });
        message.success(t('templateScenarios.objectCreated'));
      }
      setModalOpen(false);
      onSaved();
    } catch (error) {
      message.error(
        getErrorMessage(
          error,
          editingObject ? t('templateScenarios.updateObjectFailed') : t('templateScenarios.createObjectFailed'),
        ),
      );
    } finally {
      setSubmitting(false);
    }
  }, [selectedSceneId, editingObject, objectForm, onSaved, t]);

  return (
    <Modal
      className="tom-modal"
      title={editingObject ? t('templateScenarios.editObject') : t('templateScenarios.createObjectTitle')}
      open={modalOpen}
      onCancel={() => setModalOpen(false)}
      onOk={handleSave}
      okText={t('common.save')}
      cancelText={t('common.cancel')}
      confirmLoading={submitting}
      width={860}
      styles={{ body: { paddingTop: 6 } }}
    >
      <Form form={objectForm} layout="vertical" className="tom-form">
        <p className="tom-hint">{t('templateScenarios.objectFormHint')}</p>

        <div className="tom-basic">
          <Form.Item
            label={t('common.name')}
            name="name"
            rules={[{ required: true, whitespace: true, message: t('templateScenarios.objectNameInput') }]}
          >
            <Input placeholder={t('templateScenarios.objectNameInput')} maxLength={64} />
          </Form.Item>
          <Form.Item label={t('common.description')} name="desc">
            <Input.TextArea
              placeholder={t('templateScenarios.objectDescPlaceholder')}
              autoSize={{ minRows: 2, maxRows: 4 }}
              maxLength={512}
              showCount
            />
          </Form.Item>
        </div>

        <Form.List name="attrs">
          {(fields, { add, remove }) => (
            <div className="tom-attrs">
              <div className="tom-attrs-head">
                <span className="tom-attrs-title">
                  {t('templateScenarios.attrDefSection')}
                  <span className="tom-attrs-count">{fields.length}</span>
                </span>
                <Button type="dashed" size="small" icon={<PlusOutlined />} onClick={() => add(draftAttr())}>
                  {t('templateScenarios.addAttribute')}
                </Button>
              </div>

              {fields.length === 0 ? (
                <div className="tom-empty">
                  <span>{t('templateScenarios.attrEmptyHint')}</span>
                  <Button type="link" size="small" icon={<PlusOutlined />} onClick={() => add(draftAttr())}>
                    {t('templateScenarios.addAttribute')}
                  </Button>
                </div>
              ) : (
                <>
                  <div className="tom-row tom-row-head">
                    <span>{t('common.name')}</span>
                    <span>{t('common.description')}</span>
                    <span>{t('templateScenarios.attrType')}</span>
                    <span className="tom-row-key">{t('templateScenarios.uniquePrimaryKey')}</span>
                    <span />
                  </div>

                  {fields.map((field) => (
                    <div className="tom-row" key={field.key}>
                      <Form.Item
                        name={[field.name, 'name']}
                        rules={[{ required: true, whitespace: true, message: t('templateScenarios.attrNameRequired') }]}
                      >
                        <Input placeholder={t('templateScenarios.attrNamePlaceholder')} maxLength={64} />
                      </Form.Item>
                      <Form.Item name={[field.name, 'desc']}>
                        <Input placeholder={t('templateScenarios.attrDescPlaceholder')} maxLength={200} />
                      </Form.Item>
                      <Form.Item name={[field.name, 'type']}>
                        <Select options={attributeTypeOptions} />
                      </Form.Item>
                      <Form.Item name={[field.name, 'isPrimary']} valuePropName="checked" className="tom-row-key">
                        <Checkbox aria-label={t('templateScenarios.uniquePrimaryKey')} />
                      </Form.Item>
                      <Tooltip title={t('common.delete')}>
                        <Button
                          type="text"
                          danger
                          className="tom-remove"
                          aria-label={t('common.delete')}
                          icon={<DeleteOutlined />}
                          onClick={() => remove(field.name)}
                        />
                      </Tooltip>
                    </div>
                  ))}
                </>
              )}
            </div>
          )}
        </Form.List>
      </Form>
    </Modal>
  );
});

export default ObjectModal;
