import { Button, Descriptions, Drawer, Space } from 'antd';
import { useTranslation } from 'react-i18next';
import { formatDateTime } from '@jonex/shared-lib';
import type { DgKbSchema } from '../standardTypes';
import { displayNameOrId } from '../standardUtils';
import { BindingStatusTag, DocStatsCell } from './StandardCatalogBits';

interface Props {
  open: boolean;
  row: DgKbSchema | null;
  onClose: () => void;
  onViewScenario?: (scenarioId: string) => void;
}

/** 知识库应用详情：只读展示绑定/编译与文档统计，不提供任何写操作。 */
export default function KbSchemaDrawer({ open, row, onClose, onViewScenario }: Props) {
  const { t } = useTranslation();
  // 窄化后存成常量，闭包里才能保持非空类型（不必用断言）。
  const scenarioId = row?.scenarioId ?? null;
  const templateUrl = `/apps/ecosystem-management/template-domains?domain_id=${encodeURIComponent(
    row?.domainId ?? '',
  )}`;

  return (
    <Drawer
      open={open}
      onClose={onClose}
      width={680}
      destroyOnHidden
      title={row?.kbName || t('dataGovernance.standards.kbTab')}
    >
      {!row ? null : (
        <Space direction="vertical" size={16} style={{ width: '100%' }}>
          <Descriptions column={2} size="small">
            <Descriptions.Item label={t('dataGovernance.standards.kb')}>
              {row.kbName}
            </Descriptions.Item>
            <Descriptions.Item label={t('dataGovernance.standards.spaceId')}>
              {row.spaceId || '—'}
            </Descriptions.Item>
            <Descriptions.Item label={t('dataGovernance.standards.domainScenario')}>
              {/* 名称缺失时回退对应 id（有 id 就不显示占位符），两者都无才显示 —。 */}
              {`${displayNameOrId(row.domainName, row.domainId)} / ${displayNameOrId(
                row.scenarioName,
                row.scenarioId,
              )}`}
            </Descriptions.Item>
            <Descriptions.Item label={t('dataGovernance.standards.bindingStatus')}>
              <BindingStatusTag
                bindingStatus={row.bindingStatus}
                bindingScenarioId={row.bindingScenarioId}
                compiledScenarioId={row.compiledScenarioId}
              />
            </Descriptions.Item>
            <Descriptions.Item label={t('dataGovernance.standards.sourceType')}>
              {row.sourceType || '—'}
            </Descriptions.Item>
            <Descriptions.Item label={t('dataGovernance.standards.sourceHash')}>
              {row.sourceHash ? (
                <code style={{ wordBreak: 'break-all' }}>{row.sourceHash}</code>
              ) : (
                '—'
              )}
            </Descriptions.Item>
            <Descriptions.Item label={t('dataGovernance.standards.sourceVersion')}>
              {row.sourceVersion ?? '—'}
            </Descriptions.Item>
            <Descriptions.Item label={t('dataGovernance.standards.schemaVersion')}>
              {row.schemaVersion ?? '—'}
            </Descriptions.Item>
            <Descriptions.Item label={t('dataGovernance.standards.schemaMode')}>
              {row.schemaMode || '—'}
            </Descriptions.Item>
            <Descriptions.Item label={t('dataGovernance.standards.syncStatus')}>
              {row.syncStatus || '—'}
            </Descriptions.Item>
            <Descriptions.Item label={t('dataGovernance.standards.compiledAt')}>
              {row.compiledAt ? formatDateTime(row.compiledAt) : '—'}
            </Descriptions.Item>
          </Descriptions>

          <div>
            <div style={{ fontWeight: 600, marginBottom: 8 }}>
              {t('dataGovernance.standards.docStats')}
            </div>
            <DocStatsCell stats={row.docStats} />
          </div>

          <Space size={12} wrap>
            {scenarioId && onViewScenario && (
              <Button onClick={() => onViewScenario(scenarioId)}>
                {t('dataGovernance.standards.viewScenario')}
              </Button>
            )}
            {row.domainId && (
              <a href={templateUrl} target="_blank" rel="noreferrer">
                {t('dataGovernance.standards.openInTemplate')}
              </a>
            )}
          </Space>
        </Space>
      )}
    </Drawer>
  );
}
