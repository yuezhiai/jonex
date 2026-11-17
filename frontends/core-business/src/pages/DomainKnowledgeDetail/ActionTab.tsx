import React from 'react';
import { Button, Tag } from 'antd';
import { ThunderboltOutlined, PlusOutlined, BugOutlined, BellOutlined } from '@ant-design/icons';
import { useTranslation } from 'react-i18next';
import type { ActionRule, RuleTextSegment } from '@/types/domainKnowledge';
import { actionIconMap } from './index';

interface ActionTabProps {
  actionRules: ActionRule[];
  actionLoading: boolean;
  ruleStatusLabel: (status: string) => string;
  renderRuleText: (segments: RuleTextSegment[]) => React.ReactNode;
}

export default function ActionTab({ actionRules, actionLoading, ruleStatusLabel, renderRuleText }: ActionTabProps) {
  const { t } = useTranslation();

  return (
    <div>
      <div className="yx-kb-flex-header">
        <h3 className="yx-kb-section-title">
          <ThunderboltOutlined className="yx-kb-icon-purple" /> {t('domainKnowledge.triggerRules')}
        </h3>
        <Button
          type="text"
          className="yx-kb-section-add-btn"
          icon={<PlusOutlined />}
          style={{ display: 'inline-flex', alignItems: 'center', gap: 6 }}
        >
          {t('domainKnowledge.newRule')}
        </Button>
      </div>
      <p className="yx-kb-section-desc">{t('domainKnowledge.ruleHelp')}</p>
      {actionLoading ? (
        <div className="yx-kb-empty-state">{t('common.loading')}</div>
      ) : (
        actionRules.map((rule) => {
          const TrigIcon = actionIconMap[rule.triggerIconType] || BugOutlined;
          const ActIcon = actionIconMap[rule.actionIconType] || BellOutlined;
          return (
            <div key={rule.id} className="yx-kb-rule-box">
              <div className="yx-kb-flex-between">
                <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
                  <span style={{ fontSize: 14, fontWeight: 600, color: 'var(--yx-brand-dark)' }}>{rule.name}</span>
                  <Tag color={rule.status === '启用' ? 'success' : 'warning'} style={{ fontSize: 11 }}>
                    {ruleStatusLabel(rule.status)}
                  </Tag>
                </div>
                <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                  <label className="yx-kb-toggle-track">
                    <input
                      type="checkbox"
                      defaultChecked={rule.status === '启用'}
                      style={{ opacity: 0, width: 0, height: 0 }}
                    />
                    <span
                      style={{
                        position: 'absolute',
                        cursor: 'pointer',
                        top: 0,
                        left: 0,
                        right: 0,
                        bottom: 0,
                        background: rule.status === '启用' ? 'var(--yx-accent)' : '#cbd5e1',
                        borderRadius: 20,
                        transition: '0.3s',
                      }}
                    />
                    <span
                      style={{
                        position: 'absolute',
                        height: 14,
                        width: 14,
                        left: rule.status === '启用' ? 19 : 3,
                        bottom: 3,
                        background: '#fff',
                        borderRadius: '50%',
                        transition: '0.3s',
                      }}
                    />
                  </label>
                  <a className="yx-table-action">{t('domainKnowledge.edit')}</a>
                  <a className="yx-table-action" style={{ color: '#ef4444' }}>
                    {t('domainKnowledge.delete')}
                  </a>
                </div>
              </div>
              <div className="yx-kb-trigger-grid">
                <div className="yx-kb-flex-gap">
                  <div className="yx-kb-rule-icon">
                    <TrigIcon />
                  </div>
                  <div>
                    <div className="yx-kb-meta-label">{t('domainKnowledge.triggerCondition')}</div>
                    <div className="yx-kb-meta-value">{renderRuleText(rule.triggerText)}</div>
                  </div>
                </div>
                <div className="yx-kb-flex-gap">
                  <div className="yx-kb-rule-icon">
                    <ActIcon />
                  </div>
                  <div>
                    <div className="yx-kb-meta-label">{t('domainKnowledge.actionExecution')}</div>
                    <div className="yx-kb-meta-value">{renderRuleText(rule.actionText)}</div>
                  </div>
                </div>
              </div>
            </div>
          );
        })
      )}
    </div>
  );
}
