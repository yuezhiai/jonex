import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { MouseEvent } from 'react';
import { useTranslation } from 'react-i18next';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { Alert, Button, Menu, message, Modal, Select, Table, Tag, Typography } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import {
  ArrowLeftOutlined,
  DeleteOutlined,
  EditOutlined,
  ExportOutlined,
  ImportOutlined,
  PlusOutlined,
  ProfileOutlined,
  ReloadOutlined,
  SafetyOutlined,
  ShareAltOutlined,
} from '@ant-design/icons';
import dayjs from 'dayjs';
import {
  exportScenarioOntologyYaml,
  fetchTemplateConstraints,
  fetchTemplateDomains,
  fetchTemplateObjects,
  fetchTemplateRelations,
  fetchTemplateScenarios,
  importScenarioOntologyYaml,
} from '../../api/templateScenarios';
import type {
  TemplateAttribute,
  TemplateConstraint,
  TemplateDomain,
  TemplateObject,
  TemplateRelation,
  TemplateScenario,
  YamlImportResult,
} from '../../api/templateScenarios';
import {
  getTemplateAttributeDisplay,
  getTemplateDomainDisplay,
  getTemplateObjectDisplay,
  getTemplateRelationDisplay,
  getTemplateScenarioDisplay,
  isEnglishLanguage,
} from '../../utils/builtInTemplateDisplay';
import SceneModal, { type SceneModalHandle } from './SceneModal';
import ObjectModal, { type ObjectModalHandle } from './ObjectModal';
import RelationModal, { type RelationModalHandle } from './RelationModal';
import ConstraintModal, { type ConstraintModalHandle } from './ConstraintModal';
import DeleteConfirmModal, { type DeleteConfirmModalHandle } from './DeleteConfirmModal';
import './index.css';

const ATTRIBUTE_TYPE_VALUES = ['字符串', '数值', '日期', '枚举', '布尔', '文本'] as const;
const RELATION_TYPE_VALUES = ['一对一', '一对多', '多对一', '多对多'] as const;

function formatTime(value?: string | null) {
  if (!value) return '-';
  const parsed = dayjs(value);
  return parsed.isValid() ? parsed.format('YYYY-MM-DD HH:mm') : value;
}

function getErrorMessage(error: unknown, fallback: string) {
  const err = error as { response?: { data?: { message?: string } }; message?: string };
  return err.response?.data?.message || err.message || fallback;
}

export default function TemplateScenarios() {
  const [domains, setDomains] = useState<TemplateDomain[]>([]);
  const [scenes, setScenes] = useState<TemplateScenario[]>([]);
  const [objects, setObjects] = useState<TemplateObject[]>([]);
  const [relations, setRelations] = useState<TemplateRelation[]>([]);
  const [constraints, setConstraints] = useState<TemplateConstraint[]>([]);
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  // [jonex] 进入页面时按 URL domain_id 参数自动默认选中对应领域（首次渲染固定，后续变更不影响）
  const initialDomainIdRef = useRef(searchParams.get('domain_id') || '');
  const { t, i18n } = useTranslation();
  const english = isEnglishLanguage(i18n.resolvedLanguage || i18n.language);

  const attributeTypeOptions = useMemo(
    () => [
      { label: t('compile.attrType.string'), value: ATTRIBUTE_TYPE_VALUES[0] },
      { label: t('compile.attrType.number'), value: ATTRIBUTE_TYPE_VALUES[1] },
      { label: t('compile.attrType.date'), value: ATTRIBUTE_TYPE_VALUES[2] },
      { label: t('compile.attrType.enum'), value: ATTRIBUTE_TYPE_VALUES[3] },
      { label: t('compile.attrType.boolean'), value: ATTRIBUTE_TYPE_VALUES[4] },
      { label: t('compile.attrType.text'), value: ATTRIBUTE_TYPE_VALUES[5] },
    ],
    [t],
  );

  const relationTypeOptions = useMemo(
    () => [
      { label: t('compile.cardinality.oneToOne'), value: RELATION_TYPE_VALUES[0] },
      { label: t('compile.cardinality.oneToMany'), value: RELATION_TYPE_VALUES[1] },
      { label: t('compile.cardinality.manyToOne'), value: RELATION_TYPE_VALUES[2] },
      { label: t('compile.cardinality.manyToMany'), value: RELATION_TYPE_VALUES[3] },
    ],
    [t],
  );

  const sceneModalRef = useRef<SceneModalHandle>(null);
  const objectModalRef = useRef<ObjectModalHandle>(null);
  const relationModalRef = useRef<RelationModalHandle>(null);
  const constraintModalRef = useRef<ConstraintModalHandle>(null);
  const deleteModalRef = useRef<DeleteConfirmModalHandle>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const selectedFileRef = useRef<File | null>(null);

  const [domainFilter, setDomainFilter] = useState(initialDomainIdRef.current);
  const [selectedSceneId, setSelectedSceneId] = useState<string | null>(null);
  const [activeTab, setActiveTab] = useState<'objects' | 'relations' | 'constraints'>('objects');

  const [loadingDomains, setLoadingDomains] = useState(false);
  const [loadingScenes, setLoadingScenes] = useState(false);
  const [loadingDetails, setLoadingDetails] = useState(false);

  const [importModalVisible, setImportModalVisible] = useState(false);
  const [importResult, setImportResult] = useState<YamlImportResult | null>(null);
  const [importLoading, setImportLoading] = useState(false);

  const domainNameMap = useMemo(() => {
    return new Map(domains.map((domain) => [domain.id, getTemplateDomainDisplay(domain, t, english).name]));
  }, [domains, t, english]);

  const domainOptions = useMemo(() => {
    return domains.map((domain) => ({
      label: getTemplateDomainDisplay(domain, t, english).name,
      value: domain.id,
    }));
  }, [domains, t, english]);

  const selectedScene = useMemo(() => {
    return scenes.find((scene) => scene.id === selectedSceneId) ?? null;
  }, [scenes, selectedSceneId]);

  const objectSelectOptions = useMemo(() => {
    return objects.map((item) => ({
      label: getTemplateObjectDisplay(item, english).name,
      value: item.id,
    }));
  }, [english, objects]);

  const getDomainName = useCallback(
    (domainId: string) => domainNameMap.get(domainId) || domainId || '-',
    [domainNameMap],
  );

  const getObjectName = useCallback(
    (objectId?: string | null) => {
      const object = objects.find((item) => item.id === objectId);
      return object ? getTemplateObjectDisplay(object, english).name : objectId || '-';
    },
    [english, objects],
  );

  const refreshScenarios = useCallback(async (nextDomainId = '', preferredSceneId?: string | null) => {
    setLoadingScenes(true);
    try {
      const result = await fetchTemplateScenarios(nextDomainId || undefined);
      const items = result.items ?? [];
      const nextSelected =
        preferredSceneId && items.some((scene) => scene.id === preferredSceneId)
          ? preferredSceneId
          : (items[0]?.id ?? null);
      setScenes(items);
      setSelectedSceneId(nextSelected);
      if (!nextSelected) {
        setObjects([]);
        setRelations([]);
      }
      return nextSelected;
    } catch (error) {
      message.error(getErrorMessage(error, t('templateScenarios.loadSceneFailed')));
      setScenes([]);
      setSelectedSceneId(null);
      setObjects([]);
      setRelations([]);
      return null;
    } finally {
      setLoadingScenes(false);
    }
  }, []);

  const refreshTemplateDetails = useCallback(async (sceneId: string | null) => {
    if (!sceneId) {
      setObjects([]);
      setRelations([]);
      setConstraints([]);
      return;
    }

    setLoadingDetails(true);
    try {
      const [objectResult, relationResult, constraintResult] = await Promise.all([
        fetchTemplateObjects(sceneId),
        fetchTemplateRelations(sceneId),
        fetchTemplateConstraints(sceneId),
      ]);
      setObjects(objectResult.items ?? []);
      setRelations(relationResult.items ?? []);
      setConstraints(constraintResult.items ?? []);
    } catch (error) {
      message.error(getErrorMessage(error, t('templateScenarios.loadSceneDataFailed')));
      setObjects([]);
      setRelations([]);
    } finally {
      setLoadingDetails(false);
    }
  }, []);

  useEffect(() => {
    setLoadingDomains(true);
    fetchTemplateDomains()
      .then((result) => setDomains(result.items ?? []))
      .catch((error) => message.error(getErrorMessage(error, t('templateScenarios.loadDomainsFailed'))))
      .finally(() => setLoadingDomains(false));

    // [jonex] 进入页面时按 URL domain_id 参数自动默认选中对应领域
    void refreshScenarios(initialDomainIdRef.current);
  }, [refreshScenarios]);

  useEffect(() => {
    void refreshTemplateDetails(selectedSceneId);
  }, [refreshTemplateDetails, selectedSceneId]);

  // ── Callback handlers for modal onSaved / onDeleted ──

  const handleSceneSaved = useCallback(() => {
    void refreshScenarios(domainFilter, selectedSceneId);
  }, [domainFilter, selectedSceneId, refreshScenarios]);

  const handleDetailItemSaved = useCallback(() => {
    void refreshTemplateDetails(selectedSceneId);
  }, [selectedSceneId, refreshTemplateDetails]);

  const handleItemDeleted = useCallback(
    (deleteType: string) => {
      if (deleteType === 'scene') {
        void refreshScenarios(domainFilter);
      } else {
        void refreshTemplateDetails(selectedSceneId);
      }
    },
    [domainFilter, selectedSceneId, refreshScenarios, refreshTemplateDetails],
  );

  // ── Table column definitions ──

  const objectColumns: ColumnsType<TemplateObject> = [
    {
      title: t('common.name'),
      dataIndex: 'name',
      key: 'name',
      width: 160,
      render: (_, record) => <strong>{getTemplateObjectDisplay(record, english).name}</strong>,
    },
    {
      title: t('common.description'),
      dataIndex: 'description',
      key: 'description',
      ellipsis: true,
      render: (_, record) => {
        const description = getTemplateObjectDisplay(record, english).description;
        return description || <span className="template-scenarios-muted">{t('templateScenarios.noDescription')}</span>;
      },
    },
    {
      title: t('templateScenarios.attributes'),
      dataIndex: 'attributes',
      key: 'attributes',
      width: 340,
      render: (attrs: TemplateAttribute[]) =>
        attrs.length > 0 ? (
          <Table
            dataSource={attrs}
            rowKey="id"
            size="small"
            pagination={false}
            showHeader={false}
            className="attr-inline-table"
            style={{ width: '100%' }}
            columns={[
              {
                title: t('templateScenarios.attrNamePlaceholder'),
                dataIndex: 'attr_name',
                key: 'attr_name',
                width: 120,
                render: (_, record) => getTemplateAttributeDisplay(record, english).name,
              },
              {
                title: t('templateScenarios.attrType'),
                dataIndex: 'attr_type',
                key: 'attr_type',
                width: 80,
                render: (val) => attributeTypeOptions.find((o) => o.value === val)?.label || val,
              },
              {
                title: t('templateScenarios.uniquePrimaryKey'),
                key: 'primaryKey',
                width: 60,
                align: 'center',
                render: (_, record) =>
                  record.is_primary_key === true || record.is_primary_key === 1 ? (
                    <span className="template-scenarios-key-mark">✓</span>
                  ) : null,
              },
            ]}
          />
        ) : (
          <span className="template-scenarios-muted">{t('templateScenarios.noAttributes')}</span>
        ),
    },
    {
      title: t('common.createdAt'),
      dataIndex: 'created_at',
      key: 'created_at',
      width: 150,
      render: formatTime,
    },
    {
      title: t('common.actions'),
      key: 'action',
      width: 170,
      render: (_, record) => (
        <span className="template-scenarios-table-actions">
          <a className="yx-table-action" onClick={() => objectModalRef.current?.openEdit(record)}>
            <EditOutlined /> {t('common.edit')}
          </a>
          <a className="yx-table-action template-scenarios-danger-link" onClick={() => openDeleteObject(record)}>
            <DeleteOutlined /> {t('common.delete')}
          </a>
        </span>
      ),
    },
  ];

  const relationColumns: ColumnsType<TemplateRelation> = [
    {
      title: t('templateScenarios.sourceObjectSelect'),
      dataIndex: 'source_object_name',
      key: 'source_object_name',
      width: 140,
      render: (_, record) => <Tag color="processing">{getObjectName(record.source_object_id)}</Tag>,
    },
    {
      title: t('templateScenarios.relationNameLabel'),
      dataIndex: 'name',
      key: 'name',
      width: 130,
      render: (_, record) => (
        <strong>
          {
            getTemplateRelationDisplay(
              record,
              english,
              getObjectName(record.source_object_id),
              getObjectName(record.target_object_id),
            ).name
          }
        </strong>
      ),
    },
    {
      title: t('templateScenarios.targetObjectSelect'),
      dataIndex: 'target_object_name',
      key: 'target_object_name',
      width: 140,
      render: (_, record) => <Tag color="success">{getObjectName(record.target_object_id)}</Tag>,
    },
    {
      title: t('common.description'),
      dataIndex: 'description',
      key: 'description',
      ellipsis: true,
      render: (_, record) => {
        const description = getTemplateRelationDisplay(
          record,
          english,
          getObjectName(record.source_object_id),
          getObjectName(record.target_object_id),
        ).description;
        return description || <span className="template-scenarios-muted">{t('templateScenarios.noDescription')}</span>;
      },
    },
    {
      title: t('templateScenarios.relationType'),
      dataIndex: 'relation_type',
      key: 'relation_type',
      width: 110,
      render: (text) => <Tag>{relationTypeOptions.find((option) => option.value === text)?.label || text}</Tag>,
    },
    {
      title: t('common.actions'),
      key: 'action',
      width: 170,
      render: (_, record) => (
        <span className="template-scenarios-table-actions">
          <a className="yx-table-action" onClick={() => relationModalRef.current?.openEdit(record)}>
            <EditOutlined /> {t('common.edit')}
          </a>
          <a className="yx-table-action template-scenarios-danger-link" onClick={() => openDeleteRelation(record)}>
            <DeleteOutlined /> {t('common.delete')}
          </a>
        </span>
      ),
    },
  ];

  const constraintColumns: ColumnsType<TemplateConstraint> = [
    { title: t('templateScenarios.constraintName'), dataIndex: 'name', key: 'name', width: 160 },
    {
      title: t('templateScenarios.constraintTargetType'),
      dataIndex: 'target_type',
      key: 'target_type',
      width: 120,
      render: (val: string) => {
        const label = t(`compile.constraintTargetType.${val}`);
        return <Tag>{label}</Tag>;
      },
    },
    {
      title: t('templateScenarios.constraintTargetObject'),
      dataIndex: 'target_label',
      key: 'target_label',
      width: 160,
    },
    {
      title: t('templateScenarios.constraintType'),
      dataIndex: 'constraint_type',
      key: 'constraint_type',
      width: 100,
      render: (val: string) => {
        const label = t(`compile.constraintType.${val}`);
        return <Tag color="blue">{label}</Tag>;
      },
    },
    {
      title: t('templateScenarios.constraintExpression'),
      dataIndex: 'expression',
      key: 'expression',
      width: 160,
      render: (text) => (text ? <code style={{ fontSize: 12 }}>{text}</code> : '-'),
    },
    {
      title: t('templateScenarios.suggestion'),
      dataIndex: 'suggestion',
      key: 'suggestion',
      ellipsis: true,
      render: (text) => text || '-',
    },
    {
      title: t('common.actions'),
      key: 'action',
      width: 140,
      render: (_, record) => (
        <span className="template-scenarios-table-actions">
          <a className="yx-table-action" onClick={() => constraintModalRef.current?.openEdit(record)}>
            <EditOutlined /> {t('common.edit')}
          </a>
          <a className="yx-table-action template-scenarios-danger-link" onClick={() => openDeleteConstraint(record)}>
            <DeleteOutlined /> {t('common.delete')}
          </a>
        </span>
      ),
    },
  ];

  // ── Modal open handlers ──

  function openCreateScene() {
    if (domainOptions.length === 0) {
      message.warning(t('templateScenarios.createDomainFirstWarning'));
      return;
    }
    sceneModalRef.current?.openCreate(domainFilter || domainOptions[0]?.value || '');
  }

  function openEditScene(scene: TemplateScenario, event?: MouseEvent) {
    event?.stopPropagation();
    sceneModalRef.current?.openEdit(scene);
  }

  function openDeleteScene(scene: TemplateScenario, event?: MouseEvent) {
    event?.stopPropagation();
    deleteModalRef.current?.open({ type: 'scene', item: scene });
  }

  function openCreateObject() {
    if (!selectedSceneId) return;
    objectModalRef.current?.openCreate();
  }

  function openDeleteObject(item: TemplateObject) {
    deleteModalRef.current?.open({ type: 'object', item });
  }

  function openCreateRelation() {
    if (!selectedSceneId) return;
    if (objects.length === 0) {
      message.warning(t('templateScenarios.createObjectFirstWarning'));
      return;
    }
    relationModalRef.current?.openCreate();
  }

  function openDeleteRelation(item: TemplateRelation) {
    deleteModalRef.current?.open({ type: 'relation', item });
  }

  function openDeleteConstraint(item: TemplateConstraint) {
    deleteModalRef.current?.open({ type: 'constraint', item });
  }

  // ── Domain filter ──

  async function handleDomainFilterChange(value?: string) {
    const nextFilter = value ?? '';
    setDomainFilter(nextFilter);
    setActiveTab('objects');
    await refreshScenarios(nextFilter);
  }

  async function refreshCurrentView() {
    const nextSelected = await refreshScenarios(domainFilter, selectedSceneId);
    if (nextSelected) {
      await refreshTemplateDetails(nextSelected);
    }
  }

  // ── YAML Import/Export handlers ──

  const handleExportYaml = useCallback(async () => {
    if (!selectedSceneId) return
    try {
      const result = await exportScenarioOntologyYaml(selectedSceneId)
      const blob = new Blob([result.yaml_text], { type: 'application/x-yaml' })
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = result.filename
      document.body.appendChild(a)
      a.click()
      document.body.removeChild(a)
      URL.revokeObjectURL(url)
      message.success(t('templateScenarios.exportSuccess'))
    } catch (error) {
      message.error(getErrorMessage(error, t('templateScenarios.exportFailed')))
    }
  }, [selectedSceneId, t])

  const handleImportClick = useCallback(() => {
    fileInputRef.current?.click()
  }, [])

  const handleFileSelected = useCallback(
    async (event: React.ChangeEvent<HTMLInputElement>) => {
      const file = event.target.files?.[0]
      if (!file || !selectedSceneId) {
        if (event.target) event.target.value = ''
        return
      }
      selectedFileRef.current = file
      setImportLoading(true)
      try {
        const result = await importScenarioOntologyYaml(selectedSceneId, file, true)
        setImportResult(result)
        setImportModalVisible(true)
      } catch (error) {
        message.error(getErrorMessage(error, t('templateScenarios.importFailed')))
      } finally {
        setImportLoading(false)
        if (event.target) event.target.value = ''
      }
    },
    [selectedSceneId, t],
  )

  const handleConfirmImport = useCallback(async () => {
    if (!selectedSceneId || !selectedFileRef.current) return
    setImportLoading(true)
    try {
      await importScenarioOntologyYaml(selectedSceneId, selectedFileRef.current, false)
      setImportModalVisible(false)
      setImportResult(null)
      selectedFileRef.current = null
      message.success(t('templateScenarios.importSuccess'))
      await refreshTemplateDetails(selectedSceneId)
    } catch (error) {
      message.error(getErrorMessage(error, t('templateScenarios.importFailed')))
    } finally {
      setImportLoading(false)
    }
  }, [selectedSceneId, refreshTemplateDetails, t])

  const handleCloseImportModal = useCallback(() => {
    setImportModalVisible(false)
    setImportResult(null)
    selectedFileRef.current = null
  }, [])

  return (
    <div className="template-scenarios-page">
      <div className="yx-page-title">
        <Button
          type="link"
          icon={<ArrowLeftOutlined />}
          onClick={() => navigate('/template-domains')}
          style={{ paddingLeft: 0, marginBottom: 4 }}
        >
          {t('templateScenarios.backToDomains')}
        </Button>
        <h1>{t('templateScenarios.pageTitle')}</h1>
        <p className="yx-page-subtitle">{t('templateScenarios.pageSubtitle')}</p>
      </div>

      <div className="yx-card template-scenarios-card">
        <div className="template-scenarios-split">
          <aside className="template-scenarios-left">
            <div className="template-scenarios-left-title">
              <h2>{t('templateScenarios.domainScenes')}</h2>
              <span>{t('templateScenarios.sceneCount', { count: scenes.length })}</span>
            </div>

            <Select
              className="template-scenarios-domain-filter"
              placeholder={t('templateScenarios.allDomains')}
              value={domainFilter || undefined}
              onChange={handleDomainFilterChange}
              allowClear
              loading={loadingDomains}
              options={domainOptions}
            />

            {loadingScenes ? (
              <div style={{ textAlign: 'center', padding: '40px 20px', color: '#94a3b8' }}>
                {t('templateScenarios.loadingScenes')}
              </div>
            ) : scenes.length === 0 ? (
              <div style={{ textAlign: 'center', padding: '40px 20px', color: '#94a3b8' }}>
                {t('templateScenarios.noMatchScene')}
              </div>
            ) : (
              <Menu
                className="template-scenarios-list"
                mode="inline"
                selectedKeys={selectedSceneId ? [selectedSceneId] : []}
                onSelect={({ key }) => {
                  setSelectedSceneId(key);
                  setActiveTab('objects');
                }}
                items={scenes.map((scene) => ({
                  key: scene.id,
                  label: (
                    <div
                      style={{
                        display: 'flex',
                        justifyContent: 'space-between',
                        alignItems: 'flex-start',
                        width: '100%',
                      }}
                    >
                      <div className="template-scenarios-item-main">
                        <div className="template-scenarios-item-name">
                          {getTemplateScenarioDisplay(scene, english, t).name}
                        </div>
                        <div className="template-scenarios-item-meta">{formatTime(scene.created_at)}</div>
                        <Tag className="template-scenarios-domain-tag">{getDomainName(scene.domain_id)}</Tag>
                      </div>
                      <div className="template-scenarios-item-actions">
                        <Button
                          type="text"
                          size="small"
                          icon={<EditOutlined />}
                          aria-label={t('templateScenarios.ariaLabelEditScene')}
                          onClick={(event) => {
                            event.stopPropagation();
                            openEditScene(scene, event);
                          }}
                        />
                        <Button
                          type="text"
                          danger
                          size="small"
                          icon={<DeleteOutlined />}
                          aria-label={t('templateScenarios.ariaLabelDeleteScene')}
                          onClick={(event) => {
                            event.stopPropagation();
                            openDeleteScene(scene, event);
                          }}
                        />
                      </div>
                    </div>
                  ),
                }))}
              />
            )}

            <Button
              className="template-scenarios-create-scene"
              type="primary"
              icon={<PlusOutlined />}
              block
              onClick={openCreateScene}
            >
              {t('templateScenarios.createScene')}
            </Button>
          </aside>

          <section className="template-scenarios-right">
            {!selectedScene ? (
              <>
                <div className="template-scenarios-right-header">
                  <h2>{t('templateScenarios.selectSceneFromLeft')}</h2>
                  <div style={{ display: 'flex', gap: 8 }}>
                    <Button icon={<ExportOutlined />} disabled>
                      {t('templateScenarios.exportYaml')}
                    </Button>
                    <Button icon={<ImportOutlined />} disabled>
                      {t('templateScenarios.importYaml')}
                    </Button>
                    <Button icon={<ReloadOutlined />} onClick={refreshCurrentView} loading={loadingScenes}>
                      {t('templateScenarios.refresh')}
                    </Button>
                  </div>
                </div>
                <div className="yx-empty-state template-scenarios-empty">{t('templateScenarios.selectSceneHint')}</div>
              </>
            ) : (
              <>
                <div className="template-scenarios-right-header">
                  <div>
                    <h2>{getTemplateScenarioDisplay(selectedScene, english, t).name}</h2>
                    <p className="yx-page-subtitle">
                      {getTemplateScenarioDisplay(selectedScene, english, t).description ||
                        t('templateScenarios.noSceneDescription')}
                    </p>
                  </div>
                  <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', justifyContent: 'flex-end' }}>
                    <Button icon={<ExportOutlined />} onClick={handleExportYaml}>
                      {t('templateScenarios.exportYaml')}
                    </Button>
                    <Button icon={<ImportOutlined />} onClick={handleImportClick} loading={importLoading}>
                      {t('templateScenarios.importYaml')}
                    </Button>
                    <Button
                      icon={<ReloadOutlined />}
                      onClick={refreshCurrentView}
                      loading={loadingScenes || loadingDetails}
                    >
                      {t('templateScenarios.refresh')}
                    </Button>
                  </div>
                </div>

                <div className="yx-tabs template-scenarios-tabs">
                  <Button
                    type={activeTab === 'objects' ? 'primary' : 'default'}
                    ghost={activeTab !== 'objects'}
                    icon={<ProfileOutlined />}
                    onClick={() => setActiveTab('objects')}
                    style={{ borderRadius: 8 }}
                  >
                    {t('templateScenarios.objectTemplate')}
                  </Button>
                  <Button
                    type={activeTab === 'relations' ? 'primary' : 'default'}
                    ghost={activeTab !== 'relations'}
                    icon={<ShareAltOutlined />}
                    onClick={() => setActiveTab('relations')}
                    style={{ borderRadius: 8 }}
                  >
                    {t('templateScenarios.relationTemplate')}
                  </Button>
                  <Button
                    type={activeTab === 'constraints' ? 'primary' : 'default'}
                    ghost={activeTab !== 'constraints'}
                    icon={<SafetyOutlined />}
                    onClick={() => setActiveTab('constraints')}
                    style={{ borderRadius: 8 }}
                  >
                    {t('templateScenarios.constraintTemplate')}
                  </Button>
                </div>

                <p className="template-scenarios-tab-note" style={{ color: '#94a3b8', fontSize: 12, marginTop: 8 }}>
                  {activeTab === 'constraints'
                    ? t('templateScenarios.constraintNote')
                    : t('templateScenarios.relationNote')}
                </p>

                {activeTab === 'objects' && (
                  <div>
                    <div className="template-scenarios-tab-toolbar">
                      <Button type="primary" icon={<PlusOutlined />} onClick={openCreateObject}>
                        {t('templateScenarios.createObject')}
                      </Button>
                    </div>
                    <Table
                      className="yx-data-table"
                      dataSource={objects}
                      columns={objectColumns}
                      rowKey="id"
                      loading={loadingDetails}
                      pagination={false}
                      locale={{ emptyText: t('templateScenarios.noObjectTemplates') }}
                    />
                  </div>
                )}
                {activeTab === 'relations' && (
                  <div>
                    <div className="template-scenarios-tab-toolbar">
                      <Button type="primary" icon={<PlusOutlined />} onClick={openCreateRelation}>
                        {t('templateScenarios.createRelation')}
                      </Button>
                    </div>
                    <Table
                      className="yx-data-table"
                      dataSource={relations}
                      columns={relationColumns}
                      rowKey="id"
                      loading={loadingDetails}
                      pagination={false}
                      locale={{ emptyText: t('templateScenarios.noRelationTemplates') }}
                    />
                  </div>
                )}
                {activeTab === 'constraints' && (
                  <div>
                    <div className="template-scenarios-tab-toolbar">
                      <Button
                        type="primary"
                        icon={<PlusOutlined />}
                        onClick={() => constraintModalRef.current?.openCreate()}
                      >
                        {t('templateScenarios.createConstraint')}
                      </Button>
                    </div>
                    <Table
                      dataSource={constraints}
                      columns={constraintColumns}
                      rowKey="id"
                      loading={loadingDetails}
                      pagination={false}
                      locale={{ emptyText: t('templateScenarios.noConstraintTemplates') }}
                    />
                  </div>
                )}
              </>
            )}
          </section>
        </div>
      </div>

      <SceneModal
        ref={sceneModalRef}
        domainOptions={domainOptions}
        loadingDomains={loadingDomains}
        onSaved={handleSceneSaved}
      />
      <ObjectModal
        ref={objectModalRef}
        selectedSceneId={selectedSceneId}
        attributeTypeOptions={attributeTypeOptions}
        onSaved={handleDetailItemSaved}
      />
      <RelationModal
        ref={relationModalRef}
        selectedSceneId={selectedSceneId}
        objectSelectOptions={objectSelectOptions}
        relationTypeOptions={relationTypeOptions}
        onSaved={handleDetailItemSaved}
      />
      <ConstraintModal
        ref={constraintModalRef}
        selectedSceneId={selectedSceneId}
        objects={objects}
        relations={relations}
        getObjectName={getObjectName}
        english={english}
        onSaved={handleDetailItemSaved}
      />
      <DeleteConfirmModal
        ref={deleteModalRef}
        selectedSceneId={selectedSceneId}
        domainFilter={domainFilter}
        english={english}
        onDeleted={handleItemDeleted}
      />

      <input
        type="file"
        accept=".yaml,.yml"
        style={{ display: 'none' }}
        ref={fileInputRef}
        onChange={handleFileSelected}
      />

      <Modal
        title={t('templateScenarios.importSummary')}
        open={importModalVisible}
        onCancel={handleCloseImportModal}
        width={640}
        footer={
          importResult?.errors && importResult.errors.length > 0
            ? null
            : [
                <Button key="cancel" onClick={handleCloseImportModal}>
                  {t('common.cancel')}
                </Button>,
                <Button key="confirm" type="primary" onClick={handleConfirmImport} loading={importLoading}>
                  {t('templateScenarios.confirmImport')}
                </Button>,
              ]
        }
      >
        {importResult && (
          <>
            {importResult.dry_run && (
              <Alert
                message={t('templateScenarios.dryRunNotice')}
                type="info"
                showIcon
                style={{ marginBottom: 16 }}
              />
            )}
            <Table
              dataSource={[
                { key: 'entities', category: t('templateScenarios.entities'), ...importResult.summary.entities },
                { key: 'attributes', category: t('templateScenarios.attributes'), ...importResult.summary.attributes },
                { key: 'relations', category: t('templateScenarios.relations'), ...importResult.summary.relations },
                { key: 'constraints', category: t('templateScenarios.constraints'), ...importResult.summary.constraints },
              ]}
              columns={[
                { title: t('templateScenarios.category'), dataIndex: 'category', key: 'category' },
                { title: t('templateScenarios.create'), dataIndex: 'create', key: 'create' },
                { title: t('templateScenarios.update'), dataIndex: 'update', key: 'update' },
                { title: t('templateScenarios.skip'), dataIndex: 'skip', key: 'skip' },
              ]}
              rowKey="key"
              pagination={false}
              size="small"
              style={{ marginBottom: 16 }}
            />
            {importResult.warnings.length > 0 && (
              <div style={{ marginBottom: importResult.errors.length > 0 ? 12 : 0 }}>
                {importResult.warnings.map((w, i) => (
                  <Alert key={`warn-${i}`} message={w} type="warning" showIcon style={{ marginBottom: 8 }} />
                ))}
              </div>
            )}
            {importResult.errors.length > 0 && (
              <div>
                {importResult.errors.map((e, i) => (
                  <Alert key={`err-${i}`} message={e} type="error" showIcon style={{ marginBottom: 8 }} />
                ))}
              </div>
            )}
          </>
        )}
      </Modal>
    </div>
  );
}
