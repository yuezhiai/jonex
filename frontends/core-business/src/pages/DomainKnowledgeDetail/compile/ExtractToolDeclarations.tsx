// 编辑对象弹窗里的「抽取期调用工具」一节：编辑本类型的 catalog: 声明，并可拿图里现有实体试跑。
// builtin: 内置技能只读展示、保存时原样保留（见 extractToolDraft.mergeSkills）。
import { useEffect, useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import {
  Alert,
  AutoComplete,
  Button,
  Empty,
  Input,
  Popconfirm,
  Select,
  Space,
  Spin,
  Table,
  Tag,
  Tooltip,
  Typography,
} from 'antd';
import { DeleteOutlined, ExperimentOutlined, PlusOutlined } from '@ant-design/icons';
import type { BindableTool, ExtractionSkillRef } from '@/types/domainKnowledge';
import { listBindableTools } from '@/api/ontologyToolBinding';
import { previewExtractTool, type ExtractToolPreviewResult } from '@/api/extractTool';
import {
  CONTRACT_FIELDS,
  type CatalogDraft,
  type DraftError,
  type ExtractGroup,
  type InputDraft,
  type WriteDraft,
  defaultInputsFor,
  draftToRef,
  emptyDraft,
} from './extractToolDraft';

export interface AttrOption {
  key: string;
  label: string;
}

interface Props {
  kbId: string;
  /** 图里实体的类型名；新建对象时为空 —— 不能试跑 */
  entityType?: string;
  attributes: AttrOption[];
  builtin: { refine: ExtractionSkillRef[]; constrain: ExtractionSkillRef[] };
  value: CatalogDraft[];
  onChange: (next: CatalogDraft[]) => void;
}

interface PreviewState {
  loading: boolean;
  result?: ExtractToolPreviewResult;
  error?: string;
  entity?: string;
}

function pretty(value: unknown): string {
  if (value === undefined) return '—';
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

function short(value: unknown): string {
  if (value === undefined || value === null || value === '') return '—';
  const text = typeof value === 'string' ? value : JSON.stringify(value);
  return text.length > 120 ? `${text.slice(0, 120)}…` : text;
}

/** 同名工具只留一个：本库工具优先（执行时也是先找本库工具） */
function dedupeTools(tools: BindableTool[]): BindableTool[] {
  const seen = new Map<string, BindableTool>();
  tools.forEach((tool) => {
    const prev = seen.get(tool.toolName);
    if (!prev || (prev.source !== 'kb' && tool.source === 'kb')) seen.set(tool.toolName, tool);
  });
  return [...seen.values()];
}

export default function ExtractToolDeclarations({ kbId, entityType, attributes, builtin, value, onChange }: Props) {
  const { t } = useTranslation();
  const [tools, setTools] = useState<BindableTool[]>([]);
  const [toolsLoading, setToolsLoading] = useState(true);
  const [toolsError, setToolsError] = useState(false);
  const [reload, setReload] = useState(0);
  const [previews, setPreviews] = useState<Record<string, PreviewState>>({});

  useEffect(() => {
    let alive = true;
    setToolsLoading(true);
    setToolsError(false);
    listBindableTools(kbId)
      .then((items) => {
        if (alive) setTools(dedupeTools(items));
      })
      .catch(() => {
        if (alive) setToolsError(true);
      })
      .finally(() => {
        if (alive) setToolsLoading(false);
      });
    return () => {
      alive = false;
    };
  }, [kbId, reload]);

  const toolByName = useMemo(() => new Map(tools.map((tool) => [tool.toolName, tool])), [tools]);
  const attrKeys = useMemo(() => attributes.map((a) => a.key), [attributes]);
  const attrKeySet = useMemo(() => new Set(attrKeys), [attrKeys]);
  const attrOptions = useMemo(
    () =>
      attributes
        .filter((a) => !CONTRACT_FIELDS.includes(a.key) && !a.key.startsWith('_'))
        .map((a) => ({ value: a.key, label: a.label === a.key ? a.key : `${a.label}（${a.key}）` })),
    [attributes],
  );
  const inputAttrOptions = useMemo(
    () => attributes.map((a) => ({ value: a.key, label: a.label === a.key ? a.key : `${a.label}（${a.key}）` })),
    [attributes],
  );

  const builtinItems = [
    ...builtin.refine.map((r) => ({ group: 'refine', ref: r })),
    ...builtin.constrain.map((r) => ({ group: 'constrain', ref: r })),
  ];

  const update = (key: string, patch: Partial<CatalogDraft>) => {
    onChange(value.map((d) => (d.key === key ? { ...d, ...patch } : d)));
    // 声明改了，旧的试跑结果不再对应
    setPreviews((prev) => {
      if (!prev[key]?.result && !prev[key]?.error) return prev;
      const next = { ...prev };
      delete next[key];
      return next;
    });
  };

  const changeTool = (d: CatalogDraft, toolName: string) => {
    const tool = toolByName.get(toolName);
    const patch: Partial<CatalogDraft> = { tool: toolName };
    // 入参还是空的才按工具入参 schema 预填；用户已填的不覆盖
    if (!d.inputs.length && tool) patch.inputs = defaultInputsFor(tool.inputSchema, attrKeys);
    update(d.key, patch);
  };

  const changeGroup = (d: CatalogDraft, group: ExtractGroup) => {
    update(d.key, {
      group,
      onError: 'keep',
      writes: group === 'refine' && !d.writes.length ? [{ attr: '', path: '$.data.' }] : d.writes,
    });
  };

  const errorText = (e: DraftError) => t(`compile.extractTool.err.${e.code}`, e.params || {});

  const runPreview = async (d: CatalogDraft, entity?: string) => {
    if (!entityType) return;
    const { ref, errors } = draftToRef(d);
    if (!ref) {
      setPreviews((prev) => ({ ...prev, [d.key]: { loading: false, error: errors.map(errorText).join('；') } }));
      return;
    }
    setPreviews((prev) => ({ ...prev, [d.key]: { ...prev[d.key], loading: true, error: undefined } }));
    try {
      const result = await previewExtractTool(kbId, {
        entityType,
        group: d.group,
        declaration: ref,
        canonicalName: entity,
      });
      setPreviews((prev) => ({
        ...prev,
        [d.key]: { loading: false, result, entity: result.entity?.canonical_name || entity },
      }));
    } catch (err) {
      setPreviews((prev) => ({
        ...prev,
        [d.key]: {
          loading: false,
          error: (err as { message?: string })?.message || t('compile.extractTool.preview.failed'),
        },
      }));
    }
  };

  const toolOptions = (d: CatalogDraft) => {
    const opts = tools.map((tool) => ({
      value: tool.toolName,
      disabled: !tool.readOnly,
      label: (
        <Space size={4}>
          <Tag color={tool.source === 'kb' ? 'cyan' : 'geekblue'} style={{ marginInlineEnd: 0 }}>
            {tool.source === 'kb' ? t('toolBinding.sourceKb') : t('toolBinding.sourcePlatform')}
          </Tag>
          <span>
            {tool.name}（{tool.toolName}）
          </span>
          {!tool.readOnly && <Typography.Text type="secondary">{t('compile.extractTool.decl.notReadOnly')}</Typography.Text>}
        </Space>
      ),
    }));
    // 声明里引用的工具已删除 / 停用：仍显示出来，免得下拉框里是一个裸字符串
    if (d.tool && !toolByName.has(d.tool)) {
      opts.unshift({
        value: d.tool,
        disabled: true,
        label: (
          <Space size={4}>
            <span>{d.tool}</span>
            <Tag color="red">{t('compile.extractTool.decl.toolMissing')}</Tag>
          </Space>
        ),
      });
    }
    return opts;
  };

  const renderInputs = (d: CatalogDraft) => {
    const tool = toolByName.get(d.tool);
    const required = new Set<string>(((tool?.inputSchema as any)?.required as string[]) || []);
    const setRow = (i: number, patch: Partial<InputDraft>) =>
      update(d.key, { inputs: d.inputs.map((row, idx) => (idx === i ? { ...row, ...patch } : row)) });
    return (
      <div>
        <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 4 }}>
          <Typography.Text strong style={{ fontSize: 13 }}>
            {t('compile.extractTool.decl.inputs')}
          </Typography.Text>
          <Button
            size="small"
            type="dashed"
            icon={<PlusOutlined />}
            onClick={() => update(d.key, { inputs: [...d.inputs, { param: '', source: 'name' }] })}
          >
            {t('compile.extractTool.decl.addInput')}
          </Button>
        </div>
        {!d.inputs.length && (
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            {t('compile.extractTool.decl.noInputs')}
          </Typography.Text>
        )}
        {d.inputs.map((row, i) => (
          <div key={i} style={{ display: 'grid', gridTemplateColumns: '1fr 150px 1fr 32px', gap: 8, marginBottom: 6 }}>
            <Input
              size="small"
              value={row.param}
              placeholder={t('compile.extractTool.decl.paramPlaceholder')}
              prefix={required.has(row.param) ? <span style={{ color: '#ff4d4f' }}>*</span> : undefined}
              onChange={(e) => setRow(i, { param: e.target.value })}
            />
            <Select
              size="small"
              value={row.source}
              onChange={(source) => setRow(i, { source })}
              options={[
                { value: 'name', label: t('compile.extractTool.decl.srcName') },
                { value: 'type', label: t('compile.extractTool.decl.srcType') },
                { value: 'attr', label: t('compile.extractTool.decl.srcAttr') },
                { value: 'literal', label: t('compile.extractTool.decl.srcLiteral') },
              ]}
            />
            {row.source === 'attr' ? (
              <Select
                size="small"
                showSearch
                value={row.attr || undefined}
                placeholder={t('compile.extractTool.decl.pickAttr')}
                options={inputAttrOptions}
                onChange={(attr) => setRow(i, { attr })}
              />
            ) : row.source === 'literal' ? (
              <Input
                size="small"
                value={row.literal}
                placeholder={t('compile.extractTool.decl.literalPlaceholder')}
                onChange={(e) => setRow(i, { literal: e.target.value })}
              />
            ) : (
              <Typography.Text type="secondary" style={{ fontSize: 12, lineHeight: '24px' }}>
                {row.source === 'name' ? t('compile.extractTool.decl.srcNameHint') : t('compile.extractTool.decl.srcTypeHint')}
              </Typography.Text>
            )}
            <Button
              size="small"
              type="text"
              danger
              icon={<DeleteOutlined />}
              onClick={() => update(d.key, { inputs: d.inputs.filter((_, idx) => idx !== i) })}
            />
          </div>
        ))}
      </div>
    );
  };

  const renderWrites = (d: CatalogDraft) => {
    const setRow = (i: number, patch: Partial<WriteDraft>) =>
      update(d.key, { writes: d.writes.map((row, idx) => (idx === i ? { ...row, ...patch } : row)) });
    return (
      <div>
        <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 4 }}>
          <Typography.Text strong style={{ fontSize: 13 }}>
            {t('compile.extractTool.decl.writes')}
          </Typography.Text>
          <Button
            size="small"
            type="dashed"
            icon={<PlusOutlined />}
            onClick={() => update(d.key, { writes: [...d.writes, { attr: '', path: '$.data.' }] })}
          >
            {t('compile.extractTool.decl.addWrite')}
          </Button>
        </div>
        {d.writes.map((row, i) => {
          const key = (row.attr || '').trim();
          const isNew = !!key && !attrKeySet.has(key);
          return (
            <div key={i} style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 32px', gap: 8, marginBottom: 6 }}>
              <AutoComplete
                size="small"
                value={row.attr}
                options={attrOptions}
                filterOption={(input, opt) => String(opt?.value || '').toLowerCase().includes(input.toLowerCase())}
                onChange={(attr) => setRow(i, { attr })}
              >
                <Input
                  size="small"
                  placeholder={t('compile.extractTool.decl.writeAttrPlaceholder')}
                  suffix={isNew ? <Tag color="gold" style={{ marginInlineEnd: 0 }}>{t('compile.extractTool.decl.newAttr')}</Tag> : undefined}
                />
              </AutoComplete>
              <Input
                size="small"
                value={row.path}
                placeholder="$.data.system_code"
                onChange={(e) => setRow(i, { path: e.target.value })}
              />
              <Button
                size="small"
                type="text"
                danger
                icon={<DeleteOutlined />}
                onClick={() => update(d.key, { writes: d.writes.filter((_, idx) => idx !== i) })}
              />
            </div>
          );
        })}
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          {t('compile.extractTool.decl.writesHint')}
        </Typography.Text>
      </div>
    );
  };

  const renderReject = (d: CatalogDraft) => (
    <div>
      <Typography.Text strong style={{ fontSize: 13 }}>
        {t('compile.extractTool.decl.rejectWhen')}
      </Typography.Text>
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 120px 1fr', gap: 8, marginTop: 4 }}>
        <Input
          size="small"
          value={d.rejectPath}
          placeholder="$.data.valid"
          onChange={(e) => update(d.key, { rejectPath: e.target.value })}
        />
        <Select
          size="small"
          value={d.equalsKind}
          onChange={(equalsKind) => update(d.key, { equalsKind })}
          options={[
            { value: 'false', label: t('compile.extractTool.decl.eqFalse') },
            { value: 'true', label: t('compile.extractTool.decl.eqTrue') },
            { value: 'string', label: t('compile.extractTool.decl.eqString') },
            { value: 'number', label: t('compile.extractTool.decl.eqNumber') },
            { value: 'null', label: t('compile.extractTool.decl.eqNull') },
          ]}
        />
        {d.equalsKind === 'string' || d.equalsKind === 'number' ? (
          <Input
            size="small"
            value={d.equalsValue}
            placeholder={t('compile.extractTool.decl.eqValuePlaceholder')}
            onChange={(e) => update(d.key, { equalsValue: e.target.value })}
          />
        ) : (
          <span />
        )}
      </div>
      <Typography.Text type="secondary" style={{ fontSize: 12 }}>
        {t('compile.extractTool.decl.rejectHint')}
      </Typography.Text>
    </div>
  );

  const statusTag = (status: string) => {
    if (status === 'executed') return <Tag color="green">{t('compile.extractTool.preview.status.executed')}</Tag>;
    if (status === 'failed') return <Tag color="red">{t('compile.extractTool.preview.status.failed')}</Tag>;
    return <Tag color="orange">{t(`compile.extractTool.preview.status.${status}`, { defaultValue: status })}</Tag>;
  };

  const renderPreview = (d: CatalogDraft) => {
    const p = previews[d.key];
    if (!p) return null;
    const r = p.result;
    return (
      <div style={{ marginTop: 10, padding: 10, background: '#fff', border: '1px dashed #d9d9d9', borderRadius: 6 }}>
        <Spin spinning={p.loading}>
          {p.error && <Alert type="error" showIcon title={p.error} style={{ marginBottom: 8 }} />}
          {!r && !p.error && p.loading && <div style={{ height: 40 }} />}
          {r && (
            <Space orientation="vertical" size={8} style={{ width: '100%' }}>
              <Space wrap>
                <Typography.Text>{t('compile.extractTool.preview.entity')}</Typography.Text>
                {r.candidates.length ? (
                  <Select
                    size="small"
                    style={{ minWidth: 220 }}
                    showSearch
                    value={p.entity}
                    options={r.candidates.map((c) => ({ value: c, label: c }))}
                    onChange={(name) => void runPreview(d, name)}
                  />
                ) : (
                  <Typography.Text type="secondary">—</Typography.Text>
                )}
                {statusTag(r.status)}
                {r.tool && (
                  <Tag color={r.tool.source === 'kb' ? 'cyan' : 'geekblue'}>
                    {r.tool.source === 'kb' ? t('toolBinding.sourceKb') : t('toolBinding.sourcePlatform')} · {r.tool.tool_name}
                  </Tag>
                )}
              </Space>
              {r.status === 'no_entity' && (
                <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={t('compile.extractTool.preview.noEntity')} />
              )}
              {r.inputs && (
                <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                  {t('compile.extractTool.preview.inputs')}
                  <code style={{ marginInlineStart: 6 }}>{short(r.inputs)}</code>
                </Typography.Text>
              )}
              {r.error && (
                <Typography.Text type="danger" style={{ fontSize: 12 }}>
                  {t('compile.extractTool.preview.error')}
                  {r.error}
                </Typography.Text>
              )}
              {d.group === 'refine' && (r.status === 'executed' || r.status === 'failed') && (
                <>
                  <Table
                    size="small"
                    pagination={false}
                    rowKey="attr"
                    dataSource={Object.keys(r.writes || {})
                      .concat((r.dropped || []).filter((a) => !(a in (r.writes || {}))))
                      .map((attr) => ({
                        attr,
                        before: r.entity?.attributes?.[attr],
                        after: (r.dropped || []).includes(attr) ? undefined : r.writes?.[attr],
                        dropped: (r.dropped || []).includes(attr),
                      }))}
                    locale={{ emptyText: t('compile.extractTool.preview.nothingWritten') }}
                    columns={[
                      {
                        title: t('compile.extractTool.preview.colAttr'),
                        dataIndex: 'attr',
                        render: (attr: string) => (
                          <Space size={4}>
                            <code>{attr}</code>
                            {!attrKeySet.has(attr) && <Tag color="gold">{t('compile.extractTool.decl.newAttr')}</Tag>}
                          </Space>
                        ),
                      },
                      { title: t('compile.extractTool.preview.colBefore'), dataIndex: 'before', render: short },
                      {
                        title: t('compile.extractTool.preview.colAfter'),
                        dataIndex: 'after',
                        render: (v: unknown, row: { dropped: boolean }) =>
                          row.dropped ? <Tag>{t('compile.extractTool.preview.dropped')}</Tag> : short(v),
                      },
                    ]}
                  />
                  {!!r.unresolved_writes?.length && (
                    <Alert
                      type="warning"
                      showIcon
                      title={t('compile.extractTool.preview.unresolved', { attrs: r.unresolved_writes.join('、') })}
                    />
                  )}
                </>
              )}
              {d.group === 'constrain' && r.status === 'executed' && (
                <Alert
                  type={r.rejected ? 'warning' : 'success'}
                  showIcon
                  title={r.rejected ? t('compile.extractTool.preview.rejected') : t('compile.extractTool.preview.kept')}
                />
              )}
              {d.group === 'constrain' && r.status === 'failed' && (
                <Alert
                  type="warning"
                  showIcon
                  title={d.onError === 'reject' ? t('compile.extractTool.preview.failedReject') : t('compile.extractTool.preview.failedKeep')}
                />
              )}
              {r.output !== undefined && (
                <details>
                  <summary style={{ cursor: 'pointer', fontSize: 12 }}>{t('compile.extractTool.preview.output')}</summary>
                  <pre style={{ maxHeight: 240, overflow: 'auto', fontSize: 12, background: '#f6f8fa', padding: 8, margin: '6px 0 0' }}>
                    {pretty(r.output)}
                  </pre>
                </details>
              )}
            </Space>
          )}
        </Spin>
      </div>
    );
  };

  return (
    <div style={{ marginTop: 16, padding: '12px 14px', background: '#f8fafc', borderRadius: 8 }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 6 }}>
        <Typography.Text strong>{t('compile.extractTool.decl.title')}</Typography.Text>
        <Space size={6}>
          <Button size="small" icon={<PlusOutlined />} onClick={() => onChange([...value, emptyDraft('refine')])}>
            {t('compile.extractTool.decl.addRefine')}
          </Button>
          <Button size="small" icon={<PlusOutlined />} onClick={() => onChange([...value, emptyDraft('constrain')])}>
            {t('compile.extractTool.decl.addConstrain')}
          </Button>
        </Space>
      </div>
      <Typography.Paragraph type="secondary" style={{ fontSize: 12, marginBottom: 8 }}>
        {t('compile.extractTool.decl.hint')}
      </Typography.Paragraph>

      {toolsError && (
        <Alert
          type="error"
          showIcon
          style={{ marginBottom: 8 }}
          title={t('compile.extractTool.decl.toolsLoadFailed')}
          action={
            <Button size="small" onClick={() => setReload((v) => v + 1)}>
              {t('common.retry')}
            </Button>
          }
        />
      )}
      {!toolsLoading && !toolsError && !tools.some((tool) => tool.readOnly) && (
        <Alert type="info" showIcon style={{ marginBottom: 8 }} title={t('compile.extractTool.decl.noReadOnlyTool')} />
      )}

      {builtinItems.length > 0 && (
        <div style={{ marginBottom: 8 }}>
          <Typography.Text type="secondary" style={{ fontSize: 12, marginInlineEnd: 6 }}>
            {t('compile.extractTool.decl.builtin')}
          </Typography.Text>
          {builtinItems.map((b, i) => (
            <Tooltip key={i} title={b.ref.description || undefined}>
              <Tag>
                {b.group === 'refine' ? t('compile.extractTool.decl.groupRefine') : t('compile.extractTool.decl.groupConstrain')} ·{' '}
                {b.ref.skill}
              </Tag>
            </Tooltip>
          ))}
        </div>
      )}

      {!value.length && (
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          {t('compile.extractTool.decl.empty')}
        </Typography.Text>
      )}

      {value.map((d) => {
        const { errors } = draftToRef(d);
        const canPreview = !!entityType && !!d.tool;
        return (
          <div
            key={d.key}
            style={{ marginTop: 10, padding: 10, background: '#fff', border: '1px solid #e5e7eb', borderRadius: 6 }}
          >
            <div style={{ display: 'grid', gridTemplateColumns: '150px 1fr auto auto', gap: 8, alignItems: 'center' }}>
              <Select
                size="small"
                value={d.group}
                onChange={(group) => changeGroup(d, group)}
                options={[
                  { value: 'refine', label: t('compile.extractTool.decl.groupRefine') },
                  { value: 'constrain', label: t('compile.extractTool.decl.groupConstrain') },
                ]}
              />
              <Select
                size="small"
                showSearch
                loading={toolsLoading}
                value={d.tool || undefined}
                placeholder={t('compile.extractTool.decl.pickTool')}
                options={toolOptions(d)}
                optionFilterProp="value"
                onChange={(toolName) => changeTool(d, toolName)}
              />
              <Tooltip
                title={
                  !entityType
                    ? t('compile.extractTool.preview.needSaved')
                    : t('compile.extractTool.preview.tooltip')
                }
              >
                <Button
                  size="small"
                  icon={<ExperimentOutlined />}
                  disabled={!canPreview}
                  loading={previews[d.key]?.loading}
                  onClick={() => void runPreview(d, previews[d.key]?.entity)}
                >
                  {t('compile.extractTool.preview.button')}
                </Button>
              </Tooltip>
              <Popconfirm
                title={t('compile.extractTool.decl.removeConfirm')}
                onConfirm={() => onChange(value.filter((x) => x.key !== d.key))}
              >
                <Button size="small" type="text" danger icon={<DeleteOutlined />} />
              </Popconfirm>
            </div>
            <Input
              size="small"
              style={{ marginTop: 8 }}
              value={d.description}
              placeholder={t('compile.extractTool.decl.descPlaceholder')}
              onChange={(e) => update(d.key, { description: e.target.value })}
            />
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16, marginTop: 8 }}>
              {renderInputs(d)}
              {d.group === 'refine' ? renderWrites(d) : renderReject(d)}
            </div>
            <Space size={6} style={{ marginTop: 8 }}>
              <Typography.Text style={{ fontSize: 13 }}>{t('compile.extractTool.decl.onError')}</Typography.Text>
              <Select
                size="small"
                style={{ minWidth: 220 }}
                value={d.onError}
                onChange={(onError) => update(d.key, { onError })}
                options={
                  d.group === 'refine'
                    ? [
                        { value: 'keep', label: t('compile.extractTool.decl.onErrorKeep') },
                        { value: 'drop_attr', label: t('compile.extractTool.decl.onErrorDrop') },
                      ]
                    : [
                        { value: 'keep', label: t('compile.extractTool.decl.onErrorPass') },
                        { value: 'reject', label: t('compile.extractTool.decl.onErrorReject') },
                      ]
                }
              />
            </Space>
            {errors.length > 0 && (d.tool || d.inputs.length > 0) && (
              <div style={{ marginTop: 6 }}>
                {errors.map((e, i) => (
                  <div key={i}>
                    <Typography.Text type="danger" style={{ fontSize: 12 }}>
                      {errorText(e)}
                    </Typography.Text>
                  </div>
                ))}
              </div>
            )}
            {renderPreview(d)}
          </div>
        );
      })}
    </div>
  );
}
