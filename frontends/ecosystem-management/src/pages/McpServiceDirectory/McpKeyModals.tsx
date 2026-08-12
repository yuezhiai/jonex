import { forwardRef, useImperativeHandle, useState } from 'react';
import { Modal, Form, Input, Select, Alert, Button, Space, Typography, message } from 'antd';
import { useTranslation } from 'react-i18next';
import {
  createMcpKey,
  updateMcpKey,
  resetMcpKey,
  type McpKeyItem,
  type McpKeyCreateResult,
  type McpKeyPermission,
} from '@/api/mcpKeys';

const PERMISSION_VALUES: McpKeyPermission[] = ['view', 'call', 'write'];

/** 权限文案（多分支用 switch 提升可读性） */
const permissionLabel = (v: McpKeyPermission, t: (key: string) => string) => {
  switch (v) {
    case 'view':
      return t('mcpKeyManagement.permissionView');
    case 'call':
      return t('mcpKeyManagement.permissionCall');
    default:
      return t('mcpKeyManagement.permissionWrite');
  }
};

interface McpKeyFormValues {
  name?: string;
  permissions?: McpKeyPermission[];
  allowed_kb_ids?: string[];
  service_ids?: string[];
}

/** 创建 / 启用成功后的一次性明文展示块 */
function PlaintextBlock({ result }: { result: McpKeyCreateResult }) {
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
        <strong>{result.key_prefix ? `yxm_${result.key_prefix}` : '-'}</strong>
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

/** 权限 + KB 范围 + 领域服务范围 表单字段（创建 / 编辑共用） */
function KeyFormFields({
  form,
  knowledgeBaseOptions,
  serviceOptions,
}: {
  form: ReturnType<typeof Form.useForm<McpKeyFormValues>>[0];
  knowledgeBaseOptions?: { label: string; value: string }[];
  serviceOptions?: { label: string; value: string }[];
}) {
  const { t } = useTranslation();
  return (
    <Form form={form} layout="vertical" style={{ marginTop: 8 }}>
      <Form.Item name="name" label={t('mcpKeyManagement.nameLabel')}>
        <Input placeholder={t('mcpKeyManagement.namePlaceholder')} maxLength={255} />
      </Form.Item>
      <Form.Item
        name="permissions"
        label={t('mcpKeyManagement.permissionLabel')}
        rules={[{ required: true, message: t('mcpKeyManagement.permissionLabel') }]}
      >
        <Select
          mode="multiple"
          placeholder={t('mcpKeyManagement.permissionLabel')}
          options={PERMISSION_VALUES.map((v) => ({ value: v, label: permissionLabel(v, t) }))}
        />
      </Form.Item>
      <Form.Item name="allowed_kb_ids" label={t('mcpKeyManagement.kbRangeLabel')} extra={t('mcpKeyManagement.kbRangeHint')}>
        <Select
          mode="multiple"
          placeholder={t('mcpKeyManagement.kbRangeHint')}
          options={knowledgeBaseOptions}
          filterOption={(input, option) => (option?.label as string)?.toLowerCase().includes(input.toLowerCase())}
        />
      </Form.Item>
      <Form.Item name="service_ids" label={t('mcpKeyManagement.serviceScopeLabel')} extra={t('mcpKeyManagement.serviceScopeHint')}>
        <Select
          mode="multiple"
          placeholder={t('mcpKeyManagement.serviceScopeHint')}
          options={serviceOptions}
          filterOption={(input, option) => (option?.label as string)?.toLowerCase().includes(input.toLowerCase())}
        />
      </Form.Item>
    </Form>
  );
}

/**
 * MCP Key 弹窗集：创建 / 编辑 / 启用（重置）
 *
 * 通过 ref 暴露 openCreate / openEdit / openEnable 方法；
 * 任一操作成功后触发 onSuccess（如刷新列表）。
 */
export type McpKeyModalsHandle = {
  openCreate: () => void;
  openEdit: (record: McpKeyItem) => void;
  openEnable: (record: McpKeyItem) => void;
};

export interface McpKeyModalsProps {
  /** 任一操作成功后的回调（如刷新列表），由调用方实现 */
  onSuccess?: () => void;
  /** 知识库范围下拉选项（可选，由调用方加载后传入） */
  knowledgeBaseOptions?: { label: string; value: string }[];
  /** 领域服务范围下拉选项（可选，由调用方加载后传入） */
  serviceOptions?: { label: string; value: string }[];
}

export const McpKeyModals = forwardRef<McpKeyModalsHandle, McpKeyModalsProps>(
  ({ onSuccess, knowledgeBaseOptions, serviceOptions }, ref) => {
    const { t } = useTranslation();

    // ── 创建 ──
    const [createOpen, setCreateOpen] = useState(false);
    const [createSaving, setCreateSaving] = useState(false);
    const [createResult, setCreateResult] = useState<McpKeyCreateResult | null>(null);
    const [createForm] = Form.useForm<McpKeyFormValues>();
    // ── 编辑 ──
    const [editOpen, setEditOpen] = useState(false);
    const [editSaving, setEditSaving] = useState(false);
    const [editTarget, setEditTarget] = useState<McpKeyItem | null>(null);
    const [editForm] = Form.useForm<McpKeyFormValues>();
    // ── 启用（重置） ──
    const [enableOpen, setEnableOpen] = useState(false);
    const [enableSaving, setEnableSaving] = useState(false);
    const [enableTarget, setEnableTarget] = useState<McpKeyItem | null>(null);
    const [enableResult, setEnableResult] = useState<McpKeyCreateResult | null>(null);

    useImperativeHandle(
      ref,
      () => ({
        openCreate: () => {
          createForm.resetFields();
          setCreateResult(null);
          setCreateOpen(true);
        },
        openEdit: (record: McpKeyItem) => {
          setEditTarget(record);
          editForm.setFieldsValue({
            name: record.name || '',
            permissions: record.permissions?.length ? record.permissions : ['view'],
            allowed_kb_ids: record.allowed_kb_ids || [],
            service_ids: record.service_ids || [],
          });
          setEditOpen(true);
        },
        openEnable: (record: McpKeyItem) => {
          setEnableTarget(record);
          setEnableResult(null);
          setEnableOpen(true);
        },
      }),
      [createForm, editForm],
    );

    // ── 创建 ──
    const handleCreate = async () => {
      try {
        const values = await createForm.validateFields();
        setCreateSaving(true);
        const res = await createMcpKey({
          name: values.name?.trim() || undefined,
          permissions: values.permissions?.length ? values.permissions : ['view'],
          allowed_kb_ids: values.allowed_kb_ids || [],
          service_ids: values.service_ids || [],
        });
        setCreateResult(res);
      } catch (e) {
        if (e && typeof e === 'object' && 'errorFields' in e) return;
        message.error(t('mcpKeyManagement.operationFailed'));
      } finally {
        setCreateSaving(false);
      }
    };
    const closeCreate = (refresh = false) => {
      setCreateOpen(false);
      setCreateResult(null);
      if (refresh) onSuccess?.();
    };

    // ── 编辑 ──
    const handleEdit = async () => {
      if (!editTarget) return;
      try {
        const values = await editForm.validateFields();
        setEditSaving(true);
        await updateMcpKey(editTarget.id, {
          name: values.name?.trim() || undefined,
          permissions: values.permissions?.length ? values.permissions : ['view'],
          allowed_kb_ids: values.allowed_kb_ids || [],
          service_ids: values.service_ids || [],
        });
        message.success(t('mcpKeyManagement.editSuccess'));
        setEditOpen(false);
        setEditTarget(null);
        onSuccess?.();
      } catch (e) {
        if (e && typeof e === 'object' && 'errorFields' in e) return;
        message.error(t('mcpKeyManagement.operationFailed'));
      } finally {
        setEditSaving(false);
      }
    };

    // ── 启用（重置） ──
    const handleEnable = async () => {
      if (!enableTarget) return;
      setEnableSaving(true);
      try {
        const res = await resetMcpKey(enableTarget.id);
        setEnableResult(res);
        message.success(t('mcpKeyManagement.enableSuccess'));
      } catch (err: unknown) {
        message.error(err instanceof Error ? err.message : t('mcpKeyManagement.operationFailed'));
      } finally {
        setEnableSaving(false);
      }
    };
    const closeEnable = (refresh = false) => {
      setEnableOpen(false);
      setEnableTarget(null);
      setEnableResult(null);
      if (refresh) onSuccess?.();
    };

    return (
      <>
        {/* 创建 */}
        <Modal
          title={createResult ? t('mcpKeyManagement.plaintextTitle') : t('mcpKeyManagement.createTitle')}
          open={createOpen}
          onCancel={() => closeCreate(!!createResult)}
          footer={
            createResult ? (
              <Button type="primary" onClick={() => closeCreate(true)}>
                {t('common.confirm')}
              </Button>
            ) : (
              <Space>
                <Button onClick={() => closeCreate(false)}>{t('common.cancel')}</Button>
                <Button type="primary" loading={createSaving} onClick={handleCreate}>
                  {t('common.save')}
                </Button>
              </Space>
            )
          }
          width={520}
          maskClosable={false}
          destroyOnHidden
        >
          {createResult ? (
            <PlaintextBlock result={createResult} />
          ) : (
            <KeyFormFields
              form={createForm}
              knowledgeBaseOptions={knowledgeBaseOptions}
              serviceOptions={serviceOptions}
            />
          )}
        </Modal>

        {/* 编辑 */}
        <Modal
          title={t('mcpKeyManagement.editTitle')}
          open={editOpen}
          onCancel={() => {
            setEditOpen(false);
            setEditTarget(null);
          }}
          onOk={handleEdit}
          confirmLoading={editSaving}
          okText={t('common.save')}
          cancelText={t('common.cancel')}
          width={520}
          maskClosable={false}
          destroyOnHidden
        >
          <KeyFormFields
            form={editForm}
            knowledgeBaseOptions={knowledgeBaseOptions}
            serviceOptions={serviceOptions}
          />
        </Modal>

        {/* 启用（重置） */}
        <Modal
          title={enableResult ? t('mcpKeyManagement.plaintextTitle') : t('mcpKeyManagement.enableTitle')}
          open={enableOpen}
          onCancel={() => closeEnable(!!enableResult)}
          footer={
            enableResult ? (
              <Button type="primary" onClick={() => closeEnable(true)}>
                {t('common.confirm')}
              </Button>
            ) : (
              <Space>
                <Button onClick={() => closeEnable(false)}>{t('common.cancel')}</Button>
                <Button type="primary" loading={enableSaving} onClick={handleEnable}>
                  {t('mcpKeyManagement.enableBtn')}
                </Button>
              </Space>
            )
          }
          width={520}
          maskClosable={false}
          destroyOnHidden
        >
          {enableResult ? (
            <PlaintextBlock result={enableResult} />
          ) : (
            <div style={{ textAlign: 'center', padding: '12px 0' }}>
              <p style={{ fontSize: 14, color: '#64748b', marginBottom: 8 }}>
                {t('mcpKeyManagement.enableConfirm', { name: enableTarget?.name || enableTarget?.key_prefix || '-' })}
              </p>
              <p style={{ fontSize: 15, fontWeight: 600, margin: 0 }}>
                {enableTarget?.key_prefix ? `yxm_${enableTarget.key_prefix}` : '-'}
              </p>
            </div>
          )}
        </Modal>
      </>
    );
  },
);
McpKeyModals.displayName = 'McpKeyModals';
