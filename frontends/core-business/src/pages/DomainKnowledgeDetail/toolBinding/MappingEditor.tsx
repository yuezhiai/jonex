import React, { useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import {
  Alert, AutoComplete, Button, Input, Select, Space, Tag, Tooltip, Typography, message,
} from 'antd';
import { DeleteOutlined, PlusOutlined } from '@ant-design/icons';
import type {
  BindableTool, BindingMappingSuggestion, MappingPreview,
} from '@/types/domainKnowledge';
import {
  literalTypeOf,
  parseMapping,
  rowsFromMapping,
  serializeRows,
  splitMappingSource,
  type LiteralType,
  type MappingRow,
  type MappingRowError,
  type MappingSource,
} from './mappingUtils';

interface Props {
  value?: string;
  onChange?: (value: string) => void;
  tool?: BindableTool | null;
  preview?: MappingPreview | null;
  previewStale?: boolean;
  previewLoading?: boolean;
  onPreview?: () => void;
  onDirty?: () => void;
  onValidityChange?: (valid: boolean) => void;
  recommendations?: BindingMappingSuggestion[] | null;
  recommendationSeq?: number;
  onRecommend?: () => void;
  recommendLoading?: boolean;
}

export default function MappingEditor({
  value, onChange, tool, preview, previewStale, previewLoading,
  onPreview, onDirty, onValidityChange,
  recommendations, recommendationSeq, onRecommend, recommendLoading,
}: Props) {
  const { t } = useTranslation();
  const idRef = useRef(0);
  const lastEmittedRef = useRef<string | null>(null);
  const [rows, setRows] = useState<MappingRow[]>([]);
  const [rowErrors, setRowErrors] = useState<Record<string, MappingRowError>>({});
  const [jsonMode, setJsonMode] = useState(false);

  useEffect(() => {
    const text = value || '';
    if (!jsonMode && lastEmittedRef.current !== null && text === lastEmittedRef.current) {
      return;
    }
    const parsed = parseMapping(text);
    if (parsed === null) {
      setRows([]);
      setRowErrors({});
      lastEmittedRef.current = text;
      return;
    }
    setRows(rowsFromMapping(parsed, () => `r${++idRef.current}`));
    setRowErrors({});
    lastEmittedRef.current = text;
  }, [value, jsonMode]);

  const invalidJson = parseMapping(value) === null;
  const properties = (tool?.inputSchema?.properties || {}) as Record<string, unknown>;
  const paramOptions = Object.keys(properties).map((k) => ({ value: k }));
  const required = new Set<string>((tool?.inputSchema?.required || []) as string[]);
  const attrOptions = (preview?.action.attributes || []).map((a) => ({ value: a.name }));
  const showTable = !jsonMode && !invalidJson;
  const hasRowErrors = Object.keys(rowErrors).length > 0;

  const emit = (nextRows: MappingRow[]) => {
    setRows(nextRows);
    const { mapping, errors } = serializeRows(nextRows);
    setRowErrors(errors);
    const valid = Object.keys(errors).length === 0;
    onValidityChange?.(valid);
    onDirty?.();
    if (valid) {
      const text = JSON.stringify(mapping, null, 2);
      lastEmittedRef.current = text;
      onChange?.(text);
    }
  };

  const update = (index: number, patch: Partial<MappingRow>) => {
    const row = rows[index];
    if (!row) return;
    if (patch.param !== undefined && patch.param !== row.param
        && rows.some((r, i) => i !== index && r.param === patch.param)) {
      message.warning(t('toolBinding.mappingDuplicateParam'));
      return;
    }
    emit(rows.map((r, i) => (i === index
      ? { ...r, ...patch, provenance: 'user', confidence: undefined, reason: undefined }
      : r)));
  };

  const applyRecommendations = (items: BindingMappingSuggestion[]) => {
    const next = [...rows];
    for (const item of items) {
      const split = splitMappingSource(item.value);
      const index = next.findIndex((r) => r.param === item.param);
      const patch: Omit<MappingRow, 'id'> = {
        param: item.param,
        source: split.source,
        sourceValue: split.value,
        literalType: literalTypeOf(item.value),
        provenance: item.provenance,
        confidence: item.confidence,
        reason: item.reason,
      };
      if (index >= 0) next[index] = { ...next[index], ...patch };
      else next.push({ id: `r${++idRef.current}`, ...patch });
    }
    emit(next);
  };

  useEffect(() => {
    if (!recommendationSeq || !recommendations?.length) return;
    applyRecommendations(recommendations);
    // recommendations 只在 recommendationSeq 变化时应用一次
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [recommendationSeq]);

  const addRow = (param?: string) => {
    const existing = new Set(rows.map((r) => r.param));
    let candidate = param;
    if (!candidate) {
      candidate = Array.from(required).find((r) => !existing.has(r));
    }
    if (!candidate) {
      let index = rows.length + 1;
      while (existing.has(`param_${index}`)) index += 1;
      candidate = `param_${index}`;
    }
    emit([...rows, {
      id: `r${++idRef.current}`,
      param: candidate,
      source: 'input',
      sourceValue: candidate,
      literalType: 'string',
      provenance: 'user',
    }]);
  };

  const removeRow = (index: number) => {
    emit(rows.filter((_, i) => i !== index));
  };

  const toggleMode = () => {
    if (jsonMode || invalidJson) {
      const next = parseMapping(value);
      if (next === null) return;
      setRows(rowsFromMapping(next, () => `r${++idRef.current}`));
      setRowErrors({});
      onValidityChange?.(true);
      setJsonMode(false);
      return;
    }
    onValidityChange?.(parseMapping(value) !== null);
    setJsonMode(true);
  };

  const statusTag = (row: MappingRow) => {
    if (previewStale || !preview) {
      return <Tag>{t('toolBinding.mappingStatusStale')}</Tag>;
    }
    if (preview.unknownKeys.includes(row.param)) {
      return <Tag color="orange">{t('toolBinding.mappingStatusUnknown')}</Tag>;
    }
    if (preview.emptyAttr.includes(row.param)) {
      return <Tag color="orange">{t('toolBinding.mappingStatusUnresolvedAttr')}</Tag>;
    }
    return <Tag color="green">{t('toolBinding.mappingStatusMapped')}</Tag>;
  };

  const rowErrorText = (row: MappingRow) => {
    const code = rowErrors[row.id];
    return code ? t(`toolBinding.mappingRowError_${code}`) : null;
  };

  const valueEditor = (row: MappingRow, index: number) => {
    if (row.source === 'input') {
      return (
        <Input
          value={row.sourceValue}
          placeholder={row.param}
          onChange={(e) => update(index, { sourceValue: e.target.value })}
        />
      );
    }
    if (row.source === 'attr') {
      return (
        <AutoComplete
          value={row.sourceValue}
          options={attrOptions}
          placeholder="definition"
          onChange={(v) => update(index, { sourceValue: v })}
        />
      );
    }
    if (row.source === 'entity') {
      return <Typography.Text code>$entity.name</Typography.Text>;
    }
    return (
      <Space.Compact style={{ width: '100%' }}>
        <Select
          value={row.literalType}
          style={{ width: 90 }}
          onChange={(v: LiteralType) => update(index, { literalType: v })}
          options={[
            { value: 'string', label: t('toolBinding.mappingLiteralString') },
            { value: 'number', label: t('toolBinding.mappingLiteralNumber') },
            { value: 'boolean', label: t('toolBinding.mappingLiteralBoolean') },
            { value: 'null', label: t('toolBinding.mappingLiteralNull') },
          ]}
        />
        {row.literalType === 'boolean' ? (
          <Select
            value={row.sourceValue}
            style={{ width: '100%' }}
            onChange={(v) => update(index, { sourceValue: v })}
            options={[{ value: 'true' }, { value: 'false' }]}
          />
        ) : row.literalType === 'null' ? (
          <Input value="null" disabled />
        ) : (
          <Input
            value={row.sourceValue}
            onChange={(e) => update(index, { sourceValue: e.target.value })}
          />
        )}
      </Space.Compact>
    );
  };

  const callerSuppliedOnly = preview && !previewStale
    ? preview.callerSupplied.filter((p) => !rows.some((r) => r.param === p))
    : [];

  return (
    <div>
      <Space wrap style={{ marginBottom: 8 }}>
        <Button size="small" icon={<PlusOutlined />} onClick={() => addRow()} disabled={invalidJson}>
          {t('toolBinding.mappingAddRow')}
        </Button>
        <Button size="small" onClick={toggleMode} disabled={hasRowErrors || invalidJson}>
          {jsonMode || invalidJson
            ? t('toolBinding.mappingViewTable')
            : t('toolBinding.mappingViewJson')}
        </Button>
        {onPreview && (
          <Button
            size="small"
            loading={previewLoading}
            onClick={onPreview}
            disabled={invalidJson || hasRowErrors}
          >
            {t('toolBinding.mappingValidate')}
          </Button>
        )}
        {onRecommend && (
          <Button
            size="small"
            loading={recommendLoading}
            onClick={onRecommend}
            disabled={invalidJson || hasRowErrors}
          >
            {t('toolBinding.mappingAiRecommend')}
          </Button>
        )}
      </Space>

      {invalidJson && (
        <Alert
          type="error"
          showIcon
          style={{ marginBottom: 8 }}
          message={t('toolBinding.mappingInvalidJson')}
        />
      )}
      {previewStale && !invalidJson && (
        <Typography.Text type="secondary" style={{ display: 'block', marginBottom: 8 }}>
          {t('toolBinding.mappingPreviewStale')}
        </Typography.Text>
      )}
      {!previewStale && preview?.statusMessage && (
        <Alert
          type={preview.status === 'ok' ? 'info' : 'warning'}
          showIcon
          style={{ marginBottom: 8 }}
          message={preview.statusMessage}
        />
      )}
      {!previewStale && !!preview?.errors?.length && (
        <Alert
          type="error"
          showIcon
          style={{ marginBottom: 8 }}
          message={t('toolBinding.mappingPreviewFailed')}
          description={<ul style={{ margin: 0, paddingInlineStart: 18 }}>
            {preview.errors.map((e) => <li key={e}>{e}</li>)}
          </ul>}
        />
      )}
      {!previewStale && !!preview?.warnings?.length && (
        <Alert
          type="warning"
          showIcon
          style={{ marginBottom: 8 }}
          description={<ul style={{ margin: 0, paddingInlineStart: 18 }}>
            {preview.warnings.map((w) => <li key={w}>{w}</li>)}
          </ul>}
        />
      )}

      {showTable ? (
        rows.length || callerSuppliedOnly.length ? (
          <Space direction="vertical" style={{ width: '100%' }} size={6}>
            {rows.map((row, index) => (
              <Space key={row.id} wrap style={{ width: '100%' }} align="start">
                <AutoComplete
                  value={row.param}
                  options={paramOptions}
                  style={{ width: 150 }}
                  onChange={(v) => update(index, { param: v })}
                />
                {required.has(row.param) && <Tag color="blue">{t('toolBinding.mappingRequiredTag')}</Tag>}
                <Select
                  value={row.source}
                  style={{ width: 130 }}
                  onChange={(v: MappingSource) => update(index, { source: v })}
                  options={[
                    { value: 'input', label: t('toolBinding.mappingSourceInput') },
                    { value: 'attr', label: t('toolBinding.mappingSourceAttr') },
                    { value: 'entity', label: t('toolBinding.mappingSourceEntity') },
                    { value: 'literal', label: t('toolBinding.mappingSourceLiteral') },
                  ]}
                />
                <div style={{ width: 230 }}>{valueEditor(row, index)}</div>
                {statusTag(row)}
                {row.provenance === 'rule' && (
                  <Tag color="cyan">{t('toolBinding.mappingProvenanceRule')}</Tag>
                )}
                {row.provenance === 'ai' && (
                  <Tooltip
                    title={t('toolBinding.mappingSuggestionHint', {
                      confidence: Math.round((row.confidence || 0) * 100),
                      reason: row.reason || '',
                    })}
                  >
                    <Tag color="purple">{t('toolBinding.mappingProvenanceAi')}</Tag>
                  </Tooltip>
                )}
                {row.provenance === 'user' && (
                  <Tag>{t('toolBinding.mappingProvenanceUser')}</Tag>
                )}
                <Button
                  size="small"
                  type="text"
                  danger
                  icon={<DeleteOutlined />}
                  onClick={() => removeRow(index)}
                />
                {rowErrorText(row) && (
                  <Typography.Text type="danger">{rowErrorText(row)}</Typography.Text>
                )}
              </Space>
            ))}
            {callerSuppliedOnly.map((param) => (
              <Space key={`caller-${param}`} wrap style={{ width: '100%' }} align="start">
                <Tag>{param}</Tag>
                <Typography.Text type="secondary">
                  {t('toolBinding.mappingCallerSupplied')}
                </Typography.Text>
                <Button size="small" onClick={() => addRow(param)}>
                  {t('toolBinding.mappingMakeExplicit')}
                </Button>
              </Space>
            ))}
            {preview && !previewStale && !preview.inputSchemaDeclared && (
              <Typography.Text type="secondary">
                {t('toolBinding.mappingNoSchema')}
              </Typography.Text>
            )}
          </Space>
        ) : (
          <Typography.Text type="secondary">{t('toolBinding.mappingNoRows')}</Typography.Text>
        )
      ) : (
        <Input.TextArea
          rows={6}
          value={value}
          onChange={(e) => {
            const text = e.target.value;
            onDirty?.();
            onValidityChange?.(parseMapping(text) !== null);
            onChange?.(text);
          }}
          placeholder='{"card_no": "$input.card_no", "reason": "$attr.definition"}'
        />
      )}
    </div>
  );
}
