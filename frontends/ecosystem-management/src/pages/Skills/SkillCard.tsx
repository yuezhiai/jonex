import { Button, Card, Switch, Tag, Typography } from 'antd';
import { ArrowRightOutlined } from '@ant-design/icons';
import { colors } from '@jonex/platform-theme/tokens';
import type { SkillItem } from '../../api/skills';
import { CATEGORY_ICONS, CATEGORY_LABEL_KEYS, CATEGORY_TINTS, getSkillCopy } from './shared';

const { Text } = Typography;

/** 卡片上直出的自定义标签数，其余折叠成 +N */
const MAX_VISIBLE_TAGS = 2;

interface SkillCardProps {
  skill: SkillItem;
  busy: boolean;
  onToggle: (skill: SkillItem) => void;
  onOpen: (skill: SkillItem) => void;
  t: (key: string) => string;
}

export default function SkillCard({ skill, busy, onToggle, onOpen, t }: SkillCardProps) {
  const display = getSkillCopy(skill, t);
  const tint = CATEGORY_TINTS[skill.category] || CATEGORY_TINTS.custom;
  const hiddenTags = Math.max(0, display.tags.length - MAX_VISIBLE_TAGS);

  return (
    <Card
      hoverable
      className="skill-card"
      style={{ display: 'flex', flexDirection: 'column' }}
      styles={{ body: { display: 'flex', flexDirection: 'column', flex: 1, padding: 16 } }}
      onClick={() => onOpen(skill)}
    >
      <div className="skill-card-top">
        <div className="skill-card-icon" style={{ background: tint.background, color: tint.color }}>
          {CATEGORY_ICONS[skill.category] || CATEGORY_ICONS.custom}
        </div>
        <div className="skill-card-heading">
          <div className="skill-card-title">{display.name}</div>
          <Text className="skill-card-toolname">{skill.tool_name}</Text>
        </div>
      </div>

      <p className="skill-card-desc">{display.description || t('common.noDescription')}</p>

      <div className="skill-card-tags">
        <Tag className="skill-tag-category">
          {CATEGORY_LABEL_KEYS[skill.category] ? t(CATEGORY_LABEL_KEYS[skill.category]) : skill.category}
        </Tag>
        {display.tags.slice(0, MAX_VISIBLE_TAGS).map((tag) => (
          <Tag key={tag}>{tag}</Tag>
        ))}
        {hiddenTags > 0 && <span className="skill-tag-more">+{hiddenTags}</span>}
      </div>

      <div className="skill-card-footer">
        <div className="skill-card-state" onClick={(e) => e.stopPropagation()}>
          <Switch size="small" checked={skill.enabled} loading={busy} onChange={() => onToggle(skill)} />
          <span style={{ color: skill.enabled ? colors.successText : colors.textMuted }}>
            {skill.enabled ? t('skills.enabled') : t('skills.disabled')}
          </span>
        </div>
        <Button
          type="link"
          size="small"
          className="skill-card-detail"
          onClick={(e) => {
            e.stopPropagation();
            onOpen(skill);
          }}
        >
          {t('skills.detailBtn')}
          <ArrowRightOutlined />
        </Button>
      </div>
    </Card>
  );
}
