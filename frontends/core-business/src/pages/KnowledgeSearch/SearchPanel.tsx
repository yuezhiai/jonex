import React, { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Select, Button, Switch, InputNumber, Divider, Dropdown, Modal, Radio, Tooltip, Upload } from 'antd';
import { SettingOutlined, DownOutlined, PictureOutlined, CloseOutlined } from '@ant-design/icons';
import TelegramFilled from '@/components/TelegramFilled';
import { Sender } from '@ant-design/x';
import classNames from 'classnames';
import type { AttachedImage, KnowledgeSearchDomain, KnowledgeSearchStrictConfig, KnowledgeSearchUiMode, WikiThinkingMode } from '@/types/knowledgeSearch';
import { DEFAULT_FAST_STRICT_CONFIG, DEFAULT_DEEP_STRICT_CONFIG, DEFAULT_WIKI_STRICT_CONFIG } from '@/api/knowledgeSearch';
import { canSubmitSearch } from './searchPanelUtils';
import styles from './SearchPanel.module.scss';

/** 档位元数据：label/desc 的 i18n key + 是否支持严格模式配置 */
const MODE_META: { mode: KnowledgeSearchUiMode; labelKey: string; descKey: string; configurable: boolean }[] = [
  { mode: 'fast', labelKey: 'knowledgeSearch.fastSearch', descKey: 'knowledgeSearch.fastSearchDesc', configurable: true },
  { mode: 'deep', labelKey: 'knowledgeSearch.deepSearch', descKey: 'knowledgeSearch.deepSearchDesc', configurable: true },
  { mode: 'wiki', labelKey: 'knowledgeSearch.wikiSearch', descKey: 'knowledgeSearch.wikiSearchDesc', configurable: true },
];

const WIKI_THINKING_ICONS: Record<WikiThinkingMode, string> = { fast: '⚡', standard: '◐', deep: '✦' };
const WIKI_THINKING_MODES: WikiThinkingMode[] = ['fast', 'standard', 'deep'];

const DEFAULT_CONFIG_BY_MODE: Record<KnowledgeSearchUiMode, KnowledgeSearchStrictConfig> = {
  fast: DEFAULT_FAST_STRICT_CONFIG,
  deep: DEFAULT_DEEP_STRICT_CONFIG,
  wiki: DEFAULT_WIKI_STRICT_CONFIG,
};

interface SearchPanelProps {
  selectedDomain: string;
  visibleDomains: KnowledgeSearchDomain[];
  query: string;
  isSearching: boolean;
  /** 检索模式：快速 / 深度 / Wiki（Wiki 走 /search/jonex_wiki 专用端点） */
  searchMode: KnowledgeSearchUiMode;
  onSearchModeChange: (mode: KnowledgeSearchUiMode) => void;
  wikiThinkingMode: WikiThinkingMode;
  onWikiThinkingModeChange: (mode: WikiThinkingMode) => void;
  /** 严格模式配置 */
  strictConfig: KnowledgeSearchStrictConfig;
  onStrictConfigChange: (config: KnowledgeSearchStrictConfig) => void;
  onDomainChange: (value: string) => void;
  onQueryChange: (value: string) => void;
  onSearch: () => void;
  onStop: () => void;
  // ── [jonex] 以图问知识 ──
  /** 已附的图片（压缩后的字节 + 本地预览地址）；状态由页面持有 */
  attachedImages?: AttachedImage[];
  /** 追加图片（选图 / 粘贴都会走这里；已压缩） */
  onAttachImages?: (files: File[] | Blob[]) => void;
  /** 移除第 index 张 */
  onRemoveImage?: (index: number) => void;
  /** 最多几张（后端回包里的 limits 为准，未收到时为内置默认值） */
  maxImages?: number;
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
  searchMode,
  onSearchModeChange,
  wikiThinkingMode,
  onWikiThinkingModeChange,
  onStrictConfigChange,
  onDomainChange,
  onQueryChange,
  onSearch,
  onStop,
  attachedImages = [],
  onAttachImages,
  onRemoveImage,
  maxImages = 4,
}: SearchPanelProps) {
  const { t } = useTranslation();
  const imagesFull = attachedImages.length >= maxImages;
  // 「检索」在「有文字或有图片」时可用；两者都没有就禁用
  const canSearch = canSubmitSearch(query, attachedImages.length);

  // [jonex] 各档位各自保留一份严格模式配置：切档位时把该档位的配置回传给页面（当前生效配置）
  const [modeConfigs, setModeConfigs] =
    useState<Record<KnowledgeSearchUiMode, KnowledgeSearchStrictConfig>>(DEFAULT_CONFIG_BY_MODE);
  const [modalOpen, setModalOpen] = useState(false);
  const [editingMode, setEditingMode] = useState<KnowledgeSearchUiMode>('fast');
  const [draftConfig, setDraftConfig] = useState<KnowledgeSearchStrictConfig>(DEFAULT_FAST_STRICT_CONFIG);
  const [dropdownOpen, setDropdownOpen] = useState(false);
  const [draftWikiThinking, setDraftWikiThinking] = useState<WikiThinkingMode>(wikiThinkingMode);

  const activeMeta = MODE_META.find((m) => m.mode === searchMode) ?? MODE_META[0];
  const activeModeLabel = t(activeMeta.labelKey) + (searchMode === 'wiki' ? ` ${WIKI_THINKING_ICONS[wikiThinkingMode]}` : '');
  const editingMeta = MODE_META.find((m) => m.mode === editingMode) ?? MODE_META[0];
  const editingModeLabel = t(editingMeta.labelKey);

  const switchMode = (next: KnowledgeSearchUiMode) => {
    if (next === searchMode) {
      setDropdownOpen(false);
      return;
    }
    onSearchModeChange(next);
    onStrictConfigChange(modeConfigs[next]);
    setDropdownOpen(false);
  };

  const openConfigModal = (mode: KnowledgeSearchUiMode) => {
    setEditingMode(mode);
    setDraftConfig(modeConfigs[mode]);
    setDraftWikiThinking(wikiThinkingMode);
    setModalOpen(true);
    setDropdownOpen(false);
  };

  const saveConfig = () => {
    if (editingMode === 'wiki') {
      onWikiThinkingModeChange(draftWikiThinking);
      setModalOpen(false);
      return;
    }
    setModeConfigs((prev) => ({ ...prev, [editingMode]: draftConfig }));
    if (editingMode === searchMode) {
      onStrictConfigChange(draftConfig);
    }
    setModalOpen(false);
  };

  const updateDraft = (patch: Partial<KnowledgeSearchStrictConfig>) => {
    setDraftConfig((prev) => ({ ...prev, ...patch }));
  };

  const dropdownContent = (
    <div className={styles.modeDropdown}>
      {MODE_META.map((item, index) => (
        <React.Fragment key={item.mode}>
          {index > 0 && <Divider style={{ margin: 0 }} />}
          <div className={classNames(styles.modeOption, { [styles.modeOptionActive]: searchMode === item.mode })}>
            {/* 整行是切换档位的按钮；齿轮是它的兄弟节点，避免按钮嵌套 */}
            <Button
              type="text"
              className={styles.modeOptionMain}
              onClick={() => switchMode(item.mode)}
            >
              <div className={styles.modeOptionContent}>
                <div className={styles.modeOptionTitle}>{t(item.labelKey)}{item.mode === 'wiki' && ` ${WIKI_THINKING_ICONS[wikiThinkingMode]}`}</div>
                <div className={styles.modeOptionDesc}>{t(item.descKey)}</div>
              </div>
            </Button>
            {item.configurable && (
              <Button
                type="text"
                size="small"
                icon={<SettingOutlined />}
                className={styles.modeOptionSetting}
                onClick={(e) => {
                  e.stopPropagation();
                  openConfigModal(item.mode);
                }}
                title={t(item.mode === 'wiki' ? 'knowledgeSearch.wikiThinkingTitle' : 'knowledgeSearch.strictConfigTitle')}
              />
            )}
          </div>
        </React.Fragment>
      ))}
    </div>
  );

  return (
    <>
    <div className={styles.dock}>
      <div className={styles.composer}>
      {/* [jonex] 以图问知识：已附图片的缩略图（只在输入框上方做本地预览，不发请求、不显示识别状态） */}
      {attachedImages.length > 0 && (
        <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap', marginBottom: 12 }}>
          {attachedImages.map((image, index) => (
            <div
              key={`${image.objectUrl}-${index}`}
              style={{ position: 'relative', width: 56, height: 56, borderRadius: 8, overflow: 'hidden', border: '1px solid #e2e8f0' }}
            >
              <img src={image.objectUrl} alt={image.name} style={{ width: '100%', height: '100%', objectFit: 'cover' }} />
              <Button
                type="text"
                size="small"
                icon={<CloseOutlined style={{ fontSize: 10 }} />}
                onClick={() => onRemoveImage?.(index)}
                aria-label={t('knowledgeSearch.removeImage')}
                style={{ position: 'absolute', top: 0, right: 0, width: 18, height: 18, minWidth: 18, padding: 0, background: 'rgba(15,23,42,.6)', color: '#fff', borderRadius: 0 }}
              />
            </div>
          ))}
          <span style={{ fontSize: 12, color: '#94a3b8' }}>
            {t('knowledgeSearch.attachedCount', { count: attachedImages.length, total: maxImages })}
          </span>
        </div>
      )}

      <Sender
        value={query}
        onChange={(value) => onQueryChange(value)}
        // Sender 的 `disabled` 会连输入一起禁掉 —— 空输入时禁用就等于开不了头，
        // 所以闸门放在提交这一步：没有文字也没有图片时不触发检索。
        onSubmit={() => {
          if (canSearch) onSearch();
        }}
        loading={isSearching}
        // 检索期间锁住输入：这一轮的 query 已随请求发出，改输入框只会让人以为改的是正在跑的这轮
        readOnly={isSearching}
        onCancel={onStop}
        submitType="enter"
        placeholder={attachedImages.length > 0 ? t('knowledgeSearch.searchPlaceholderWithImage') : t('knowledgeSearch.searchPlaceholder')}
        autoSize={{ minRows: 1, maxRows: 5 }}
        // 外层的 .composer 已经是卡片本体，Sender 再画一层边框就成了「框里套框」
        styles={{
          root: { border: 'none', boxShadow: 'none', background: 'transparent' },
          footer: { borderTop: '1px solid #f1f5f9', marginTop: 6, paddingTop: 8 },
        }}
        // 剪贴板里同时有图片和文字时（从 Word 复制），文字照常进输入框、图片顺手追加（F3）
        onPasteFile={isSearching ? undefined : (files) => onAttachImages?.(Array.from(files))}
        // 默认动作栏就挂在 suffix 槽（Sender.js `suffixNode = actionNode`），这里把它换成
        // 自己控制禁用条件的版本：内置 SendButton 只要 value 为空就置灰，而「以图问知识」
        // 本来就允许**只有图片没有文字**，照内置逻辑那条路会被堵死。
        // ActionButton 取 `restProps.disabled ?? ...`，显式传入即可覆盖内置判据。
        suffix={(_actionNode, { components }) =>
          // SendButton 把 icon 放在 props 之前，显式传 icon 即可覆盖内置的 ArrowUpOutlined
          isSearching ? <components.LoadingButton /> : <components.SendButton disabled={!canSearch} icon={<TelegramFilled />} />
        }
        // 工具条放到输入区下方（主流对话输入框的布局）：文字输入占据最显眼的位置，
        // 领域与档位作为辅助控件退到次一级，只用文字按钮表达。
        footer={
          <div className={styles.toolbar}>
            <Select
              value={selectedDomain}
              onChange={onDomainChange}
              size="small"
              variant="borderless"
              className={styles.toolbarSelect}
              popupMatchSelectWidth={220}
              options={visibleDomains.map((d) => ({
                value: d.id,
                // 当前领域空间（kind=space）为返回首项，label 固定显示「当前领域空间」
                label: d.kind === 'space' ? t('knowledgeSearch.currentSpace') : d.name,
              }))}
            />
            <span className={styles.toolbarDivider} />
            {/* [jonex] 档位由「检索按钮 + 下拉」按钮组降为独立下拉：提交与停止已交给
                Sender 自带的发送按钮，档位只表达「用哪一档」。各档位的齿轮仍在下拉项里，
                点击打开该档位自己的严格配置弹窗。 */}
            <Dropdown
              open={dropdownOpen}
              onOpenChange={setDropdownOpen}
              trigger={['click']}
              placement="topRight"
              menu={{ items: [] }}
              popupRender={() => dropdownContent}
              disabled={isSearching}
            >
              <Button type="text" size="small" icon={<DownOutlined />} iconPlacement="end">
                {activeModeLabel}
              </Button>
            </Dropdown>
            <span className={styles.toolbarHint}>{t('knowledgeSearch.enterToSend')}</span>
          </div>
        }
        prefix={
          // 图片按钮放在输入框**内部**（D7）：点选图（可多选）；也可直接 Ctrl+V 粘贴。
          // `beforeUpload` 返回 false 表示「自己处理，不上传」——这里只收集文件，
          // 真正上传（连文字一起）发生在点「检索」之后。
          <Upload
            multiple
            // ⚠️ 只禁用里面那个 Button 不够：Upload 自己仍可被键盘（Enter/Space 聚焦后）
            // 唤起选图框。要挡住得禁 Upload 本身。
            disabled={imagesFull || isSearching}
            accept="image/jpeg,image/png,image/webp,image/gif,image/bmp"
            showUploadList={false}
            beforeUpload={(_file, fileList) => {
              // ⚠️ antd 的 beforeUpload 对多选会**每个文件各调一次**，
              // 只在最后一个（fileList 末尾）处理整批，否则同一批会被追加 N 次。
              if (fileList[fileList.length - 1] === _file) onAttachImages?.(fileList as File[]);
              return false;
            }}
          >
            <Tooltip title={imagesFull ? t('knowledgeSearch.imageLimitReached', { count: maxImages }) : t('knowledgeSearch.uploadImage')}>
              <Button
                type="text"
                size="small"
                icon={<PictureOutlined style={{ fontSize: 16, color: imagesFull || isSearching ? '#cbd5e1' : '#64748b' }} />}
                disabled={imagesFull || isSearching}
                aria-label={t('knowledgeSearch.uploadImage')}
              />
            </Tooltip>
          </Upload>
        }
      />

      <Modal
        title={`${editingModeLabel} · ${t(editingMode === 'wiki' ? 'knowledgeSearch.wikiThinkingTitle' : 'knowledgeSearch.strictConfigTitle')}`}
        open={modalOpen}
        onOk={saveConfig}
        onCancel={() => setModalOpen(false)}
        okText={t('knowledgeSearch.saveSettings')}
        cancelText={t('knowledgeSearch.cancel')}
        width={560}
      >
        {editingMode === 'wiki' ? (
          <Radio.Group value={draftWikiThinking} onChange={(event) => setDraftWikiThinking(event.target.value)} style={{ width: '100%' }}>
            {WIKI_THINKING_MODES.map((mode) => (
              <div key={mode} style={{ padding: '12px 0' }}>
                <Radio value={mode}>{WIKI_THINKING_ICONS[mode]} {mode === 'fast' ? 'Fast' : mode === 'standard' ? 'Standard' : 'Deep'}</Radio>
                <div className={styles.configFieldDesc} style={{ marginLeft: 24, marginTop: 6 }}>{t(`knowledgeSearch.wikiThinking${mode[0].toUpperCase()}${mode.slice(1)}Desc`)}</div>
              </div>
            ))}
          </Radio.Group>
        ) : (<>
        <div className={styles.configIntro}>{t('knowledgeSearch.strictConfigDescription')}</div>
        <div className={styles.configForm}>
          <ConfigField
            label={t('knowledgeSearch.strictMode')}
            description={t('knowledgeSearch.strictModeDesc')}
          >
            <Switch checked={draftConfig.strict_mode} onChange={(v) => updateDraft({ strict_mode: v })} />
          </ConfigField>
          {draftConfig.strict_mode && (
            <>
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
            </>
          )}
        </div>
        </>)}
      </Modal>
      </div>
    </div>
    </>
  );
}
