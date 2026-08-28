import { forwardRef, useImperativeHandle, useState, useEffect, useCallback } from 'react';
import {
  Drawer,
  Form,
  Button,
  Space,
  Input,
  Select,
  Table,
  Checkbox,
  Typography,
  Alert,
  Result,
  Steps,
  Radio,
  DatePicker,
  message,
} from 'antd';
import { CopyOutlined } from '@ant-design/icons';
import { useTranslation } from 'react-i18next';
import copy from 'copy-to-clipboard';
import dayjs, { type Dayjs } from 'dayjs';
import type { ColumnsType } from 'antd/es/table';
import { createMcpKey, updateMcpKey, type McpKeyCreateResult, type McpKeyItem } from '@/api/mcpKeys';
import { listMcpServices } from '@/api/mcpServices';
import type { McpServiceItem } from '@/api/mcpServices';
import { listKnowledgeBases, listSpaces, type KnowledgeBaseBrief } from '@/api/spaces';
import { readPersistedSpaceId, onSpaceChanged } from '@jonex/shell-sdk';
import type { WriteGrant } from '@/api/mcpKeys';
import WriteGrantsEditor from './WriteGrantsEditor';
import './index.css';

/** 创建/编辑抽屉对外暴露的句柄 */
export type McpKeyDrawerHandle = {
  /** 创建模式打开 */
  open: () => void;
  /** 编辑模式打开（预填数据，领域空间只读） */
  openEdit: (record: McpKeyItem) => void;
};

interface McpKeyDrawerProps {
  /** 创建成功后的回调（如刷新列表） */
  onSuccess?: () => void;
}

/** 服务级权限选项（Phase 16 收敛为 call/view） */
const SERVICE_PERMISSIONS = ['view', 'call'] as const;
type ServiceLevel = (typeof SERVICE_PERMISSIONS)[number];

/** 有效期固定档位（月数 + i18n label key） */
const EXPIRY_OPTIONS = [
  { value: '3m', labelKey: 'expiry3m', months: 3 },
  { value: '6m', labelKey: 'expiry6m', months: 6 },
  { value: '12m', labelKey: 'expiry12m', months: 12 },
];

/** 计算有效期截止日期（ISO datetime；永久 → null） */
const calcExpiry = (type: string, customDate?: Dayjs): string | null => {
  if (type === 'forever') return null;
  if (type === 'custom') return customDate ? customDate.endOf('day').toISOString() : null;
  const opt = EXPIRY_OPTIONS.find((o) => o.value === type);
  return opt ? dayjs().add(opt.months, 'month').endOf('day').toISOString() : null;
};

/** 有效期截止日期展示（YYYY-MM-DD；永久 → null） */
const calcExpiryDate = (type: string, customDate?: Dayjs): string | null => {
  if (type === 'forever') return null;
  if (type === 'custom') return customDate ? customDate.format('YYYY-MM-DD') : null;
  const opt = EXPIRY_OPTIONS.find((o) => o.value === type);
  return opt ? dayjs().add(opt.months, 'month').format('YYYY-MM-DD') : null;
};

/** 生成幂等键：优先 crypto.randomUUID；非安全上下文（如内网 http 部署）降级 */
const genClientRequestId = () => {
  try {
    return crypto.randomUUID();
  } catch {
    return `kb-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
  }
};

interface FormValues {
  name?: string;
  note?: string;
  expiry_type?: string;
  expiry_custom?: Dayjs;
}

/** 创建/编辑 MCP Key — 三步向导：①基础信息 → ②授权领域服务 → ③创建成功 */
export const McpKeyDrawer = forwardRef<McpKeyDrawerHandle, McpKeyDrawerProps>(
  ({ onSuccess }, ref) => {
    const { t } = useTranslation();
    const [open, setOpen] = useState(false);
    const [step, setStep] = useState(1);
    const [saving, setSaving] = useState(false);
    // 幂等键：每次打开抽屉生成一次，重复提交复用同一 UUID，后端按此去重
    const [clientRequestId, setClientRequestId] = useState<string | null>(null);
    const [result, setResult] = useState<McpKeyCreateResult | null>(null);
    const [form] = Form.useForm<FormValues>();
    // 第 1 步填写的表单值（进入第 2 步时保存，避免 Form 卸载后取值丢失）
    const [basicInfo, setBasicInfo] = useState<FormValues | null>(null);
    // 编辑模式：非空表示编辑指定 Key（复用创建向导，领域空间只读）
    const [editRecord, setEditRecord] = useState<McpKeyItem | null>(null);
    // 当前领域空间：创建态 = Shell 全局当前空间（左上角切换器）；编辑态 = 原 Key 的空间（锁定，不跟随切换）
    const [currentSpaceId, setCurrentSpaceId] = useState<string | null>(null);
    // 领域服务列表（第 2 步授权用）
    const [services, setServices] = useState<McpServiceItem[]>([]);
    // 已选服务授权：service_id → permission_level（call/view）
    const [selected, setSelected] = useState<Record<string, ServiceLevel>>({});
    // 知识写入授权：知识库列表 + 领域空间名称映射（WriteGrantsEditor 数据源）
    const [kbList, setKbList] = useState<KnowledgeBaseBrief[]>([]);
    const [spaceNameMap, setSpaceNameMap] = useState<Record<string, string>>({});
    // 知识写入授权开关 + 授权范围（grants[]）
    const [writeEnabled, setWriteEnabled] = useState(false);
    const [writeGrants, setWriteGrants] = useState<WriteGrant[]>([]);

    // 知识库列表 + 空间名称映射（一次性加载，数据量小）
    useEffect(() => {
      listKnowledgeBases()
        .then(setKbList)
        .catch(() => setKbList([]));
      listSpaces()
        .then((items) => {
          const m: Record<string, string> = {};
          items.forEach((s) => {
            m[s.id] = s.name ?? s.id;
          });
          setSpaceNameMap(m);
        })
        .catch(() => {});
    }, []);

    /** 加载领域服务列表（按所选空间过滤，确保勾选的服务均属于该空间） */
    const loadServices = useCallback(async (spaceId?: string) => {
      if (!spaceId) {
        setServices([]);
        return;
      }
      try {
        const res = await listMcpServices({ space_id: spaceId });
        setServices(res.items || []);
      } catch {
        setServices([]);
      }
    }, []);

    // 订阅全局「当前空间」切换；编辑态锁定原 Key 空间，不跟随切换。
    // 创建态切换空间时清空已选服务（跨空间服务不可用）
    useEffect(() => {
      return onSpaceChanged((spaceId) => {
        if (editRecord) return;
        setCurrentSpaceId(spaceId);
        setSelected({});
      });
    }, [editRecord]);

    // 当前空间 → 加载该空间下的领域服务列表（唯一入口，open/openEdit 只设 currentSpaceId）
    useEffect(() => {
      loadServices(currentSpaceId || undefined);
    }, [currentSpaceId, loadServices]);

    const expiryType = Form.useWatch('expiry_type', form);
    const customDate = Form.useWatch('expiry_custom', form);
    const expiryTo = calcExpiryDate(expiryType || 'forever', customDate);

    useImperativeHandle(
      ref,
      () => ({
        open: () => {
          // 空空间边界：未选择领域空间时拦截，不发请求、不打开抽屉
          const sid = readPersistedSpaceId();
          if (!sid) {
            message.warning(t('mcpKeyManagement.noSpaceSelected'));
            return;
          }
          form.resetFields();
          setEditRecord(null);
          setStep(1);
          setResult(null);
          setBasicInfo(null);
          setSelected({});
          // 幂等键：本次打开生成，重复提交复用
          setClientRequestId(genClientRequestId());
          // 创建模式：知识写入授权重置为关闭
          setWriteEnabled(false);
          setWriteGrants([]);
          // 锁定当前全局空间（创建态）
          setCurrentSpaceId(sid);
          setOpen(true);
        },
        openEdit: (record: McpKeyItem) => {
          setEditRecord(record);
          form.setFieldsValue({
            name: record.name || '',
            note: record.note || '',
            expiry_type: record.expires_at ? 'custom' : 'forever',
            expiry_custom: record.expires_at ? dayjs(record.expires_at) : undefined,
          });
          // 预选已授权服务（含服务级权限 call/view）
          const sel: Record<string, ServiceLevel> = {};
          (record.service_permissions || []).forEach((sp) => {
            if (sp.permission_level === 'view' || sp.permission_level === 'call') {
              sel[sp.service_id] = sp.permission_level;
            }
          });
          setSelected(sel);
          // 知识写入授权回填（开关 + 授权范围）
          setWriteEnabled(!!(record.write_grants || []).length);
          setWriteGrants(record.write_grants || []);
          // 编辑态锁定原 Key 空间（服务列表按此加载，不跟随全局切换）
          setCurrentSpaceId(record.space_id || null);
          // M5：历史 Key 未关联领域空间时提示（服务授权仅原样保留，表格不展示）
          if (!record.space_id) {
            message.warning(t('mcpKeyManagement.historicalKeyNoSpace'));
          }
          // 幂等键：本次打开生成，重复提交复用
          setClientRequestId(genClientRequestId());
          setStep(1);
          setResult(null);
          setBasicInfo(null);
          setOpen(true);
        },
      }),
      [form, loadServices],
    );

    /** 服务级权限 label */
    const permissionLabel = (p: string): string => {
      switch (p) {
        case 'view':
          return t('mcpKeyManagement.permissionView');
        case 'call':
          return t('mcpKeyManagement.permissionCall');
        default:
          return p;
      }
    };

    /** 第 1 步 → 第 2 步 */
    const nextStep = async () => {
      try {
        const values = await form.validateFields();
        setBasicInfo(values);
        setStep(2);
      } catch {
        // 校验失败，antd 已展示错误
      }
    };

    /** 第 2 步创建/编辑提交 */
    const handleCreate = async () => {
      const values = basicInfo || form.getFieldsValue();
      const servicePermissions = Object.entries(selected).map(([service_id, permission_level]) => ({
        service_id,
        permission_level,
      }));
      // 知识写入：开关开启且至少一项授权才提交，否则显式置空数组
      const effectiveGrants = writeEnabled ? writeGrants.filter((g) => g.kb) : [];
      // 至少一项授权：领域服务授权 或 有效写入授权，二者至少要有其一
      // （校验实际生效的 grants 而非开关：开关开着但没选任何知识库也算零授权）
      if (servicePermissions.length === 0 && effectiveGrants.length === 0) {
        message.error(t('mcpKeyManagement.atLeastOneAuth'));
        return;
      }
      // 提交前轻量校验（与 WriteGrantsEditor 约束一致）：specified 必选目录、all 目录必须为空，
      // 避免提交后才被后端 400 拒绝而只弹通用「操作失败」
      if (effectiveGrants.some((g) => g.mode === 'specified' && !(g.directories || []).length)) {
        message.error(t('mcpKeyManagement.directoriesRequired'));
        return;
      }
      if (effectiveGrants.some((g) => g.mode === 'all' && (g.directories || []).length > 0)) {
        message.error(t('mcpKeyManagement.allNoDirectories'));
        return;
      }
      const common = {
        name: values.name?.trim() || undefined,
        note: values.note?.trim() || null,
        expires_at: calcExpiry(values.expiry_type || '12m', values.expiry_custom),
        // 空数组会触发后端「写入已开启但未指定知识库」报错；未开启写入必须传 null
        write_grants: effectiveGrants.length ? effectiveGrants : null,
      };
      setSaving(true);
      try {
        if (editRecord) {
          // 编辑：提交授权服务 + 写入授权（write_grants 传 null = 清除写入授权）
          const payload = {
            ...common,
            service_permissions: servicePermissions,
          };
          await updateMcpKey(editRecord.id, payload);
          message.success(t('mcpKeyManagement.editSuccess'));
          close(true);
        } else {
          // M1：抽屉打开期间全局空间被清空时兜底拦截（open() 已拦打开前的空空间）
          if (!currentSpaceId) {
            message.warning(t('mcpKeyManagement.noSpaceSelected'));
            return;
          }
          const res = await createMcpKey({
            ...common,
            // 锁定当前全局空间（创建态必非空：上方已校验）
            space_id: currentSpaceId,
            // 幂等键：本次打开生成的 UUID，重复提交由后端按 client_request_id 去重
            client_request_id: clientRequestId ?? genClientRequestId(),
            service_permissions: servicePermissions,
          });
          setResult(res);
          setStep(3);
        }
      } catch (err: any) {
        message.error(err?.message || t('mcpKeyManagement.operationFailed'));
      } finally {
        setSaving(false);
      }
    };

    /** 复制文本（降级逻辑见 @jonex/shared-lib copyToClipboard） */
    const copyText = async (text: string) => {
      const ok = await copy(text);
      if (ok) message.success(t('common.copySuccess'));
      else message.error(t('mcpKeyManagement.operationFailed'));
    };

    const close = (refresh = false) => {
      setOpen(false);
      setResult(null);
      setEditRecord(null);
      setClientRequestId(null);
      if (refresh) onSuccess?.();
    };

    // WorkBuddy 连接配置 JSON（第 3 步展示）—— 直接使用后端下发的 mcp_config，URL 来源交给后端配置
    const workbuddyConfig = result?.mcp_config ? JSON.stringify(result.mcp_config, null, 2) : '';

    // 判断后端下发的配置是否含非空 url（不依赖 key 名，取第一个 server 的 url）
    const mcpConfigUrl = (() => {
      const cfg = result?.mcp_config;
      if (!cfg || typeof cfg !== 'object') return '';
      const servers = (cfg as Record<string, unknown>).mcpServers;
      if (!servers || typeof servers !== 'object') return '';
      const first = Object.values(servers as Record<string, unknown>)[0];
      if (!first || typeof first !== 'object') return '';
      const url = (first as Record<string, unknown>).url;
      return typeof url === 'string' ? url : '';
    })();

    // 第 2 步：授权领域服务表格列（勾选 + 服务级权限 call/view）
    const serviceColumns: ColumnsType<McpServiceItem> = [
      {
        title: t('mcpKeyManagement.selectColumn'),
        key: 'select',
        width: 60,
        render: (_: unknown, record: McpServiceItem) => (
          <Checkbox
            checked={!!selected[record.id]}
            disabled={!record.is_published}
            onChange={(e) => {
              const next = { ...selected };
              if (e.target.checked) next[record.id] = 'view';
              else delete next[record.id];
              setSelected(next);
            }}
          />
        ),
      },
      {
        title: t('mcpKeyManagement.serviceNameColumn'),
        dataIndex: 'name',
        key: 'name',
        render: (_: unknown, record: McpServiceItem) => (
          <div>
            <div>{record.name}</div>
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              {record.space_name || '-'}·MCP{record.is_published ? t('mcpKeyManagement.publishedStatus') : t('mcpKeyManagement.unpublishedStatus')}
            </Typography.Text>
          </div>
        ),
      },
      {
        title: t('mcpKeyManagement.mcpToolColumn'),
        dataIndex: 'tool',
        key: 'tool',
        render: (v: string | null | undefined) => v || '-',
      },
      {
        title: t('mcpKeyManagement.permissionLabel'),
        key: 'permission',
        width: 130,
        render: (_: unknown, record: McpServiceItem) => (
          <Select
            size="small"
            style={{ width: 110 }}
            value={selected[record.id]}
            disabled={!selected[record.id] || !record.is_published}
            onChange={(v) => setSelected((prev) => ({ ...prev, [record.id]: v }))}
            options={SERVICE_PERMISSIONS.map((p) => ({ value: p, label: permissionLabel(p) }))}
          />
        ),
      },
    ];

    // 底部按钮按步骤
    const footer =
      step === 3 ? (
        <div style={{ textAlign: 'right' }}>
          <Button type="primary" onClick={() => close(true)}>
            {t('mcpKeyManagement.done')}
          </Button>
        </div>
      ) : (
        <div style={{ display: 'flex', justifyContent: 'space-between' }}>
          <Button onClick={() => close(false)}>{t('common.cancel')}</Button>
          <Space>
            {step === 2 && (
              <Button onClick={() => setStep(1)}>{t('mcpKeyManagement.prevStep')}</Button>
            )}
            {step === 1 ? (
              <Button type="primary" onClick={nextStep}>
                {t('mcpKeyManagement.nextStep')}
              </Button>
            ) : (
              <Button type="primary" loading={saving} onClick={handleCreate}>
                {editRecord ? t('common.save') : t('mcpKeyManagement.createBtn')}
              </Button>
            )}
          </Space>
        </div>
      );

    // 知识写入授权摘要：与提交过滤一致（剔除未选 KB 的空行），避免成功页展示与真实写入授权不一致
    const effectiveWriteGrants = writeGrants.filter((g) => g.kb);

    return (
      <Drawer
        title={editRecord ? t('mcpKeyManagement.editTitle') : t('mcpKeyManagement.createTitle')}
        placement="right"
        width={680}
        open={open}
        onClose={() => close(step === 3)}
        footer={footer}
        destroyOnHidden
      >
        {/* 步骤指示 */}
        <Steps
          size="small"
          current={step - 1}
          items={
            editRecord
              ? [
                  { title: t('mcpKeyManagement.createStep1') },
                  { title: t('mcpKeyManagement.createStep2') },
                ]
              : [
                  { title: t('mcpKeyManagement.createStep1') },
                  { title: t('mcpKeyManagement.createStep2') },
                  { title: t('mcpKeyManagement.createStep3') },
                ]
          }
          style={
            editRecord
              ? { maxWidth: 380, margin: '0 auto 24px' }
              : { marginBottom: 24 }
          }
        />

        {/* Form 始终挂载（step 2/3 时隐藏），避免卸载导致已填字段值丢失 */}
        <div style={{ display: step === 1 ? undefined : 'none' }}>
          <Form form={form} layout="vertical">
            <Form.Item
              name="name"
              label={t('mcpKeyManagement.keyNameLabel')}
              rules={[{ required: true, whitespace: true, message: t('mcpKeyManagement.keyNameRequired') }]}
            >
              <Input placeholder={t('mcpKeyManagement.keyNamePlaceholder')} maxLength={255} />
            </Form.Item>
            <Form.Item name="note" label={t('mcpKeyManagement.noteLabel')}>
              <Input.TextArea
                placeholder={t('mcpKeyManagement.notePlaceholder')}
                rows={2}
                maxLength={500}
              />
            </Form.Item>
            <Form.Item name="expiry_type" label={t('mcpKeyManagement.expiryLabel')} initialValue="12m">
              <Radio.Group>
                <Space direction="vertical">
                  {EXPIRY_OPTIONS.map((o) => (
                    <Radio key={o.value} value={o.value}>
                      {t(`mcpKeyManagement.${o.labelKey}`)}
                    </Radio>
                  ))}
                  <Radio value="forever">{t('mcpKeyManagement.expiryForever')}</Radio>
                  <Radio value="custom">{t('mcpKeyManagement.expiryCustom')}</Radio>
                </Space>
              </Radio.Group>
            </Form.Item>
            {expiryType === 'custom' && (
              <Form.Item name="expiry_custom" label={t('mcpKeyManagement.expiryLabel')}>
                <DatePicker style={{ width: '100%' }} />
              </Form.Item>
            )}
            {expiryTo && (
              <Typography.Text type="secondary">
                {t('mcpKeyManagement.expiryTo', { date: expiryTo })}
              </Typography.Text>
            )}
          </Form>
        </div>

        {step === 2 && (
          <Table<McpServiceItem>
            rowKey="id"
            columns={serviceColumns}
            dataSource={services}
            size="small"
            pagination={false}
            bordered
            locale={{
              emptyText: <Typography.Text type="secondary">{t('mcpKeyManagement.emptyServices')}</Typography.Text>,
            }}
          />
        )}

        {step === 2 && (
          <div style={{ marginTop: 16 }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 8 }}>
              <Typography.Text strong>{t('mcpKeyManagement.writeAuthLabel')}</Typography.Text>
              <Radio.Group
                value={writeEnabled}
                onChange={(e) => {
                  setWriteEnabled(e.target.value);
                  if (!e.target.value) setWriteGrants([]);
                }}
              >
                <Radio.Button value={false}>{t('mcpKeyManagement.writeAuthOff')}</Radio.Button>
                <Radio.Button value={true}>{t('mcpKeyManagement.writeAuthOn')}</Radio.Button>
              </Radio.Group>
            </div>
            {writeEnabled && (
              <WriteGrantsEditor
                value={writeGrants}
                onChange={setWriteGrants}
                kbList={kbList}
                spaceNameMap={spaceNameMap}
              />
            )}
          </div>
        )}

        {step === 3 && result && (
          <div>
            <Result
              status={result.delivery_failed ? 'warning' : 'success'}
              title={
                result.delivery_failed
                  ? t('mcpKeyManagement.idempotentHitTitle')
                  : t('mcpKeyManagement.createStep3')
              }
            />
            {result.delivery_failed ? (
              <Alert
                type="warning"
                showIcon
                message={t('mcpKeyManagement.idempotentHitWarning')}
                style={{ marginBottom: 16 }}
              />
            ) : (
              <>
                {writeEnabled && effectiveWriteGrants.length > 0 && (
                  <Alert
                    type="info"
                    showIcon
                    message={t('mcpKeyManagement.writeAuthIncluded', { count: effectiveWriteGrants.length })}
                    style={{ marginBottom: 16 }}
                  />
                )}
                <Alert
                  type="warning"
                  showIcon
                  message={t('mcpKeyManagement.keyPlaintextWarning')}
                  style={{ marginBottom: 16 }}
                />
                {/* MCP Key 模块 */}
                <div className="mcp-svc-auth-plaintext">
                  <Typography.Title level={5} style={{ marginTop: 0 }}>
                    {t('mcpKeyManagement.mcpKeyBlock')}
                  </Typography.Title>
                  <Space.Compact style={{ width: '100%' }}>
                    <Input.Password readOnly value={result.plaintext ?? ''} />
                    <Button icon={<CopyOutlined />} onClick={() => copyText(result.plaintext ?? '')}>
                      {t('mcpKeyManagement.copyKey')}
                    </Button>
                  </Space.Compact>
                </div>
                {/* WorkBuddy 配置模块：url 非空才展示配置与复制，缺失则告警 */}
                <div className="mcp-svc-auth-plaintext">
              <Typography.Title level={5} style={{ marginTop: 0 }}>
                {t('mcpKeyManagement.workbuddyBlock')}
              </Typography.Title>
              {!mcpConfigUrl ? (
                <Alert type="warning" showIcon message={t('mcpKeyManagement.mcpConfigMissing')} />
              ) : (
                <>
                  <pre
                    style={{
                      background: '#f8fafc',
                      border: '1px solid #eef2f6',
                      borderRadius: 8,
                      padding: 12,
                      fontSize: 12,
                      maxHeight: 180,
                      overflow: 'auto',
                      marginBottom: 12,
                    }}
                  >
                    {workbuddyConfig}
                  </pre>
                  <Button icon={<CopyOutlined />} onClick={() => copyText(workbuddyConfig)}>
                    {t('mcpKeyManagement.copyConfig')}
                  </Button>
                </>
              )}
                </div>
              </>
            )}
          </div>
        )}
      </Drawer>
    );
  },
);
McpKeyDrawer.displayName = 'McpKeyDrawer';
