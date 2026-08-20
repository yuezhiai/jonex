import { forwardRef, useImperativeHandle, useState } from 'react';
import { Modal, Button, Space, message } from 'antd';
import { useTranslation } from 'react-i18next';
import { resetMcpKey, type McpKeyItem, type McpKeyCreateResult } from '@/api/mcpKeys';
import { PlaintextBlock } from './McpKeyPlaintext';

/** 启用（重置）弹窗对外暴露的句柄 */
export type EnableKeyModalHandle = {
  open: (record: McpKeyItem) => void;
};

interface EnableKeyModalProps {
  /** 启用成功后的回调（如刷新列表） */
  onSuccess?: () => void;
}

/** 启用（重置）MCP Key 弹窗：撤销旧 Key 并生成新 Key，返回一次性明文 */
export const EnableKeyModal = forwardRef<EnableKeyModalHandle, EnableKeyModalProps>(({ onSuccess }, ref) => {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const [saving, setSaving] = useState(false);
  const [target, setTarget] = useState<McpKeyItem | null>(null);
  const [result, setResult] = useState<McpKeyCreateResult | null>(null);

  useImperativeHandle(
    ref,
    () => ({
      open: (record: McpKeyItem) => {
        setTarget(record);
        setResult(null);
        setOpen(true);
      },
    }),
    [],
  );

  const handleEnable = async () => {
    if (!target) return;
    setSaving(true);
    try {
      const res = await resetMcpKey(target.id);
      setResult(res);
      message.success(t('mcpKeyManagement.enableSuccess'));
    } catch (err: unknown) {
      message.error(err instanceof Error ? err.message : t('mcpKeyManagement.operationFailed'));
    } finally {
      setSaving(false);
    }
  };

  const close = (refresh = false) => {
    setOpen(false);
    setTarget(null);
    setResult(null);
    if (refresh) onSuccess?.();
  };

  return (
    <Modal
      title={result ? t('mcpKeyManagement.plaintextTitle') : t('mcpKeyManagement.enableTitle')}
      open={open}
      onCancel={() => close(!!result)}
      footer={
        result ? (
          <Button type="primary" onClick={() => close(true)}>
            {t('common.confirm')}
          </Button>
        ) : (
          <Space>
            <Button onClick={() => close(false)}>{t('common.cancel')}</Button>
            <Button type="primary" loading={saving} onClick={handleEnable}>
              {t('mcpKeyManagement.enableBtn')}
            </Button>
          </Space>
        )
      }
      width={520}
      maskClosable={false}
      destroyOnHidden
    >
      {result ? (
        <PlaintextBlock result={result} />
      ) : (
        <div style={{ textAlign: 'center', padding: '12px 0' }}>
          <p style={{ fontSize: 14, color: '#64748b', marginBottom: 8 }}>
            {t('mcpKeyManagement.enableConfirm', { name: target?.name || target?.key_prefix || '-' })}
          </p>
          <p style={{ fontSize: 15, fontWeight: 600, margin: 0 }}>
            {target?.key_prefix || '-'}
          </p>
        </div>
      )}
    </Modal>
  );
});
EnableKeyModal.displayName = 'EnableKeyModal';
