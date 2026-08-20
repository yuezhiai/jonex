import { forwardRef, useImperativeHandle, useState } from 'react';
import { useTranslation } from 'react-i18next';
import {
  Drawer,
  Form,
  Button,
  Space,
  Input,
  Radio,
  DatePicker,
  Steps,
  Result,
  Alert,
  Typography,
  message,
} from 'antd';
import { CopyOutlined } from '@ant-design/icons';
import dayjs, { type Dayjs } from 'dayjs';
import {
  createMcpWriteKey,
  type WriteGrant,
  type WriteKeyCreateResult,
} from '@/api/mcpWriteKeys';
import type { KnowledgeBaseBrief } from '@/api/spaces';
import WriteGrantsEditor from './WriteGrantsEditor';
import './index.css';

/** 创建抽屉对外暴露的句柄 */
export type McpWriteKeyDrawerHandle = {
  open: () => void;
};

interface McpWriteKeyDrawerProps {
  /** 知识库列表（含 space_id），由调用方加载后传入 */
  kbList: KnowledgeBaseBrief[];
  /** 领域空间 ID → 名称（写入范围同空间约束提示展示） */
  spaceNameMap?: Record<string, string>;
  /** 创建成功后的回调（如刷新列表） */
  onSuccess?: () => void;
}

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

interface FormValues {
  name?: string;
  expiry_type?: string;
  expiry_custom?: Dayjs;
}

/** 创建知识写入 Key — 三步向导：①基础信息 → ②授权知识库/目录 → ③创建成功（一次性明文） */
export const McpWriteKeyDrawer = forwardRef<McpWriteKeyDrawerHandle, McpWriteKeyDrawerProps>(
  ({ kbList, spaceNameMap, onSuccess }, ref) => {
    const { t } = useTranslation();
    const [open, setOpen] = useState(false);
    const [step, setStep] = useState(1);
    const [saving, setSaving] = useState(false);
    const [result, setResult] = useState<WriteKeyCreateResult | null>(null);
    const [form] = Form.useForm<FormValues>();
    // 第 1 步填写的表单值（进入第 2 步时保存，避免 Form 卸载后取值丢失）
    const [basicInfo, setBasicInfo] = useState<FormValues | null>(null);
    // 写入范围 grants[]（Step2 编辑）
    const [grants, setGrants] = useState<WriteGrant[]>([]);

    const expiryType = Form.useWatch('expiry_type', form);
    const customDate = Form.useWatch('expiry_custom', form);
    const expiryTo = calcExpiryDate(expiryType || 'forever', customDate);

    useImperativeHandle(
      ref,
      () => ({
        open: () => {
          form.resetFields();
          setStep(1);
          setResult(null);
          setBasicInfo(null);
          setGrants([]);
          setOpen(true);
        },
      }),
      [form],
    );

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

    /** grants 前端校验（对齐后端 2.2，避免保存报 400） */
    const validateGrants = (): string | null => {
      if (!grants.length) return t('mcpWriteKeys.grantsRequired');
      for (const g of grants) {
        if (!g.kb) return t('mcpWriteKeys.kbRequired');
        if (g.mode === 'specified' && !g.directories.length) return t('mcpWriteKeys.directoriesRequired');
        if (g.mode === 'all' && g.directories.length) return t('mcpWriteKeys.allNoDirectories');
      }
      const spaces = new Set(
        grants.map((g) => kbList.find((k) => k.id === g.kb)?.space_id).filter(Boolean),
      );
      if (spaces.size > 1) return t('mcpWriteKeys.sameSpaceRequired');
      return null;
    };

    /** 第 2 步创建提交 */
    const handleCreate = async () => {
      const values = basicInfo || form.getFieldsValue();
      const error = validateGrants();
      if (error) {
        message.error(error);
        return;
      }
      setSaving(true);
      try {
        const res = await createMcpWriteKey({
          name: values.name?.trim() || '',
          expires_at: calcExpiry(values.expiry_type || '12m', values.expiry_custom),
          grants,
        });
        setResult(res);
        setStep(3);
      } catch (e) {
        if (e && typeof e === 'object' && 'errorFields' in e) return;
        message.error(t('mcpWriteKeys.operationFailed'));
      } finally {
        setSaving(false);
      }
    };

    const copyText = async (text: string) => {
      try {
        await navigator.clipboard.writeText(text);
        message.success(t('common.copySuccess'));
      } catch {
        message.error(t('mcpWriteKeys.operationFailed'));
      }
    };

    const close = (refresh = false) => {
      setOpen(false);
      setResult(null);
      if (refresh) onSuccess?.();
    };

    const footer =
      step === 3 ? (
        <div style={{ textAlign: 'right' }}>
          <Button type="primary" onClick={() => close(true)}>
            {t('mcpWriteKeys.done')}
          </Button>
        </div>
      ) : (
        <div style={{ display: 'flex', justifyContent: 'space-between' }}>
          <Button onClick={() => close(false)}>{t('common.cancel')}</Button>
          <Space>
            {step === 2 && <Button onClick={() => setStep(1)}>{t('mcpWriteKeys.prevStep')}</Button>}
            {step === 1 ? (
              <Button type="primary" onClick={nextStep}>
                {t('mcpWriteKeys.nextStep')}
              </Button>
            ) : (
              <Button type="primary" loading={saving} onClick={handleCreate}>
                {t('mcpWriteKeys.createBtn')}
              </Button>
            )}
          </Space>
        </div>
      );

    return (
      <Drawer
        title={t('mcpWriteKeys.createTitle')}
        placement="right"
        width={680}
        open={open}
        onClose={() => close(step === 3)}
        footer={footer}
        destroyOnHidden
      >
        <Steps
          size="small"
          current={step - 1}
          items={[
            { title: t('mcpWriteKeys.step1') },
            { title: t('mcpWriteKeys.step2') },
            { title: t('mcpWriteKeys.step3') },
          ]}
          style={{ marginBottom: 24 }}
        />

        {/* Form 始终挂载（step 2/3 时隐藏），避免卸载导致已填字段值丢失 */}
        <div style={{ display: step === 1 ? undefined : 'none' }}>
          <Form form={form} layout="vertical">
            <Form.Item
              name="name"
              label={t('mcpWriteKeys.nameLabel')}
              rules={[{ required: true, whitespace: true, message: t('mcpWriteKeys.nameRequired') }]}
            >
              <Input placeholder={t('mcpWriteKeys.namePlaceholder')} maxLength={255} />
            </Form.Item>
            <Form.Item name="expiry_type" label={t('mcpWriteKeys.expiryLabel')} initialValue="12m">
              <Radio.Group>
                <Space direction="vertical">
                  {EXPIRY_OPTIONS.map((o) => (
                    <Radio key={o.value} value={o.value}>
                      {t(`mcpWriteKeys.${o.labelKey}`)}
                    </Radio>
                  ))}
                  <Radio value="forever">{t('mcpWriteKeys.expiryForever')}</Radio>
                  <Radio value="custom">{t('mcpWriteKeys.expiryCustom')}</Radio>
                </Space>
              </Radio.Group>
            </Form.Item>
            {expiryType === 'custom' && (
              <Form.Item
                name="expiry_custom"
                label={t('mcpWriteKeys.expiryLabel')}
                rules={[{ required: true, message: t('mcpWriteKeys.customDateRequired') }]}
              >
                <DatePicker style={{ width: '100%' }} />
              </Form.Item>
            )}
            {expiryTo && (
              <Typography.Text type="secondary">
                {t('mcpWriteKeys.expiryTo', { date: expiryTo })}
              </Typography.Text>
            )}
          </Form>
        </div>

        {step === 2 && (
          <div>
            <Alert
              type="info"
              showIcon
              message={t('mcpWriteKeys.step2Hint')}
              style={{ marginBottom: 16 }}
            />
            <WriteGrantsEditor
              value={grants}
              onChange={setGrants}
              kbList={kbList}
              spaceNameMap={spaceNameMap}
            />
          </div>
        )}

        {step === 3 && result && (
          <div>
            <Result status="success" title={t('mcpWriteKeys.createSuccess')} />
            <Alert
              type="warning"
              showIcon
              message={t('mcpWriteKeys.plaintextWarning')}
              style={{ marginBottom: 16 }}
            />
            <div className="mcp-svc-auth-plaintext">
              <Typography.Text type="secondary" style={{ display: 'block', marginBottom: 4 }}>
                {t('mcpWriteKeys.nameLabel')}
              </Typography.Text>
              <div style={{ marginBottom: 12 }}>{result.name || '-'}</div>
              <Typography.Text type="secondary" style={{ display: 'block', marginBottom: 4 }}>
                {t('mcpWriteKeys.keyPrefix')}
              </Typography.Text>
              <Space.Compact style={{ width: '100%' }}>
                <Input.Password readOnly value={result.plaintext} />
                <Button icon={<CopyOutlined />} onClick={() => copyText(result.plaintext)}>
                  {t('mcpWriteKeys.copyKey')}
                </Button>
              </Space.Compact>
            </div>
          </div>
        )}
      </Drawer>
    );
  },
);
McpWriteKeyDrawer.displayName = 'McpWriteKeyDrawer';
