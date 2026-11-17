/**
 * [jonex] jonex_wiki 编译设置 Tab（kb_type=jonex_wiki 的编译设置分流目标）。
 *
 * jonex_wiki 编译设置只有「编译偏好 suffix」——编辑框内容追加到引擎 compile_main
 * 固定骨架（路径/证据/JSON 输出硬约束，代码维护、不可改）之后；没有 openkb 的
 * 实体/概念受控词表。复用 llm-wiki/schema 后端（DB 版本 + fencing），UI 只暴露
 * 一个编辑框；agents_md 字段承载 suffix（后端 kb_type=jonex_wiki 分支：
 * agents_md = suffix，不渲染 openkb 词表段）。空串合法 = 无偏好 = 引擎纯骨架。
 */
import { useCallback, useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Alert, Button, Input, Space, Spin, Typography, message } from 'antd';
import { SaveOutlined, SyncOutlined } from '@ant-design/icons';
import { applyLlmWikiSchema, getLlmWikiSchema, saveLlmWikiSchema } from '@/api/llmWikiSchema';
import type { LlmWikiSchema } from '@/types/domainKnowledge';

interface Props {
  kbId: string;
  /** KB 写权限（权限矩阵） */
  canWrite?: boolean;
}

// 白底卡片样式（对齐参考页 llmWikiSchema/LlmWikiSchemaTab 的 sectionCardStyle）
const sectionCardStyle: React.CSSProperties = {
  background: '#fff',
  borderRadius: 14,
  border: '1px solid var(--yx-border-light)',
  padding: 24,
  boxShadow: '0 1px 4px rgba(0,0,0,0.04)',
};

export default function JonexWikiPromptTab({ kbId, canWrite = false }: Props) {
  const { t } = useTranslation();

  const [schema, setSchema] = useState<LlmWikiSchema | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [syncing, setSyncing] = useState(false);
  const [promptText, setPromptText] = useState('');
  const [dirty, setDirty] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const s = await getLlmWikiSchema(kbId);
      setSchema(s);
      setPromptText(s.agents_md || '');
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

  const handleSave = async () => {
    if (!schema) return;
    setSaving(true);
    try {
      const saved = await saveLlmWikiSchema({
        knowledge_base_id: kbId,
        expected_schema_version: schema.schema_version,
        schema_name: schema.schema_name,
        language: schema.language,
        model: schema.model,
        // jonex_wiki 无词表：entity/concept 传空；编译偏好 suffix 放 agents_md_extra
        entity_types: [],
        concept_types: [],
        agents_md_extra: promptText,
      });
      setSchema(saved);
      setPromptText(saved.agents_md || '');
      setDirty(false);
      if (saved.sync_status === 'apply_failed') {
        message.warning(t('llmWikiSchema.applyFailedHint'));
      } else {
        message.success(t('common.saveSuccess'));
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
      </Space>

      <div style={sectionCardStyle}>
        <Typography.Title level={5} style={{ marginTop: 0 }}>
          compile_main
        </Typography.Title>
        <Typography.Paragraph type="secondary">
          {t('jonexWikiPrompt.hint')}
        </Typography.Paragraph>
        <Input.TextArea
          rows={22}
          value={promptText}
          onChange={(e) => {
            setPromptText(e.target.value);
            setDirty(true);
          }}
          placeholder={t('jonexWikiPrompt.placeholder')}
        />
      </div>
    </div>
  );
}
