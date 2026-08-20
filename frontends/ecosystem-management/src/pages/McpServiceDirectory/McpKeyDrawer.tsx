import { forwardRef, useImperativeHandle, useState, useEffect, useRef, useCallback } from 'react';
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
import dayjs, { type Dayjs } from 'dayjs';
import type { ColumnsType } from 'antd/es/table';
import { createMcpKey, updateMcpKey, type McpKeyCreateResult, type McpKeyItem, type McpKeyPermission } from '@/api/mcpKeys';
import { listMcpServices } from '@/api/mcpServices';
import type { McpServiceItem } from '@/api/mcpServices';
import './index.css';

/** 创建/编辑抽屉对外暴露的句柄 */
export type McpKeyDrawerHandle = {
  /** 创建模式打开 */
  open: () => void;
  /** 编辑模式打开（预填数据，领域空间只读） */
  openEdit: (record: McpKeyItem) => void;
};

interface McpKeyDrawerProps {
  /** 领域空间下拉选项（由调用方加载后传入） */
  spaceOptions?: { label: string; value: string }[];
  /** 创建成功后的回调（如刷新列表） */
  onSuccess?: () => void;
}

// MCP Server URL（WorkBuddy 配置展示用）
const mcpServerUrl = 'http://172.30.2.57:8002/mcp';

/** Key 级权限选项（Phase 16 收敛为 call/view） */
const KEY_PERMISSIONS: McpKeyPermission[] = ['view', 'call'];
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

/** 存量 Key 是否含 write/* 遗留权限（需降级角标 / 编辑只读） */
const hasLegacyPermission = (record: McpKeyItem): boolean => {
  if ((record.permissions || []).some((p) => p === 'write' || (p as string) === '*')) return true;
  if ((record.service_permissions || []).some((s) => s.permission_level === 'write' || s.permission_level === '*')) return true;
  return false;
};

interface FormValues {
  space_id?: string;
  name?: string;
  note?: string;
  permissions?: McpKeyPermission[];
  expiry_type?: string;
  expiry_custom?: Dayjs;
}

/** 创建/编辑 MCP Key — 三步向导：①基础信息 → ②授权领域服务 → ③创建成功 */
export const McpKeyDrawer = forwardRef<McpKeyDrawerHandle, McpKeyDrawerProps>(
  ({ spaceOptions, onSuccess }, ref) => {
    const { t } = useTranslation();
    const [open, setOpen] = useState(false);
    const [step, setStep] = useState(1);
    const [saving, setSaving] = useState(false);
    const [result, setResult] = useState<McpKeyCreateResult | null>(null);
    const [form] = Form.useForm<FormValues>();
    // 第 1 步填写的表单值（进入第 2 步时保存，避免 Form 卸载后取值丢失）
    const [basicInfo, setBasicInfo] = useState<FormValues | null>(null);
    // 编辑模式：非空表示编辑指定 Key（复用创建向导，领域空间只读）
    const [editRecord, setEditRecord] = useState<McpKeyItem | null>(null);
    // 领域服务列表（第 2 步授权用）
    const [services, setServices] = useState<McpServiceItem[]>([]);
    // 已选服务授权：service_id → permission_level（call/view）
    const [selected, setSelected] = useState<Record<string, ServiceLevel>>({});
    // 存量 write/* key 编辑时权限只读（仅 note/有效期可改）
    const legacyReadonly = editRecord ? hasLegacyPermission(editRecord) : false;

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

    // 领域空间变更：跨空间服务不可用 → 清空已选服务并重新拉取。
    // 用 ref 追踪上次空间，仅在空间真正变化时清空（避免返回上一步时 Form 重挂载触发误清空）
    const spaceId = Form.useWatch('space_id', form);
    const prevSpaceId = useRef<string | undefined>(undefined);
    useEffect(() => {
      if (prevSpaceId.current === spaceId) return;
      prevSpaceId.current = spaceId;
      setSelected({});
      loadServices(spaceId);
    }, [spaceId, loadServices]);

    const expiryType = Form.useWatch('expiry_type', form);
    const customDate = Form.useWatch('expiry_custom', form);
    const expiryTo = calcExpiryDate(expiryType || 'forever', customDate);

    useImperativeHandle(
      ref,
      () => ({
        open: () => {
          form.resetFields();
          prevSpaceId.current = undefined;
          setEditRecord(null);
          setStep(1);
          setResult(null);
          setBasicInfo(null);
          setSelected({});
          setOpen(true);
        },
        openEdit: (record: McpKeyItem) => {
          setEditRecord(record);
          form.setFieldsValue({
            space_id: record.space_id || undefined,
            name: record.name || '',
            note: record.note || '',
            permissions: (record.permissions || []).filter((p) => p !== 'write' && (p as string) !== '*'),
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
          (record.service_ids || []).forEach((sid) => {
            if (!sel[sid]) sel[sid] = 'view';
          });
          setSelected(sel);
          // 空间只读：按 Key 空间加载服务，prevSpaceId 预置避免 effect 误清空预填值
          prevSpaceId.current = record.space_id || undefined;
          loadServices(record.space_id || undefined);
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
      const common = {
        name: values.name?.trim() || undefined,
        note: values.note?.trim() || null,
        expires_at: calcExpiry(values.expiry_type || '12m', values.expiry_custom),
      };
      setSaving(true);
      try {
        if (editRecord) {
          // 编辑：存量 write/* 时权限只读，仅提交 note/有效期；否则提交权限与授权服务
          const payload = legacyReadonly
            ? common
            : {
                ...common,
                permissions: values.permissions?.length ? values.permissions : (['view'] as McpKeyPermission[]),
                service_permissions: servicePermissions,
              };
          await updateMcpKey(editRecord.id, payload);
          message.success(t('mcpKeyManagement.editSuccess'));
          close(true);
        } else {
          const res = await createMcpKey({
            ...common,
            space_id: values.space_id as string,
            permissions: values.permissions?.length ? values.permissions : (['view'] as McpKeyPermission[]),
            service_permissions: servicePermissions,
          });
          setResult(res);
          setStep(3);
        }
      } catch (e) {
        if (e && typeof e === 'object' && 'errorFields' in e) return;
        message.error(t('mcpKeyManagement.operationFailed'));
      } finally {
        setSaving(false);
      }
    };

    /** 复制文本 */
    const copyText = async (text: string) => {
      try {
        await navigator.clipboard.writeText(text);
        message.success(t('common.copySuccess'));
      } catch {
        message.error(t('mcpKeyManagement.operationFailed'));
      }
    };

    const close = (refresh = false) => {
      setOpen(false);
      setResult(null);
      setEditRecord(null);
      if (refresh) onSuccess?.();
    };

    // WorkBuddy 连接配置 JSON（第 3 步展示）
    const workbuddyConfig = result
      ? JSON.stringify(
          {
            mcpServers: {
              'jonex-knowledge': {
                url: mcpServerUrl,
                transport: 'streamable-http',
                headers: { Authorization: `Bearer ${result.plaintext}` },
              },
            },
          },
          null,
          2,
        )
      : '';

    // 第 2 步：授权领域服务表格列（勾选 + 服务级权限 call/view）
    const serviceColumns: ColumnsType<McpServiceItem> = [
      {
        title: t('mcpKeyManagement.selectColumn'),
        key: 'select',
        width: 60,
        render: (_: unknown, record: McpServiceItem) => (
          <Checkbox
            checked={!!selected[record.id]}
            disabled={legacyReadonly}
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
        dataIndex: 'tool_name',
        key: 'tool_name',
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
            disabled={!selected[record.id] || legacyReadonly}
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
              name="space_id"
              label={t('mcpKeyManagement.spaceLabel')}
              rules={[{ required: true, message: t('mcpKeyManagement.spaceRequired') }]}
            >
              <Select
                placeholder={t('mcpKeyManagement.spacePlaceholder')}
                options={spaceOptions}
                disabled={!!editRecord}
              />
            </Form.Item>
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
            <Form.Item
              name="permissions"
              label={t('mcpKeyManagement.permissions')}
              initialValue={['view']}
              extra={legacyReadonly ? t('mcpKeyManagement.legacyPermissionReadonlyHint') : undefined}
            >
              <Select
                mode="multiple"
                allowClear
                maxTagCount={3}
                placeholder={t('mcpKeyManagement.permissionsPlaceholder')}
                disabled={legacyReadonly}
                options={KEY_PERMISSIONS.map((p) => ({ value: p, label: permissionLabel(p) }))}
              />
            </Form.Item>
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

        {step === 3 && result && (
          <div>
            <Result status="success" title={t('mcpKeyManagement.createStep3')} />
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
                <Input.Password readOnly value={result.plaintext} />
                <Button icon={<CopyOutlined />} onClick={() => copyText(result.plaintext)}>
                  {t('mcpKeyManagement.copyKey')}
                </Button>
              </Space.Compact>
            </div>
            {/* WorkBuddy 配置模块 */}
            <div className="mcp-svc-auth-plaintext">
              <Typography.Title level={5} style={{ marginTop: 0 }}>
                {t('mcpKeyManagement.workbuddyBlock')}
              </Typography.Title>
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
            </div>
          </div>
        )}
      </Drawer>
    );
  },
);
McpKeyDrawer.displayName = 'McpKeyDrawer';
