/**
 * KB 级 compiled schema 生命周期面板。
 *
 * 新鲜度由后端**读时计算**（`GET /ontology/compiled-schema/status`，不落列、
 * 不自动重编译）；这里只负责显式呈现 + 两个人工动作：
 * - 重新编译：先预览 diff（只读），确认后 `force: true` 强制刷新版本与状态
 *   （默认 `force: false` 的短路会把 `outdated` 标记原样留着）；
 * - 从模板重置：走既有 reseed 接口（模板/YAML → compiled schema）。
 *
 * 过期不静默：outdated / manual_edited 一律显示提示与对齐影响面；
 * 对齐行的清理仍由用户在「概念对齐」页显式操作，本面板不删任何东西。
 */
import { useCallback, useEffect, useRef, useState } from 'react';
import { Alert, Button, Checkbox, Descriptions, Modal, Space, Tag, message } from 'antd';
import { useTranslation } from 'react-i18next';
import {
  getCompiledSchemaStatus,
  previewRecompileCompiledSchema,
  recompileCompiledSchema,
  reseedOntologyCompiledSchema,
} from '@/api/domainKnowledge';
import type { CompiledSchemaStatus, RecompilePreview } from '@/types/domainKnowledge';

interface Props {
  kbId: string;
  canWrite?: boolean;
  /**
   * 外部触发器：页内编辑（对象/关系/约束/YAML 导入）后由父组件自增，
   * 驱动本面板重新拉取状态（否则面板会一直显示编辑前的旧新鲜度）。
   */
  refreshKey?: number;
  /** recompile / reseed 成功后回调，供 CompileTab 重拉编辑数据。 */
  onChanged?: () => void;
}

const FRESHNESS_LABEL: Record<string, string> = {
  missing: 'compile.lifecycle.missing',
  seeded: 'compile.lifecycle.seeded',
  outdated: 'compile.lifecycle.outdated',
  manual_edited: 'compile.lifecycle.manualEdited',
  abstract_projection: 'compile.lifecycle.abstract',
};

const FRESHNESS_COLOR: Record<string, string> = {
  missing: 'red',
  seeded: 'green',
  outdated: 'orange',
  manual_edited: 'blue',
  abstract_projection: 'purple',
};

export default function SchemaLifecyclePanel({
  kbId,
  canWrite = false,
  refreshKey,
  onChanged,
}: Props) {
  const { t } = useTranslation();
  const [status, setStatus] = useState<CompiledSchemaStatus | null>(null);
  const [preview, setPreview] = useState<RecompilePreview | null>(null);
  const [previewOpen, setPreviewOpen] = useState(false);
  const [actionRunning, setActionRunning] = useState<'recompile' | 'reseed' | null>(null);
  const [previewing, setPreviewing] = useState(false);
  // 重编译后是否把该 KB 文档置 PENDING（默认不勾：批量重置代价高，必须显式选择）。
  const [applyToDocuments, setApplyToDocuments] = useState(false);
  const running = actionRunning !== null;

  // 请求序号：切换 kbId / 连续刷新时，旧响应不得覆盖新响应。
  const requestSeqRef = useRef(0);
  // 预览请求发起时的 kbId：响应回来时 KB 已切换 ⇒ 丢弃（M-6）。
  const kbIdRef = useRef(kbId);
  kbIdRef.current = kbId;
  // 记住上一个 kbId：切换时先清空旧状态，避免拿旧 KB 的 template_scenario_id
  // 去 reseed 新 KB（也避免旧徽标在新数据到达前短暂显示）。
  const prevKbIdRef = useRef(kbId);
  useEffect(() => {
    if (prevKbIdRef.current !== kbId) {
      prevKbIdRef.current = kbId;
      setStatus(null);
      setPreview(null);
      setPreviewOpen(false);
      setPreviewing(false);
      setApplyToDocuments(false);
    }
  }, [kbId]);

  const load = useCallback(
    () => {
      // refreshKey 只是触发器：父组件页内编辑后自增，强制重新拉取状态。
      void refreshKey;
      const seq = ++requestSeqRef.current;
      return getCompiledSchemaStatus(kbId)
        .then((next) => {
          if (seq === requestSeqRef.current) setStatus(next);
        })
        // 读取失败保留上次状态（首次失败仍为 null）：本面板是增强呈现，
        // 不能把编辑器弄瘫，也不能因为一次抖动把已显示的状态清空。
        .catch((e: unknown) => {
          if (seq !== requestSeqRef.current) return;
          console.warn('[SchemaLifecyclePanel] load compiled schema status failed', e);
          setStatus((prev) => prev);
        });
    },
    [kbId, refreshKey],
  );

  useEffect(() => {
    void load();
  }, [load]);

  const openPreview = async () => {
    if (running || previewing) return;
    const requestedKbId = kbId;
    setPreviewing(true);
    try {
      const p = await previewRecompileCompiledSchema(requestedKbId);
      // M-6：预览响应回来时 KB 已切换（或组件已切走）⇒ 丢弃，不污染新 KB 的弹窗。
      if (kbIdRef.current !== requestedKbId) return;
      setPreview(p);
      setPreviewOpen(true);
    } catch (e: unknown) {
      if (kbIdRef.current !== requestedKbId) return;
      message.error(e instanceof Error ? e.message : t('compile.lifecycle.previewFailed'));
    } finally {
      if (kbIdRef.current === requestedKbId) setPreviewing(false);
    }
  };

  // 关闭预览 = 同时清掉勾选。否则「重编译后重置文档」会粘滞到下一次
  // 工具栏动作，在不提示的情况下批量重置文档（P1-2）。
  const closePreview = () => {
    setPreviewOpen(false);
    setApplyToDocuments(false);
  };

  const runRecompile = async (applyDocuments: boolean) => {
    if (running || previewing) return;
    setActionRunning('recompile');
    try {
      // 显式 force：面板动作的目的就是清掉 outdated 标记，不能走默认短路。
      await recompileCompiledSchema(kbId, {
        force: true,
        applyToDocuments: applyDocuments,
      });
      message.success(t('compile.lifecycle.done'));
      closePreview();
      await load();
      onChanged?.();
    } catch (e: unknown) {
      message.error(e instanceof Error ? e.message : t('compile.lifecycle.actionFailed'));
    } finally {
      setActionRunning(null);
    }
  };

  // `applyDocuments` 由调用方显式给出：工具栏动作一律 false，只有预览弹窗内
  // 的确认动作才读取复选框。
  const runReseed = async (applyDocuments: boolean) => {
    if (running || previewing) return;
    if (!status?.template_scenario_id) {
      message.error(t('compile.lifecycle.templateMissing'));
      return;
    }
    setActionRunning('reseed');
    try {
      await reseedOntologyCompiledSchema(
        kbId,
        status.template_scenario_id,
        status.template_domain_id ?? undefined,
        applyDocuments,
      );
      message.success(t('compile.lifecycle.done'));
      closePreview();
      await load();
      onChanged?.();
    } catch (e: unknown) {
      message.error(e instanceof Error ? e.message : t('compile.lifecycle.actionFailed'));
    } finally {
      setActionRunning(null);
    }
  };

  // 工具栏「从模板重置」不得一击即执行（会丢弃人工编辑）：先弹二次确认。
  // 工具栏动作**一律不读**预览弹窗的隐藏勾选状态：显式 false，只有弹窗内的
  // 「确认从模板重置」才按复选框决定是否重置文档。
  const confirmReseed = () => {
    if (running || previewing) return;
    if (!status?.template_scenario_id) {
      message.error(t('compile.lifecycle.templateMissing'));
      return;
    }
    Modal.confirm({
      title: t('compile.lifecycle.confirmReseed'),
      content: t('compile.lifecycle.reseedConfirmContent'),
      okText: t('common.confirm'),
      cancelText: t('common.cancel'),
      okButtonProps: { danger: true },
      onOk: () => runReseed(false),
    });
  };

  if (!status) return null;

  const outdated = status.freshness === 'outdated';
  const manual = status.freshness === 'manual_edited';
  const hasTemplate = !!status.template_scenario_id;
  const staleDocTypes = status.stale_doc_types ?? [];
  const freshnessKey = FRESHNESS_LABEL[status.freshness];
  const freshnessColor = FRESHNESS_COLOR[status.freshness];

  // 禁用原因显式可见（不是只给 tooltip）：无写权限与人工编辑是两类不同原因。
  const noPermTitle = canWrite ? undefined : t('domainSpace.noManagePermission');
  const recompileTitle = !canWrite
    ? noPermTitle
    : manual
      ? t('compile.lifecycle.manualHint')
      : undefined;
  const reseedTitle = !canWrite
    ? noPermTitle
    : !hasTemplate
      ? t('compile.lifecycle.templateMissing')
      : undefined;
  const modalRecompileTitle = !canWrite
    ? noPermTitle
    : manual || preview?.manual_edited
      ? t('compile.lifecycle.manualHint')
      : undefined;

  return (
    <div style={{ marginBottom: 16 }}>
      <Space wrap style={{ marginBottom: 8 }}>
        <Tag color={freshnessColor ?? 'default'}>
          {freshnessKey
            ? t(freshnessKey)
            : `${t('compile.lifecycle.unknown')}: ${status.freshness}`}
        </Tag>
        <span>
          {t('compile.lifecycle.lastCompiled')}: {status.compiled_at ?? '—'}
        </span>
        <span>
          {t('compile.lifecycle.sourceVersion')}: {status.source_version ?? '—'}
        </span>
        <Button
          size="small"
          loading={previewing}
          disabled={!canWrite || running || previewing}
          title={noPermTitle}
          onClick={openPreview}
        >
          {t('compile.lifecycle.preview')}
        </Button>
        <Button
          size="small"
          type="primary"
          loading={actionRunning === 'recompile'}
          disabled={!canWrite || manual || running || previewing}
          title={recompileTitle}
          onClick={() => runRecompile(false)}
        >
          {t('compile.lifecycle.recompile')}
        </Button>
        <Button
          size="small"
          loading={actionRunning === 'reseed'}
          disabled={!canWrite || !hasTemplate || running || previewing}
          title={reseedTitle}
          onClick={confirmReseed}
        >
          {t('compile.lifecycle.reseed')}
        </Button>
      </Space>

      {outdated || manual ? (
        <Alert
          type={manual ? 'info' : 'warning'}
          showIcon
          message={t(manual ? 'compile.lifecycle.manualHint' : 'compile.lifecycle.outdatedHint')}
          description={
            <div>
              <Descriptions
                size="small"
                column={3}
                items={[
                  {
                    key: 'a',
                    label: t('compile.lifecycle.activeAlignments'),
                    children: status.alignment_impact?.active_alignments ?? 0,
                  },
                  {
                    key: 's',
                    label: t('compile.lifecycle.staleDocTypes'),
                    children: status.alignment_impact?.stale_doc_types ?? 0,
                  },
                  {
                    key: 'm',
                    label: t('compile.lifecycle.affectedMapsTo'),
                    children: status.alignment_impact?.projected_maps_to ?? 0,
                  },
                ]}
              />
              {staleDocTypes.length > 0 ? (
                <div style={{ marginTop: 4 }}>
                  {t('compile.lifecycle.staleDocTypesList')}: {staleDocTypes.join(', ')}
                </div>
              ) : null}
            </div>
          }
        />
      ) : null}

      <Modal
        open={previewOpen}
        title={t('compile.lifecycle.previewTitle')}
        width={640}
        onCancel={closePreview}
        footer={[
          <Button key="cancel" onClick={closePreview}>
            {t('common.cancel')}
          </Button>,
          <Button
            key="recompile"
            type="primary"
            loading={actionRunning === 'recompile'}
            disabled={!canWrite || manual || preview?.manual_edited || !preview}
            title={modalRecompileTitle}
            onClick={() => runRecompile(applyToDocuments)}
          >
            {t('compile.lifecycle.confirmRecompile')}
          </Button>,
          <Button
            key="reseed"
            danger
            loading={actionRunning === 'reseed'}
            disabled={!canWrite || !hasTemplate}
            title={reseedTitle}
            onClick={() => runReseed(applyToDocuments)}
          >
            {t('compile.lifecycle.confirmReseed')}
          </Button>,
        ]}
      >
        {preview ? (
          <div>
            <Checkbox
              checked={applyToDocuments}
              onChange={(e) => setApplyToDocuments(e.target.checked)}
              style={{ marginBottom: 12 }}
            >
              {t('compile.lifecycle.applyToDocuments')}
            </Checkbox>
            {preview.changed === false ? (
              <Alert
                type="info"
                showIcon
                style={{ marginBottom: 12 }}
                message={t('compile.lifecycle.noContentChange')}
              />
            ) : null}
            <Descriptions
              column={1}
              size="small"
              items={[
                {
                  key: 'c',
                  label: t('compile.lifecycle.changed'),
                  children: preview.changed ? t('common.yes') : t('common.no'),
                },
                {
                  key: 'a',
                  label: t('compile.lifecycle.added'),
                  children: preview.added_entity_types.join(', ') || '—',
                },
                {
                  key: 'r',
                  label: t('compile.lifecycle.removed'),
                  children: preview.removed_entity_types.join(', ') || '—',
                },
                {
                  key: 'n',
                  label: t('compile.lifecycle.renamed'),
                  children:
                    preview.renamed_entity_types.map((r) => `${r.from} → ${r.to}`).join(', ') ||
                    '—',
                },
                {
                  key: 's',
                  label: t('compile.lifecycle.staleAfter'),
                  children: preview.stale_after.join(', ') || '—',
                },
                {
                  key: 'al',
                  label: t('compile.lifecycle.affectedAlignments'),
                  children: preview.affected_alignments,
                },
                {
                  key: 'm',
                  label: t('compile.lifecycle.affectedMapsTo'),
                  children: preview.affected_maps_to,
                },
              ]}
            />
          </div>
        ) : null}
      </Modal>
    </div>
  );
}
