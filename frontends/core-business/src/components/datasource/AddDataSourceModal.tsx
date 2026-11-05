import React, { useEffect, useState } from 'react';
import { Modal, Form, Input, Select, InputNumber, Typography, message, Alert, Table } from 'antd';
import { useTranslation } from 'react-i18next';
import { listAccessMethods, createDataSource, listConnections } from '@/api/dataSource';
import type { AccessMethodItem, ConnectionItem, DataSourceInstance } from '@/types/dataSource';
import { EXTENSIONS_NO_DOT } from '@/constants/upload';
import { accessMethodDisplayName } from '@/utils/dataSourceDisplay';

interface Props {
  open: boolean;
  kbId: string;
  /** 该知识库所属领域：连接池按领域隔离，只能选同领域的连接。 */
  spaceId?: string;
  /** 已有数据源的 access_type 集合，用于禁止重复添加同类型 */
  existingTypes: string[];
  onClose: () => void;
  onCreated: (ds: DataSourceInstance) => void;
}

export default function AddDataSourceModal({
  open, kbId, spaceId, existingTypes, onClose, onCreated,
}: Props) {
  const { t } = useTranslation();
  const [methods, setMethods] = useState<AccessMethodItem[]>([]);
  const [connections, setConnections] = useState<ConnectionItem[]>([]);
  /** 连接池单独一份 loading / error：它和接入方式互不牵连，但下拉里「在拉」「拉挂了」
   *  「真的一条都没有」是三种话——只有第三种才该让用户去平台管理创建。 */
  const [connectionsLoading, setConnectionsLoading] = useState(false);
  const [connectionsError, setConnectionsError] = useState(false);
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
    // 连接池并行拉，失败不挡别的接入类型；但也不能降级成「空下拉」——那会让用户看到
    // 「还没有可用的连接，请先到平台管理创建」，而连接可能好好地在那儿。单独记错误态。
    setConnections([]); // 重开弹窗先清掉上一轮的列表，免得旧数据闪一下
    setConnectionsError(false);
    if (!spaceId) {
      // 没有领域就拉不到连接（后端按领域隔离）；这不是错误，等详情带回领域。
      setConnectionsLoading(false);
      return;
    }
    setConnectionsLoading(true);
    listConnections(spaceId)
      .then(setConnections)
      .catch((e: any) => {
        setConnectionsError(true);
        message.error(e?.message || t('dataSource.loadConnectionsFailed'));
      })
      .finally(() => setConnectionsLoading(false));
  }, [open, form, t, spaceId]);

  // 选择接入方式后自动填写默认名称
  useEffect(() => {
    if (selected) {
      form.setFieldsValue({ name: accessMethodDisplayName(selected, t) });
    }
  }, [selected, form, t]);

  // 过滤已添加的类型。
  //
  // **`database` 例外**：一个知识库本来就可以接多个数据库（每个数据源一份自己的
  // schema，见 037 的按数据源粒度）。把它一起过滤掉会直接掐死「一库多数据源」。
  // 其余类型仍只允许一份——重复接同一类文档来源没有意义。
  const existingSet = new Set(existingTypes);
  const availableMethods = methods.filter(
    (m) => m.accessType === 'database' || !existingSet.has(m.accessType),
  );

  const accessType = selected?.accessType;

  // 连接下拉的三种空态：在拉 / 拉挂了 / 真的一条都没有。混成一种会把「拉挂了」说成
  // 「还没有可用的连接，请先到平台管理创建」，用户照着去建，建完发现列表里已经有几条。
  const connectionsEmptyText = connectionsLoading
    ? t('common.loading')
    : connectionsError
      ? t('dataSource.loadConnectionsFailed')
      : t('dataSource.noConnections');

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
    // `database` 走**引用**路径，不经这里（见 handleCreate）——本函数产出的是内联
    // config（engine/host/密码…），那是存量路径，添上 database 分支等于把凭证
    // 在引用行再抄一份，「改一次密码、N 个库一起生效」就不成立了。
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
      // database 走**引用**路径：只传连接 id，config_json 留空（后端 `create_source`
      // 见 connection_id 就不会去读 config_json）。别走 buildConfig —— 它产出的
      // 是内联 config（engine/host/密码…），那是存量路径，会把凭证在引用行再抄一份。
      const isReference = accessType === 'database';
      const ds = await createDataSource(
        isReference
          ? {
              knowledge_base_id: kbId,
              access_method_id: selected.id,
              access_type: selected.accessType,
              name: v.name,
              connection_id: v.connection_id,
              config_json: {},
            }
          : {
              knowledge_base_id: kbId,
              access_method_id: selected.id,
              access_type: selected.accessType,
              name: v.name,
              config_json: buildConfig(v),
            },
      );
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

          {accessType === 'database' && (
            <Form.Item
              label={t('dataSource.selectConnection')}
              name="connection_id"
              rules={[{ required: true, message: t('dataSource.connectionRequired') }]}
              extra={t('dataSource.connectionHint')}
            >
              <Select
                placeholder={t('dataSource.selectConnectionPlaceholder')}
                options={connections.map((c) => ({
                  value: c.id,
                  label: `${c.name}（${(c.config_json || {}).engine ?? ''} / ${(c.config_json || {}).database ?? ''}）`,
                }))}
                showSearch
                optionFilterProp="label"
                loading={connectionsLoading}
                notFoundContent={connectionsEmptyText}
                onChange={(value: string) => {
                  // 数据库接入默认只把名称填成接入方式名（「数据库接入」），一库多源时
                  // 会出现三条同名数据源，ETL 的目标下拉分不清是哪台库。选中连接后，
                  // 若名称还是默认值（或为空），自动带出连接名；用户敲过的不覆盖。
                  const picked = connections.find((c) => c.id === value);
                  if (!picked) return;
                  const current = String(form.getFieldValue('name') ?? '').trim();
                  const defaultName = selected ? accessMethodDisplayName(selected, t) : '';
                  if (!current || current === defaultName) {
                    form.setFieldsValue({ name: picked.name });
                  }
                }}
              />
            </Form.Item>
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
                      // 表头走 common：同app 里几张文件表的「类型」列都用它
                      // （DataSourceDocTable / DomainKnowledgeBlank），dataSource 下没有也不需要重复一份
                      { title: t('common.fileType'), dataIndex: 'type', key: 'type', width: 80 },
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
