import { Alert, Input, Space, Typography } from 'antd';
import { useTranslation } from 'react-i18next';
import type { McpKeyCreateResult } from '@/api/mcpKeys';

/** 创建 / 启用成功后的一次性明文展示块 */
export function PlaintextBlock({ result }: { result: McpKeyCreateResult }) {
  const { t } = useTranslation();
  return (
    <div>
      <Alert type="warning" showIcon message={t('mcpKeyManagement.plaintextWarning')} style={{ marginBottom: 16 }} />
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 8 }}>
        <span style={{ color: '#64748b' }}>{t('mcpKeyManagement.nameLabel')}</span>
        <strong>{result.name || '-'}</strong>
      </div>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 12 }}>
        <span style={{ color: '#64748b' }}>{t('mcpKeyManagement.keyPrefix')}</span>
        <strong>{result.key_prefix || '-'}</strong>
      </div>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
        <Input.Password readOnly value={result.plaintext} style={{ flex: 1 }} />
        <Typography.Text
          copyable={{
            text: result.plaintext,
            tooltips: [t('mcpKeyManagement.copyBtn'), t('mcpKeyManagement.copied')],
          }}
        />
      </div>
    </div>
  );
}
