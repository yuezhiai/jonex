import React, { useState, useEffect } from 'react';
import { Modal, Form, Input, message } from 'antd';
import { PlusCircleOutlined } from '@ant-design/icons';
import { useTranslation } from 'react-i18next';
import { createSpace, updateSpace } from '@/api/domainSpace';
import type { DomainSpace, DomainSpaceFormData } from '@/types/domainSpace';

interface SpaceFormModalProps {
  open: boolean;
  editing: DomainSpace | null;
  onClose: () => void;
  onSaved: (saved: DomainSpace | null) => void;
}

export default function SpaceFormModal({ open, editing, onClose, onSaved }: SpaceFormModalProps) {
  const { t } = useTranslation();
  const [form, setForm] = useState<DomainSpaceFormData>({
    name: '',
    description: '',
  });
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    if (open) {
      if (editing) {
        setForm({
          name: editing.name,
          description: editing.description || '',
          status: editing.status,
        });
      } else {
        setForm({ name: '', description: '' });
      }
    }
  }, [open, editing]);

  const handleSave = async () => {
    if (!form.name.trim()) {
      message.warning(t('domainSpace.nameRequired'));
      return;
    }
    setSubmitting(true);
    try {
      let saved: DomainSpace | null = null;
      if (editing) {
        saved = await updateSpace(editing.id, form);
        message.success(t('domainSpace.updateSuccess'));
      } else {
        saved = await createSpace(form);
        message.success(t('domainSpace.createSuccess'));
      }
      onSaved(saved);
    } catch (err: unknown) {
      message.error(err instanceof Error ? err.message : t('common.saveFailed'));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Modal
      title={
        <span>
          <PlusCircleOutlined style={{ color: '#3b82f6', marginRight: 8 }} />
          {editing ? t('domainSpace.edit') : t('domainSpace.create')}
        </span>
      }
      open={open}
      onCancel={onClose}
      onOk={handleSave}
      confirmLoading={submitting}
      okText={editing ? t('common.save') : t('common.create')}
      cancelText={t('common.cancel')}
      destroyOnHidden
      width={480}
    >
      <Form layout="vertical">
        <Form.Item label={t('domainSpace.name')} required>
          <Input
            value={form.name}
            onChange={(e) => setForm((f) => ({ ...f, name: e.target.value }))}
            placeholder={t('domainSpace.namePlaceholder')}
            maxLength={255}
          />
        </Form.Item>
        <Form.Item label={t('domainSpace.description')}>
          <Input.TextArea
            value={form.description}
            onChange={(e) => setForm((f) => ({ ...f, description: e.target.value }))}
            placeholder={t('domainSpace.descriptionPlaceholder')}
            rows={3}
            maxLength={1024}
            showCount
          />
        </Form.Item>
      </Form>
    </Modal>
  );
}
