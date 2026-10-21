import { useEffect, useState } from 'react';
import { Alert, Checkbox, Modal, Select, Space, message } from 'antd';
import { useTranslation } from 'react-i18next';
import {
  fetchTemplateDomains,
  fetchTemplateScenarios,
  type TemplateDomain,
  type TemplateScenario,
} from '@/api/templateImport';
import { reseedOntologyCompiledSchema } from '@/api/domainKnowledge';

interface Props {
  open: boolean;
  kbId: string;
  /** 当前已绑定的场景（compiled schema 上的 template_scenario_id），可为空。 */
  currentScenarioId?: string | null;
  onClose: () => void;
  onBound: () => void;
}

/**
 * 知识库绑定模板场景。
 *
 * 与「从模板导入」不同：绑定会写 `ontology_template_bindings`，模板场景发布时
 * 该 KB 会被标记 schema 过期；落标校验也能按场景统计到它。
 * 代价：reseed 会**强制重建** compiled schema，丢弃 KB 侧人工编辑。
 */
export default function TemplateBindModal({
  open,
  kbId,
  currentScenarioId,
  onClose,
  onBound,
}: Props) {
  const { t } = useTranslation();
  const [domains, setDomains] = useState<TemplateDomain[]>([]);
  const [scenarios, setScenarios] = useState<TemplateScenario[]>([]);
  const [domainId, setDomainId] = useState<string | undefined>();
  const [scenarioId, setScenarioId] = useState<string | undefined>();
  const [loadingDomains, setLoadingDomains] = useState(false);
  const [loadingScenarios, setLoadingScenarios] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [applyToDocuments, setApplyToDocuments] = useState(false);

  useEffect(() => {
    if (!open) return;
    setDomainId(undefined);
    setScenarioId(currentScenarioId ?? undefined);
    setScenarios([]);
    setApplyToDocuments(false);
    let cancelled = false;
    setLoadingDomains(true);
    fetchTemplateDomains()
      .then((res: { items: TemplateDomain[] }) => {
        if (!cancelled) setDomains(res.items ?? []);
      })
      .catch((err: unknown) => {
        if (!cancelled) {
          message.error(
            (err as { message?: string })?.message ||
              t('compile.binding.loadDomainsFailed'),
          );
        }
      })
      .finally(() => {
        if (!cancelled) setLoadingDomains(false);
      });
    return () => {
      cancelled = true;
    };
  }, [open, currentScenarioId, t]);

  useEffect(() => {
    if (!open || !domainId) {
      setScenarios([]);
      return;
    }
    let cancelled = false;
    setLoadingScenarios(true);
    fetchTemplateScenarios(domainId)
      .then((res: { items: TemplateScenario[] }) => {
        if (!cancelled) setScenarios(res.items ?? []);
      })
      .catch((err: unknown) => {
        if (!cancelled) {
          message.error(
            (err as { message?: string })?.message ||
              t('compile.binding.loadScenariosFailed'),
          );
        }
      })
      .finally(() => {
        if (!cancelled) setLoadingScenarios(false);
      });
    return () => {
      cancelled = true;
    };
  }, [open, domainId, t]);

  const handleSubmit = async () => {
    if (!scenarioId) {
      message.warning(t('compile.binding.selectScenarioFirst'));
      return;
    }
    setSubmitting(true);
    try {
      await reseedOntologyCompiledSchema(
        kbId,
        scenarioId,
        domainId,
        applyToDocuments,
      );
      message.success(t('compile.binding.success'));
      onBound();
      onClose();
    } catch (err: unknown) {
      message.error(
        (err as { message?: string })?.message || t('compile.binding.failed'),
      );
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Modal
      open={open}
      title={t('compile.binding.title')}
      okText={t('compile.binding.confirm')}
      cancelText={t('common.cancel')}
      confirmLoading={submitting}
      onOk={handleSubmit}
      onCancel={onClose}
      destroyOnHidden
    >
      <Space direction="vertical" size={12} style={{ width: '100%' }}>
        <Alert type="warning" showIcon title={t('compile.binding.warning')} />
        {currentScenarioId && (
          <Alert
            type="info"
            showIcon
            title={t('compile.binding.currentBinding', {
              scenario: currentScenarioId,
            })}
          />
        )}
        <div>
          <div style={{ marginBottom: 4 }}>{t('compile.binding.domain')}</div>
          <Select
            style={{ width: '100%' }}
            loading={loadingDomains}
            value={domainId}
            placeholder={t('compile.binding.domainPlaceholder')}
            options={domains.map((domain) => ({
              value: domain.id,
              label: domain.name,
            }))}
            onChange={(value) => {
              setDomainId(value);
              setScenarioId(undefined);
            }}
          />
        </div>
        <div>
          <div style={{ marginBottom: 4 }}>{t('compile.binding.scenario')}</div>
          <Select
            style={{ width: '100%' }}
            loading={loadingScenarios}
            value={scenarioId}
            disabled={!domainId}
            placeholder={
              domainId
                ? t('compile.binding.scenarioPlaceholder')
                : t('compile.binding.selectDomainFirst')
            }
            options={scenarios.map((scenario) => ({
              value: scenario.id,
              label: scenario.name,
            }))}
            onChange={setScenarioId}
          />
        </div>
        <Checkbox
          checked={applyToDocuments}
          onChange={(event) => setApplyToDocuments(event.target.checked)}
        >
          {t('compile.binding.applyToDocuments')}
        </Checkbox>
      </Space>
    </Modal>
  );
}
