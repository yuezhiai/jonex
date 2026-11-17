import React, { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Button, Input, Select } from 'antd';
import { SettingOutlined, SaveOutlined } from '@ant-design/icons';
import type { EngineSetting } from '@/types/domainKnowledge';
import { ENGINE_MODEL_OPTIONS, DEFAULT_ENGINE_MODEL } from './constants';

// 首层卡面已由 PageContent 的卡片承担，这里只留分区之间的间距
const sectionStyle: React.CSSProperties = {
  marginBottom: 20,
};
const h3Style: React.CSSProperties = {
  margin: 0,
  fontSize: 15,
  fontWeight: 600,
  color: 'var(--yx-brand-dark)',
  display: 'flex',
  alignItems: 'center',
  gap: 8,
};

interface Props {
  engine: EngineSetting | null;
  loading: boolean;
  onSave: (model: string) => void;
}

export default function EngineBasicSection({ engine, loading, onSave }: Props) {
  const { t } = useTranslation();
  const [model, setModel] = useState<string>(DEFAULT_ENGINE_MODEL);
  useEffect(() => {
    if (engine) setModel(engine.semanticModel);
  }, [engine]);

  return (
    <div style={sectionStyle}>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 12 }}>
        <h3 style={h3Style}>
          <SettingOutlined style={{ color: 'var(--yx-accent)' }} /> {t('compile.engine.sectionTitle')}
        </h3>
      </div>
      <p style={{ fontSize: 13, color: 'var(--yx-text-muted)', marginBottom: 12 }}>{t('compile.engine.sectionDesc')}</p>
      <div style={{ padding: 20, maxWidth: 560, border: '1px solid var(--yx-border-light)', borderRadius: 10 }}>
        <div className="yx-form-row">
          <label>
            {t('compile.engine.modelLabel')} <span style={{ color: 'var(--yx-error-text)' }}>*</span>
          </label>
          <Select
            value={model}
            onChange={setModel}
            style={{ width: '100%' }}
            loading={loading}
            options={ENGINE_MODEL_OPTIONS}
          />
        </div>
        <div className="yx-form-row" style={{ marginTop: 16 }}>
          <label>{t('compile.engine.promptLabel')}</label>
          <Input.TextArea rows={4} disabled placeholder={t('compile.engine.promptPlaceholder')} />
          <div style={{ fontSize: 12, color: 'var(--yx-text-muted)', marginTop: 6 }}>{t('compile.engine.promptHint')}</div>
        </div>
        <div style={{ marginTop: 16, display: 'flex', gap: 10 }}>
          <Button type="primary" icon={<SaveOutlined />} onClick={() => onSave(model)}>
            {t('compile.engine.saveBtn')}
          </Button>
          <Button onClick={() => setModel(DEFAULT_ENGINE_MODEL)}>{t('compile.engine.resetBtn')}</Button>
        </div>
      </div>
    </div>
  );
}
