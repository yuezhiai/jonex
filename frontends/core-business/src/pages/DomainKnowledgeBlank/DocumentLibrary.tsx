import React, { useState, useEffect, useCallback, useRef } from 'react';
import { useKbPermission } from '@/hooks/useKbPermission';
import { useNavigate } from 'react-router-dom';
import { Input, Button, Table, Space, Modal, Dropdown, message, Menu, Badge } from 'antd';
import {
  SearchOutlined,
  PlusOutlined,
  ReloadOutlined,
  SyncOutlined,
  FileOutlined,
  FolderOutlined,
  MoreOutlined,
  EditOutlined,
  DeleteOutlined,
  CaretDownOutlined,
  CaretRightOutlined,
  DownloadOutlined,
} from '@ant-design/icons';
import { useTranslation } from 'react-i18next';
import { createColumns } from './config';
import {
  getManualDocList,
  getFolderList,
  createFolder,
  renameFolder,
  deleteFolder,
  reparseDocument,
  retryDocumentOntology,
  deleteManualDocument,
  batchMoveDocuments,
  getDocumentViewTicket,
} from '@/api/domainKnowledge';
import { getShellContext, isPlatformAdmin } from '@jonex/shell-sdk';
import { batchDownloadDocuments } from '@/utils/batchDocumentDownload';
import { listAccessMethods } from '@/api/dataSource';
import type { ManualDocItem, FolderItem } from '@/types/domainKnowledge';
import type { AccessMethodItem } from '@/types/dataSource';
import type { DocPhase } from '@/utils/docPhase';
import { useDocumentViewer } from '@/components/DocumentViewer';
import DocumentStatusFilter from '@/components/DocumentStatusFilter';
import UploadModal from './UploadModal';
import FolderNameModal from './FolderNameModal';
import MoveDocModal from './MoveDocModal';
import TagModal from './TagModal';
import './index.scss';

const PAGE_SIZE = 10;

interface DocumentLibraryProps {
  kbId: string;
  /** [jonex] 知识库类型：openkb 时文档状态列按 llm_wiki_compile_status 显示。 */
  kbType?: string;
  /** 文档配额已达上限（提升自父级，控制上传按钮禁用） */
  docReached: boolean;
  /** 配额加载失败（提升自父级，控制上传按钮 title 提示） */
  quotaError: boolean;
  /** 刷新配额数据：父级 useQuota.reload，列表刷新时同步调用（统计栏与上传按钮共用一份） */
  onRefreshQuota: () => void;
}

export default function DocumentLibrary({
  kbId,
  kbType,
  docReached,
  quotaError,
  onRefreshQuota,
}: DocumentLibraryProps) {
  const { canWrite } = useKbPermission(kbId);
  const { t } = useTranslation();
  const navigate = useNavigate();
  const [selectedKeys, setSelectedKeys] = useState<string[]>(['all']);
  const [folders, setFolders] = useState<FolderItem[]>([]);
  const [data, setData] = useState<ManualDocItem[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(PAGE_SIZE);
  const [loading, setLoading] = useState(false);
  const [modalOpen, setModalOpen] = useState(false);
  const [folderModalOpen, setFolderModalOpen] = useState(false);
  const [folderModalValue, setFolderModalValue] = useState('');
  const [folderModalLoading, setFolderModalLoading] = useState(false);
  const [folderModalMode, setFolderModalMode] = useState<'create' | 'rename'>('create');
  const [editingFolderId, setEditingFolderId] = useState('');
  const [reloadFlag, setReloadFlag] = useState(0);
  const [foldersCollapsed, setFoldersCollapsed] = useState(false);
  const [keyword, setKeyword] = useState('');
  const [inputValue, setInputValue] = useState('');
  const [statusFilter, setStatusFilter] = useState<DocPhase[]>([]);
  const [tagModalOpen, setTagModalOpen] = useState(false);
  const [tagDoc, setTagDoc] = useState<ManualDocItem | null>(null);
  // 受控行选中（批量移动按钮可用性 + 移动目标文档集）
  const [selectedRowKeys, setSelectedRowKeys] = useState<React.Key[]>([]);
  const [moveModal, setMoveModal] = useState<{
    open: boolean;
    mode: 'single' | 'batch';
    doc?: ManualDocItem;
  }>({ open: false, mode: 'single' });
  const [moveLoading, setMoveLoading] = useState(false);
  // 批量下载进度（0 = 空闲；>0 时按钮显示「下载中 i/N」）
  const [downloadProgress, setDownloadProgress] = useState(0);
  // 拖拽迁移状态（表格行 → 侧栏目录）：当前拖拽的文档 id + 悬停的 drop 目标（'all' = 根目录 / folder id）
  const [draggingDocId, setDraggingDocId] = useState<string | null>(null);
  const [dropTargetId, setDropTargetId] = useState<string | null>(null);
  const [accessMethods, setAccessMethods] = useState<AccessMethodItem[]>([]);
  const debounceRef = useRef<ReturnType<typeof setTimeout>>();

  const fetchFolders = useCallback(() => {
    if (!kbId) return;
    getFolderList(kbId)
      .then((res) => setFolders(res.items))
      .catch((err: any) => message.error(err?.message || t('common.folderListLoadFailed')));
  }, [kbId]);

  useEffect(() => {
    fetchFolders();
  }, [fetchFolders]);

  useEffect(() => {
    listAccessMethods()
      .then(setAccessMethods)
      .catch(() => {
        /* 数据来源列降级显示原始值 */
      });
  }, []);

  const handleFolderModalOk = async (value: string) => {
    const name = value.trim();
    if (!name) {
      message.warning(t('common.folderNameRequired'));
      return;
    }
    setFolderModalLoading(true);
    try {
      if (folderModalMode === 'create') {
        await createFolder(kbId, name);
        message.success(t('common.folderCreateSuccess'));
      } else {
        await renameFolder(editingFolderId, kbId, name);
        message.success(t('common.folderRenameSuccess'));
      }
      setFolderModalOpen(false);
      fetchFolders();
    } catch (err: any) {
      message.error(
        err?.message ||
          (folderModalMode === 'create' ? t('common.folderCreateFailed') : t('common.folderRenameFailed')),
      );
    } finally {
      setFolderModalLoading(false);
    }
  };

  const handleDeleteFolder = async (folderId: string, name: string) => {
    Modal.confirm({
      title: t('common.delete'),
      content: t('common.confirmDeleteMessage', { name }),
      okText: t('common.delete'),
      okType: 'danger',
      cancelText: t('common.cancel'),
      onOk: async () => {
        try {
          await deleteFolder(folderId, kbId);
          message.success(t('common.folderDeleted'));
          if (selectedKeys[0] === folderId) {
            setSelectedKeys(['all']);
          }
          fetchFolders();
        } catch (err: any) {
          message.error(err?.message || t('common.folderDeleteFailed'));
        }
      },
    });
  };

  const fetchList = useCallback(async () => {
    setLoading(true);
    try {
      const currentKey = selectedKeys[0];
      const result = await getManualDocList({
        knowledgeBaseId: kbId,
        page,
        pageSize,
        keyword: keyword || undefined,
        phase: statusFilter.length ? statusFilter : undefined,
        folder_id: currentKey !== 'all' ? String(currentKey) : undefined,
      });
      setData(result.list);
      setTotal(result.pagination.total);
    } catch (err: any) {
      message.error(err?.message || t('common.docListLoadFailed'));
    } finally {
      setLoading(false);
    }
  }, [kbId, page, pageSize, keyword, selectedKeys, statusFilter]);

  useEffect(() => {
    fetchList();
  }, [fetchList, reloadFlag]);

  // 数据变更（上传/删除/重解析/移动等）触发列表刷新，同步刷新配额数据（used 随之变化）
  useEffect(() => {
    if (reloadFlag > 0) onRefreshQuota();
  }, [reloadFlag, onRefreshQuota]);

  // 关键词、文件夹或状态变化时，重置页码并清空行选中（避免批量移动作用于当前数据集之外的旧选中）
  useEffect(() => {
    setPage(1);
    setSelectedRowKeys([]);
  }, [keyword, selectedKeys, statusFilter]);

  // 卸载时清理防抖定时器
  useEffect(() => {
    return () => {
      if (debounceRef.current) clearTimeout(debounceRef.current);
    };
  }, []);

  const handleKeywordChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const val = e.target.value;
    setInputValue(val);
    if (debounceRef.current) clearTimeout(debounceRef.current);
    debounceRef.current = setTimeout(() => {
      setKeyword(val);
    }, 300);
  };

  const handleUploadSuccess = () => {
    setPage(1);
    setReloadFlag((f) => f + 1);
  };

  const { openDocument, viewer } = useDocumentViewer();

  const handleView = useCallback(
    (record: ManualDocItem) => {
      openDocument({ docId: record.id, fileName: record.name });
    },
    [openDocument],
  );

  const handleViewResult = useCallback(
    (record: ManualDocItem) => {
      navigate(`/domain-knowledge/${kbId}/documents/${record.id}/result`);
    },
    [kbId, navigate],
  );

  const handleOpenTag = (record: ManualDocItem) => {
    setTagDoc(record);
    setTagModalOpen(true);
  };

  const handleReparse = useCallback(async (record: ManualDocItem) => {
    try {
      await reparseDocument(record.id);
      message.success(t('common.retryTriggered'));
      setReloadFlag((f) => f + 1);
    } catch (err: any) {
      message.error(err?.message || t('common.retryFailed'));
    }
  }, []);

  const handleRecompile = useCallback(
    async (record: ManualDocItem) => {
      try {
        await retryDocumentOntology(kbId, record.id);
        message.success(t('common.recompileTriggered'));
        setReloadFlag((f) => f + 1);
      } catch (err: any) {
        message.error(err?.message || t('common.recompileFailed'));
      }
    },
    [kbId],
  );

  const handleDelete = useCallback(
    (record: ManualDocItem) => {
      Modal.confirm({
        title: t('common.confirmDelete'),
        content: t('common.confirmDeleteDoc', { name: record.name }),
        okText: t('common.delete'),
        okType: 'danger',
        onOk: async () => {
          try {
            await deleteManualDocument(kbId, record.id);
            message.success(t('common.deleteSuccess'));
            setReloadFlag((f) => f + 1);
          } catch (err: any) {
            message.error(err?.message || t('common.deleteFailed'));
          }
        },
      });
    },
    [kbId, t],
  );

  const handleMove = useCallback((record: ManualDocItem) => {
    setMoveModal({ open: true, mode: 'single', doc: record });
  }, []);

  const handleMoveOk = async (folderId: string | null) => {
    const docIds =
      moveModal.mode === 'batch'
        ? selectedRowKeys.map(String)
        : moveModal.doc
          ? [moveModal.doc.id]
          : [];
    if (!docIds.length) return;
    setMoveLoading(true);
    try {
      await batchMoveDocuments(kbId, docIds, folderId);
      // 接口成功即提示，不显示移动数量
      message.success(t('common.moveSuccess'));
      setMoveModal({ open: false, mode: 'single' });
      setSelectedRowKeys([]);
      fetchFolders();
      setReloadFlag((f) => f + 1);
    } catch (err: any) {
      // 直接显示接口报错信息（request 层 toApiError 已提取后端 message）
      message.error(err?.message);
    } finally {
      setMoveLoading(false);
    }
  };

  // ===== 批量下载（仅平台管理员可见；多选选中的才下载）=====
  const isAdmin = isPlatformAdmin(getShellContext()?.user ?? null);

  const handleBatchDownload = useCallback(async () => {
    const ids = selectedRowKeys.map(String);
    if (!ids.length) return;
    setDownloadProgress(0);
    try {
      const { ok, failed } = await batchDownloadDocuments(ids, {
        fetchTicket: getDocumentViewTicket,
        onProgress: (done) => setDownloadProgress(done),
      });
      if (failed > 0) {
        message.warning(t('common.batchDownloadPartial', { ok, failed }));
      } else {
        message.success(t('common.batchDownloadSuccess', { ok }));
      }
    } finally {
      setDownloadProgress(0);
    }
  }, [selectedRowKeys, t]);

  // ===== 拖拽迁移（表格行 → 侧栏目录，HTML5 DnD）=====
  const handleRowDragStart = useCallback(
    (record: ManualDocItem) => (e: React.DragEvent<HTMLElement>) => {
      e.dataTransfer.effectAllowed = 'move';
      setDraggingDocId(record.id);
    },
    [],
  );

  const handleRowDragEnd = useCallback(() => {
    setDraggingDocId(null);
    setDropTargetId(null);
  }, []);

  const handleDragMove = useCallback(
    async (targetFolderId: string | null) => {
      if (!draggingDocId) return;
      try {
        await batchMoveDocuments(kbId, [draggingDocId], targetFolderId);
        message.success(t('common.moveSuccess'));
        fetchFolders();
        setReloadFlag((f) => f + 1);
      } catch (err: any) {
        message.error(err?.message);
      } finally {
        setDraggingDocId(null);
        setDropTargetId(null);
      }
    },
    [draggingDocId, kbId, t, fetchFolders],
  );

  const handleFolderDrop = useCallback(
    (targetFolderId: string | null) => (e: React.DragEvent<HTMLElement>) => {
      e.preventDefault();
      e.stopPropagation();
      if (!draggingDocId) return;
      setDropTargetId(null);
      const doc = data.find((d) => d.id === draggingDocId);
      if (!doc) return;
      // 拖到文档当前所在目录（含同为根目录）→ 不发请求，提示
      if ((doc.folder_id ?? null) === targetFolderId) {
        message.info(t('common.moveInCurrentFolder'));
        return;
      }
      handleDragMove(targetFolderId);
    },
    [draggingDocId, data, t, handleDragMove],
  );

  const handleDropTargetDragOver = useCallback(
    (key: string | null) => (e: React.DragEvent<HTMLElement>) => {
      // dragover 必须 preventDefault 才允许 drop；stopPropagation 避免干扰 Menu onSelect
      e.preventDefault();
      e.stopPropagation();
      setDropTargetId((cur) => (cur !== key ? key : cur));
    },
    [],
  );

  const handleDropTargetDragLeave = useCallback(
    (key: string | null) => (e: React.DragEvent<HTMLElement>) => {
      // 子元素间移动会触发父级 dragleave，用 contains 判断是否真正离开
      if (e.currentTarget.contains(e.relatedTarget as Node)) return;
      setDropTargetId((cur) => (cur === key ? null : cur));
    },
    [],
  );

  const columns = createColumns(
    t,
    {
      onView: handleView,
      onTag: handleOpenTag,
      onViewResult: handleViewResult,
      onReparse: handleReparse,
      onRecompile: handleRecompile,
      onDelete: handleDelete,
      onMove: handleMove,
    },
    accessMethods,
    kbType,
    canWrite,
  );

  const sidebarContent = (
    <>
      <div className="doc-library-sidebar-header">
        <Space>
          <FileOutlined />
          <span>{t('common.documentDirectory')}</span>
        </Space>
        <Button
          type="text"
          icon={<PlusOutlined />}
          size="small"
          disabled={!canWrite}
          title={canWrite ? undefined : t('domainSpace.noManagePermission')}
          onClick={() => {
            setFolderModalMode('create');
            setFolderModalValue('');
            setFolderModalOpen(true);
          }}
        />
      </div>
      <Menu
        mode="inline"
        selectedKeys={selectedKeys}
        onSelect={({ key }) => setSelectedKeys([key])}
        className="doc-library-menu"
        items={[
          {
            key: 'all',
            icon: <FolderOutlined />,
            label: (
              <div
                style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', width: '100%' }}
                className={`doc-library-drop-target${dropTargetId === 'all' ? ' doc-library-drop-target--active' : ''}`}
                onDragOver={handleDropTargetDragOver('all')}
                onDragLeave={handleDropTargetDragLeave('all')}
                onDrop={handleFolderDrop(null)}
              >
                <span>{t('common.allDocuments')}</span>
                <span
                  style={{
                    cursor: 'pointer',
                    display: 'inline-flex',
                    alignItems: 'center',
                    fontSize: 12,
                    color: '#64748b',
                  }}
                  onClick={(e) => {
                    e.stopPropagation();
                    e.preventDefault();
                    setFoldersCollapsed(!foldersCollapsed);
                  }}
                  onMouseDown={(e) => e.stopPropagation()}
                >
                  {foldersCollapsed ? <CaretRightOutlined /> : <CaretDownOutlined />}
                </span>
              </div>
            ),
          },
          ...(!foldersCollapsed
            ? folders.map((f) => ({
                key: f.id,
                icon: <FolderOutlined />,
                className: f.is_preset ? 'doc-library-menu-item--preset' : '',
                label: (
                  <div
                    style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', width: '100%' }}
                    className={`doc-library-drop-target${dropTargetId === f.id ? ' doc-library-drop-target--active' : ''}`}
                    onDragOver={handleDropTargetDragOver(f.id)}
                    onDragLeave={handleDropTargetDragLeave(f.id)}
                    onDrop={handleFolderDrop(f.id)}
                  >
                    <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', flex: 1, minWidth: 0 }}>
                      {f.name}
                    </span>
                    <Badge
                      count={f.document_count ?? 0}
                      showZero
                      style={{ backgroundColor: '#f0f0f0', color: '#595959', boxShadow: 'none', marginRight: 4 }}
                    />
                    <Dropdown
                      menu={{
                        items: [
                          {
                            key: 'rename',
                            icon: <EditOutlined />,
                            label: t('common.rename'),
                            disabled: !canWrite,
                            onClick: ({ domEvent }) => {
                              domEvent.stopPropagation();
                              if (!canWrite) return;
                              setFolderModalMode('rename');
                              setEditingFolderId(f.id);
                              setFolderModalValue(f.name);
                              setFolderModalOpen(true);
                            },
                          },
                          {
                            key: 'delete',
                            icon: <DeleteOutlined />,
                            label: t('common.delete'),
                            danger: true,
                            disabled: !canWrite,
                            onClick: ({ domEvent }) => {
                              domEvent.stopPropagation();
                              if (!canWrite) return;
                              handleDeleteFolder(f.id, f.name);
                            },
                          },
                        ],
                      }}
                      trigger={['click']}
                    >
                      <span className="doc-library-nav-more" onClick={(e) => e.stopPropagation()}>
                        <MoreOutlined style={{ fontSize: 12 }} />
                      </span>
                    </Dropdown>
                  </div>
                ),
              }))
            : []),
        ]}
      />
    </>
  );

  return (
    <div className="doc-library">
      <div className="doc-library-sidebar">{sidebarContent}</div>

      <div className="doc-library-main">
        <div className="doc-library-toolbar">
          <Space size={12}>
            <Input
              prefix={<SearchOutlined style={{ color: '#94a3b8' }} />}
              placeholder={t('common.keywordSearch')}
              style={{ width: 240 }}
              value={inputValue}
              onChange={handleKeywordChange}
              allowClear
              onClear={() => {
                setInputValue('');
                setKeyword('');
              }}
            />
            <DocumentStatusFilter value={statusFilter} onChange={(p) => setStatusFilter(p)} />
          </Space>
          <Space size={12}>
            <Button
              icon={<ReloadOutlined />}
              onClick={() => {
                fetchList();
                onRefreshQuota();
              }}
            >
              {t('common.refresh')}
            </Button>
            <Button
              type="primary"
              icon={<PlusOutlined />}
              disabled={!canWrite || docReached}
              title={
                !canWrite
                  ? t('domainSpace.noManagePermission')
                  : quotaError
                    ? t('common.quotaLoadFailed')
                    : docReached
                      ? t('common.quotaDocReached')
                      : undefined
              }
              onClick={() => setModalOpen(true)}
            >
              {t('common.addDocument')}
            </Button>
            <Button icon={<SyncOutlined />} onClick={() => navigate(`/domain-knowledge/${kbId}/compile-results`)}>
              {t('common.fullCompileResults')}
            </Button>
            <Button
              icon={<FolderOutlined />}
              disabled={selectedRowKeys.length === 0}
              onClick={() => setMoveModal({ open: true, mode: 'batch' })}
            >
              {t('common.batchMove')}
            </Button>
            {isAdmin && (
              <Button icon={<DownloadOutlined />} disabled={selectedRowKeys.length === 0} onClick={handleBatchDownload}>
                {downloadProgress > 0
                  ? t('common.downloading', { i: downloadProgress, n: selectedRowKeys.length })
                  : t('common.batchDownload')}
              </Button>
            )}
          </Space>
        </div>

        <Table
          rowKey="id"
          columns={columns}
          dataSource={data}
          loading={loading}
          pagination={{
            current: page,
            pageSize,
            total,
            showSizeChanger: true,
            showTotal: (total) => t('common.totalItems', { total }),
            onChange: (p, ps) => {
              setPage(p);
              setPageSize(ps);
              setSelectedRowKeys([]);
            },
          }}
          onRow={(record) => ({
            draggable: true,
            onDragStart: handleRowDragStart(record),
            onDragEnd: handleRowDragEnd,
          })}
          rowClassName={(record) => (draggingDocId === record.id ? 'doc-library-row--dragging' : '')}
          rowSelection={{ type: 'checkbox', selectedRowKeys, onChange: setSelectedRowKeys }}
          size="middle"
          className="doc-library-table"
          scroll={{ x: 1100 }}
        />
      </div>
      <UploadModal
        kbId={kbId}
        open={modalOpen}
        defaultFolderId={selectedKeys[0] !== 'all' ? selectedKeys[0] : ''}
        onClose={() => setModalOpen(false)}
        onSuccess={handleUploadSuccess}
      />
      <FolderNameModal
        title={folderModalMode === 'create' ? t('common.newFolder') : t('common.renameFolder')}
        placeholder={folderModalMode === 'create' ? t('common.folderNamePlaceholder') : t('common.renamePlaceholder')}
        open={folderModalOpen}
        initialValue={folderModalValue}
        confirmLoading={folderModalLoading}
        onOk={handleFolderModalOk}
        onCancel={() => setFolderModalOpen(false)}
      />
      <TagModal
        open={tagModalOpen}
        kbId={kbId}
        docId={tagDoc?.id}
        docName={tagDoc?.name}
        onClose={() => {
          setTagModalOpen(false);
          setTagDoc(null);
        }}
      />
      <MoveDocModal
        open={moveModal.open}
        title={moveModal.mode === 'single' ? t('common.moveDocTitle') : t('common.moveBatchTitle')}
        folders={folders}
        disabledFolderId={moveModal.mode === 'single' ? (moveModal.doc?.folder_id ?? null) : undefined}
        confirmLoading={moveLoading}
        onOk={handleMoveOk}
        onCancel={() => setMoveModal({ open: false, mode: 'single' })}
      />
      {viewer}
    </div>
  );
}
