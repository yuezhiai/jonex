import React, { useCallback } from 'react';
import { useTranslation } from 'react-i18next';
import { Button, message, Tooltip, Typography } from 'antd';
import { CopyOutlined, DeleteOutlined, EditOutlined, EyeOutlined, BranchesOutlined } from '@ant-design/icons';
import copy from 'copy-to-clipboard';
import type { PromptTemplateItem } from '../../api/promptTemplates';
import { CATEGORY_ICON_MAP, PROMPT_CATEGORY_LABEL_KEYS } from '../../api/promptTemplates';
import { systemPromptTemplateDisplay } from '../../utils/systemPromptTemplateDisplay';

interface PromptCardProps {
  template: PromptTemplateItem;
  onEdit: (id: string) => void;
  onView: (id: string) => void;
  onDelete: (id: string) => void;
  onVersion: (id: string) => void;
  onCopy: (id: string) => void;
}

function getCurrentContent(t: PromptTemplateItem): string {
  const versions = t.versions_json || [];
  return versions.length > 0 ? versions[0].content : '';
}

function escapeHtml(s: string | null | undefined): string {
  if (!s) return '';
  return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

const PromptCard: React.FC<PromptCardProps> = ({ template, onEdit, onView, onDelete, onVersion, onCopy }) => {
  const { t } = useTranslation();
  const displayTemplate = systemPromptTemplateDisplay(template, t);
  const categoryInfo = CATEGORY_ICON_MAP[displayTemplate.category] || CATEGORY_ICON_MAP['其他'];
  const isSystem = displayTemplate.scope === 'system';
  const content = getCurrentContent(displayTemplate);
  const previewHtml = escapeHtml(content).replace(/\n/g, '<br>');

  const handleCopyContent = useCallback(
    async (e: React.MouseEvent) => {
      e.stopPropagation();
      const ok = await copy(content);
      if (ok) message.success(t('promptTemplate.copySuccess'));
      else message.error(t('promptTemplate.copyFailed'));
    },
    [content],
  );

  const handleCopyTemplate = useCallback(
    (e: React.MouseEvent) => {
      e.stopPropagation();
      onCopy(template.id);
    },
    [template.id, onCopy],
  );

  return (
    <div className="pt-card">
      {/* Header: icon + meta */}
      <div className="pt-card-top">
        <div className="pt-icon" style={{ background: categoryInfo.bg }}>
          {categoryInfo.icon}
        </div>
        <div className="pt-meta">
          {/* 名称与状态徽章同一行、左右分布；描述在下一行，不影响徽章位置 */}
          <div className="pt-title-row">
            {/* 名称单行显示，超出省略；溢出时 hover 显示全文（Typography ellipsis tooltip 仅在溢出时启用） */}
            <Typography.Paragraph
              className="pt-name"
              ellipsis={{ rows: 1, tooltip: displayTemplate.name }}
              style={{ marginBottom: 0 }}
            >
              {displayTemplate.name}
            </Typography.Paragraph>
            {!isSystem && (
              <span className={`pt-status ${displayTemplate.status === '启用' ? 'on' : 'off'}`}>
                {displayTemplate.status === '启用'
                  ? t('promptTemplate.enabled')
                  : displayTemplate.status === '停用'
                    ? t('promptTemplate.disabled')
                    : displayTemplate.status}
              </span>
            )}
          </div>
          <div className="pt-desc">{displayTemplate.description || t('promptTemplate.noDescription')}</div>
        </div>
      </div>

      {/* Content preview */}
      <div className="pt-preview">
        <Tooltip
          rootClassName="pt-preview-tooltip-wrap"
          mouseEnterDelay={0.2}
          mouseLeaveDelay={0.2}
          title={
            content ? (
              // hover 显示完整提示词原文（保留换行，超长可滚动）
              <div className="pt-preview-tooltip">{content}</div>
            ) : undefined
          }
        >
          <div dangerouslySetInnerHTML={{ __html: previewHtml }} />
        </Tooltip>
      </div>

      {/* Tags */}
      <div className="pt-tags">
        <span className={`pt-scope-badge ${isSystem ? 'system' : 'domain'}`}>
          {isSystem ? '🌐 ' + t('promptTemplate.systemScope') : '📦 ' + t('promptTemplate.domainScope')}
        </span>
        <span className="pt-tag-cat">
          {t(PROMPT_CATEGORY_LABEL_KEYS[displayTemplate.category] || displayTemplate.category)}
        </span>
        <span className="pt-ver-badge">🔀 v{displayTemplate.current_version || '1'}</span>
      </div>

      {/* Actions */}
      <div className="pt-actions">
        {isSystem ? (
          <>
            <Tooltip title={t('promptTemplate.viewFullInfo')}>
              <Button size="small" icon={<EyeOutlined />} onClick={() => onView(template.id)}>
                {t('promptTemplate.viewBtn')}
              </Button>
            </Tooltip>
            <Tooltip title={t('promptTemplate.copyToTenant')}>
              <Button size="small" icon={<CopyOutlined />} onClick={handleCopyTemplate}>
                {t('promptTemplate.copy')}
              </Button>
            </Tooltip>
            <Tooltip title={t('promptTemplate.copyPromptText')}>
              <Button size="small" icon={<CopyOutlined />} onClick={handleCopyContent}>
                {t('promptTemplate.copyText')}
              </Button>
            </Tooltip>
          </>
        ) : (
          <>
            <Tooltip title={t('common.edit')}>
              <Button size="small" icon={<EditOutlined />} onClick={() => onEdit(template.id)}>
                {t('common.edit')}
              </Button>
            </Tooltip>
            <Tooltip title={t('promptTemplate.copyToTenant')}>
              <Button size="small" icon={<CopyOutlined />} onClick={handleCopyTemplate}>
                {t('promptTemplate.copy')}
              </Button>
            </Tooltip>
            <Tooltip title={t('promptTemplate.version')}>
              <Button size="small" icon={<BranchesOutlined />} onClick={() => onVersion(template.id)}>
                {t('promptTemplate.versionBtn')}
              </Button>
            </Tooltip>
            <Tooltip title={t('common.delete')}>
              <Button size="small" danger icon={<DeleteOutlined />} onClick={() => onDelete(template.id)}>
                {t('common.delete')}
              </Button>
            </Tooltip>
          </>
        )}
      </div>
    </div>
  );
};

export default React.memo(PromptCard);
