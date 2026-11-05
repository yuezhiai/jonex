import React, { useState, useEffect, useRef, useCallback, useMemo } from 'react';
import { Modal, Upload, Select, Space, Button, Progress, Tag, Tooltip, message } from 'antd';
import type { UploadProps } from 'antd/es/upload';
import type { UploadFile } from 'antd';
import { PlusOutlined, CloudUploadOutlined, QuestionCircleOutlined, DeleteOutlined } from '@ant-design/icons';
import { useTranslation } from 'react-i18next';
import {
  uploadManualDocument,
  uploadDocumentVersion,
  confirmDocumentVersionUpload,
  discardDocumentVersionUpload,
  findDocumentByName,
  previewDocumentVersion,
  getFolderList,
} from '@/api/domainKnowledge';
import type { VersionPreviewResult } from '@/api/domainKnowledge';
import { ACCEPT_EXTENSIONS } from '@/constants/upload';
import type { FolderItem } from '@/types/domainKnowledge';
import { useQuota, fileSizeLimitBytes, formatMiB } from '@/hooks/useQuota';
import {
  runWithByteBudget,
  fingerprint,
  computeTotalProgress,
  validate,
  isDuplicateError,
  type UploadItem,
  type FileUploadStatus,
  type SkipReason,
} from '@/utils/batchUpload';

type VersionChoice = 'version' | 'new' | 'cancel';

export interface UploadModalProps {
  kbId: string;
  open: boolean;
  onClose: () => void;
  onSuccess?: () => void;
  /** 打开弹窗时默认选中的目标目录（'' = 全部文档/根目录） */
  defaultFolderId?: string;
}

const UploadModal: React.FC<UploadModalProps> = ({ kbId, open, onClose, onSuccess, defaultFolderId }) => {
  const { t } = useTranslation();

  // 配额视图：挂载即拉一次，每次打开弹窗重拉（打开时取最新上限）
  const { quotas, error: quotaError, getQuota, reload: reloadQuota } = useQuota(kbId);
  useEffect(() => {
    if (open) reloadQuota();
  }, [open, reloadQuota]);

  // 单次上传数量上限；quota 未就绪时为 null（不做前端拦截，最终以后端阻断为准）
  const uploadFileCountLimit = getQuota('uploadFileCountLimit')?.limit ?? null;

  const [selectedFolderId, setSelectedFolderId] = useState<string>('');
  const [folders, setFolders] = useState<FolderItem[]>([]);
  const [running, setRunning] = useState(false);

  // 批量上传状态：可变数据放 useRef，节流 flush 到 state（终态立即 flush）
  const itemsRef = useRef<UploadItem[]>([]);
  const [items, setItems] = useState<UploadItem[]>([]);
  const flushTimer = useRef<number | null>(null);
  const abortMapRef = useRef<Map<string, AbortController>>(new Map());
  const closedRef = useRef(false);
  const notifiedSuccessRef = useRef(0);
  // 每个文件的去向：同名文档命中时由用户选择「作为新版本」/「作为新文档」
  const modesRef = useRef<
    Map<
      string,
      | { kind: 'new' }
      | { kind: 'canceled' }
      | { kind: 'version'; documentId: string; hint?: VersionPreviewResult }
    >
  >(new Map());
  // 预检已落对象的临时文件（用户关闭弹窗时要删掉）
  const previewHintsRef = useRef<Map<string, { documentId: string; hint: VersionPreviewResult }>>(new Map());
  // 用户在「同名文档」提示里明确选了「作为新文档」的抑制目标（uid → 已有文档 id）；
  // 解析完成后的相似检测会跳过它，避免重复提示同一条。
  const suppressedRef = useRef<Map<string, string>>(new Map());
  const overflowNotifiedRef = useRef(false); // 同批溢出提示只报一次（onChange 可能逐次触发）
  // 同名文档提示（三选一：新版本 / 新文档 / 取消）
  const [versionPrompt, setVersionPrompt] = useState<{
    fileName: string;
    preview: VersionPreviewResult | null;
  } | null>(null);
  const versionPromptResolveRef = useRef<((choice: VersionChoice) => void) | null>(null);

  // 打开弹窗时重置所有批量状态 + 加载文件夹
  useEffect(() => {
    if (open) {
      itemsRef.current = [];
      setItems([]);
      abortMapRef.current = new Map();
      closedRef.current = false;
      notifiedSuccessRef.current = 0;
      overflowNotifiedRef.current = false;
      modesRef.current = new Map();
      previewHintsRef.current = new Map();
      suppressedRef.current = new Map();
      setVersionPrompt(null);
      setRunning(false);
      setSelectedFolderId(defaultFolderId ?? '');
      getFolderList(kbId)
        .then((res) => setFolders(res.items ?? []))
        .catch(() => setFolders([]));
    }
  }, [open, kbId, defaultFolderId]);

  // 卸载时清定时器，避免 setState on unmounted
  useEffect(() => () => {
    if (flushTimer.current != null) window.clearTimeout(flushTimer.current);
  }, []);

  const flushNow = useCallback(() => {
    if (flushTimer.current != null) {
      window.clearTimeout(flushTimer.current);
      flushTimer.current = null;
    }
    setItems(itemsRef.current.map((it) => ({ ...it })));
  }, []);

  const scheduleFlush = useCallback(() => {
    if (flushTimer.current != null) return;
    flushTimer.current = window.setTimeout(() => {
      flushTimer.current = null;
      setItems(itemsRef.current.map((it) => ({ ...it })));
    }, 200);
  }, []);

  /** 改 item 字段的唯一入口；终态传 immediate=true 立即渲染 */
  const patchItem = useCallback(
    (uid: string, patch: Partial<UploadItem>, immediate = false) => {
      const it = itemsRef.current.find((x) => x.uid === uid);
      if (!it) return;
      Object.assign(it, patch);
      if (immediate) flushNow();
      else scheduleFlush();
    },
    [flushNow, scheduleFlush],
  );

  /** 前置校验跳过原因 → 展示文案（fileTooLarge 用该文件类型的动态大小上限） */
  const renderSkipReason = useCallback(
    (reason: SkipReason, maxBytes: number | null): string => {
      switch (reason) {
        case 'fileEmpty':
          return t('common.fileEmpty');
        case 'fileTooLarge':
          return t('common.batchUploadFileTooLarge', {
            max: maxBytes != null ? formatMiB(maxBytes) : t('common.quotaSizeUnknown'),
          });
        case 'extNotAllowed':
          return t('common.batchUploadExtNotAllowed');
        case 'dupInBatch':
          return t('common.batchUploadDupInBatch');
        default:
          return t('common.uploadFailed');
      }
    },
    [t],
  );

  /**
   * fileList → UploadItem[]。含数量上限、前置校验、同批去重。
   * antd 多选时 onChange 可能逐次触发（每次带累积的 fileList 快照），
   * 因此靠 uid 去重实现幂等，不能假设"一次选择 = 一次调用"。
   */
  const enqueue = useCallback(
    (fileList: UploadFile[]) => {
      const existingUids = new Set(itemsRef.current.map((it) => it.uid));
      const seen = new Set(itemsRef.current.map((it) => fingerprint(it.file)));
      let overflow = 0;

      for (const uf of fileList) {
        if (existingUids.has(uf.uid)) continue;
        const file = uf.originFileObj as File | undefined;
        if (!file) continue;
        if (uploadFileCountLimit != null && itemsRef.current.length >= uploadFileCountLimit) {
          overflow += 1;
          continue;
        }

        const item: UploadItem = { uid: uf.uid, file, status: 'pending', progress: 0 };
        const maxBytes = fileSizeLimitBytes(quotas, file.name);
        const reason = validate(file, seen, maxBytes);
        if (reason) {
          item.status = 'skipped';
          item.error = renderSkipReason(reason, maxBytes);
        } else {
          seen.add(fingerprint(file));
        }
        itemsRef.current.push(item);
      }

      if (overflow > 0 && !overflowNotifiedRef.current) {
        // antd 多选时 onChange 可能逐次触发（累积快照），同批溢出只提示一次
        overflowNotifiedRef.current = true;
        message.warning(
          t('common.batchUploadTooManyFiles', { max: uploadFileCountLimit ?? 0 }),
        );
      }
      flushNow();
    },
    [renderSkipReason, t, uploadFileCountLimit, quotas],
  );

  /** 删除选中文件：上传中先中止请求，再从列表移除 */
  const removeItem = useCallback(
    (uid: string) => {
      const ctrl = abortMapRef.current.get(uid);
      if (ctrl) ctrl.abort();
      abortMapRef.current.delete(uid);
      itemsRef.current = itemsRef.current.filter((it) => it.uid !== uid);
      flushNow();
    },
    [flushNow],
  );

  /** 临时对象清理（放弃 / 关闭时）；失败只提示不阻断。 */
  const discardHint = useCallback(
    (documentId: string, hint: VersionPreviewResult) => {
      void discardDocumentVersionUpload(documentId, hint.storageKey, hint.storageBackend).catch(
        () => {
          message.warning(t('domainKnowledge.versions.similarityDiscardFailed'));
        },
      );
    },
    [t],
  );

  /** 三选一提示的结算入口（按钮 / 关闭 X / ESC 都走这里）。 */
  const settleVersionPrompt = useCallback((choice: VersionChoice) => {
    versionPromptResolveRef.current?.(choice);
  }, []);

  /** 同名文档提示：展示与现有版本的内容差异，确认「作为新版本」/「作为新文档」/「取消」。 */
  const askVersionChoice = useCallback(
    (fileName: string, preview: VersionPreviewResult | null): Promise<VersionChoice> =>
      new Promise((resolve) => {
        versionPromptResolveRef.current = (choice: VersionChoice) => {
          versionPromptResolveRef.current = null;
          setVersionPrompt(null);
          resolve(choice);
        };
        setVersionPrompt({ fileName, preview });
      }),
    [],
  );

  /** 预检：把文件落到临时对象并与现有版本比对内容（不写库）。 */
  const runPreview = async (
    item: UploadItem,
    documentId: string,
  ): Promise<VersionPreviewResult | null> => {
    const controller = new AbortController();
    abortMapRef.current.set(item.uid, controller);
    patchItem(item.uid, { status: 'uploading', progress: 0 }, true);
    try {
      return await previewDocumentVersion(documentId, kbId, item.file, {
        signal: controller.signal,
        onUploadProgress: (e) => {
          if (e.total) {
            patchItem(item.uid, { progress: Math.round((e.loaded / e.total) * 100) });
          }
        },
      });
    } catch {
      // 预检失败不阻断：退回「仅按文件名」的提示
      return null;
    } finally {
      abortMapRef.current.delete(item.uid);
      patchItem(item.uid, { status: 'pending', progress: 0 }, true);
    }
  };

  /** 上传前逐个判定同名文档（串行，避免多个弹窗叠加）。 */
  const resolveUploadModes = async (files: UploadItem[]) => {
    for (const item of files) {
      modesRef.current.delete(item.uid);
      let mode:
        | { kind: 'new' }
        | { kind: 'canceled' }
        | { kind: 'version'; documentId: string; hint?: VersionPreviewResult } = { kind: 'new' };
      try {
        const existing = await findDocumentByName(kbId, item.file.name);
        if (existing) {
          const preview = await runPreview(item, existing.id);
          if (closedRef.current) return;
          if (preview) {
            previewHintsRef.current.set(item.uid, { documentId: existing.id, hint: preview });
          }
          const choice = await askVersionChoice(item.file.name, preview);
          if (closedRef.current) return;
          if (choice === 'version') {
            // 改选新版本：之前若有抑制目标，已不再适用
            suppressedRef.current.delete(item.uid);
            mode = { kind: 'version', documentId: existing.id, hint: preview ?? undefined };
          } else {
            if (preview) {
              // 选择新文档 / 取消上传：预检临时对象都不再需要
              previewHintsRef.current.delete(item.uid);
              discardHint(existing.id, preview);
            }
            mode = choice === 'cancel' ? { kind: 'canceled' } : { kind: 'new' };
            if (choice === 'new') {
              // 用户明确拒绝「作为新版本」：记录目标，解析后的相似检测不再提示它
              suppressedRef.current.set(item.uid, existing.id);
            } else {
              suppressedRef.current.delete(item.uid);
            }
          }
        }
      } catch {
        // 查询失败不阻断上传：按新文档处理（后端仍有重复校验）
        mode = { kind: 'new' };
      }
      modesRef.current.set(item.uid, mode);
    }
  };

  /** 单文件执行器：吞掉所有异常，只写状态，不向上抛 */
  const uploadOne = async (item: UploadItem): Promise<void> => {
    const mode = modesRef.current.get(item.uid) ?? { kind: 'new' as const };
    if (mode.kind === 'canceled') {
      // 用户在「是否作为新版本」提示里选择了取消：本次不上传
      patchItem(item.uid, { status: 'canceled', progress: 0 }, true);
      return;
    }
    const controller = new AbortController();
    abortMapRef.current.set(item.uid, controller);
    patchItem(item.uid, { status: 'uploading', progress: 0 }, true);

    const options = {
      signal: controller.signal,
      onUploadProgress: (e: { loaded: number; total?: number }) => {
        if (e.total) {
          patchItem(item.uid, { progress: Math.round((e.loaded / e.total) * 100) });
        }
      },
    };

    try {
      if (mode.kind === 'version') {
        // 用户已明确选「作为新版本」，跳过相似度二次询问。
        if (mode.hint) {
          // 预检已把文件落到临时对象，直接复用（不再重传字节）
          await confirmDocumentVersionUpload(mode.documentId, kbId, mode.hint);
          previewHintsRef.current.delete(item.uid);
        } else {
          const result = await uploadDocumentVersion(mode.documentId, kbId, item.file, {
            ...options,
            confirmSimilar: true,
          });
          if (result.kind === 'needs_confirmation') {
            await confirmDocumentVersionUpload(mode.documentId, kbId, result.hint);
          }
        }
      } else {
        await uploadManualDocument(kbId, item.file, selectedFolderId || undefined, t, {
          ...options,
          suppressSimilarityTargetId: suppressedRef.current.get(item.uid),
        });
      }
      patchItem(item.uid, { status: 'success', progress: 100 }, true);
    } catch (err: any) {
      const mode = modesRef.current.get(item.uid);
      if (mode?.kind === 'version' && mode.hint) {
        // 建版本失败：临时对象没有归属，顺手清掉
        previewHintsRef.current.delete(item.uid);
        discardHint(mode.documentId, mode.hint);
      }
      if (controller.signal.aborted) {
        patchItem(item.uid, { status: 'canceled', progress: 0 }, true);
      } else if (
        /内容相同|同一文档/.test(String(err?.message || '')) &&
        modesRef.current.get(item.uid)?.kind === 'version'
      ) {
        // 版本上传的同内容拦截：保留后端更准确的文案（当前版 / 历史版 vN）
        patchItem(item.uid, { status: 'duplicate', error: err.message }, true);
      } else if (isDuplicateError(err)) {
        patchItem(item.uid, { status: 'duplicate', error: t('common.batchUploadDupInKb') }, true);
      } else {
        // 限流（HTTP 500 + code=1000，与服务器错误无法区分）也落这里
        patchItem(item.uid, { status: 'error', error: err?.message || t('common.uploadFailed') }, true);
      }
    } finally {
      abortMapRef.current.delete(item.uid);
      // 本次去向已结算，清掉抑制项避免整批上传期间内存滞留
      suppressedRef.current.delete(item.uid);
    }
  };

  /** 每轮结束 / 关闭弹窗前都调它；靠水位线保证不重复刷新，返回本轮新增成功数 */
  const notifySuccessIfNeeded = useCallback((): number => {
    const total = itemsRef.current.filter((it) => it.status === 'success').length;
    const added = total - notifiedSuccessRef.current;
    if (added > 0) {
      notifiedSuccessRef.current = total;
      onSuccess?.();
    }
    return added;
  }, [onSuccess]);

  const finishRound = useCallback(() => {
    const added = notifySuccessIfNeeded();
    if (added > 0) {
      message.success(t('common.batchUploadSubmitted', { count: added }));
    }
  }, [notifySuccessIfNeeded, t]);

  const startUpload = async () => {
    if (running) return;
    setRunning(true);
    const toUpload = itemsRef.current.filter((it) => it.status === 'pending');
    await resolveUploadModes(toUpload);
    if (closedRef.current) {
      // 询问过程中用户已关闭弹窗：别再继续上传
      setRunning(false);
      return;
    }
    await runWithByteBudget(toUpload, uploadOne);
    setRunning(false);
    finishRound();
  };

  const retryFailed = async () => {
    if (running) return;
    itemsRef.current.forEach((it) => {
      if (it.status === 'error' || it.status === 'canceled') {
        it.status = 'pending';
        it.progress = 0;
        delete it.error;
      }
    });
    flushNow();
    setRunning(true);
    const toUpload = itemsRef.current.filter((it) => it.status === 'pending');
    await resolveUploadModes(toUpload);
    if (closedRef.current) {
      // 询问过程中用户已关闭弹窗：别再继续上传
      setRunning(false);
      return;
    }
    await runWithByteBudget(toUpload, uploadOne);
    setRunning(false);
    finishRound();
  };

  const doClose = () => {
    closedRef.current = true;
    versionPromptResolveRef.current?.('cancel');
    abortMapRef.current.forEach((c) => c.abort());
    // 预检产生但未被采用的临时对象，关闭时删除
    previewHintsRef.current.forEach(({ documentId, hint }) => discardHint(documentId, hint));
    previewHintsRef.current.clear();
    notifySuccessIfNeeded();
    onClose();
  };

  const handleCancel = () => {
    if (running) {
      Modal.confirm({
        title: t('common.batchUploadCloseConfirm'),
        okText: t('common.confirm'),
        cancelText: t('common.cancel'),
        onOk: () => doClose(),
      });
    } else {
      doClose();
    }
  };

  const summary = useMemo(() => {
    const s = { total: 0, success: 0, failed: 0, duplicate: 0, skipped: 0 };
    for (const it of items) {
      s.total++;
      if (it.status === 'success') s.success++;
      else if (it.status === 'error') s.failed++;
      else if (it.status === 'duplicate') s.duplicate++;
      else if (it.status === 'skipped') s.skipped++;
    }
    return s;
  }, [items]);

  const hasPending = useMemo(() => items.some((it) => it.status === 'pending'), [items]);
  const hasErrorOrCanceled = useMemo(
    () => items.some((it) => it.status === 'error' || it.status === 'canceled'),
    [items],
  );
  const totalProgress = useMemo(() => computeTotalProgress(items), [items]);

  const statusText = (s: FileUploadStatus): string => {
    switch (s) {
      case 'pending':
        return t('common.batchUploadStatusPending');
      case 'uploading':
        return t('common.uploading');
      case 'success':
        return t('common.batchUploadStatusSubmitted');
      case 'error':
        return t('common.uploadFailed');
      case 'duplicate':
        return t('common.batchUploadStatusDuplicate');
      case 'skipped':
        return t('common.batchUploadStatusSkipped');
      case 'canceled':
        return t('common.batchUploadCanceled');
      default:
        return s;
    }
  };

  const statusColor = (s: FileUploadStatus): string => {
    switch (s) {
      case 'success':
        return 'green';
      case 'error':
        return 'red';
      case 'duplicate':
        return 'orange';
      case 'uploading':
        return 'blue';
      default:
        return 'default';
    }
  };

  const formatSize = (bytes: number): string => {
    if (bytes >= 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
    if (bytes >= 1024) return `${(bytes / 1024).toFixed(0)} KB`;
    return `${bytes} B`;
  };

  // 同名提示正文：展示与现有版本的内容差异
  const versionPromptContent = useMemo(() => {
    if (!versionPrompt) return '';
    const { fileName, preview } = versionPrompt;
    if (!preview) {
      return t('common.versionPromptBody', { name: fileName });
    }
    if (preview.sameAsCurrent) {
      return t('common.versionPromptBodyIdenticalCurrent', {
        name: fileName,
        version: preview.currentVersionNo ?? '-',
      });
    }
    if (preview.sameAsVersionNo != null) {
      return t('common.versionPromptBodyIdenticalHistory', {
        name: fileName,
        version: preview.sameAsVersionNo,
      });
    }
    if (preview.method === 'unsupported') {
      // 压缩/容器格式（PDF、老版 Office 等）不做字节估算，避免误导性百分比
      return t('common.versionPromptBodyUnsupported', { name: fileName });
    }
    if (preview.method === 'empty' || preview.method === 'error') {
      // 现有文件读不到（存量缺 storage_key / 存储读取失败）时不展示百分比
      return t('common.versionPromptBody', { name: fileName });
    }
    return t('common.versionPromptBodySimilar', {
      name: fileName,
      version: preview.currentVersionNo ?? '-',
      percent: Math.round(preview.similarity * 100),
      currentSize: formatSize(preview.currentFileSize),
      newSize: formatSize(preview.fileSize),
    });
  }, [versionPrompt, t]);

  // 单文件大小摘要：只列后端下发配额的类别（「类别 ≤xxMB」用 · 连接）
  const sizeSummary = useMemo(() => {
    if (!quotas) return '';
    const typeDefs: { key: string; labelKey: string }[] = [
      { key: 'documentFileSizeLimitMiB', labelKey: 'common.quotaTypeDocument' },
      { key: 'spreadsheetFileSizeLimitMiB', labelKey: 'common.quotaTypeSpreadsheet' },
      { key: 'imageFileSizeLimitMiB', labelKey: 'common.quotaTypeImage' },
      { key: 'audioFileSizeLimitMiB', labelKey: 'common.quotaTypeAudio' },
      { key: 'videoFileSizeLimitMiB', labelKey: 'common.quotaTypeVideo' },
    ];
    return typeDefs
      .map((d) => {
        const item = quotas.find((q) => q.quotaKey === d.key);
        return item ? `${t(d.labelKey)} ≤${formatMiB(item.limit)}` : null;
      })
      .filter(Boolean)
      .join(' · ');
  }, [quotas, t]);

  // 音视频时长摘要（一期仅展示，后端不校验）
  const durationSummary = useMemo(() => {
    if (!quotas) return '';
    const audio = quotas.find((q) => q.quotaKey === 'audioDurationLimitMinutes');
    const video = quotas.find((q) => q.quotaKey === 'videoDurationLimitMinutes');
    const parts: string[] = [];
    if (audio) parts.push(`${t('common.quotaTypeAudio')} ≤${audio.limit}${audio.unit}`);
    if (video) parts.push(`${t('common.quotaTypeVideo')} ≤${video.limit}${video.unit}`);
    return parts.length ? `${t('common.quotaDurationPrefix')}：${parts.join(' · ')}` : '';
  }, [quotas, t]);

  // 格式要求 Tooltip 内容：quota 加载失败显示刷新提示；就绪显示数量/大小/时长三条；未就绪为空（不弹）
  const quotaTooltipContent = useMemo(() => {
    if (quotaError) return t('common.quotaLoadFailed');
    if (!quotas || quotas.length === 0) return null;
    const rows: string[] = [];
    if (uploadFileCountLimit != null) {
      rows.push(t('common.quotaUploadCount', { count: uploadFileCountLimit }));
    }
    if (sizeSummary) rows.push(`${t('common.quotaSizePrefix')}：${sizeSummary}`);
    if (durationSummary) rows.push(durationSummary);
    if (!rows.length) return null;
    return (
      <div style={{ lineHeight: 1.7 }}>
        {rows.map((row, i) => (
          <div key={i}>{row}</div>
        ))}
      </div>
    );
  }, [quotaError, quotas, uploadFileCountLimit, sizeSummary, durationSummary, t]);

  const onChange: UploadProps['onChange'] = ({ fileList }) => enqueue(fileList);

  // 已达单次数量上限：禁止继续点击拖拽区（删除文件腾出名额后自动恢复）
  const countReached = uploadFileCountLimit != null && items.length >= uploadFileCountLimit;

  return (
    <>
    <Modal
      title={
        <Space>
          <PlusOutlined style={{ color: '#3b82f6' }} />
          <span>{t('common.addDocument')}</span>
        </Space>
      }
      open={open}
      onCancel={handleCancel}
      width={640}
      footer={
        <Space>
          {running ? null : hasPending ? (
            <Button type="primary" onClick={startUpload}>
              {t('common.batchUploadStart')}
            </Button>
          ) : (
            <>
              {hasErrorOrCanceled && (
                <Button onClick={retryFailed}>{t('common.batchUploadRetryFailed')}</Button>
              )}
              <Button type="primary" onClick={doClose}>
                {t('common.batchUploadDone')}
              </Button>
            </>
          )}
        </Space>
      }
    >
      <div style={{ marginBottom: 16 }}>
        <div style={{ fontSize: 14, fontWeight: 600, color: '#0b2b5c', marginBottom: 6 }}>
          {t('common.targetDirectory')}
        </div>
        <Select
          value={selectedFolderId}
          onChange={setSelectedFolderId}
          placeholder={t('common.selectDirectory')}
          style={{ width: '100%' }}
          disabled={running}
          options={[
            { value: '', label: t('common.allDocuments') },
            ...folders.map((f) => ({ value: f.id, label: f.name })),
          ]}
        />
      </div>

      <Upload.Dragger
        name="file"
        multiple
        accept={ACCEPT_EXTENSIONS}
        showUploadList={false}
        fileList={[]}
        beforeUpload={() => false}
        onChange={onChange}
        disabled={running || countReached}
      >
        <div style={{ padding: '20px 0' }}>
          <p className="ant-upload-drag-icon">
            <CloudUploadOutlined style={{ fontSize: 48, color: '#3b82f6' }} />
          </p>
          <p style={{ fontSize: 16, color: '#0b2b5c', fontWeight: 500, marginBottom: 8 }}>
            {t('common.uploadDraggerText')}
          </p>
          <p style={{ fontSize: 14, color: '#64748b', marginBottom: 8, lineHeight: 1.6 }}>
            {t('common.uploadDescription')}
          </p>
          <p style={{ fontSize: 13, color: '#f97316', margin: 0 }}>
            <Tooltip title={quotaTooltipContent ?? undefined}>
              <span style={{ cursor: 'pointer' }}>
                {t('common.quotaFormatHint')}
                <QuestionCircleOutlined style={{ marginLeft: 4 }} />
              </span>
            </Tooltip>
          </p>
        </div>
      </Upload.Dragger>

      {uploadFileCountLimit != null && (
        <div
          style={{
            marginTop: 12,
            fontSize: 13,
            color: countReached ? '#ef4444' : '#64748b',
            textAlign: 'right',
          }}
        >
          {t('common.batchUploadCount', { count: items.length, max: uploadFileCountLimit })}
        </div>
      )}

      {items.length > 0 && (
        <div style={{ marginTop: 16 }}>
          <div style={{ maxHeight: 260, overflowY: 'auto' }}>
            {items.map((it) => (
              <div
                key={it.uid}
                style={{
                  display: 'flex',
                  alignItems: 'center',
                  padding: '8px 0',
                  borderBottom: '1px solid #f0f0f0',
                }}
              >
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                    <span
                      style={{
                        overflow: 'hidden',
                        textOverflow: 'ellipsis',
                        whiteSpace: 'nowrap',
                        fontSize: 14,
                        color: '#0b2b5c',
                      }}
                    >
                      {it.file.name}
                    </span>
                    <Tag color={statusColor(it.status)} style={{ margin: 0 }}>
                      {statusText(it.status)}
                    </Tag>
                  </div>
                  <div style={{ fontSize: 12, color: '#64748b', marginTop: 2 }}>
                    {formatSize(it.file.size)}
                  </div>
                  {it.error && (
                    <div style={{ fontSize: 12, color: '#f97316', marginTop: 2 }}>{it.error}</div>
                  )}
                  {it.status === 'uploading' && (
                    <Progress percent={it.progress} size="small" style={{ marginTop: 4 }} />
                  )}
                </div>
                <Button
                  type="text"
                  size="small"
                  icon={<DeleteOutlined />}
                  aria-label={t('common.delete')}
                  onClick={() => removeItem(it.uid)}
                  style={{ color: '#ef4444', flexShrink: 0 }}
                />
              </div>
            ))}
          </div>

          <div style={{ marginTop: 12 }}>
            <Progress percent={totalProgress} />
            <div style={{ fontSize: 13, color: '#64748b', textAlign: 'center' }}>
              {t('common.batchUploadSummary', {
                success: summary.success,
                failed: summary.failed,
                duplicate: summary.duplicate,
                skipped: summary.skipped,
              })}
            </div>
          </div>
        </div>
      )}
    </Modal>

      <Modal
        open={versionPrompt !== null}
        title={t('common.versionPromptTitle')}
        maskClosable={false}
        onCancel={() => settleVersionPrompt('cancel')}
        footer={[
          <Button key="cancel" onClick={() => settleVersionPrompt('cancel')}>
            {t('common.versionPromptCancel')}
          </Button>,
          <Button key="new" onClick={() => settleVersionPrompt('new')}>
            {t('common.versionPromptNew')}
          </Button>,
          <Button
            key="version"
            type="primary"
            disabled={Boolean(versionPrompt?.preview?.sameAsCurrent)}
            onClick={() => settleVersionPrompt('version')}
          >
            {t('common.versionPromptOk')}
          </Button>,
        ]}
      >
        <div>{versionPromptContent}</div>
      </Modal>
    </>
  );
};

export default UploadModal;
