import React, { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Input, Select, Button, Card, Switch, InputNumber, Divider, Dropdown, Modal } from 'antd';
import { SearchOutlined, StopOutlined, SettingOutlined, DownOutlined } from '@ant-design/icons';
import classNames from 'classnames';
import type { KnowledgeSearchDomain, KnowledgeSearchStrictConfig } from '@/types/knowledgeSearch';
import { DEFAULT_FAST_STRICT_CONFIG, DEFAULT_DEEP_STRICT_CONFIG } from '@/api/knowledgeSearch';
import styles from './SearchPanel.module.scss';

interface SearchPanelProps {
  selectedDomain: string;
  visibleDomains: KnowledgeSearchDomain[];
  query: string;
  isSearching: boolean;
  /** 是否深度检索（打开时调用 /search/deep 接口） */
  deepSearch: boolean;
  onDeepSearchChange: (value: boolean) => void;
  /** 严格模式配置 */
  strictConfig: KnowledgeSearchStrictConfig;
  onStrictConfigChange: (config: KnowledgeSearchStrictConfig) => void;
  onDomainChange: (value: string) => void;
  onQueryChange: (value: string) => void;
  onSearch: () => void;
  onStop: () => void;
}

interface ConfigFieldProps {
  label: string;
  description: string;
  children: React.ReactNode;
}

function ConfigField({ label, description, children }: ConfigFieldProps) {
  return (
    <div className={styles.configField}>
      <div className={styles.configFieldInfo}>
        <div className={styles.configFieldLabel}>{label}</div>
        <div className={styles.configFieldDesc}>{description}</div>
      </div>
      <div className={styles.configFieldControl}>{children}</div>
    </div>
  );
}

export default function SearchPanel({
  selectedDomain,
  visibleDomains,
  query,
  isSearching,
  deepSearch,
  onDeepSearchChange,
  onStrictConfigChange,
  onDomainChange,
  onQueryChange,
  onSearch,
  onStop,
}: SearchPanelProps) {
  const { t } = useTranslation();

  const [fastConfig, setFastConfig] = useState<KnowledgeSearchStrictConfig>(DEFAULT_FAST_STRICT_CONFIG);
  const [deepConfig, setDeepConfig] = useState<KnowledgeSearchStrictConfig>(DEFAULT_DEEP_STRICT_CONFIG);
  const [modalOpen, setModalOpen] = useState(false);
  const [editingMode, setEditingMode] = useState<'fast' | 'deep'>('fast');
  const [draftConfig, setDraftConfig] = useState<KnowledgeSearchStrictConfig>(fastConfig);
  const [dropdownOpen, setDropdownOpen] = useState(false);

  const activeModeLabel = deepSearch ? t('knowledgeSearch.deepSearch') : t('knowledgeSearch.fastSearch');
  const editingModeLabel = editingMode === 'deep' ? t('knowledgeSearch.deepSearch') : t('knowledgeSearch.fastSearch');

  const switchMode = (nextDeep: boolean) => {
    onDeepSearchChange(nextDeep);
    onStrictConfigChange(nextDeep ? deepConfig : fastConfig);
    setDropdownOpen(false);
  };

  const openConfigModal = (mode: 'fast' | 'deep') => {
    setEditingMode(mode);
    setDraftConfig(mode === 'deep' ? deepConfig : fastConfig);
    setModalOpen(true);
    setDropdownOpen(false);
  };

  const saveConfig = () => {
    if (editingMode === 'deep') {
      setDeepConfig(draftConfig);
      if (deepSearch) {
        onStrictConfigChange(draftConfig);
      }
    } else {
      setFastConfig(draftConfig);
      if (!deepSearch) {
        onStrictConfigChange(draftConfig);
      }
    }
    setModalOpen(false);
  };

  const updateDraft = (patch: Partial<KnowledgeSearchStrictConfig>) => {
    setDraftConfig((prev) => ({ ...prev, ...patch }));
  };

  const dropdownContent = (
    <div className={styles.modeDropdown}>
      <div
        className={classNames(styles.modeOption, { [styles.modeOptionActive]: !deepSearch })}
        onClick={() => switchMode(false)}
        role="button"
        tabIndex={0}
      >
        <div className={styles.modeOptionContent}>
          <div className={styles.modeOptionTitle}>{t('knowledgeSearch.fastSearch')}</div>
          <div className={styles.modeOptionDesc}>{t('knowledgeSearch.fastSearchDesc')}</div>
        </div>
        <Button
          type="text"
          size="small"
          icon={<SettingOutlined />}
          className={styles.modeOptionSetting}
          onClick={(e) => {
            e.stopPropagation();
            openConfigModal('fast');
          }}
          title={t('knowledgeSearch.strictConfigTitle')}
        />
      </div>
      <Divider style={{ margin: 0 }} />
      <div
        className={classNames(styles.modeOption, { [styles.modeOptionActive]: deepSearch })}
        onClick={() => switchMode(true)}
        role="button"
        tabIndex={0}
      >
        <div className={styles.modeOptionContent}>
          <div className={styles.modeOptionTitle}>{t('knowledgeSearch.deepSearch')}</div>
          <div className={styles.modeOptionDesc}>{t('knowledgeSearch.deepSearchDesc')}</div>
        </div>
        <Button
          type="text"
          size="small"
          icon={<SettingOutlined />}
          className={styles.modeOptionSetting}
          onClick={(e) => {
            e.stopPropagation();
            openConfigModal('deep');
          }}
          title={t('knowledgeSearch.strictConfigTitle')}
        />
      </div>
    </div>
  );

  return (
    <Card style={{ borderRadius: 16, marginBottom: 24 }} styles={{ body: { padding: '24px 28px' } }}>
      <div style={{ display: 'flex', gap: 12, alignItems: 'center' }}>
        <Select
          value={selectedDomain}
          onChange={onDomainChange}
          style={{ minWidth: 160, height: 48 }}
          size="large"
          options={visibleDomains.map((d) => ({
            value: d.id,
            label: d.id === 'all' ? t('knowledgeSearch.allDomain') : d.name,
          }))}
        />
        <Input
          prefix={<SearchOutlined style={{ color: '#94a3b8', fontSize: 16 }} />}
          placeholder={t('knowledgeSearch.searchPlaceholder')}
          value={query}
          onChange={(e) => onQueryChange(e.target.value)}
          onPressEnter={onSearch}
          style={{ height: 48, fontSize: 15, background: '#f8fafc', borderRadius: 10 }}
        />
        {isSearching && (
          <Button
            icon={<StopOutlined />}
            onClick={onStop}
            style={{ height: 48, padding: '0 18px', fontSize: 15, borderRadius: 10, flexShrink: 0 }}
          >
            {t('knowledgeSearch.stop')}
          </Button>
        )}
        <div className={styles.modeSelector}>
          <Button
            type="primary"
            icon={<SearchOutlined />}
            onClick={onSearch}
            loading={isSearching}
            className={styles.modeSearchButton}
          >
            {activeModeLabel}
          </Button>
          <Dropdown
            open={dropdownOpen}
            onOpenChange={setDropdownOpen}
            trigger={['click']}
            placement="bottomRight"
            menu={{ items: [] }}
            popupRender={() => dropdownContent}
          >
            <Button type="primary" icon={<DownOutlined />} className={styles.modeDropdownTrigger} disabled={isSearching} />
          </Dropdown>
        </div>
      </div>

      <Modal
        title={`${editingModeLabel} · ${t('knowledgeSearch.strictConfigTitle')}`}
        open={modalOpen}
        onOk={saveConfig}
        onCancel={() => setModalOpen(false)}
        okText={t('knowledgeSearch.saveSettings')}
        cancelText={t('knowledgeSearch.cancel')}
        width={560}
      >
        <div className={styles.configIntro}>{t('knowledgeSearch.strictConfigDescription')}</div>
        <div className={styles.configForm}>
          <ConfigField
            label={t('knowledgeSearch.strictMaxAttempts')}
            description={t('knowledgeSearch.strictMaxAttemptsDesc')}
          >
            <InputNumber
              min={0}
              max={10}
              value={draftConfig.strict_max_attempts}
              onChange={(v) => updateDraft({ strict_max_attempts: v ?? 0 })}
              className={styles.configNumber}
            />
          </ConfigField>
          <ConfigField
            label={t('knowledgeSearch.strictMinScore')}
            description={t('knowledgeSearch.strictMinScoreDesc')}
          >
            <InputNumber
              min={0}
              max={1}
              step={0.05}
              value={draftConfig.strict_min_score}
              onChange={(v) => updateDraft({ strict_min_score: v ?? 0 })}
              className={styles.configNumber}
            />
          </ConfigField>
          <ConfigField
            label={t('knowledgeSearch.strictRequireReference')}
            description={t('knowledgeSearch.strictRequireReferenceDesc')}
          >
            <Switch
              checked={draftConfig.strict_require_reference}
              onChange={(v) => updateDraft({ strict_require_reference: v })}
            />
          </ConfigField>
          <ConfigField
            label={t('knowledgeSearch.strictRequireGrounded')}
            description={t('knowledgeSearch.strictRequireGroundedDesc')}
          >
            <Switch
              checked={draftConfig.strict_require_grounded}
              onChange={(v) => updateDraft({ strict_require_grounded: v })}
            />
          </ConfigField>
          {editingMode === 'deep' && (
            <>
              <ConfigField
                label={t('knowledgeSearch.maxSubqueries')}
                description={t('knowledgeSearch.maxSubqueriesDesc')}
              >
                <InputNumber
                  min={1}
                  max={20}
                  value={draftConfig.max_subqueries}
                  onChange={(v) => updateDraft({ max_subqueries: v ?? 1 })}
                  className={styles.configNumber}
                />
              </ConfigField>
              <ConfigField
                label={t('knowledgeSearch.allowCommonSense')}
                description={t('knowledgeSearch.allowCommonSenseDesc')}
              >
                <Switch
                  checked={draftConfig.allow_common_sense}
                  onChange={(v) => updateDraft({ allow_common_sense: v })}
                />
              </ConfigField>
            </>
          )}
        </div>
      </Modal>
    </Card>
  );
}
