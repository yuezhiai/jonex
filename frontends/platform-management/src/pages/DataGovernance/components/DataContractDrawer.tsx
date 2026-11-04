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
  Segmented,
  Select,
  Skeleton,
  Space,
  Tag,
  Tooltip,
  message,
} from 'antd';
import { DownloadOutlined, ReloadOutlined } from '@ant-design/icons';
import { useTranslation } from 'react-i18next';
import { formatDateTime } from '@jonex/shared-lib';
import { getShellContext } from '@jonex/shell-sdk';
import {
  createDataContractDraft,
  deleteDataContractDraft,
  exportDataContract,
  getDataContract,
  listDataContractVersions,
  listDataContracts,
  publishDataContract,
  retireDataContract,
  updateDataContractDraft,
} from '@/api/dataContracts';
import type {
  DgContractFormat,
  DgDataContractDetail,
  DgDataContractItem,
  DgDataContractStatus,
} from '../types';
import {
  formatContractContent,
  isConflictError,
  statusColor,
  statusLabelKey,
  toDataContractItem,
  validateJsonContent,
} from '../contractUtils';
import { loadErrorText } from '../standardUtils';
import { actionErrorMessage, errorMeta } from '../utils';

/** 抽屉模式：目前只有「查看某条列表行」；新建/导入在列表页的弹窗里完成。 */
export interface DataContractDrawerMode {
  type: 'view';
  row: DgDataContractItem;
}

interface Props {
  open: boolean;
  mode: DataContractDrawerMode | null;
  onClose: () => void;
  /** 服务端写入成功后通知父级重拉列表。 */
  onChanged: () => void;
  /** 写操作换出新行（如新建草稿）时替换父级的列表行。 */
  onSelectRow: (row: DgDataContractItem) => void;
}

/** 触发浏览器下载（导出内容由后端给出，前端只负责落盘）。 */
function downloadText(content: string, filename: string, format: DgContractFormat): void {
  const blob = new Blob([content], {
    type: format === 'yaml' ? 'application/yaml;charset=utf-8' : 'application/json;charset=utf-8',
  });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = filename;
  anchor.rel = 'noopener';
  // Firefox 要求锚点在文档里才会触发下载；点击后再释放 URL（同步 revoke 会让
  // 部分浏览器拿不到内容，setTimeout(0) 让下载先启动）。
  document.body.appendChild(anchor);
  anchor.click();
  setTimeout(() => {
    URL.revokeObjectURL(url);
    anchor.remove();
  }, 0);
}

/**
 * 1003 既可能是「该数据源已有草稿」，也可能是 revision 冲突。
 * 后端消息为「该数据源已有草稿」，只有前者需要引导用户去编辑草稿行。
 */
function isDraftExistsConflict(err: unknown): boolean {
  if (!isConflictError(err)) return false;
  const text = errorMeta(err).message ?? '';
  // 「该数据源已有草稿」= uq_dc_draft；排除「…发布/删除…草稿…」这类
  // 其它动作冲突，它们仍按乐观锁冲突处理。
  return (
    text.includes('草稿') && !text.includes('发布') && !text.includes('删除')
  );
}

/**
 * 数据合同抽屉。
 *
 * 关键语义：
 * - JSON 可编辑（草稿 + 管理员）；YAML 只读，内容来自 `exportDataContract` 的
 *   后端序列化（前端不实现 YAML 编辑）；
 * - 每次写操作成功后就地刷新详情并通知父级重拉列表；
 * - 所有异步回写都过代际守卫：抽屉关闭、切换对象、重新加载都会递增代际，
 *   旧响应一律丢弃；
 * - 冲突（1003/409）后刷新 revision 并保留未保存的编辑值。
 */
export default function DataContractDrawer({
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

  const openRef = useRef(open);
  openRef.current = open;

  const contractId = mode?.row.contractId ?? null;
  const viewDataSourceId = mode?.row.dataSourceId ?? null;
  const contractIdRef = useRef<string | null>(contractId);
  contractIdRef.current = contractId;

  const isTenantAdmin = getShellContext()?.getCurrentUser?.()?.isTenantAdmin === true;

  const [detail, setDetail] = useState<DgDataContractDetail | null>(null);
  const [versions, setVersions] = useState<DgDataContractDetail[]>([]);
  const [versionsLoading, setVersionsLoading] = useState(false);
  const [versionsError, setVersionsError] = useState<string | null>(null);
  const [selectedVersion, setSelectedVersion] = useState<number | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [refreshError, setRefreshError] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [draftLookup, setDraftLookup] = useState(false);
  const [saving, setSaving] = useState(false);
  const [exporting, setExporting] = useState(false);
  const [action, setAction] = useState<'publish' | 'retire' | 'delete' | 'newDraft' | null>(
    null,
  );

  // JSON 编辑器
  const [format, setFormat] = useState<DgContractFormat>('json');
  const [editName, setEditName] = useState('');
  const [editText, setEditText] = useState('');

  // YAML 只读预览（后端导出）
  const [yamlText, setYamlText] = useState('');
  const [yamlLoading, setYamlLoading] = useState(false);
  const [yamlError, setYamlError] = useState<string | null>(null);
  const [yamlNonce, setYamlNonce] = useState(0);

  const detailGenRef = useRef(0);
  const versionsSeq = useRef(0);
  const writeSeqRef = useRef(0);
  const yamlSeq = useRef(0);

  const detailRef = useRef<DgDataContractDetail | null>(detail);
  detailRef.current = detail;

  const syncEditForm = useCallback((next: DgDataContractDetail) => {
    setEditName(next.name);
    setEditText(formatContractContent(next.payload, 'json'));
  }, []);

  const loadDetail = useCallback(
    async (id: string) => {
      const gen = ++detailGenRef.current;
      setLoading(true);
      setError(null);
      setRefreshError(null);
      try {
        const next = await getDataContract(id);
        if (
          gen !== detailGenRef.current ||
          !openRef.current ||
          contractIdRef.current !== id
        ) {
          return;
        }
        detailRef.current = next;
        setDetail(next);
        syncEditForm(next);
      } catch (err: unknown) {
        if (
          gen !== detailGenRef.current ||
          !openRef.current ||
          contractIdRef.current !== id
        ) {
          return;
        }
        setDetail(null);
        setError(
          loadErrorText(err, 'dataGovernance.dataContracts.loadFailed', tRef.current),
        );
      } finally {
        if (
          gen === detailGenRef.current &&
          openRef.current &&
          contractIdRef.current === id
        ) {
          setLoading(false);
        }
      }
    },
    [syncEditForm],
  );

  const loadVersions = useCallback(async (dataSourceId: string) => {
    if (!dataSourceId) return;
    const my = ++versionsSeq.current;
    setVersionsLoading(true);
    setVersionsError(null);
    try {
      const next = await listDataContractVersions(dataSourceId);
      if (my !== versionsSeq.current) return;
      setVersions(next);
    } catch (err: unknown) {
      if (my !== versionsSeq.current) return;
      // 版本历史是辅助信息：失败显式提示 + 重试，但不挡住详情与编辑。
      setVersions([]);
      setVersionsError(
        loadErrorText(
          err,
          'dataGovernance.dataContracts.versionsFailed',
          tRef.current,
        ),
      );
    } finally {
      if (my === versionsSeq.current) setVersionsLoading(false);
    }
  }, []);

  // 开关抽屉 / 切换查看对象：作废在途请求并重置本地状态。
  useEffect(() => {
    detailGenRef.current += 1;
    versionsSeq.current += 1;
    yamlSeq.current += 1;
    setSelectedVersion(null);
    setVersions([]);
    setVersionsLoading(false);
    setVersionsError(null);
    setRefreshError(null);
    setRefreshing(false);
    setDraftLookup(false);
    setError(null);
    setSaving(false);
    setExporting(false);
    setAction(null);
    setFormat('json');
    setYamlText('');
    setYamlLoading(false);
    setYamlError(null);

    if (!open || !contractId) {
      setDetail(null);
      setLoading(false);
      return;
    }

    // 写操作刚落地时已持有同一详情：不重复请求，避免闪一次骨架屏。
    if (detailRef.current?.contractId === contractId) {
      setLoading(false);
      void loadVersions(detailRef.current.dataSourceId || viewDataSourceId || '');
      return;
    }

    setDetail(null);
    void loadDetail(contractId);
    void loadVersions(viewDataSourceId || '');
  }, [open, contractId, viewDataSourceId, loadDetail, loadVersions]);

  /**
   * 冲突后刷新详情：仍是草稿时只更新详情/revision，**不回填编辑表单**
   * （保留用户输入）；若服务端已不是草稿，同步服务端内容并告知修改已丢弃。
   */
  const refreshAfterConflict = useCallback(
    async (id: string) => {
      const gen = detailGenRef.current;
      const writeSeq = writeSeqRef.current;
      setRefreshing(true);
      setRefreshError(null);
      try {
        const next = await getDataContract(id);
        if (
          gen !== detailGenRef.current ||
          !openRef.current ||
          contractIdRef.current !== id
        ) {
          return;
        }
        if (writeSeq !== writeSeqRef.current) return;
        const current = detailRef.current;
        if (current && current.contractId === id && next.revision < current.revision) return;
        detailRef.current = next;
        setDetail(next);
        if (next.status !== 'draft') {
          syncEditForm(next);
          message.warning(
            tRef.current('dataGovernance.dataContracts.draftPublishedElsewhere'),
          );
        }
        setYamlNonce((value) => value + 1);
        void loadVersions(next.dataSourceId);
      } catch (err: unknown) {
        if (
          gen !== detailGenRef.current ||
          !openRef.current ||
          contractIdRef.current !== id
        ) {
          return;
        }
        setRefreshError(
          loadErrorText(
            err,
            'dataGovernance.dataContracts.refreshFailed',
            tRef.current,
          ),
        );
      } finally {
        if (
          gen === detailGenRef.current &&
          openRef.current &&
          contractIdRef.current === id
        ) {
          setRefreshing(false);
        }
      }
    },
    [loadVersions, syncEditForm],
  );

  const showActionError = useCallback(
    (err: unknown, options: { refresh?: boolean } = {}) => {
      if (isDraftExistsConflict(err)) {
        // 「已有草稿」不是乐观锁冲突：引导用户刷新后编辑草稿行，
        // 不要把用户带进 revision 冲突的刷新流程。
        message.error(tRef.current('dataGovernance.dataContracts.draftConflict'));
        onChanged();
        return;
      }
      if (isConflictError(err)) {
        message.error(tRef.current('dataGovernance.dataContracts.conflict'));
        onChanged();
        const id = detailRef.current?.contractId;
        if (options.refresh !== false && id) void refreshAfterConflict(id);
        return;
      }
      const text = actionErrorMessage(err, {
        forbiddenKey: 'dataGovernance.standards.errorForbidden',
        fallbackKey: 'dataGovernance.dataContracts.actionFailed',
      });
      message.error('key' in text ? tRef.current(text.key) : text.message);
    },
    [onChanged, refreshAfterConflict],
  );

  const runAction = async (
    kind: 'publish' | 'retire' | 'delete' | 'newDraft',
    request: () => Promise<DgDataContractDetail | void>,
  ) => {
    if (refreshing) return;
    const gen = detailGenRef.current;
    const startId = detailRef.current?.contractId ?? contractIdRef.current;
    writeSeqRef.current += 1;
    setAction(kind);
    try {
      const next = await request();
      // 服务端已落库：即使抽屉已关闭/切走也要让列表刷新。
      onChanged();
      if (
        gen !== detailGenRef.current ||
        !openRef.current ||
        contractIdRef.current !== startId
      ) {
        return;
      }
      if (kind === 'delete') {
        message.success(tRef.current('common.operationSuccessful'));
        onClose();
        return;
      }
      if (next) {
        const contractChanged = detailRef.current?.contractId !== next.contractId;
        detailRef.current = next;
        setDetail(next);
        syncEditForm(next);
        setSelectedVersion(null);
        setYamlNonce((value) => value + 1);
        if (contractChanged) {
          // 新草稿是新行：替换父级列表行；发布/退役只更新详情。
          onSelectRow(toDataContractItem(next));
        } else {
          void loadVersions(next.dataSourceId);
        }
      }
      message.success(tRef.current('common.operationSuccessful'));
    } catch (err: unknown) {
      if (
        gen !== detailGenRef.current ||
        !openRef.current ||
        contractIdRef.current !== startId
      ) {
        return;
      }
      showActionError(err);
    } finally {
      if (
        gen === detailGenRef.current &&
        openRef.current &&
        contractIdRef.current === startId
      ) {
        setAction(null);
      }
    }
  };

  const confirmAction = (
    kind: 'publish' | 'retire' | 'delete',
    titleKey: string,
    request: (target: DgDataContractDetail) => Promise<DgDataContractDetail | void>,
    danger = false,
  ) => {
    if (!detail || action || refreshing) return;
    const target = detail;
    // 弹窗打开期间可能切换了查看对象：确认时再校验代际。
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
          contractIdRef.current !== target.contractId
        ) {
          return;
        }
        return runAction(kind, () => request(target));
      },
    });
  };

  const handleSave = async () => {
    if (!detail || !editing || saving || refreshing) return;
    if (format !== 'json') return;

    const validation = validateJsonContent(editText);
    if (!validation.ok) {
      message.error(tRef.current(validation.error ?? 'dataGovernance.dataContracts.contentRequired'));
      return;
    }
    const name = editName.trim();
    if (!name) {
      message.error(tRef.current('dataGovernance.dataContracts.nameRequired'));
      return;
    }

    let payload: Record<string, unknown>;
    try {
      payload = JSON.parse(editText) as Record<string, unknown>;
    } catch {
      // `validateJsonContent` 已覆盖语法错误，这里只是防御性兜底。
      message.error(tRef.current('dataGovernance.dataContracts.contentInvalidJson'));
      return;
    }

    const gen = detailGenRef.current;
    const startId = detail.contractId;
    writeSeqRef.current += 1;
    setSaving(true);
    try {
      const next = await updateDataContractDraft(detail.contractId, {
        expectedRevision: detail.revision,
        name,
        payload,
      });
      onChanged();
      if (
        gen !== detailGenRef.current ||
        !openRef.current ||
        contractIdRef.current !== startId
      ) {
        return;
      }
      detailRef.current = next;
      setDetail(next);
      syncEditForm(next);
      setSelectedVersion(null);
      setYamlNonce((value) => value + 1);
      void loadVersions(next.dataSourceId);
      message.success(tRef.current('common.operationSuccessful'));
    } catch (err: unknown) {
      if (
        gen !== detailGenRef.current ||
        !openRef.current ||
        contractIdRef.current !== startId
      ) {
        return;
      }
      // 冲突刷新保留编辑值；其余错误只提示。
      showActionError(err);
    } finally {
      if (
        gen === detailGenRef.current &&
        openRef.current &&
        contractIdRef.current === startId
      ) {
        setSaving(false);
      }
    }
  };

  const handleNewDraft = () => {
    if (!detail || action || refreshing || draftLookup) return;
    // 以当前查看版本为 base：选中历史版本时克隆历史。
    const base =
      selectedVersion === null
        ? detail
        : versions.find((version) => version.version === selectedVersion) ?? detail;
    void runAction('newDraft', () =>
      createDataContractDraft({
        dataSourceId: base.dataSourceId,
        baseVersion: base.version,
      }),
    );
  };

  /**
   * 「编辑草稿」：列表聚合时 published 行优先，「已发布 + 有草稿」只会暴露
   * published 行（`hasDraft=true`）。此时按数据源 id 精确检索草稿行并切换过去，
   * 不再发起注定撞 `uq_dc_draft` 的「新建草稿」。
   *
   * 找不到说明列表已过期：提示并让父级重拉。
   */
  const handleEditDraft = async () => {
    if (!detail || action || refreshing || draftLookup) return;
    const gen = detailGenRef.current;
    const startId = detail.contractId;
    const dataSourceId = detail.dataSourceId;
    setDraftLookup(true);
    try {
      const result = await listDataContracts({
        status: 'draft',
        includeDrafts: true,
        keyword: dataSourceId,
        page: 1,
        pageSize: 100,
      });
      if (
        gen !== detailGenRef.current ||
        !openRef.current ||
        contractIdRef.current !== startId
      ) {
        return;
      }
      const draft = result.items.find(
        (item) => item.dataSourceId === dataSourceId && item.status === 'draft',
      );
      if (draft) {
        if (draft.contractId !== startId) {
          onSelectRow(draft);
        } else {
          // 同一行被列表判定为有草稿但详情过期：直接重载详情。
          // loadDetail 会递增详情代际，finally 的代际守卫不再成立，
          // 必须在这里显式复位，否则「编辑草稿」按钮永久 disabled。
          setDraftLookup(false);
          await loadDetail(draft.contractId);
        }
        return;
      }
      message.warning(tRef.current('dataGovernance.dataContracts.draftMissing'));
      onChanged();
    } catch (err: unknown) {
      if (
        gen !== detailGenRef.current ||
        !openRef.current ||
        contractIdRef.current !== startId
      ) {
        return;
      }
      message.error(
        loadErrorText(err, 'dataGovernance.dataContracts.loadFailed', tRef.current),
      );
    } finally {
      if (
        gen === detailGenRef.current &&
        openRef.current &&
        contractIdRef.current === startId
      ) {
        setDraftLookup(false);
      }
    }
  };

  const handleExport = async () => {
    if (!displayed || exporting) return;
    const target = displayed;
    const startId = target.contractId;
    setExporting(true);
    try {
      const result = await exportDataContract(target.contractId, format);
      // 会话守卫：抽屉已关闭或已切到别的合同时，放弃本次下载（M8）。
      if (!openRef.current || contractIdRef.current !== startId) return;
      const fallback = `contract-${target.contractId}.${result.format}`;
      downloadText(result.content, result.filename || fallback, result.format);
      message.success(tRef.current('common.operationSuccessful'));
    } catch (err: unknown) {
      message.error(
        loadErrorText(
          err,
          'dataGovernance.dataContracts.exportFailed',
          tRef.current,
        ),
      );
    } finally {
      setExporting(false);
    }
  };

  // 当前展示对象：选中历史版本时只读展示历史，不改变草稿编辑目标。
  const displayed =
    selectedVersion === null
      ? detail
      : versions.find((version) => version.version === selectedVersion) ?? detail;

  const viewingHistory =
    selectedVersion !== null && selectedVersion !== detail?.version;

  // 可编辑 = 管理员 + 草稿 + 未切到历史版本；YAML 只读由渲染层限制
  // （正文与名称禁用、保存按钮只在 JSON 模式出现、handleSave 也校验 format）。
  const editing =
    isTenantAdmin &&
    Boolean(detail) &&
    detail?.status === 'draft' &&
    !viewingHistory;

  // 未保存编辑：两侧都按格式化后的 JSON 比较。
  const dirty = detail
    ? editName.trim() !== detail.name.trim() ||
      editText !== formatContractContent(detail.payload, 'json')
    : false;

  // YAML 只读预览：按当前展示对象的 id 拉后端序列化内容。
  const yamlTargetId =
    open && format === 'yaml' && displayed ? displayed.contractId : null;

  useEffect(() => {
    if (!yamlTargetId) {
      yamlSeq.current += 1;
      setYamlText('');
      setYamlLoading(false);
      setYamlError(null);
      return;
    }
    const my = ++yamlSeq.current;
    let cancelled = false;
    setYamlLoading(true);
    setYamlError(null);
    setYamlText('');
    exportDataContract(yamlTargetId, 'yaml')
      .then((result) => {
        if (cancelled || my !== yamlSeq.current || !openRef.current) return;
        setYamlText(result.content);
      })
      .catch((err: unknown) => {
        if (cancelled || my !== yamlSeq.current || !openRef.current) return;
        setYamlText('');
        setYamlError(
          loadErrorText(
            err,
            'dataGovernance.dataContracts.yamlLoadFailed',
            tRef.current,
          ),
        );
      })
      .finally(() => {
        if (!cancelled && my === yamlSeq.current) setYamlLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [yamlTargetId, yamlNonce]);

  const title = detail?.name || t('dataGovernance.dataContracts.title');

  const statusText = (status: DgDataContractStatus) => t(statusLabelKey(status));

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

  const renderView = (shown: DgDataContractDetail) => {
    const saveFirst = t('dataGovernance.dataContracts.saveFirst');
    const readonlyHint = t('dataGovernance.dataContracts.readonlyHint');
    const historyHint = t('dataGovernance.dataContracts.historyReadonlyHint');
    const yamlHint = t('dataGovernance.dataContracts.yamlReadonly');
    // 只有「仍可编辑的草稿」才需要先保存：非 draft 状态没有保存入口，
    // 不能再被 dirty 卡住。
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
    const rowActionDisabled =
      viewingHistory || Boolean(action) || draftLookup || refreshing;
    const rowActionTooltip = viewingHistory ? historyHint : undefined;

    const jsonReadonly = !editing;
    // YAML 模式整块只读（含合同名），避免出现「可改但无法保存」的死路。
    const nameEditable = editing && format === 'json';
    const editorText = viewingHistory
      ? formatContractContent(shown.payload, 'json')
      : editText;

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
                  if (detail) void refreshAfterConflict(detail.contractId);
                }}
              >
                {t('common.retry')}
              </Button>
            }
          />
        )}

        <Descriptions column={2} size="small">
          <Descriptions.Item label={t('common.status')}>
            <Tag color={statusColor(shown.status)}>{statusText(shown.status)}</Tag>
          </Descriptions.Item>
          <Descriptions.Item label={t('dataGovernance.dataContracts.version')}>
            {`v${shown.version}`}
          </Descriptions.Item>
          <Descriptions.Item label={t('dataGovernance.dataContracts.dataSource')}>
            {shown.dataSourceName || shown.dataSourceId || '—'}
          </Descriptions.Item>
          <Descriptions.Item label={t('dataGovernance.dataContracts.updatedAt')}>
            {shown.updatedAt ? formatDateTime(shown.updatedAt) : '—'}
          </Descriptions.Item>
        </Descriptions>

        {versionsError ? (
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
                  if (detail) void loadVersions(detail.dataSourceId);
                }}
              >
                {t('common.retry')}
              </Button>
            }
          />
        ) : versions.length > 0 ? (
          <Space size={8} wrap>
            <span>{t('dataGovernance.dataContracts.versionHistory')}</span>
            <Select
              allowClear
              value={selectedVersion ?? undefined}
              loading={versionsLoading}
              placeholder={t('dataGovernance.dataContracts.version')}
              style={{ width: 260 }}
              onChange={(value: number | undefined) => setSelectedVersion(value ?? null)}
              options={versions.map((version) => ({
                value: version.version,
                label: `${t('dataGovernance.dataContracts.version')} ${version.version} · ${statusText(version.status)}`,
              }))}
            />
          </Space>
        ) : null}

        {viewingHistory && <Alert type="info" showIcon message={historyHint} />}
        {!editing && !viewingHistory && (
          <Alert type="info" showIcon message={readonlyHint} />
        )}
        {format === 'yaml' && <Alert type="info" showIcon message={yamlHint} />}

        <Form layout="vertical" size="small">
          <Form.Item
            label={t('dataGovernance.dataContracts.name')}
            required={nameEditable}
          >
            <Input
              value={nameEditable ? editName : shown.name}
              disabled={!nameEditable}
              onChange={(event) => setEditName(event.target.value)}
            />
          </Form.Item>
          <Form.Item label={t('dataGovernance.dataContracts.format')}>
            <Segmented
              value={format}
              onChange={(value) => setFormat(value as DgContractFormat)}
              options={[
                { value: 'json', label: t('dataGovernance.dataContracts.json') },
                { value: 'yaml', label: t('dataGovernance.dataContracts.yaml') },
              ]}
            />
          </Form.Item>
        </Form>

        {format === 'json' ? (
          <Input.TextArea
            rows={16}
            value={editorText}
            readOnly={jsonReadonly}
            onChange={(event) => setEditText(event.target.value)}
            spellCheck={false}
          />
        ) : yamlError ? (
          <Alert
            type="error"
            showIcon
            message={yamlError}
            action={
              <Button
                size="small"
                icon={<ReloadOutlined />}
                loading={yamlLoading}
                onClick={() => setYamlNonce((value) => value + 1)}
              >
                {t('common.retry')}
              </Button>
            }
          />
        ) : yamlLoading ? (
          <Skeleton active paragraph={{ rows: 8 }} />
        ) : (
          <Input.TextArea
            rows={16}
            value={yamlText}
            readOnly
            spellCheck={false}
          />
        )}

        <Space wrap>
          {editing && format === 'json' && (
            <Button
              type="primary"
              loading={saving}
              disabled={refreshing}
              onClick={() => void handleSave()}
            >
              {t('dataGovernance.dataContracts.save')}
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
                  confirmAction(
                    'publish',
                    'dataGovernance.dataContracts.publish',
                    (target) =>
                      publishDataContract(target.contractId, target.revision),
                  )
                }
              >
                {t('dataGovernance.dataContracts.publish')}
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
                    'dataGovernance.dataContracts.deleteDraft',
                    (target) => deleteDataContractDraft(target.contractId),
                    true,
                  )
                }
              >
                {t('dataGovernance.dataContracts.deleteDraft')}
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
                  confirmAction(
                    'retire',
                    'dataGovernance.dataContracts.retire',
                    (target) => retireDataContract(target.contractId),
                  )
                }
              >
                {t('dataGovernance.dataContracts.retire')}
              </Button>,
            )}
          {/* hasDraft 只认列表行：已有草稿时改为「编辑草稿」，避免重复新建。 */}
          {isTenantAdmin &&
            detail &&
            detail.status !== 'draft' &&
            (mode?.row.hasDraft === true ? (
              guardButton(
                rowActionDisabled,
                rowActionTooltip,
                <Button
                  loading={draftLookup}
                  disabled={rowActionDisabled}
                  onClick={() => void handleEditDraft()}
                >
                  {t('dataGovernance.dataContracts.editDraft')}
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
                  {t('dataGovernance.dataContracts.newDraft')}
                </Button>,
              )
            ))}
          <Button
            icon={<DownloadOutlined />}
            loading={exporting}
            onClick={() => void handleExport()}
          >
            {t('dataGovernance.dataContracts.export')}
          </Button>
        </Space>
      </Space>
    );
  };

  return (
    <Drawer open={open} onClose={onClose} width={820} destroyOnHidden title={title}>
      {loading ? (
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
                if (contractId) void loadDetail(contractId);
              }}
            >
              {t('common.retry')}
            </Button>
          }
        />
      ) : !displayed ? (
        <Empty description={t('dataGovernance.dataContracts.empty')} />
      ) : (
        renderView(displayed)
      )}
    </Drawer>
  );
}
