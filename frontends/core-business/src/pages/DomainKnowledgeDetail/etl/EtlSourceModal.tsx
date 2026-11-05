/**
 * 「ETL 集成」页签 → 新建来源弹窗。
 *
 * 只登记来源（名称 / 类型 / 目标数据源），不在这里上传东西：
 * - `file` 类型：建成后到列表行里上传 JSON/CSV 血缘文件；
 * - `openlineage` 类型：建成后把一次性 key + 推送地址交给 OpenLineage 端。
 *
 * 目标数据源**只能是本知识库下的 `database` 源**（后端 `_validate_etl_target`
 * 同口径校验）——血缘边最终落在它的编译 schema 上，选错源等于把边写到空气里。
 *
 * 创建成功且回包带 `ingestKey` 时，**不立即关闭**：key 明文只回这一次，
 * 关掉就再也看不到，只能重置。展示到用户点「完成」为止（同 AddDataSourceModal）。
 */
import { useEffect, useState } from 'react';
import { Alert, Form, Input, Modal, Select, Typography, message } from 'antd';
import { useTranslation } from 'react-i18next';
import { createDataSource } from '@/api/dataSource';
import type { DataSourceInstance } from '@/types/dataSource';
import { buildEtlConfig } from './etlModel';

interface Props {
  open: boolean;
  kbId: string;
  /** 可选目标（仅本 KB 的 database 源，由 EtlTab 传入） */
  databaseSources: DataSourceInstance[];
  /** 固定目标数据源（从数据源卡片进入时用）：隐藏选择器并预填。 */
  fixedDataSourceId?: string;
  onClose: () => void;
  onCreated: (ds: DataSourceInstance) => void;
}

interface FormValues {
  name: string;
  etl_kind: 'file' | 'openlineage' | 'openmetadata' | 'datahub';
  data_source_id: string;
  /** 仅 OpenLineage 用；留空 = 不限制 namespace */
  namespace?: string;
}

export default function EtlSourceModal({ open, kbId, databaseSources,
                                          fixedDataSourceId, onClose,
                                          onCreated }: Props) {
  const { t } = useTranslation();
  const [form] = Form.useForm<FormValues>();
  /** 命名空间只对 OpenLineage 有意义：跟着类型联动显隐 */
  const etlKind = Form.useWatch('etl_kind', form);
  const [submitting, setSubmitting] = useState(false);
  /** 创建成功、一次性 key 尚未确认时的行（null = 还没建成） */
  const [created, setCreated] = useState<DataSourceInstance | null>(null);

  useEffect(() => {
    if (!open) return;
    setCreated(null);
    form.resetFields();
  }, [open, form]);

  const submit = async () => {
    let v: FormValues;
    try {
      v = await form.validateFields();
    } catch {
      return; // 表单自身已高亮错误
    }
    if (fixedDataSourceId) v.data_source_id = fixedDataSourceId;
    setSubmitting(true);
    try {
      const ds = await createDataSource({
        knowledge_base_id: kbId,
        access_type: 'etl',
        name: v.name,
        config_json: buildEtlConfig(v),
      });
      if (ds.ingestKey) {
        setCreated(ds); // 一次性 key：留在弹窗里让用户复制
      } else {
        message.success(t('domainKnowledge.etl.createSuccess'));
        onCreated(ds);
        onClose();
      }
    } catch (err: any) {
      // 原样透出后端 message（目标不是 database / 跨 KB / 参数错误都在里面）
      message.error(err?.message || t('domainKnowledge.etl.createFailed'));
    } finally {
      setSubmitting(false);
    }
  };

  const handleOk = () => {
    if (created) {
      onCreated(created);
      onClose();
    } else {
      void submit();
    }
  };

  /**
   * 关闭弹窗（取消 / 右上角 X）。
   *
   * `created` 非空说明来源已经建成、一次性 key 已经显示过：直接关掉会让列表停在
   * 旧数据（新来源不出现）。所以必须先 `onCreated` 让列表刷新；key 本身仍会随
   * 弹窗关闭而消失（只回一次），但至少来源不是「悄悄建成、列表里看不到」。
   * 成功态下取消按钮隐藏，只留「完成」，避免用户把「取消」理解成「没创建」。
   */
  const handleCancel = () => {
    if (created) onCreated(created);
    onClose();
  };

  const copyTooltips: [string, string] = [
    t('domainKnowledge.etl.copy'),
    t('domainKnowledge.etl.copied'),
  ];

  return (
    <Modal
      title={t('domainKnowledge.etl.addSource')}
      open={open}
      onCancel={handleCancel}
      onOk={handleOk}
      okText={created ? t('domainKnowledge.etl.done') : t('domainKnowledge.etl.create')}
      cancelText={t('domainKnowledge.etl.cancel')}
      cancelButtonProps={{ style: created ? { display: 'none' } : undefined }}
      confirmLoading={submitting}
      width={560}
      destroyOnHidden
    >
      {!created && (
        <Form form={form} layout="vertical"
              initialValues={{ etl_kind: 'file',
                               ...(fixedDataSourceId
                                 ? { data_source_id: fixedDataSourceId } : {}) }}>
          <Form.Item
            label={t('domainKnowledge.etl.name')}
            name="name"
            rules={[{ required: true, whitespace: true, message: t('domainKnowledge.etl.nameRequired') }]}
          >
            <Input placeholder={t('domainKnowledge.etl.namePlaceholder')} />
          </Form.Item>

          <Form.Item
            label={t('domainKnowledge.etl.kind')}
            name="etl_kind"
            rules={[{ required: true, message: t('domainKnowledge.etl.kindRequired') }]}
          >
            <Select
              options={[
                { value: 'file', label: t('domainKnowledge.etl.kindFile') },
                { value: 'openlineage', label: t('domainKnowledge.etl.kindOpenlineage') },
                { value: 'openmetadata', label: t('domainKnowledge.etl.kindOpenmetadata') },
                { value: 'datahub', label: t('domainKnowledge.etl.kindDatahub') },
              ]}
            />
          </Form.Item>

          {(etlKind === 'openmetadata' || etlKind === 'datahub') && (
            <Alert
              type="info"
              showIcon
              style={{ marginBottom: 16 }}
              title={t(etlKind === 'openmetadata'
                ? 'domainKnowledge.etl.pushHintOpenmetadata'
                : 'domainKnowledge.etl.pushHintDatahub')}
            />
          )}

          {etlKind === 'openlineage' && (
            <Form.Item
              label={t('domainKnowledge.etl.namespace')}
              name="namespace"
              extra={t('domainKnowledge.etl.namespaceHint')}
            >
              <Input
                placeholder={t('domainKnowledge.etl.namespacePlaceholder')}
                allowClear
              />
            </Form.Item>
          )}

          {!fixedDataSourceId && (
            <Form.Item
              label={t('domainKnowledge.etl.targetSource')}
              name="data_source_id"
              rules={[{ required: true, message: t('domainKnowledge.etl.targetRequired') }]}
              extra={t('domainKnowledge.etl.targetHint')}
            >
              <Select
                placeholder={t('domainKnowledge.etl.targetSourcePlaceholder')}
                options={databaseSources.map((ds) => ({ value: ds.id, label: ds.name }))}
                showSearch
                optionFilterProp="label"
                notFoundContent={t('domainKnowledge.etl.noDatabaseSource')}
              />
            </Form.Item>
          )}
        </Form>
      )}

      {created && (
        <Alert
          type="success"
          showIcon
          title={t('domainKnowledge.etl.createdTitle')}
          description={
            <div>
              <p style={{ margin: '0 0 8px' }}>{t('domainKnowledge.etl.keyOnce')}</p>
              <div style={{ marginBottom: 8 }}>
                {t('domainKnowledge.etl.ingestUrlLabel')}
                <Typography.Text copyable={{ tooltips: copyTooltips }}>{created.ingestUrl}</Typography.Text>
              </div>
              <div>
                {t('domainKnowledge.etl.ingestKeyLabel')}
                <Typography.Text copyable={{ text: created.ingestKey, tooltips: copyTooltips }} code>
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
