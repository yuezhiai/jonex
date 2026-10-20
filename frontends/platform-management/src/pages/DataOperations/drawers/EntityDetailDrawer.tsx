import { useCallback } from 'react';
import { Alert, Flex, Tag, Typography } from 'antd';
import type { DescriptionsProps } from 'antd';
import { useTranslation } from 'react-i18next';
import { getOntologyEntityDetail } from '@/api/dataOperations';
import { DASH } from './format';
import { DetailHeader, DetailSection, NameTags } from './fields';
import DetailDrawer from './DetailDrawer';
import useDetail from './useDetail';
import type { DoEntityDetail, DoEntityTarget, DoWindow } from '../types';

const { Text } = Typography;

interface Props {
  target: DoEntityTarget | null;
  spaceId: string | null;
  window: DoWindow;
  onClose: () => void;
}

/**
 * 抽屉 B · Ontology 对象详情。
 *
 * ⚠️ 定位键是三元组（`kbId` + `entityType` + `canonicalName`），列表行上的 `id` 是
 * 单向哈希、查不了详情 —— 所以列表里 `kbId` 为 null 的那几行本来就不可点（不在本组件范围）。
 *
 * `degraded` 时属性与关系为空是**取不到**而不是「没有」，两者渲染必须分开：
 * 这里给一条提示条 + 属性/关系区的空态文案会写成「暂不可展示」。而命中次数来自 PG，
 * 降级时照常显示。
 */
export default function EntityDetailDrawer({ target, spaceId, window, onClose }: Props) {
  const { t } = useTranslation();
  const load = useCallback((item: DoEntityTarget) => getOntologyEntityDetail(item, spaceId, window), [spaceId, window]);
  const { data, loading, error, retry } = useDetail(target, load);

  const basicItems: DescriptionsProps['items'] = data
    ? [
        { key: 'kb', label: t('dataOperations.drawer.belongKb'), children: data.kbName ?? DASH },
        { key: 'createdAt', label: t('dataOperations.drawer.createdAt'), children: data.createdAt ?? DASH },
        { key: 'updatedAt', label: t('dataOperations.drawer.updatedAt'), children: data.updatedAt ?? DASH },
      ]
    : [];

  // 别名与描述都可能很长，各占满整行，不挤进两列网格
  const profileItems: DescriptionsProps['items'] = data
    ? [
        {
          key: 'aliases',
          label: t('dataOperations.drawer.aliases'),
          span: 2,
          children: data.aliases.length ? (
            <Flex wrap gap={8}>
              {data.aliases.map((alias) => (
                <Tag key={alias}>{alias}</Tag>
              ))}
            </Flex>
          ) : (
            DASH
          ),
        },
        {
          key: 'description',
          label: t('dataOperations.drawer.description'),
          span: 2,
          children: <span style={{ whiteSpace: 'pre-wrap' }}>{data.description || DASH}</span>,
        },
      ]
    : [];

  const attributeRows = data?.attributes ?? [];

  const usageItems: DescriptionsProps['items'] = data
    ? [
        {
          key: 'hitCount',
          label: t('dataOperations.drawer.hitCount'),
          children: <span className="yx-do-num">{data.hitCount.toLocaleString()}</span>,
        },
        {
          key: 'adoptCount',
          label: t('dataOperations.drawer.adoptCount'),
          children: <span className="yx-do-num">{data.adoptedCount.toLocaleString()}</span>,
        },
        {
          key: 'lastUsedAt',
          label: t('dataOperations.drawer.lastUsedAt'),
          children: data.lastUsedAt ?? <Text type="secondary">{t('dataOperations.common.neverUsed')}</Text>,
        },
      ]
    : [];

  const relationEmpty =
    data?.degraded === true ? t('dataOperations.drawer.relationsDegraded') : t('dataOperations.drawer.relationsEmpty');

  return (
    <DetailDrawer
      open={target !== null}
      title={t('dataOperations.drawer.entityTitle')}
      onClose={onClose}
      loading={loading}
      error={error}
      onRetry={retry}
    >
      {data && (
        <div className="yx-do-detail">
          <DetailHeader
            title={data.canonicalName}
            meta={`${t('dataOperations.drawer.entityType')}: ${data.entityType || DASH}`}
          />

          {/* 降级只影响属性与关系；命中次数在 PG，照常显示 —— 提示条要说到这一点，免得用户以为整页都不可信 */}
          {data.degraded && <Alert type="warning" showIcon message={t('dataOperations.drawer.degraded')} />}

          <DetailSection title={t('dataOperations.drawer.sectionBasic')} items={basicItems} />

          <DetailSection title={t('dataOperations.drawer.sectionAliases')} items={profileItems} />

          <DetailSection title={t('dataOperations.drawer.attributes')}>
            {attributeRows.length ? (
              <dl className="yx-do-attr-list">
                {attributeRows.map((attribute) => (
                  <div key={attribute.key} className="yx-do-attr-card">
                    <dt className="yx-do-attr-card-key">{attribute.key}</dt>
                    <dd className="yx-do-attr-card-value">{attribute.value || DASH}</dd>
                  </div>
                ))}
              </dl>
            ) : (
              <Text type="secondary">
                {data.degraded
                  ? t('dataOperations.drawer.attributesDegraded')
                  : t('dataOperations.drawer.attributesEmpty')}
              </Text>
            )}
          </DetailSection>

          <DetailSection
            title={
              <Flex align="center" gap={8}>
                {t('dataOperations.drawer.relations')}
                <Text type="secondary" style={{ fontWeight: 400, fontSize: 13 }}>
                  {t('dataOperations.drawer.relationTotal', { total: data.relationTotal })}
                </Text>
              </Flex>
            }
          >
            {data.relationGroups.length ? (
              <Flex vertical gap={12}>
                {data.relationGroups.map((group) => (
                  <Flex key={group.relationType} vertical gap={6}>
                    <Text strong>{group.relationType}</Text>
                    <NameTags
                      items={group.targets.map((item) => ({
                        key: `${item.canonicalName}|${item.entityType ?? ''}`,
                        label: item.canonicalName,
                        hint: item.entityType ?? undefined,
                      }))}
                      empty={relationEmpty}
                    />
                  </Flex>
                ))}
                {/* 邻居拉取有上限，条数与总数对不上时得说清楚是截断而不是丢数据 */}
                {data.relationTruncated && (
                  <Text type="secondary">
                    {t('dataOperations.drawer.relationTruncated', { shown: countRelationTargets(data) })}
                  </Text>
                )}
              </Flex>
            ) : (
              <Text type="secondary">{relationEmpty}</Text>
            )}
          </DetailSection>

          <DetailSection title={t('dataOperations.drawer.sectionUsage')} items={usageItems} />
        </div>
      )}
    </DetailDrawer>
  );
}

/** 已展开的关系条数。后端只给「被截断」这个布尔，不给实际条数，所以这里自己数 */
function countRelationTargets(data: DoEntityDetail): number {
  return data.relationGroups.reduce((sum, group) => sum + group.targets.length, 0);
}
