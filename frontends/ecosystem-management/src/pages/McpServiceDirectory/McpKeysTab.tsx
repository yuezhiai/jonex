import React, { useState, useRef, useEffect, useMemo, useCallback } from 'react';
import { useTranslation } from 'react-i18next';
import {
  Table,
  Tag,
  Typography,
  Input,
  Empty,
  Space,
  Button,
  Spin,
  Result,
  Modal,
  message,
  Card,
  Alert,
} from 'antd';
import { PlusOutlined, SearchOutlined, ReloadOutlined } from '@ant-design/icons';
import type { ColumnsType } from 'antd/es/table';
import { listMcpKeys, deleteMcpKey, revokeMcpKey, type McpKeyItem } from '@/api/mcpKeys';
import { McpKeyModals, type McpKeyModalsHandle } from './McpKeyModals';
import apiClient from '@/api/client';
import { listMcpServices } from '@/api/mcpServices';
import KeyDetailDrawer, { type KeyDetailDrawerHandle } from './KeyDetailDrawer';
import './index.css';

// MCP Server 地址：优先环境变量注入（构建时替换），本地开发默认 localhost:8002
const mcpServerUrl = (import.meta as any).env?.VITE_MCP_SERVER_URL || 'http://localhost:8002/mcp';

// WorkBuddy 连接配置的 JSON 示例（供一键复制）
const workbuddyJsonConfig = `{
  "mcpServers": {
    "jonex-knowledge": {
      "url": "${mcpServerUrl}",
      "transport": "streamable-http",
      "headers": {
        "Authorization": "Bearer YOUR_MCP_KEY"
      }
    }
  }
}`;

const defaultFormatDate = (d: string | null): string => {
  if (!d) return '-';
  try {
    return new Date(d).toISOString().slice(0, 10);
  } catch {
    return d;
  }
};

/** MCP 服务目录 — MCP Key 列表 Tab（含完整列表管理能力） */
export default function McpKeysTab() {
  const { t } = useTranslation();
  const [keys, setKeys] = useState<McpKeyItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [loaded, setLoaded] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [search, setSearch] = useState('');
  // 是否有 Key（控制 WorkBuddy 引导显隐）
  const [hasKeys, setHasKeys] = useState(false);
  // 知识库 / 领域服务下拉选项（供 Key 创建/编辑弹窗使用）
  const [kbOptions, setKbOptions] = useState<{ label: string; value: string }[]>([]);
  const [serviceOptions, setServiceOptions] = useState<{ label: string; value: string }[]>([]);
  // 领域服务 ID → 名称映射（列表展示用，来源全量服务确保都能映射名称）
  const [serviceNameMap, setServiceNameMap] = useState<Map<string, string>>(new Map());
  // 知识库 ID → 名称映射（列表展示用）
  const kbNameMap = useMemo(() => {
    const map = new Map<string, string>();
    kbOptions.forEach((o) => map.set(o.value, o.label));
    return map;
  }, [kbOptions]);

  const modalsRef = useRef<McpKeyModalsHandle>(null);
  const detailDrawerRef = useRef<KeyDetailDrawerHandle>(null);

  /** 加载 Key 列表 */
  const loadKeys = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const result = await listMcpKeys();
      setKeys(result.items || []);
      setLoaded(true);
      setHasKeys((result.items || []).length > 0);
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : t('mcpKeyManagement.loadFailed'));
    } finally {
      setLoading(false);
    }
  }, [t]);

  useEffect(() => {
    if (!loaded) loadKeys();
  }, [loaded, loadKeys]);

  // 知识库 / 领域服务下拉选项
  useEffect(() => {
    // 知识库范围选项（GET /knowledge-base/knowledge-info）
    apiClient
      .get('/api/v1/knowledge-base/knowledge-info')
      .then((resp) => {
        const data = (
          resp.data as {
            data?: { items?: Array<{ id: string; name: string }>; list?: Array<{ id: string; name: string }> };
          }
        )?.data;
        const items = data?.items ?? data?.list ?? [];
        setKbOptions(items.map((kb) => ({ label: kb.name ?? kb.id, value: kb.id })));
      })
      .catch(() => {
        // 失败静默，下拉为空
      });
    // 全部领域服务 → 列表名称映射（含未发布服务，确保 service_ids 都能显示名称）
    listMcpServices()
      .then((result) => {
        const map = new Map<string, string>();
        result.items.forEach((s) => map.set(s.id, s.name));
        setServiceNameMap(map);
      })
      .catch(() => {
        // 失败静默，映射为空
      });
    // 已发布领域服务选项（service_ids 下拉范围）
    listMcpServices({ status: 'published' })
      .then((result) => {
        setServiceOptions(result.items.map((s) => ({ label: s.name, value: s.id })));
      })
      .catch(() => {
        // 失败静默，下拉为空
      });
  }, []);

  // 搜索过滤（客户端按名称 / 前缀过滤）
  const filtered = useMemo(() => {
    const q = search.trim().toLocaleLowerCase();
    if (!q) return keys;
    return keys.filter((k) =>
      `${k.name || ''} ${k.key_prefix || ''} yxm_${k.key_prefix || ''}`.toLocaleLowerCase().includes(q),
    );
  }, [keys, search]);

  // ── 删除 / 停用确认 ──
  const confirmDelete = (record: McpKeyItem) => {
    Modal.confirm({
      title: t('mcpKeyManagement.deleteTitle'),
      content: t('mcpKeyManagement.deleteConfirm', { name: record.name || record.key_prefix }),
      okText: t('mcpKeyManagement.deleteBtn'),
      cancelText: t('common.cancel'),
      okButtonProps: { danger: true },
      onOk: async () => {
        try {
          await deleteMcpKey(record.id);
          message.success(t('mcpKeyManagement.deleteSuccess'));
          await loadKeys();
        } catch (err: unknown) {
          message.error(err instanceof Error ? err.message : t('mcpKeyManagement.operationFailed'));
        }
      },
    });
  };

  const confirmDisable = (record: McpKeyItem) => {
    Modal.confirm({
      title: t('mcpKeyManagement.disableTitle'),
      content: t('mcpKeyManagement.disableConfirm', { name: record.name || record.key_prefix }),
      okText: t('mcpKeyManagement.disableBtn'),
      cancelText: t('common.cancel'),
      okButtonProps: { danger: true },
      onOk: async () => {
        try {
          await revokeMcpKey(record.id);
          message.success(t('mcpKeyManagement.disableSuccess'));
          await loadKeys();
        } catch (err: unknown) {
          message.error(err instanceof Error ? err.message : t('mcpKeyManagement.operationFailed'));
        }
      },
    });
  };

  /** 权限 Tag 渲染（多分支用 switch 提升可读性） */
  const renderPermissionTag = (p: string) => {
    switch (p) {
      case 'view':
        return <Tag color="gold">{t('mcpKeyManagement.permissionView')}</Tag>;
      case 'call':
        return <Tag color="blue">{t('mcpKeyManagement.permissionCall')}</Tag>;
      default:
        return <Tag color="green">{t('mcpKeyManagement.permissionWrite')}</Tag>;
    }
  };

  const columns: ColumnsType<McpKeyItem> = [
    {
      title: t('mcpKeyManagement.nameLabel'),
      dataIndex: 'name',
      key: 'name',
      width: 140,
      render: (v: string) => v || <span style={{ color: '#94a3b8' }}>-</span>,
    },
    {
      title: t('mcpKeyManagement.keyPrefix'),
      dataIndex: 'key_prefix',
      key: 'key_prefix',
      width: 120,
      render: (v: string) => <Typography.Text code>yxm_{v}</Typography.Text>,
    },
    {
      title: t('mcpKeyManagement.permissions'),
      dataIndex: 'permissions',
      key: 'permissions',
      width: 120,
      render: (_: unknown, record: McpKeyItem) => (
        <span>
          {(record.permissions || []).map((p) => (
            <span key={p}>{renderPermissionTag(p)}</span>
          ))}
        </span>
      ),
    },
    {
      title: t('mcpKeyManagement.allowedKb'),
      dataIndex: 'allowed_kb_ids',
      key: 'allowed_kb_ids',
      width: 160,
      render: (_: unknown, record: McpKeyItem) => {
        const allowed = record.allowed_kb_ids || [];
        if (allowed.length === 0) return <Tag>{t('mcpKeyManagement.allKb')}</Tag>;
        return (
          <span>
            {allowed.slice(0, 3).map((id) => (
              <Tag key={id} style={{ marginBottom: 2 }}>
                {kbNameMap.get(id) || id}
              </Tag>
            ))}
            {allowed.length > 3 && <Tag style={{ marginBottom: 2 }}>+{allowed.length - 3}</Tag>}
          </span>
        );
      },
    },
    {
      title: t('mcpKeyManagement.serviceScopeLabel'),
      dataIndex: 'service_ids',
      key: 'service_ids',
      width: 160,
      render: (_: unknown, record: McpKeyItem) => {
        const ids = record.service_ids || [];
        if (ids.length === 0) return <Tag>{t('mcpKeyManagement.allServices')}</Tag>;
        return (
          <span>
            {ids.slice(0, 2).map((id) => (
              <Tag key={id} style={{ marginBottom: 2 }}>
                {serviceNameMap.get(id) || id}
              </Tag>
            ))}
            {ids.length > 2 && <Tag style={{ marginBottom: 2 }}>+{ids.length - 2}</Tag>}
          </span>
        );
      },
    },
    {
      title: t('mcpKeyManagement.createdAt'),
      dataIndex: 'created_at',
      key: 'created_at',
      width: 110,
      render: (v: string | null) => defaultFormatDate(v),
    },
    {
      title: t('mcpKeyManagement.status'),
      dataIndex: 'revoked_at',
      key: 'status',
      width: 90,
      render: (_: unknown, record: McpKeyItem) =>
        record.revoked_at ? (
          <Tag color="error">{t('mcpKeyManagement.revokedStatus')}</Tag>
        ) : (
          <Tag color="success">{t('mcpKeyManagement.activeStatus')}</Tag>
        ),
    },
    {
      title: t('mcpKeyManagement.actions'),
      key: 'actions',
      width: 180,
      render: (_: unknown, record: McpKeyItem) => (
        <Space size={0}>
          <Button type="link" size="small" onClick={() => detailDrawerRef.current?.open(record)}>
            {t('mcpKeyManagement.detailBtn')}
          </Button>
          <Button type="link" size="small" onClick={() => modalsRef.current?.openEdit(record)}>
            {t('mcpKeyManagement.editBtn')}
          </Button>
          <Button type="link" size="small" danger onClick={() => confirmDelete(record)}>
            {t('mcpKeyManagement.deleteBtn')}
          </Button>
          {record.revoked_at ? (
            <Button type="link" size="small" onClick={() => modalsRef.current?.openEnable(record)}>
              {t('mcpKeyManagement.enableBtn')}
            </Button>
          ) : (
            <Button type="link" size="small" danger onClick={() => confirmDisable(record)}>
              {t('mcpKeyManagement.disableBtn')}
            </Button>
          )}
        </Space>
      ),
    },
  ];

  // ── 状态覆盖 ──
  if (!loaded) {
    return (
      <div style={{ display: 'flex', justifyContent: 'center', alignItems: 'center', minHeight: 200 }}>
        <Spin size="large" />
      </div>
    );
  }

  if (error) {
    return (
      <Result
        status="error"
        title={t('mcpKeyManagement.loadFailed')}
        subTitle={error}
        extra={
          <Button type="primary" icon={<ReloadOutlined />} onClick={loadKeys}>
            {t('common.retry')}
          </Button>
        }
      />
    );
  }

  return (
    <div>
      {/* 工具栏：搜索 + 刷新 / 创建 */}
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 12 }}>
        <Input
          allowClear
          prefix={<SearchOutlined style={{ color: '#94a3b8' }} />}
          placeholder={t('mcpKeyManagement.searchPlaceholder')}
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          style={{ width: 220 }}
        />
        <Space>
          <Button icon={<ReloadOutlined />} onClick={loadKeys}>
            {t('common.refresh')}
          </Button>
          <Button type="primary" icon={<PlusOutlined />} onClick={() => modalsRef.current?.openCreate()}>
            {t('mcpKeyManagement.createBtn')}
          </Button>
        </Space>
      </div>

      {keys.length === 0 ? (
        <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={t('mcpKeyManagement.emptyText')}>
          <Button type="primary" icon={<PlusOutlined />} onClick={() => modalsRef.current?.openCreate()}>
            {t('mcpKeyManagement.createFirst')}
          </Button>
        </Empty>
      ) : filtered.length === 0 ? (
        <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={t('mcpKeyManagement.emptyFilteredText')} />
      ) : (
        <Table<McpKeyItem>
          columns={columns}
          dataSource={filtered}
          rowKey="id"
          pagination={false}
          size="middle"
          loading={loading}
          bordered
        />
      )}

      {/* 创建 / 编辑 / 启用 弹窗 */}
      <McpKeyModals
        ref={modalsRef}
        onSuccess={loadKeys}
        knowledgeBaseOptions={kbOptions}
        serviceOptions={serviceOptions}
      />

      {/* WorkBuddy 连接配置引导（仅当存在 Key 时展示） */}
      {hasKeys && (
        <div className="mcp-key-workbuddy-guide">
          <Card title={t('mcpKeyManagement.workbuddyGuideTitle')}>
            <Typography.Paragraph type="secondary">
              {t('mcpKeyManagement.workbuddyGuideDesc')}
            </Typography.Paragraph>
            <Typography.Paragraph
              copyable={{
                text: workbuddyJsonConfig,
                tooltips: [t('mcpKeyManagement.workbuddyGuideCopyConfig'), t('mcpKeyManagement.workbuddyGuideCopied')],
              }}
            >
              {t('mcpKeyManagement.workbuddyGuideConfigLabel')}
            </Typography.Paragraph>
            <pre className="mcp-key-workbuddy-json">
              <Typography.Text
                code
                copyable={{
                  text: workbuddyJsonConfig,
                  tooltips: [t('mcpKeyManagement.workbuddyGuideCopyConfig'), t('mcpKeyManagement.workbuddyGuideCopied')],
                }}
              >
                {workbuddyJsonConfig}
              </Typography.Text>
            </pre>
            <Alert type="info" showIcon message={t('mcpKeyManagement.workbuddyGuideNote')} />
          </Card>
        </div>
      )}

      {/* 详情抽屉 */}
      <KeyDetailDrawer ref={detailDrawerRef} />
    </div>
  );
}
