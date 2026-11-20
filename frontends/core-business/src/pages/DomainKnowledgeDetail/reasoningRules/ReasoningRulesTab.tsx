import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import {
  Alert, Button, Drawer, Empty, Input, Modal, Popconfirm, Select, Space,
  Spin, Switch, Table, Tag, Tooltip, Upload, message,
} from 'antd';
import type { ColumnsType } from 'antd/es/table';
import {
  DeleteOutlined, EditOutlined, ExportOutlined, ImportOutlined,
  PlusOutlined, ReloadOutlined, WarningOutlined,
} from '@ant-design/icons';
import {
  createReasoningRule, deleteReasoningRule, exportReasoningRules,
  importReasoningRules, listReasoningRules,
  previewReasoning, setReasoningRuleEnabled, updateReasoningRule,
  validateReasoningRules,
} from '@/api/reasoningRules';
import type {
  PreviewDerived, PreviewResult, ReasoningRule,
  RuleImportResult, RuleIssue, RuleSavePayload, RuleStatus,
} from '@/api/reasoningRules';

interface Props {
  kbId: string;
  canWrite?: boolean;
}

const RULE_STATUS_COLOR: Record<RuleStatus, string> = {
  active: 'green',
  proposed: 'gold',
  rejected: 'default',
};

const ORIGIN_COLOR: Record<string, string> = {
  platform: 'blue',
  studio: 'purple',
  imported: 'default',
};

function downloadText(filename: string, text: string) {
  const blob = new Blob([text], { type: 'text/yaml;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}

const ReasoningRulesTab: React.FC<Props> = ({ kbId, canWrite = false }) => {
  const { t } = useTranslation();
  const [items, setItems] = useState<ReasoningRule[]>([]);
  const [loading, setLoading] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setLoadError(null);
    try {
      const ruleRes = await listReasoningRules(kbId);
      setItems(ruleRes.items || []);
    } catch (err: any) {
      setLoadError(err?.message || t('domainKnowledge.reasoningRules.loadFailed'));
    } finally {
      setLoading(false);
    }
  }, [kbId, t]);

  useEffect(() => {
    load();
  }, [load]);

  // ── 编辑 ──────────────────────────────────────────────
  const [editorOpen, setEditorOpen] = useState(false);
  const [editing, setEditing] = useState<ReasoningRule | null>(null);
  const [name, setName] = useState('');
  const [text, setText] = useState('');
  const [status, setStatus] = useState<RuleStatus>('active');
  const [enabled, setEnabled] = useState(true);
  const [editorIssues, setEditorIssues] = useState<RuleIssue[]>([]);
  const [validating, setValidating] = useState(false);
  const [saving, setSaving] = useState(false);
  const validateTimer = useRef<ReturnType<typeof setTimeout>>();

  const openCreate = () => {
    setEditing(null);
    setName('');
    setText('');
    setStatus('active');
    setEnabled(true);
    setEditorIssues([]);
    setEditorOpen(true);
  };

  const openEdit = (rule: ReasoningRule) => {
    setEditing(rule);
    setName(rule.name);
    setText(rule.text);
    setStatus(rule.status);
    setEnabled(rule.enabled);
    setEditorIssues(rule.issues || []);
    setEditorOpen(true);
  };

  useEffect(() => {
    if (!editorOpen || !text.trim()) {
      setEditorIssues([]);
      return;
    }
    if (validateTimer.current) clearTimeout(validateTimer.current);
    validateTimer.current = setTimeout(async () => {
      setValidating(true);
      try {
        const res = await validateReasoningRules(kbId, [{
          rule_key: editing?.rule_key || 'draft',
          name: name || 'draft',
          text,
          status,
          enabled,
        }]);
        setEditorIssues(res.issues || []);
      } catch (err: any) {
        // 校验接口失败不打断编辑；保存时还会再校验一次
      } finally {
        setValidating(false);
      }
    }, 400);
    return () => {
      if (validateTimer.current) clearTimeout(validateTimer.current);
    };
  }, [editorOpen, editing?.rule_key, name, text, status, enabled, kbId]);

  const save = async () => {
    if (!name.trim() || !text.trim()) {
      message.error(t('domainKnowledge.reasoningRules.nameTextRequired'));
      return;
    }
    setSaving(true);
    try {
      const payload: RuleSavePayload = {
        name: name.trim(),
        text,
        status,
        enabled,
        ...(editing ? {
          expected_content_revision: editing.content_revision,
        } : {}),
      };
      if (editing) {
        await updateReasoningRule(kbId, editing.rule_key, payload);
      } else {
        await createReasoningRule(kbId, payload);
      }
      message.success(t('domainKnowledge.reasoningRules.saveSuccess'));
      setEditorOpen(false);
      await load();
    } catch (err: any) {
      message.error(err?.message || t('domainKnowledge.reasoningRules.saveFailed'));
    } finally {
      setSaving(false);
    }
  };

  const toggleEnabled = async (rule: ReasoningRule, next: boolean) => {
    try {
      await setReasoningRuleEnabled(kbId, rule.rule_key, next);
      setItems((prev) => prev.map((item) => (
        item.rule_key === rule.rule_key ? { ...item, enabled: next } : item
      )));
    } catch (err: any) {
      message.error(err?.message || t('domainKnowledge.reasoningRules.toggleFailed'));
    }
  };

  const remove = async (rule: ReasoningRule) => {
    try {
      await deleteReasoningRule(kbId, rule.rule_key);
      message.success(t('domainKnowledge.reasoningRules.deleteSuccess'));
      await load();
    } catch (err: any) {
      message.error(err?.message || t('domainKnowledge.reasoningRules.deleteFailed'));
    }
  };

  // ── 试跑（T9）────────────────────────────────────────
  const [previewOpen, setPreviewOpen] = useState(false);
  const [previewLoading, setPreviewLoading] = useState(false);
  const [previewData, setPreviewData] = useState<PreviewResult | null>(null);

  const runPreviewSaved = async (rule: ReasoningRule) => {
    setPreviewLoading(true);
    try {
      const res = await previewReasoning(kbId, { ruleKeys: [rule.rule_key] });
      setPreviewData(res);
      setPreviewOpen(true);
    } catch (err: any) {
      message.error(err?.message || t('domainKnowledge.reasoningRules.previewFailed'));
    } finally {
      setPreviewLoading(false);
    }
  };

  const runPreviewEditor = async () => {
    if (!text.trim()) {
      message.error(t('domainKnowledge.reasoningRules.nameTextRequired'));
      return;
    }
    setPreviewLoading(true);
    try {
      const res = await previewReasoning(kbId, {
        rule: {
          rule_key: editing?.rule_key || 'draft',
          name: name || 'draft',
          text,
          status,
          enabled,
        },
      });
      setPreviewData(res);
      setPreviewOpen(true);
    } catch (err: any) {
      message.error(err?.message || t('domainKnowledge.reasoningRules.previewFailed'));
    } finally {
      setPreviewLoading(false);
    }
  };

  const previewRows = useMemo<PreviewDerived[]>(() => {
    return (previewData?.results || []).flatMap((result) => (
      (result.derived || []).map((item) => ({
        ...item,
        rule_key: item.rule_key || result.rule_key,
      }))
    ));
  }, [previewData]);

  const previewStats = useMemo(() => {
    const results = previewData?.results || [];
    return {
      total: results.length,
      derived: previewRows.length,
      unresolved: results.filter(
        (item) => item.status && item.status !== 'done',
      ),
    };
  }, [previewData, previewRows]);

  // ── 冲突报告 ──────────────────────────────────────────
  const [conflictsOpen, setConflictsOpen] = useState(false);
  const conflicts = useMemo(() => {
    const byKey = new Map(items.map((item) => [item.rule_key, item.name]));
    const seen = new Set<string>();
    const rows: { ruleKey: string; ruleName: string; issue: RuleIssue }[] = [];
    items.forEach((item) => {
      (item.issues || []).forEach((issue) => {
        if (issue.severity !== 'Violation' && issue.severity !== 'Warning') return;
        const signature = `${item.rule_key}|${issue.code}|${issue.target?.field || ''}`;
        if (seen.has(signature)) return;
        seen.add(signature);
        rows.push({
          ruleKey: item.rule_key,
          ruleName: byKey.get(item.rule_key) || item.rule_key,
          issue,
        });
      });
    });
    return rows;
  }, [items]);

  // ── 导入导出 ──────────────────────────────────────────
  const [importOpen, setImportOpen] = useState(false);
  const [importText, setImportText] = useState('');
  const [importMode, setImportMode] = useState<'merge' | 'replace'>('merge');
  const [importResult, setImportResult] = useState<RuleImportResult | null>(null);
  const [importing, setImporting] = useState(false);

  const dryRunImport = async () => {
    if (!importText.trim()) {
      message.error(t('domainKnowledge.reasoningRules.importEmpty'));
      return;
    }
    setImporting(true);
    try {
      const res = await importReasoningRules(kbId, importText, {
        dryRun: true, mode: importMode,
      });
      setImportResult(res);
    } catch (err: any) {
      message.error(err?.message || t('domainKnowledge.reasoningRules.importFailed'));
    } finally {
      setImporting(false);
    }
  };

  const confirmImport = async () => {
    if (!importResult?.plan_hash) {
      message.error(t('domainKnowledge.reasoningRules.importNeedDryRun'));
      return;
    }
    setImporting(true);
    try {
      await importReasoningRules(kbId, importText, {
        dryRun: false, mode: importMode, planHash: importResult.plan_hash,
      });
      message.success(t('domainKnowledge.reasoningRules.importSuccess'));
      setImportOpen(false);
      setImportText('');
      setImportResult(null);
      await load();
    } catch (err: any) {
      message.error(err?.message || t('domainKnowledge.reasoningRules.importFailed'));
    } finally {
      setImporting(false);
    }
  };

  const exportRules = async () => {
    try {
      const res = await exportReasoningRules(kbId);
      downloadText(res.filename || `${kbId}_reasoning_rules.yaml`, res.yaml_text || '');
    } catch (err: any) {
      message.error(err?.message || t('domainKnowledge.reasoningRules.exportFailed'));
    }
  };

  const columns: ColumnsType<ReasoningRule> = [
    {
      title: t('domainKnowledge.reasoningRules.name'),
      dataIndex: 'name',
      width: 180,
      render: (value: string, record) => (
        <Space orientation="vertical" size={0}>
          <span style={{ fontWeight: 500 }}>{value}</span>
          <Tooltip title={record.rule_key}>
            <span style={{ color: 'var(--yx-text-muted)', fontSize: 12 }}>{record.rule_key}</span>
          </Tooltip>
        </Space>
      ),
    },
    {
      title: t('domainKnowledge.reasoningRules.text'),
      dataIndex: 'text',
      ellipsis: true,
      render: (value: string) => (
        <Tooltip title={value}>
          <span style={{ fontFamily: 'monospace', fontSize: 12 }}>{value}</span>
        </Tooltip>
      ),
    },
    {
      title: t('domainKnowledge.reasoningRules.status'),
      dataIndex: 'status',
      width: 100,
      render: (value: RuleStatus) => (
        <Tag color={RULE_STATUS_COLOR[value] || 'default'}>
          {t(`domainKnowledge.reasoningRules.status.${value}`)}
        </Tag>
      ),
    },
    {
      title: t('domainKnowledge.reasoningRules.enabled'),
      dataIndex: 'enabled',
      width: 90,
      render: (value: boolean, record) => (
        <Switch
          size="small"
          checked={value}
          disabled={!canWrite}
          onChange={(next) => toggleEnabled(record, next)}
        />
      ),
    },
    {
      title: t('domainKnowledge.reasoningRules.issues'),
      width: 100,
      render: (_: unknown, record) => {
        const issues = record.issues || [];
        const violations = issues.filter((i) => i.severity === 'Violation').length;
        const warnings = issues.filter((i) => i.severity === 'Warning').length;
        if (!violations && !warnings) return <Tag color="green">OK</Tag>;
        return (
          <Button type="link" size="small" onClick={() => setConflictsOpen(true)}>
            <WarningOutlined />
            {violations ? ` ${violations}` : ''}
            {warnings ? ` ${t('domainKnowledge.reasoningRules.warnings', { count: warnings })}` : ''}
          </Button>
        );
      },
    },
    {
      title: t('domainKnowledge.reasoningRules.origin'),
      dataIndex: 'origin',
      width: 110,
      render: (value: string) => (
        <Tag color={ORIGIN_COLOR[value] || 'default'}>
          {t(`domainKnowledge.reasoningRules.origin.${value}`)}
        </Tag>
      ),
    },
    {
      title: t('domainKnowledge.reasoningRules.updatedBy'),
      dataIndex: 'updated_by',
      width: 120,
      render: (value: string | null | undefined) => value || '-',
    },
    {
      title: t('domainKnowledge.reasoningRules.actions'),
      width: 170,
      render: (_: unknown, record) => (
        <Space>
          <Button
            type="link"
            size="small"
            loading={previewLoading}
            onClick={() => runPreviewSaved(record)}
          >
            {t('domainKnowledge.reasoningRules.previewRun')}
          </Button>
          <Button
            type="link"
            size="small"
            icon={<EditOutlined />}
            disabled={!canWrite}
            onClick={() => openEdit(record)}
          >
            {t('domainKnowledge.reasoningRules.edit')}
          </Button>
          <Popconfirm
            title={t('domainKnowledge.reasoningRules.deleteConfirm')}
            onConfirm={() => remove(record)}
            disabled={!canWrite}
          >
            <Button type="link" size="small" danger icon={<DeleteOutlined />} disabled={!canWrite}>
              {t('domainKnowledge.reasoningRules.delete')}
            </Button>
          </Popconfirm>
        </Space>
      ),
    },
  ];

  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 12 }}>
        <Space>
          <Button
            type="primary"
            icon={<PlusOutlined />}
            disabled={!canWrite}
            onClick={openCreate}
          >
            {t('domainKnowledge.reasoningRules.create')}
          </Button>
          <Button icon={<ReloadOutlined />} onClick={load} loading={loading}>
            {t('domainKnowledge.reasoningRules.refresh')}
          </Button>
          <Button icon={<WarningOutlined />} onClick={() => setConflictsOpen(true)}>
            {t('domainKnowledge.reasoningRules.conflictReport')}
          </Button>
        </Space>
        <Space>
          <Button icon={<ImportOutlined />} onClick={() => setImportOpen(true)} disabled={!canWrite}>
            {t('domainKnowledge.reasoningRules.import')}
          </Button>
          <Button icon={<ExportOutlined />} onClick={exportRules}>
            {t('domainKnowledge.reasoningRules.export')}
          </Button>
        </Space>
      </div>

      {loadError ? (
        <Alert
          type="error"
          showIcon
          title={loadError}
          action={<Button size="small" onClick={load}>{t('common.retry')}</Button>}
        />
      ) : loading ? (
        <div style={{ padding: 48, textAlign: 'center' }}><Spin /></div>
      ) : items.length === 0 ? (
        <Empty description={t('domainKnowledge.reasoningRules.empty')} />
      ) : (
        <Table
          rowKey="rule_key"
          size="small"
          columns={columns}
          dataSource={items}
          pagination={{ pageSize: 20, showSizeChanger: true }}
        />
      )}

      <Drawer
        title={editing
          ? t('domainKnowledge.reasoningRules.editTitle')
          : t('domainKnowledge.reasoningRules.createTitle')}
        size="large"
        open={editorOpen}
        onClose={() => setEditorOpen(false)}
        destroyOnHidden
        extra={(
          <Space>
            <Button loading={previewLoading} onClick={runPreviewEditor}>
              {t('domainKnowledge.reasoningRules.previewRun')}
            </Button>
            <Button onClick={() => setEditorOpen(false)}>{t('common.cancel')}</Button>
            <Button type="primary" loading={saving} onClick={save} disabled={!canWrite}>
              {t('common.save')}
            </Button>
          </Space>
        )}
      >
        <Space orientation="vertical" size="middle" style={{ width: '100%' }}>
          <div>
            <div style={{ marginBottom: 4 }}>{t('domainKnowledge.reasoningRules.name')}</div>
            <Input value={name} onChange={(e) => setName(e.target.value)} maxLength={128} />
          </div>
          <Space>
            <div>
              <div style={{ marginBottom: 4 }}>{t('domainKnowledge.reasoningRules.status')}</div>
              <Select
                value={status}
                onChange={setStatus}
                style={{ width: 140 }}
                options={(['active', 'proposed', 'rejected'] as RuleStatus[]).map((s) => ({
                  value: s,
                  label: t(`domainKnowledge.reasoningRules.status.${s}`),
                }))}
              />
            </div>
            {!editing && (
              <div>
                <div style={{ marginBottom: 4 }}>{t('domainKnowledge.reasoningRules.enabled')}</div>
                <Switch checked={enabled} onChange={setEnabled} />
              </div>
            )}
          </Space>
          <div>
            <div style={{ marginBottom: 4 }}>{t('domainKnowledge.reasoningRules.ruleText')}</div>
            <Input.TextArea
              value={text}
              onChange={(e) => setText(e.target.value)}
              autoSize={{ minRows: 5, maxRows: 12 }}
              placeholder={t('domainKnowledge.reasoningRules.textPlaceholder')}
              style={{ fontFamily: 'monospace' }}
            />
            <div style={{ color: 'var(--yx-text-muted)', fontSize: 12, marginTop: 4 }}>
              {t('domainKnowledge.reasoningRules.textHint')}
            </div>
          </div>
          {validating && <Spin size="small" />}
          {editorIssues.length > 0 && (
            <Alert
              type={editorIssues.some((i) => i.severity === 'Violation') ? 'error' : 'warning'}
              showIcon
              title={t('domainKnowledge.reasoningRules.issuePreview')}
              description={(
                <ul style={{ margin: 0, paddingLeft: 18 }}>
                  {editorIssues.map((issue, idx) => (
                    <li key={`${issue.code}-${idx}`}>
                      <code>{issue.code}</code> ({issue.severity})
                      {issue.target?.field ? ` @${issue.target.field}` : ''}
                    </li>
                  ))}
                </ul>
              )}
            />
          )}
        </Space>
      </Drawer>

      <Drawer
        title={t('domainKnowledge.reasoningRules.conflictReport')}
        size="large"
        open={conflictsOpen}
        onClose={() => setConflictsOpen(false)}
      >
        {conflicts.length === 0 ? (
          <Empty description={t('domainKnowledge.reasoningRules.noConflicts')} />
        ) : (
          <Table
            rowKey={(row) => `${row.ruleKey}-${row.issue.code}-${row.issue.target?.field || ''}`}
            size="small"
            pagination={false}
            dataSource={conflicts}
            columns={[
              { title: t('domainKnowledge.reasoningRules.name'), dataIndex: 'ruleName' },
              { title: t('domainKnowledge.reasoningRules.issueCode'), dataIndex: ['issue', 'code'] },
              {
                title: t('domainKnowledge.reasoningRules.severity'),
                dataIndex: ['issue', 'severity'],
                render: (value: string) => (
                  <Tag color={value === 'Violation' ? 'red' : value === 'Warning' ? 'gold' : 'default'}>
                    {value}
                  </Tag>
                ),
              },
              {
                title: t('domainKnowledge.reasoningRules.location'),
                render: (_: unknown, row) => row.issue.target?.field || '-',
              },
            ]}
          />
        )}
      </Drawer>

      <Modal
        title={t('domainKnowledge.reasoningRules.import')}
        open={importOpen}
        width={720}
        onCancel={() => setImportOpen(false)}
        footer={[
          <Button key="cancel" onClick={() => setImportOpen(false)}>{t('common.cancel')}</Button>,
          <Button key="dry" loading={importing} onClick={dryRunImport}>
            {t('domainKnowledge.reasoningRules.dryRun')}
          </Button>,
          <Button
            key="ok"
            type="primary"
            loading={importing}
            disabled={
              !importResult?.plan_hash
              || (importResult?.issues || []).some((i) => i.severity === 'Violation')
            }
            onClick={confirmImport}
          >
            {t('domainKnowledge.reasoningRules.confirmImport')}
          </Button>,
        ]}
      >
        <Space orientation="vertical" size="middle" style={{ width: '100%' }}>
          <Space>
            <span>{t('domainKnowledge.reasoningRules.importMode')}</span>
            <Select
              value={importMode}
              onChange={setImportMode}
              style={{ width: 160 }}
              options={[
                { value: 'merge', label: 'merge' },
                { value: 'replace', label: 'replace' },
              ]}
            />
            <Upload
              accept=".yaml,.yml"
              showUploadList={false}
              beforeUpload={(file) => {
                const reader = new FileReader();
                reader.onload = () => setImportText(String(reader.result || ''));
                reader.readAsText(file);
                return false;
              }}
            >
              <Button size="small">{t('domainKnowledge.reasoningRules.chooseFile')}</Button>
            </Upload>
          </Space>
          <Input.TextArea
            value={importText}
            onChange={(e) => {
              setImportText(e.target.value);
              setImportResult(null);
            }}
            autoSize={{ minRows: 10, maxRows: 20 }}
            placeholder={t('domainKnowledge.reasoningRules.importPlaceholder')}
            style={{ fontFamily: 'monospace' }}
          />
          {importResult && (
            <Alert
              type={(importResult.issues || []).some((i) => i.severity === 'Violation')
                ? 'error' : 'info'}
              showIcon
              title={t('domainKnowledge.reasoningRules.dryRunResult')}
              description={(
                <div>
                  <div>
                    {Object.entries(importResult.summary || {})
                      .map(([key, value]) => `${key}: ${value}`)
                      .join(' · ')}
                  </div>
                  {(importResult.issues || []).length > 0 && (
                    <ul style={{ margin: '8px 0 0', paddingLeft: 18 }}>
                      {importResult.issues!.slice(0, 20).map((issue, idx) => (
                        <li key={`${issue.code}-${idx}`}>
                          <code>{issue.code}</code> ({issue.severity})
                        </li>
                      ))}
                    </ul>
                  )}
                </div>
              )}
            />
          )}
        </Space>
      </Modal>

      <Modal
        title={t('domainKnowledge.reasoningRules.previewTitle')}
        open={previewOpen}
        width={860}
        footer={<Button onClick={() => setPreviewOpen(false)}>{t('common.cancel')}</Button>}
        onCancel={() => setPreviewOpen(false)}
      >
        <Alert
          type="info"
          showIcon
          title={t('domainKnowledge.reasoningRules.previewDisclaimer')}
          style={{ marginBottom: 12 }}
        />
        {previewData ? (
          <div style={{ color: 'var(--yx-text-muted)', fontSize: 12, marginBottom: 8 }}>
            {t('domainKnowledge.reasoningRules.previewSummary', {
              total: previewStats.total,
              count: previewStats.derived,
            })}
          </div>
        ) : null}
        {previewRows.length === 0 ? (
          previewStats.unresolved.length > 0 ? (
            <Alert
              type="warning"
              showIcon
              title={t('domainKnowledge.reasoningRules.previewUnresolved', {
                count: previewStats.unresolved.length,
              })}
              description={(
                <ul style={{ margin: 0, paddingLeft: 20 }}>
                  {previewStats.unresolved.map((item, index) => (
                    <li key={`${item.rule_key || 'r'}-${index}`}>
                      {item.rule_key || '-'}
                      {item.reason
                        ? `（${t('domainKnowledge.reasoningRules.previewReason')}: ${item.reason}）`
                        : ''}
                    </li>
                  ))}
                </ul>
              )}
            />
          ) : (
            <>
              <Empty description={t('domainKnowledge.reasoningRules.previewNoFacts')} />
              {previewData ? (
                <div style={{
                  color: 'var(--yx-text-muted)',
                  fontSize: 12,
                  textAlign: 'center',
                }}>
                  {t('domainKnowledge.reasoningRules.previewNoFactsHint')}
                </div>
              ) : null}
            </>
          )
        ) : (
          <Table
            rowKey={(row, index) => `${row.rule_key || 'r'}-${index}`}
            size="small"
            pagination={{ pageSize: 20 }}
            dataSource={previewRows}
            columns={[
              { title: t('domainKnowledge.reasoningRules.rule'), dataIndex: 'rule_key', width: 120 },
              { title: t('domainKnowledge.reasoningRules.subject'), dataIndex: 's' },
              { title: t('domainKnowledge.reasoningRules.predicate'), dataIndex: 'p' },
              { title: t('domainKnowledge.reasoningRules.object'), dataIndex: 'o' },
              {
                title: t('domainKnowledge.reasoningRules.premiseDocs'),
                render: (_: unknown, row) => (row.premise?.doc_ids || []).join(', ') || '-',
              },
              {
                title: t('domainKnowledge.reasoningRules.alreadyAsserted'),
                dataIndex: 'already_asserted',
                width: 110,
                render: (value: boolean) => (value ? '✓' : '-'),
              },
            ]}
          />
        )}
        {(previewData?.skipped || []).length > 0 && (
          <Alert
            type="warning"
            showIcon
            style={{ marginTop: 12 }}
            title={t('domainKnowledge.reasoningRules.previewSkipped', {
              count: previewData?.skipped?.length || 0,
            })}
          />
        )}
      </Modal>
    </div>
  );
};

export default ReasoningRulesTab;
