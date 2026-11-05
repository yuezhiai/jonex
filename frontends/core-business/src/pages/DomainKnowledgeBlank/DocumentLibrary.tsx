import React, { useState, useEffect, useCallback, useRef } from 'react';
import { useKbPermission } from '@/hooks/useKbPermission';
import { useNavigate } from 'react-router-dom';
import { Button, Space, Modal, Dropdown, message, Menu, Badge } from 'antd';
import {
  PlusOutlined,
  ReloadOutlined,
  FileOutlined,
  FolderOutlined,
  MoreOutlined,
  EditOutlined,
  DeleteOutlined,
  CaretDownOutlined,
  CaretRightOutlined,
  DownloadOutlined,
} from '@ant-design/icons';
import { CommonTable } from '@jonex/shared-lib';
import type { CommonTableRef, TableQueryParams } from '@jonex/shared-lib';
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
import { FILTER_PHASES, PHASE_DISPLAY, type DocPhase } from '@/utils/docPhase';
import { useDocumentViewer } from '@/components/DocumentViewer';
import UploadModal from './UploadModal';
import FolderNameModal from './FolderNameModal';
import MoveDocModal from './MoveDocModal';
import TagModal from './TagModal';
import ModelUpdateModal from './ModelUpdateModal';
import { useWikiCompilation } from '@/hooks/useWikiCompilation';
import WikiCompilationControl from '@/components/WikiCompilationControl';
import './index.scss';

// 用 type 而非 interface：需要隐式索引签名才能满足 CommonTable 的 F extends Record<string, unknown>
type DocQuery = { keyword?: string; phase?: DocPhase[] };

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
  /**
   * 刷新顶部文档统计栏（文档总数/各阶段计数）与页头三个标签：文档增删、点「刷新」后由本组件调用，
   * 避免统计栏、页头与列表三者脱节。
   */
  onRefreshStats?: () => void;
}

export default function DocumentLibrary({
  kbId,
  kbType,
  docReached,
  quotaError,
  onRefreshQuota,
  onRefreshStats,
}: DocumentLibraryProps) {
  // jonex_wiki 的编译控制（状态文字 + 暂停/恢复按钮）暂时隐藏。
  // 组件与调用代码都保留，恢复展示时把开关改回 true 即可。
  const SHOW_WIKI_COMPILATION_CONTROL = false;
  const { canWrite } = useKbPermission(kbId);
  const { t } = useTranslation();
  const navigate = useNavigate();
  const [selectedKeys, setSelectedKeys] = useState<string[]>(['all']);
  const [folders, setFolders] = useState<FolderItem[]>([]);
  const [modalOpen, setModalOpen] = useState(false);
  const [folderModalOpen, setFolderModalOpen] = useState(false);
  const [folderModalValue, setFolderModalValue] = useState('');
  const [folderModalLoading, setFolderModalLoading] = useState(false);
  const [folderModalMode, setFolderModalMode] = useState<'create' | 'rename'>('create');
  const [editingFolderId, setEditingFolderId] = useState('');
  const [foldersCollapsed, setFoldersCollapsed] = useState(false);
  const [tagModalOpen, setTagModalOpen] = useState(false);
  const [tagDoc, setTagDoc] = useState<ManualDocItem | null>(null);
  const [modelUpdateDoc, setModelUpdateDoc] = useState<ManualDocItem | null>(null);
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
  // 拖拽源文档所在目录。拖回原目录时直接提示，不发请求——列表数据已由 CommonTable 持有，
  // 这里记在拖拽开始时，比放着整份列表再去 find 稳。
  const [draggingDocFolderId, setDraggingDocFolderId] = useState<string | null>(null);
  const [dropTargetId, setDropTargetId] = useState<string | null>(null);
  const [accessMethods, setAccessMethods] = useState<AccessMethodItem[]>([]);
  const tableRef = useRef<CommonTableRef>(null);
  const compilation = useWikiCompilation(kbId, kbType === 'jonex_wiki');
  useEffect(() => {
    if (!compilation.state) return;
    tableRef.current?.refresh();
    onRefreshStats?.();
  }, [compilation.state]);
  // 切换目录要显式通知 CommonTable 重取（见下方 effect），首次挂载除外
  const folderEffectMounted = useRef(false);
  // 表体高度：antd 的固定表头只认具体像素。用 calc(100vh - N) 会在 hosted（iframe 内
  // 100vh 是 iframe 高度）下比 standalone 多算一个顶栏的高度，所以改成量宿主容器。
  // 取容器全高而非减去表头/分页：y 只是 max-height 上限，实际高度由 CSS 的 flex 分配决定。
  const tableHostRef = useRef<HTMLDivElement>(null);
  const [tableBodyHeight, setTableBodyHeight] = useState(400);

  useEffect(() => {
    const host = tableHostRef.current;
    if (!host) return;
    const sync = () => setTableBodyHeight(Math.max(host.clientHeight, 200));
    sync();
    const observer = new ResizeObserver(sync);
    observer.observe(host);
    return () => observer.disconnect();
  }, []);

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

  // 数据变更（上传/删除/重解析/移动等）与工具栏「刷新」按钮统一走这里：列表 + 配额（used 随之变化）
  // + 顶部统计栏与页头标签。统计刷新原先只挂在上传/删除两个调用点上，
  // 刷新按钮走的是本函数、漏了这条链路，文档数会停在旧值。
  const reload = useCallback(
    (options?: { resetPage?: boolean }) => {
      tableRef.current?.refresh(options);
      onRefreshQuota();
      onRefreshStats?.();
    },
    [onRefreshQuota, onRefreshStats],
  );

  const fetchDocs = useCallback(
    async (params: TableQueryParams<DocQuery>) => {
      const currentKey = selectedKeys[0];
      // 空值剔除由 CommonTable 的 buildQueryParams 负责，不必再写 || undefined
      const res = await getManualDocList({
        knowledgeBaseId: kbId,
        page: params.page,
        pageSize: params.pageSize,
        keyword: params.keyword,
        phase: params.phase,
        folder_id: currentKey !== 'all' ? String(currentKey) : undefined,
      });
      return { list: res.list, total: res.pagination.total };
    },
    [kbId, selectedKeys],
  );

  // 翻页或改筛选后清空行选中（避免批量移动/下载作用于当前数据集之外的旧选中）。
  // 返回同一引用，让本来就空的时候 React 直接跳过重渲染。
  const handleQueryChange = useCallback(() => {
    setSelectedRowKeys((prev) => (prev.length ? [] : prev));
  }, []);

  // selectedKeys 是页面自己的查询条件，CommonTable 看不见它变了，必须显式通知。
  // 首次挂载跳过——组件自己会取一次。
  useEffect(() => {
    if (!folderEffectMounted.current) {
      folderEffectMounted.current = true;
      return;
    }
    setSelectedRowKeys([]);
    tableRef.current?.refresh({ resetPage: true });
  }, [selectedKeys]);

  const handleUploadSuccess = () => {
    // 新增文档后：刷新列表 + 文件夹计数 + 统计（统计刷新在 reload 里，见上）
    fetchFolders();
    reload({ resetPage: true });
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

  const handleReparse = useCallback(
    async (record: ManualDocItem) => {
      try {
        await reparseDocument(record.id);
        message.success(t('common.retryTriggered'));
        reload();
      } catch (err: any) {
        message.error(err?.message || t('common.retryFailed'));
      }
    },
    [reload],
  );

  const handleRecompile = useCallback(
    async (record: ManualDocItem) => {
      try {
        await retryDocumentOntology(kbId, record.id);
        message.success(t('common.recompileTriggered'));
        reload();
      } catch (err: any) {
        message.error(err?.message || t('common.recompileFailed'));
      }
    },
    [kbId, reload],
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
            // 删除后：刷新列表 + 文件夹计数 + 统计（统计刷新在 reload 里，见上）
            fetchFolders();
            reload();
          } catch (err: any) {
            message.error(err?.message || t('common.deleteFailed'));
          }
        },
      });
    },
    [kbId, t, fetchFolders, reload],
  );

  const handleMove = useCallback((record: ManualDocItem) => {
    setMoveModal({ open: true, mode: 'single', doc: record });
  }, []);

  const handleMoveOk = async (folderId: string | null) => {
    const docIds = moveModal.mode === 'batch' ? selectedRowKeys.map(String) : moveModal.doc ? [moveModal.doc.id] : [];
    if (!docIds.length) return;
    setMoveLoading(true);
    try {
      await batchMoveDocuments(kbId, docIds, folderId);
      // 接口成功即提示，不显示移动数量
      message.success(t('common.moveSuccess'));
      setMoveModal({ open: false, mode: 'single' });
      setSelectedRowKeys([]);
      fetchFolders();
      reload();
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
      setDraggingDocFolderId(record.folder_id ?? null);
    },
    [],
  );

  const handleRowDragEnd = useCallback(() => {
    setDraggingDocId(null);
    setDraggingDocFolderId(null);
    setDropTargetId(null);
  }, []);

  const handleDragMove = useCallback(
    async (targetFolderId: string | null) => {
      if (!draggingDocId) return;
      try {
        await batchMoveDocuments(kbId, [draggingDocId], targetFolderId);
        message.success(t('common.moveSuccess'));
        fetchFolders();
        reload();
      } catch (err: any) {
        message.error(err?.message);
      } finally {
        setDraggingDocId(null);
        setDraggingDocFolderId(null);
        setDropTargetId(null);
      }
    },
    [draggingDocId, kbId, t, fetchFolders, reload],
  );

  const handleFolderDrop = useCallback(
    (targetFolderId: string | null) => (e: React.DragEvent<HTMLElement>) => {
      e.preventDefault();
      e.stopPropagation();
      if (!draggingDocId) return;
      setDropTargetId(null);
      // 拖到文档当前所在目录（含同为根目录）→ 不发请求，提示
      if (draggingDocFolderId === targetFolderId) {
        message.info(t('common.moveInCurrentFolder'));
        return;
      }
      handleDragMove(targetFolderId);
    },
    [draggingDocId, draggingDocFolderId, t, handleDragMove],
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
      onUpdateModel: setModelUpdateDoc,
      onDelete: handleDelete,
      onMove: handleMove,
    },
    accessMethods,
    kbType,
    canWrite,
    compilation.state?.paused,
    // 行操作菜单里的「暂停/恢复自动编译」与顶部状态条共用同一个开关：
    // 传 undefined 时 actionItems 不生成该菜单项（关闭时整个消失，而非置灰）。
    SHOW_WIKI_COMPILATION_CONTROL && kbType === 'jonex_wiki' ? () => void compilation.toggle() : undefined,
    !compilation.canToggle,
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
                    <span
                      style={{
                        overflow: 'hidden',
                        textOverflow: 'ellipsis',
                        whiteSpace: 'nowrap',
                        flex: 1,
                        minWidth: 0,
                      }}
                    >
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
        <div className="doc-library-table-host" ref={tableHostRef}>
          <CommonTable<ManualDocItem, DocQuery>
            ref={tableRef}
            className="doc-library-table"
            filters={[
              {
                key: 'keyword',
                label: t('common.documentName'),
                type: 'input',
                placeholder: t('common.keywordSearch'),
              },
              {
                key: 'phase',
                label: t('common.documentStatus'),
                type: 'select',
                mode: 'multiple',
                placeholder: t('common.all'),
                maxTagCount: 3,
                options: FILTER_PHASES.map((phase) => ({
                  value: phase,
                  label: t(PHASE_DISPLAY[phase].label),
                })),
              },
            ]}
            // 两个内置按钮都关掉：页面自己的「刷新」还要连带 onRefreshQuota，改用 toolbar 里那个；
            // 「重置」在本页对用户没意义（筛选就两项，清空用控件自带的 allowClear 更快）
            actions={{ refresh: false, reset: false }}
            toolbar={
              <>
                {SHOW_WIKI_COMPILATION_CONTROL && kbType === 'jonex_wiki'
                  && <WikiCompilationControl kbId={kbId} control={compilation} compact showToggle={false} />}
                <Button icon={<ReloadOutlined />} onClick={() => reload()}>
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
                <Button
                  icon={<FolderOutlined />}
                  disabled={selectedRowKeys.length === 0}
                  onClick={() => setMoveModal({ open: true, mode: 'batch' })}
                >
                  {t('common.batchMove')}
                </Button>
                {isAdmin && (
                  <Button
                    icon={<DownloadOutlined />}
                    disabled={selectedRowKeys.length === 0}
                    onClick={handleBatchDownload}
                  >
                    {downloadProgress > 0
                      ? t('common.downloading', { i: downloadProgress, n: selectedRowKeys.length })
                      : t('common.batchDownload')}
                  </Button>
                )}
              </>
            }
            rowKey="id"
            columns={columns}
            size="middle"
            scroll={{ x: 1100, y: tableBodyHeight }}
            rowSelection={{ type: 'checkbox', selectedRowKeys, onChange: setSelectedRowKeys }}
            onRow={(record) => ({
              draggable: true,
              onDragStart: handleRowDragStart(record),
              onDragEnd: handleRowDragEnd,
            })}
            rowClassName={(record) => (draggingDocId === record.id ? 'doc-library-row--dragging' : '')}
            onQueryChange={handleQueryChange}
            fetch={fetchDocs}
            onError={(err: any) => message.error(err?.message || t('common.docListLoadFailed'))}
          />
        </div>
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
      <ModelUpdateModal
        open={modelUpdateDoc !== null}
        kbId={kbId}
        docId={modelUpdateDoc?.id ?? ''}
        docName={modelUpdateDoc?.name ?? ''}
        onClose={() => setModelUpdateDoc(null)}
        onApplied={() => reload()}
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
