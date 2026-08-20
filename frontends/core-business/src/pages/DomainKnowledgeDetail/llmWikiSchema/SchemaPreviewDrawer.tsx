/**
 * [jonex] 预览抽屉（方案 §10）：显示最终 AGENTS.md 与 OpenKB config.yaml。
 * 预览数据 = 后端 render_agents_md 的产物（与保存时实际投影一致）。
 */
import { Drawer, Tabs } from 'antd';
import { useTranslation } from 'react-i18next';
import type { LlmWikiSchema } from '@/types/domainKnowledge';

interface Props {
  open: boolean;
  onClose: () => void;
  schema: LlmWikiSchema | null;
}

export default function SchemaPreviewDrawer({ open, onClose, schema }: Props) {
  const { t } = useTranslation();
  if (!schema) return null;

  const configYaml = [
    'model: ' + (schema.model ?? '(null = 继承 OpenKB 默认)'),
    'language: ' + schema.language,
    'entity_types:',
    ...schema.entity_types.map((e) => `  - ${e.code}`),
  ].join('\n');

  return (
    <Drawer
      title={t('llmWikiSchema.preview')}
      width={720}
      open={open}
      onClose={onClose}
    >
      <Tabs
        items={[
          {
            key: 'agents_md',
            label: 'AGENTS.md',
            children: (
              <pre
                style={{
                  whiteSpace: 'pre-wrap',
                  fontSize: 12,
                  lineHeight: 1.7,
                  background: '#f8fafc',
                  padding: 12,
                  borderRadius: 8,
                  maxHeight: 'calc(100vh - 200px)',
                  overflow: 'auto',
                }}
              >
                {schema.agents_md}
              </pre>
            ),
          },
          {
            key: 'config',
            label: 'config.yaml',
            children: (
              <pre
                style={{
                  whiteSpace: 'pre-wrap',
                  fontSize: 12,
                  lineHeight: 1.7,
                  background: '#f8fafc',
                  padding: 12,
                  borderRadius: 8,
                }}
              >
                {configYaml}
              </pre>
            ),
          },
        ]}
      />
    </Drawer>
  );
}
