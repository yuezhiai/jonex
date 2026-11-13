import { useTranslation } from 'react-i18next';
import { Button, Modal, Space, Tag, Typography, Descriptions } from 'antd';
import type { SkillItem } from '../../api/skills';
import {
  CATEGORY_ICONS,
  CATEGORY_LABEL_KEYS,
  BUILT_IN_SKILL_KEYS,
  localizeSchemaDescriptions,
  formatJsonSchema,
} from './shared';

const { Text, Paragraph } = Typography;

function getSkillDetail(skill: SkillItem, t: (key: string) => string) {
  const key = BUILT_IN_SKILL_KEYS[skill.id];
  if (!key) {
    return {
      name: skill.name,
      description: skill.description || '',
      instruction: skill.instruction,
    };
  }
  return {
    name: t(`skills.builtIn.${key}.name`),
    description: t(`skills.builtIn.${key}.description`),
    instruction: t(`skills.builtIn.${key}.instruction`),
  };
}

interface SkillDetailModalProps {
  open: boolean;
  skill: SkillItem | null;
  onClose: () => void;
}

export default function SkillDetailModal({ open, skill, onClose }: SkillDetailModalProps) {
  const { t } = useTranslation();
  const detail = skill ? getSkillDetail(skill, t) : null;

  return (
    <Modal
      title={
        <Space>
          <span style={{ fontSize: 24 }}>{CATEGORY_ICONS[skill?.category || 'custom']}</span>
          <span>{detail?.name}</span>
        </Space>
      }
      open={open}
      onCancel={onClose}
      footer={<Button onClick={onClose}>{t('common.close')}</Button>}
      width={680}
    >
      {skill && detail && (
        <div>
          <Descriptions column={2} size="small" bordered style={{ marginBottom: 16 }}>
            <Descriptions.Item label={t('skills.categoryLabel')}>
              {t(CATEGORY_LABEL_KEYS[skill.category])}
            </Descriptions.Item>
            <Descriptions.Item label={t('common.status')}>
              <Tag color={skill.enabled ? 'green' : 'default'}>
                {skill.enabled ? t('skills.enabled') : t('skills.disabled')}
              </Tag>
            </Descriptions.Item>
            <Descriptions.Item label={t('skills.toolName')} span={2}>
              <code>{skill.tool_name}</code>
            </Descriptions.Item>
          </Descriptions>

          <Text strong style={{ display: 'block', marginBottom: 8 }}>
            {t('skills.instruction')}
          </Text>
          <Paragraph style={{ background: 'var(--yx-bg-subtle)', padding: 12, borderRadius: 8, fontSize: 13, lineHeight: 1.7 }}>
            {detail.instruction}
          </Paragraph>

          <Text strong style={{ display: 'block', marginBottom: 8, marginTop: 16 }}>
            {t('skills.inputSchema')}
          </Text>
          <pre
            style={{
              background: '#1e293b',
              color: 'var(--yx-border)',
              padding: 12,
              borderRadius: 8,
              fontSize: 12,
              overflow: 'auto',
              maxHeight: 200,
            }}
          >
            {formatJsonSchema(
              (BUILT_IN_SKILL_KEYS[skill.id]
                ? localizeSchemaDescriptions(skill.input_schema, t)
                : skill.input_schema) as Record<string, unknown>,
            )}
          </pre>

          <Text strong style={{ display: 'block', marginBottom: 8, marginTop: 16 }}>
            {t('skills.outputSchema')}
          </Text>
          <pre
            style={{
              background: '#1e293b',
              color: 'var(--yx-border)',
              padding: 12,
              borderRadius: 8,
              fontSize: 12,
              overflow: 'auto',
              maxHeight: 200,
            }}
          >
            {formatJsonSchema(skill.output_schema)}
          </pre>

          <Text strong style={{ display: 'block', marginBottom: 8, marginTop: 16 }}>
            {t('skills.capability')}
          </Text>
          <pre
            style={{
              background: 'var(--yx-bg-subtle)',
              padding: 12,
              borderRadius: 8,
              fontSize: 12,
              overflow: 'auto',
              maxHeight: 150,
            }}
          >
            {formatJsonSchema(skill.capability || {})}
          </pre>
        </div>
      )}
    </Modal>
  );
}
