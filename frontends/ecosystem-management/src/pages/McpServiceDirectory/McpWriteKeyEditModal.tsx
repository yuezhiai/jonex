import { forwardRef, useImperativeHandle, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Modal, Form, Button, Space, Input, Radio, DatePicker, Alert, message, Typography } from 'antd';
import dayjs, { type Dayjs } from 'dayjs';
import {
  updateMcpWriteKey,
  type WriteGrant,
  type WriteKeyItem,
  type WriteKeyUpdatePayload,
} from '@/api/mcpWriteKeys';
import type { KnowledgeBaseBrief } from '@/api/spaces';
import WriteGrantsEditor from './WriteGrantsEditor';

/** 编辑模态框对外暴露的句柄 */
export type McpWriteKeyEditModalHandle = {
  openEdit: (record: WriteKeyItem) => void;
};

interface McpWriteKeyEditModalProps {
  /** 知识库列表（含 space_id），由调用方加载后传入 */
  kbList: KnowledgeBaseBrief[];
  /** 领域空间 ID → 名称 */
  spaceNameMap?: Record<string, string>;
  /** 保存成功后的回调（如刷新列表） */
  onSuccess?: () => void;
}

/** 有效期固定档位（月数 + i18n label key） */
const EXPIRY_OPTIONS = [
  { value: '3m', labelKey: 'expiry3m', months: 3 },
  { value: '6m', labelKey: 'expiry6m', months: 6 },
  { value: '12m', labelKey: 'expiry12m', months: 12 },
];

const calcExpiry = (type: string, customDate?: Dayjs): string | null => {
  if (type === 'custom') return customDate ? customDate.endOf('day').toISOString() : null;
  const opt = EXPIRY_OPTIONS.find((o) => o.value === type);
  return opt ? dayjs().add(opt.months, 'month').endOf('day').toISOString() : null;
};

interface FormValues {
  name?: string;
  expiry_edit?: string;
  expiry_custom?: Dayjs;
}

/** 编辑知识写入 Key 模态框：name / expires_at / grants（PUT 局部更新，全 Optional）。
 *
 * 有效期默认「保持当前」（后端 expires_at: null = 不改动，无法清空回永久，见 Phase 17 待确认 #2）。
 * grants 仅在与初始值不同时提交，避免无谓的后端重新校验。
 */
export const McpWriteKeyEditModal = forwardRef<McpWriteKeyEditModalHandle, McpWriteKeyEditModalProps>(
  ({ kbList, spaceNameMap, onSuccess }, ref) => {
    const { t } = useTranslation();
    const [open, setOpen] = useState(false);
    const [target, setTarget] = useState<WriteKeyItem | null>(null);
    const [saving, setSaving] = useState(false);
    const [grants, setGrants] = useState<WriteGrant[]>([]);
    const [form] = Form.useForm<FormValues>();
    // 打开时的初始 grants（用于判断是否修改）
    const initialGrantsRef = useRef<WriteGrant[]>([]);
    // 当前有效期（编辑「保持当前」展示用）
    const currentExpiry = useRef<string | null>(null);

    const expiryEdit = Form.useWatch('expiry_edit', form);
    const customDate = Form.useWatch('expiry_custom', form);

    useImperativeHandle(
      ref,
      () => ({
        openEdit(record: WriteKeyItem) {
          setTarget(record);
          initialGrantsRef.current = record.grants || [];
          setGrants(record.grants || []);
          currentExpiry.current = record.expires_at || null;
          form.setFieldsValue({
            name: record.name || '',
            expiry_edit: 'keep',
            expiry_custom: undefined,
          });
          setOpen(true);
        },
      }),
      [form],
    );

    /** grants 前端校验（对齐后端 2.2） */
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

    const handleSave = async () => {
      if (!target) return;
      let values: FormValues;
      try {
        values = await form.validateFields();
      } catch {
        return;
      }
      const error = validateGrants();
      if (error) {
        message.error(error);
        return;
      }
      const payload: WriteKeyUpdatePayload = {
        name: values.name?.trim() || '',
      };
      // 有效期：仅「保持当前」以外的档位才传新 expires_at（null = 不改动）
      if (values.expiry_edit && values.expiry_edit !== 'keep') {
        payload.expires_at = calcExpiry(values.expiry_edit, values.expiry_custom);
      }
      // grants：与初始值不同才提交（避免无谓重新校验）
      if (JSON.stringify(grants) !== JSON.stringify(initialGrantsRef.current)) {
        payload.grants = grants;
      }
      setSaving(true);
      try {
        await updateMcpWriteKey(target.id, payload);
        message.success(t('mcpWriteKeys.editSuccess'));
        setOpen(false);
        onSuccess?.();
      } catch (e) {
        if (e && typeof e === 'object' && 'errorFields' in e) return;
        message.error(t('mcpWriteKeys.operationFailed'));
      } finally {
        setSaving(false);
      }
    };

    // 「保持当前」label：当前永久 → 永久有效；当前有限期 → 至 YYYY-MM-DD
    const keepLabel = currentExpiry.current
      ? `${t('mcpWriteKeys.keepCurrent')}（${dayjs(currentExpiry.current).format('YYYY-MM-DD')}）`
      : `${t('mcpWriteKeys.keepCurrent')}（${t('mcpWriteKeys.expiryForever')}）`;

    return (
      <Modal
        title={t('mcpWriteKeys.editTitle')}
        open={open}
        onCancel={() => setOpen(false)}
        width={720}
        maskClosable={false}
        destroyOnHidden
        footer={
          <Space>
            <Button onClick={() => setOpen(false)}>{t('common.cancel')}</Button>
            <Button type="primary" loading={saving} onClick={handleSave}>
              {t('common.save')}
            </Button>
          </Space>
        }
      >
        {target && (
          <Form form={form} layout="vertical">
            <Form.Item
              name="name"
              label={t('mcpWriteKeys.nameLabel')}
              rules={[{ required: true, whitespace: true, message: t('mcpWriteKeys.nameRequired') }]}
            >
              <Input placeholder={t('mcpWriteKeys.namePlaceholder')} maxLength={255} />
            </Form.Item>

            <Form.Item name="expiry_edit" label={t('mcpWriteKeys.expiryLabel')} initialValue="keep">
              <Radio.Group>
                <Space direction="vertical">
                  <Radio value="keep">{keepLabel}</Radio>
                  {EXPIRY_OPTIONS.map((o) => (
                    <Radio key={o.value} value={o.value}>
                      {t(`mcpWriteKeys.${o.labelKey}`)}
                    </Radio>
                  ))}
                  <Radio value="custom">{t('mcpWriteKeys.expiryCustom')}</Radio>
                </Space>
              </Radio.Group>
            </Form.Item>
            {expiryEdit === 'custom' && (
              <Form.Item
                name="expiry_custom"
                label={t('mcpWriteKeys.expiryLabel')}
                rules={[{ required: true, message: t('mcpWriteKeys.customDateRequired') }]}
              >
                <DatePicker style={{ width: '100%' }} />
              </Form.Item>
            )}

            <div style={{ marginBottom: 4 }}>
              <Typography.Text strong>{t('mcpWriteKeys.grantsLabel')}</Typography.Text>
            </div>
            <WriteGrantsEditor value={grants} onChange={setGrants} kbList={kbList} spaceNameMap={spaceNameMap} />

            <Alert
              type="info"
              showIcon
              message={t('mcpWriteKeys.editHint')}
              style={{ marginTop: 16 }}
            />
          </Form>
        )}
      </Modal>
    );
  },
);
McpWriteKeyEditModal.displayName = 'McpWriteKeyEditModal';
