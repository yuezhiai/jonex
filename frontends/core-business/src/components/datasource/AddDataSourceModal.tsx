import React, { useEffect, useState } from 'react';
import { Modal, Form, Input, Select, InputNumber, Typography, message, Alert, Table } from 'antd';
import { useTranslation } from 'react-i18next';
import { listAccessMethods, createDataSource } from '@/api/dataSource';
import type { AccessMethodItem, DataSourceInstance } from '@/types/dataSource';
import { EXTENSIONS_NO_DOT } from '@/constants/upload';
import { accessMethodDisplayName } from '@/utils/dataSourceDisplay';

interface Props {
  open: boolean;
  kbId: string;
  /** 已有数据源的 access_type 集合，用于禁止重复添加同类型 */
  existingTypes: string[];
  onClose: () => void;
  onCreated: (ds: DataSourceInstance) => void;
}

export default function AddDataSourceModal({ open, kbId, existingTypes, onClose, onCreated }: Props) {
  const { t } = useTranslation();
  const [methods, setMethods] = useState<AccessMethodItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [showApiHelp, setShowApiHelp] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [selected, setSelected] = useState<AccessMethodItem | null>(null);
  const [created, setCreated] = useState<DataSourceInstance | null>(null);
  const [form] = Form.useForm();

  useEffect(() => {
    if (!open) return;
    setSelected(null);
    setCreated(null);
    setShowApiHelp(false);
    form.resetFields();
    setLoading(true);
    listAccessMethods()
      .then(setMethods)
      .catch((e: any) => message.error(e?.message || t('dataSource.loadMethodsFailed')))
      .finally(() => setLoading(false));
  }, [open, form, t]);

  // 选择接入方式后自动填写默认名称
  useEffect(() => {
    if (selected) {
      form.setFieldsValue({ name: accessMethodDisplayName(selected, t) });
    }
  }, [selected, form, t]);

  // 过滤已添加的类型
  const existingSet = new Set(existingTypes);
  const availableMethods = methods.filter((m) => !existingSet.has(m.accessType));

  const accessType = selected?.accessType;

  function buildConfig(v: any): Record<string, any> {
    if (accessType === 'api') {
      return {
        endpoint: v.endpoint,
        method: v.method || 'GET',
        auth: { type: v.authType || 'none', token: v.token || undefined, header_name: v.headerName || undefined },
        list_path: v.listPath || '$.data.items',
        file_url_field: v.fileUrlField || 'url',
        file_name_field: v.fileNameField || 'name',
      };
    }
    if (accessType === 'storage') {
      return {
        backend: v.backend || 'minio',
        endpoint: v.endpoint || undefined,
        bucket: v.bucket,
        prefix: v.prefix || '',
        region: v.region || undefined,
        credential: v.credential || undefined,
        include_ext: (v.includeExt || '')
          .split(',')
          .map((s: string) => s.trim())
          .filter(Boolean),
      };
    }
    if (accessType === 'api_push') {
      return {
        allowed_ext: (v.allowedExt || 'pdf,docx,md,txt')
          .split(',')
          .map((s: string) => s.trim())
          .filter(Boolean),
        max_file_mb: v.maxFileMb || 50,
      };
    }
    if (accessType === 'file') {
      return {};
    }
    return {};
  }

  async function handleCreate() {
    if (!selected) {
      message.warning(t('dataSource.selectFirst'));
      return;
    }
    const v = await form.validateFields();
    setSubmitting(true);
    try {
      const ds = await createDataSource({
        knowledge_base_id: kbId,
        access_method_id: selected.id,
        access_type: selected.accessType,
        name: v.name,
        config_json: buildConfig(v),
      });
      if (ds.accessType === 'api_push' && ds.ingestKey) {
        setCreated(ds); // 展示一次性 key，不立即关闭
      } else {
        message.success(t('dataSource.createdSuccess'));
        onCreated(ds);
        onClose();
      }
    } catch (e: any) {
      message.error(e?.message || t('dataSource.createFailed'));
    } finally {
      setSubmitting(false);
    }
  }

  function handleOk() {
    if (created) {
      onCreated(created);
      onClose();
    } else {
      void handleCreate();
    }
  }

  return (
    <Modal
      title={t('dataSource.addDataSource')}
      open={open}
      onCancel={onClose}
      onOk={handleOk}
      okText={created ? t('dataSource.done') : t('common.create')}
      confirmLoading={submitting}
      width={640}
      destroyOnHidden
    >
      <Select
        style={{ width: '100%', marginBottom: 16 }}
        loading={loading}
        placeholder={t('dataSource.selectAccessMethod')}
        value={selected?.id}
        disabled={!!created}
        notFoundContent={loading ? t('common.loading') : t('dataSource.noAvailableMethods')}
        onChange={(id) => setSelected(methods.find((m) => m.id === id) || null)}
        options={availableMethods.map((m) => ({
          label: accessMethodDisplayName(m, t),
          value: m.id,
        }))}
      />

      {!created && selected && (
        <Form form={form} layout="vertical">
          <Form.Item
            label={t('domainKnowledge.dataSourceName')}
            name="name"
            rules={[{ required: true, whitespace: true, message: t('common.nameRequired') }]}
          >
            <Input placeholder={t('dataSource.namePlaceholder')} />
          </Form.Item>

          {accessType === 'api' && (
            <>
              <div
                onClick={() => setShowApiHelp((prev) => !prev)}
                style={{
                  cursor: 'pointer',
                  color: '#3b82f6',
                  fontSize: 13,
                  marginBottom: showApiHelp ? 8 : 16,
                  userSelect: 'none',
                }}
              >
                {showApiHelp ? '▼' : '▶'} {t('dataSource.apiPullTitle')}
              </div>
              {showApiHelp && (
                <Alert
                  type="info"
                  showIcon
                  style={{ marginBottom: 16 }}
                  description={
                    <div style={{ fontSize: 13 }}>
                      <p style={{ margin: '4px 0' }}>{t('dataSource.apiPullDesc1')}</p>
                      <p style={{ margin: '4px 0' }}>{t('dataSource.apiPullDesc2')}</p>
                      <p style={{ margin: '4px 0', color: '#94a3b8' }}>
                        {t('dataSource.apiPullDesc3')}
                        {`{"code":0,"data":{"items":[{"url":"https://...","name":"report.pdf"},{"url":"https://...","name":"contract.docx"}]}}`}
                      </p>
                    </div>
                  }
                />
              )}
              <Form.Item
                label={t('domainKnowledge.apiEndpoint')}
                name="endpoint"
                rules={[
                  { required: true, message: t('dataSource.endpointRequired') },
                  {
                    pattern: /^https?:\/\/\S+/i,
                    message: t('dataSource.endpointInvalid'),
                  },
                ]}
                tooltip={t('dataSource.endpointTooltip')}
              >
                <Input placeholder={t('dataSource.endpointPlaceholder')} />
              </Form.Item>
              <Form.Item
                label={t('domainKnowledge.httpMethod')}
                name="method"
                initialValue="GET"
                tooltip={t('dataSource.methodTooltip')}
              >
                <Select
                  options={[
                    { value: 'GET', label: 'GET' },
                    { value: 'POST', label: 'POST' },
                  ]}
                />
              </Form.Item>
              <Form.Item
                label={t('domainKnowledge.authMethod')}
                name="authType"
                initialValue="none"
                tooltip={t('dataSource.authMethodTooltip')}
              >
                <Select
                  options={[
                    { value: 'none', label: t('domainKnowledge.noAuth') },
                    { value: 'bearer', label: t('domainKnowledge.bearerToken') },
                    { value: 'api_key', label: t('domainKnowledge.apiKey') },
                    { value: 'basic', label: t('domainKnowledge.basicAuth') },
                  ]}
                />
              </Form.Item>
              <Form.Item
                label={t('domainKnowledge.credentialToken')}
                name="token"
                tooltip={t('dataSource.credentialTooltipAdd')}
              >
                <Input.Password placeholder={t('dataSource.credentialPlaceholderAdd')} />
              </Form.Item>
              <Form.Item
                label={t('domainKnowledge.apiKeyHeaderName')}
                name="headerName"
                tooltip={t('dataSource.headerNameTooltip')}
              >
                <Input placeholder="X-API-Key" />
              </Form.Item>
              <Form.Item
                label={t('domainKnowledge.docListJsonPath')}
                name="listPath"
                initialValue="$.data.items"
                tooltip={t('dataSource.jsonPathTooltip')}
              >
                <Input placeholder="$.data.items" />
              </Form.Item>
              <Form.Item
                label={t('domainKnowledge.downloadUrlField')}
                name="fileUrlField"
                initialValue="url"
                tooltip={t('dataSource.urlFieldTooltip')}
              >
                <Input placeholder="url" />
              </Form.Item>
              <Form.Item
                label={t('domainKnowledge.fileNameField')}
                name="fileNameField"
                initialValue="name"
                tooltip={t('dataSource.nameFieldTooltip')}
              >
                <Input placeholder="name" />
              </Form.Item>
            </>
          )}

          {accessType === 'storage' && (
            <>
              <Form.Item label={t('domainKnowledge.storageBackend')} name="backend" initialValue="minio">
                <Select
                  options={[
                    { value: 'minio', label: 'MinIO' },
                    { value: 's3', label: 'AWS S3' },
                    { value: 'cos', label: t('domainKnowledge.tencentCos') },
                    { value: 'oss', label: t('domainKnowledge.aliyunOss') },
                  ]}
                />
              </Form.Item>
              <Form.Item
                label={t('dataSource.endpoint')}
                name="endpoint"
                rules={[
                  {
                    pattern: /^https?:\/\/\S+/i,
                    message: t('dataSource.endpointInvalid'),
                  },
                ]}
                tooltip={t('dataSource.storageEndpointTooltip')}
              >
                <Input placeholder="http://minio:9000" />
              </Form.Item>
              <Form.Item
                label={t('domainKnowledge.bucket')}
                name="bucket"
                rules={[{ required: true, message: t('dataSource.bucketRequired') }]}
              >
                <Input placeholder="product-files" />
              </Form.Item>
              <Form.Item label={t('domainKnowledge.prefix')} name="prefix">
                <Input placeholder="kb/finance/" />
              </Form.Item>
              <Form.Item label={t('dataSource.region')} name="region">
                <Input placeholder={t('dataSource.regionPlaceholder')} />
              </Form.Item>
              <Form.Item
                label={t('domainKnowledge.credential')}
                name="credential"
                rules={[{ required: true, message: t('dataSource.credentialRequired') }]}
              >
                <Input.Password placeholder="accessKey:secretKey" />
              </Form.Item>
              <Form.Item label={t('domainKnowledge.includeExt')} name="includeExt" initialValue={EXTENSIONS_NO_DOT}>
                <Input.TextArea rows={2} placeholder={EXTENSIONS_NO_DOT} />
              </Form.Item>
            </>
          )}

          {accessType === 'file' && (
            <Alert
              type="info"
              showIcon
              style={{ marginBottom: 16 }}
              description={
                <div style={{ fontSize: 13 }}>
                  <p style={{ margin: '0 0 8px' }}>{t('dataSource.fileUploadDesc')}</p>
                  <Table
                    dataSource={[
                      {
                        type: t('dataSource.fileTypeDoc'),
                        exts: 'PDF · DOC · DOCX · PPT · PPTX · XLS · XLSX · TXT · MD',
                      },
                      { type: t('dataSource.fileTypeImage'), exts: 'JPG · JPEG · PNG · GIF · BMP · TIFF · TIF · WEBP' },
                      {
                        type: t('dataSource.fileTypeAudio'),
                        exts: 'MP3 · WAV · FLAC · AAC · M4A · OGG · WMA · OPUS · AMR',
                      },
                      {
                        type: t('dataSource.fileTypeVideo'),
                        exts: 'MP4 · AVI · MOV · MKV · FLV · WMV · WEBM · M4V · MPG · MPEG · 3GP',
                      },
                    ]}
                    columns={[
                      { title: t('dataSource.fileType'), dataIndex: 'type', key: 'type', width: 80 },
                      { title: t('dataSource.extensions'), dataIndex: 'exts', key: 'exts' },
                    ]}
                    rowKey="type"
                    pagination={false}
                    size="small"
                    bordered
                  />
                </div>
              }
            />
          )}

          {accessType === 'api_push' && (
            <>
              <Form.Item label={t('domainKnowledge.allowedExt')} name="allowedExt" initialValue={EXTENSIONS_NO_DOT}>
                <Input.TextArea rows={2} placeholder={EXTENSIONS_NO_DOT} />
              </Form.Item>
              <Form.Item label={t('domainKnowledge.maxFileSize')} name="maxFileMb" initialValue={50}>
                <InputNumber min={1} max={500} style={{ width: '100%' }} />
              </Form.Item>
            </>
          )}
        </Form>
      )}

      {created && (
        <Alert
          type="success"
          showIcon
          title={t('dataSource.createdTitle')}
          description={
            <div>
              <div style={{ marginBottom: 8 }}>
                {t('dataSource.ingestEndpoint')}
                <Typography.Text copyable>{created.ingestUrl}</Typography.Text>
              </div>
              <div>
                {t('dataSource.apiKeyLabel')}
                <Typography.Text copyable code>
                  {created.ingestKey}
                </Typography.Text>
              </div>
            </div>
          }
        />
      )}
    </Modal>
  );
}
