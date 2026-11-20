import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import {
  Alert, Button, Checkbox, Empty, Input, Modal, Select, Space, Spin,
  Switch, Table, Tag, Tooltip, message,
} from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { ReloadOutlined, WarningOutlined } from '@ant-design/icons';
import {
  listOntologyAxioms, previewAxiomStatus, setAxiomStatus,
} from '@/api/reasoningRules';
import type {
  AxiomRef, AxiomStatusPreview, AxiomViewChange, OntologyAxiom,
  RuleIssue, RuleStatus,
} from '@/api/reasoningRules';

interface Props {
  kbId: string;
  canWrite?: boolean;
}

const STATUS_COLOR: Record<string, string> = {
  active: 'green',
  proposed: 'gold',
  rejected: 'red',
};

const STATUS_LABEL: Record<string, string> = {
  active: 'statusActive',
  proposed: 'statusProposed',
  rejected: 'statusRejected',
};

const KIND_OPTIONS = [
  'subClassOf', 'disjointWith', 'equivalentClass', 'subPropertyOf',
  'transitive', 'symmetric', 'functional', 'inverseFunctional',
  'reflexive', 'irreflexive', 'asymmetric', 'propertyChain',
  'datatypeFacet',
];

const SEVERITY_COLOR: Record<string, string> = {
  Violation: 'red',
  Warning: 'gold',
  Info: 'blue',
};

const refText = (ref?: AxiomRef | null): string => {
  if (!ref) return '-';
  return ref.owner ? `${ref.owner}.${ref.ref}` : ref.ref;
};

const AxiomTab: React.FC<Props> = ({ kbId, canWrite = false }) => {
  const { t } = useTranslation();
  const [items, setItems] = useState<OntologyAxiom[]>([]);
  const [loading, setLoading] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [kindFilter, setKindFilter] = useState<string>('all');
  const [statusFilter, setStatusFilter] = useState<string>('all');
  const [keyword, setKeyword] = useState('');

  const [togglingKey, setTogglingKey] = useState<string | null>(null);
  const [pending, setPending] = useState<{
    axiom: OntologyAxiom;
    target: 'active' | 'proposed';
    data: AxiomStatusPreview;
  } | null>(null);
  const [force, setForce] = useState(false);
  const [applying, setApplying] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setLoadError(null);
    try {
      const res = await listOntologyAxioms(kbId);
      setItems(res.items || []);
    } catch (err: any) {
      setLoadError(err?.message || t('domainKnowledge.axioms.loadFailed'));
    } finally {
      setLoading(false);
    }
  }, [kbId, t]);

  useEffect(() => {
    load();
  }, [load]);

  const filtered = useMemo(() => {
    const q = keyword.trim().toLowerCase();
    return items.filter((item) => {
      if (kindFilter !== 'all' && item.kind !== kindFilter) return false;
      if (statusFilter !== 'all' && item.status !== statusFilter) return false;
      if (!q) return true;
      const haystack = [
        item.axiom_key, item.kind, item.description || '',
        item.subject?.ref || '', item.subject?.owner || '',
        item.object?.ref || '', item.object?.owner || '',
      ].join(' ').toLowerCase();
      return haystack.includes(q);
    });
  }, [items, kindFilter, statusFilter, keyword]);

  const statusLabel = (status: RuleStatus) => (
    t(`domainKnowledge.axioms.${STATUS_LABEL[status] || 'statusProposed'}`)
  );

  const openToggle = async (record: OntologyAxiom) => {
    const target = record.status === 'active' ? 'proposed' : 'active';
    setTogglingKey(record.axiom_key);
    try {
      const data = await previewAxiomStatus(kbId, record.axiom_key, target);
      if (data.unchanged) {
        message.info(t('domainKnowledge.axioms.statusUnchanged'));
        return;
      }
      setForce(false);
      setPending({ axiom: record, target, data });
    } catch (err: any) {
      message.error(err?.message || t('domainKnowledge.axioms.previewFailed'));
    } finally {
      setTogglingKey(null);
    }
  };

  const applyToggle = async () => {
    if (!pending) return;
    setApplying(true);
    try {
      await setAxiomStatus(kbId, pending.axiom.axiom_key, pending.target, {
        force,
        expectedStatus: pending.axiom.status,
      });
      message.success(t('domainKnowledge.axioms.applySuccess'));
      setPending(null);
      await load();
    } catch (err: any) {
      message.error(err?.message || t('domainKnowledge.axioms.applyFailed'));
    } finally {
      setApplying(false);
    }
  };

  const renderChanges = (titleKey: string, changes: AxiomViewChange[]) => {
    if (!changes.length) return null;
    return (
      <div style={{ marginTop: 12 }}>
        <div style={{ fontWeight: 600, marginBottom: 4 }}>
          {t(`domainKnowledge.axioms.${titleKey}`)}
        </div>
        {changes.map((change) => (
          <div key={change.key} style={{ fontSize: 12, marginBottom: 4 }}>
            <code>{change.key}</code>
            {change.added.length > 0 && (
              <Tag color="green" style={{ marginLeft: 8 }}>
                + {change.added.join(', ')}
              </Tag>
            )}
            {change.removed.length > 0 && (
              <Tag color="red" style={{ marginLeft: 8 }}>
                - {change.removed.join(', ')}
              </Tag>
            )}
          </div>
        ))}
      </div>
    );
  };

  const renderIssues = (issues: RuleIssue[]) => (
    <Space size={[4, 4]} wrap>
      {issues.map((issue, index) => (
        <Tooltip key={`${issue.code}-${index}`} title={issue.code}>
          <Tag color={SEVERITY_COLOR[issue.severity] || 'default'}>
            {issue.code}
          </Tag>
        </Tooltip>
      ))}
    </Space>
  );

  const columns: ColumnsType<OntologyAxiom> = [
    {
      title: t('domainKnowledge.axioms.enabled'),
      width: 80,
      render: (_: unknown, record) => (
        <Switch
          size="small"
          checked={record.status === 'active'}
          loading={togglingKey === record.axiom_key}
          disabled={!canWrite}
          onChange={() => openToggle(record)}
        />
      ),
    },
    {
      title: t('domainKnowledge.axioms.key'),
      dataIndex: 'axiom_key',
      width: 200,
      render: (value: string) => <code>{value}</code>,
    },
    {
      title: t('domainKnowledge.axioms.kind'),
      dataIndex: 'kind',
      width: 150,
      render: (value: string) => <code>{value}</code>,
    },
    {
      title: t('domainKnowledge.axioms.subject'),
      dataIndex: 'subject',
      render: (value: AxiomRef) => refText(value),
    },
    {
      title: t('domainKnowledge.axioms.object'),
      dataIndex: 'object',
      render: (value: AxiomRef | null | undefined) => refText(value),
    },
    {
      title: t('domainKnowledge.axioms.status'),
      dataIndex: 'status',
      width: 100,
      render: (value: RuleStatus) => (
        <Tag color={STATUS_COLOR[value] || 'default'}>{statusLabel(value)}</Tag>
      ),
    },
    {
      title: t('domainKnowledge.axioms.description'),
      dataIndex: 'description',
      ellipsis: true,
      render: (value: string | null) => value || '-',
    },
    {
      title: t('domainKnowledge.axioms.updatedAt'),
      dataIndex: 'updated_at',
      width: 170,
      render: (value: string | null) => value || '-',
    },
  ];

  const notes = pending?.data.notes || [];
  const noteText: Record<string, string> = {
    display_only: t('domainKnowledge.axioms.noteDisplayOnly'),
    conflict_only: t('domainKnowledge.axioms.noteConflictOnly'),
    no_impact: t('domainKnowledge.axioms.noteNoImpact'),
  };

  return (
    <div>
      <Alert
        type="info"
        showIcon
        title={t('domainKnowledge.axioms.hint')}
        style={{ marginBottom: 12 }}
      />
      <Space wrap style={{ marginBottom: 12 }}>
        <Select
          value={kindFilter}
          onChange={setKindFilter}
          style={{ width: 180 }}
          options={[
            { value: 'all', label: t('domainKnowledge.axioms.allKinds') },
            ...KIND_OPTIONS.map((kind) => ({ value: kind, label: kind })),
          ]}
        />
        <Select
          value={statusFilter}
          onChange={setStatusFilter}
          style={{ width: 150 }}
          options={[
            { value: 'all', label: t('domainKnowledge.axioms.allStatuses') },
            { value: 'active', label: t('domainKnowledge.axioms.statusActive') },
            { value: 'proposed', label: t('domainKnowledge.axioms.statusProposed') },
            { value: 'rejected', label: t('domainKnowledge.axioms.statusRejected') },
          ]}
        />
        <Input
          allowClear
          value={keyword}
          onChange={(event) => setKeyword(event.target.value)}
          placeholder={t('domainKnowledge.axioms.searchPlaceholder')}
          style={{ width: 240 }}
        />
        <Button icon={<ReloadOutlined />} loading={loading} onClick={load}>
          {t('common.refresh')}
        </Button>
      </Space>

      {loadError ? (
        <Alert type="error" showIcon title={loadError} />
      ) : loading ? (
        <div style={{ padding: 48, textAlign: 'center' }}><Spin /></div>
      ) : filtered.length === 0 ? (
        <Empty description={t('domainKnowledge.axioms.empty')} />
      ) : (
        <Table
          rowKey="axiom_key"
          size="small"
          dataSource={filtered}
          columns={columns}
          pagination={{ pageSize: 20, showSizeChanger: true }}
        />
      )}

      <Modal
        title={pending?.target === 'active'
          ? t('domainKnowledge.axioms.enableTitle')
          : t('domainKnowledge.axioms.disableTitle')}
        open={!!pending}
        width={760}
        onCancel={() => setPending(null)}
        footer={(
          <Space>
            <Button onClick={() => setPending(null)}>
              {t('common.cancel')}
            </Button>
            <Button
              type="primary"
              loading={applying}
              disabled={!!pending?.data.has_violation && !force}
              onClick={applyToggle}
            >
              {t('domainKnowledge.axioms.confirm')}
            </Button>
          </Space>
        )}
      >
        {pending ? (
          <div>
            <Alert
              type="info"
              showIcon
              title={t('domainKnowledge.axioms.impactTitle')}
              description={(
                <div>
                  <div><code>{pending.axiom.kind}</code> · {pending.axiom.axiom_key}</div>
                  <div style={{ marginTop: 4 }}>
                    {t('domainKnowledge.axioms.impactSummary', {
                      entities: pending.data.impact.summary.entities,
                      relations: pending.data.impact.summary.relations,
                      attributes: pending.data.impact.summary.attributes,
                      characteristics: pending.data.impact.summary.characteristics,
                      rules: pending.data.affected_rules.length,
                    })}
                  </div>
                </div>
              )}
            />

            {notes.map((note) => (
              <Alert
                key={note}
                type="warning"
                showIcon
                style={{ marginTop: 8 }}
                title={noteText[note] || note}
              />
            ))}

            {(pending.data.new_axiom_issues || []).length > 0 ? (
              <div style={{ marginTop: 12 }}>
                <div style={{ fontWeight: 600, marginBottom: 4 }}>
                  {t('domainKnowledge.axioms.axiomIssues')}
                </div>
                {renderIssues(pending.data.new_axiom_issues || [])}
              </div>
            ) : null}

            {renderChanges('impactEntities', pending.data.impact.entities)}
            {renderChanges('impactRelations', pending.data.impact.relations)}
            {renderChanges('impactAttributes', pending.data.impact.attributes)}
            {renderChanges('impactCharacteristics', pending.data.impact.characteristics)}

            {pending.data.affected_rules.length > 0 ? (
              <div style={{ marginTop: 12 }}>
                <div style={{ fontWeight: 600, marginBottom: 4 }}>
                  {t('domainKnowledge.axioms.impactRules')}
                </div>
                <Table
                  rowKey="rule_key"
                  size="small"
                  pagination={false}
                  dataSource={pending.data.affected_rules}
                  columns={[
                    {
                      title: t('domainKnowledge.reasoningRules.rule'),
                      dataIndex: 'name',
                      render: (value: string, row) => (
                        <div>
                          <div>{value || row.rule_key}</div>
                          <div style={{ fontSize: 11, color: 'var(--yx-text-muted)' }}>
                            {row.rule_key}
                          </div>
                        </div>
                      ),
                    },
                    {
                      title: t('domainKnowledge.axioms.newIssues'),
                      render: (_: unknown, row) => renderIssues(row.new_issues),
                    },
                    {
                      title: t('domainKnowledge.axioms.resolvedIssues'),
                      render: (_: unknown, row) => renderIssues(row.resolved_issues),
                    },
                  ]}
                />
              </div>
            ) : (
              <div style={{ marginTop: 12, color: 'var(--yx-text-muted)', fontSize: 12 }}>
                {t('domainKnowledge.axioms.noAffectedRules')}
              </div>
            )}

            {pending.data.has_violation ? (
              <Alert
                type="error"
                showIcon
                icon={<WarningOutlined />}
                style={{ marginTop: 12 }}
                title={t('domainKnowledge.axioms.violationWarning')}
                description={(
                  <Checkbox
                    checked={force}
                    onChange={(event) => setForce(event.target.checked)}
                  >
                    {t('domainKnowledge.axioms.forceConfirm')}
                  </Checkbox>
                )}
              />
            ) : null}
          </div>
        ) : null}
      </Modal>
    </div>
  );
};

export default AxiomTab;
