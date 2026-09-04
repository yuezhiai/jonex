import React, { useEffect, useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Modal, Select, Table, message } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import type { SaveOntologyObjectPayload, SaveOntologyRelationPayload, ConstraintTargetType } from '@/types/domainKnowledge';
import {
  fetchTemplateDomains,
  fetchTemplateScenarios,
  fetchTemplateObjects,
  fetchTemplateRelations,
  fetchTemplateConstraints,
  type TemplateDomain,
  type TemplateScenario,
  type TemplateObject,
  type TemplateRelation,
  type TemplateConstraint,
} from '@/api/templateImport';
import {
  importOntologyObjectsFromTemplate,
  importOntologyRelationsFromTemplate,
  importOntologyConstraintsFromTemplate,
  type ConstraintImportItem,
} from '@/api/domainKnowledge';
import { normalizeAttrType, normalizeRelationType } from './constants';

interface Props {
  open: boolean;
  mode: 'object' | 'relation' | 'constraint';
  kbId: string;
  onClose: () => void;
  onImported: () => void;
}

interface ObjectRow extends TemplateObject {
  scenarioName: string;
}

interface RelationRow extends TemplateRelation {
  scenarioName: string;
}

interface ConstraintRow extends TemplateConstraint {
  scenarioName: string;
}

/** 模板约束类型 → 本体约束类型（中文值，与 ConstraintFormModal 的 CONSTRAINT_TYPE_OPTIONS 对齐） */
const CONSTRAINT_TYPE_MAP: Record<string, string> = {
  unique: '唯一',
  exists: '必填',
  range: '值域要求',
  conditional: '自定义',
};

/** 模板 target_type → 本体约束目标类型 i18n key（object 复用「对象」） */
const CONSTRAINT_TARGET_TYPE_LABEL: Record<string, string> = {
  object: 'compile.constraintTargetType.entity',
  attribute: 'compile.constraintTargetType.attribute',
  relation: 'compile.constraintTargetType.relation',
};

export default function TemplateImportModal({ open, mode, kbId, onClose, onImported }: Props) {
  const { t } = useTranslation();
  const [domains, setDomains] = useState<TemplateDomain[]>([]);
  const [scenarios, setScenarios] = useState<TemplateScenario[]>([]);
  const [domainId, setDomainId] = useState<string | undefined>();
  const [scenarioIds, setScenarioIds] = useState<string[]>([]);
  const [objectRows, setObjectRows] = useState<ObjectRow[]>([]);
  const [relationRows, setRelationRows] = useState<RelationRow[]>([]);
  const [constraintRows, setConstraintRows] = useState<ConstraintRow[]>([]);
  const [attrOwnerById, setAttrOwnerById] = useState<Map<string, { objectName: string; attrName: string }>>(new Map());
  const [selectedKeys, setSelectedKeys] = useState<React.Key[]>([]);
  const [loadingDomains, setLoadingDomains] = useState(false);
  const [loadingScenarios, setLoadingScenarios] = useState(false);
  const [loadingRows, setLoadingRows] = useState(false);
  const [submitting, setSubmitting] = useState(false);

  const scenarioNameMap = useMemo(() => new Map(scenarios.map((s) => [s.id, s.name])), [scenarios]);

  useEffect(() => {
    if (!open) return;
    setDomainId(undefined);
    setScenarios([]);
    setScenarioIds([]);
    setObjectRows([]);
    setRelationRows([]);
    setConstraintRows([]);
    setAttrOwnerById(new Map());
    setSelectedKeys([]);
    setLoadingDomains(true);
    fetchTemplateDomains()
      .then((r) => setDomains(r.items || []))
      .catch((e) => message.error(e?.message || t('compile.template.loadDomainFailed')))
      .finally(() => setLoadingDomains(false));
  }, [open]);

  useEffect(() => {
    if (!open) return;
    setScenarioIds([]);
    setObjectRows([]);
    setRelationRows([]);
    setConstraintRows([]);
    setAttrOwnerById(new Map());
    setSelectedKeys([]);
    if (!domainId) {
      setScenarios([]);
      return;
    }
    setLoadingScenarios(true);
    fetchTemplateScenarios(domainId)
      .then((r) => setScenarios(r.items || []))
      .catch((e) => message.error(e?.message || t('compile.template.loadScenarioFailed')))
      .finally(() => setLoadingScenarios(false));
  }, [open, domainId]);

  useEffect(() => {
    if (!open) return;
    setSelectedKeys([]);
    if (scenarioIds.length === 0) {
      setObjectRows([]);
      setRelationRows([]);
      setConstraintRows([]);
      setAttrOwnerById(new Map());
      return;
    }
    setLoadingRows(true);
    if (mode === 'object') {
      Promise.all(
        scenarioIds.map((sid) =>
          fetchTemplateObjects(sid).then((r) =>
            (r.items || []).map((o) => ({ ...o, scenarioName: scenarioNameMap.get(sid) || sid })),
          ),
        ),
      )
        .then((lists) => setObjectRows(lists.flat()))
        .catch((e) => message.error(e?.message || t('compile.template.loadObjectsFailed')))
        .finally(() => setLoadingRows(false));
    } else if (mode === 'relation') {
      Promise.all(
        scenarioIds.map((sid) =>
          fetchTemplateRelations(sid).then((r) =>
            (r.items || []).map((rel) => ({ ...rel, scenarioName: scenarioNameMap.get(sid) || sid })),
          ),
        ),
      )
        .then((lists) => setRelationRows(lists.flat()))
        .catch((e) => message.error(e?.message || t('compile.template.loadRelationsFailed')))
        .finally(() => setLoadingRows(false));
    } else {
      // constraint 模式：拉约束 + 对象（回填 attribute 约束的所属对象）
      Promise.all(
        scenarioIds.map((sid) =>
          Promise.all([fetchTemplateConstraints(sid), fetchTemplateObjects(sid)]).then(([cResp, oResp]) => ({
            constraints: (cResp.items || []).map((c) => ({ ...c, scenarioName: scenarioNameMap.get(sid) || sid })),
            objects: oResp.items || [],
          })),
        ),
      )
        .then((lists) => {
          setConstraintRows(lists.flatMap((l) => l.constraints));
          const owner = new Map<string, { objectName: string; attrName: string }>();
          lists.forEach((l) => {
            l.objects.forEach((o) => {
              (o.attributes || []).forEach((a) => {
                owner.set(a.id, { objectName: o.name, attrName: a.attr_name });
              });
            });
          });
          setAttrOwnerById(owner);
        })
        .catch((e) => message.error(e?.message || t('compile.template.loadConstraintsFailed')))
        .finally(() => setLoadingRows(false));
    }
  }, [open, mode, scenarioIds, scenarioNameMap]);

  const objectColumns: ColumnsType<ObjectRow> = [
    { title: t('compile.template.name'), dataIndex: 'name', key: 'name', render: (v) => <strong>{v}</strong> },
    { title: t('compile.template.description'), dataIndex: 'description', key: 'description', render: (v) => v || '—' },
    {
      title: t('compile.template.attrCount'),
      key: 'attrCount',
      width: 90,
      render: (_, r) => r.attributes?.length || 0,
    },
    { title: t('compile.template.sourceScenario'), dataIndex: 'scenarioName', key: 'scenarioName', width: 140 },
  ];

  const relationColumns: ColumnsType<RelationRow> = [
    {
      title: t('compile.template.sourceObject'),
      key: 'src',
      width: 120,
      render: (_, r) => r.source_object_name || '—',
    },
    { title: t('compile.template.relationName'), dataIndex: 'name', key: 'name', render: (v) => <strong>{v}</strong> },
    {
      title: t('compile.template.targetObject'),
      key: 'tgt',
      width: 120,
      render: (_, r) => r.target_object_name || '—',
    },
    { title: t('compile.template.relationType'), dataIndex: 'relation_type', key: 'relation_type', width: 90 },
    { title: t('compile.template.sourceScenario'), dataIndex: 'scenarioName', key: 'scenarioName', width: 140 },
  ];

  const constraintColumns: ColumnsType<ConstraintRow> = [
    { title: t('compile.constraint.name'), dataIndex: 'name', key: 'name', render: (v) => <strong>{v}</strong> },
    {
      title: t('compile.constraint.targetType'),
      dataIndex: 'target_type',
      key: 'target_type',
      width: 90,
      render: (v: string) => t(CONSTRAINT_TARGET_TYPE_LABEL[v] || v),
    },
    { title: t('compile.constraint.targetObject'), dataIndex: 'target_label', key: 'target_label', width: 160 },
    {
      title: t('compile.constraint.constraintType'),
      dataIndex: 'constraint_type',
      key: 'constraint_type',
      width: 100,
      render: (v: string) => CONSTRAINT_TYPE_MAP[v] || v,
    },
    { title: t('compile.template.sourceScenario'), dataIndex: 'scenarioName', key: 'scenarioName', width: 140 },
  ];

  const handleImport = async () => {
    setSubmitting(true);
    try {
      if (mode === 'object') {
        const picked = objectRows.filter((r) => selectedKeys.includes(r.id));
        const payloads: SaveOntologyObjectPayload[] = picked.map((o) => ({
          name: o.name,
          description: o.description || '',
          requirement: '',
          status: 'active',
          attributes: (o.attributes || []).map((a) => ({
            id: '',
            name: a.attr_name,
            description: a.description || '',
            type: normalizeAttrType(a.attr_type),
            isPrimaryKey: a.is_primary_key === true || a.is_primary_key === 1,
          })),
        }));
        const { created, skipped } = await importOntologyObjectsFromTemplate(kbId, payloads);
        const skipText = skipped ? t('compile.template.importSkippedSuffix', { skipped }) : '';
        message.success(t('compile.template.importObjectSuccess', { created: created.length }) + skipText);
      } else if (mode === 'relation') {
        const picked = relationRows.filter((r) => selectedKeys.includes(r.id));
        const payloads: SaveOntologyRelationPayload[] = picked.map((r) => ({
          sourceObject: r.source_object_name || t('compile.template.unknownSourceObject'),
          name: r.name,
          targetObject: r.target_object_name || t('compile.template.unknownTargetObject'),
          description: r.description || '',
          relationType: normalizeRelationType(r.relation_type),
          status: 'active',
        }));
        const { created, skipped } = await importOntologyRelationsFromTemplate(kbId, payloads);
        const skipText = skipped ? t('compile.template.importSkippedSuffix', { skipped }) : '';
        message.success(t('compile.template.importRelationSuccess', { created: created.length }) + skipText);
      } else {
        const picked = constraintRows.filter((r) => selectedKeys.includes(r.id));
        const items: ConstraintImportItem[] = picked.map((c) => {
          const targetType: ConstraintTargetType =
            c.target_type === 'attribute' ? 'attribute' : c.target_type === 'relation' ? 'relation' : 'entity';
          let entityName: string | undefined;
          let targetName = c.target_label;
          if (c.target_type === 'attribute') {
            const owner = attrOwnerById.get(c.target_id);
            entityName = owner?.objectName;
            targetName = owner?.attrName || c.target_label;
          }
          return {
            name: c.name,
            targetType,
            entityName,
            targetName,
            constraintType: CONSTRAINT_TYPE_MAP[c.constraint_type] || '自定义',
            expression: c.expression || undefined,
            suggestion: c.suggestion || undefined,
          };
        });
        const { created, skipped } = await importOntologyConstraintsFromTemplate(kbId, items);
        const skipText = skipped ? t('compile.template.importSkippedSuffix', { skipped }) : '';
        message.success(t('compile.template.importConstraintSuccess', { created: created.length }) + skipText);
      }
      onImported();
      onClose();
    } catch (e: any) {
      message.error(e?.message || t('compile.template.importFailed'));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Modal
      title={t(
        mode === 'object'
          ? 'compile.template.importTitleObjects'
          : mode === 'relation'
            ? 'compile.template.importTitleRelations'
            : 'compile.template.importTitleConstraints',
      )}
      open={open}
      onCancel={onClose}
      onOk={handleImport}
      okText={t('compile.template.importOkText')}
      cancelText={t('common.cancel')}
      confirmLoading={submitting}
      okButtonProps={{ disabled: selectedKeys.length === 0 }}
      width={760}
      destroyOnHidden
    >
      <div style={{ display: 'flex', gap: 12, marginBottom: 16 }}>
        <Select
          style={{ width: 220 }}
          placeholder={t('compile.template.selectDomain')}
          loading={loadingDomains}
          allowClear
          value={domainId}
          onChange={(v) => setDomainId(v)}
          options={domains.map((d) => ({ label: d.name, value: d.id }))}
        />
        <Select
          style={{ flex: 1 }}
          mode="multiple"
          placeholder={t('compile.template.selectScenario')}
          loading={loadingScenarios}
          value={scenarioIds}
          onChange={(v) => setScenarioIds(v)}
          disabled={!domainId}
          options={scenarios.map((s) => ({ label: s.name, value: s.id }))}
        />
      </div>
      {mode === 'object' ? (
        <Table
          rowKey="id"
          size="small"
          loading={loadingRows}
          pagination={false}
          columns={objectColumns}
          dataSource={objectRows}
          rowSelection={{ selectedRowKeys: selectedKeys, onChange: setSelectedKeys }}
          locale={{ emptyText: t('compile.template.emptyObjects') }}
          scroll={{ y: 320 }}
        />
      ) : mode === 'relation' ? (
        <Table
          rowKey="id"
          size="small"
          loading={loadingRows}
          pagination={false}
          columns={relationColumns}
          dataSource={relationRows}
          rowSelection={{ selectedRowKeys: selectedKeys, onChange: setSelectedKeys }}
          locale={{ emptyText: t('compile.template.emptyRelations') }}
          scroll={{ y: 320 }}
        />
      ) : (
        <Table
          rowKey="id"
          size="small"
          loading={loadingRows}
          pagination={false}
          columns={constraintColumns}
          dataSource={constraintRows}
          rowSelection={{ selectedRowKeys: selectedKeys, onChange: setSelectedKeys }}
          locale={{ emptyText: t('compile.template.emptyConstraints') }}
          scroll={{ y: 320 }}
        />
      )}
    </Modal>
  );
}
