import React, { useCallback, useEffect, useRef, useState } from 'react';
import {
  Alert,
  Button,
  Descriptions,
  Divider,
  Drawer,
  Empty,
  Form,
  Input,
  InputNumber,
  message,
  Popconfirm,
  Select,
  Skeleton,
  Space,
  Tag,
} from 'antd';
import {
  DeleteOutlined,
  InboxOutlined,
  ReloadOutlined,
  UndoOutlined,
} from '@ant-design/icons';
import { useTranslation } from 'react-i18next';
import { getShellContext } from '@jonex/shell-sdk';
import { formatDateTime } from '@jonex/shared-lib';
import {
  archiveAsset,
  destroyAsset,
  getAsset,
  restoreAsset,
  setRetentionPolicy,
  updateAssetMetadata,
} from '@/api/dataGovernance';
import type { DgAssetDetail, DgAssetType, DgClassificationConfig } from '../types';
import {
  actionErrorMessage,
  buildCategoryOptions,
  buildLevelOptions,
  buildMetadataPatch,
  buildRetentionPatch,
  classificationOverrideDraft,
  classificationSourceLabelKey,
  errorMeta,
  formatFileSize,
  levelColor,
  levelLabel,
  qualityColor,
  qualityLabelKey,
  retentionScopeDraft,
  retentionSourceLabelKey,
  retentionValueText,
  statusColor,
  statusLabelKey,
} from '../utils';
import type { MetadataDraft, RetentionDraft } from '../utils';
import LineageSection from '../components/LineageSection';

interface Props {
  open: boolean;
  assetType: DgAssetType;
  assetId: string;
  classificationConfig: DgClassificationConfig;
  onClose: () => void;
  onSaved: () => void;
}

export default function AssetDrawer({
  open,
  assetType,
  assetId,
  classificationConfig,
  onClose,
  onSaved,
}: Props) {
  const { t } = useTranslation();
  // 异步回调里读 tRef，effect 依赖中就可以不挂 `t`：
  // 否则切换语言会让 detail 重新拉取，覆盖用户未保存的草稿。
  const tRef = useRef(t);
  useEffect(() => {
    tRef.current = t;
  }, [t]);

  const [detail, setDetail] = useState<DgAssetDetail | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [draft, setDraft] = useState<MetadataDraft>({
    owner: null,
    domain: null,
    sensitivity: null,
    categories: null,
  });
  const [saving, setSaving] = useState(false);
  const [retentionDraft, setRetentionDraft] = useState<RetentionDraft>({
    retentionDays: null,
    graceDays: null,
  });
  const [retentionSaving, setRetentionSaving] = useState(false);
  const [lifecycleAction, setLifecycleAction] = useState<
    'archive' | 'restore' | 'destroy' | null
  >(null);

  // 请求序号：只有最后一次发起的请求允许回写状态。
  const detailSeq = useRef(0);
  const saveSeq = useRef(0);
  const retentionSeq = useRef(0);
  const lifecycleSeq = useRef(0);
  const assetKeyRef = useRef(`${assetType}:${assetId}`);
  const assetKey = `${assetType}:${assetId}`;

  const applyDetail = useCallback((next: DgAssetDetail) => {
    setDetail(next);
    // 覆盖表单只回填「资产级」自己的值：来源是租户/KB 默认时留空表示继承。
    setDraft({
      owner: next.owner,
      domain: next.domain,
      ...classificationOverrideDraft(next),
    });
    // 文档覆盖表单只回填「文档级」自己的值，其余留空表示继承。
    setRetentionDraft(retentionScopeDraft(next.retentionPolicy, 'document'));
  }, []);

  // 切换资产 / 关闭抽屉：让在途请求全部失效，并清掉上一份详情，
  // 否则会有一帧渲染上一个资产的详情。血缘区由 key 重挂 LineageSection 归零。
  useEffect(() => {
    assetKeyRef.current = assetKey;
    detailSeq.current += 1;
    saveSeq.current += 1;
    retentionSeq.current += 1;
    lifecycleSeq.current += 1;
    setDetail(null);
    setDraft({ owner: null, domain: null, sensitivity: null, categories: null });
    setRetentionDraft({ retentionDays: null, graceDays: null });
    setRetentionSaving(false);
    setLifecycleAction(null);
    setError(null);
    setSaving(false);
    setLoading(Boolean(open && assetId));
  }, [open, assetKey, assetId]);

  /**
   * `silent=true` 用于动作后的局部刷新：不切全局 loading，
   * 避免已有详情整块变 Skeleton（闪烁 + 滚动位置丢失）。
   */
  const loadDetail = useCallback(
    async (silent = false) => {
      if (!open || !assetId) return;
      const my = ++detailSeq.current;
      if (!silent) setLoading(true);
      setError(null);
      try {
        const next = await getAsset(assetType, assetId);
        if (my !== detailSeq.current) return;
        applyDetail(next);
      } catch (err: unknown) {
        if (my !== detailSeq.current) return;
        const meta = errorMeta(err);
        setDetail(null);
        // 404 统一文案；拿到 ApiError（有 status）才允许透出后端 message；
        // 网络错误没有 status，回落到本地化文案，避免英文界面串中文。
        setError(
          meta.status === 404
            ? tRef.current('dataGovernance.drawer.notFound')
            : meta.status !== undefined && meta.message
              ? meta.message
              : tRef.current('dataGovernance.drawer.loadFailed'),
        );
      } finally {
        if (my === detailSeq.current && !silent) setLoading(false);
      }
    },
    [open, assetType, assetId, applyDetail],
  );

  useEffect(() => {
    void loadDetail();
  }, [loadDetail]);

  const save = async () => {
    if (!detail || saving) return;
    const originKey = `${assetType}:${assetId}`;
    const patch = buildMetadataPatch(
      {
        owner: detail.owner,
        domain: detail.domain,
        // original 必须用「资产级覆盖值」：生效值来自租户/KB 默认时，
        // 直接拿来比较会把未修改的继承值误判成「有值 → 清空」。
        ...classificationOverrideDraft(detail),
      },
      draft,
    );
    if (!Object.keys(patch).length) {
      message.info(tRef.current('dataGovernance.drawer.noChange'));
      return;
    }
    const my = ++saveSeq.current;
    setSaving(true);
    try {
      const next = await updateAssetMetadata(assetType, assetId, patch);
      // 服务端写入已完成：即使抽屉已关闭/切走，也要让列表刷新。
      onSaved();
      // 用户已在等待期间切走资产：丢弃 UI 回写，避免 A 的元数据写进 B 的界面。
      if (my !== saveSeq.current || assetKeyRef.current !== originKey) return;
      // 只回写元数据相关状态；保留策略草稿可能正在并行编辑，不能被整份详情覆盖。
      setDetail(next);
      setDraft({
        owner: next.owner,
        domain: next.domain,
        ...classificationOverrideDraft(next),
      });
      message.success(tRef.current('dataGovernance.drawer.saved'));
    } catch (err: unknown) {
      if (my !== saveSeq.current || assetKeyRef.current !== originKey) return;
      const text = actionErrorMessage(err, {
        forbiddenKey: 'dataGovernance.drawer.saveForbidden',
        fallbackKey: 'dataGovernance.drawer.saveFailed',
      });
      message.error('key' in text ? tRef.current(text.key) : text.message);
    } finally {
      if (my === saveSeq.current) setSaving(false);
    }
  };

  const saveRetention = async () => {
    if (!detail || retentionSaving) return;
    const originKey = `${detail.assetType}:${detail.assetId}`;
    const patch = buildRetentionPatch(
      retentionScopeDraft(detail.retentionPolicy, 'document'),
      retentionDraft,
    );
    if (!Object.keys(patch).length) {
      message.info(tRef.current('dataGovernance.drawer.noChange'));
      return;
    }
    const my = ++retentionSeq.current;
    setRetentionSaving(true);
    try {
      const next = await setRetentionPolicy('document', detail.assetId, patch);
      // 服务端写入已完成：即使抽屉已关闭/切走，也要让列表刷新。
      onSaved();
      if (my !== retentionSeq.current || assetKeyRef.current !== originKey) return;
      // 接口只回策略：合并进当前详情即可刷新展示，不必重拉整份详情；
      // 元数据草稿保持原样，不因策略保存被回填。
      setDetail((current) => (current ? { ...current, retentionPolicy: next } : current));
      setRetentionDraft(retentionScopeDraft(next, 'document'));
      message.success(tRef.current('dataGovernance.lifecycle.saved'));
    } catch (err: unknown) {
      if (my !== retentionSeq.current || assetKeyRef.current !== originKey) return;
      const text = actionErrorMessage(err, {
        forbiddenKey: 'dataGovernance.drawer.saveForbidden',
        fallbackKey: 'dataGovernance.drawer.saveFailed',
      });
      message.error('key' in text ? tRef.current(text.key) : text.message);
    } finally {
      if (my === retentionSeq.current) setRetentionSaving(false);
    }
  };

  const runLifecycle = async (action: 'archive' | 'restore' | 'destroy') => {
    if (!detail || lifecycleAction) return;
    const originKey = `${detail.assetType}:${detail.assetId}`;
    const request =
      action === 'archive' ? archiveAsset : action === 'restore' ? restoreAsset : destroyAsset;
    const my = ++lifecycleSeq.current;
    setLifecycleAction(action);
    try {
      await request(detail.assetType, detail.assetId, detail.kbId);
      // 服务端写入已完成：即使抽屉已关闭/切走，也要让列表刷新。
      onSaved();
      if (my !== lifecycleSeq.current || assetKeyRef.current !== originKey) return;
      const successKey =
        action === 'archive'
          ? 'dataGovernance.lifecycle.archiveSuccess'
          : action === 'restore'
            ? 'dataGovernance.lifecycle.restoreSuccess'
            : 'dataGovernance.lifecycle.destroySuccess';
      message.success(tRef.current(successKey));
      if (action === 'destroy') {
        // 资产已删除，重拉详情只会 404，直接关抽屉。
        onClose();
        return;
      }
      // 归档/恢复都改变状态（恢复还会重新解析）：静默刷新，已有详情不闪 Skeleton。
      await loadDetail(true);
    } catch (err: unknown) {
      if (my !== lifecycleSeq.current || assetKeyRef.current !== originKey) return;
      const text = actionErrorMessage(err, {
        forbiddenKey: 'dataGovernance.drawer.saveForbidden',
        fallbackKey: 'dataGovernance.lifecycle.actionFailed',
      });
      message.error('key' in text ? tRef.current(text.key) : text.message);
    } finally {
      if (my === lifecycleSeq.current) setLifecycleAction(null);
    }
  };

  // 写权限前端拿不到 space_manager/kb_manager；能点的按钮允许点击，403 时提示。
  const isTenantAdmin = getShellContext()?.getCurrentUser?.()?.isTenantAdmin === true;
  const isArchived = detail?.status === 'archived';
  // 统一并发闸门：元数据/策略/生命周期任一写操作进行中，其它写入口一律禁用。
  const busy = saving || retentionSaving || lifecycleAction !== null;

  // 与 utils helper 共用：内置级别文案、停用项 disabled 口径一致。
  const levelOptions = buildLevelOptions(t, classificationConfig.levels);
  const categoryOptions = buildCategoryOptions(classificationConfig.categories, t);
  const categoryName = (code: string) =>
    classificationConfig.categories.find((category) => category.code === code)?.name ?? code;

  return (
    <Drawer
      open={open}
      onClose={onClose}
      width={760}
      destroyOnHidden
      title={detail?.name || t('dataGovernance.drawer.title')}
    >
      {/* 首次打开时详情尚未取回（loading 置位前的一帧）也走 Skeleton，避免 Empty 一闪而过 */}
      {loading || (open && assetId && !detail && !error) ? (
        <Skeleton active />
      ) : error ? (
        <Alert
          type="error"
          showIcon
          title={error}
          action={
            <Button size="small" icon={<ReloadOutlined />} onClick={() => void loadDetail()}>
              {t('common.retry')}
            </Button>
          }
        />
      ) : !detail ? (
        <Empty description={t('dataGovernance.drawer.notFound')} />
      ) : (
        <Space direction="vertical" size={16} style={{ width: '100%' }}>
          <Descriptions title={t('dataGovernance.drawer.basic')} column={2} size="small">
            <Descriptions.Item label={t('dataGovernance.column.type')}>
              <Tag color={detail.assetType === 'document' ? 'blue' : 'purple'}>
                {t(`dataGovernance.assetType.${detail.assetType}`)}
              </Tag>
            </Descriptions.Item>
            <Descriptions.Item label={t('dataGovernance.column.kb')}>
              {detail.kbName}
            </Descriptions.Item>
            <Descriptions.Item label={t('dataGovernance.column.status')}>
              <Tag color={statusColor(detail.status)}>
                {t(statusLabelKey(detail.assetType, detail.status))}
              </Tag>
            </Descriptions.Item>
            {detail.assetType === 'document' && (
              <>
                <Descriptions.Item label={t('dataGovernance.column.quality')}>
                  <Tag color={qualityColor(detail.quality.verdict)}>
                    {t(qualityLabelKey(detail.quality.verdict))}
                  </Tag>
                </Descriptions.Item>
                <Descriptions.Item label={t('dataGovernance.drawer.mime')}>
                  {detail.mimeType || '—'}
                </Descriptions.Item>
                <Descriptions.Item label={t('dataGovernance.drawer.size')}>
                  {formatFileSize(detail.fileSize)}
                </Descriptions.Item>
                <Descriptions.Item label={t('dataGovernance.drawer.ontology')}>
                  {detail.ontologyStatus || '—'}
                </Descriptions.Item>
              </>
            )}
            {detail.assetType === 'data_source' && (
              <>
                <Descriptions.Item label={t('dataGovernance.drawer.access')}>
                  {detail.accessType || '—'}
                </Descriptions.Item>
                <Descriptions.Item label={t('dataGovernance.drawer.sync')}>
                  {detail.syncMode || '—'}
                </Descriptions.Item>
                <Descriptions.Item label={t('dataGovernance.drawer.lastSync')}>
                  {formatDateTime(detail.lastSyncAt)}
                </Descriptions.Item>
                <Descriptions.Item label={t('dataGovernance.drawer.docs')}>
                  {detail.documentCount ?? '—'}
                </Descriptions.Item>
              </>
            )}
          </Descriptions>

          <Divider style={{ margin: 0 }} />

          <Form layout="vertical">
            <div style={{ fontWeight: 600, marginBottom: 8 }}>
              {t('dataGovernance.drawer.metadata')}
            </div>

            <Descriptions column={2} size="small">
              <Descriptions.Item label={t('dataGovernance.classification.effective')}>
                <Space size={4} wrap>
                  <Tag color={levelColor(detail.sensitivity, classificationConfig.levels)}>
                    {levelLabel(
                      t,
                      classificationConfig.levels.find(
                        (level) => level.code === detail.sensitivity,
                      ),
                      detail.sensitivity,
                    )}
                  </Tag>
                  <Tag>
                    {t(
                      classificationSourceLabelKey(
                        detail.classificationSources.sensitivity,
                      ),
                    )}
                  </Tag>
                </Space>
              </Descriptions.Item>
              <Descriptions.Item
                label={t('dataGovernance.classification.effectiveCategories')}
              >
                <Space size={[0, 4]} wrap>
                  {detail.classificationCategories.length ? (
                    detail.classificationCategories.map((code) => (
                      <Tag key={code} color="blue">
                        {categoryName(code)}
                      </Tag>
                    ))
                  ) : (
                    <Tag>{t('dataGovernance.classification.noCategories')}</Tag>
                  )}
                  <Tag>
                    {t(
                      classificationSourceLabelKey(
                        detail.classificationSources.classificationCategories,
                      ),
                    )}
                  </Tag>
                </Space>
              </Descriptions.Item>
            </Descriptions>

            <Form.Item label={t('dataGovernance.drawer.owner')} htmlFor="yx-dg-owner">
              <Input
                id="yx-dg-owner"
                value={draft.owner ?? ''}
                maxLength={128}
                disabled={busy}
                placeholder={t('dataGovernance.drawer.ownerPlaceholder')}
                onChange={(e) => setDraft((d) => ({ ...d, owner: e.target.value || null }))}
              />
            </Form.Item>
            <Form.Item label={t('dataGovernance.drawer.domain')} htmlFor="yx-dg-domain">
              <Input
                id="yx-dg-domain"
                value={draft.domain ?? ''}
                maxLength={64}
                disabled={busy}
                placeholder={t('dataGovernance.drawer.domainPlaceholder')}
                onChange={(e) => setDraft((d) => ({ ...d, domain: e.target.value || null }))}
              />
            </Form.Item>

            <div className="yx-dg-subtitle">{t('dataGovernance.classification.assetOverride')}</div>
            <Form.Item label={t('dataGovernance.classification.level')} htmlFor="yx-dg-level">
              <Select
                id="yx-dg-level"
                allowClear
                disabled={busy}
                value={draft.sensitivity ?? undefined}
                placeholder={t('dataGovernance.classification.inherit')}
                options={levelOptions}
                onChange={(value) =>
                  setDraft((d) => ({
                    ...d,
                    sensitivity: (value as DgAssetDetail['sensitivity']) ?? null,
                  }))
                }
              />
            </Form.Item>
            <Form.Item
              label={t('dataGovernance.classification.categories')}
              htmlFor="yx-dg-categories"
            >
              <Space direction="vertical" size={4} style={{ width: '100%' }}>
                <Select
                  id="yx-dg-categories"
                  mode="multiple"
                  allowClear
                  disabled={busy}
                  value={draft.categories ?? undefined}
                  placeholder={t('dataGovernance.classification.inherit')}
                  options={categoryOptions}
                  onChange={(value: string[]) =>
                    setDraft((d) => ({
                      ...d,
                      // 清空选择 = 继承；显式「无分类」走下方按钮（[]）。
                      categories: value && value.length ? value : null,
                    }))
                  }
                  style={{ width: '100%' }}
                />
                <Button
                  size="small"
                  type={draft.categories?.length === 0 ? 'primary' : 'default'}
                  disabled={busy}
                  onClick={() => setDraft((d) => ({ ...d, categories: [] }))}
                >
                  {t('dataGovernance.classification.noCategories')}
                </Button>
              </Space>
            </Form.Item>
            <div className="yx-dg-muted" style={{ marginBottom: 12 }}>
              {t('dataGovernance.classification.inheritHint')}
            </div>

            <Button
              type="primary"
              loading={saving}
              disabled={busy && !saving}
              onClick={() => void save()}
            >
              {saving ? t('dataGovernance.drawer.saving') : t('dataGovernance.drawer.save')}
            </Button>
          </Form>

          <Divider style={{ margin: 0 }} />

          <div className="yx-dg-lifecycle">
            <div className="yx-dg-section-title">{t('dataGovernance.lifecycle.title')}</div>
            <Descriptions column={3} size="small">
              <Descriptions.Item label={t('dataGovernance.column.status')}>
                <Tag color={statusColor(detail.status)}>
                  {t(statusLabelKey(detail.assetType, detail.status))}
                </Tag>
              </Descriptions.Item>
              <Descriptions.Item label={t('dataGovernance.lifecycle.createdAt')}>
                {detail.createdAt ? formatDateTime(detail.createdAt) : '—'}
              </Descriptions.Item>
              <Descriptions.Item label={t('dataGovernance.lifecycle.archivedAt')}>
                {detail.archivedAt ? formatDateTime(detail.archivedAt) : '—'}
              </Descriptions.Item>
            </Descriptions>

            {detail.retentionPolicy ? (
              <Descriptions
                title={t('dataGovernance.lifecycle.effectivePolicy')}
                column={2}
                size="small"
              >
                <Descriptions.Item label={t('dataGovernance.lifecycle.retentionDays')}>
                  {retentionValueText(t, detail.retentionPolicy.retentionDays)}
                </Descriptions.Item>
                <Descriptions.Item label={t('dataGovernance.lifecycle.graceDays')}>
                  {t('dataGovernance.retention.days', {
                    count: detail.retentionPolicy.graceDays,
                  })}
                </Descriptions.Item>
                <Descriptions.Item label={t('dataGovernance.lifecycle.effectiveFrom')}>
                  <Tag color="blue">
                    {t(retentionSourceLabelKey(detail.retentionPolicy.effectiveFrom))}
                  </Tag>
                </Descriptions.Item>
                <Descriptions.Item label={t('dataGovernance.lifecycle.source')}>
                  {t(retentionSourceLabelKey(detail.retentionPolicy.sources.retentionDays))}
                  {' / '}
                  {t(retentionSourceLabelKey(detail.retentionPolicy.sources.graceDays))}
                </Descriptions.Item>
              </Descriptions>
            ) : (
              <div className="yx-dg-muted">—</div>
            )}

            {detail.assetType === 'document' && (
              <>
                <div className="yx-dg-subtitle">
                  {t('dataGovernance.lifecycle.documentOverride')}
                </div>
                <Form layout="vertical">
                  <div className="yx-dg-lifecycle-fields">
                    <Form.Item
                      label={t('dataGovernance.lifecycle.retentionDays')}
                      htmlFor="yx-dg-retention-days"
                    >
                      <InputNumber
                        id="yx-dg-retention-days"
                        min={0}
                        precision={0}
                        style={{ width: '100%' }}
                        disabled={busy}
                        value={retentionDraft.retentionDays ?? undefined}
                        placeholder={t('dataGovernance.lifecycle.inherit')}
                        addonAfter={t('dataGovernance.retention.daysUnit')}
                        onChange={(value) =>
                          setRetentionDraft((current) => ({
                            ...current,
                            retentionDays: typeof value === 'number' ? value : null,
                          }))
                        }
                      />
                    </Form.Item>
                    <Form.Item
                      label={t('dataGovernance.lifecycle.graceDays')}
                      htmlFor="yx-dg-grace-days"
                    >
                      <InputNumber
                        id="yx-dg-grace-days"
                        min={0}
                        precision={0}
                        style={{ width: '100%' }}
                        disabled={busy}
                        value={retentionDraft.graceDays ?? undefined}
                        placeholder={t('dataGovernance.lifecycle.inherit')}
                        addonAfter={t('dataGovernance.retention.daysUnit')}
                        onChange={(value) =>
                          setRetentionDraft((current) => ({
                            ...current,
                            graceDays: typeof value === 'number' ? value : null,
                          }))
                        }
                      />
                    </Form.Item>
                  </div>
                </Form>
                <div className="yx-dg-muted">{t('dataGovernance.policy.inheritHint')}</div>

                <Space wrap>
                  <Button
                    type="primary"
                    loading={retentionSaving}
                    disabled={busy && !retentionSaving}
                    onClick={() => void saveRetention()}
                  >
                    {t('dataGovernance.lifecycle.saveOverride')}
                  </Button>
                  {isArchived ? (
                    <>
                      <Popconfirm
                        title={t('dataGovernance.lifecycle.restore')}
                        description={t('dataGovernance.lifecycle.restoreConfirm', {
                          name: detail.name,
                        })}
                        okText={t('common.confirm')}
                        cancelText={t('common.cancel')}
                        onConfirm={() => void runLifecycle('restore')}
                      >
                        <Button
                          icon={<UndoOutlined />}
                          loading={lifecycleAction === 'restore'}
                          disabled={busy && lifecycleAction !== 'restore'}
                        >
                          {t('dataGovernance.lifecycle.restore')}
                        </Button>
                      </Popconfirm>
                      {isTenantAdmin && (
                        <Popconfirm
                          title={t('dataGovernance.lifecycle.destroy')}
                          description={t('dataGovernance.lifecycle.destroyConfirm', {
                            name: detail.name,
                          })}
                          okText={t('common.confirm')}
                          cancelText={t('common.cancel')}
                          okButtonProps={{ danger: true }}
                          onConfirm={() => void runLifecycle('destroy')}
                        >
                          <Button
                            danger
                            icon={<DeleteOutlined />}
                            loading={lifecycleAction === 'destroy'}
                            disabled={busy && lifecycleAction !== 'destroy'}
                          >
                            {t('dataGovernance.lifecycle.destroy')}
                          </Button>
                        </Popconfirm>
                      )}
                    </>
                  ) : (
                    <Popconfirm
                      title={t('dataGovernance.lifecycle.archive')}
                      description={t('dataGovernance.lifecycle.archiveConfirm', {
                        name: detail.name,
                      })}
                      okText={t('common.confirm')}
                      cancelText={t('common.cancel')}
                      onConfirm={() => void runLifecycle('archive')}
                    >
                      <Button
                        icon={<InboxOutlined />}
                        loading={lifecycleAction === 'archive'}
                        disabled={busy && lifecycleAction !== 'archive'}
                      >
                        {t('dataGovernance.lifecycle.archive')}
                      </Button>
                    </Popconfirm>
                  )}
                </Space>
              </>
            )}
          </div>

          {detail.assetType === 'document' && (
            <>
              <Divider style={{ margin: 0 }} />
              <LineageSection
                key={`${detail.assetType}:${detail.assetId}`}
                assetId={detail.assetId}
              />
            </>
          )}
        </Space>
      )}
    </Drawer>
  );
}
