import React from 'react';
import { useTranslation } from 'react-i18next';
import { Button } from 'antd';
import { SearchOutlined } from '@ant-design/icons';
import styles from './WelcomeEmpty.module.scss';

/** 建议卡：标题与示例问题各自一个 i18n key，点击把示例问题填进输入框 */
const SUGGESTIONS = [
  { titleKey: 'knowledgeSearch.suggestionBusinessTitle', promptKey: 'knowledgeSearch.suggestionBusinessPrompt' },
  { titleKey: 'knowledgeSearch.suggestionRuleTitle', promptKey: 'knowledgeSearch.suggestionRulePrompt' },
  { titleKey: 'knowledgeSearch.suggestionRelationTitle', promptKey: 'knowledgeSearch.suggestionRelationPrompt' },
  { titleKey: 'knowledgeSearch.suggestionProcessTitle', promptKey: 'knowledgeSearch.suggestionProcessPrompt' },
] as const;

interface WelcomeEmptyProps {
  /** 点建议卡：回传该卡的示例问题，由页面填进输入框（不自动提交） */
  onPickSuggestion: (prompt: string) => void;
}

export default function WelcomeEmpty({ onPickSuggestion }: WelcomeEmptyProps) {
  const { t } = useTranslation();
  return (
    <div className={styles.welcome}>
      <div className={styles.logo}>
        <SearchOutlined />
      </div>
      <h2 className={styles.title}>{t('knowledgeSearch.welcomeTitle')}</h2>
      <p className={styles.subtitle}>{t('knowledgeSearch.welcomeSubtitle')}</p>
      <div className={styles.suggestLabel}>{t('knowledgeSearch.welcomeSuggestLabel')}</div>
      <div className={styles.suggestions}>
        {SUGGESTIONS.map((item) => (
          <Button
            key={item.titleKey}
            type="text"
            className={styles.suggestion}
            // antd Button 默认横排居中且 nowrap，建议卡要的是竖排左对齐、可换行
            style={{
              display: 'flex',
              flexDirection: 'column',
              alignItems: 'stretch',
              justifyContent: 'flex-start',
              gap: 8,
              height: 'auto',
              padding: '20px 22px',
              whiteSpace: 'normal',
              textAlign: 'left',
            }}
            onClick={() => onPickSuggestion(t(item.promptKey))}
          >
            <span className={styles.suggestionTitle}>{t(item.titleKey)}</span>
            <span className={styles.suggestionPrompt}>{t(item.promptKey)}</span>
          </Button>
        ))}
      </div>
    </div>
  );
}
