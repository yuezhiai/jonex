/**
 * [jonex] LLM-Wiki Schema 编译设置 Tab（kb_type=openkb 的编译设置分流目标，
 * 方案 llmwiki-schema-settings-execution-plan §10）。
 *
 * 页面结构（三个 tab，白底卡片样式对齐本体 CompileTab）：
 * - 实体类型（VocabSection kind=entity，受控词表）
 * - 概念类型（VocabSection kind=concept，分类引导无硬校验）
 * - 自定义内容（多行 markdown，原样拼到 AGENTS.md 末尾）
 * - 操作：保存（CAS）、同步到引擎、导入/导出 YAML、预览、全部重新编译
 */
import { useCallback, useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Alert, Button, Card, Col, Input, Modal, Row, Space, Spin, Tabs, Typography, Upload, message } from 'antd';
import {
  EyeOutlined,
  ReloadOutlined,
  SaveOutlined,
  SyncOutlined,
} from '@ant-design/icons';
import {
  applyLlmWikiSchema,
  exportLlmWikiSchemaYaml,
  getLlmWikiSchema,
  importLlmWikiSchemaYaml,
  recompileOutdatedDocuments,
  saveLlmWikiSchema,
} from '@/api/llmWikiSchema';
import type {
  LlmWikiConceptTypeItem,
  LlmWikiEntityTypeItem,
  LlmWikiSchema,
} from '@/types/domainKnowledge';
import VocabSection from './VocabSection';
import SchemaPreviewDrawer from './SchemaPreviewDrawer';

interface Props {
  kbId: string;
  /** KB 写权限（权限矩阵） */
  canWrite?: boolean;
}

// [jonex] 白底卡片样式（对齐参考页 compile/OntologyObjectSection 的 cardStyle）
const sectionCardStyle: React.CSSProperties = {
  background: '#fff',
  borderRadius: 14,
  border: '1px solid #eef2f6',
  padding: 24,
  boxShadow: '0 1px 4px rgba(0,0,0,0.04)',
};

export default function LlmWikiSchemaTab({ kbId, canWrite = false }: Props) {
  const { t } = useTranslation();

  const [schema, setSchema] = useState<LlmWikiSchema | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [syncing, setSyncing] = useState(false);
  const [recompiling, setRecompiling] = useState(false);
  const [previewOpen, setPreviewOpen] = useState(false);
  const [importOpen, setImportOpen] = useState(false);
  const [importText, setImportText] = useState('');
  const [dirty, setDirty] = useState(false);

  // 编辑态（与 schema 分开，保存时组装 payload）
  const [entityTypes, setEntityTypes] = useState<LlmWikiEntityTypeItem[]>([]);
  const [conceptTypes, setConceptTypes] = useState<LlmWikiConceptTypeItem[]>([]);
  const [agentsMdExtra, setAgentsMdExtra] = useState('');

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const s = await getLlmWikiSchema(kbId);
      setSchema(s);
      setEntityTypes(s.entity_types || []);
      setConceptTypes(s.concept_types || []);
      setAgentsMdExtra(s.agents_md_extra || '');
      setDirty(false);
    } catch (err: any) {
      message.error(err?.message || t('common.loadFailed'));
    } finally {
      setLoading(false);
    }
  }, [kbId, t]);

  useEffect(() => {
    load();
  }, [load]);

  const markDirty = () => setDirty(true);

  const handleSave = async () => {
    if (!schema) return;
    // 前端拦截：code 非法 / 缺 other 不发请求（后端也会拒，双保险）
    const CODE_RE = /^[a-z0-9 _-]+$/;
    if (entityTypes.some((e) => !CODE_RE.test(e.code.trim()))) {
      message.error(t('llmWikiSchema.codeInvalid'));
      return;
    }
    if (!entityTypes.some((e) => e.code.trim() === 'other')) {
      message.error(t('llmWikiSchema.otherRequired'));
      return;
    }
    // 前端拦截：code 重复（entity 内部 / concept 内部各自唯一，空 code 跳过交由后端 DTO 拦截）
    const findDup = (items: { code: string }[]) => {
      const seen = new Set<string>();
      for (const it of items) {
        const c = it.code.trim();
        if (!c) continue;
        if (seen.has(c)) return c;
        seen.add(c);
      }
      return null;
    };
    const dupCode = findDup(entityTypes) ?? findDup(conceptTypes);
    if (dupCode) {
      message.error(t('llmWikiSchema.codeDuplicate', { code: dupCode }));
      return;
    }
    setSaving(true);
    try {
      const saved = await saveLlmWikiSchema({
        knowledge_base_id: kbId,
        expected_schema_version: schema.schema_version,
        schema_name: schema.schema_name,
        language: schema.language,
        model: schema.model,
        entity_types: entityTypes,
        concept_types: conceptTypes,
        agents_md_extra: agentsMdExtra,
      });
      setSchema(saved);
      setDirty(false);
      if (saved.sync_status === 'apply_failed') {
        message.warning(t('llmWikiSchema.applyFailedHint'));
      } else {
        message.success(t('common.saveSuccess'));
      }
      if (saved.affected_documents) {
        message.info(
          t('llmWikiSchema.affectedDocuments', { count: saved.affected_documents }),
        );
      }
    } catch (err: any) {
      message.error(err?.message || t('common.saveFailed'));
    } finally {
      setSaving(false);
    }
  };

  const handleSync = async () => {
    setSyncing(true);
    try {
      await applyLlmWikiSchema(kbId);
      message.success(t('llmWikiSchema.synced'));
      await load();
    } catch (err: any) {
      message.error(err?.message || t('common.loadFailed'));
    } finally {
      setSyncing(false);
    }
  };

  const handleRecompileAll = async () => {
    if (!schema) return;
    setRecompiling(true);
    try {
      const r = await recompileOutdatedDocuments(kbId);
      message.success(
        t('llmWikiSchema.recompileSubmitted', {
          matched: r.matched,
          submitted: r.submitted,
        }),
      );
    } catch (err: any) {
      message.error(err?.message || t('common.saveFailed'));
    } finally {
      setRecompiling(false);
    }
  };

  const handleExport = async () => {
    try {
      const { yaml_text } = await exportLlmWikiSchemaYaml(kbId);
      const blob = new Blob([yaml_text], { type: 'text/yaml;charset=utf-8' });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `llm-wiki-schema-${kbId}.yaml`;
      a.click();
      URL.revokeObjectURL(url);
    } catch (err: any) {
      message.error(err?.message || t('common.loadFailed'));
    }
  };

  const handleImport = async () => {
    if (!schema || !importText.trim()) return;
    try {
      const result = await importLlmWikiSchemaYaml({
        knowledge_base_id: kbId,
        expected_schema_version: schema.schema_version,
        yaml_text: importText,
        dry_run: false,
      });
      if ('schema_version' in result) {
        setSchema(result as LlmWikiSchema);
        setEntityTypes((result as LlmWikiSchema).entity_types || []);
        setConceptTypes((result as LlmWikiSchema).concept_types || []);
        setAgentsMdExtra((result as LlmWikiSchema).agents_md_extra || '');
        setDirty(false);
        setImportOpen(false);
        setImportText('');
        message.success(t('common.saveSuccess'));
      }
    } catch (err: any) {
      message.error(err?.message || t('llmWikiSchema.invalidYaml'));
    }
  };

  if (loading) return <div style={{ padding: 48, textAlign: 'center' }}><Spin /></div>;

  return (
    <div>
      {schema?.sync_status === 'apply_failed' && (
        <Alert
          type="warning"
          showIcon
          style={{ marginBottom: 12 }}
          message={t('llmWikiSchema.applyFailedHint')}
          action={
            <Button size="small" icon={<SyncOutlined />} loading={syncing} onClick={handleSync}>
              {t('llmWikiSchema.syncNow')}
            </Button>
          }
        />
      )}

      <Space style={{ marginBottom: 12 }}>
        <Typography.Text type="secondary">
          {t('llmWikiSchema.version', { version: schema?.schema_version ?? '—' })}
        </Typography.Text>
        <Button
          type="primary"
          icon={<SaveOutlined />}
          loading={saving}
          disabled={!dirty || !canWrite}
          title={canWrite ? undefined : t('domainSpace.noManagePermission')}
          onClick={handleSave}
        >
          {t('common.save')}
        </Button>
        <Button icon={<EyeOutlined />} onClick={() => setPreviewOpen(true)}>
          {t('llmWikiSchema.preview')}
        </Button>
        <Button
          icon={<SyncOutlined />}
          loading={syncing}
          disabled={!canWrite}
          title={canWrite ? undefined : t('domainSpace.noManagePermission')}
          onClick={handleSync}
        >
          {t('llmWikiSchema.syncToEngine')}
        </Button>
        <Button onClick={handleExport}>{t('llmWikiSchema.exportYaml')}</Button>
        <Button
          disabled={!canWrite}
          title={canWrite ? undefined : t('domainSpace.noManagePermission')}
          onClick={() => setImportOpen(true)}
        >
          {t('llmWikiSchema.importYaml')}
        </Button>
        <Button
          danger
          icon={<ReloadOutlined />}
          loading={recompiling}
          disabled={!canWrite}
          title={canWrite ? undefined : t('domainSpace.noManagePermission')}
          onClick={handleRecompileAll}
        >
          {t('llmWikiSchema.recompileAll')}
        </Button>
      </Space>

      <Tabs
        items={[
          {
            key: 'entity',
            label: t('llmWikiSchema.tabEntityTypes'),
            children: (
              <div style={sectionCardStyle}>
                <VocabSection
                  kind="entity"
                  value={entityTypes}
                  onChange={(v) => {
                    setEntityTypes(v as LlmWikiEntityTypeItem[]);
                    markDirty();
                  }}
                />
              </div>
            ),
          },
          {
            key: 'concept',
            label: t('llmWikiSchema.tabConceptTypes'),
            children: (
              <div style={sectionCardStyle}>
                <VocabSection
                  kind="concept"
                  value={conceptTypes}
                  onChange={(v) => {
                    setConceptTypes(v as LlmWikiConceptTypeItem[]);
                    markDirty();
                  }}
                />
              </div>
            ),
          },
          {
            key: 'custom',
            label: t('llmWikiSchema.tabCustomBlock'),
            children: (
              <div style={sectionCardStyle}>
                <Input.TextArea
                  rows={24}
                  value={agentsMdExtra}
                  onChange={(e) => {
                    setAgentsMdExtra(e.target.value);
                    markDirty();
                  }}
                  placeholder={t('llmWikiSchema.customBlockPlaceholder')}
                />
              </div>
            ),
          },
        ]}
      />

      <SchemaPreviewDrawer
        open={previewOpen}
        onClose={() => setPreviewOpen(false)}
        schema={schema}
      />

      <Modal
        open={importOpen}
        title={t('llmWikiSchema.importYaml')}
        onCancel={() => setImportOpen(false)}
        onOk={handleImport}
        okText={t('common.confirm')}
        cancelText={t('common.cancel')}
        width={640}
      >
        <Input.TextArea
          rows={16}
          value={importText}
          onChange={(e) => setImportText(e.target.value)}
          placeholder={'format_version: 1\nschema_name: default\nentity_types:\n  - code: other\n    name: 其他'}
        />
      </Modal>
    </div>
  );
}
