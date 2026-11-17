import React, { useState, useCallback, forwardRef, useImperativeHandle } from 'react';
import { useTranslation } from 'react-i18next';
import { Form, message, Modal, Select } from 'antd';
import { createOntologyRelation, updateOntologyRelation, searchOntologyInstances } from '@/api/domainKnowledge';
import type { RelationInstanceRow } from '@/types/domainKnowledge';

const ENTITY_SEP = '|';

export interface RelationFormModalHandle {
  openCreate: () => void;
  openEdit: (row: RelationInstanceRow) => void;
}

interface RelationFormModalProps {
  id: string;
  typeNameMap: Record<string, string>;
  relTypeOptions: Array<{ value: string; label: string }>;
  onSaved: () => void;
}

const RelationFormModal = forwardRef<RelationFormModalHandle, RelationFormModalProps>(function RelationFormModal(
  { id, typeNameMap, relTypeOptions, onSaved },
  ref,
) {
  const { t } = useTranslation();

  const [modalOpen, setModalOpen] = useState(false);
  const [modalMode, setModalMode] = useState<'create' | 'edit'>('create');
  const [saving, setSaving] = useState(false);
  const [form] = Form.useForm();

  const [sourceOptions, setSourceOptions] = useState<Array<{ value: string; label: string }>>([]);
  const [targetOptions, setTargetOptions] = useState<Array<{ value: string; label: string }>>([]);

  const [editIdentity, setEditIdentity] = useState<{
    sourceType: string;
    sourceName: string;
    relType: string;
    targetType: string;
    targetName: string;
  } | null>(null);

  // ── 暴露给父组件的方法 ──
  useImperativeHandle(
    ref,
    () => ({
      openCreate: () => {
        setModalMode('create');
        setSourceOptions([]);
        setTargetOptions([]);
        setEditIdentity(null);
        form.setFieldsValue({ source: undefined, rel_type: undefined, target: undefined });
        setModalOpen(true);
      },
      openEdit: (row: RelationInstanceRow) => {
        setModalMode('edit');
        setSourceOptions([
          {
            value: `${row.source_type}${ENTITY_SEP}${row.source}`,
            label: `${row.source} (${row.source_type})`,
          },
        ]);
        setTargetOptions([
          {
            value: `${row.target_type}${ENTITY_SEP}${row.target}`,
            label: `${row.target} (${row.target_type})`,
          },
        ]);
        setEditIdentity({
          sourceType: row.source_type,
          sourceName: row.source,
          relType: row.relation_type,
          targetType: row.target_type,
          targetName: row.target,
        });
        form.setFieldsValue({
          source: `${row.source_type}${ENTITY_SEP}${row.source}`,
          rel_type: row.relation_type,
          target: `${row.target_type}${ENTITY_SEP}${row.target}`,
        });
        setModalOpen(true);
      },
    }),
    [form],
  );

  // ── 实体搜索 ──
  const searchEntities = useCallback(
    async (kw: string, setter: (opts: Array<{ value: string; label: string }>) => void) => {
      if (!id || !kw.trim()) return;
      try {
        const res = await searchOntologyInstances(id, kw.trim(), 20);
        const opts = (res.items || []).map((item: any) => ({
          value: `${item.entity_type || item.type || ''}${ENTITY_SEP}${item.canonical_name || item.name}`,
          label: `${item.canonical_name || item.name} (${item.entity_type || item.type})`,
        }));
        setter(opts);
      } catch {
        /* silent */
      }
    },
    [id],
  );

  const [debounceTimer, setDebounceTimer] = useState<ReturnType<typeof setTimeout> | null>(null);

  const debouncedSourceSearch = useCallback(
    (kw: string) => {
      if (debounceTimer) clearTimeout(debounceTimer);
      const timer = setTimeout(() => searchEntities(kw, setSourceOptions), 300);
      setDebounceTimer(timer);
    },
    [searchEntities, debounceTimer],
  );

  const debouncedTargetSearch = useCallback(
    (kw: string) => {
      if (debounceTimer) clearTimeout(debounceTimer);
      const timer = setTimeout(() => searchEntities(kw, setTargetOptions), 300);
      setDebounceTimer(timer);
    },
    [searchEntities, debounceTimer],
  );

  // ── 保存 ──
  const handleSave = async () => {
    try {
      const values = await form.validateFields();
      const [sourceType, ...sourceRest] = values.source.split(ENTITY_SEP);
      const [targetType, ...targetRest] = values.target.split(ENTITY_SEP);
      const sourceName = sourceRest.join(ENTITY_SEP).trim();
      const targetName = targetRest.join(ENTITY_SEP).trim();
      if (!sourceName || !values.rel_type || !targetName) return;

      setSaving(true);
      if (modalMode === 'create') {
        await createOntologyRelation(id, sourceType, sourceName, values.rel_type, targetType, targetName);
      } else if (editIdentity) {
        await updateOntologyRelation(
          id,
          editIdentity.sourceType,
          editIdentity.sourceName,
          editIdentity.relType,
          editIdentity.targetType,
          editIdentity.targetName,
          { relation_type: values.rel_type },
        );
      }
      setModalOpen(false);
      onSaved();
    } catch (err: any) {
      if (err && typeof err === 'object' && 'errorFields' in err) return;
      message.error(err?.message || t('common.saveFailed'));
    } finally {
      setSaving(false);
    }
  };

  return (
    <Modal
      title={
        <span style={{ fontSize: 18, fontWeight: 700, color: 'var(--yx-brand-dark)' }}>
          {modalMode === 'create' ? t('domainKnowledge.newRelation') : t('compile.relation.editBtn')}
        </span>
      }
      open={modalOpen}
      onCancel={() => setModalOpen(false)}
      width={600}
      centered
      confirmLoading={saving}
      okText={t('common.save')}
      cancelText={t('common.cancel')}
      onOk={handleSave}
      destroyOnClose
    >
      <Form form={form} layout="vertical" style={{ marginTop: 8 }}>
        {/* 源实体 */}
        <Form.Item
          name="source"
          label={t('compile.relation.sourceObject')}
          rules={[{ required: true, message: t('common.required') }]}
        >
          {modalMode === 'edit' ? (
            <div
              style={{
                padding: '6px 12px',
                background: 'var(--yx-bg-subtle)',
                borderRadius: 6,
                border: '1px solid var(--yx-border)',
                color: 'var(--yx-text-secondary)',
                fontSize: 13,
                minHeight: 34,
                display: 'flex',
                alignItems: 'center',
              }}
            >
              {(() => {
                const [st, ...sr] = (form.getFieldValue('source') || '').split(ENTITY_SEP);
                return `${sr.join(ENTITY_SEP)} (${typeNameMap[st] || st})`;
              })()}
            </div>
          ) : (
            <Select
              onSearch={debouncedSourceSearch}
              onChange={(v) => {
                const [type, ...rest] = v.split(ENTITY_SEP);
                const name = rest.join(ENTITY_SEP);
                setSourceOptions((prev) => {
                  if (prev.some((o) => o.value === v)) return prev;
                  return [{ value: v, label: `${name} (${type})` }, ...prev];
                });
              }}
              options={sourceOptions}
              placeholder={t('compile.relation.sourceEntityPlaceholder')}
              style={{ width: '100%' }}
              size="middle"
              showSearch
              filterOption={false}
              notFoundContent={null}
            />
          )}
        </Form.Item>

        {/* 关系类型 */}
        <Form.Item
          name="rel_type"
          label={t('compile.relation.name')}
          rules={[{ required: true, message: t('common.required') }]}
        >
          <Select options={relTypeOptions} placeholder={t('compile.relation.namePlaceholder')} size="middle" />
        </Form.Item>

        {/* 目标实体 */}
        <Form.Item
          name="target"
          label={t('compile.relation.targetObject')}
          rules={[{ required: true, message: t('common.required') }]}
        >
          {modalMode === 'edit' ? (
            <div
              style={{
                padding: '6px 12px',
                background: 'var(--yx-bg-subtle)',
                borderRadius: 6,
                border: '1px solid var(--yx-border)',
                color: 'var(--yx-text-secondary)',
                fontSize: 13,
                minHeight: 34,
                display: 'flex',
                alignItems: 'center',
              }}
            >
              {(() => {
                const [tt, ...tr] = (form.getFieldValue('target') || '').split(ENTITY_SEP);
                return `${tr.join(ENTITY_SEP)} (${typeNameMap[tt] || tt})`;
              })()}
            </div>
          ) : (
            <Select
              onSearch={debouncedTargetSearch}
              onChange={(v) => {
                const [type, ...rest] = v.split(ENTITY_SEP);
                const name = rest.join(ENTITY_SEP);
                setTargetOptions((prev) => {
                  if (prev.some((o) => o.value === v)) return prev;
                  return [{ value: v, label: `${name} (${type})` }, ...prev];
                });
              }}
              options={targetOptions}
              placeholder={t('compile.relation.targetEntityPlaceholder')}
              style={{ width: '100%' }}
              size="middle"
              showSearch
              filterOption={false}
              notFoundContent={null}
            />
          )}
        </Form.Item>
      </Form>
    </Modal>
  );
});

export default RelationFormModal;
