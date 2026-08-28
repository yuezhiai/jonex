import { useState, useEffect } from 'react';
import { useTranslation } from 'react-i18next';
import { Card, Typography, Button, Table, Divider, Modal, message, Flex, Input, Tag } from 'antd';
import { SaveOutlined } from '@ant-design/icons';
import type { ColumnsType } from 'antd/es/table';
import { request, getData } from '../../api/request';
import type { DomainServiceItem } from '../../types/domainService';

const { Title, Text } = Typography;

/** 授权 Key 条目（来自 GET /platform/mcp-services/{id}/authorized-keys） */
interface AuthKeyItem {
  key_id: string;
  key_name: string;
  key_prefix: string;
  permission_level: string;
  key_status: string;
  /** 有效期（null = 永久有效） */
  expires_at?: string | null;
  /** 创建时间 */
  created_at?: string | null;
}

/** 格式化日期：YYYY-MM-DD */
const formatDate = (value?: string | null): string => {
  if (!value) return '-';
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return '-';
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
};

/** MCP 服务详情（来自 GET /platform/mcp-services/{id}） */
interface McpServiceDetail {
  is_published?: boolean;
  /** 已保存的 Tool 名称（未配置过为 null，回填表单用） */
  tool?: string | null;
  /** 已保存的 Tool 描述 */
  tool_description?: string | null;
}

interface McpKeyTabProps {
  /** 当前领域服务 */
  service: DomainServiceItem | null;
  /** Tab 是否激活 */
  visible: boolean;
}

/** 区块标题：左侧蓝色竖线 + 深灰加粗文字 */
const SectionTitle: React.FC<{ children: React.ReactNode }> = ({ children }) => (
  <Flex align="center" gap={8} style={{ marginBottom: 12 }}>
    <span style={{ width: 3, height: 16, background: '#1C6FEA', borderRadius: 2, flexShrink: 0 }} />
    <Text strong style={{ fontSize: 15, color: '#334155' }}>
      {children}
    </Text>
  </Flex>
);

export default function McpKeyTab({ service, visible }: McpKeyTabProps) {
  const { t } = useTranslation();
  const [toolName, setToolName] = useState('');
  const [toolDesc, setToolDesc] = useState('');
  const [published, setPublished] = useState<boolean>(false);
  const [authKeys, setAuthKeys] = useState<AuthKeyItem[]>([]);
  const [saving, setSaving] = useState(false);
  /** 最后一次落库/回读的 Tool 名称：已发布改名二次确认的比对基准 */
  const [savedToolName, setSavedToolName] = useState('');

  useEffect(() => {
    if (!visible || !service) return;
    // 切服务/重新打开先清空 Tool 配置，避免详情回读前残留上一个服务的值
    // （Tool 名是 MCP 工具标识符，与领域服务名无关，不预填服务名）
    setToolName('');
    setToolDesc('');
    setPublished(false);
    setAuthKeys([]);
    setSavedToolName('');
    // 访问授权列表
    getData<{ items: AuthKeyItem[] }>(
      request.get(`/platform/mcp-services/${service.id}/authorized-keys`),
    )
      .then((data) => setAuthKeys(data?.items ?? []))
      .catch(() => setAuthKeys([]));
    // MCP 服务详情：发布状态 + 已保存的 Tool 配置（有值则回填表单，并记录比对基准）
    getData<McpServiceDetail>(request.get(`/platform/mcp-services/${service.id}`))
      .then((data) => {
        setPublished(data?.is_published === true);
        setToolName(data?.tool ?? '');
        setToolDesc(data?.tool_description ?? '');
        setSavedToolName(data?.tool || '');
      })
      .catch(() => {});
  }, [visible, service]);

  /** 提交保存请求（已通过校验与二次确认） */
  const doSaveTool = async (name: string) => {
    if (!service) return;
    setSaving(true);
    try {
      await getData(
        request.put(`/platform/mcp-services/${service.id}/tool`, {
          tool: name,
          tool_description: toolDesc.trim() || undefined,
        }),
      );
      setSavedToolName(name);
      message.success(t('domainManagement.mcpToolSaved'));
    } catch (err: unknown) {
      // apiClient 已透传后端 message（如「Tool 名称已存在」），直接展示
      message.error(err instanceof Error ? err.message : t('common.saveFailed'));
    } finally {
      setSaving(false);
    }
  };

  /** 校验 Tool 名称：合法返回 null，否则返回错误提示的 i18n key */
  const validateToolName = (name: string): string | null => {
    if (!name) return 'domainManagement.mcpToolNameRequired';
    if (!/^[A-Za-z0-9_]+$/.test(name)) return 'domainManagement.mcpToolNameFormat';
    if (name.length > 128) return 'domainManagement.mcpToolNameMaxLength';
    return null;
  };

  /** 保存 Tool 配置：已发布且修改 Tool 名称时二次确认（未发布或未改名直接保存） */
  const handleSaveTool = async () => {
    if (!service) return;
    const name = toolName.trim();
    const invalidKey = validateToolName(name);
    if (invalidKey) {
      message.warning(t(invalidKey));
      return;
    }
    if (toolDesc.trim().length > 255) {
      message.warning(t('domainManagement.mcpToolDescMaxLength'));
      return;
    }

    if (published && savedToolName && savedToolName !== name) {
      Modal.confirm({
        title: t('domainManagement.mcpToolNameChangeTitle'),
        content: t('domainManagement.mcpToolNameChangeConfirm', {
          oldName: savedToolName,
          newName: name,
        }),
        okText: t('common.confirm'),
        cancelText: t('common.cancel'),
        onOk: () => doSaveTool(name),
      });
      return;
    }
    await doSaveTool(name);
  };

  /** 发布 MCP 能力：Tool 名称须合法且已保存，未保存不允许发布 */
  const handlePublish = async () => {
    if (!service) return;
    const name = toolName.trim();
    const invalidKey = validateToolName(name);
    if (invalidKey) {
      message.warning(t(invalidKey));
      return;
    }
    if (name !== savedToolName) {
      message.warning(t('domainManagement.mcpToolNameNotSaved'));
      return;
    }
    try {
      await getData(request.post(`/platform/mcp-services/${service.id}/publish`));
      setPublished(true);
      message.success(t('domainManagement.mcpPublished'));
    } catch (err: unknown) {
      message.error(err instanceof Error ? err.message : t('common.saveFailed'));
    }
  };

  const handleUnpublish = () => {
    Modal.confirm({
      title: t('domainManagement.mcpUnpublish'),
      content: t('domainManagement.mcpUnpublishConfirm'),
      okText: t('common.confirm'),
      cancelText: t('common.cancel'),
      okType: 'danger',
      onOk: async () => {
        if (!service) return;
        try {
          await getData(request.post(`/platform/mcp-services/${service.id}/unpublish`));
          setPublished(false);
          message.success(t('domainManagement.mcpUnpublishedMsg'));
        } catch (err: unknown) {
          message.error(err instanceof Error ? err.message : t('common.saveFailed'));
        }
      },
    });
  };

  const handleGoToKeyManagement = () => {
    // 跳转到生态管理 MCP 服务目录，并定位到「服务访问 Key」视图（area=domain + view=access）
    const target = window.top?.location ?? window.location;
    target.href = '/apps/ecosystem-management/mcp-service-directory?area=domain&view=access';
  };

  const columns: ColumnsType<AuthKeyItem> = [
    {
      title: t('domainManagement.apiKey'),
      dataIndex: 'key_name',
      key: 'key_name',
      width: 180,
      render: (v: string) => v || '-',
    },
    {
      title: t('domainManagement.mcpAuthKeyPrefix'),
      dataIndex: 'key_prefix',
      key: 'key_prefix',
      width: 160,
      render: (v: string) => (v ? <Text code>yxm_{v}</Text> : '-'),
    },
    {
      title: t('domainManagement.mcpAuthPermission'),
      dataIndex: 'permission_level',
      key: 'permission_level',
      width: 100,
      render: (v: string) =>
        v === 'view' ? (
          <Tag color="gold">{t('domainManagement.mcpPermissionView')}</Tag>
        ) : (
          <Tag color="blue">{t('domainManagement.mcpPermissionCall')}</Tag>
        ),
    },
    {
      title: t('domainManagement.mcpAuthStatus'),
      dataIndex: 'key_status',
      key: 'key_status',
      width: 100,
      render: (v: string) =>
        v === 'active' ? (
          <Tag color="success">{t('domainManagement.mcpKeyStatusActive')}</Tag>
        ) : (
          <Tag color="error">{t('domainManagement.mcpKeyStatusRevoked')}</Tag>
        ),
    },
    {
      title: t('domainManagement.mcpAuthExpiry'),
      key: 'expiry',
      width: 120,
      render: (_: unknown, item: AuthKeyItem) =>
        item.expires_at ? formatDate(item.expires_at) : t('domainManagement.mcpKeyPermanent'),
    },
    {
      title: t('domainManagement.createdAt'),
      dataIndex: 'created_at',
      key: 'created_at',
      width: 120,
      render: (v: string | null) => formatDate(v),
    },
    {
      title: t('domainManagement.mcpAuthAction'),
      key: 'actions',
      width: 100,
      render: () => (
        <Typography.Link onClick={handleGoToKeyManagement}>
          {t('domainManagement.mcpAuthManage')}
        </Typography.Link>
      ),
    },
  ];

  const kbNames = service?.kb_names && service.kb_names.length > 0 ? service.kb_names.join('、') : '-';

  return (
    <Card bordered={false} style={{ borderRadius: 12 }}>
      {/* ===== 工具信息展示区 ===== */}
      <SectionTitle>{t('domainManagement.mcpAbilityTitle')}</SectionTitle>

      <div style={{ marginBottom: 14 }}>
        <Text type="secondary" style={{ display: 'block', marginBottom: 6 }}>
          {t('domainManagement.mcpToolName')}
        </Text>
        <Input
          value={toolName}
          onChange={(e) => setToolName(e.target.value)}
          maxLength={128}
          placeholder={t('domainManagement.mcpToolNameFormat')}
        />
      </div>
      <div style={{ marginBottom: 14 }}>
        <Text type="secondary" style={{ display: 'block', marginBottom: 6 }}>
          {t('domainManagement.mcpToolDesc')}
        </Text>
        <Input.TextArea
          value={toolDesc}
          onChange={(e) => setToolDesc(e.target.value)}
          autoSize={{ minRows: 2, maxRows: 4 }}
        />
      </div>

      {/* 关联领域服务 / 关联知识库：左右两栏 */}
      <Flex gap={24} style={{ marginBottom: 14 }}>
        <div style={{ flex: 1 }}>
          <Text type="secondary" style={{ display: 'block', marginBottom: 6 }}>
            {t('domainManagement.mcpRelatedService')}
          </Text>
          <Text>{service?.name || '-'}</Text>
        </div>
        <div style={{ flex: 1 }}>
          <Text type="secondary" style={{ display: 'block', marginBottom: 6 }}>
            {t('domainManagement.mcpRelatedKb')}
          </Text>
          <Text>{kbNames}</Text>
        </div>
      </Flex>

      {/* 保存 Tool 配置按钮 */}
      <Flex justify="flex-start">
        <Button type="primary" icon={<SaveOutlined />} loading={saving} onClick={handleSaveTool}>
          {t('domainManagement.saveToolConfig')}
        </Button>
      </Flex>

      <Divider style={{ margin: '24px 0' }} />

      {/* ===== MCP 发布状态 ===== */}
      <SectionTitle>{t('domainManagement.mcpPublishStatus')}</SectionTitle>
      <Flex justify="space-between" align="center" wrap="wrap" gap={12}>
        {/* 左侧：状态 + 描述 */}
        <Flex align="center" gap={8}>
          <span
            style={{
              width: 8,
              height: 8,
              borderRadius: '50%',
              background: published ? '#10b981' : '#d1d5db',
            }}
          />
          <Text strong style={{ color: published ? '#10b981' : '#64748b' }}>
            {published ? t('domainManagement.mcpPublished') : t('domainManagement.mcpUnpublished')}
          </Text>
          <Text type="secondary">{t('domainManagement.mcpPublishHint')}</Text>
        </Flex>
        {/* 右侧：发布 / 取消发布 按钮 */}
        {published ? (
          <Button type="primary" danger onClick={handleUnpublish}>
            {t('domainManagement.mcpUnpublish')}
          </Button>
        ) : (
          <Button type="primary" onClick={handlePublish}>
            {t('domainManagement.mcpPublishBtn')}
          </Button>
        )}
      </Flex>

      <Divider style={{ margin: '24px 0' }} />

      {/* ===== 访问授权 ===== */}
      <Flex justify="space-between" align="center" style={{ marginBottom: 12 }}>
        <SectionTitle>{t('domainManagement.mcpAccessAuth')}</SectionTitle>
        <Button type="primary" onClick={handleGoToKeyManagement}>
          {t('domainManagement.goToKeyManagement')}
        </Button>
      </Flex>
      <Text type="secondary" style={{ display: 'block', marginBottom: 16 }}>
        {t('domainManagement.mcpAccessAuthDesc')}
      </Text>

      <Table
        columns={columns}
        dataSource={authKeys}
        rowKey="key_id"
        pagination={false}
        bordered
        size="middle"
        locale={{ emptyText: t('common.noData') }}
      />
    </Card>
  );
}
