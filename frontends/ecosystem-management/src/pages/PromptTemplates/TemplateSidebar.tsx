import React from 'react';
import { useTranslation } from 'react-i18next';
import { Button, Empty, Input, Segmented, Select, Spin } from 'antd';
import {
  AppstoreOutlined,
  GlobalOutlined,
  PlusOutlined,
  ReloadOutlined,
  SearchOutlined,
} from '@ant-design/icons';
import {
  CATEGORY_ICON_MAP,
  PROMPT_CATEGORIES,
  PROMPT_CATEGORY_LABEL_KEYS,
  type PromptTemplateItem,
} from '../../api/promptTemplates';
import { systemPromptTemplateDisplay } from '../../utils/systemPromptTemplateDisplay';
import { formatUpdatedAt } from './formatTime';

export interface TemplateSidebarProps {
  scope: 'system' | 'domain';
  onScopeChange: (scope: 'system' | 'domain') => void;
  counts: { system: number; domain: number };
  keywordInput: string;
  onKeywordChange: (value: string) => void;
  category: string;
  onCategoryChange: (value: string) => void;
  total: number;
  loading: boolean;
  templates: PromptTemplateItem[];
  selectedId: string | null;
  onSelect: (id: string) => void;
  /** 「新建」只在领域空间可用（系统模板由后端种子维护，不可建）。 */
  onCreate: () => void;
  onRetry: () => void;
  /** 未选择领域空间时领域模板列表为空，需要区分「真的没有」和「没选空间」。 */
  hasDomainSpace: boolean;
}

/** 左列：范围切换 + 搜索 + 分类 + 模板列表 + 新建入口。 */
const TemplateSidebar: React.FC<TemplateSidebarProps> = ({
  scope,
  onScopeChange,
  counts,
  keywordInput,
  onKeywordChange,
  category,
  onCategoryChange,
  total,
  loading,
  templates,
  selectedId,
  onSelect,
  onCreate,
  onRetry,
  hasDomainSpace,
}) => {
  const { t } = useTranslation();
  const isDomain = scope === 'domain';

  const categoryOptions = [
    { value: 'all', label: t('promptTemplate.allCategories') },
    ...PROMPT_CATEGORIES.map((cat) => ({
      value: cat,
      label: t(PROMPT_CATEGORY_LABEL_KEYS[cat] || cat),
    })),
  ];

  // 空列表原因有三：加载中、筛选无命中、领域空间未选（此时后端按空间隔离直接返回空）
  const emptyHint = !isDomain
    ? t('promptTemplate.noSystemTemplates')
    : !hasDomainSpace
      ? t('promptTemplate.noDomainSpaceHint')
      : keywordInput || category
        ? t('promptTemplate.listEmpty')
        : t('promptTemplate.noDomainTemplates');

  return (
    <aside className="ptp-side">
      <div className="ptp-side-head">
        <Segmented
          block
          size="small"
          value={scope}
          onChange={(value) => onScopeChange(value as 'system' | 'domain')}
          options={[
            {
              value: 'system',
              label: (
                <span className="ptp-seg-label">
                  <GlobalOutlined />
                  <span className="ptp-seg-name">{t('promptTemplate.scopeTabSystem')}</span>
                  <span className="ptp-seg-n">{counts.system}</span>
                </span>
              ),
            },
            {
              value: 'domain',
              label: (
                <span className="ptp-seg-label">
                  <AppstoreOutlined />
                  <span className="ptp-seg-name">{t('promptTemplate.scopeTabDomain')}</span>
                  <span className="ptp-seg-n">{counts.domain}</span>
                </span>
              ),
            },
          ]}
        />
        <Input
          prefix={<SearchOutlined style={{ color: '#94a3b8' }} />}
          placeholder={t('promptTemplate.searchPlaceholder')}
          value={keywordInput}
          onChange={(e) => onKeywordChange(e.target.value)}
          allowClear
          size="small"
        />
        <div className="ptp-filter-row">
          <Select
            value={category || 'all'}
            onChange={(v: string) => onCategoryChange(v === 'all' ? '' : v)}
            options={categoryOptions}
            size="small"
          />
          <span className="ptp-count">{t('promptTemplate.countText', { count: total })}</span>
        </div>
      </div>

      <div className="ptp-list">
        {/* 只有在「一条都还没拿到」时才显示 Spin：切 tab / 后台刷新都保留现有列表，避免闪烁 */}
        {loading && templates.length === 0 ? (
          <div className="ptp-empty">
            <Spin size="small" />
          </div>
        ) : templates.length === 0 ? (
          // 空态统一用 antd Empty，与「模板域 / MCP 服务目录」等页面保持一致
          <Empty
            className="ptp-empty"
            image={Empty.PRESENTED_IMAGE_SIMPLE}
            description={<span style={{ fontSize: 12.5 }}>{emptyHint}</span>}
          >
            {!isDomain || hasDomainSpace ? null : (
              <Button size="small" icon={<ReloadOutlined />} onClick={onRetry}>
                {t('promptTemplate.retry')}
              </Button>
            )}
          </Empty>
        ) : (
          templates.map((raw) => {
            const tpl = systemPromptTemplateDisplay(raw, t);
            const categoryInfo = CATEGORY_ICON_MAP[tpl.category] || CATEGORY_ICON_MAP['其他'];
            const isSystem = tpl.scope === 'system';
            const active = tpl.id === selectedId;
            const metaText = `${t(PROMPT_CATEGORY_LABEL_KEYS[tpl.category] || tpl.category)} · v${tpl.current_version} · ${formatUpdatedAt(tpl.updated_at)}`;
            return (
              <Button
                key={tpl.id}
                type="text"
                block
                className={`ptp-item${active ? ' on' : ''}`}
                onClick={() => onSelect(tpl.id)}
                title={tpl.name}
              >
                <span className="ptp-item-top">
                  <span className="ptp-item-ico" style={{ background: categoryInfo.bg }}>
                    {categoryInfo.icon}
                  </span>
                  <span className="ptp-item-name">{tpl.name}</span>
                  {!isSystem && <span className={`ptp-dot ${tpl.status === '启用' ? '' : 'off'}`} />}
                </span>
                {/* 英文等语种这行会更长，超出时省略，hover 用 title 兜底 */}
                <span className="ptp-item-meta" title={metaText}>
                  {metaText}
                </span>
              </Button>
            );
          })
        )}
      </div>

      {isDomain && (
        <div className="ptp-side-foot">
          <Button type="primary" icon={<PlusOutlined />} block onClick={onCreate}>
            {t('promptTemplate.create')}
          </Button>
        </div>
      )}
    </aside>
  );
};

export default React.memo(TemplateSidebar);
