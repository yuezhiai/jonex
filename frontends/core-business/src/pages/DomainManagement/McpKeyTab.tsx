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
}

/** MCP 服务发布状态（来自 GET /platform/mcp-services/{id}） */
interface McpPublishState {
  is_published?: boolean;
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

  useEffect(() => {
    if (!visible || !service) return;
    setToolName(service.name || '');
    setToolDesc(service.description || '');
    // 访问授权列表
    getData<{ items: AuthKeyItem[] }>(
      request.get(`/platform/mcp-services/${service.id}/authorized-keys`),
    )
      .then((data) => setAuthKeys(data?.items ?? []))
      .catch(() => setAuthKeys([]));
    // MCP 发布状态
    getData<McpPublishState>(request.get(`/platform/mcp-services/${service.id}`))
      .then((data) => setPublished(data?.is_published === true))
      .catch(() => {});
  }, [visible, service]);

  /** 保存 Tool 配置：暂不加点击事件（仅展示按钮） */

  /** 发布 MCP 能力 */
  const handlePublish = async () => {
    if (!service) return;
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
    // 跳转到生态管理 MCP 服务目录，并定位到 MCP Key Tab
    const target = window.top?.location ?? window.location;
    target.href = '/apps/ecosystem-management/mcp-service-directory?tab=keys';
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
      render: () => '-',
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
        <Input value={toolName} onChange={(e) => setToolName(e.target.value)} />
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

      {/* 保存按钮（暂不加点击事件） */}
      <Flex justify="flex-start">
        <Button type="primary" icon={<SaveOutlined />}>
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
