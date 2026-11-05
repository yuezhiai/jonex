import React, { useCallback, useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Alert, Button, Empty, Modal, Popconfirm, Space, Switch, Table, Tag, Tooltip, message } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { ApiOutlined, DeleteOutlined, EditOutlined, PlayCircleOutlined, PlusOutlined } from '@ant-design/icons';
import type { BindableTool, ToolBinding } from '@/types/domainKnowledge';
import { useToolBindings } from '@/hooks/useToolBindings';
import {
  createToolBinding,
  deleteToolBinding,
  listBindableTools,
  setToolBindingEnabled,
  setToolBindingQaCallable,
  updateToolBinding,
  type CreateToolBindingInput,
  type UpdateToolBindingInput,
} from '@/api/ontologyToolBinding';
import ActionExecuteDrawer from './ActionExecuteDrawer';
import ToolBindingFormModal from './ToolBindingFormModal';

interface Props {
  kbId: string;
  /** KB 写权限（权限矩阵） */
  canWrite?: boolean;
}

export default function ToolBindingTab({ kbId, canWrite = false }: Props) {
  const { t } = useTranslation();
  const { data, total, page, pageSize, loading, error, refresh, setPage } = useToolBindings(kbId);
  const [formOpen, setFormOpen] = useState(false);
  // 正在编辑的绑定；null = 新建
  const [editing, setEditing] = useState<ToolBinding | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [togglingId, setTogglingId] = useState<string | null>(null);
  const [qaTogglingId, setQaTogglingId] = useState<string | null>(null);
  const [executeTarget, setExecuteTarget] = useState<string | null>(null);
  const [tools, setTools] = useState<BindableTool[]>([]);
  const [toolsLoading, setToolsLoading] = useState(false);

  const loadTools = useCallback(() => {
    setToolsLoading(true);
    listBindableTools(kbId)
      .then(setTools)
      .catch((e: any) => {
        setTools([]);
        message.error(e?.message || t('common.operationFailed'));
      })
      .finally(() => setToolsLoading(false));
  }, [kbId, t]);

  useEffect(() => {
    if (formOpen) loadTools();
  }, [formOpen, loadTools]);

  const handleCreate = async (input: CreateToolBindingInput) => {
    setSubmitting(true);
    try {
      await createToolBinding(kbId, input);
      message.success(t('common.saveSuccess'));
      closeForm();
      refresh();
    } catch (e: any) {
      message.error(e?.message || t('common.saveFailed'));
    } finally {
      setSubmitting(false);
    }
  };

  const openCreate = () => {
    setEditing(null);
    setFormOpen(true);
  };

  const openEdit = (record: ToolBinding) => {
    setEditing(record);
    setFormOpen(true);
  };

  const closeForm = () => {
    setFormOpen(false);
    setEditing(null);
  };

  const handleUpdate = async (record: ToolBinding, input: UpdateToolBindingInput) => {
    // 什么都没改：直接关掉，不发请求
    if (!Object.keys(input).length) {
      closeForm();
      return;
    }
    setSubmitting(true);
    try {
      await updateToolBinding(record.id, input);
      message.success(t('common.saveSuccess'));
      closeForm();
      refresh();
    } catch (e: any) {
      message.error(e?.message || t('common.saveFailed'));
    } finally {
      setSubmitting(false);
    }
  };

  const handleToggle = async (record: ToolBinding, enabled: boolean) => {
    setTogglingId(record.id);
    try {
      await setToolBindingEnabled(record.id, enabled);
      message.success(t('common.saveSuccess'));
      refresh();
    } catch (e: any) {
      message.error(e?.message || t('common.saveFailed'));
    } finally {
      setTogglingId(null);
    }
  };

  const saveQaCallable = async (record: ToolBinding, qaCallable: boolean) => {
    setQaTogglingId(record.id);
    try {
      await setToolBindingQaCallable(record.id, qaCallable);
      message.success(t('common.saveSuccess'));
      refresh();
    } catch (e: any) {
      message.error(e?.message || t('common.saveFailed'));
    } finally {
      setQaTogglingId(null);
    }
  };

  // 打开问答调用前提示数据外发；关闭不需要确认
  const handleQaToggle = (record: ToolBinding, qaCallable: boolean) => {
    if (!qaCallable) {
      saveQaCallable(record, false);
      return;
    }
    Modal.confirm({
      title: t('toolBinding.qaCallableEnableTitle'),
      content: (
        <>
          <p style={{ marginBottom: 6 }}>{t('toolBinding.qaCallableEgress')}</p>
          {!record.toolReadOnly && <p style={{ margin: 0 }}>{t('toolBinding.qaCallableNotReadOnly')}</p>}
        </>
      ),
      okText: t('common.confirm'),
      cancelText: t('common.cancel'),
      onOk: () => saveQaCallable(record, true),
    });
  };

  const handleDelete = async (record: ToolBinding) => {
    try {
      await deleteToolBinding(record.id);
      message.success(t('common.deleteSuccess'));
      refresh();
    } catch (e: any) {
      message.error(e?.message || t('common.deleteFailed'));
    }
  };

  const columns: ColumnsType<ToolBinding> = [
    {
      title: t('toolBinding.colEntity'),
      key: 'entity',
      render: (_, r) => (
        <Space size={4} wrap>
          <Tag color={r.entityType === 'Skill' ? 'purple' : 'blue'}>{r.entityType}</Tag>
          <span>{r.canonicalName}</span>
          {!r.executable && (
            <Tooltip title={t('toolBinding.legacyTip')}>
              <Tag color="default">{t('toolBinding.legacyTag')}</Tag>
            </Tooltip>
          )}
        </Space>
      ),
    },
    {
      title: t('toolBinding.colTool'),
      key: 'tool',
      render: (_, r) => (
        <Space size={4}>
          <Tag color={r.toolSource === 'kb' ? 'cyan' : 'geekblue'}>
            {r.toolSource === 'kb' ? t('toolBinding.sourceKb') : t('toolBinding.sourcePlatform')}
          </Tag>
          <span>
            {r.toolLabel || r.toolName}
            {r.toolLabel ? <span style={{ color: '#94a3b8' }}>（{r.toolName}）</span> : null}
          </span>
        </Space>
      ),
    },
    {
      title: t('toolBinding.colMapping'),
      key: 'mapping',
      render: (_, r) => {
        const entries = Object.entries(r.paramMapping || {});
        if (!entries.length) return <span style={{ color: '#94a3b8' }}>{t('toolBinding.noMapping')}</span>;
        return (
          <Space size={[4, 4]} wrap>
            {entries.map(([k, v]) => (
              <Tag key={k}>
                {k} ← {String(v)}
              </Tag>
            ))}
          </Space>
        );
      },
    },
    {
      title: t('toolBinding.colStatus'),
      key: 'status',
      width: 160,
      render: (_, r) => {
        const tags: React.ReactNode[] = [];
        if (r.targetExists === false) tags.push(<Tag key="t" color="red">{t('toolBinding.targetMissing')}</Tag>);
        if (r.targetExists === null) tags.push(<Tag key="u" color="default">{t('toolBinding.targetUnknown')}</Tag>);
        if (!r.toolAvailable) tags.push(<Tag key="x" color="orange">{t('toolBinding.toolUnavailable')}</Tag>);
        return tags.length ? <Space size={[4, 4]} wrap>{tags}</Space> : <Tag color="green">{t('toolBinding.statusOk')}</Tag>;
      },
    },
    {
      title: t('toolBinding.colEnabled'),
      key: 'enabled',
      width: 90,
      render: (_, r) => (
        <Switch
          size="small"
          checked={r.enabled}
          disabled={!canWrite}
          loading={togglingId === r.id}
          onChange={(checked) => handleToggle(r, checked)}
        />
      ),
    },
    {
      title: t('toolBinding.colQaCallable'),
      key: 'qaCallable',
      width: 110,
      render: (_, r) => {
        if (!r.executable) return <span style={{ color: '#94a3b8' }}>—</span>;
        return (
          <Space size={4}>
            <Switch
              size="small"
              checked={r.qaCallable}
              disabled={!canWrite}
              loading={qaTogglingId === r.id}
              onChange={(checked) => handleQaToggle(r, checked)}
            />
            {r.qaCallable && !r.toolReadOnly && (
              <Tooltip title={t('toolBinding.qaCallableTipOff')}>
                <Tag color="orange" style={{ marginInlineEnd: 0 }}>!</Tag>
              </Tooltip>
            )}
          </Space>
        );
      },
    },
    {
      title: t('common.actions'),
      key: 'actions',
      width: 230,
      render: (_, r) => {
        const disabledReason = !r.executable
          ? t('toolBinding.trialExecuteDisabledLegacy')
          : !r.enabled
            ? t('toolBinding.trialExecuteDisabledOff')
            : !r.toolAvailable
              ? t('toolBinding.trialExecuteDisabledTool')
              : !canWrite
                ? t('domainSpace.noManagePermission')
                : null;
        return (
          <Space size={12}>
            <Tooltip title={disabledReason || undefined}>
              <a
                className="yx-table-action"
                style={disabledReason ? { opacity: 0.4, cursor: 'not-allowed' } : undefined}
                onClick={() => !disabledReason && setExecuteTarget(r.canonicalName)}
              >
                <PlayCircleOutlined /> {t('toolBinding.trialExecute')}
              </a>
            </Tooltip>
            <Tooltip title={canWrite ? undefined : t('toolBinding.editDisabledNoPerm')}>
              <a
                className="yx-table-action"
                style={canWrite ? undefined : { opacity: 0.4, cursor: 'not-allowed' }}
                onClick={() => canWrite && openEdit(r)}
              >
                <EditOutlined /> {t('toolBinding.edit')}
              </a>
            </Tooltip>
            {renderDelete(r)}
          </Space>
        );
      },
    },
  ];

  function renderDelete(r: ToolBinding) {
    return (
        <Popconfirm
          title={t('toolBinding.deleteTitle')}
          description={t('toolBinding.deleteDesc')}
          okText={t('toolBinding.deleteOkText')}
          cancelText={t('common.cancel')}
          okButtonProps={{ danger: true }}
          disabled={!canWrite}
          onConfirm={() => handleDelete(r)}
        >
          <a
            className="yx-table-action"
            style={canWrite ? { color: '#ef4444' } : { color: '#ef4444', opacity: 0.4, cursor: 'not-allowed' }}
          >
            <DeleteOutlined /> {t('common.delete')}
          </a>
        </Popconfirm>
    );
  }

  return (
    <div>
      <div className="yx-kb-flex-header">
        <h3 className="yx-kb-section-title">
          <ApiOutlined className="yx-kb-icon-blue" /> {t('toolBinding.title')}
        </h3>
        <Space>
          <Tooltip title={canWrite ? undefined : t('domainSpace.noManagePermission')}>
            <Button type="primary" icon={<PlusOutlined />} disabled={!canWrite} onClick={openCreate}>
              {t('toolBinding.create')}
            </Button>
          </Tooltip>
        </Space>
      </div>

      <Alert type="info" showIcon style={{ marginBottom: 12 }} message={t('toolBinding.hint')} />

      {error && (
        <Alert
          type="error"
          showIcon
          style={{ marginBottom: 12 }}
          message={error}
          action={<a onClick={refresh}>{t('common.retry')}</a>}
        />
      )}

      <Table
        columns={columns}
        dataSource={data}
        rowKey="id"
        size="middle"
        loading={loading}
        locale={{ emptyText: <Empty description={t('toolBinding.empty')} /> }}
        pagination={{
          current: page,
          pageSize,
          total,
          showTotal: (n) => t('toolBinding.totalItems', { total: n }),
          onChange: setPage,
        }}
      />

      <ToolBindingFormModal
        open={formOpen}
        kbId={kbId}
        tools={tools}
        toolsLoading={toolsLoading}
        submitting={submitting}
        onCancel={closeForm}
        onSubmit={handleCreate}
        editing={editing}
        onUpdate={handleUpdate}
      />

      <ActionExecuteDrawer
        open={!!executeTarget}
        kbId={kbId}
        actionName={executeTarget || ''}
        onClose={() => setExecuteTarget(null)}
      />
    </div>
  );
}
