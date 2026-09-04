import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useParams } from 'react-router-dom';
import { Button, Input, Select, Table, Typography } from 'antd';
import { useStore } from '@/store';
import {
  AudioFilled,
  CloseOutlined,
  DownOutlined,
  ExclamationCircleFilled,
  FileImageFilled,
  FileTextOutlined,
  FileWordFilled,
  FilterFilled,
  PlusOutlined,
  SaveFilled,
  VideoCameraFilled,
} from '@ant-design/icons';
import {
  createParserSetting,
  deleteParserSetting,
  getParserConfigs,
  getParserSettings,
  listPromptTemplates,
  updateParserSetting,
  type ParserConfigItem,
  type ParserSettingItem,
  type ParserSettingPayload,
  type PromptTemplateListItem,
} from '@/api/domainKnowledge';
import './index.css';

interface ParserConfigRow {
  id: string;
  parserType: string;
  label: string;
  extensions: string[];
  parserConfigId: string;
  parserName: string;
  preprocessing: string[];
  postprocessing: string[];
  prompt: string;
  promptTemplateId?: string | null;
  promptTemplateVersion?: string | null;
  summaryPrompt: string;
  summaryTemplateId?: string | null;
  summaryTemplateVersion?: string | null;
  tagPrompt: string;
  tagTemplateId?: string | null;
  tagTemplateVersion?: string | null;
}

interface ParserFormState {
  parserType: string;
  parserConfigId: string;
  prompt: string;
  promptTemplateId?: string | null;
  promptTemplateVersion?: string | null;
  preprocessing: string[];
  postSummary: boolean;
  postTag: boolean;
  summaryPrompt: string;
  summaryTemplateId?: string | null;
  summaryTemplateVersion?: string | null;
  tagPrompt: string;
  tagTemplateId?: string | null;
  tagTemplateVersion?: string | null;
}

const PARSER_TYPE_LOCALE_KEYS: Record<string, string> = {
  document: 'parserConfig.parserType.document',
  txt: 'parserConfig.parserType.txt',
  image: 'parserConfig.parserType.image',
  audio: 'parserConfig.parserType.audio',
  video: 'parserConfig.parserType.video',
  web: 'parserConfig.parserType.web',
  cad: 'parserConfig.parserType.cad',
};

const PARSER_TYPE_ORDER = ['document', 'txt', 'image', 'audio', 'video', 'web', 'cad'];

const BUILT_IN_PARSER_NAME_KEYS: Record<string, string> = {
  video_full_pipeline: 'parserConfig.parserName.video',
  parser_demo_video: 'parserConfig.parserName.video',
  audio_transcribe: 'parserConfig.parserName.audio',
  parser_demo_audio: 'parserConfig.parserName.audio',
  image_parse: 'parserConfig.parserName.image',
  parser_demo_image: 'parserConfig.parserName.image',
  document_parse: 'parserConfig.parserName.document',
  parser_demo_document: 'parserConfig.parserName.document',
  text_parse: 'parserConfig.parserName.text',
  parser_demo_text: 'parserConfig.parserName.text',
  parser_demo_web: 'parserConfig.parserName.web',
  parser_demo_cad: 'parserConfig.parserName.cad',
};

function parserConfigDisplayName(id: string, fallback: string, t: (key: string) => string): string {
  const key = BUILT_IN_PARSER_NAME_KEYS[id];
  return key ? t(key) : fallback;
}

function parserTypeLabel(parserType: string, t: (key: string) => string): string {
  return PARSER_TYPE_LOCALE_KEYS[parserType]
    ? t(PARSER_TYPE_LOCALE_KEYS[parserType])
    : parserType
      ? parserType.toUpperCase()
      : t('parserConfig.parserType.unknown');
}

function parserTypeSortIndex(parserType: string): number {
  const idx = PARSER_TYPE_ORDER.indexOf(parserType);
  return idx < 0 ? PARSER_TYPE_ORDER.length : idx;
}

/** 后缀集合归一为大写、去点、去重、排序，用于只读展示 */
function normalizeExtensions(fileTypes?: string[]): string[] {
  const set = new Set<string>();
  (fileTypes || []).forEach((ft) => {
    const v = String(ft).trim().replace(/^\.+/, '').toUpperCase();
    if (v) set.add(v);
  });
  return Array.from(set).sort();
}

// 解析器类目候选项（由 active 解析器按 parser_type 聚合而来）
interface CategoryOption {
  parserType: string;
  label: string;
  extensions: string[];
}

type TemplateTarget = 'prompt' | 'summaryPrompt' | 'tagPrompt';

interface ParserConfigContentProps {
  kbId?: string;
  spaceId?: string | null;
  /** KB 写权限（权限矩阵） */
  canWrite?: boolean;
}

const preprocessOptions = [
  '大文档预处理',
  '长文档预处理',
  'Excel多Sheet处理',
  'HTML Base64媒体处理',
  '视频质量检测',
  '视频缩帧处理',
  '长视频分段处理',
];

const emptyForm: ParserFormState = {
  parserType: '',
  parserConfigId: '',
  prompt: '',
  promptTemplateId: null,
  promptTemplateVersion: null,
  preprocessing: [],
  postSummary: false,
  postTag: false,
  summaryPrompt: '',
  summaryTemplateId: null,
  summaryTemplateVersion: null,
  tagPrompt: '',
  tagTemplateId: null,
  tagTemplateVersion: null,
};

function getErrorMessage(error: unknown, t: (key: string) => string): string {
  if (error instanceof Error) return error.message;
  return t('parserConfig.requestFailed');
}

function renderFileIcon(parserType: string): React.ReactNode {
  switch (parserType) {
    case 'document':
      return <FileWordFilled />;
    case 'image':
      return <FileImageFilled />;
    case 'audio':
      return <AudioFilled />;
    case 'video':
      return <VideoCameraFilled />;
    default:
      return <FileTextOutlined />;
  }
}

function getIconClass(parserType: string): string {
  switch (parserType) {
    case 'document':
      return 'word';
    case 'image':
      return 'image';
    case 'audio':
      return 'audio';
    case 'video':
      return 'video';
    case 'cad':
      return 'cad';
    default:
      return 'text';
  }
}

// 后处理选项在 DB 中以固定中文值存储，这些值同时用作展示标识
function buildPostprocessing(form: ParserFormState): string[] {
  const postprocessing: string[] = [];
  if (form.postSummary) postprocessing.push('自动摘要');
  if (form.postTag) postprocessing.push('自动标签');
  return postprocessing;
}

function toList(value: unknown): string[] {
  return Array.isArray(value) ? value.map(String) : [];
}

function mapSettingToRow(item: ParserSettingItem, tFunc: (key: string) => string): ParserConfigRow {
  const postprocessing = toList(item.postprocessing_json);
  return {
    id: item.id,
    parserType: item.parser_type,
    label: parserTypeLabel(item.parser_type, tFunc),
    extensions: normalizeExtensions(item.parser_file_types),
    parserConfigId: item.parser_config_id || '',
    parserName: parserConfigDisplayName(item.parser_config_id || '', item.parser_name || '', tFunc),
    preprocessing: toList(item.preprocessing_json),
    postprocessing,
    prompt: item.prompt_text || '',
    promptTemplateId: item.prompt_template_id,
    promptTemplateVersion: item.prompt_template_version,
    summaryPrompt: item.summary_prompt_text || '',
    summaryTemplateId: item.summary_template_id,
    summaryTemplateVersion: item.summary_template_version,
    tagPrompt: item.tag_prompt_text || '',
    tagTemplateId: item.tag_template_id,
    tagTemplateVersion: item.tag_template_version,
  };
}

// 后处理选项在 DB 中以固定中文值存储，includes 使用原始值进行比较
function rowToForm(row: ParserConfigRow): ParserFormState {
  return {
    parserType: row.parserType,
    parserConfigId: row.parserConfigId,
    prompt: row.prompt,
    promptTemplateId: row.promptTemplateId,
    promptTemplateVersion: row.promptTemplateVersion,
    preprocessing: row.preprocessing,
    postSummary: row.postprocessing.includes('自动摘要'),
    postTag: row.postprocessing.includes('自动标签'),
    summaryPrompt: row.summaryPrompt,
    summaryTemplateId: row.summaryTemplateId,
    summaryTemplateVersion: row.summaryTemplateVersion,
    tagPrompt: row.tagPrompt,
    tagTemplateId: row.tagTemplateId,
    tagTemplateVersion: row.tagTemplateVersion,
  };
}

const PREPROCESS_DISPLAY_KEYS: Record<string, string> = {
  大文档预处理: 'parserConfig.preprocess.largeDoc',
  长文档预处理: 'parserConfig.preprocess.longDoc',
  Excel多Sheet处理: 'parserConfig.preprocess.excelMultiSheet',
  'HTML Base64媒体处理': 'parserConfig.preprocess.htmlBase64',
  视频质量检测: 'parserConfig.preprocess.videoQuality',
  视频缩帧处理: 'parserConfig.preprocess.videoThumbnail',
  长视频分段处理: 'parserConfig.preprocess.videoSegmentation',
};

const POSTPROCESS_DISPLAY_KEYS: Record<string, string> = {
  自动摘要: 'parserConfig.postSummary',
  自动标签: 'parserConfig.postTag',
};

function getDisplayKey(type: 'pre' | 'post'): Record<string, string> {
  return type === 'pre' ? PREPROCESS_DISPLAY_KEYS : POSTPROCESS_DISPLAY_KEYS;
}

function renderTags(items: string[], type: 'pre' | 'post', t: (key: string) => string) {
  if (items.length === 0) {
    return <span className="parser-config-muted">{t('parserConfig.notConfigured')}</span>;
  }

  const displayMap = getDisplayKey(type);

  return (
    <div className="parser-config-tags">
      {items.map((item) => (
        <span key={item} className={`parser-config-tag ${type}`}>
          {t(displayMap[item] || item)}
        </span>
      ))}
    </div>
  );
}

function templateContent(template: PromptTemplateListItem): string {
  const versions = template.versions_json || [];
  return versions.find((item) => item.version === template.current_version)?.content || versions[0]?.content || '';
}

function templateDesc(template: PromptTemplateListItem, t: (key: string) => string): string {
  return (
    template.description ||
    `${template.category} · ${template.scope === 'system' ? t('parserConfig.systemTemplate') : t('parserConfig.domainTemplate')}`
  );
}

export function ParserConfigContent({ kbId, spaceId, canWrite = false }: ParserConfigContentProps) {
  const { t } = useTranslation();
  const params = useParams<{ id: string }>();
  const resolvedKbId = kbId || params.id || '';

  const [rows, setRows] = useState<ParserConfigRow[]>([]);
  const [parsers, setParsers] = useState<ParserConfigItem[]>([]);
  const [templates, setTemplates] = useState<PromptTemplateListItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [templateLoading, setTemplateLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [pageError, setPageError] = useState('');
  const [templateError, setTemplateError] = useState('');
  const [modalOpen, setModalOpen] = useState(false);
  const [mode, setMode] = useState<'add' | 'edit'>('add');
  const [editingId, setEditingId] = useState<string | null>(null);
  const [form, setForm] = useState<ParserFormState>(emptyForm);
  const [formError, setFormError] = useState('');
  const [deleteTarget, setDeleteTarget] = useState<ParserConfigRow | null>(null);
  const [templateOpen, setTemplateOpen] = useState(false);
  const [templateTarget, setTemplateTarget] = useState<TemplateTarget>('prompt');
  const [notice, setNotice] = useState('');

  // 解析器类目候选：由 active 解析器按 parser_type 聚合，后缀取该类目下解析器 file_types 的并集
  const categoryOptions = useMemo<CategoryOption[]>(() => {
    const extMap = new Map<string, Set<string>>();
    parsers
      .filter((parser) => parser.status === 'active')
      .forEach((parser) => {
        const set = extMap.get(parser.parser_type) || new Set<string>();
        normalizeExtensions(parser.file_types).forEach((ext) => set.add(ext));
        extMap.set(parser.parser_type, set);
      });
    return Array.from(extMap.entries())
      .map(([parserType, exts]) => ({
        parserType,
        label: parserTypeLabel(parserType, t),
        extensions: Array.from(exts).sort(),
      }))
      .sort((a, b) => parserTypeSortIndex(a.parserType) - parserTypeSortIndex(b.parserType));
  }, [parsers]);

  // 关联解析器候选：该类目下的 active 解析器
  const parsersForType = useCallback(
    (parserType: string): ParserConfigItem[] =>
      parsers
        .filter((parser) => parser.status === 'active' && parser.parser_type === parserType)
        .sort((a, b) =>
          parserConfigDisplayName(a.id, a.name, t).localeCompare(parserConfigDisplayName(b.id, b.name, t)),
        ),
    [parsers, t],
  );

  const availableParsers = useMemo(
    () => (form.parserType ? parsersForType(form.parserType) : []),
    [parsersForType, form.parserType],
  );

  // 新增模式下，隐藏本 KB 已配置过的类目；编辑模式保留全部（含当前行类目）
  const selectableCategories = useMemo<CategoryOption[]>(() => {
    if (mode === 'edit') return categoryOptions;
    const used = new Set(rows.map((row) => row.parserType));
    return categoryOptions.filter((item) => !used.has(item.parserType));
  }, [categoryOptions, rows, mode]);

  const selectedCategory = useMemo(
    () => categoryOptions.find((item) => item.parserType === form.parserType),
    [categoryOptions, form.parserType],
  );

  const parserNameMap = useMemo(() => {
    const map = new Map<string, string>();
    parsers.forEach((parser) => map.set(parser.id, parser.name));
    return map;
  }, [parsers]);

  const loadData = useCallback(async () => {
    if (!resolvedKbId) {
      setPageError(t('parserConfig.missingKbIdLoad'));
      return;
    }

    setLoading(true);
    setPageError('');
    try {
      const [parserResp, settingsResp] = await Promise.all([getParserConfigs(0, 100), getParserSettings(resolvedKbId)]);
      setParsers(parserResp.items || []);
      setRows((settingsResp.items || []).map((item) => mapSettingToRow(item, t)));
    } catch (error) {
      setPageError(getErrorMessage(error, t));
    } finally {
      setLoading(false);
    }
  }, [resolvedKbId]);

  const loadTemplates = useCallback(async () => {
    setTemplateLoading(true);
    setTemplateError('');
    try {
      const params: { offset: number; limit: number; domain_space_id?: string } = { offset: 0, limit: 100 };
      if (spaceId) {
        params.domain_space_id = spaceId;
      }
      const resp = await listPromptTemplates(params);
      setTemplates(resp.items || []);
    } catch (error) {
      setTemplateError(getErrorMessage(error, t));
    } finally {
      setTemplateLoading(false);
    }
  }, [spaceId]);

  useEffect(() => {
    void loadData();
  }, [loadData]);

  const chooseDefaultParser = (parserType: string): string => {
    const matches = parsersForType(parserType);
    return matches[0]?.id || '';
  };

  const openAddModal = () => {
    setMode('add');
    setEditingId(null);
    setForm(emptyForm);
    setFormError('');

    setModalOpen(true);
  };

  const openEditModal = (row: ParserConfigRow) => {
    setMode('edit');
    setEditingId(row.id);
    setForm(rowToForm(row));
    setFormError('');

    setModalOpen(true);
  };

  const closeFormModal = () => {
    setModalOpen(false);

    setFormError('');
  };

  const updateForm = <K extends keyof ParserFormState>(key: K, value: ParserFormState[K]) => {
    setForm((prev) => ({ ...prev, [key]: value }));
    setFormError('');
  };

  const updateCategory = (parserType: string) => {
    setForm((prev) => {
      // 切换类目后，若已选解析器不属于新类目，则重置为新类目下的默认解析器
      const stillValid = prev.parserConfigId
        ? parsersForType(parserType).some((parser) => parser.id === prev.parserConfigId)
        : false;
      return {
        ...prev,
        parserType,
        parserConfigId: stillValid ? prev.parserConfigId : chooseDefaultParser(parserType),
      };
    });
    setFormError('');
  };

  const buildPayload = (): ParserSettingPayload => ({
    knowledge_base_id: resolvedKbId,
    parser_type: form.parserType,
    parser_config_id: form.parserConfigId,
    preprocessing_json: form.preprocessing,
    postprocessing_json: buildPostprocessing(form),
    prompt_text: form.prompt,
    prompt_template_id: form.promptTemplateId || null,
    prompt_template_version: form.promptTemplateVersion || null,
    summary_prompt_text: form.summaryPrompt,
    summary_template_id: form.summaryTemplateId || null,
    summary_template_version: form.summaryTemplateVersion || null,
    tag_prompt_text: form.tagPrompt,
    tag_template_id: form.tagTemplateId || null,
    tag_template_version: form.tagTemplateVersion || null,
    status: 'active',
  });

  const saveForm = async () => {
    if (!resolvedKbId) {
      setFormError(t('parserConfig.missingKbIdSave'));
      return;
    }
    if (!form.parserType || !form.parserConfigId) {
      setFormError(t('parserConfig.selectRequired'));
      return;
    }

    const duplicate = rows.some((row) => row.parserType === form.parserType && row.id !== editingId);
    if (duplicate) {
      setFormError(t('parserConfig.duplicateType'));
      return;
    }

    setSaving(true);
    setFormError('');
    try {
      const payload = buildPayload();
      const saved =
        mode === 'edit' && editingId
          ? await updateParserSetting(editingId, payload)
          : await createParserSetting(payload);
      const label = parserTypeLabel(form.parserType, t);
      if (mode === 'edit') {
        const nextRow = mapSettingToRow(saved, t);
        setRows((prev) => prev.map((row) => (row.id === nextRow.id ? nextRow : row)));
      } else {
        // 添加成功后不本地插入数据，改为重新请求列表接口，保证与后端一致
        await loadData();
      }
      setNotice(mode === 'edit' ? t('parserConfig.savedLabel', { label }) : t('parserConfig.addedLabel', { label }));
      closeFormModal();
    } catch (error) {
      setFormError(getErrorMessage(error, t));
    } finally {
      setSaving(false);
    }
  };

  const confirmDelete = async () => {
    if (!deleteTarget) return;
    setSaving(true);
    try {
      await deleteParserSetting(deleteTarget.id);
      setRows((prev) => prev.filter((row) => row.id !== deleteTarget.id));
      setNotice(t('parserConfig.deletedLabel', { label: deleteTarget.label }));
      setDeleteTarget(null);
    } catch (error) {
      setNotice(getErrorMessage(error, t));
    } finally {
      setSaving(false);
    }
  };

  const openTemplateModal = (target: TemplateTarget) => {
    setTemplateTarget(target);
    setTemplateOpen(true);
    // SDD: 每次打开弹窗都重新加载模板，确保空间切换后数据刷新
    void loadTemplates();
  };

  const applyTemplate = (template: PromptTemplateListItem) => {
    const content = templateContent(template);
    setForm((prev) => {
      if (templateTarget === 'summaryPrompt') {
        return {
          ...prev,
          summaryPrompt: content,
          summaryTemplateId: template.id,
          summaryTemplateVersion: template.current_version,
        };
      }
      if (templateTarget === 'tagPrompt') {
        return {
          ...prev,
          tagPrompt: content,
          tagTemplateId: template.id,
          tagTemplateVersion: template.current_version,
        };
      }
      return {
        ...prev,
        prompt: content,
        promptTemplateId: template.id,
        promptTemplateVersion: template.current_version,
      };
    });
    setTemplateOpen(false);
  };

  return (
    <>
      <section className="parser-config-panel">
        <div className="parser-config-header">
          <div>
            <h1>
              <FilterFilled />
              {t('parserConfig.title')}
            </h1>
          </div>
          <Button
            type="primary"
            className="parser-config-add"
            onClick={openAddModal}
            disabled={loading || !resolvedKbId || !canWrite}
          >
            <PlusOutlined />
            {t('parserConfig.addSetting')}
          </Button>
        </div>

        {notice && (
          <div className="parser-config-notice">
            <span>{notice}</span>
            <Button type="text" onClick={() => setNotice('')} aria-label={t('parserConfig.closeNotice')}>
              <CloseOutlined />
            </Button>
          </div>
        )}

        {pageError && (
          <div className="parser-config-error">
            <span>{pageError}</span>
            <Button type="text" onClick={() => void loadData()}>
              {t('common.retry')}
            </Button>
          </div>
        )}

        <Table<ParserConfigRow>
          columns={[
            {
              title: t('parserConfig.tableHeaderType'),
              dataIndex: 'label',
              key: 'type',
              width: 220,
              render: (_: unknown, record: ParserConfigRow) => (
                <div className="parser-config-filetype">
                  <span className={`parser-file-icon ${getIconClass(record.parserType)}`}>
                    {renderFileIcon(record.parserType)}
                  </span>
                  <div className="parser-config-filetype-meta">
                    <span>{record.label}</span>
                    {record.extensions.length > 0 && (
                      <span className="parser-config-muted">
                        {record.extensions.map((ext) => `.${ext.toLowerCase()}`).join(' ')}
                      </span>
                    )}
                  </div>
                </div>
              ),
            },
            {
              title: t('parserConfig.tableHeaderParser'),
              dataIndex: 'parserConfigId',
              key: 'parser',
              width: 180,
              render: (val: string, record: ParserConfigRow) =>
                record.parserName || parserNameMap.get(val) || t('parserConfig.notLinkedParser'),
            },
            {
              title: t('parserConfig.tableHeaderPre'),
              dataIndex: 'preprocessing',
              key: 'pre',
              render: (val: string[]) => renderTags(val, 'pre', t),
            },
            {
              title: t('parserConfig.tableHeaderPost'),
              dataIndex: 'postprocessing',
              key: 'post',
              render: (val: string[]) => renderTags(val, 'post', t),
            },
            {
              title: t('parserConfig.tableHeaderActions'),
              key: 'actions',
              width: 180,
              render: (_: unknown, record: ParserConfigRow) => (
                <div className="parser-config-actions">
                  <Button
                    type="text"
                    disabled={!canWrite}
                    title={canWrite ? undefined : t('domainSpace.noManagePermission')}
                    onClick={() => openEditModal(record)}
                  >
                    {t('parserConfig.settingsButton')}
                  </Button>
                  <span />
                  <Button
                    type="text"
                    danger
                    disabled={!canWrite}
                    title={canWrite ? undefined : t('domainSpace.noManagePermission')}
                    onClick={() => setDeleteTarget(record)}
                  >
                    {t('common.delete')}
                  </Button>
                </div>
              ),
            },
          ]}
          dataSource={rows}
          rowKey="id"
          pagination={false}
          size="middle"
          loading={loading}
          locale={{ emptyText: t('parserConfig.empty') }}
        />
      </section>

      {modalOpen && (
        <div className="parser-modal-mask">
          <div className="parser-modal" role="dialog" aria-modal="true" aria-labelledby="parser-modal-title">
            <div className="parser-modal-header">
              <h2 id="parser-modal-title">
                {mode === 'edit' ? t('parserConfig.editSetting') : t('parserConfig.addSetting')}
              </h2>
              <Button
                type="text"
                className="parser-modal-close"
                onClick={closeFormModal}
                aria-label={t('common.close')}
              >
                <CloseOutlined />
              </Button>
            </div>

            <div className="parser-modal-body">
              {formError && <div className="parser-form-error">{formError}</div>}

              <div className="parser-form-row">
                <label htmlFor="parser-type">
                  {t('parserConfig.tableHeaderType')} <em>*</em>
                </label>
                <Select
                  id="parser-type"
                  className="parser-form-control parser-select-control"
                  value={form.parserType}
                  onChange={(value) => updateCategory(value)}
                  disabled={mode === 'edit'}
                  getPopupContainer={(trigger) => trigger.parentNode as HTMLElement}
                  options={[
                    { value: '', label: t('parserConfig.selectParserType') },
                    ...selectableCategories.map((item) => ({
                      value: item.parserType,
                      label:
                        item.extensions.length > 0
                          ? `${item.label}（${item.extensions.map((ext) => `.${ext.toLowerCase()}`).join(' ')}）`
                          : item.label,
                    })),
                  ]}
                />
                {selectedCategory && selectedCategory.extensions.length > 0 && (
                  <span className="parser-config-muted">
                    {t('parserConfig.supportedExtensions')}
                    {selectedCategory.extensions.map((ext) => `.${ext.toLowerCase()}`).join(' ')}
                  </span>
                )}
              </div>

              <div className="parser-form-row">
                <label htmlFor="parser-engine">
                  {t('parserConfig.tableHeaderParser')} <em>*</em>
                </label>
                <Select
                  id="parser-engine"
                  className="parser-form-control parser-select-control"
                  value={form.parserConfigId}
                  onChange={(value) => updateForm('parserConfigId', value)}
                  getPopupContainer={(trigger) => trigger.parentNode as HTMLElement}
                  options={[
                    {
                      value: '',
                      label: form.parserType ? t('parserConfig.selectParser') : t('parserConfig.selectParserTypeFirst'),
                    },
                    ...availableParsers.map((parser) => ({
                      value: parser.id,
                      label: parserConfigDisplayName(parser.id, parser.name, t),
                    })),
                  ]}
                />
              </div>

              <div className="parser-form-row">
                <label htmlFor="parser-prompt">
                  {t('parserConfig.prompt')}
                  <Button
                    type="text"
                    size="small"
                    className="parser-template-button"
                    onClick={() => openTemplateModal('prompt')}
                  >
                    <FileTextOutlined />
                    {t('parserConfig.fromTemplate')}
                  </Button>
                </label>
                <Input.TextArea
                  id="parser-prompt"
                  className="parser-form-control parser-textarea"
                  value={form.prompt}
                  onChange={(event) => updateForm('prompt', event.target.value)}
                  placeholder={t('parserConfig.promptPlaceholder')}
                />
              </div>

              <div className="parser-form-row">
                <label>
                  {t('parserConfig.preprocessing')}
                  <span>{t('parserConfig.preprocessingHint')}</span>
                </label>
                <Select
                  mode="multiple"
                  style={{ width: '100%' }}
                  placeholder={t('parserConfig.clickSelect')}
                  value={form.preprocessing}
                  onChange={(values) => setForm((prev) => ({ ...prev, preprocessing: values }))}
                  options={preprocessOptions.map((item) => ({
                    value: item,
                    label: t(PREPROCESS_DISPLAY_KEYS[item] || item),
                  }))}
                />
              </div>

              <div className="parser-modal-divider" />

              <div className="parser-form-row parser-post-row">
                <label>
                  {t('parserConfig.postprocessing')}
                  <span>{t('parserConfig.postprocessingHint')}</span>
                </label>
                <Button
                  className={`parser-checkbox ${form.postSummary ? 'is-checked' : ''}`}
                  onClick={() => updateForm('postSummary', !form.postSummary)}
                >
                  <span className="parser-checkbox-box" />
                  {t('parserConfig.postSummary')}
                </Button>
                {form.postSummary && (
                  <div className="parser-post-field">
                    <label htmlFor="parser-summary-prompt">
                      {t('parserConfig.summaryPrompt')}
                      <Button
                        type="text"
                        size="small"
                        className="parser-template-button"
                        onClick={() => openTemplateModal('summaryPrompt')}
                      >
                        <FileTextOutlined />
                        {t('parserConfig.fromTemplate')}
                      </Button>
                    </label>
                    <Input.TextArea
                      id="parser-summary-prompt"
                      className="parser-form-control parser-mini-textarea"
                      value={form.summaryPrompt}
                      onChange={(event) => updateForm('summaryPrompt', event.target.value)}
                      placeholder={t('parserConfig.summaryPromptPlaceholder')}
                    />
                  </div>
                )}
                <Button
                  className={`parser-checkbox ${form.postTag ? 'is-checked' : ''}`}
                  onClick={() => updateForm('postTag', !form.postTag)}
                >
                  <span className="parser-checkbox-box" />
                  {t('parserConfig.postTag')}
                </Button>
                {form.postTag && (
                  <div className="parser-post-field">
                    <label htmlFor="parser-tag-prompt">
                      {t('parserConfig.tagPrompt')}
                      <Button
                        type="text"
                        size="small"
                        className="parser-template-button"
                        onClick={() => openTemplateModal('tagPrompt')}
                      >
                        <FileTextOutlined />
                        {t('parserConfig.fromTemplate')}
                      </Button>
                    </label>
                    <Input.TextArea
                      id="parser-tag-prompt"
                      className="parser-form-control parser-mini-textarea"
                      value={form.tagPrompt}
                      onChange={(event) => updateForm('tagPrompt', event.target.value)}
                      placeholder={t('parserConfig.tagPromptPlaceholder')}
                    />
                  </div>
                )}
              </div>
            </div>

            <div className="parser-modal-footer">
              <Button className="parser-modal-cancel" onClick={closeFormModal}>
                {t('common.cancel')}
              </Button>
              <Button
                type="primary"
                className="parser-modal-save"
                onClick={() => void saveForm()}
                disabled={saving || !canWrite}
                title={canWrite ? undefined : t('domainSpace.noManagePermission')}
              >
                <SaveFilled />
                {saving ? t('parserConfig.saving') : t('common.save')}
              </Button>
            </div>
          </div>
        </div>
      )}

      {deleteTarget && (
        <div className="parser-modal-mask">
          <div className="parser-modal parser-delete-modal" role="dialog" aria-modal="true">
            <div className="parser-delete-body">
              <div className="parser-delete-icon">
                <ExclamationCircleFilled />
              </div>
              <strong>{deleteTarget.label}</strong>
              <p>{t('parserConfig.deleteConfirmMessage')}</p>
            </div>
            <div className="parser-modal-footer parser-delete-footer">
              <Button className="parser-modal-cancel" onClick={() => setDeleteTarget(null)}>
                {t('common.cancel')}
              </Button>
              <Button
                danger
                className="parser-modal-danger"
                onClick={() => void confirmDelete()}
                disabled={saving || !canWrite}
                title={canWrite ? undefined : t('domainSpace.noManagePermission')}
              >
                {t('common.confirmDelete')}
              </Button>
            </div>
          </div>
        </div>
      )}

      {templateOpen && (
        <div className="parser-modal-mask parser-template-mask">
          <div className="parser-modal parser-template-modal" role="dialog" aria-modal="true">
            <div className="parser-modal-header">
              <h2>{t('parserConfig.selectTemplate')}</h2>
              <Button
                type="text"
                className="parser-modal-close"
                onClick={() => setTemplateOpen(false)}
                aria-label={t('common.close')}
              >
                <CloseOutlined />
              </Button>
            </div>
            <div className="parser-modal-body">
              {templateError && <div className="parser-form-error">{templateError}</div>}
              {templateLoading ? (
                <div className="parser-template-state">{t('parserConfig.loadingTemplate')}</div>
              ) : templates.length === 0 ? (
                <div className="parser-template-state">{t('parserConfig.emptyTemplate')}</div>
              ) : (
                <div className="parser-template-list">
                  {templates.map((template) => (
                    <Button key={template.id} onClick={() => applyTemplate(template)}>
                      <span className="parser-template-icon">
                        <FileTextOutlined />
                      </span>
                      <span className="parser-template-info">
                        {/* 名称单行省略 + 溢出 hover 全文，交由 Typography 组件处理（不使用 CSS 省略） */}
                        <Typography.Text
                          ellipsis={{ tooltip: template.name }}
                          style={{ display: 'block', color: '#0b2b5c', fontSize: 14, fontWeight: 500 }}
                        >
                          {template.name}
                        </Typography.Text>
                        <span className="parser-template-desc">{templateDesc(template, t)}</span>
                      </span>
                      <em>{t('parserConfig.useTemplate')}</em>
                    </Button>
                  ))}
                </div>
              )}
            </div>
          </div>
        </div>
      )}
    </>
  );
}

const DomainKnowledgeParser = function DomainKnowledgeParser() {
  const { global } = useStore();
  return <ParserConfigContent spaceId={global.currentSpaceId} />;
};

export default DomainKnowledgeParser;
