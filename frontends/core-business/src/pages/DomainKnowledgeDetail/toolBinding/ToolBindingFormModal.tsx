import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import {
  Alert, Button, Form, Input, Modal, Radio, Select, Space, Switch, Tag, Typography, message,
} from 'antd';
import type {
  BindableTool, BindingMappingProposal, BindingMappingSuggestion, MappingPreview, ToolBinding,
} from '@/types/domainKnowledge';
import { searchOntologyInstances } from '@/api/domainKnowledge';
import {
  previewToolBindingMapping,
  recommendToolBindingMapping,
  refineToolBindingMapping,
  type CreateToolBindingInput,
  type UpdateToolBindingInput,
} from '@/api/ontologyToolBinding';
import MappingEditor from './MappingEditor';
import { mappingToText, parseMapping, sameMapping } from './mappingUtils';

// 新绑定只接受 Action：执行器只执行单个动作；Skill 是多步规程，后端同样拒绝
const ENTITY_TYPE = 'Action' as const;

interface FormValues {
  canonicalName?: string;
  toolName?: string;
  mappingText?: string;
  enabled: boolean;
  qaCallable: boolean;
}

interface Props {
  open: boolean;
  kbId: string;
  tools: BindableTool[];
  toolsLoading: boolean;
  submitting: boolean;
  onCancel: () => void;
  onSubmit: (input: CreateToolBindingInput) => void;
  /** 给了 = 编辑模式：实例不可改，提交走 onUpdate */
  editing?: ToolBinding | null;
  onUpdate?: (binding: ToolBinding, input: UpdateToolBindingInput) => void;
}

function toolKey(tool: { source: string; toolName: string }): string {
  return `${tool.source}:${tool.toolName}`;
}

export default function ToolBindingFormModal({
  open,
  kbId,
  tools,
  toolsLoading,
  submitting,
  onCancel,
  onSubmit,
  editing = null,
  onUpdate,
}: Props) {
  const { t } = useTranslation();
  const [form] = Form.useForm<FormValues>();
  const canonicalName = Form.useWatch('canonicalName', form);
  const toolName = Form.useWatch('toolName', form);
  const qaCallable = Form.useWatch('qaCallable', form);
  const [preview, setPreview] = useState<MappingPreview | null>(null);
  const [previewLoading, setPreviewLoading] = useState(false);
  const [previewError, setPreviewError] = useState<string | null>(null);
  const [previewStale, setPreviewStale] = useState(false);
  const [mappingValid, setMappingValid] = useState(true);
  const previewSeq = useRef(0);
  const [aiLoading, setAiLoading] = useState(false);
  const [aiProposal, setAiProposal] = useState<BindingMappingProposal | null>(null);
  const [aiRecommendations, setAiRecommendations] = useState<BindingMappingSuggestion[]>([]);
  const [aiRecommendationSeq, setAiRecommendationSeq] = useState(0);
  const [aiError, setAiError] = useState<string | null>(null);
  const [aiAnswers, setAiAnswers] = useState<Record<string, string>>({});
  const aiRequestSeq = useRef(0);
  const [entityOptions, setEntityOptions] = useState<{ label: string; value: string }[]>([]);
  const [searching, setSearching] = useState(false);
  const searchSeq = useRef(0);
  const isEdit = !!editing;

  useEffect(() => {
    if (!open) return;
    setPreview(null);
    setPreviewLoading(false);
    setPreviewError(null);
    setPreviewStale(false);
    setMappingValid(true);
    previewSeq.current = 0;
    setAiLoading(false);
    setAiProposal(null);
    setAiRecommendations([]);
    setAiRecommendationSeq(0);
    setAiError(null);
    setAiAnswers({});
    aiRequestSeq.current = 0;
    if (editing) {
      form.setFieldsValue({
        canonicalName: editing.canonicalName,
        toolName: toolKey({ source: editing.toolSource, toolName: editing.toolName }),
        mappingText: mappingToText(editing.paramMapping),
        enabled: editing.enabled,
        qaCallable: editing.qaCallable,
      });
      return;
    }
    form.setFieldsValue({
      canonicalName: undefined,
      toolName: undefined,
      mappingText: '',
      enabled: true,
      qaCallable: false,
    });
  }, [editing, form, open]);

  const doSearch = useCallback(
    async (keyword: string) => {
      const seq = ++searchSeq.current;
      setSearching(true);
      try {
        const res = await searchOntologyInstances(kbId, keyword.trim() || undefined, 50);
        if (seq !== searchSeq.current) return;
        const seen = new Set<string>();
        const opts: { label: string; value: string }[] = [];
        for (const item of (res.items || []) as any[]) {
          const itemType = item.entity_type || item.type || '';
          const name = item.canonical_name || item.name || '';
          if (!name || itemType !== ENTITY_TYPE || seen.has(name)) continue;
          seen.add(name);
          opts.push({ label: name, value: name });
        }
        setEntityOptions(opts);
      } catch {
        if (seq === searchSeq.current) setEntityOptions([]);
      } finally {
        if (seq === searchSeq.current) setSearching(false);
      }
    },
    [kbId],
  );

  // 新建时打开预取一批 Action 候选；编辑时实例不可改，不用查
  useEffect(() => {
    if (!open || isEdit) return;
    doSearch('');
  }, [doSearch, open, isEdit]);

  const currentKey = editing ? toolKey({ source: editing.toolSource, toolName: editing.toolName }) : null;
  // 编辑时当前工具可能已不在可选列表里（停用 / 下架 / 删除）
  const currentToolMissing = !!currentKey && !toolsLoading && !tools.some((x) => toolKey(x) === currentKey);

  // 选项值 = `${source}:${toolName}`：本库工具与平台工具可以同名，必须带来源才能区分
  const selectedTool = useMemo(() => tools.find((x) => toolKey(x) === toolName), [toolName, tools]);

  const runPreview = useCallback(async () => {
    const actionName = editing ? editing.canonicalName : canonicalName;
    if (!actionName || !selectedTool) {
      previewSeq.current += 1;
      setPreview(null);
      setPreviewStale(false);
      return;
    }
    const mapping = parseMapping(form.getFieldValue('mappingText'));
    if (mapping === null) {
      previewSeq.current += 1;
      setPreview(null);
      setPreviewStale(false);
      return;
    }
    const seq = ++previewSeq.current;
    setPreviewLoading(true);
    setPreviewError(null);
    try {
      const res = await previewToolBindingMapping(kbId, {
        canonicalName: actionName,
        toolSource: selectedTool.source,
        toolName: selectedTool.toolName,
        paramMapping: mapping,
      });
      if (seq !== previewSeq.current) return;
      setPreview(res);
      setPreviewStale(false);
    } catch (e: any) {
      if (seq !== previewSeq.current) return;
      setPreview(null);
      setPreviewStale(false);
      setPreviewError(e?.message || t('toolBinding.mappingPreviewFailed'));
    } finally {
      if (seq === previewSeq.current) setPreviewLoading(false);
    }
  }, [canonicalName, editing, form, kbId, selectedTool, t]);

  const markMappingDirty = useCallback(() => {
    previewSeq.current += 1;
    setPreviewLoading(false);
    setPreviewStale(true);
  }, []);

  const applyAiProposal = useCallback((proposal: BindingMappingProposal) => {
    setAiProposal(proposal);
    setAiRecommendations(proposal.mappings);
    if (proposal.mappings.length) setAiRecommendationSeq((value) => value + 1);
    setAiError(proposal.error);
    setAiAnswers({});
  }, []);

  const runRecommend = useCallback(async () => {
    const actionName = editing ? editing.canonicalName : canonicalName;
    if (!actionName || !selectedTool) return;
    const mapping = parseMapping(form.getFieldValue('mappingText'));
    if (mapping === null) return;
    const seq = ++aiRequestSeq.current;
    setAiLoading(true);
    setAiError(null);
    try {
      const res = await recommendToolBindingMapping(kbId, {
        canonicalName: actionName,
        toolSource: selectedTool.source,
        toolName: selectedTool.toolName,
        currentMapping: mapping,
      });
      if (seq !== aiRequestSeq.current) return;
      applyAiProposal(res);
    } catch (e: any) {
      if (seq !== aiRequestSeq.current) return;
      setAiError(e?.message || t('toolBinding.mappingAiFailed'));
    } finally {
      if (seq === aiRequestSeq.current) setAiLoading(false);
    }
  }, [applyAiProposal, canonicalName, editing, form, kbId, selectedTool, t]);

  const runRefine = useCallback(async () => {
    const actionName = editing ? editing.canonicalName : canonicalName;
    if (!actionName || !selectedTool || !aiProposal) return;
    const mapping = parseMapping(form.getFieldValue('mappingText'));
    if (mapping === null) return;
    const answers = aiProposal.questions
      .map((q) => ({ question: q.question, answer: (aiAnswers[q.id] || '').trim() }))
      .filter((item) => item.answer);
    if (!answers.length) {
      message.warning(t('toolBinding.mappingAiAnswerRequired'));
      return;
    }
    const seq = ++aiRequestSeq.current;
    setAiLoading(true);
    setAiError(null);
    try {
      const res = await refineToolBindingMapping(kbId, {
        canonicalName: actionName,
        toolSource: selectedTool.source,
        toolName: selectedTool.toolName,
        currentMapping: mapping,
        assistSessionId: aiProposal.assistSessionId,
        answers,
        contextHash: aiProposal.contextHash,
        round: aiProposal.round,
      });
      if (seq !== aiRequestSeq.current) return;
      applyAiProposal(res);
    } catch (e: any) {
      if (seq !== aiRequestSeq.current) return;
      setAiError(e?.message || t('toolBinding.mappingAiFailed'));
    } finally {
      if (seq === aiRequestSeq.current) setAiLoading(false);
    }
  }, [aiAnswers, aiProposal, applyAiProposal, canonicalName, editing, form, kbId, selectedTool, t]);

  useEffect(() => {
    if (!open) return;
    setPreviewError(null);
    aiRequestSeq.current += 1;
    setAiLoading(false);
    setAiProposal(null);
    setAiRecommendations([]);
    setAiError(null);
    const actionName = editing ? editing.canonicalName : canonicalName;
    if (!actionName || !selectedTool) {
      previewSeq.current += 1;
      setPreview(null);
      setPreviewStale(false);
      return;
    }
    // 工具 / 动作变了，旧预览不再对应当前配置
    setPreviewStale(true);
    void runPreview();
    // 只在动作 / 工具变化时自动预览；映射改动由「校验映射」手动触发
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, editing, canonicalName, toolName, selectedTool]);

  const toolOptions = useMemo(() => {
    const group = (source: 'kb' | 'platform', label: string) => {
      const opts = tools
        .filter((x) => x.source === source)
        .map((x) => ({
          label: `${x.name}（${x.toolName}）${x.readOnly ? ` · ${t('toolRuntime.readOnlyTag')}` : ''}`,
          value: toolKey(x),
        }));
      return opts.length ? [{ label, options: opts }] : [];
    };
    const groups: { label: string; options: { label: string; value: string; disabled?: boolean }[] }[] = [
      ...group('kb', t('toolBinding.toolGroupKb')),
      ...group('platform', t('toolBinding.toolGroupPlatform')),
    ];
    if (editing && currentToolMissing && currentKey) {
      // 不在列表里也要能显示当前值，否则下拉框里只剩一个裸的 `kb:xxx`
      const label = t('toolBinding.toolUnavailableOption', { name: editing.toolLabel || editing.toolName });
      groups.unshift({
        label: editing.toolSource === 'kb' ? t('toolBinding.toolGroupKb') : t('toolBinding.toolGroupPlatform'),
        options: [{ label, value: currentKey, disabled: true }],
      });
    }
    return groups;
  }, [currentKey, currentToolMissing, editing, t, tools]);
  const toolParams = useMemo(() => {
    const props = (selectedTool?.inputSchema?.properties || {}) as Record<string, any>;
    const required = new Set<string>((selectedTool?.inputSchema?.required || []) as string[]);
    return Object.keys(props).map((k) => (required.has(k) ? `${k}*` : k));
  }, [selectedTool]);
  // 只读判断：选中的工具在列表里就看列表，否则（编辑时当前工具不可用）看绑定上记的
  // 新建时还没选工具不提示（与原行为一致）
  const selectedReadOnly = selectedTool ? selectedTool.readOnly : editing ? editing.toolReadOnly === true : true;

  async function handleOk() {
    const values = await form.validateFields();
    if (!mappingValid) {
      message.error(t('toolBinding.mappingInvalid'));
      return;
    }
    const mapping = parseMapping(values.mappingText) || {};
    if (editing) {
      // 只提交改动过的字段：工具没换就不提交，当前工具不可用时也能保存映射 / 开关
      const input: UpdateToolBindingInput = {};
      if (values.toolName && values.toolName !== currentKey) {
        const tool = tools.find((x) => toolKey(x) === values.toolName);
        if (!tool) return;
        input.toolSource = tool.source;
        input.toolName = tool.toolName;
      }
      if (!sameMapping(mapping, editing.paramMapping || {})) input.paramMapping = mapping;
      if (values.enabled !== editing.enabled) input.enabled = values.enabled;
      if (editing.executable && !!values.qaCallable !== editing.qaCallable) input.qaCallable = !!values.qaCallable;
      onUpdate?.(editing, input);
      return;
    }
    const tool = tools.find((x) => toolKey(x) === values.toolName);
    if (!tool) return;
    onSubmit({
      entityType: ENTITY_TYPE,
      canonicalName: values.canonicalName!,
      toolSource: tool.source,
      toolName: tool.toolName,
      paramMapping: mapping,
      enabled: values.enabled,
      qaCallable: !!values.qaCallable,
    });
  }

  return (
    <Modal
      title={isEdit ? t('toolBinding.editTitle') : t('toolBinding.newTitle')}
      open={open}
      onCancel={onCancel}
      onOk={handleOk}
      okText={t('common.confirm')}
      cancelText={t('common.cancel')}
      confirmLoading={submitting}
      okButtonProps={{
        disabled: (!isEdit && !toolsLoading && tools.length === 0) || !mappingValid,
      }}
      width={600}
      destroyOnHidden
    >
      {!isEdit && !toolsLoading && tools.length === 0 && (
        <Alert type="warning" showIcon style={{ marginBottom: 12 }} message={t('toolBinding.noTools')} />
      )}
      {editing && !editing.executable && (
        <Alert type="info" showIcon style={{ marginBottom: 12 }} message={t('toolBinding.legacyEditHint')} />
      )}
      <Form form={form} layout="vertical" preserve={false}>
        <Form.Item
          label={t('toolBinding.entityTypeLabel')}
          extra={isEdit ? undefined : t('toolBinding.actionOnlyHint')}
        >
          <Tag color={editing?.entityType === 'Skill' ? 'purple' : 'blue'}>{editing?.entityType || 'Action'}</Tag>
        </Form.Item>
        {isEdit ? (
          <Form.Item label={t('toolBinding.entityNameLabel')} extra={t('toolBinding.entityFixedHint')}>
            <Space size={4}>
              <Typography.Text strong>{editing!.canonicalName}</Typography.Text>
              {editing!.targetExists === false && <Tag color="red">{t('toolBinding.targetMissing')}</Tag>}
            </Space>
          </Form.Item>
        ) : (
          <Form.Item
            label={t('toolBinding.entityNameLabel')}
            name="canonicalName"
            rules={[{ required: true, message: t('toolBinding.entityNameRequired') }]}
          >
            <Select
              showSearch
              filterOption={false}
              loading={searching}
              placeholder={t('toolBinding.entityNamePlaceholder')}
              options={entityOptions}
              onSearch={(kw) => doSearch(kw)}
            />
          </Form.Item>
        )}
        <Form.Item
          label={t('toolBinding.toolLabel')}
          name="toolName"
          rules={[{ required: true, message: t('toolBinding.toolRequired') }]}
          extra={
            isEdit && currentToolMissing && toolName === currentKey
              ? t('toolBinding.toolUnchangedHint')
              : toolParams.length
                ? t('toolBinding.toolSchemaHint', { params: toolParams.join(', ') })
                : undefined
          }
        >
          <Select
            loading={toolsLoading}
            placeholder={t('toolBinding.toolPlaceholder')}
            options={toolOptions}
          />
        </Form.Item>
        <Form.Item
          label={t('toolBinding.mappingLabel')}
          name="mappingText"
          extra={<Typography.Text type="secondary">{t('toolBinding.mappingExtra')}</Typography.Text>}
          rules={[
            {
              validator: (_, value) =>
                parseMapping(value) === null
                  ? Promise.reject(new Error(t('toolBinding.mappingInvalid')))
                  : Promise.resolve(),
            },
          ]}
        >
          <MappingEditor
            tool={selectedTool}
            preview={preview}
            previewStale={previewStale}
            previewLoading={previewLoading}
            onPreview={() => void runPreview()}
            onDirty={markMappingDirty}
            onValidityChange={setMappingValid}
            recommendations={aiRecommendations}
            recommendationSeq={aiRecommendationSeq}
            onRecommend={selectedTool && (isEdit || !!canonicalName) ? () => void runRecommend() : undefined}
            recommendLoading={aiLoading}
          />
        </Form.Item>
        {aiError && (
          <Alert type="error" showIcon style={{ marginBottom: 12 }} message={aiError} />
        )}
        {!!aiProposal?.warnings?.length && (
          <Alert
            type="warning"
            showIcon
            style={{ marginBottom: 12 }}
            description={<ul style={{ margin: 0, paddingInlineStart: 18 }}>
              {aiProposal.warnings.map((w) => <li key={w}>{w}</li>)}
            </ul>}
          />
        )}
        {aiProposal && aiProposal.status === 'ready' && aiProposal.mappings.length > 0
          && aiProposal.questions.length === 0 && (
          <Alert
            type="success"
            showIcon
            style={{ marginBottom: 12 }}
            message={t('toolBinding.mappingAiReady')}
          />
        )}
        {aiProposal && aiProposal.questions.length > 0 && (
          <Alert
            type="info"
            showIcon
            style={{ marginBottom: 12 }}
            message={t('toolBinding.mappingAiNeedsInput', { round: aiProposal.round })}
            description={(
              <div>
                {aiProposal.questions.map((q) => (
                  <div key={q.id} style={{ marginBottom: 10 }}>
                    <div style={{ marginBottom: 4 }}>{q.question}</div>
                    <Radio.Group
                      value={aiAnswers[q.id] || ''}
                      onChange={(e) => setAiAnswers((prev) => ({ ...prev, [q.id]: e.target.value }))}
                    >
                      {q.options.map((o) => (
                        <Radio key={o.value} value={o.value}>
                          {o.label}
                          {o.recommended ? ` ${t('toolBinding.mappingAiRecommended')}` : ''}
                        </Radio>
                      ))}
                    </Radio.Group>
                    {q.allowFreeText && !q.options.some((o) => o.value === aiAnswers[q.id]) && (
                      <Input
                        style={{ marginTop: 4 }}
                        value={aiAnswers[q.id] || ''}
                        placeholder={t('toolBinding.mappingAiAnswerPlaceholder')}
                        onChange={(e) => setAiAnswers((prev) => ({ ...prev, [q.id]: e.target.value }))}
                      />
                    )}
                  </div>
                ))}
                <Space>
                  <Button size="small" type="primary" loading={aiLoading} onClick={() => void runRefine()}>
                    {t('toolBinding.mappingAiContinue')}
                  </Button>
                  <Button
                    size="small"
                    onClick={() => setAiProposal((prev) => (
                      prev ? { ...prev, questions: [], status: 'ready' } : prev
                    ))}
                  >
                    {t('toolBinding.mappingAiClose')}
                  </Button>
                </Space>
              </div>
            )}
          />
        )}
        {previewError && (
          <Alert type="error" showIcon style={{ marginBottom: 12 }} message={previewError} />
        )}
        {!selectedTool && isEdit && currentToolMissing && (
          <Alert
            type="info"
            showIcon
            style={{ marginBottom: 12 }}
            message={t('toolBinding.mappingPreviewNoTool')}
          />
        )}
        <Form.Item label={t('toolBinding.enabledLabel')} name="enabled" valuePropName="checked">
          <Switch />
        </Form.Item>
        {(!editing || editing.executable) && (
          <Form.Item
            label={selectedReadOnly
              ? t('toolBinding.qaCallableLabel')
              : t('toolBinding.qaCallableLabelWrite')}
            name="qaCallable"
            valuePropName="checked"
            extra={selectedReadOnly
              ? t('toolBinding.qaCallableHint')
              : t('toolBinding.qaCallableHintWrite')}
          >
            <Switch />
          </Form.Item>
        )}
        {qaCallable && (!editing || editing.executable) && (
          <Alert
            type="warning"
            showIcon
            style={{ marginBottom: 12 }}
            message={t('toolBinding.qaCallableEgress')}
            description={!selectedReadOnly ? t('toolBinding.qaCallableNotReadOnly') : undefined}
          />
        )}
      </Form>
    </Modal>
  );
}
