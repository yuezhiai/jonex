import { forwardRef, useImperativeHandle, useState } from 'react';
import { Modal, Form, Button, Space, Radio, DatePicker, Alert, Input, Typography, message } from 'antd';
import { CopyOutlined } from '@ant-design/icons';
import { useTranslation } from 'react-i18next';
import dayjs, { type Dayjs } from 'dayjs';
import { recreateKey } from '@/api/domainServices';
import type { McpKeyCreateResult, McpKeyItem } from '@/api/mcpKeys';

/** 重新创建抽屉对外暴露的句柄 */
export type RecreateKeyModalHandle = {
  open: (record: McpKeyItem) => void;
};

interface RecreateKeyModalProps {
  /** 成功后的回调（如刷新列表） */
  onSuccess?: () => void;
}

/** 有效期固定档位（月数 + i18n label key） */
const EXPIRY_OPTIONS = [
  { value: '3m', labelKey: 'expiry3m', months: 3 },
  { value: '6m', labelKey: 'expiry6m', months: 6 },
  { value: '12m', labelKey: 'expiry12m', months: 12 },
];

const calcExpiry = (type: string, customDate?: Dayjs): string | null => {
  if (type === 'forever') return null;
  if (type === 'custom') return customDate ? customDate.endOf('day').toISOString() : null;
  const opt = EXPIRY_OPTIONS.find((o) => o.value === type);
  return opt ? dayjs().add(opt.months, 'month').endOf('day').toISOString() : null;
};

interface FormValues {
  expiry_type?: string;
  expiry_custom?: Dayjs;
}

/** 重新创建已过期 Key 弹窗：可选新有效期，成功后展示一次性明文 */
export const RecreateKeyModal = forwardRef<RecreateKeyModalHandle, RecreateKeyModalProps>(
  ({ onSuccess }, ref) => {
    const { t } = useTranslation();
    const [open, setOpen] = useState(false);
    const [target, setTarget] = useState<McpKeyItem | null>(null);
    const [saving, setSaving] = useState(false);
    const [result, setResult] = useState<McpKeyCreateResult | null>(null);
    const [form] = Form.useForm<FormValues>();
    const expiryType = Form.useWatch('expiry_type', form);

    useImperativeHandle(
      ref,
      () => ({
        open: (record: McpKeyItem) => {
          setTarget(record);
          setResult(null);
          form.setFieldsValue({ expiry_type: '12m' });
          setOpen(true);
        },
      }),
      [form],
    );

    const handleRecreate = async () => {
      if (!target) return;
      const values = form.getFieldsValue();
      setSaving(true);
      try {
        // service_id 仅为契约兼容，key_id 唯一定位
        const serviceId = target.service_ids?.[0] || '';
        const res = await recreateKey(serviceId, target.id, {
          expires_at: calcExpiry(values.expiry_type || '12m', values.expiry_custom),
        });
        setResult(res);
      } catch (e) {
        if (e && typeof e === 'object' && 'errorFields' in e) return;
        message.error(t('mcpKeyManagement.operationFailed'));
      } finally {
        setSaving(false);
      }
    };

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
      if (refresh) onSuccess?.();
    };

    return (
      <Modal
        title={result ? t('mcpKeyManagement.recreateResultTitle') : t('mcpKeyManagement.recreateTitle')}
        open={open}
        onCancel={() => close(!!result)}
        footer={
          result ? (
            <Button type="primary" onClick={() => close(true)}>
              {t('mcpKeyManagement.done')}
            </Button>
          ) : (
            <Space>
              <Button onClick={() => close(false)}>{t('common.cancel')}</Button>
              <Button type="primary" loading={saving} onClick={handleRecreate}>
                {t('mcpKeyManagement.recreateBtn')}
              </Button>
            </Space>
          )
        }
        width={520}
        maskClosable={false}
        destroyOnHidden
      >
        {result ? (
          <div>
            <Alert
              type="warning"
              showIcon
              message={t('mcpKeyManagement.keyPlaintextWarning')}
              style={{ marginBottom: 16 }}
            />
            <Typography.Text type="secondary">{t('mcpKeyManagement.nameLabel')}</Typography.Text>
            <div style={{ marginBottom: 12 }}>{result.name || '-'}</div>
            <Typography.Text type="secondary">{t('mcpKeyManagement.keyPrefix')}</Typography.Text>
            <Space.Compact style={{ width: '100%', marginBottom: 12 }}>
              <Input.Password readOnly value={result.plaintext} />
              <Button icon={<CopyOutlined />} onClick={() => copyText(result.plaintext)}>
                {t('mcpKeyManagement.copyKey')}
              </Button>
            </Space.Compact>
          </div>
        ) : (
          <Form form={form} layout="vertical" initialValues={{ expiry_type: '12m' }}>
            <Form.Item name="expiry_type" label={t('mcpKeyManagement.expiryLabel')}>
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
          </Form>
        )}
      </Modal>
    );
  },
);
RecreateKeyModal.displayName = 'RecreateKeyModal';
