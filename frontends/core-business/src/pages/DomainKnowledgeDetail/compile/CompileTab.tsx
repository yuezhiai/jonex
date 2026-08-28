import React, { useCallback, useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Alert, Button, Modal, Space, Spin, Tabs, Typography, message } from 'antd';
import type {
  OntologyObjectDef,
  OntologyRelationDef,
  OntologyConstraint,
  CompileStep,
  EngineSetting,
  SaveOntologyObjectPayload,
  SaveOntologyRelationPayload,
  SaveOntologyConstraintPayload,
  SaveCompileStepPayload,
  CompiledSchema,
  YamlImportResult,
} from '@/types/domainKnowledge';
import { ontologyStatusLabelKey } from '@/types/domainKnowledge';
import {
  getOntologyObjects,
  createOntologyObject,
  updateOntologyObject,
  deleteOntologyObject,
  getOntologyRelations,
  createSchemaRelationType,
  updateSchemaRelationType,
  deleteSchemaRelationType,
  getOntologyConstraints,
  createOntologyConstraint,
  updateOntologyConstraint,
  deleteOntologyConstraint,
  getConstraintTargetOptions,
  type ConstraintTargetOptions,
  getCompileSteps,
  createCompileStep,
  updateCompileStep,
  deleteCompileStep,
  getEngineSetting,
  saveEngineSetting,
  fetchCompiledSchemaForEditing,
  exportCompiledSchemaYaml,
  importCompiledSchemaYaml,
} from '@/api/domainKnowledge';
import OntologyObjectSection from './OntologyObjectSection';
import OntologyRelationSection from './OntologyRelationSection';
import OntologyConstraintSection from './OntologyConstraintSection';
import CompileStepSection from './CompileStepSection';
import EngineBasicSection from './EngineBasicSection';
import ObjectFormModal from './ObjectFormModal';
import RelationFormModal from './RelationFormModal';
import ConstraintFormModal from './ConstraintFormModal';
import StepFormModal from './StepFormModal';
import PromptViewModal from './PromptViewModal';
import TemplateImportModal from './TemplateImportModal';

interface Props {
  kbId: string;  canWrite?: boolean;
}

const EMPTY_TARGET_OPTIONS: ConstraintTargetOptions = {
  entity: [],
  attribute: [],
  relation: [],
};

export default function CompileTab({ kbId, canWrite = false }: Props) {
  const { t } = useTranslation();

  function buildObjectPrompt(o: OntologyObjectDef): string {
    let prompt = `${t('compile.prompt.objectIntro', { name: o.name })}\n`;
    prompt += `${t('compile.prompt.objectNameLabel', { name: o.name })}\n`;
    prompt += `${t('compile.prompt.objectDescLabel', { description: o.description || t('compile.prompt.none') })}\n`;
    prompt += `${t('compile.prompt.attrDefLabel')}\n`;
    o.attributes.forEach((a, i) => {
      const pkTag = a.isPrimaryKey ? t('compile.prompt.primaryKeyBracket') : '';
      prompt += t('compile.prompt.attrLine', {
        index: i + 1,
        name: a.name,
        type: a.type,
        primaryKey: pkTag,
      });
    });
    prompt += `\n${t('compile.prompt.moreReqLabel', { requirement: o.requirement || t('compile.prompt.none') })}\n`;
    prompt += `${t('compile.statusLabel')}${t(ontologyStatusLabelKey[o.status])}`;
    return prompt;
  }

  function buildRelationPrompt(r: OntologyRelationDef): string {
    let prompt = `${t('compile.prompt.relationIntro', { name: r.name })}\n`;
    prompt += `${t('compile.prompt.sourceObjectLabel', { name: r.sourceObject })}\n`;
    prompt += `${t('compile.prompt.relationNameLabel', { name: r.name })}\n`;
    prompt += `${t('compile.prompt.targetObjectLabel', { name: r.targetObject })}\n`;
    prompt += `${t('compile.prompt.relationDescLabel', { description: r.description || t('compile.prompt.none') })}\n`;
    prompt += `${t('compile.prompt.relationTypeLabel', { type: r.relationType })}\n`;
    prompt += `${t('compile.statusLabel')}${t(ontologyStatusLabelKey[r.status])}`;
    return prompt;
  }
  const [activeSubTab, setActiveSubTab] = useState('obj');
  const [objects, setObjects] = useState<OntologyObjectDef[]>([]);
  const [relations, setRelations] = useState<OntologyRelationDef[]>([]);
  const [constraints, setConstraints] = useState<OntologyConstraint[]>([]);
  const [targetOptions, setTargetOptions] = useState<ConstraintTargetOptions>(EMPTY_TARGET_OPTIONS);
  const [steps, setSteps] = useState<CompileStep[]>([]);
  const [engine, setEngine] = useState<EngineSetting | null>(null);
  const [loadingCompiledSchema, setLoadingCompiledSchema] = useState(false);

  // compiled schema (for schema_version tracking and YAML import)
  const [compiledSchema, setCompiledSchema] = useState<CompiledSchema | null>(null);
  const [loadingObjects, setLoadingObjects] = useState(false);
  const [loadingRelations, setLoadingRelations] = useState(false);
  const [loadingConstraints, setLoadingConstraints] = useState(false);
  const [loadingSteps, setLoadingSteps] = useState(false);
  const [loadingEngine, setLoadingEngine] = useState(false);

  const [objectModal, setObjectModal] = useState<{
    open: boolean;
    editing: OntologyObjectDef | null;
  }>({ open: false, editing: null });
  const [relationModal, setRelationModal] = useState<{
    open: boolean;
    editing: OntologyRelationDef | null;
  }>({ open: false, editing: null });
  const [constraintModal, setConstraintModal] = useState<{
    open: boolean;
    editing: OntologyConstraint | null;
  }>({ open: false, editing: null });
  const [stepModal, setStepModal] = useState<{
    open: boolean;
    editing: CompileStep | null;
  }>({ open: false, editing: null });
  const [promptModal, setPromptModal] = useState<{
    open: boolean;
    title: string;
    desc: string;
    content: string;
  }>({ open: false, title: '', desc: '', content: '' });
  const [importModal, setImportModal] = useState<{
    open: boolean;
    mode: 'object' | 'relation';
  }>({ open: false, mode: 'object' });
  const [submitting, setSubmitting] = useState(false);

  // YAML import state
  const yamlFileInputRef = useRef<HTMLInputElement>(null);
  const [yamlImportModal, setYamlImportModal] = useState<{
    open: boolean;
    result: YamlImportResult | null;
    loading: boolean;
    error: string | null;
  }>({ open: false, result: null, loading: false, error: null });
  const [yamlExporting, setYamlExporting] = useState(false);
  const [yamlImporting, setYamlImporting] = useState(false);

  const loadObjects = useCallback(() => {
    setLoadingObjects(true);
    getOntologyObjects(kbId)
      .then(setObjects)
      .catch((e) => message.error(e?.message || t('compile.loadObjectsFailed')))
      .finally(() => setLoadingObjects(false));
  }, [kbId]);

  const loadRelations = useCallback(() => {
    setLoadingRelations(true);
    getOntologyRelations(kbId)
      .then(setRelations)
      .catch((e) => message.error(e?.message || t('compile.loadRelationsFailed')))
      .finally(() => setLoadingRelations(false));
  }, [kbId]);

  const loadConstraints = useCallback(() => {
    setLoadingConstraints(true);
    getOntologyConstraints(kbId)
      .then(setConstraints)
      .catch((e) => message.error(e?.message || t('compile.loadConstraintsFailed')))
      .finally(() => setLoadingConstraints(false));
  }, [kbId]);

  const loadTargetOptions = useCallback(() => {
    getConstraintTargetOptions(kbId)
      .then(setTargetOptions)
      .catch(() => setTargetOptions(EMPTY_TARGET_OPTIONS));
  }, [kbId]);

  const loadSteps = useCallback(() => {
    setLoadingSteps(true);
    getCompileSteps(kbId)
      .then(setSteps)
      .catch((e) => message.error(e?.message || t('compile.loadStepsFailed')))
      .finally(() => setLoadingSteps(false));
  }, [kbId]);

  const loadEngine = useCallback(() => {
    setLoadingEngine(true);
    getEngineSetting(kbId)
      .then(setEngine)
      .catch((e) => message.error(e?.message || t('compile.loadEngineFailed')))
      .finally(() => setLoadingEngine(false));
  }, [kbId]);

  const loadCompiledSchema = useCallback(() => {
    setLoadingCompiledSchema(true);
    fetchCompiledSchemaForEditing(kbId)
      .then(setCompiledSchema)
      .catch((e) => message.error(e?.message || 'Failed to load compiled schema'))
      .finally(() => setLoadingCompiledSchema(false));
  }, [kbId]);

  useEffect(() => {
    loadObjects();
    loadRelations();
    loadConstraints();
    loadTargetOptions();
    loadSteps();
    loadEngine();
    loadCompiledSchema();
  }, [loadObjects, loadRelations, loadConstraints, loadTargetOptions, loadSteps, loadEngine, loadCompiledSchema]);

  // 编译 schema 三类共用全量保存，任一保存后同步刷新三类，保证 schema_version 与目标选项一致
  const reloadCompiledSchema = useCallback(() => {
    loadObjects();
    loadRelations();
    loadConstraints();
    loadTargetOptions();
    loadCompiledSchema();
  }, [loadObjects, loadRelations, loadConstraints, loadTargetOptions, loadCompiledSchema]);

  const handleSaveError = (e: any, fallback: string) => {
    const msg = e?.message || fallback;
    message.error(msg);
    // 并发版本冲突（后端 409）后，刷新到最新数据
    if (typeof msg === 'string' && msg.includes('已被更新')) {
      reloadCompiledSchema();
    }
  };

  const submitObject = async (payload: SaveOntologyObjectPayload) => {
    setSubmitting(true);
    try {
      if (objectModal.editing) {
        await updateOntologyObject(kbId, objectModal.editing.id, payload);
        message.success(t('compile.objectUpdated'));
      } else {
        await createOntologyObject(kbId, payload);
        message.success(t('compile.objectCreated'));
      }
      setObjectModal({ open: false, editing: null });
      reloadCompiledSchema();
    } catch (e: any) {
      handleSaveError(e, t('compile.saveObjectFailed'));
    } finally {
      setSubmitting(false);
    }
  };

  const removeObject = (o: OntologyObjectDef) => {
    Modal.confirm({
      title: t('compile.deleteObject'),
      content: t('compile.confirmDeleteObject', { name: o.name }),
      okText: t('compile.confirmDelete'),
      cancelText: t('common.cancel'),
      okButtonProps: { danger: true },
      onOk: async () => {
        try {
          await deleteOntologyObject(kbId, o.id);
          message.success(t('compile.objectDeleted'));
          reloadCompiledSchema();
        } catch (e: any) {
          handleSaveError(e, t('compile.deleteObjectFailed'));
        }
      },
    });
  };

  const submitRelation = async (payload: SaveOntologyRelationPayload) => {
    setSubmitting(true);
    try {
      if (relationModal.editing) {
        await updateSchemaRelationType(kbId, relationModal.editing.id, payload);
        message.success(t('compile.relationUpdated'));
      } else {
        await createSchemaRelationType(kbId, payload);
        message.success(t('compile.relationCreated'));
      }
      setRelationModal({ open: false, editing: null });
      reloadCompiledSchema();
    } catch (e: any) {
      handleSaveError(e, t('compile.saveRelationFailed'));
    } finally {
      setSubmitting(false);
    }
  };

  const removeRelation = (r: OntologyRelationDef) => {
    Modal.confirm({
      title: t('compile.deleteRelation'),
      content: t('compile.confirmDeleteRelation', { name: r.name }),
      okText: t('compile.confirmDelete'),
      cancelText: t('common.cancel'),
      okButtonProps: { danger: true },
      onOk: async () => {
        try {
          await deleteSchemaRelationType(kbId, r.id);
          message.success(t('compile.relationDeleted'));
          reloadCompiledSchema();
        } catch (e: any) {
          handleSaveError(e, t('compile.deleteRelationFailed'));
        }
      },
    });
  };

  const submitConstraint = async (payload: SaveOntologyConstraintPayload) => {
    setSubmitting(true);
    try {
      if (constraintModal.editing) {
        await updateOntologyConstraint(kbId, constraintModal.editing.id, payload);
        message.success(t('compile.constraintUpdated'));
      } else {
        await createOntologyConstraint(kbId, payload);
        message.success(t('compile.constraintCreated'));
      }
      setConstraintModal({ open: false, editing: null });
      loadConstraints();
    } catch (e: any) {
      handleSaveError(e, t('compile.saveConstraintFailed'));
    } finally {
      setSubmitting(false);
    }
  };

  const removeConstraint = (c: OntologyConstraint) => {
    Modal.confirm({
      title: t('compile.deleteConstraint'),
      content: t('compile.confirmDeleteConstraint', { name: c.name }),
      okText: t('compile.confirmDelete'),
      cancelText: t('common.cancel'),
      okButtonProps: { danger: true },
      onOk: async () => {
        try {
          await deleteOntologyConstraint(kbId, c.id);
          message.success(t('compile.constraintDeleted'));
          loadConstraints();
        } catch (e: any) {
          handleSaveError(e, t('compile.deleteConstraintFailed'));
        }
      },
    });
  };

  const submitStep = async (payload: SaveCompileStepPayload) => {
    setSubmitting(true);
    try {
      if (stepModal.editing) {
        await updateCompileStep(kbId, stepModal.editing.id, payload);
        message.success(t('compile.stepUpdated'));
      } else {
        await createCompileStep(kbId, payload);
        message.success(t('compile.stepCreated'));
      }
      setStepModal({ open: false, editing: null });
      loadSteps();
    } catch (e: any) {
      message.error(e?.message || t('compile.saveStepFailed'));
    } finally {
      setSubmitting(false);
    }
  };

  const removeStep = (s: CompileStep) => {
    Modal.confirm({
      title: t('compile.deleteStep'),
      content: t('compile.confirmDeleteStep', { name: s.name }),
      okText: t('compile.confirmDelete'),
      cancelText: t('common.cancel'),
      okButtonProps: { danger: true },
      onOk: async () => {
        await deleteCompileStep(kbId, s.id);
        message.success(t('compile.stepDeleted'));
        loadSteps();
      },
    });
  };

  const handleSaveEngine = async (model: string) => {
    try {
      const next = await saveEngineSetting(kbId, model);
      setEngine(next);
      message.success(t('compile.engineSaved'));
    } catch (e: any) {
      message.error(e?.message || t('compile.saveEngineFailed'));
    }
  };

  // ─── YAML 导出/导入 ────────────────────────────────

  const yamlImportFileRef = useRef<File | null>(null);

  const handleExportYaml = async () => {
    setYamlExporting(true);
    try {
      const result = await exportCompiledSchemaYaml(kbId);
      const blob = new Blob([result.yaml_text], { type: 'application/x-yaml' });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = result.filename || `compiled-schema-${kbId}.yaml`;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(url);
    } catch (e: any) {
      message.error(e?.message || t('compile.yamlImportFailed'));
    } finally {
      setYamlExporting(false);
    }
  };

  const handleImportYamlClick = () => {
    yamlFileInputRef.current?.click();
  };

  const doImport = async (dryRun: boolean) => {
    const file = yamlImportFileRef.current;
    const schemaVersion = compiledSchema?.schema_version;
    if (!file || schemaVersion == null) {
      message.error(t('compile.yamlImportSelectFile'));
      return;
    }

    if (!dryRun) {
      setYamlImporting(true);
    }
    try {
      const result = await importCompiledSchemaYaml(kbId, schemaVersion, file, dryRun);
      if (dryRun) {
        setYamlImportModal({ open: true, result, loading: false, error: null });
      } else {
        message.success(t('compile.yamlImportSuccess'));
        setYamlImportModal({ open: false, result: null, loading: false, error: null });
        yamlImportFileRef.current = null;
        reloadCompiledSchema();
      }
    } catch (e: any) {
      const errMsg = e?.message || t('compile.yamlImportFailed');
      if (dryRun) {
        setYamlImportModal((prev) => ({ ...prev, loading: false, error: errMsg }));
      } else {
        // Check for 409 conflict
        if (errMsg.includes('409') || errMsg.includes('conflict') || errMsg.includes('已被更新')) {
          message.error(t('compile.yamlImportConflict'));
        } else {
          message.error(errMsg);
        }
      }
    } finally {
      if (!dryRun) {
        setYamlImporting(false);
      }
    }
  };

  const handleYamlFileSelected = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;

    yamlImportFileRef.current = file;
    setYamlImportModal({ open: true, result: null, loading: true, error: null });
    await doImport(true);
    // reset file input so the same file can be re-selected
    e.target.value = '';
  };

  const handleYamlImportConfirm = async () => {
    setYamlImportModal((prev) => ({ ...prev, loading: true, error: null }));
    await doImport(false);
  };

  const tabItems = [
    {
      key: 'obj',
      label: t('compile.tabObjectDef'),
      children: (
        <OntologyObjectSection
                canWrite={canWrite}
          data={objects}
          loading={loadingObjects}
          onCreate={() => setObjectModal({ open: true, editing: null })}
          onImport={() => setImportModal({ open: true, mode: 'object' })}
          onEdit={(o) => setObjectModal({ open: true, editing: o })}
          onDelete={removeObject}
          onPrompt={(o) =>
            setPromptModal({
              open: true,
              title: t('compile.promptModal.objectTitle', { name: o.name }),
              desc: t('compile.promptModal.objectDesc', { name: o.name }),
              content: buildObjectPrompt(o),
            })
          }
        />
      ),
    },
    {
      key: 'rel',
      label: t('compile.tabRelationDef'),
      children: (
        <OntologyRelationSection
                canWrite={canWrite}
          data={relations}
          loading={loadingRelations}
          onCreate={() => setRelationModal({ open: true, editing: null })}
          onImport={() => setImportModal({ open: true, mode: 'relation' })}
          onEdit={(r) => setRelationModal({ open: true, editing: r })}
          onDelete={removeRelation}
          onPrompt={(r) =>
            setPromptModal({
              open: true,
              title: t('compile.promptModal.relationTitle', { name: r.name }),
              desc: t('compile.promptModal.relationDesc', { name: r.name }),
              content: buildRelationPrompt(r),
            })
          }
        />
      ),
    },
    {
      key: 'constraint',
      label: t('compile.tabConstraintDef'),
      children: (
        <OntologyConstraintSection
                canWrite={canWrite}
          data={constraints}
          loading={loadingConstraints}
          onCreate={() => setConstraintModal({ open: true, editing: null })}
          onEdit={(c) => setConstraintModal({ open: true, editing: c })}
          onDelete={removeConstraint}
        />
      ),
    },
  ];

  return (
    <div>
      <Tabs
        activeKey={activeSubTab}
        onChange={setActiveSubTab}
        items={tabItems}
        tabBarExtraContent={{
          right: (
            <div style={{ display: 'flex', gap: 8 }}>
              <Button icon={<span>&#x1F4E4;</span>} loading={yamlExporting} onClick={handleExportYaml}>
                {t('compile.exportYaml')}
              </Button>
              <Button
                icon={<span>&#x1F4E5;</span>}
                disabled={!canWrite}
                title={canWrite ? undefined : t('domainSpace.noManagePermission')}
                onClick={handleImportYamlClick}
              >
                {t('compile.importYaml')}
              </Button>
              <input
                ref={yamlFileInputRef}
                type="file"
                accept=".yaml,.yml"
                style={{ display: 'none' }}
                onChange={handleYamlFileSelected}
              />
            </div>
          ),
        }}
      />

      {/* 编译步骤设置、编译设置暂时隐藏（保留代码以便后续再开） */}
      {false && (
        <>
          <CompileStepSection
            data={steps}
            loading={loadingSteps}
            onCreate={() => setStepModal({ open: true, editing: null })}
            onEdit={(s) => setStepModal({ open: true, editing: s })}
            onDelete={removeStep}
          />
          <EngineBasicSection engine={engine} loading={loadingEngine} onSave={handleSaveEngine} />
        </>
      )}

      <ObjectFormModal
        open={objectModal.open}
        editing={objectModal.editing}
        submitting={submitting}
        onCancel={() => setObjectModal({ open: false, editing: null })}
        onSubmit={submitObject}
      />
      <RelationFormModal
        open={relationModal.open}
        editing={relationModal.editing}
        objectNames={objects.map((o) => o.name)}
        submitting={submitting}
        onCancel={() => setRelationModal({ open: false, editing: null })}
        onSubmit={submitRelation}
      />
      <ConstraintFormModal
        open={constraintModal.open}
        editing={constraintModal.editing}
        targetOptions={targetOptions}
        existingNames={constraints.map((c) => c.name)}
        submitting={submitting}
        onCancel={() => setConstraintModal({ open: false, editing: null })}
        onSubmit={submitConstraint}
      />
      <StepFormModal
        open={stepModal.open}
        editing={stepModal.editing}
        submitting={submitting}
        onCancel={() => setStepModal({ open: false, editing: null })}
        onSubmit={submitStep}
      />
      <PromptViewModal
        open={promptModal.open}
        title={promptModal.title}
        desc={promptModal.desc}
        content={promptModal.content}
        onClose={() => setPromptModal((s) => ({ ...s, open: false }))}
      />
      <TemplateImportModal
        open={importModal.open}
        mode={importModal.mode}
        kbId={kbId}
        onClose={() => setImportModal((s) => ({ ...s, open: false }))}
        onImported={() => {
          if (importModal.mode === 'object') loadObjects();
          else loadRelations();
          loadTargetOptions();
        }}
      />

      {/* YAML 导入预览/确认弹窗 */}
      <Modal
        title={t('compile.yamlImportTitle')}
        open={yamlImportModal.open}
        onCancel={() => {
          setYamlImportModal({ open: false, result: null, loading: false, error: null });
          yamlImportFileRef.current = null;
        }}
        footer={
          <Space>
            <Button
              onClick={() => {
                setYamlImportModal({ open: false, result: null, loading: false, error: null });
                yamlImportFileRef.current = null;
              }}
            >
              {t('compile.yamlImportCancel')}
            </Button>
            <Button
              type="primary"
              loading={yamlImportModal.loading || yamlImporting}
              disabled={
                yamlImportModal.loading ||
                !!yamlImportModal.error ||
                (!!yamlImportModal.result?.errors && yamlImportModal.result.errors.length > 0)
              }
              onClick={handleYamlImportConfirm}
            >
              {t('compile.yamlImportConfirm')}
            </Button>
          </Space>
        }
      >
        {yamlImportModal.loading ? (
          <div style={{ textAlign: 'center', padding: 40 }}>
            <Spin />
            <div style={{ marginTop: 12 }}>
              <Typography.Text type="secondary">
                {yamlImportModal.result ? t('compile.yamlImportConfirm') + '...' : t('compile.importYaml') + '...'}
              </Typography.Text>
            </div>
          </div>
        ) : yamlImportModal.error ? (
          <Alert type="error" message={t('compile.yamlImportFailed')} description={yamlImportModal.error} showIcon />
        ) : yamlImportModal.result ? (
          <div>
            {yamlImportModal.result.errors && yamlImportModal.result.errors.length > 0 && (
              <Alert
                type="error"
                message={t('compile.yamlImportErrorsTitle')}
                description={
                  <ul style={{ margin: 0, paddingLeft: 20 }}>
                    {yamlImportModal.result.errors.map((err, idx) => (
                      <li key={idx}>{err}</li>
                    ))}
                  </ul>
                }
                showIcon
                style={{ marginBottom: 16 }}
              />
            )}
            <Typography.Title level={5}>{t('compile.yamlImportSummary')}</Typography.Title>
            <div style={{ paddingLeft: 8 }}>
              {yamlImportModal.result.counts?.entities != null && (
                <div>{t('compile.yamlImportCountEntities', { count: yamlImportModal.result.counts.entities })}</div>
              )}
              {yamlImportModal.result.counts?.attributes != null && (
                <div>{t('compile.yamlImportCountAttributes', { count: yamlImportModal.result.counts.attributes })}</div>
              )}
              {yamlImportModal.result.counts?.relations != null && (
                <div>{t('compile.yamlImportCountRelations', { count: yamlImportModal.result.counts.relations })}</div>
              )}
              {yamlImportModal.result.counts?.constraints != null && (
                <div>{t('compile.yamlImportCountConstraints', { count: yamlImportModal.result.counts.constraints })}</div>
              )}
            </div>
          </div>
        ) : null}
      </Modal>
    </div>
  );
}
