/**
 * 「导入 TBox」弹窗（抽象编辑器工具栏入口；spec §4.2）。
 *
 * 流程：粘贴/选择 YAML → 预览（dry_run：只算 diff 不写库，前端确认前不落库）
 * → 确认导入（乐观锁）→ 成功回调（页面重新 GET 上屏）。冲突（1003/409）提示重试。
 * 文本一变，预览作废（避免「改完文本、点确认带的却是旧预览的认知」——确认与预览
 * 始终绑定同一份文本，因为提交的就是当前 TextArea 内容）。
 * 确认导入前经 `confirmDirty` 守卫：成功回调会让页面重新 GET 上屏、覆盖本地状态，
 * 画布/指标的未保存编辑会随之丢失——页面借此做「未保存」确认（同 `constructDirtyConfirm`）。
 */
import { useRef, useState, type ReactNode } from 'react';
import { Alert, Button, Input, Modal, Space, Typography, message } from 'antd';
import { useTranslation } from 'react-i18next';

import { importAbstractSchema, type ImportTboxResult } from '@/api/abstractSchema';

const { Text } = Typography;

interface Props {
  kbId: string;
  /** 乐观锁版本；**首次导入传 0**（后端 upsert 对「行不存在」走 INSERT、不看该值）。 */
  expectedSchemaVersion: number;
  open: boolean;
  onClose: () => void;
  /** 导入成功 → 页面重新 GET 上屏。 */
  onImported: () => void;
  /**
   * 发起正式导入前的守卫；返回 false = 放弃本次导入。
   * 页面用它做「未保存编辑」确认（导入完成后的重新上屏会盖掉本地编辑
   * ——与 `constructDirtyConfirm` 同一课）。缺省 = 不设防。
   */
  confirmDirty?: () => boolean;
}

export function ImportTboxModal({ kbId, expectedSchemaVersion, open, onClose,
                                  onImported, confirmDirty }: Props): ReactNode {
  const { t } = useTranslation();
  const [yamlText, setYamlText] = useState('');
  const [preview, setPreview] = useState<ImportTboxResult>();
  const [loading, setLoading] = useState(false);
  const [importing, setImporting] = useState(false);
  const fileRef = useRef<HTMLInputElement | null>(null);

  const doPreview = async () => {
    if (!yamlText.trim()) {
      message.warning(t('abstractEditor.importEmptyYaml'));
      return;
    }
    setLoading(true);
    try {
      const result = await importAbstractSchema(kbId, {
        yamlText, expectedSchemaVersion, dryRun: true,
      });
      setPreview(result);
    } catch (e) {
      message.error(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  };

  const doImport = async () => {
    if (!preview) {
      message.warning(t('abstractEditor.importNeedPreview'));
      return;
    }
    if (confirmDirty && !confirmDirty()) return;
    setImporting(true);
    try {
      await importAbstractSchema(kbId, { yamlText, expectedSchemaVersion, dryRun: false });
      message.success(t('abstractEditor.importDone'));
      setYamlText('');
      setPreview(undefined);
      onImported();
      onClose();
    } catch (e) {
      const text = e instanceof Error ? e.message : String(e);
      if (text.includes('版本') || text.includes('已被') || text.includes('CONFLICT')
          || text.includes('409')) {
        message.error(t('abstractEditor.importConflict'));
      } else {
        message.error(text);
      }
    } finally {
      setImporting(false);
    }
  };

  const onPickFile = async (event: React.ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0];
    event.target.value = '';   // 允许重选同一文件
    if (!file) return;
    setYamlText(await file.text());
    setPreview(undefined);     // 文本变了 → 预览作废
  };

  return (
    <Modal
      open={open}
      title={t('abstractEditor.importTitle')}
      width={720}
      onCancel={onClose}
      destroyOnHidden
      footer={
        <Space>
          <Button onClick={onClose}>{t('common.cancel')}</Button>
          <Button onClick={() => void doPreview()} loading={loading} disabled={importing}>
            {t('abstractEditor.importPreview')}
          </Button>
          <Button type="primary" onClick={() => void doImport()}
            disabled={!preview || importing} loading={importing}>
            {t('abstractEditor.importConfirm')}
          </Button>
        </Space>
      }
    >
      <p>{t('abstractEditor.importHint')}</p>
      <Space style={{ marginBottom: 8 }}>
        <Button size="small" onClick={() => fileRef.current?.click()}>
          {t('abstractEditor.importPickFile')}
        </Button>
        <input ref={fileRef} type="file" accept=".yaml,.yml,.txt" style={{ display: 'none' }}
          onChange={(e) => void onPickFile(e)} />
      </Space>
      <Input.TextArea
        rows={10}
        value={yamlText}
        placeholder={t('abstractEditor.importYamlPlaceholder')}
        onChange={(e) => { setYamlText(e.target.value); setPreview(undefined); }}
      />
      {preview ? (
        <Alert
          style={{ marginTop: 12 }}
          type={preview.warnings.length ? 'warning' : 'success'}
          showIcon
          message={t('abstractEditor.importSummary', {
            added: preview.summary.entitiesAdded,
            updated: preview.summary.entitiesUpdated,
            attrs: preview.summary.attributesAdded,
          })}
          description={
            <div>
              {preview.summary.ignoredSections.length ? (
                <p>{t('abstractEditor.importIgnored',
                      { sections: preview.summary.ignoredSections.join('、') })}</p>
              ) : null}
              {preview.warnings.length ? (
                <div>
                  <Text type="secondary">{t('abstractEditor.importWarnings')}：</Text>
                  <ul>{preview.warnings.map((w) => <li key={w}>{w}</li>)}</ul>
                </div>
              ) : null}
            </div>
          }
        />
      ) : null}
    </Modal>
  );
}

export default ImportTboxModal;
