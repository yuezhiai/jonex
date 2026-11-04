import { useEffect, useRef, useState } from 'react';
import {
  Alert, Button, Drawer, Input, InputNumber, Select, Space, Switch, Tag, message,
} from 'antd';
import { useTranslation } from 'react-i18next';

import { listContractDataSources } from '@/api/dataContracts';
import { getAsset, listAssets } from '@/api/dataGovernance';
import { listKbOptions } from '@/api/dataOperations';
import { createQualityRule, updateQualityRule } from '@/api/quality';
import type {
  DgQualityCheckType,
  DgQualityDimension,
  DgQualityRule,
  DgQualityRuleBody,
  DgQualitySeverity,
} from '../types';
import {
  QUALITY_CHECK_TYPES,
  QUALITY_DIMENSIONS,
  QUALITY_SEVERITIES,
  buildQualityAssetId,
  checkTypeLabelKey,
  dimensionLabelKey,
  parseQualityAssetId,
  severityLabelKey,
} from '../qualityUtils';

interface Props {
  open: boolean;
  rule: DgQualityRule | null;
  canWrite: boolean;
  onClose: () => void;
  onSaved: () => void;
}

interface Option {
  value: string;
  label: string;
}

const DEFAULT_DEFINITION = '{"metric":"nullValues"}';
const DEFAULT_THRESHOLD = '{"mustBeLessThan":1}';
const TABLE_PAGE_SIZE = 100;

async function loadAllTables(kbId: string) {
  const items = [];
  for (let page = 1; ; page += 1) {
    const result = await listAssets({
      assetType: 'table', kbId, page, pageSize: TABLE_PAGE_SIZE,
    });
    items.push(...result.items);
    if (result.items.length < TABLE_PAGE_SIZE || items.length >= result.total) break;
  }
  return items;
}

export default function QualityRuleDrawer({
  open, rule, canWrite, onClose, onSaved,
}: Props) {
  const { t } = useTranslation();
  const [name, setName] = useState('');
  const [kbId, setKbId] = useState('');
  const [kbOptions, setKbOptions] = useState<Option[]>([]);
  const [dataSourceId, setDataSourceId] = useState('');
  const [dataSourceOptions, setDataSourceOptions] = useState<Option[]>([]);
  const [tableAssetId, setTableAssetId] = useState('');
  const [tableName, setTableName] = useState('');
  const [tableOptions, setTableOptions] = useState<Option[]>([]);
  const [columnName, setColumnName] = useState('');
  const [columnOptions, setColumnOptions] = useState<Option[]>([]);
  const [assetFallback, setAssetFallback] = useState<string | null>(null);
  const [loadingAssets, setLoadingAssets] = useState(false);
  const [dimension, setDimension] = useState<DgQualityDimension>('completeness');
  const [severity, setSeverity] = useState<DgQualitySeverity>('warning');
  const [checkType, setCheckType] = useState<DgQualityCheckType>('library');
  const [definition, setDefinition] = useState(DEFAULT_DEFINITION);
  const [threshold, setThreshold] = useState(DEFAULT_THRESHOLD);
  const [enabled, setEnabled] = useState(0);
  const [saving, setSaving] = useState(false);
  const backfillSeq = useRef(0);
  const tableSeq = useRef(0);
  const columnSeq = useRef(0);

  // 打开抽屉时初始化表单，并回填已有规则的资产层级。
  useEffect(() => {
    if (!open) return;
    const seq = ++backfillSeq.current;
    setName(rule?.name ?? '');
    setDimension(rule?.dimension ?? 'completeness');
    setSeverity(rule?.severity ?? 'warning');
    setCheckType(rule?.checkType ?? 'library');
    setDefinition(JSON.stringify(rule?.definition ?? JSON.parse(DEFAULT_DEFINITION), null, 2));
    setThreshold(JSON.stringify(rule?.threshold ?? JSON.parse(DEFAULT_THRESHOLD), null, 2));
    setEnabled(rule?.enabled ?? 0);
    setColumnOptions([]);
    setTableOptions([]);
    setDataSourceOptions([]);

    const parsed = rule
      ? parseQualityAssetId(rule.assetId, rule.assetType)
      : { dataSourceId: '', table: '', column: null };
    const initialDataSource = rule?.dataSourceId || parsed.dataSourceId;
    const initialTable = parsed.table;
    setKbId(rule?.kbId ?? '');
    setDataSourceId(initialDataSource);
    setTableName(initialTable);
    setTableAssetId(initialDataSource && initialTable
      ? buildQualityAssetId(initialDataSource, initialTable)
      : '');
    setColumnName(rule?.assetType === 'column' ? parsed.column ?? '' : '');
    const resolvable = Boolean(initialDataSource && initialTable);
    setAssetFallback(resolvable ? null : (rule?.assetId || null));

    // KB 缺失时，从资产详情补一次；失败则保留原 assetId 只读展示。
    if (rule && !rule.kbId && resolvable) {
      void getAsset(rule.assetType, rule.assetId)
        .then((detail) => {
          if (seq !== backfillSeq.current) return;
          if (detail.kbId) setKbId(detail.kbId);
        })
        .catch(() => {
          if (seq !== backfillSeq.current) return;
          setAssetFallback(rule.assetId || null);
        });
    }

    void listKbOptions()
      .then((options) => {
        if (seq !== backfillSeq.current) return;
        setKbOptions(options.map((item) => ({ value: item.id, label: item.name })));
      })
      .catch(() => {
        if (seq !== backfillSeq.current) return;
        setKbOptions([]);
      });

    return () => {
      backfillSeq.current += 1;
    };
  }, [open, rule]);

  // KB → 数据源（后端已过滤 database 源）。
  useEffect(() => {
    if (!open || !kbId) {
      setDataSourceOptions([]);
      return;
    }
    let cancelled = false;
    void listContractDataSources(kbId)
      .then((options) => {
        if (cancelled) return;
        setDataSourceOptions(options.map((item) => ({ value: item.id, label: item.name })));
      })
      .catch(() => {
        if (!cancelled) setDataSourceOptions([]);
      });
    return () => { cancelled = true; };
  }, [open, kbId]);

  // KB → 全量表，再按 sourceId 收敛到当前数据源。
  useEffect(() => {
    if (!open || !kbId || !dataSourceId) {
      setTableOptions([]);
      return;
    }
    const seq = ++tableSeq.current;
    setLoadingAssets(true);
    void loadAllTables(kbId)
      .then((items) => {
        if (seq !== tableSeq.current) return;
        setTableOptions(items
          .filter((item) => item.sourceId === dataSourceId)
          .map((item) => ({ value: item.assetId, label: item.name || item.assetId })));
      })
      .catch(() => {
        if (seq !== tableSeq.current) return;
        setTableOptions([]);
      })
      .finally(() => {
        if (seq === tableSeq.current) setLoadingAssets(false);
      });
    return () => { tableSeq.current += 1; };
  }, [dataSourceId, kbId, open]);

  // 表 → 列（详情接口的 columns）。
  useEffect(() => {
    if (!open || !tableAssetId) {
      setColumnOptions([]);
      return;
    }
    const seq = ++columnSeq.current;
    void getAsset('table', tableAssetId)
      .then((detail) => {
        if (seq !== columnSeq.current) return;
        setColumnOptions((detail.columns ?? []).map((column) => ({
          value: column.name,
          label: column.displayName ? `${column.displayName} (${column.name})` : column.name,
        })));
      })
      .catch(() => {
        if (seq !== columnSeq.current) return;
        setColumnOptions([]);
      });
    return () => { columnSeq.current += 1; };
  }, [open, tableAssetId]);

  const resolvedAssetId = assetFallback
    ?? (dataSourceId && tableName
      ? buildQualityAssetId(dataSourceId, tableName, columnName || null)
      : '');

  const save = async () => {
    if (!canWrite || saving) return;
    if (!resolvedAssetId) {
      message.error(t('dataGovernance.quality.rule.assetRequired'));
      return;
    }
    let parsedDefinition: unknown;
    let parsedThreshold: unknown;
    try {
      parsedDefinition = JSON.parse(definition);
      parsedThreshold = JSON.parse(threshold);
    } catch {
      message.error(t('dataGovernance.quality.rule.jsonInvalid'));
      return;
    }
    if (
      typeof parsedDefinition !== 'object' || parsedDefinition === null
      || Array.isArray(parsedDefinition)
      || typeof parsedThreshold !== 'object' || parsedThreshold === null
      || Array.isArray(parsedThreshold)
    ) {
      message.error(t('dataGovernance.quality.rule.jsonObjectRequired'));
      return;
    }
    setSaving(true);
    try {
      const body: DgQualityRuleBody = {
        name,
        assetType: columnName ? 'column' : 'table',
        assetId: resolvedAssetId,
        dimension,
        severity,
        checkType,
        definition: parsedDefinition as Record<string, unknown>,
        threshold: parsedThreshold as Record<string, unknown>,
        enabled: enabled === 1 ? 1 : 0,
        ...(kbId ? { kbId } : {}),
      };
      if (rule) {
        await updateQualityRule(rule.id, {
          ...body,
          expectedRevision: rule.revision,
        });
      } else {
        await createQualityRule(body);
      }
      message.success(t('dataGovernance.quality.rule.saved'));
      onSaved();
      onClose();
    } catch (error) {
      message.error(
        error instanceof Error
          ? error.message
          : t('dataGovernance.quality.rule.saveFailed'),
      );
    } finally {
      setSaving(false);
    }
  };

  return (
    <Drawer
      open={open}
      width={640}
      title={rule
        ? t('dataGovernance.quality.rule.editTitle')
        : t('dataGovernance.quality.rule.createTitle')}
      onClose={onClose}
      destroyOnClose
      footer={(
        <Space style={{ float: 'right' }}>
          <Button onClick={onClose}>{t('common.cancel')}</Button>
          <Button type="primary" loading={saving} disabled={!canWrite} onClick={() => void save()}>
            {t('common.save')}
          </Button>
        </Space>
      )}
    >
      <Space direction="vertical" style={{ width: '100%' }} size="middle">
        <Input
          value={name}
          placeholder={t('dataGovernance.quality.rule.name')}
          onChange={(event) => setName(event.target.value)}
        />

        <Space direction="vertical" style={{ width: '100%' }} size="small">
          <Select
            showSearch
            allowClear
            optionFilterProp="label"
            value={kbId || undefined}
            style={{ width: '100%' }}
            placeholder={t('dataGovernance.quality.rule.selectKb')}
            options={kbOptions}
            onChange={(value) => {
              setKbId(value ?? '');
              setDataSourceId('');
              setTableAssetId('');
              setTableName('');
              setColumnName('');
              setAssetFallback(null);
            }}
          />
          {assetFallback ? (
            <>
              <Input
                value={assetFallback}
                disabled
                placeholder={t('dataGovernance.quality.rule.assetIdPlaceholder')}
              />
              <Alert type="info" showIcon message={t('dataGovernance.quality.rule.assetUnresolved')} />
            </>
          ) : (
            <>
              <Space.Compact style={{ width: '100%' }}>
                <Select
                  showSearch
                  allowClear
                  optionFilterProp="label"
                  value={dataSourceId || undefined}
                  style={{ width: 220 }}
                  disabled={!kbId}
                  placeholder={t('dataGovernance.quality.rule.selectDataSource')}
                  options={dataSourceOptions}
                  onChange={(value) => {
                    setDataSourceId(value ?? '');
                    setTableAssetId('');
                    setTableName('');
                    setColumnName('');
                    setAssetFallback(null);
                  }}
                />
                <Select
                  showSearch
                  allowClear
                  optionFilterProp="label"
                  value={tableAssetId || undefined}
                  style={{ width: 240 }}
                  disabled={!dataSourceId}
                  loading={loadingAssets}
                  placeholder={t('dataGovernance.quality.rule.selectTable')}
                  options={tableOptions}
                  onChange={(value) => {
                    const next = value ?? '';
                    setTableAssetId(next);
                    setTableName(next ? parseQualityAssetId(next, 'table').table : '');
                    setColumnName('');
                    setAssetFallback(null);
                  }}
                />
              </Space.Compact>
              <Select
                showSearch
                allowClear
                optionFilterProp="label"
                value={columnName || undefined}
                style={{ width: '100%' }}
                disabled={!tableAssetId}
                placeholder={t('dataGovernance.quality.rule.selectColumn')}
                options={columnOptions}
                onChange={(value) => {
                  setColumnName(value ?? '');
                  setAssetFallback(null);
                }}
              />
              {resolvedAssetId ? (
                <Tag color={columnName ? 'blue' : 'default'}>
                  {columnName
                    ? t('dataGovernance.quality.rule.column')
                    : t('dataGovernance.quality.rule.table')}
                  {' · '}
                  {resolvedAssetId}
                </Tag>
              ) : null}
            </>
          )}
        </Space>

        <Space wrap>
          <Select
            value={dimension}
            style={{ width: 140 }}
            options={QUALITY_DIMENSIONS.map((value) => ({
              value, label: t(dimensionLabelKey(value)),
            }))}
            onChange={setDimension}
          />
          <Select
            value={severity}
            style={{ width: 120 }}
            options={QUALITY_SEVERITIES.map((value) => ({
              value, label: t(severityLabelKey(value)),
            }))}
            onChange={setSeverity}
          />
          <Select
            value={checkType}
            style={{ width: 130 }}
            options={QUALITY_CHECK_TYPES.map((value) => ({
              value, label: t(checkTypeLabelKey(value)),
            }))}
            onChange={setCheckType}
          />
          <Space>
            <span>{t('dataGovernance.quality.rule.enabled')}</span>
            <Switch
              checked={enabled === 1}
              onChange={(checked) => setEnabled(checked ? 1 : 0)}
            />
          </Space>
        </Space>
        <div>
          <div className="yx-dg-quality-field-label">
            {t('dataGovernance.quality.rule.definition')}
          </div>
          <Input.TextArea
            rows={5}
            value={definition}
            onChange={(event) => setDefinition(event.target.value)}
          />
        </div>
        <div>
          <div className="yx-dg-quality-field-label">
            {t('dataGovernance.quality.rule.threshold')}
          </div>
          <Input.TextArea
            rows={3}
            value={threshold}
            onChange={(event) => setThreshold(event.target.value)}
          />
        </div>
        {rule ? (
          <InputNumber
            value={rule.revision}
            disabled
            addonBefore="revision"
            style={{ width: '100%' }}
          />
        ) : null}
      </Space>
    </Drawer>
  );
}
