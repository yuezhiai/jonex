import React, { useState, useEffect, useCallback, useRef } from 'react';
import { useNavigate } from 'react-router-dom';
import { Input, Button, Table, Space, Modal, Dropdown, message, Menu } from 'antd';
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
} from '@/api/domainKnowledge';
import { listAccessMethods } from '@/api/dataSource';
import type { ManualDocItem, FolderItem } from '@/types/domainKnowledge';
import type { AccessMethodItem } from '@/types/dataSource';
import type { DocPhase } from '@/utils/docPhase';
import { useDocumentViewer } from '@/components/DocumentViewer';
import DocumentStatusFilter from '@/components/DocumentStatusFilter';
import UploadModal from './UploadModal';
import FolderNameModal from './FolderNameModal';
import TagModal from './TagModal';
import './index.scss';

const PAGE_SIZE = 10;

interface DocumentLibraryProps {
  kbId: string;
  /** [jonex] 知识库类型：openkb 时文档状态列按 llm_wiki_compile_status 显示。 */
  kbType?: string;
}

export default function DocumentLibrary({ kbId, kbType }: DocumentLibraryProps) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const [selectedKeys, setSelectedKeys] = useState<string[]>(['all']);
  const [folders, setFolders] = useState<FolderItem[]>([]);
  const [data, setData] = useState<ManualDocItem[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
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
  const [accessMethods, setAccessMethods] = useState<AccessMethodItem[]>([]);
  const debounceRef = useRef<ReturnType<typeof setTimeout>>();

  const fetchFolders = useCallback(() => {
    if (!kbId) return;
    getFolderList(kbId)
      .then((res) => setFolders(res.items))
      .catch(() => message.error(t('common.folderListLoadFailed')));
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
        pageSize: PAGE_SIZE,
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
  }, [kbId, page, keyword, selectedKeys, statusFilter]);

  useEffect(() => {
    fetchList();
  }, [fetchList, reloadFlag]);

  // 关键词、文件夹或状态变化时，重置页码
  useEffect(() => {
    setPage(1);
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

  const columns = createColumns(
    t,
    {
      onView: handleView,
      onTag: handleOpenTag,
      onViewResult: handleViewResult,
      onReparse: handleReparse,
      onRecompile: handleRecompile,
      onDelete: handleDelete,
    },
    accessMethods,
    kbType,
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
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', width: '100%' }}>
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
                  >
                    <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', flex: 1 }}>
                      {f.name}
                    </span>
                    <Dropdown
                      menu={{
                        items: [
                          {
                            key: 'rename',
                            icon: <EditOutlined />,
                            label: t('common.rename'),
                            onClick: ({ domEvent }) => {
                              domEvent.stopPropagation();
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
                            onClick: ({ domEvent }) => {
                              domEvent.stopPropagation();
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
            <Button icon={<ReloadOutlined />} onClick={() => fetchList()}>
              {t('common.refresh')}
            </Button>
            <Button type="primary" icon={<PlusOutlined />} onClick={() => setModalOpen(true)}>
              {t('common.addDocument')}
            </Button>
            <Button icon={<SyncOutlined />} onClick={() => navigate(`/domain-knowledge/${kbId}/compile-results`)}>
              {t('common.fullCompileResults')}
            </Button>
          </Space>
        </div>

        <Table
          rowKey="id"
          columns={columns}
          dataSource={data}
          loading={loading}
          pagination={{
            current: page,
            pageSize: PAGE_SIZE,
            total,
            showSizeChanger: false,
            showTotal: (total) => t('common.totalItems', { total }),
            onChange: (p) => setPage(p),
          }}
          rowSelection={{ type: 'checkbox' }}
          size="middle"
          className="doc-library-table"
          scroll={{ y: 400, x: 1100 }}
        />
      </div>
      <UploadModal kbId={kbId} open={modalOpen} onClose={() => setModalOpen(false)} onSuccess={handleUploadSuccess} />
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
      {viewer}
    </div>
  );
}
