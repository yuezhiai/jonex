import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useParams } from 'react-router-dom';
import { Alert, Button, Empty, Input, Modal, Skeleton, Tag, message } from 'antd';
import { ReloadOutlined, SaveOutlined } from '@ant-design/icons';
import {
  getModelSettings,
  saveModelSettings,
  type KbModelSettingItem,
  type KbModelSettingPayloadItem,
  type KbModelSettingsView,
} from '@/api/domainKnowledge';
import './index.css';

/**
 * 知识库模型配置。
 *
 * 只覆盖**解析**与**编译**两个阶段的模型（本页不碰查询侧）：后端按 KB 类型决定哪几行可见，
 * 前端不再自己判断类型 —— 唯一例外是"空值 = 继承"的输入框语义，这里也只做展示。
 *
 * 语义（见 docs/knowledge-base/kb-model-settings-design.md）：
 * - 留空 = 清除该 scope 的覆盖，恢复继承引擎默认；
 * - 保存只影响**尚未派发**的步骤，正在跑的解析/编译不变；
 * - 无覆盖时后端返回 effective=null（平台不编造引擎侧默认值）。
 */
interface ModelSettingsContentProps {
  /** 嵌在 KB 详情 Tab 里时由父组件传入；独立路由时从 URL 取 */
  kbId?: string;
  /** false = 只读（无管理权限时输入框禁用，仍可看当前生效值） */
  canWrite?: boolean;
}

export function ModelSettingsContent({ kbId, canWrite = true }: ModelSettingsContentProps) {
  const { t } = useTranslation();
  const { id: routeKbId = '' } = useParams<{ id: string }>();
  const knowledgeBaseId = kbId || routeKbId;

  const [view, setView] = useState<KbModelSettingsView | null>(null);
  const [draft, setDraft] = useState<Record<string, string>>({});
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');

  const labelOf = useCallback((item: KbModelSettingItem) => t(item.label_key, { defaultValue: item.label }), [t]);

  const load = useCallback(async () => {
    if (!knowledgeBaseId) {
      setError(t('kbModel.missingKbId'));
      return;
    }
    setLoading(true);
    setError('');
    try {
      const data = await getModelSettings(knowledgeBaseId);
      setView(data);
      setDraft(
        Object.fromEntries(
          data.items.filter((item) => item.configurable).map((item) => [item.scope, item.kb_value || '']),
        ),
      );
    } catch (err) {
      setError((err as Error)?.message || t('kbModel.loadError'));
    } finally {
      setLoading(false);
    }
  }, [knowledgeBaseId, t]);

  useEffect(() => {
    void load();
  }, [load]);

  const dirtyItems = useMemo<KbModelSettingPayloadItem[]>(() => {
    if (!view) return [];
    return view.items
      .filter((item) => item.configurable)
      .map((item) => ({
        scope: item.scope,
        model_name: (draft[item.scope] ?? '').trim(),
      }))
      .filter((item, index) => {
        const origin = view.items.filter((entry) => entry.configurable)[index];
        return item.model_name !== (origin?.kb_value || '');
      });
  }, [view, draft]);

  const submit = useCallback(async () => {
    setSaving(true);
    try {
      const data = await saveModelSettings(knowledgeBaseId, dirtyItems);
      setView(data);
      setDraft(
        Object.fromEntries(
          data.items.filter((item) => item.configurable).map((item) => [item.scope, item.kb_value || '']),
        ),
      );
      // [jonex] P3b（方案 B）：保存只改平台表 —— 不向引擎推送，也不在这里排任务。
      // 新模型在**下一次**编译/解析发起时随任务下发给引擎（保存前的成本提示已说明）。
      message.success(t('kbModel.saved'));
    } catch (err) {
      message.error((err as Error)?.message || t('kbModel.saveError'));
    } finally {
      setSaving(false);
    }
  }, [view, dirtyItems, knowledgeBaseId, t]);

  const onSave = useCallback(() => {
    if (!view || dirtyItems.length === 0) return;
    // [jonex] P3b：改/清 Wiki 编译模型会整库重放，先讲清成本（设置与清空都要提示）。
    const wikiItem = dirtyItems.find((item) => item.scope === 'compile_wiki');
    if (wikiItem) {
      Modal.confirm({
        title: t('kbModel.compileCostTitle'),
        content: wikiItem.model_name ? t('kbModel.compileCostContent') : t('kbModel.clearCostContent'),
        okText: t('kbModel.save'),
        cancelText: t('common.cancel'),
        onOk: () => submit(),
      });
      return;
    }
    void submit();
  }, [view, dirtyItems, submit, t]);

  const renderValue = (item: KbModelSettingItem) => {
    if (!item.configurable) {
      return (
        <div className="kb-model-readonly">
          <span className="kb-model-readonly-value">{item.effective || t('kbModel.unknown')}</span>
          <span className="kb-model-readonly-hint">{item.disabled_reason}</span>
        </div>
      );
    }
    return (
      <Input
        allowClear
        value={draft[item.scope] ?? ''}
        placeholder={t('kbModel.placeholder')}
        /* 只有权限能挡住编辑：`configurable=false` 的行在上面就走只读分支了 */
        disabled={!canWrite}
        onChange={(event) => setDraft((prev) => ({ ...prev, [item.scope]: event.target.value }))}
      />
    );
  };

  const renderStatus = (item: KbModelSettingItem) => {
    if (!item.configurable) {
      return <Tag>{t('kbModel.statusReadonly')}</Tag>;
    }
    if ((draft[item.scope] ?? '').trim()) {
      return <Tag color="blue">{t('kbModel.statusCustom')}</Tag>;
    }
    return <Tag>{t('kbModel.statusInherited')}</Tag>;
  };

  const stages = useMemo(() => {
    const items = view?.items || [];
    return [
      { key: 'parse', title: t('kbModel.stageParse'), items: items.filter((i) => i.stage === 'parse') },
      { key: 'compile', title: t('kbModel.stageCompile'), items: items.filter((i) => i.stage === 'compile') },
      { key: 'retrieve', title: t('kbModel.stageRetrieve'), items: items.filter((i) => i.stage === 'retrieve') },
    ].filter((group) => group.items.length > 0);
  }, [view, t]);

  if (loading && !view) {
    return (
      <div className="kb-model-page">
        <Skeleton active paragraph={{ rows: 6 }} />
      </div>
    );
  }

  return (
    <div className="kb-model-page">
      <Alert
        type="info"
        showIcon
        className="kb-model-notice"
        message={t('kbModel.title')}
        description={t('kbModel.subtitle')}
      />

      {error ? (
        <Alert
          type="error"
          showIcon
          message={error}
          action={
            <Button size="small" onClick={() => void load()}>
              {t('common.retry')}
            </Button>
          }
        />
      ) : null}

      {view && view.items.length === 0 ? <Empty description={t('kbModel.empty')} /> : null}

      {stages.map((group) => (
        <div className="kb-model-group" key={group.key}>
          <h3 className="kb-model-group-title">{group.title}</h3>
          <div className="kb-model-rows">
            {group.items.map((item) => (
              <div className="kb-model-row" key={item.scope}>
                <div className="kb-model-row-head">
                  <span className="kb-model-row-label">{labelOf(item)}</span>
                  {renderStatus(item)}
                </div>
                <div className="kb-model-row-body">{renderValue(item)}</div>
                <div className="kb-model-row-foot">
                  {item.kb_value ? (
                    <span>
                      {t('kbModel.effective')}: {item.kb_value}
                    </span>
                  ) : (
                    <span>
                      {t('kbModel.effective')}: {t('kbModel.engineDefault')}
                      {item.env_var
                        ? ` (${item.env_var}${item.engine_default ? ` = ${item.engine_default}` : ''})`
                        : ''}
                    </span>
                  )}
                </div>
              </div>
            ))}
          </div>
        </div>
      ))}

      <div className="kb-model-actions">
        <Button icon={<ReloadOutlined />} onClick={() => void load()} disabled={saving}>
          {t('kbModel.refresh')}
        </Button>
        <Button
          type="primary"
          icon={<SaveOutlined />}
          loading={saving}
          disabled={!canWrite || dirtyItems.length === 0}
          onClick={() => void onSave()}
        >
          {t('kbModel.save')}
        </Button>
      </div>
    </div>
  );
}

/** 独立路由 `domain-knowledge/:id/models` 的入口（深链/旧书签用）。 */
export default function DomainKnowledgeModelSettings() {
  return <ModelSettingsContent />;
}
