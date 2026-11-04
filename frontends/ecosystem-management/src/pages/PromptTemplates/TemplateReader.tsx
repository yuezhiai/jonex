import React from 'react';
import { useTranslation } from 'react-i18next';
import { Button, Empty, message, Tooltip } from 'antd';
import {
  BranchesOutlined,
  CopyOutlined,
  DeleteOutlined,
  EditOutlined,
  SnippetsOutlined,
} from '@ant-design/icons';
import copy from 'copy-to-clipboard';
import {
  CATEGORY_ICON_MAP,
  PROMPT_CATEGORY_LABEL_KEYS,
  type PromptTemplateItem,
} from '../../api/promptTemplates';
import { systemPromptTemplateDisplay } from '../../utils/systemPromptTemplateDisplay';
import { formatUpdatedAt } from './formatTime';

export interface TemplateReaderProps {
  template: PromptTemplateItem | null;
  onEdit: (id: string) => void;
  onDelete: (id: string) => void;
  onVersionHistory: (id: string) => void;
  onCopyTemplate: (id: string) => void;
  /** 复制到当前空间需要已选领域空间，未选时按钮置灰 */
  canCopyToSpace: boolean;
}

/** 中栏：选中模板的全文 + 全部操作（编辑/删除/版本历史仅领域模板可用）。 */
const TemplateReader: React.FC<TemplateReaderProps> = ({
  template,
  onEdit,
  onDelete,
  onVersionHistory,
  onCopyTemplate,
  canCopyToSpace,
}) => {
  const { t } = useTranslation();

  if (!template) {
    return (
      <section className="ptp-reader">
        <div className="ptp-reader-empty">
          <Empty
            image={Empty.PRESENTED_IMAGE_SIMPLE}
            description={
              <div style={{ lineHeight: 1.9 }}>
                <div style={{ fontSize: 13.5, color: '#64748b' }}>{t('promptTemplate.selectHint')}</div>
                <div style={{ fontSize: 12 }}>{t('promptTemplate.selectHintSub')}</div>
              </div>
            }
          />
        </div>
      </section>
    );
  }

  const display = systemPromptTemplateDisplay(template, t);
  const categoryInfo = CATEGORY_ICON_MAP[display.category] || CATEGORY_ICON_MAP['其他'];
  const isSystem = display.scope === 'system';
  const versions = display.versions_json || [];
  const content = versions.length > 0 ? versions[0].content : '';
  const lastEditor = versions.length > 0 ? versions[0].updated_by : display.created_by;

  // 普通函数而非 useCallback：上面有 template 为空的提前 return，用 Hook 会违反调用顺序
  const handleCopyText = async () => {
    const ok = await copy(content);
    if (ok) message.success(t('promptTemplate.copySuccess'));
    else message.error(t('promptTemplate.copyFailed'));
  };

  return (
    <section className="ptp-reader">
      <div className="ptp-reader-head">
        <div className="ptp-reader-title">
          <span className="ptp-reader-ico" style={{ background: categoryInfo.bg }}>
            {categoryInfo.icon}
          </span>
          <span className="ptp-reader-name" title={display.name}>
            {display.name}
          </span>
          {!isSystem && (
            <span className={`ptp-badge ${display.status === '启用' ? 'on' : 'off'}`}>
              {display.status === '启用'
                ? t('promptTemplate.enabled')
                : display.status === '停用'
                  ? t('promptTemplate.disabled')
                  : display.status}
            </span>
          )}
        </div>

        <div className="ptp-reader-meta">
          <span className={`ptp-chip ${isSystem ? 'system' : 'domain'}`}>
            {isSystem ? t('promptTemplate.systemScope') : t('promptTemplate.domainScope')}
          </span>
          <span className="ptp-chip cat">
            {t(PROMPT_CATEGORY_LABEL_KEYS[display.category] || display.category)}
          </span>
          <span className="ptp-chip ver">v{display.current_version}</span>
          <span style={{ marginLeft: 4 }}>
            {lastEditor ? `${lastEditor} · ` : ''}
            {formatUpdatedAt(display.updated_at)}
          </span>
        </div>

        <div className="ptp-reader-acts">
          {/* 左：对「内容」的操作；右：对「模板记录」的操作，两端分列减少误点 */}
          <div className="ptp-acts-main">
            <Tooltip title={t('promptTemplate.copyPromptText')}>
              <Button
                className="ptp-act-primary"
                type="primary"
                size="small"
                icon={<SnippetsOutlined />}
                onClick={handleCopyText}
                disabled={!content}
              >
                {t('promptTemplate.copyFullText')}
              </Button>
            </Tooltip>
            <Tooltip
              title={canCopyToSpace ? t('promptTemplate.copyToTenant') : t('promptTemplate.noDomainSpaceHint')}
            >
              <Button
                size="small"
                icon={<CopyOutlined />}
                onClick={() => onCopyTemplate(display.id)}
                disabled={!canCopyToSpace}
              >
                {t('promptTemplate.copyToSpace')}
              </Button>
            </Tooltip>
            {!isSystem && (
              <Button size="small" icon={<BranchesOutlined />} onClick={() => onVersionHistory(display.id)}>
                {t('promptTemplate.version')}
                <span className="ptp-act-count">{versions.length}</span>
              </Button>
            )}
          </div>

          {!isSystem && (
            <div className="ptp-acts-manage">
              <Button className="ptp-act-quiet" type="text" size="small" icon={<EditOutlined />} onClick={() => onEdit(display.id)}>
                {t('common.edit')}
              </Button>
              <Button
                className="ptp-act-quiet"
                type="text"
                size="small"
                danger
                icon={<DeleteOutlined />}
                onClick={() => onDelete(display.id)}
              >
                {t('common.delete')}
              </Button>
            </div>
          )}
        </div>
      </div>

      <div className="ptp-reader-body">
        {content ? (
          <pre className="ptp-content">{content}</pre>
        ) : (
          <div className="ptp-content-empty">{t('promptTemplate.noContent')}</div>
        )}
      </div>
    </section>
  );
};

export default React.memo(TemplateReader);
