import React, { useState, useEffect, useRef, useCallback, useMemo } from 'react';
import { Modal, Upload, Select, Space, Button, Progress, Tag, message } from 'antd';
import type { UploadProps } from 'antd/es/upload';
import type { UploadFile } from 'antd';
import { PlusOutlined, CloudUploadOutlined, ExclamationCircleOutlined } from '@ant-design/icons';
import { useTranslation } from 'react-i18next';
import { uploadManualDocument, getFolderList } from '@/api/domainKnowledge';
import { ACCEPT_EXTENSIONS, BATCH_UPLOAD_MAX_FILES } from '@/constants/upload';
import type { FolderItem } from '@/types/domainKnowledge';
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

export interface UploadModalProps {
  kbId: string;
  open: boolean;
  onClose: () => void;
  onSuccess?: () => void;
}

const UploadModal: React.FC<UploadModalProps> = ({ kbId, open, onClose, onSuccess }) => {
  const { t } = useTranslation();

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

  // 打开弹窗时重置所有批量状态 + 加载文件夹
  useEffect(() => {
    if (open) {
      itemsRef.current = [];
      setItems([]);
      abortMapRef.current = new Map();
      closedRef.current = false;
      notifiedSuccessRef.current = 0;
      setRunning(false);
      setSelectedFolderId('');
      getFolderList(kbId)
        .then((res) => setFolders(res.items ?? []))
        .catch(() => setFolders([]));
    }
  }, [open, kbId]);

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

  /** 前置校验跳过原因 → 展示文案 */
  const renderSkipReason = useCallback(
    (reason: SkipReason): string => {
      switch (reason) {
        case 'fileEmpty':
          return t('common.fileEmpty');
        case 'fileTooLarge':
          return t('common.batchUploadFileTooLarge', { max: '500MB' });
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
        if (itemsRef.current.length >= BATCH_UPLOAD_MAX_FILES) {
          overflow += 1;
          continue;
        }

        const item: UploadItem = { uid: uf.uid, file, status: 'pending', progress: 0 };
        const reason = validate(file, seen);
        if (reason) {
          item.status = 'skipped';
          item.error = renderSkipReason(reason);
        } else {
          seen.add(fingerprint(file));
        }
        itemsRef.current.push(item);
      }

      if (overflow > 0) {
        message.warning(t('common.batchUploadTooManyFiles', { max: BATCH_UPLOAD_MAX_FILES }));
      }
      flushNow();
    },
    [renderSkipReason, t],
  );

  /** 单文件执行器：吞掉所有异常，只写状态，不向上抛 */
  const uploadOne = async (item: UploadItem): Promise<void> => {
    const controller = new AbortController();
    abortMapRef.current.set(item.uid, controller);
    patchItem(item.uid, { status: 'uploading', progress: 0 }, true);

    try {
      await uploadManualDocument(kbId, item.file, selectedFolderId || undefined, t, {
        signal: controller.signal,
        onUploadProgress: (e) => {
          if (e.total) {
            patchItem(item.uid, { progress: Math.round((e.loaded / e.total) * 100) });
          }
        },
      });
      patchItem(item.uid, { status: 'success', progress: 100 }, true);
    } catch (err: any) {
      if (controller.signal.aborted) {
        patchItem(item.uid, { status: 'canceled', progress: 0 }, true);
      } else if (isDuplicateError(err)) {
        patchItem(item.uid, { status: 'duplicate', error: t('common.batchUploadDupInKb') }, true);
      } else {
        // 限流（HTTP 500 + code=1000，与服务器错误无法区分）也落这里
        patchItem(item.uid, { status: 'error', error: err?.message || t('common.uploadFailed') }, true);
      }
    } finally {
      abortMapRef.current.delete(item.uid);
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
    await runWithByteBudget(toUpload, uploadOne);
    setRunning(false);
    finishRound();
  };

  const doClose = () => {
    closedRef.current = true;
    abortMapRef.current.forEach((c) => c.abort());
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

  const onChange: UploadProps['onChange'] = ({ fileList }) => enqueue(fileList);

  return (
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
        disabled={running}
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
            <ExclamationCircleOutlined style={{ marginRight: 4 }} />
            {t('common.uploadMaxSize')}
          </p>
        </div>
      </Upload.Dragger>

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
  );
};

export default UploadModal;
