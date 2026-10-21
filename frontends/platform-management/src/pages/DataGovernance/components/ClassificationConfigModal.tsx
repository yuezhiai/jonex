import { useCallback, useEffect, useRef, useState } from 'react';
import {
  Alert,
  Button,
  Empty,
  Form,
  Input,
  InputNumber,
  Modal,
  Select,
  Skeleton,
  Space,
  Switch,
  Table,
  Tabs,
  Tag,
  message,
} from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { PlusOutlined, ReloadOutlined } from '@ant-design/icons';
import { useTranslation } from 'react-i18next';
import {
  getClassificationDefault,
  listUserClearances,
  setClassificationDefault,
  setUserClearance,
  upsertClassificationCategory,
  upsertClassificationLevel,
} from '@/api/dataGovernance';
import type {
  DgClassificationCategory,
  DgClassificationConfig,
  DgClassificationLevel,
  DgUserClearance,
} from '../types';
import {
  actionErrorMessage,
  buildCategoryOptions,
  buildClassificationDefaultPatch,
  buildLevelOptions,
  classificationCodeError,
  classificationNameError,
  classificationRankError,
  errorMeta,
} from '../utils';

interface Props {
  open: boolean;
  config: DgClassificationConfig;
  /** 词表刷新中（父级拉取）；三 Tab 需要给出明确加载态。 */
  loading: boolean;
  /** 词表刷新失败（父级）；有旧配置时仍展示旧数据，但需提示可重试。 */
  error: string | null;
  canManageTenant: boolean;
  kbs: Array<{ id: string; name: string }>;
  onClose: () => void;
  onChanged: () => void;
  onRetry: () => void;
}

type ConfigTab = 'levels' | 'categories' | 'defaults' | 'clearances';

interface LevelFormValues {
  code: string;
  name: string;
  rank: number;
  isActive: boolean;
}

interface CategoryFormValues {
  code: string;
  name: string;
  isActive: boolean;
}

/** 默认值草稿：`null` = 未设置（继承），`[]` = 显式「无分类」。 */
interface DefaultDraft {
  levelCode: string | null;
  categories: string[] | null;
}

const EMPTY_DEFAULT_DRAFT: DefaultDraft = { levelCode: null, categories: null };

/**
 * 分级配置弹窗（第 4 期）：维护等级 / 分类词表，以及租户级、知识库级默认值。
 *
 * 词表与默认值的接口都是「写后由父级重新拉配置」——这里不维护本地真相，
 * 保存成功后调用 `onChanged()` 让页面与弹窗一起刷新。
 */
export default function ClassificationConfigModal({
  open,
  config,
  loading,
  error,
  canManageTenant,
  kbs,
  onClose,
  onChanged,
  onRetry,
}: Props) {
  const { t } = useTranslation();
  // 异步回调里读 tRef，避免切换语言触发默认值的重新拉取、覆盖未保存草稿。
  const tRef = useRef(t);
  useEffect(() => {
    tRef.current = t;
  }, [t]);

  const [activeTab, setActiveTab] = useState<ConfigTab>('levels');

  // ── 词表编辑（等级 / 分类各一个小弹窗） ────────────────────────────
  const [levelModalOpen, setLevelModalOpen] = useState(false);
  const [levelEditing, setLevelEditing] = useState<DgClassificationLevel | null>(null);
  const [levelSaving, setLevelSaving] = useState(false);
  const [levelForm] = Form.useForm<LevelFormValues>();

  const [categoryModalOpen, setCategoryModalOpen] = useState(false);
  const [categoryEditing, setCategoryEditing] = useState<DgClassificationCategory | null>(null);
  const [categorySaving, setCategorySaving] = useState(false);
  const [categoryForm] = Form.useForm<CategoryFormValues>();

  // ── 默认值 ─────────────────────────────────────────────────────────
  const [tenantDraft, setTenantDraft] = useState<DefaultDraft>(EMPTY_DEFAULT_DRAFT);
  const [tenantSnapshot, setTenantSnapshot] = useState<DefaultDraft>(EMPTY_DEFAULT_DRAFT);
  const [tenantLoaded, setTenantLoaded] = useState(false);
  const [tenantLoading, setTenantLoading] = useState(false);
  const [tenantSaving, setTenantSaving] = useState(false);
  const [tenantError, setTenantError] = useState<string | null>(null);

  const [kbId, setKbId] = useState<string | undefined>();
  const [kbDraft, setKbDraft] = useState<DefaultDraft>(EMPTY_DEFAULT_DRAFT);
  const [kbSnapshot, setKbSnapshot] = useState<DefaultDraft>(EMPTY_DEFAULT_DRAFT);
  const [kbLoaded, setKbLoaded] = useState(false);
  const [kbLoading, setKbLoading] = useState(false);
  const [kbSaving, setKbSaving] = useState(false);
  const [kbError, setKbError] = useState<string | null>(null);

  // ── 用户密级（规格 §6.3） ─────────────────────────────────────────
  const [clearanceRows, setClearanceRows] = useState<DgUserClearance[]>([]);
  const [clearanceLoading, setClearanceLoading] = useState(false);
  const [clearanceError, setClearanceError] = useState<string | null>(null);
  const [clearanceSavingId, setClearanceSavingId] = useState<string | null>(null);

  // 作用域快速切换时，只有最后一次请求允许回写。
  const tenantSeq = useRef(0);
  const kbSeq = useRef(0);
  const clearanceSeq = useRef(0);

  const loadTenantDefault = useCallback(async () => {
    const my = ++tenantSeq.current;
    setTenantLoading(true);
    setTenantError(null);
    setTenantLoaded(false);
    try {
      const next = await getClassificationDefault('tenant');
      if (my !== tenantSeq.current) return;
      const draft = { levelCode: next.levelCode, categories: next.categories };
      setTenantDraft(draft);
      setTenantSnapshot(draft);
      setTenantLoaded(true);
    } catch (err: unknown) {
      if (my !== tenantSeq.current) return;
      const meta = errorMeta(err);
      setTenantError(
        meta.status !== undefined && meta.message
          ? meta.message
          : tRef.current('dataGovernance.classification.defaultsLoadFailed'),
      );
    } finally {
      if (my === tenantSeq.current) setTenantLoading(false);
    }
  }, []);

  const loadKbDefault = useCallback(async (id: string) => {
    const my = ++kbSeq.current;
    setKbLoading(true);
    setKbError(null);
    setKbLoaded(false);
    try {
      const next = await getClassificationDefault('kb', id);
      if (my !== kbSeq.current) return;
      const draft = { levelCode: next.levelCode, categories: next.categories };
      setKbDraft(draft);
      setKbSnapshot(draft);
      setKbLoaded(true);
    } catch (err: unknown) {
      if (my !== kbSeq.current) return;
      const meta = errorMeta(err);
      setKbError(
        meta.status !== undefined && meta.message
          ? meta.message
          : tRef.current('dataGovernance.classification.defaultsLoadFailed'),
      );
    } finally {
      if (my === kbSeq.current) setKbLoading(false);
    }
  }, []);

  const loadUserClearances = useCallback(async () => {
    const my = ++clearanceSeq.current;
    setClearanceLoading(true);
    setClearanceError(null);
    try {
      const next = await listUserClearances();
      if (my !== clearanceSeq.current) return;
      setClearanceRows(next);
    } catch (err: unknown) {
      if (my !== clearanceSeq.current) return;
      const meta = errorMeta(err);
      setClearanceError(
        meta.status !== undefined && meta.message
          ? meta.message
          : tRef.current('dataGovernance.classification.clearance.loadFailed'),
      );
    } finally {
      if (my === clearanceSeq.current) setClearanceLoading(false);
    }
  }, []);

  // 只在打开且有权维护租户默认值时拉取；关闭/无权时不发请求。
  useEffect(() => {
    if (open && canManageTenant) {
      void loadTenantDefault();
    } else {
      tenantSeq.current += 1;
      setTenantLoading(false);
      setTenantError(null);
      setTenantLoaded(false);
      setTenantDraft(EMPTY_DEFAULT_DRAFT);
      setTenantSnapshot(EMPTY_DEFAULT_DRAFT);
    }
  }, [open, canManageTenant, loadTenantDefault]);

  // 用户密级与租户默认值同权：只在打开且可管理租户时拉取。
  useEffect(() => {
    if (open && canManageTenant) {
      void loadUserClearances();
      return;
    }
    clearanceSeq.current += 1;
    setClearanceLoading(false);
    setClearanceError(null);
    setClearanceRows([]);
    setClearanceSavingId(null);
  }, [open, canManageTenant, loadUserClearances]);

  // KB 默认只在选中 KB 时拉取；切换/清空立即丢弃旧值。
  useEffect(() => {
    if (open && kbId) {
      void loadKbDefault(kbId);
      return;
    }
    kbSeq.current += 1;
    setKbLoading(false);
    setKbError(null);
    setKbLoaded(false);
    setKbDraft(EMPTY_DEFAULT_DRAFT);
    setKbSnapshot(EMPTY_DEFAULT_DRAFT);
  }, [open, kbId, loadKbDefault]);

  // 关闭时清空全部状态，避免下次打开看到上一次的草稿/错误。
  useEffect(() => {
    if (open) return;
    tenantSeq.current += 1;
    kbSeq.current += 1;
    setActiveTab('levels');
    setLevelModalOpen(false);
    setLevelEditing(null);
    setLevelSaving(false);
    levelForm.resetFields();
    setCategoryModalOpen(false);
    setCategoryEditing(null);
    setCategorySaving(false);
    categoryForm.resetFields();
    setTenantDraft(EMPTY_DEFAULT_DRAFT);
    setTenantSnapshot(EMPTY_DEFAULT_DRAFT);
    setTenantLoaded(false);
    setTenantLoading(false);
    setTenantSaving(false);
    setTenantError(null);
    setKbId(undefined);
    setKbDraft(EMPTY_DEFAULT_DRAFT);
    setKbSnapshot(EMPTY_DEFAULT_DRAFT);
    setKbLoaded(false);
    setKbLoading(false);
    setKbSaving(false);
    setKbError(null);
    clearanceSeq.current += 1;
    setClearanceRows([]);
    setClearanceLoading(false);
    setClearanceError(null);
    setClearanceSavingId(null);
  }, [open, levelForm, categoryForm]);

  const showActionError = (
    err: unknown,
    fallbackKey = 'dataGovernance.classification.saveFailed',
  ) => {
    const text = actionErrorMessage(err, {
      forbiddenKey: 'dataGovernance.drawer.saveForbidden',
      fallbackKey,
    });
    message.error('key' in text ? tRef.current(text.key) : text.message);
  };

  // 任一保存进行中：禁止关闭弹窗 / 切换 Tab / 关闭内层编辑弹窗。
  const busySaving =
    tenantSaving || kbSaving || levelSaving || categorySaving || clearanceSavingId !== null;

  // ── 词表状态（加载/失败） ─────────────────────────────────────────
  const configEmpty = config.levels.length === 0 && config.categories.length === 0;
  const configBlocked = (loading || Boolean(error)) && configEmpty;

  const renderConfigState = () => {
    if (loading && configEmpty) {
      return <Skeleton active paragraph={{ rows: 4 }} />;
    }
    if (error) {
      return (
        <Alert
          type="error"
          showIcon
          title={error}
          action={
            <Button size="small" icon={<ReloadOutlined />} onClick={onRetry}>
              {t('common.retry')}
            </Button>
          }
        />
      );
    }
    return null;
  };

  // ── 等级 ───────────────────────────────────────────────────────────
  const openAddLevel = () => {
    // 词表维护仅租户管理员可操作（规格 §7.4）；按钮已 disabled，这里再兜一层。
    if (!canManageTenant) return;
    const nextRank =
      config.levels.reduce((max, level) => Math.max(max, level.rank), 0) + 1;
    setLevelEditing(null);
    levelForm.setFieldsValue({ code: '', name: '', rank: nextRank, isActive: true });
    setLevelModalOpen(true);
  };

  const openEditLevel = (row: DgClassificationLevel) => {
    if (!canManageTenant) return;
    setLevelEditing(row);
    levelForm.setFieldsValue(row);
    setLevelModalOpen(true);
  };

  const submitLevel = async () => {
    if (levelSaving) return;
    let values: LevelFormValues;
    try {
      values = await levelForm.validateFields();
    } catch {
      return;
    }
    const code = values.code.trim();
    setLevelSaving(true);
    try {
      await upsertClassificationLevel(code, {
        name: values.name.trim(),
        rank: values.rank,
        is_active: values.isActive,
      });
      message.success(tRef.current('dataGovernance.classification.saved'));
      setLevelModalOpen(false);
      onChanged();
    } catch (err: unknown) {
      showActionError(err);
    } finally {
      setLevelSaving(false);
    }
  };

  const levelColumns: ColumnsType<DgClassificationLevel> = [
    { title: t('dataGovernance.classification.code'), dataIndex: 'code', width: 140 },
    { title: t('dataGovernance.classification.name'), dataIndex: 'name' },
    { title: t('dataGovernance.classification.rank'), dataIndex: 'rank', width: 100 },
    {
      title: t('dataGovernance.classification.active'),
      dataIndex: 'isActive',
      width: 100,
      render: (value: boolean) => (
        <Tag color={value ? 'green' : 'default'}>
          {value ? t('common.yes') : t('common.no')}
        </Tag>
      ),
    },
    {
      title: t('common.actions'),
      key: 'actions',
      width: 100,
      render: (_: unknown, row) => (
        <Button
          type="link"
          size="small"
          disabled={!canManageTenant}
          onClick={() => openEditLevel(row)}
        >
          {t('dataGovernance.classification.edit')}
        </Button>
      ),
    },
  ];

  // ── 分类 ───────────────────────────────────────────────────────────
  const openAddCategory = () => {
    // 词表维护仅租户管理员可操作（规格 §7.4）；按钮已 disabled，这里再兜一层。
    if (!canManageTenant) return;
    setCategoryEditing(null);
    categoryForm.setFieldsValue({ code: '', name: '', isActive: true });
    setCategoryModalOpen(true);
  };

  const openEditCategory = (row: DgClassificationCategory) => {
    if (!canManageTenant) return;
    setCategoryEditing(row);
    categoryForm.setFieldsValue(row);
    setCategoryModalOpen(true);
  };

  const submitCategory = async () => {
    if (categorySaving) return;
    let values: CategoryFormValues;
    try {
      values = await categoryForm.validateFields();
    } catch {
      return;
    }
    const code = values.code.trim();
    setCategorySaving(true);
    try {
      await upsertClassificationCategory(code, {
        name: values.name.trim(),
        is_active: values.isActive,
      });
      message.success(tRef.current('dataGovernance.classification.saved'));
      setCategoryModalOpen(false);
      onChanged();
    } catch (err: unknown) {
      showActionError(err);
    } finally {
      setCategorySaving(false);
    }
  };

  const categoryColumns: ColumnsType<DgClassificationCategory> = [
    { title: t('dataGovernance.classification.code'), dataIndex: 'code', width: 180 },
    { title: t('dataGovernance.classification.name'), dataIndex: 'name' },
    {
      title: t('dataGovernance.classification.active'),
      dataIndex: 'isActive',
      width: 100,
      render: (value: boolean) => (
        <Tag color={value ? 'green' : 'default'}>
          {value ? t('common.yes') : t('common.no')}
        </Tag>
      ),
    },
    {
      title: t('common.actions'),
      key: 'actions',
      width: 100,
      render: (_: unknown, row) => (
        <Button
          type="link"
          size="small"
          disabled={!canManageTenant}
          onClick={() => openEditCategory(row)}
        >
          {t('dataGovernance.classification.edit')}
        </Button>
      ),
    },
  ];

  // ── 默认值 ─────────────────────────────────────────────────────────
  const levelOptions = buildLevelOptions(t, config.levels);
  const categoryOptions = buildCategoryOptions(config.categories, t);

  const tenantPatch = buildClassificationDefaultPatch(tenantSnapshot, tenantDraft);
  const kbPatch = buildClassificationDefaultPatch(kbSnapshot, kbDraft);
  const tenantDirty = Object.keys(tenantPatch).length > 0;
  const kbDirty = Object.keys(kbPatch).length > 0;

  const saveTenantDefault = async () => {
    if (tenantSaving || !tenantLoaded) return;
    const patch = buildClassificationDefaultPatch(tenantSnapshot, tenantDraft);
    if (!Object.keys(patch).length) {
      message.info(tRef.current('common.noChanges'));
      return;
    }
    setTenantSaving(true);
    try {
      const next = await setClassificationDefault('tenant', null, patch);
      const draft = { levelCode: next.levelCode, categories: next.categories };
      setTenantDraft(draft);
      setTenantSnapshot(draft);
      message.success(tRef.current('dataGovernance.classification.saved'));
      onChanged();
    } catch (err: unknown) {
      showActionError(err);
    } finally {
      setTenantSaving(false);
    }
  };

  const saveKbDefault = async () => {
    if (kbSaving || !kbId || !kbLoaded) return;
    const patch = buildClassificationDefaultPatch(kbSnapshot, kbDraft);
    if (!Object.keys(patch).length) {
      message.info(tRef.current('common.noChanges'));
      return;
    }
    setKbSaving(true);
    try {
      const next = await setClassificationDefault('kb', kbId, patch);
      const draft = { levelCode: next.levelCode, categories: next.categories };
      setKbDraft(draft);
      setKbSnapshot(draft);
      message.success(tRef.current('dataGovernance.classification.saved'));
      onChanged();
    } catch (err: unknown) {
      showActionError(err);
    } finally {
      setKbSaving(false);
    }
  };

  // ── 用户密级 ───────────────────────────────────────────────────────
  const saveClearance = async (userId: string, maxLevelCode: string | null) => {
    if (clearanceSavingId) return;
    setClearanceSavingId(userId);
    try {
      await setUserClearance(userId, maxLevelCode);
      message.success(tRef.current('dataGovernance.classification.saved'));
      // 写后以服务端为准重新拉取，并通知父级刷新配置（规格 §6.3）。
      await loadUserClearances();
      onChanged();
    } catch (err: unknown) {
      showActionError(err, 'dataGovernance.classification.clearance.saveFailed');
    } finally {
      setClearanceSavingId(null);
    }
  };

  const clearanceColumns: ColumnsType<DgUserClearance> = [
    {
      title: t('dataGovernance.classification.clearance.user'),
      dataIndex: 'displayName',
      key: 'user',
    },
    {
      title: t('dataGovernance.classification.clearance.username'),
      dataIndex: 'username',
      key: 'username',
    },
    {
      title: t('dataGovernance.classification.clearance.level'),
      dataIndex: 'maxLevelCode',
      render: (value: string | null, row) => (
        <Select
          size="small"
          value={value ?? undefined}
          placeholder={t('dataGovernance.classification.clearance.default')}
          options={levelOptions}
          loading={clearanceSavingId === row.userId}
          disabled={!canManageTenant || clearanceSavingId !== null}
          onChange={(next: string) => void saveClearance(row.userId, next)}
          style={{ width: '100%' }}
        />
      ),
    },
    {
      title: t('common.actions'),
      key: 'actions',
      width: 100,
      render: (_: unknown, row) =>
        row.maxLevelCode ? (
          <Button
            type="link"
            size="small"
            disabled={clearanceSavingId !== null}
            onClick={() => void saveClearance(row.userId, null)}
          >
            {t('dataGovernance.classification.clearance.clear')}
          </Button>
        ) : null,
    },
  ];

  const renderClearances = () => {
    // 仅首屏无数据时才整块 Skeleton；刷新时保留旧行与滚动位置。
    if (clearanceLoading && clearanceRows.length === 0) {
      return <Skeleton active paragraph={{ rows: 4 }} />;
    }
    if (clearanceError) {
      return (
        <Alert
          type="error"
          showIcon
          title={clearanceError}
          action={
            <Button
              size="small"
              icon={<ReloadOutlined />}
              onClick={() => void loadUserClearances()}
            >
              {t('common.retry')}
            </Button>
          }
        />
      );
    }
    return (
      <Table<DgUserClearance>
        rowKey="userId"
        size="small"
        loading={clearanceLoading}
        pagination={{ pageSize: 10, showSizeChanger: false }}
        dataSource={clearanceRows}
        columns={clearanceColumns}
        locale={{ emptyText: <Empty description={t('common.noData')} /> }}
      />
    );
  };

  const renderDefaultEditor = (
    draft: DefaultDraft,
    setDraft: (updater: (current: DefaultDraft) => DefaultDraft) => void,
    disabled: boolean,
    saving: boolean,
    dirty: boolean,
    onSave: () => void,
  ) => (
    <Form layout="vertical">
      <Form.Item label={t('dataGovernance.classification.level')}>
        <Select
          allowClear
          disabled={disabled}
          value={draft.levelCode ?? undefined}
          placeholder={t('dataGovernance.classification.inherit')}
          options={levelOptions}
          onChange={(value) =>
            setDraft((current) => ({ ...current, levelCode: value ?? null }))
          }
          style={{ width: '100%' }}
        />
      </Form.Item>
      <Form.Item label={t('dataGovernance.classification.categories')}>
        <Select
          mode="multiple"
          allowClear
          disabled={disabled}
          value={draft.categories ?? undefined}
          placeholder={t('dataGovernance.classification.inherit')}
          options={categoryOptions}
          onChange={(value: string[]) =>
            setDraft((current) => ({
              ...current,
              // 清空选择 = 继承；显式「无分类」走下方按钮（[]）。
              categories: value && value.length ? value : null,
            }))
          }
          style={{ width: '100%' }}
        />
        <Space wrap style={{ marginTop: 8 }}>
          <Button
            size="small"
            disabled={disabled || draft.categories === null}
            onClick={() => setDraft((current) => ({ ...current, categories: null }))}
          >
            {t('dataGovernance.classification.inherit')}
          </Button>
          <Button
            size="small"
            disabled={disabled || draft.categories?.length === 0}
            onClick={() => setDraft((current) => ({ ...current, categories: [] }))}
          >
            {t('dataGovernance.classification.none')}
          </Button>
        </Space>
      </Form.Item>
      <div className="yx-dg-muted" style={{ marginBottom: 12 }}>
        {t('dataGovernance.classification.inheritHint')}
      </div>
      <Button
        type="primary"
        loading={saving}
        disabled={disabled || !dirty}
        onClick={onSave}
      >
        {t('common.save')}
      </Button>
    </Form>
  );

  const renderTenantDefault = () => {
    if (!canManageTenant) {
      return (
        <Alert
          type="info"
          showIcon
          title={t('dataGovernance.classification.tenantDefaultForbidden')}
        />
      );
    }
    if (tenantLoading) return <Skeleton active paragraph={{ rows: 3 }} />;
    if (tenantError) {
      return (
        <Alert
          type="error"
          showIcon
          title={tenantError}
          action={
            <Button
              size="small"
              icon={<ReloadOutlined />}
              onClick={() => void loadTenantDefault()}
            >
              {t('common.retry')}
            </Button>
          }
        />
      );
    }
    return renderDefaultEditor(
      tenantDraft,
      setTenantDraft,
      tenantSaving || !tenantLoaded,
      tenantSaving,
      tenantDirty,
      () => void saveTenantDefault(),
    );
  };

  const renderKbDefault = () => (
    <Space direction="vertical" size={12} style={{ width: '100%' }}>
      <Select
        showSearch
        allowClear
        optionFilterProp="label"
        disabled={kbSaving}
        placeholder={t('dataGovernance.policy.selectKb')}
        value={kbId}
        options={kbs.map((kb) => ({ value: kb.id, label: kb.name }))}
        onChange={(value) => setKbId(value)}
        style={{ width: '100%' }}
      />
      {!kbId ? (
        <Empty description={t('dataGovernance.policy.selectKb')} />
      ) : kbLoading ? (
        <Skeleton active paragraph={{ rows: 3 }} />
      ) : kbError ? (
        <Alert
          type="error"
          showIcon
          title={kbError}
          action={
            <Button
              size="small"
              icon={<ReloadOutlined />}
              onClick={() => void loadKbDefault(kbId)}
            >
              {t('common.retry')}
            </Button>
          }
        />
      ) : (
        renderDefaultEditor(
          kbDraft,
          setKbDraft,
          kbSaving || !kbLoaded,
          kbSaving,
          kbDirty,
          () => void saveKbDefault(),
        )
      )}
    </Space>
  );

  const tabItems = [
    {
      key: 'levels',
      label: t('dataGovernance.classification.levelManage'),
      disabled: busySaving,
      children: (
        <Space direction="vertical" size={12} style={{ width: '100%' }}>
          {renderConfigState()}
          {!configBlocked && (
            <>
              <Button
                type="primary"
                icon={<PlusOutlined />}
                disabled={!canManageTenant}
                onClick={openAddLevel}
              >
                {t('dataGovernance.classification.addLevel')}
              </Button>
              <Table<DgClassificationLevel>
                rowKey="code"
                size="small"
                pagination={false}
                dataSource={config.levels}
                columns={levelColumns}
                locale={{ emptyText: <Empty description={t('common.noData')} /> }}
              />
            </>
          )}
        </Space>
      ),
    },
    {
      key: 'categories',
      label: t('dataGovernance.classification.categoryManage'),
      disabled: busySaving,
      children: (
        <Space direction="vertical" size={12} style={{ width: '100%' }}>
          {renderConfigState()}
          {!configBlocked && (
            <>
              <Button
                type="primary"
                icon={<PlusOutlined />}
                disabled={!canManageTenant}
                onClick={openAddCategory}
              >
                {t('dataGovernance.classification.addCategory')}
              </Button>
              <Table<DgClassificationCategory>
                rowKey="code"
                size="small"
                pagination={false}
                dataSource={config.categories}
                columns={categoryColumns}
                locale={{ emptyText: <Empty description={t('common.noData')} /> }}
              />
            </>
          )}
        </Space>
      ),
    },
    {
      key: 'defaults',
      label: t('dataGovernance.classification.defaults'),
      disabled: busySaving,
      children: (
        <Space direction="vertical" size={16} style={{ width: '100%' }}>
          {renderConfigState()}
          {!configBlocked && (
            <>
              <div>
                <div className="yx-dg-section-title">
                  {t('dataGovernance.classification.tenantDefault')}
                </div>
                {renderTenantDefault()}
              </div>
              <div>
                <div className="yx-dg-section-title">
                  {t('dataGovernance.classification.kbDefault')}
                </div>
                {renderKbDefault()}
              </div>
            </>
          )}
        </Space>
      ),
    },
  ];

  // 用户密级是租户管理操作：无权限时连 Tab 都不出现。
  if (canManageTenant) {
    tabItems.push({
      key: 'clearances',
      label: t('dataGovernance.classification.clearance.title'),
      disabled: busySaving,
      children: (
        <Space direction="vertical" size={12} style={{ width: '100%' }}>
          {renderConfigState()}
          {!configBlocked && (
            <>
              <Button
                icon={<ReloadOutlined />}
                loading={clearanceLoading}
                disabled={busySaving}
                onClick={() => void loadUserClearances()}
              >
                {t('common.refresh')}
              </Button>
              {renderClearances()}
            </>
          )}
        </Space>
      ),
    });
  }

  // 校验规则统一走 utils 纯函数，保证前后端约束同源、错误 key 可本地化。
  const codeRules = [
    {
      validator: (_: unknown, value: string | undefined) => {
        const key = classificationCodeError(value);
        return key ? Promise.reject(new Error(t(key))) : Promise.resolve();
      },
    },
  ];
  const nameRules = [
    {
      validator: (_: unknown, value: string | undefined) => {
        const key = classificationNameError(value);
        return key ? Promise.reject(new Error(t(key))) : Promise.resolve();
      },
    },
  ];
  const rankRules = [
    {
      validator: (_: unknown, value: number | null | undefined) => {
        const key = classificationRankError(value);
        return key ? Promise.reject(new Error(t(key))) : Promise.resolve();
      },
    },
  ];

  return (
    <>
      <Modal
        open={open}
        title={t('dataGovernance.classification.config')}
        onCancel={() => {
          if (!busySaving) onClose();
        }}
        closable={!busySaving}
        maskClosable={false}
        keyboard={!busySaving}
        footer={null}
        width={820}
        destroyOnHidden
      >
        <Tabs
          activeKey={activeTab}
          onChange={(key) => {
            if (!busySaving) setActiveTab(key as ConfigTab);
          }}
          items={tabItems}
        />
      </Modal>

      <Modal
        open={levelModalOpen}
        title={
          levelEditing
            ? t('dataGovernance.classification.edit')
            : t('dataGovernance.classification.addLevel')
        }
        onCancel={() => {
          if (!levelSaving) setLevelModalOpen(false);
        }}
        onOk={() => void submitLevel()}
        okText={t('common.save')}
        cancelText={t('common.cancel')}
        confirmLoading={levelSaving}
        closable={!levelSaving}
        maskClosable={false}
        keyboard={!levelSaving}
        cancelButtonProps={{ disabled: levelSaving }}
        destroyOnHidden
      >
        <Form form={levelForm} layout="vertical">
          <Form.Item
            name="code"
            label={t('dataGovernance.classification.code')}
            rules={codeRules}
          >
            <Input maxLength={32} disabled={Boolean(levelEditing)} />
          </Form.Item>
          <Form.Item
            name="name"
            label={t('dataGovernance.classification.name')}
            rules={nameRules}
          >
            <Input maxLength={64} />
          </Form.Item>
          <Form.Item
            name="rank"
            label={t('dataGovernance.classification.rank')}
            rules={rankRules}
          >
            <InputNumber min={1} max={1000} precision={0} style={{ width: '100%' }} />
          </Form.Item>
          <Form.Item
            name="isActive"
            label={t('dataGovernance.classification.active')}
            valuePropName="checked"
          >
            <Switch />
          </Form.Item>
        </Form>
      </Modal>

      <Modal
        open={categoryModalOpen}
        title={
          categoryEditing
            ? t('dataGovernance.classification.edit')
            : t('dataGovernance.classification.addCategory')
        }
        onCancel={() => {
          if (!categorySaving) setCategoryModalOpen(false);
        }}
        onOk={() => void submitCategory()}
        okText={t('common.save')}
        cancelText={t('common.cancel')}
        confirmLoading={categorySaving}
        closable={!categorySaving}
        maskClosable={false}
        keyboard={!categorySaving}
        cancelButtonProps={{ disabled: categorySaving }}
        destroyOnHidden
      >
        <Form form={categoryForm} layout="vertical">
          <Form.Item
            name="code"
            label={t('dataGovernance.classification.code')}
            rules={codeRules}
          >
            <Input maxLength={32} disabled={Boolean(categoryEditing)} />
          </Form.Item>
          <Form.Item
            name="name"
            label={t('dataGovernance.classification.name')}
            rules={nameRules}
          >
            <Input maxLength={64} />
          </Form.Item>
          <Form.Item
            name="isActive"
            label={t('dataGovernance.classification.active')}
            valuePropName="checked"
          >
            <Switch />
          </Form.Item>
        </Form>
      </Modal>
    </>
  );
}
