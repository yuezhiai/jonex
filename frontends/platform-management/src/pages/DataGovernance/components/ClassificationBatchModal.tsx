import { useEffect, useState } from 'react';
import { Alert, Form, Modal, Radio, Select, Space, message } from 'antd';
import { useTranslation } from 'react-i18next';
import { batchUpdateClassification } from '@/api/dataGovernance';
import type { DgAssetType, DgClassificationConfig } from '../types';
import {
  actionErrorMessage,
  batchDraftError,
  buildBatchPayload,
  buildCategoryOptions,
  buildLevelOptions,
} from '../utils';
import type { BatchDraft } from '../utils';

interface Props {
  open: boolean;
  assets: Array<{ assetType: DgAssetType; assetId: string }>;
  config: DgClassificationConfig;
  onClose: () => void;
  onSaved: () => void;
}

const EMPTY_DRAFT: BatchDraft = {
  levelMode: 'keep',
  level: null,
  categoriesMode: 'keep',
  categories: [],
};

/**
 * 批量分级弹窗：对勾选资产同时设置等级与分类。
 *
 * 两级各自独立选择「不修改 / 继承 / 设置」；分类另有「无分类」（显式空数组）。
 * `keep` 不发键（保持原值），`inherit` 发显式 `null`（清除资产级覆盖）。
 */
export default function ClassificationBatchModal({
  open,
  assets,
  config,
  onClose,
  onSaved,
}: Props) {
  const { t } = useTranslation();
  const [draft, setDraft] = useState<BatchDraft>(EMPTY_DRAFT);
  const [saving, setSaving] = useState(false);

  // 每次打开重置，避免上次的未提交选择残留到新一批资产。
  useEffect(() => {
    if (!open) return;
    setDraft(EMPTY_DRAFT);
    setSaving(false);
  }, [open]);

  const submit = async () => {
    if (saving) return;
    if (assets.length === 0) {
      message.info(t('dataGovernance.classification.emptySelection'));
      return;
    }
    const errorKey = batchDraftError(draft);
    if (errorKey) {
      message.error(t(errorKey));
      return;
    }
    setSaving(true);
    try {
      const result = await batchUpdateClassification(buildBatchPayload(assets, draft));
      message.success(
        t('dataGovernance.classification.batchSaved', { count: result.updated }),
      );
      onSaved();
      onClose();
    } catch (err: unknown) {
      // 失败保留弹窗与已选项，用户修正后可直接重试。
      const text = actionErrorMessage(err, {
        forbiddenKey: 'dataGovernance.drawer.saveForbidden',
        fallbackKey: 'dataGovernance.classification.saveFailed',
      });
      message.error('key' in text ? t(text.key) : text.message);
    } finally {
      setSaving(false);
    }
  };

  // 与 utils helper 共用：内置级别文案、停用项 disabled 口径一致。
  const levelOptions = buildLevelOptions(t, config.levels);
  const categoryOptions = buildCategoryOptions(config.categories, t);

  // keep/keep = 空补丁；按钮先禁用，提交前再由 batchDraftError 兜底。
  const hasChange = draft.levelMode !== 'keep' || draft.categoriesMode !== 'keep';

  return (
    <Modal
      open={open}
      title={t('dataGovernance.classification.batchTitle', { count: assets.length })}
      onCancel={() => {
        if (!saving) onClose();
      }}
      closable={!saving}
      maskClosable={false}
      keyboard={!saving}
      cancelButtonProps={{ disabled: saving }}
      onOk={() => void submit()}
      okText={t('common.save')}
      cancelText={t('common.cancel')}
      confirmLoading={saving}
      okButtonProps={{ disabled: assets.length === 0 || !hasChange }}
      destroyOnHidden
      width={560}
    >
      <Space direction="vertical" size={16} style={{ width: '100%' }}>
        {assets.length === 0 && (
          <Alert
            type="warning"
            showIcon
            title={t('dataGovernance.classification.emptySelection')}
          />
        )}

        <Form layout="vertical">
          <Form.Item label={t('dataGovernance.classification.level')}>
            <Radio.Group
              value={draft.levelMode}
              disabled={saving}
              onChange={(event) =>
                setDraft((current) => ({
                  ...current,
                  levelMode: event.target.value as BatchDraft['levelMode'],
                  // 切回非 set 模式时清掉选择，避免隐藏字段混入 payload。
                  level: event.target.value === 'set' ? current.level : null,
                }))
              }
            >
              <Radio value="keep">{t('dataGovernance.classification.keep')}</Radio>
              <Radio value="inherit">{t('dataGovernance.classification.inherit')}</Radio>
              <Radio value="set">{t('dataGovernance.classification.set')}</Radio>
            </Radio.Group>
            {draft.levelMode === 'set' && (
              <Select
                disabled={saving}
                value={draft.level ?? undefined}
                placeholder={t('dataGovernance.classification.levelRequired')}
                options={levelOptions}
                onChange={(value) =>
                  setDraft((current) => ({ ...current, level: value ?? null }))
                }
                style={{ width: '100%', marginTop: 8 }}
              />
            )}
          </Form.Item>

          <Form.Item label={t('dataGovernance.classification.categories')}>
            <Radio.Group
              value={draft.categoriesMode}
              disabled={saving}
              onChange={(event) =>
                setDraft((current) => ({
                  ...current,
                  categoriesMode: event.target.value as BatchDraft['categoriesMode'],
                }))
              }
            >
              <Radio value="keep">{t('dataGovernance.classification.keep')}</Radio>
              <Radio value="inherit">{t('dataGovernance.classification.inherit')}</Radio>
              <Radio value="empty">{t('dataGovernance.classification.none')}</Radio>
              <Radio value="set">{t('dataGovernance.classification.set')}</Radio>
            </Radio.Group>
            {draft.categoriesMode === 'set' && (
              <Select
                mode="multiple"
                disabled={saving}
                value={draft.categories}
                placeholder={t('dataGovernance.classification.categoryRequired')}
                options={categoryOptions}
                onChange={(value: string[]) =>
                  setDraft((current) => ({ ...current, categories: value ?? [] }))
                }
                style={{ width: '100%', marginTop: 8 }}
              />
            )}
          </Form.Item>
        </Form>

        <div className="yx-dg-muted">{t('dataGovernance.classification.inheritHint')}</div>
      </Space>
    </Modal>
  );
}
