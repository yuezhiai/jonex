import { useState, useEffect } from 'react';
import { useTranslation } from 'react-i18next';
import { Form, Modal, Button, Select, message } from 'antd';
import { SaveOutlined, CloseOutlined } from '@ant-design/icons';
import debounce from 'lodash/debounce';
import { searchOntologyInstances } from '@/api/domainKnowledge';

const ENTITY_SEP = '|';

interface EditRelationModalProps {
  open: boolean;
  mode: 'create' | 'edit';
  kbId: string;
  /** 文档结果页传入：源/目标实体候选只列该文档关联的实体；KB 级页面不传 → 全库候选 */
  docId?: string;
  /** 关系类型选项 */
  relationTypeOptions: Array<{ value: string; label: string }>;
  /** 编辑模式时传入初始数据 */
  initialData?: {
    sourceType: string;
    sourceName: string;
    relationType: string;
    targetType: string;
    targetName: string;
  } | null;
  onSave: (data: {
    sourceType: string;
    sourceName: string;
    relationType: string;
    targetType: string;
    targetName: string;
  }) => void;
  onCancel: () => void;
}

export default function EditRelationModal({
  open,
  mode,
  kbId,
  docId,
  relationTypeOptions,
  initialData,
  onSave,
  onCancel,
}: EditRelationModalProps) {
  const { t } = useTranslation();
  const isCreate = mode === 'create';
  const [form] = Form.useForm();

  const [sourceOptions, setSourceOptions] = useState<Array<{ value: string; label: string }>>([]);
  const [targetOptions, setTargetOptions] = useState<Array<{ value: string; label: string }>>([]);

  useEffect(() => {
    if (open) {
      if (isCreate) {
        form.setFieldsValue({ source: undefined, rel_type: undefined, target: undefined });
        setSourceOptions([]);
        setTargetOptions([]);
      } else if (initialData) {
        form.setFieldsValue({
          source: `${initialData.sourceType}${ENTITY_SEP}${initialData.sourceName}`,
          rel_type: initialData.relationType,
          target: `${initialData.targetType}${ENTITY_SEP}${initialData.targetName}`,
        });
        setSourceOptions(
          initialData.sourceName
            ? [
                {
                  value: `${initialData.sourceType}${ENTITY_SEP}${initialData.sourceName}`,
                  label: `${initialData.sourceName} (${initialData.sourceType})`,
                },
              ]
            : [],
        );
        setTargetOptions(
          initialData.targetName
            ? [
                {
                  value: `${initialData.targetType}${ENTITY_SEP}${initialData.targetName}`,
                  label: `${initialData.targetName} (${initialData.targetType})`,
                },
              ]
            : [],
        );
      }
      // 打开时预取实体列表（文档页只预取该文档关联的实体）
      searchOntologyInstances(kbId, '', 20, docId)
        .then((res) => {
          const opts = buildEntityOptions(res.items || []);
          setSourceOptions((prev) => {
            if (isCreate) return opts;
            const merged = [...prev];
            opts.forEach((o) => {
              if (!merged.some((m) => m.value === o.value)) merged.push(o);
            });
            return merged;
          });
          setTargetOptions((prev) => {
            if (isCreate) return opts;
            const merged = [...prev];
            opts.forEach((o) => {
              if (!merged.some((m) => m.value === o.value)) merged.push(o);
            });
            return merged;
          });
        })
        .catch(() => {});
    }
  }, [open, initialData, isCreate, kbId, docId, form]);

  const buildEntityOptions = (items: any[]) =>
    items.map((item: any) => ({
      value: `${item.entity_type || item.type || ''}${ENTITY_SEP}${item.canonical_name || item.name}`,
      label: `${item.canonical_name || item.name} (${item.entity_type || item.type})`,
    }));

  const debouncedSourceSearch = debounce(async (kw: string) => {
    if (!kw.trim()) return;
    try {
      const res = await searchOntologyInstances(kbId, kw.trim(), 20, docId);
      setSourceOptions(buildEntityOptions(res.items || []));
    } catch {
      /* silent */
    }
  }, 300);

  const debouncedTargetSearch = debounce(async (kw: string) => {
    if (!kw.trim()) return;
    try {
      const res = await searchOntologyInstances(kbId, kw.trim(), 20, docId);
      setTargetOptions(buildEntityOptions(res.items || []));
    } catch {
      /* silent */
    }
  }, 300);

  const handleSourceSelect = (value: string) => {
    setSourceOptions((prev) => {
      if (prev.some((o) => o.value === value)) return prev;
      const [type, ...rest] = value.split(ENTITY_SEP);
      const label = `${rest.join(ENTITY_SEP)} (${type})`;
      return [{ value, label }, ...prev];
    });
  };

  const handleTargetSelect = (value: string) => {
    setTargetOptions((prev) => {
      if (prev.some((o) => o.value === value)) return prev;
      const [type, ...rest] = value.split(ENTITY_SEP);
      const label = `${rest.join(ENTITY_SEP)} (${type})`;
      return [{ value, label }, ...prev];
    });
  };

  const handleSave = async () => {
    try {
      const values = await form.validateFields();
      const [sourceType, ...sourceRest] = values.source.split(ENTITY_SEP);
      const [targetType, ...targetRest] = values.target.split(ENTITY_SEP);
      onSave({
        sourceType,
        sourceName: sourceRest.join(ENTITY_SEP).trim(),
        relationType: values.rel_type,
        targetType,
        targetName: targetRest.join(ENTITY_SEP).trim(),
      });
    } catch {
      /* 校验失败，由 Form 提示 */
    }
  };

  const handleCancel = () => {
    onCancel();
  };

  return (
    <Modal
      title={
        <span style={{ fontSize: 18, fontWeight: 700, color: '#0b2b5c' }}>
          {isCreate ? t('compile.relation.createBtn') : t('compile.relation.editBtn')}
        </span>
      }
      open={open}
      onCancel={handleCancel}
      width={600}
      centered
      footer={
        <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 12 }}>
          <Button size="middle" onClick={handleCancel}>
            {t('common.cancel')}
          </Button>
          <Button type="primary" size="middle" icon={<SaveOutlined />} onClick={handleSave}>
            {t('common.save')}
          </Button>
        </div>
      }
      styles={{ body: { padding: '24px 24px 12px' } }}
      closeIcon={<CloseOutlined style={{ color: '#64748b' }} />}
    >
      <Form form={form} layout="vertical">
        <Form.Item
          name="source"
          label={t('compile.relation.sourceObject')}
          rules={[{ required: true, message: t('common.required') }]}
        >
          <Select
            onSearch={(kw) => {
              debouncedSourceSearch(kw);
            }}
            onChange={handleSourceSelect}
            options={sourceOptions}
            placeholder={t('compile.relation.sourceEntityPlaceholder')}
            style={{ width: '100%' }}
            size="middle"
            showSearch
            filterOption={false}
            notFoundContent={undefined}
          />
        </Form.Item>

        <Form.Item
          name="rel_type"
          label={t('compile.relation.name')}
          rules={[{ required: true, message: t('common.required') }]}
        >
          <Select options={relationTypeOptions} placeholder={t('compile.relation.namePlaceholder')} size="middle" />
        </Form.Item>

        <Form.Item
          name="target"
          label={t('compile.relation.targetObject')}
          rules={[{ required: true, message: t('common.required') }]}
        >
          <Select
            onSearch={(kw) => {
              debouncedTargetSearch(kw);
            }}
            onChange={handleTargetSelect}
            options={targetOptions}
            placeholder={t('compile.relation.targetEntityPlaceholder')}
            style={{ width: '100%' }}
            size="middle"
            showSearch
            filterOption={false}
            notFoundContent={undefined}
          />
        </Form.Item>
      </Form>
    </Modal>
  );
}
