// 「本库工具」页签：本库登记的 API 工具（增删改、启停、试运行）+ 本租户已启用的平台工具（只读）。
import React, { useCallback, useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import {
  Alert,
  Button,
  Empty,
  Popconfirm,
  Space,
  Switch,
  Table,
  Tag,
  Tooltip,
  Typography,
  message,
} from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { DeleteOutlined, EditOutlined, PlayCircleOutlined, PlusOutlined, ReloadOutlined, ToolOutlined } from '@ant-design/icons';
import type { BindableTool, KbTool } from '@/types/domainKnowledge';
import { createKbTool, deleteKbTool, listKbTools, updateKbTool, type SaveKbToolPayload } from '@/api/kbTool';
import { listBindableTools } from '@/api/ontologyToolBinding';
import KbToolFormModal from './KbToolFormModal';
import KbToolTestModal from './KbToolTestModal';

const { Text } = Typography;

interface Props {
  kbId: string;
  canWrite: boolean;
}

export default function KbToolsPanel({ kbId, canWrite }: Props) {
  const { t } = useTranslation();
  const [tools, setTools] = useState<KbTool[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [platformTools, setPlatformTools] = useState<BindableTool[]>([]);
  const [platformLoading, setPlatformLoading] = useState(true);
  const [platformError, setPlatformError] = useState<string | null>(null);
  const [togglingId, setTogglingId] = useState<string | null>(null);
  const [editing, setEditing] = useState<KbTool | null>(null);
  const [formOpen, setFormOpen] = useState(false);
  const [testing, setTesting] = useState<KbTool | null>(null);

  const load = useCallback(() => {
    setLoading(true);
    setError(null);
    listKbTools(kbId)
      .then((res) => setTools(res.items))
      .catch((e: any) => setError(e?.message || t('kbTool.loadFailed')))
      .finally(() => setLoading(false));
  }, [kbId, t]);

  const loadPlatform = useCallback(() => {
    setPlatformLoading(true);
    setPlatformError(null);
    listBindableTools(kbId)
      .then((items) => setPlatformTools(items.filter((x) => x.source === 'platform')))
      .catch((e: any) => setPlatformError(e?.message || t('kbTool.loadFailed')))
      .finally(() => setPlatformLoading(false));
  }, [kbId, t]);

  useEffect(() => {
    load();
    loadPlatform();
  }, [load, loadPlatform]);

  const handleSubmit = async (payload: SaveKbToolPayload) => {
    if (editing) {
      await updateKbTool(editing.id, payload);
    } else {
      await createKbTool(kbId, payload);
    }
    message.success(t('common.saveSuccess'));
    load();
  };

  const handleToggle = async (tool: KbTool, enabled: boolean) => {
    setTogglingId(tool.id);
    try {
      await updateKbTool(tool.id, { enabled });
      message.success(t('common.saveSuccess'));
      load();
    } catch (e: any) {
      message.error(e?.message || t('common.saveFailed'));
    } finally {
      setTogglingId(null);
    }
  };

  const handleDelete = async (tool: KbTool) => {
    try {
      await deleteKbTool(tool.id);
      message.success(t('common.deleteSuccess'));
      load();
    } catch (e: any) {
      message.error(e?.message || t('common.deleteFailed'));
    }
  };

  const disabledStyle = { opacity: 0.4, cursor: 'not-allowed' } as const;

  const columns: ColumnsType<KbTool> = [
    {
      title: t('kbTool.colName'),
      key: 'name',
      render: (_, r) => (
        <Space direction="vertical" size={0}>
          <Text strong>{r.name}</Text>
          <Text code style={{ fontSize: 12 }}>
            {r.toolName}
          </Text>
        </Space>
      ),
    },
    {
      title: t('kbTool.colRuntime'),
      key: 'runtime',
      render: (_, r) => {
        const b = r.runtimeBrief;
        const modeTag = r.readOnly
          ? <Tag color="cyan">{t('toolRuntime.readOnlyTag')}</Tag>
          : <Tag color="volcano">{t('toolRuntime.writeTag')}</Tag>;
        if (!b) return <Space size={4}><Tag>{t('kbTool.noRuntime')}</Tag>{modeTag}</Space>;
        if (b.type === 'echo') return <Space size={4}><Tag color="purple">echo</Tag>{modeTag}</Space>;
        return (
          <Space direction="vertical" size={2} style={{ maxWidth: 320 }}>
            <Space size={4}>
              <Tag color="geekblue">{b.method}</Tag>
              {modeTag}
              {b.authType && b.authType !== 'none' && (
                <Tag color={r.hasSecret ? 'green' : 'orange'}>
                  {r.hasSecret ? t('kbTool.authReady') : t('kbTool.authMissing')}
                </Tag>
              )}
            </Space>
            <Text type="secondary" ellipsis={{ tooltip: b.url }} style={{ fontSize: 12, maxWidth: 320 }}>
              {b.url}
            </Text>
          </Space>
        );
      },
    },
    {
      title: t('kbTool.colInputs'),
      key: 'inputs',
      render: (_, r) => {
        const props = Object.keys((r.inputSchema?.properties || {}) as Record<string, unknown>);
        const required = new Set<string>((r.inputSchema?.required || []) as string[]);
        if (!props.length) return <Text type="secondary">-</Text>;
        return (
          <Space size={[4, 4]} wrap>
            {props.map((p) => (
              <Tag key={p}>{required.has(p) ? `${p}*` : p}</Tag>
            ))}
          </Space>
        );
      },
    },
    {
      title: t('kbTool.colEnabled'),
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
      title: t('common.actions'),
      key: 'actions',
      width: 220,
      render: (_, r) => (
        <Space size={12}>
          <a
            className="yx-table-action"
            style={canWrite ? undefined : disabledStyle}
            onClick={() => canWrite && setTesting(r)}
          >
            <PlayCircleOutlined /> {t('kbTool.test')}
          </a>
          <a
            className="yx-table-action"
            style={canWrite ? undefined : disabledStyle}
            onClick={() => {
              if (!canWrite) return;
              setEditing(r);
              setFormOpen(true);
            }}
          >
            <EditOutlined /> {t('common.edit')}
          </a>
          <Popconfirm
            title={t('kbTool.deleteTitle')}
            description={t('kbTool.deleteDesc')}
            okText={t('common.delete')}
            cancelText={t('common.cancel')}
            okButtonProps={{ danger: true }}
            disabled={!canWrite}
            onConfirm={() => handleDelete(r)}
          >
            <a className="yx-table-action" style={canWrite ? { color: '#ef4444' } : { color: '#ef4444', ...disabledStyle }}>
              <DeleteOutlined /> {t('common.delete')}
            </a>
          </Popconfirm>
        </Space>
      ),
    },
  ];

  const platformColumns: ColumnsType<BindableTool> = [
    {
      title: t('kbTool.colName'),
      key: 'name',
      render: (_, r) => (
        <Space direction="vertical" size={0}>
          <Text strong>{r.name}</Text>
          <Text code style={{ fontSize: 12 }}>
            {r.toolName}
          </Text>
        </Space>
      ),
    },
    {
      title: t('kbTool.colDescription'),
      dataIndex: 'description',
      render: (v: string) => (
        <Text type="secondary" ellipsis={{ tooltip: v }} style={{ maxWidth: 420 }}>
          {v || '-'}
        </Text>
      ),
    },
  ];

  return (
    <div>
      <div className="yx-kb-flex-header">
        <h3 className="yx-kb-section-title">
          <ToolOutlined className="yx-kb-icon-blue" /> {t('kbTool.title')}
        </h3>
        <Space>
          <Button icon={<ReloadOutlined />} onClick={load}>
            {t('common.refresh')}
          </Button>
          <Tooltip title={canWrite ? undefined : t('domainSpace.noManagePermission')}>
            <Button
              type="primary"
              icon={<PlusOutlined />}
              disabled={!canWrite}
              onClick={() => {
                setEditing(null);
                setFormOpen(true);
              }}
            >
              {t('kbTool.create')}
            </Button>
          </Tooltip>
        </Space>
      </div>
      <Alert type="info" showIcon style={{ marginBottom: 12 }} message={t('kbTool.hint')} />
      {error && (
        <Alert
          type="error"
          showIcon
          style={{ marginBottom: 12 }}
          message={error}
          action={<a onClick={load}>{t('common.retry')}</a>}
        />
      )}
      <Table<KbTool>
        rowKey="id"
        size="middle"
        columns={columns}
        dataSource={tools}
        loading={loading}
        pagination={false}
        locale={{ emptyText: <Empty description={t('kbTool.empty')} /> }}
      />

      <h3 className="yx-kb-section-title" style={{ marginTop: 28 }}>
        {t('kbTool.platformTitle')}
      </h3>
      <Alert type="info" showIcon style={{ marginBottom: 12 }} message={t('kbTool.platformHint')} />
      {platformError && (
        <Alert
          type="error"
          showIcon
          style={{ marginBottom: 12 }}
          message={platformError}
          action={<a onClick={loadPlatform}>{t('common.retry')}</a>}
        />
      )}
      <Table<BindableTool>
        rowKey={(r) => `${r.source}:${r.toolName}`}
        size="small"
        columns={platformColumns}
        dataSource={platformTools}
        loading={platformLoading}
        pagination={false}
        locale={{ emptyText: <Empty description={t('kbTool.platformEmpty')} /> }}
      />

      <KbToolFormModal
        open={formOpen}
        tool={editing}
        onClose={() => {
          setFormOpen(false);
          setEditing(null);
        }}
        onSubmit={handleSubmit}
      />
      <KbToolTestModal open={!!testing} tool={testing} onClose={() => setTesting(null)} />
    </div>
  );
}
