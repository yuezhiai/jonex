import React, { useState } from 'react';
import { Modal, Input, Form, Select, InputNumber, Alert, message } from 'antd';
import { EditOutlined } from '@ant-design/icons';
import { useTranslation } from 'react-i18next';
import { updateDataSource } from '@/api/dataSource';
import type { DataSourceConfig } from '@/types/domainKnowledge';

interface DataSourceEditModalProps {
  editingDs: DataSourceConfig | null;
  onClose: () => void;
  onSaved: () => void;
}

export default function DataSourceEditModal({ editingDs, onClose, onSaved }: DataSourceEditModalProps) {
  const { t } = useTranslation();
  const [editingName, setEditingName] = useState(editingDs?.name || '');
  const [editingExtStr, setEditingExtStr] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [editingConfig, setEditingConfig] = useState<Record<string, any>>(editingDs?.configJson || {});
  const [endpointError, setEndpointError] = useState('');

  // 同步 props 到内部状态
  React.useEffect(() => {
    if (editingDs) {
      setEditingName(editingDs.name);
      setEditingConfig(editingDs.configJson || {});
      const extArr =
        editingDs.accessType === 'api_push'
          ? editingDs.configJson?.allowed_ext || []
          : editingDs.accessType === 'storage'
            ? editingDs.configJson?.include_ext || []
            : [];
      setEditingExtStr(Array.isArray(extArr) ? extArr.join(',') : String(extArr || ''));
      setEndpointError('');
    }
  }, [editingDs]);

  const getEditField = (key: string): any => {
    if (key in editingConfig) return editingConfig[key];
    return '';
  };

  const setEditField = (key: string, value: any) => {
    setEditingConfig((prev) => ({ ...prev, [key]: value }));
  };

  const buildEditConfig = (): Record<string, any> => {
    const cfg = { ...editingConfig };
    const at = editingDs?.accessType;
    if (at === 'api') {
      const auth = cfg.auth || {};
      if (!auth.token) delete auth.token;
      cfg.auth = auth;
    }
    if (at === 'api_push') {
      cfg.allowed_ext = editingExtStr
        .split(',')
        .map((s) => s.trim())
        .filter(Boolean);
      cfg.max_file_mb = cfg.max_file_mb || 50;
    }
    if (at === 'storage') {
      if (!cfg.credential) delete cfg.credential;
      cfg.include_ext = editingExtStr
        .split(',')
        .map((s) => s.trim())
        .filter(Boolean);
    }
    return cfg;
  };

  const handleOk = async () => {
    if (!editingDs || !editingName.trim()) {
      message.warning(t('domainKnowledge.nameCannotBeEmpty'));
      return;
    }
    // Endpoint URL 格式校验（api 必填，storage 可选，填了须合法）
    if (editingDs.accessType === 'api' || editingDs.accessType === 'storage') {
      const endpoint = (editingConfig.endpoint || '').trim();
      if (editingDs.accessType === 'api' && !endpoint) {
        setEndpointError(t('dataSource.endpointRequired'));
        return;
      }
      if (endpoint && !/^https?:\/\/\S+/i.test(endpoint)) {
        setEndpointError(t('dataSource.endpointInvalid'));
        return;
      }
    }
    setEndpointError('');
    setSubmitting(true);
    try {
      const cfg = buildEditConfig();
      await updateDataSource(editingDs.id, {
        name: editingName.trim(),
        config_json: cfg,
      });
      message.success(t('domainKnowledge.updated'));
      onSaved();
    } catch (err: any) {
      message.error(err?.message || t('domainKnowledge.updateFailed'));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Modal
      wrapClassName="yx-domain-space-modal"
      title={
        <span>
          <EditOutlined style={{ color: '#3b82f6', marginRight: 8 }} />
          {t('domainKnowledge.editDataSource')}
        </span>
      }
      open={!!editingDs}
      onCancel={onClose}
      onOk={handleOk}
      confirmLoading={submitting}
      okText={t('common.save')}
      cancelText={t('common.cancel')}
      width={560}
    >
      <p className="yx-modal-access-method">
        {t('domainKnowledge.accessMethod')}: <strong>{editingDs?.type}</strong>
      </p>
      <Form layout="vertical">
        <Form.Item label={t('domainKnowledge.dataSourceName')} required>
          <Input
            value={editingName}
            onChange={(e) => setEditingName(e.target.value)}
            placeholder={t('domainKnowledge.dataSourceNamePlaceholder')}
          />
        </Form.Item>

        {editingDs?.accessType === 'api' && (
          <>
            <Form.Item
              label={t('domainKnowledge.apiEndpoint')}
              validateStatus={endpointError ? 'error' : undefined}
              help={endpointError}
            >
              <Input
                value={getEditField('endpoint')}
                onChange={(v) => {
                  setEditField('endpoint', v.target.value);
                  setEndpointError('');
                }}
                placeholder="https://api.example.com/documents"
              />
            </Form.Item>
            <Form.Item label={t('domainKnowledge.httpMethod')}>
              <Select
                value={getEditField('method') || 'GET'}
                onChange={(v) => setEditField('method', v)}
                options={[
                  { value: 'GET', label: 'GET' },
                  { value: 'POST', label: 'POST' },
                ]}
              />
            </Form.Item>
            <Form.Item label={t('domainKnowledge.authMethod')}>
              <Select
                value={editingConfig?.auth?.type || 'none'}
                onChange={(v) => setEditField('auth', { ...(editingConfig?.auth || {}), type: v })}
                options={[
                  { value: 'none', label: t('domainKnowledge.noAuth') },
                  { value: 'bearer', label: t('domainKnowledge.bearerToken') },
                  { value: 'api_key', label: t('domainKnowledge.apiKey') },
                  { value: 'basic', label: t('domainKnowledge.basicAuth') },
                ]}
              />
            </Form.Item>
            <Form.Item label={t('domainKnowledge.credentialToken')} tooltip={t('domainKnowledge.credentialTooltip')}>
              <Input.Password
                placeholder={t('domainKnowledge.credentialPlaceholder')}
                onChange={(v) => setEditField('auth', { ...(editingConfig?.auth || {}), token: v.target.value })}
              />
            </Form.Item>
            <Form.Item label={t('domainKnowledge.apiKeyHeaderName')}>
              <Input
                value={getEditField('auth')?.header_name || ''}
                onChange={(v) => setEditField('auth', { ...(editingConfig?.auth || {}), header_name: v.target.value })}
                placeholder="X-API-Key"
              />
            </Form.Item>
            <Form.Item label={t('domainKnowledge.docListJsonPath')}>
              <Input
                value={getEditField('list_path') || '$.data.items'}
                onChange={(v) => setEditField('list_path', v.target.value)}
                placeholder="$.data.items"
              />
            </Form.Item>
            <Form.Item label={t('domainKnowledge.downloadUrlField')}>
              <Input
                value={getEditField('file_url_field') || 'url'}
                onChange={(v) => setEditField('file_url_field', v.target.value)}
                placeholder="url"
              />
            </Form.Item>
            <Form.Item label={t('domainKnowledge.fileNameField')}>
              <Input
                value={getEditField('file_name_field') || 'name'}
                onChange={(v) => setEditField('file_name_field', v.target.value)}
                placeholder="name"
              />
            </Form.Item>
          </>
        )}

        {editingDs?.accessType === 'storage' && (
          <>
            <Form.Item label={t('domainKnowledge.storageBackend')}>
              <Select
                value={getEditField('backend') || 'minio'}
                onChange={(v) => setEditField('backend', v)}
                options={[
                  { value: 'minio', label: 'MinIO' },
                  { value: 's3', label: 'AWS S3' },
                  { value: 'cos', label: t('domainKnowledge.tencentCos') },
                  { value: 'oss', label: t('domainKnowledge.aliyunOss') },
                ]}
              />
            </Form.Item>
            <Form.Item
              label={t('domainKnowledge.dataSourceDesc.endpoint')}
              validateStatus={endpointError ? 'error' : undefined}
              help={endpointError}
            >
              <Input
                value={getEditField('endpoint')}
                onChange={(v) => {
                  setEditField('endpoint', v.target.value);
                  setEndpointError('');
                }}
                placeholder="http://minio:9000"
              />
            </Form.Item>
            <Form.Item label={t('domainKnowledge.bucket')}>
              <Input
                value={getEditField('bucket')}
                onChange={(v) => setEditField('bucket', v.target.value)}
                placeholder="product-files"
              />
            </Form.Item>
            <Form.Item label={t('domainKnowledge.prefix')}>
              <Input
                value={getEditField('prefix')}
                onChange={(v) => setEditField('prefix', v.target.value)}
                placeholder="kb/finance/"
              />
            </Form.Item>
            <Form.Item label={t('domainKnowledge.credential')} tooltip={t('domainKnowledge.credentialTooltip')}>
              <Input.Password
                placeholder={t('domainKnowledge.credentialPlaceholder')}
                onChange={(v) => setEditField('credential', v.target.value)}
              />
            </Form.Item>
            <Form.Item label={t('domainKnowledge.includeExt')}>
              <Input.TextArea
                autoSize={{ minRows: 2, maxRows: 4 }}
                value={editingExtStr}
                onChange={(e) => setEditingExtStr(e.target.value)}
              />
            </Form.Item>
          </>
        )}

        {editingDs?.accessType === 'api_push' && (
          <>
            <Form.Item label={t('domainKnowledge.allowedExt')}>
              <Input.TextArea
                autoSize={{ minRows: 2, maxRows: 4 }}
                value={editingExtStr}
                onChange={(e) => setEditingExtStr(e.target.value)}
              />
            </Form.Item>
            <Form.Item label={t('domainKnowledge.maxFileSize')}>
              <InputNumber
                min={1}
                max={500}
                value={Number(getEditField('max_file_mb')) || 50}
                onChange={(v) => setEditField('max_file_mb', v ?? 50)}
                style={{ width: '100%' }}
              />
            </Form.Item>
          </>
        )}

        {editingDs?.accessType === 'file' && (
          <Alert type="info" showIcon style={{ marginTop: 4 }} description={t('domainKnowledge.fileUploadNoConfig')} />
        )}
      </Form>
    </Modal>
  );
}
