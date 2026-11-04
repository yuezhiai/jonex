import { useCallback, useEffect, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import {
  Alert,
  Button,
  Descriptions,
  Drawer,
  Empty,
  Form,
  Input,
  Modal,
  Select,
  Skeleton,
  Space,
  Tag,
  Tooltip,
  message,
} from 'antd';
import { ReloadOutlined } from '@ant-design/icons';
import { useTranslation } from 'react-i18next';
import { formatDateTime } from '@jonex/shared-lib';
import { getShellContext } from '@jonex/shell-sdk';
import {
  createStandardDraft,
  deleteStandardDraft,
  getStandardAsset,
  listStandardAssets,
  listStandardAssetVersions,
  publishStandardAsset,
  retireStandardAsset,
  updateStandardDraft,
} from '@/api/standardAssets';
import type {
  DgStandardAssetDetail,
  DgStandardAssetItem,
  DgStandardAssetKind,
  DgStandardAssetStatus,
} from '../types';
import {
  DATA_ELEMENT_DATA_TYPES,
  KINDS,
  STATUS_COLORS,
  isConflictError,
  isDraftDirty,
  kindLabelKey,
  normalizePayload,
  toStandardAssetItem,
  validateDraftFields,
} from '../standardAssetUtils';
import { loadErrorText } from '../standardUtils';
import { actionErrorMessage, errorMeta } from '../utils';

/** 抽屉模式：新建（给定默认 kind）或查看某条列表行。 */
export type StandardAssetDrawerMode =
  | { type: 'create'; kind: DgStandardAssetKind }
  | { type: 'view'; row: DgStandardAssetItem };

interface Props {
  open: boolean;
  mode: StandardAssetDrawerMode;
  onClose: () => void;
  /** 服务端写入成功（或冲突需要刷新）后通知父级重拉列表。 */
  onChanged: () => void;
  /** 把抽屉切换到另一条列表行（如「编辑草稿」定位到草稿行）。 */
  onSelectRow: (row: DgStandardAssetItem) => void;
}

/** 必填字段 → i18n 键（`data_type:enum` 是校验器的枚举标记，按 data_type 展示）。 */
const FIELD_LABEL_KEYS: Record<string, string> = {
  definition: 'dataGovernance.standardAssets.fields.definition',
  formula: 'dataGovernance.standardAssets.fields.formula',
  unit: 'dataGovernance.standardAssets.fields.unit',
  data_type: 'dataGovernance.standardAssets.fields.dataType',
  'data_type:enum': 'dataGovernance.standardAssets.fields.dataType',
};

/** 把校验器返回的缺失字段名翻成界面文案（保持后端字段口径）。 */
function missingFieldLabels(
  translate: (key: string) => string,
  keys: string[],
): string[] {
  return keys.map((key) => translate(FIELD_LABEL_KEYS[key] ?? key));
}

// ── 按 kind 渲染 payload 字段 ────────────────────────────────────────

interface KindFieldsProps {
  kind: DgStandardAssetKind;
  payload: Record<string, unknown>;
  disabled?: boolean;
  onChange?: (next: Record<string, unknown>) => void;
}

/**
 * 三类标准资产的 payload 表单。只做取值/回写，不做校验
 * （校验统一走 `validateDraftFields`，与后端同口径）。
 */
function KindFields({ kind, payload, disabled = false, onChange }: KindFieldsProps) {
  const { t } = useTranslation();
  const set = (key: string, value: unknown) => onChange?.({ ...payload, [key]: value });
  const text = (key: string): string =>
    typeof payload[key] === 'string' ? (payload[key] as string) : '';
  const tags = (key: string): string[] =>
    Array.isArray(payload[key])
      ? (payload[key] as unknown[]).filter((item): item is string => typeof item === 'string')
      : [];

  return (
    <Form layout="vertical" size="small" disabled={disabled}>
      <Form.Item label={t('dataGovernance.standardAssets.fields.definition')} required={!disabled}>
        <Input.TextArea
          rows={3}
          value={text('definition')}
          onChange={(event) => set('definition', event.target.value)}
        />
      </Form.Item>

      {kind === 'term' && (
        <>
          <Form.Item label={t('dataGovernance.standardAssets.fields.aliases')}>
            <Select
              mode="tags"
              value={tags('aliases')}
              onChange={(value: string[]) => set('aliases', value)}
              placeholder={t('dataGovernance.standardAssets.fields.aliases')}
            />
          </Form.Item>
          <Form.Item label={t('dataGovernance.standardAssets.fields.examples')}>
            <Select
              mode="tags"
              value={tags('examples')}
              onChange={(value: string[]) => set('examples', value)}
              placeholder={t('dataGovernance.standardAssets.fields.examples')}
            />
          </Form.Item>
          <Form.Item label={t('dataGovernance.standardAssets.fields.domain')}>
            <Input value={text('domain')} onChange={(event) => set('domain', event.target.value)} />
          </Form.Item>
        </>
      )}

      {kind === 'data_element' && (
        <>
          <Form.Item label={t('dataGovernance.standardAssets.fields.dataType')} required={!disabled}>
            <Select
              allowClear
              value={text('data_type') || undefined}
              onChange={(value: string | undefined) => set('data_type', value ?? '')}
              options={DATA_ELEMENT_DATA_TYPES.map((value) => ({ value, label: value }))}
              placeholder={t('dataGovernance.standardAssets.fields.dataType')}
            />
          </Form.Item>
          <Form.Item label={t('dataGovernance.standardAssets.fields.unit')}>
            <Input value={text('unit')} onChange={(event) => set('unit', event.target.value)} />
          </Form.Item>
          <Form.Item label={t('dataGovernance.standardAssets.fields.valueDomain')}>
            <Select
              mode="tags"
              value={tags('value_domain')}
              onChange={(value: string[]) => set('value_domain', value)}
              placeholder={t('dataGovernance.standardAssets.fields.valueDomain')}
            />
          </Form.Item>
          <Form.Item label={t('dataGovernance.standardAssets.fields.aliases')}>
            <Select
              mode="tags"
              value={tags('aliases')}
              onChange={(value: string[]) => set('aliases', value)}
              placeholder={t('dataGovernance.standardAssets.fields.aliases')}
            />
          </Form.Item>
        </>
      )}

      {kind === 'metric_card' && (
        <>
          <Form.Item label={t('dataGovernance.standardAssets.fields.formula')} required={!disabled}>
            <Input
              value={text('formula')}
              onChange={(event) => set('formula', event.target.value)}
            />
          </Form.Item>
          <Form.Item label={t('dataGovernance.standardAssets.fields.unit')} required={!disabled}>
            <Input value={text('unit')} onChange={(event) => set('unit', event.target.value)} />
          </Form.Item>
          <Form.Item label={t('dataGovernance.standardAssets.fields.dimensions')}>
            <Select
              mode="tags"
              value={tags('dimensions')}
              onChange={(value: string[]) => set('dimensions', value)}
              placeholder={t('dataGovernance.standardAssets.fields.dimensions')}
            />
          </Form.Item>
          <Form.Item label={t('dataGovernance.standardAssets.fields.source')}>
            <Input value={text('source')} onChange={(event) => set('source', event.target.value)} />
          </Form.Item>
        </>
      )}
    </Form>
  );
}

// ── 抽屉主体 ─────────────────────────────────────────────────────────

/**
 * 标准资产编辑抽屉。
 *
 * 关键语义（后端已确认）：
 * - 详情接口的 `hasDraft` / `publishedVersion` 不可靠，草稿标记只认**列表行**；
 * - 每次写操作成功后用返回的详情就地刷新，并通知父级重拉列表；
 * - 所有异步回写都过**代际守卫**（`detailGenRef`）：抽屉关闭、切换对象、
 *   重新加载详情都会递增代际，旧响应一律丢弃（含 loading/saving/action 回写）。
 */
export default function StandardAssetDrawer({
  open,
  mode,
  onClose,
  onChanged,
  onSelectRow,
}: Props) {
  const { t } = useTranslation();
  const tRef = useRef(t);
  useEffect(() => {
    tRef.current = t;
  }, [t]);

  // 异步回调里同步判断抽屉是否还开着（effect 清理可能晚于微任务）。
  const openRef = useRef(open);
  openRef.current = open;

  // mode 派生值在 render 期同步进 ref：effect 要等提交后才执行，
  // 这段窗口里落地的旧响应只能靠 render 期快照拦截。
  const modeType = mode.type;
  const modeKind = mode.type === 'create' ? mode.kind : null;
  const viewAssetId = mode.type === 'view' ? mode.row.assetId : null;
  const viewKind = mode.type === 'view' ? mode.row.kind : null;
  const viewCode = mode.type === 'view' ? mode.row.code : null;
  const viewAssetIdRef = useRef<string | null>(viewAssetId);
  viewAssetIdRef.current = viewAssetId;

  const isTenantAdmin = getShellContext()?.getCurrentUser?.()?.isTenantAdmin === true;

  const [detail, setDetail] = useState<DgStandardAssetDetail | null>(null);
  const [versions, setVersions] = useState<DgStandardAssetDetail[]>([]);
  const [versionsLoading, setVersionsLoading] = useState(false);
  const [versionsError, setVersionsError] = useState<string | null>(null);
  const [selectedVersion, setSelectedVersion] = useState<number | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  /** 冲突后刷新详情失败时的抽屉级错误（不覆盖正文/编辑表单）。 */
  const [refreshError, setRefreshError] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [draftLookup, setDraftLookup] = useState(false);
  const [saving, setSaving] = useState(false);
  const [action, setAction] = useState<'publish' | 'retire' | 'delete' | 'newDraft' | null>(
    null,
  );

  // 新建表单
  const [createKind, setCreateKind] = useState<DgStandardAssetKind>('term');
  const [createCode, setCreateCode] = useState('');
  const [createName, setCreateName] = useState('');
  const [createPayload, setCreatePayload] = useState<Record<string, unknown>>({});

  // 草稿编辑表单
  const [editName, setEditName] = useState('');
  const [editPayload, setEditPayload] = useState<Record<string, unknown>>({});

  /**
   * 详情代际号：`loadDetail` 启动、mode 变化、抽屉关闭时递增。
   * 异步落地前比对启动时捕获的值，不一致一律丢弃（不写 state）。
   */
  const detailGenRef = useRef(0);
  const versionsSeq = useRef(0);
  /**
   * 写操作序号：冲突刷新启动后若又发起了写操作，旧刷新响应不得落地
   * （否则会用刷新时的 revision 覆盖更新的写响应）。
   */
  const writeSeqRef = useRef(0);

  // 最新详情（异步回调里读取当前资产/版本，不依赖闭包快照）。
  const detailRef = useRef<DgStandardAssetDetail | null>(detail);
  detailRef.current = detail;

  const syncEditForm = useCallback((next: DgStandardAssetDetail) => {
    setEditName(next.name);
    setEditPayload({ ...next.payload });
  }, []);

  const loadDetail = useCallback(
    async (assetId: string) => {
      const gen = ++detailGenRef.current;
      setLoading(true);
      setError(null);
      setRefreshError(null);
      try {
        const next = await getStandardAsset(assetId);
        if (
          gen !== detailGenRef.current ||
          !openRef.current ||
          viewAssetIdRef.current !== assetId
        ) {
          return;
        }
        setDetail(next);
        syncEditForm(next);
      } catch (err: unknown) {
        if (
          gen !== detailGenRef.current ||
          !openRef.current ||
          viewAssetIdRef.current !== assetId
        ) {
          return;
        }
        setDetail(null);
        setError(
          loadErrorText(err, 'dataGovernance.standardAssets.loadFailed', tRef.current),
        );
      } finally {
        if (
          gen === detailGenRef.current &&
          openRef.current &&
          viewAssetIdRef.current === assetId
        ) {
          setLoading(false);
        }
      }
    },
    [syncEditForm],
  );

  const loadVersions = useCallback(
    async (kind: DgStandardAssetKind, code: string) => {
      const my = ++versionsSeq.current;
      setVersionsLoading(true);
      setVersionsError(null);
      try {
        const next = await listStandardAssetVersions(kind, code);
        if (my !== versionsSeq.current) return;
        setVersions(next);
      } catch (err: unknown) {
        if (my !== versionsSeq.current) return;
        // 版本历史是辅助信息：失败时显式提示 + 重试，但不挡住详情与编辑。
        setVersions([]);
        setVersionsError(
          loadErrorText(
            err,
            'dataGovernance.standardAssets.versionsFailed',
            tRef.current,
          ),
        );
      } finally {
        if (my === versionsSeq.current) setVersionsLoading(false);
      }
    },
    [],
  );

  // 开关抽屉 / 换查看对象：作废在途请求并重置本地状态。
  useEffect(() => {
    detailGenRef.current += 1;
    versionsSeq.current += 1;
    setSelectedVersion(null);
    setVersions([]);
    setVersionsLoading(false);
    setVersionsError(null);
    setRefreshError(null);
    setRefreshing(false);
    setDraftLookup(false);
    setError(null);
    setSaving(false);
    setAction(null);

    if (!open) {
      setDetail(null);
      setLoading(false);
      return;
    }

    if (modeType === 'create') {
      setDetail(null);
      setLoading(false);
      setCreateKind(modeKind ?? 'term');
      setCreateCode('');
      setCreateName('');
      setCreatePayload({});
      return;
    }

    if (!viewAssetId || !viewKind || !viewCode) return;
    // 写操作刚落地时已持有同一详情（如新建草稿成功后切到 view）：
    // 不重复请求，直接展示，避免闪一次骨架屏。
    if (detailRef.current?.assetId === viewAssetId) {
      setLoading(false);
      void loadVersions(viewKind, viewCode);
      return;
    }
    setDetail(null);
    void loadDetail(viewAssetId);
    void loadVersions(viewKind, viewCode);
  }, [open, modeType, modeKind, viewAssetId, viewKind, viewCode, loadDetail, loadVersions]);

  /**
   * 冲突后刷新详情：草稿仍可编辑时只更新详情/版本号，**不回填编辑表单**
   * （保留用户输入）；若服务端已不再是草稿（被他人发布/退役），继续保留
   * 本地编辑值会形成「dirty 但不可编辑、也没有保存入口」的死路，因此同步
   * 服务端内容并明确告知修改已丢弃。失败时在抽屉内显示错误 + 重试。
   */
  const refreshAfterConflict = useCallback(
    async (assetId: string) => {
      const gen = detailGenRef.current;
      const writeSeq = writeSeqRef.current;
      setRefreshing(true);
      setRefreshError(null);
      try {
        const next = await getStandardAsset(assetId);
        if (
          gen !== detailGenRef.current ||
          !openRef.current ||
          viewAssetIdRef.current !== assetId
        ) {
          return;
        }
        // 刷新期间又发起了写操作：写响应才是更新的一份，丢弃本次刷新结果。
        if (writeSeq !== writeSeqRef.current) return;
        // 单调保护：重试的响应可能比当前详情更旧，不回退 revision。
        const current = detailRef.current;
        if (current && current.assetId === assetId && next.revision < current.revision) return;
        detailRef.current = next;
        setDetail(next);
        if (next.status !== 'draft') {
          syncEditForm(next);
          message.warning(
            tRef.current('dataGovernance.standardAssets.draftPublishedElsewhere'),
          );
        }
        void loadVersions(next.kind, next.code);
      } catch (err: unknown) {
        if (
          gen !== detailGenRef.current ||
          !openRef.current ||
          viewAssetIdRef.current !== assetId
        ) {
          return;
        }
        setRefreshError(
          loadErrorText(
            err,
            'dataGovernance.standardAssets.refreshFailed',
            tRef.current,
          ),
        );
      } finally {
        if (
          gen === detailGenRef.current &&
          openRef.current &&
          viewAssetIdRef.current === assetId
        ) {
          setRefreshing(false);
        }
      }
    },
    [loadVersions, syncEditForm],
  );

  const showActionError = useCallback(
    (err: unknown, options: { refresh?: boolean } = {}) => {
      if (isConflictError(err)) {
        message.error(tRef.current('dataGovernance.standardAssets.conflict'));
        // 冲突说明别处已改：列表要重拉，抽屉内同时刷新详情（保留编辑值）。
        onChanged();
        const assetId = detailRef.current?.assetId;
        if (options.refresh !== false && assetId) void refreshAfterConflict(assetId);
        return;
      }
      const text = actionErrorMessage(err, {
        forbiddenKey: 'dataGovernance.standards.errorForbidden',
        fallbackKey: 'dataGovernance.standardAssets.actionFailed',
      });
      message.error('key' in text ? tRef.current(text.key) : text.message);
    },
    [onChanged, refreshAfterConflict],
  );

  const handleCreate = async () => {
    if (saving || refreshing) return;
    const code = createCode.trim();
    const name = createName.trim();
    const payload = normalizePayload(createPayload);

    const problems = missingFieldLabels(tRef.current, validateDraftFields(createKind, payload));
    if (!code) problems.push(tRef.current('dataGovernance.standardAssets.code'));
    if (!name) problems.push(tRef.current('dataGovernance.standardAssets.name'));
    if (problems.length) {
      message.error(
        tRef.current('dataGovernance.standardAssets.required', {
          fields: problems.join(' / '),
        }),
      );
      return;
    }

    const gen = detailGenRef.current;
    const startViewAssetId = viewAssetIdRef.current;
    writeSeqRef.current += 1;
    setSaving(true);
    try {
      const next = await createStandardDraft({ kind: createKind, code, name, payload });
      // 服务端已落库：即使抽屉已关闭/切走也要让列表刷新。
      onChanged();
      if (
        gen !== detailGenRef.current ||
        !openRef.current ||
        viewAssetIdRef.current !== startViewAssetId
      ) {
        return;
      }
      // 成功后切到 view：抽屉直接展示新草稿（含版本历史），用户可继续发布。
      detailRef.current = next;
      setDetail(next);
      syncEditForm(next);
      setSelectedVersion(null);
      // 资产 id 由「无」变为新行：只有这种情况需要替换父级的列表行。
      onSelectRow(toStandardAssetItem(next));
      message.success(tRef.current('common.operationSuccessful'));
    } catch (err: unknown) {
      if (
        gen !== detailGenRef.current ||
        !openRef.current ||
        viewAssetIdRef.current !== startViewAssetId
      ) {
        return;
      }
      // 同编码已有草稿：明确引导用户从列表打开，而不是笼统的乐观锁冲突。
      if (isConflictError(err) && errorMeta(err).message?.includes('已有草稿')) {
        message.error(tRef.current('dataGovernance.standardAssets.draftExists'));
        onChanged();
        return;
      }
      // 新建没有可刷新的既有详情，冲突只提示 + 父级重拉。
      showActionError(err, { refresh: false });
    } finally {
      if (
        gen === detailGenRef.current &&
        openRef.current &&
        viewAssetIdRef.current === startViewAssetId
      ) {
        setSaving(false);
      }
    }
  };

  const handleSave = async () => {
    if (!detail || saving || refreshing || detail.status !== 'draft') return;
    // 选中历史版本时表单是只读的，不允许保存。
    if (selectedVersion !== null && selectedVersion !== detail.version) return;
    const name = editName.trim();
    const payload = normalizePayload(editPayload);

    const problems = missingFieldLabels(tRef.current, validateDraftFields(detail.kind, payload));
    if (!name) problems.push(tRef.current('dataGovernance.standardAssets.name'));
    if (problems.length) {
      message.error(
        tRef.current('dataGovernance.standardAssets.required', {
          fields: problems.join(' / '),
        }),
      );
      return;
    }

    const gen = detailGenRef.current;
    const startAssetId = detail.assetId;
    writeSeqRef.current += 1;
    setSaving(true);
    try {
      const next = await updateStandardDraft(detail.assetId, {
        expectedRevision: detail.revision,
        name,
        payload,
      });
      onChanged();
      if (
        gen !== detailGenRef.current ||
        !openRef.current ||
        viewAssetIdRef.current !== startAssetId
      ) {
        return;
      }
      // 保存只更新详情，不替换 `mode.row`：详情接口的 `hasDraft` 不可靠，
      // 列表行以父级重拉结果为准。
      detailRef.current = next;
      setDetail(next);
      syncEditForm(next);
      setSelectedVersion(null);
      message.success(tRef.current('common.operationSuccessful'));
    } catch (err: unknown) {
      if (
        gen !== detailGenRef.current ||
        !openRef.current ||
        viewAssetIdRef.current !== startAssetId
      ) {
        return;
      }
      showActionError(err);
    } finally {
      if (
        gen === detailGenRef.current &&
        openRef.current &&
        viewAssetIdRef.current === startAssetId
      ) {
        setSaving(false);
      }
    }
  };

  const runAction = async (
    kind: 'publish' | 'retire' | 'delete' | 'newDraft',
    request: () => Promise<DgStandardAssetDetail | void>,
  ) => {
    if (refreshing) return;
    const gen = detailGenRef.current;
    const startAssetId = detailRef.current?.assetId ?? viewAssetIdRef.current;
    writeSeqRef.current += 1;
    setAction(kind);
    try {
      const next = await request();
      onChanged();
      if (
        gen !== detailGenRef.current ||
        !openRef.current ||
        viewAssetIdRef.current !== startAssetId
      ) {
        return;
      }
      if (kind === 'delete') {
        message.success(tRef.current('common.operationSuccessful'));
        onClose();
        return;
      }
      if (next) {
        const assetChanged = detailRef.current?.assetId !== next.assetId;
        detailRef.current = next;
        setDetail(next);
        syncEditForm(next);
        setSelectedVersion(null);
        if (assetChanged) {
          // 只有资产 id 变化（如新建草稿）才替换父级列表行；发布/退役
          // 只更新详情，避免详情接口不可靠的 `hasDraft` 污染列表行。
          onSelectRow(toStandardAssetItem(next));
        } else {
          // 同一资产的行内更新不会触发 mode effect（依赖值未变），这里主动同步版本历史。
          void loadVersions(next.kind, next.code);
        }
      }
      message.success(tRef.current('common.operationSuccessful'));
    } catch (err: unknown) {
      if (
        gen !== detailGenRef.current ||
        !openRef.current ||
        viewAssetIdRef.current !== startAssetId
      ) {
        return;
      }
      showActionError(err);
    } finally {
      if (
        gen === detailGenRef.current &&
        openRef.current &&
        viewAssetIdRef.current === startAssetId
      ) {
        setAction(null);
      }
    }
  };

  const confirmAction = (
    kind: 'publish' | 'retire' | 'delete',
    titleKey: string,
    request: (target: DgStandardAssetDetail) => Promise<DgStandardAssetDetail | void>,
    danger = false,
  ) => {
    if (!detail || action || refreshing) return;
    const target = detail;
    // 弹窗打开期间可能切换了查看对象：确认时再校验代际（含 render 期快照）。
    const startGen = detailGenRef.current;
    Modal.confirm({
      title: tRef.current(titleKey),
      content: target.name,
      okText: tRef.current(kind === 'delete' ? 'common.delete' : 'common.confirm'),
      cancelText: tRef.current('common.cancel'),
      okButtonProps: danger ? { danger: true } : undefined,
      onOk: () => {
        if (
          startGen !== detailGenRef.current ||
          !openRef.current ||
          viewAssetIdRef.current !== target.assetId
        ) {
          return;
        }
        return runAction(kind, () => request(target));
      },
    });
  };

  const handleNewDraft = () => {
    if (!detail || action || refreshing) return;
    // 新建草稿以「当前查看版本」为 base：选中历史版本时克隆历史，不被草稿目标影响。
    const base =
      selectedVersion === null
        ? detail
        : versions.find((v) => v.version === selectedVersion) ?? detail;
    void runAction('newDraft', () =>
      createStandardDraft({
        kind: base.kind,
        code: base.code,
        name: base.name,
        baseVersion: base.version,
      }),
    );
  };

  /**
   * 「编辑草稿」：列表行已标记 `hasDraft` 时，带 `keyword=code` 精确检索
   * 草稿行（api 已支持），不再只靠 pageSize=100 后手动匹配。
   * 找不到说明列表已过期，提示并让父级重拉。
   */
  const handleEditDraft = async () => {
    if (!detail || action || refreshing || draftLookup) return;
    const gen = detailGenRef.current;
    const startAssetId = detail.assetId;
    const { kind, code } = detail;
    setDraftLookup(true);
    try {
      const result = await listStandardAssets({
        kind,
        status: 'draft',
        includeDrafts: true,
        keyword: code,
        page: 1,
        pageSize: 100,
      });
      if (
        gen !== detailGenRef.current ||
        !openRef.current ||
        viewAssetIdRef.current !== startAssetId
      ) {
        return;
      }
      const draft = result.items.find((item) => item.code === code);
      if (draft) {
        if (draft.assetId !== startAssetId) {
          onSelectRow(draft);
        } else {
          // 同一行被列表判定为草稿但详情过期：直接重载详情，不再替换行。
          // loadDetail 会递增详情代际，finally 的代际守卫不再成立，
          // 必须在这里显式复位，否则「编辑草稿」按钮永久 disabled。
          setDraftLookup(false);
          await loadDetail(draft.assetId);
        }
        return;
      }
      message.warning(tRef.current('dataGovernance.standardAssets.draftMissing'));
      onChanged();
    } catch (err: unknown) {
      if (
        gen !== detailGenRef.current ||
        !openRef.current ||
        viewAssetIdRef.current !== startAssetId
      ) {
        return;
      }
      message.error(
        loadErrorText(err, 'dataGovernance.standardAssets.loadFailed', tRef.current),
      );
    } finally {
      if (
        gen === detailGenRef.current &&
        openRef.current &&
        viewAssetIdRef.current === startAssetId
      ) {
        setDraftLookup(false);
      }
    }
  };

  // 当前展示对象：选中历史版本时只读展示历史，不改变草稿编辑目标。
  const displayed =
    selectedVersion === null
      ? detail
      : versions.find((version) => version.version === selectedVersion) ?? detail;

  /** 选中的历史版本不是当前代表行 → 只读，且禁止作用于当前行的动作。 */
  const viewingHistory =
    selectedVersion !== null && selectedVersion !== detail?.version;

  // 可编辑 = 管理员 + 草稿 + 当前查看的就是详情版本（未切到历史版本）。
  const editing =
    isTenantAdmin &&
    Boolean(detail) &&
    detail?.status === 'draft' &&
    !viewingHistory;

  // 未保存编辑：发布/退役/删除必须禁用（保存按钮保持可用）。
  const dirty = detail
    ? isDraftDirty(
        { name: detail.name, payload: detail.payload },
        { name: editName, payload: editPayload },
      )
    : false;

  const isCreateForm = mode.type === 'create' && !detail;

  const title = isCreateForm
    ? t('dataGovernance.standardAssets.create')
    : detail?.name || t('dataGovernance.standardAssets.title');

  const statusText = (status: DgStandardAssetStatus) =>
    t(`dataGovernance.standardAssets.status.${status}`);

  /** disabled 按钮外层包 span，保证 Tooltip 在禁用态也能显示。 */
  const guardButton = (
    disabled: boolean,
    tooltip: string | undefined,
    button: ReactNode,
  ): ReactNode =>
    disabled && tooltip ? (
      <Tooltip title={tooltip}>
        <span style={{ display: 'inline-block' }}>{button}</span>
      </Tooltip>
    ) : (
      button
    );

  const renderCreate = () => (
    <Space direction="vertical" size={16} style={{ width: '100%' }}>
      <Form layout="vertical" size="small">
        <Form.Item label={t('dataGovernance.standardAssets.kindLabel')} required>
          <Select
            value={createKind}
            disabled={saving}
            onChange={(value: DgStandardAssetKind) => {
              setCreateKind(value);
              // 字段随 kind 变化，切换时清空旧 payload，避免提交无关字段。
              setCreatePayload({});
            }}
            options={KINDS.map((value) => ({ value, label: t(kindLabelKey(value)) }))}
          />
        </Form.Item>
        <Form.Item label={t('dataGovernance.standardAssets.code')} required>
          <Input
            value={createCode}
            disabled={saving}
            onChange={(event) => setCreateCode(event.target.value)}
          />
        </Form.Item>
        <Form.Item label={t('dataGovernance.standardAssets.name')} required>
          <Input
            value={createName}
            disabled={saving}
            onChange={(event) => setCreateName(event.target.value)}
          />
        </Form.Item>
      </Form>

      <KindFields
        kind={createKind}
        payload={createPayload}
        disabled={saving}
        onChange={setCreatePayload}
      />

      <Space>
        <Button type="primary" loading={saving} onClick={() => void handleCreate()}>
          {t('dataGovernance.standardAssets.save')}
        </Button>
        <Button onClick={onClose}>{t('common.cancel')}</Button>
      </Space>
    </Space>
  );

  const renderView = (shown: DgStandardAssetDetail) => {
    const saveFirst = t('dataGovernance.standardAssets.saveFirst');
    const readonlyHint = t('dataGovernance.standardAssets.readonlyHint');
    const historyHint = t('dataGovernance.standardAssets.historyReadonlyHint');
    // 只有「仍可编辑的草稿」才需要先保存：非 draft 状态没有保存入口，
    // 不能再被 dirty 卡住（否则会出现禁用且无出路的死路）。
    const dirtyBlocksActions = dirty && editing;
    const publishDisabled =
      dirtyBlocksActions ||
      saving ||
      refreshing ||
      (Boolean(action) && action !== 'publish');
    const deleteDisabled =
      dirtyBlocksActions ||
      saving ||
      refreshing ||
      (Boolean(action) && action !== 'delete');
    const retireDisabled =
      dirtyBlocksActions ||
      saving ||
      refreshing ||
      viewingHistory ||
      (Boolean(action) && action !== 'retire');
    const retireTooltip = viewingHistory
      ? historyHint
      : dirtyBlocksActions
        ? saveFirst
        : undefined;
    // 作用于「当前行」的动作：历史版本只读 / 其它动作、冲突刷新或草稿定位进行中都禁用。
    const rowActionDisabled =
      viewingHistory || Boolean(action) || draftLookup || refreshing;
    const rowActionTooltip = viewingHistory ? historyHint : undefined;

    return (
      <Space direction="vertical" size={16} style={{ width: '100%' }}>
        {refreshError && (
          <Alert
            type="error"
            showIcon
            message={refreshError}
            action={
              <Button
                size="small"
                icon={<ReloadOutlined />}
                loading={refreshing}
                onClick={() => {
                  if (detail) void refreshAfterConflict(detail.assetId);
                }}
              >
                {t('common.retry')}
              </Button>
            }
          />
        )}

        <Descriptions column={2} size="small">
          <Descriptions.Item label={t('common.status')}>
            <Tag color={STATUS_COLORS[shown.status]}>{statusText(shown.status)}</Tag>
          </Descriptions.Item>
          <Descriptions.Item label={t('dataGovernance.standardAssets.version')}>
            {`v${shown.version}`}
          </Descriptions.Item>
          <Descriptions.Item label={t('dataGovernance.standardAssets.code')}>
            <code>{shown.code}</code>
          </Descriptions.Item>
          <Descriptions.Item label={t('dataGovernance.standardAssets.updatedAt')}>
            {shown.updatedAt ? formatDateTime(shown.updatedAt) : '—'}
          </Descriptions.Item>
        </Descriptions>

        {/* 草稿标记只认列表行：详情接口的 hasDraft 不可靠。 */}
        {mode.type === 'view' && selectedVersion === null && mode.row.hasDraft && (
          <Tag color="gold">{t('dataGovernance.standardAssets.draft')}</Tag>
        )}

        {mode.type === 'view' &&
          (versionsError ? (
            // 版本历史失败必须显式可见 + 可重试；其余详情仍可正常使用。
            <Alert
              type="warning"
              showIcon
              message={versionsError}
              action={
                <Button
                  size="small"
                  icon={<ReloadOutlined />}
                  loading={versionsLoading}
                  onClick={() => {
                    if (detail) void loadVersions(detail.kind, detail.code);
                  }}
                >
                  {t('common.retry')}
                </Button>
              }
            />
          ) : versions.length > 0 ? (
            <Space size={8} wrap>
              <span>{t('dataGovernance.standardAssets.versionHistory')}</span>
              <Select
                allowClear
                value={selectedVersion ?? undefined}
                loading={versionsLoading}
                placeholder={t('dataGovernance.standardAssets.version')}
                style={{ width: 240 }}
                onChange={(value: number | undefined) => setSelectedVersion(value ?? null)}
                options={versions.map((version) => ({
                  value: version.version,
                  label: `${t('dataGovernance.standardAssets.version')} ${version.version} · ${statusText(version.status)}`,
                }))}
              />
            </Space>
          ) : null)}

        {viewingHistory && (
          <Alert type="info" showIcon message={historyHint} />
        )}
        {!editing && !viewingHistory && (
          <Alert type="info" showIcon message={readonlyHint} />
        )}

        <Form layout="vertical" size="small" disabled={!editing}>
          <Form.Item label={t('dataGovernance.standardAssets.name')} required={editing}>
            <Input
              value={editing ? editName : shown.name}
              onChange={(event) => setEditName(event.target.value)}
            />
          </Form.Item>
        </Form>

        <KindFields
          kind={shown.kind}
          payload={editing ? editPayload : shown.payload}
          disabled={!editing}
          onChange={editing ? setEditPayload : undefined}
        />

        <Space wrap>
          {editing && (
            <Button
              type="primary"
              loading={saving}
              disabled={refreshing}
              onClick={() => void handleSave()}
            >
              {t('dataGovernance.standardAssets.save')}
            </Button>
          )}
          {editing &&
            guardButton(
              publishDisabled,
              dirty ? saveFirst : undefined,
              <Button
                loading={action === 'publish'}
                disabled={publishDisabled}
                onClick={() =>
                  confirmAction('publish', 'dataGovernance.standardAssets.publish', (target) =>
                    publishStandardAsset(target.assetId, target.revision),
                  )
                }
              >
                {t('dataGovernance.standardAssets.publish')}
              </Button>,
            )}
          {editing &&
            guardButton(
              deleteDisabled,
              dirty ? saveFirst : undefined,
              <Button
                danger
                loading={action === 'delete'}
                disabled={deleteDisabled}
                onClick={() =>
                  confirmAction(
                    'delete',
                    'dataGovernance.standardAssets.deleteDraft',
                    (target) => deleteStandardDraft(target.assetId),
                    true,
                  )
                }
              >
                {t('dataGovernance.standardAssets.deleteDraft')}
              </Button>,
            )}
          {isTenantAdmin &&
            detail?.status === 'published' &&
            guardButton(
              retireDisabled,
              retireTooltip,
              <Button
                loading={action === 'retire'}
                disabled={retireDisabled}
                onClick={() =>
                  confirmAction('retire', 'dataGovernance.standardAssets.retire', (target) =>
                    retireStandardAsset(target.assetId),
                  )
                }
              >
                {t('dataGovernance.standardAssets.retire')}
              </Button>,
            )}
          {/* hasDraft 只认列表行：有草稿时改为「编辑草稿」，避免重复新建。 */}
          {isTenantAdmin &&
            detail &&
            detail.status !== 'draft' &&
            (mode.type === 'view' && mode.row.hasDraft ? (
              guardButton(
                rowActionDisabled,
                rowActionTooltip,
                <Button
                  loading={draftLookup}
                  disabled={rowActionDisabled}
                  onClick={() => void handleEditDraft()}
                >
                  {t('dataGovernance.standardAssets.editDraft')}
                </Button>,
              )
            ) : (
              guardButton(
                rowActionDisabled,
                rowActionTooltip,
                <Button
                  loading={action === 'newDraft'}
                  disabled={rowActionDisabled}
                  onClick={handleNewDraft}
                >
                  {t('dataGovernance.standardAssets.newDraft')}
                </Button>,
              )
            ))}
        </Space>
      </Space>
    );
  };

  return (
    <Drawer open={open} onClose={onClose} width={720} destroyOnHidden title={title}>
      {isCreateForm ? (
        renderCreate()
      ) : loading ? (
        <Skeleton active />
      ) : error ? (
        <Alert
          type="error"
          showIcon
          message={error}
          action={
            <Button
              size="small"
              icon={<ReloadOutlined />}
              onClick={() => {
                if (mode.type === 'view') void loadDetail(mode.row.assetId);
              }}
            >
              {t('common.retry')}
            </Button>
          }
        />
      ) : !displayed ? (
        <Empty description={t('dataGovernance.standardAssets.empty')} />
      ) : (
        renderView(displayed)
      )}
    </Drawer>
  );
}
