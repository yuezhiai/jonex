import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Alert, Form, Input, Modal, Select, Space, Switch, Tag, Typography } from 'antd';
import type { BindableTool, ToolBinding } from '@/types/domainKnowledge';
import { searchOntologyInstances } from '@/api/domainKnowledge';
import type { CreateToolBindingInput, UpdateToolBindingInput } from '@/api/ontologyToolBinding';

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

/** 解析参数映射 JSON：空 → {}；不是对象 → null（校验失败）。 */
export function parseMapping(raw?: string): Record<string, unknown> | null {
  const text = (raw || '').trim();
  if (!text) return {};
  try {
    const value = JSON.parse(text);
    return value && typeof value === 'object' && !Array.isArray(value) ? value : null;
  } catch {
    return null;
  }
}

/** 参数映射回填到文本框：空映射给空串（与新建一致） */
function mappingToText(mapping?: Record<string, unknown> | null): string {
  return mapping && Object.keys(mapping).length ? JSON.stringify(mapping, null, 2) : '';
}

/** 两份映射是否相同（按键排序后比较，避免只是键顺序不同也当成修改） */
function sameMapping(a: Record<string, unknown>, b: Record<string, unknown>): boolean {
  const norm = (m: Record<string, unknown>) =>
    JSON.stringify(Object.keys(m).sort().map((k) => [k, m[k]]));
  return norm(a) === norm(b);
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
  const toolName = Form.useWatch('toolName', form);
  const qaCallable = Form.useWatch('qaCallable', form);
  const [entityOptions, setEntityOptions] = useState<{ label: string; value: string }[]>([]);
  const [searching, setSearching] = useState(false);
  const searchSeq = useRef(0);
  const isEdit = !!editing;

  useEffect(() => {
    if (!open) return;
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
      okButtonProps={{ disabled: !isEdit && !toolsLoading && tools.length === 0 }}
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
          <Input.TextArea rows={4} placeholder='{"card_no": "$input.card_no", "reason": "$attr.definition"}' />
        </Form.Item>
        <Form.Item label={t('toolBinding.enabledLabel')} name="enabled" valuePropName="checked">
          <Switch />
        </Form.Item>
        {(!editing || editing.executable) && (
          <Form.Item
            label={t('toolBinding.qaCallableLabel')}
            name="qaCallable"
            valuePropName="checked"
            extra={t('toolBinding.qaCallableHint')}
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
