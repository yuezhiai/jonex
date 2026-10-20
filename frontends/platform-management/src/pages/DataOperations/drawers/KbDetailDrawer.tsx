import { useCallback } from 'react';
import { Typography } from 'antd';
import type { DescriptionsProps } from 'antd';
import { useTranslation } from 'react-i18next';
import { KB_TYPE_KEYS, getKbDetail } from '@/api/dataOperations';
import { DO_ASSET_COMPOSITION } from '@/constants/dataOperations';
import ProportionList from '../components/ProportionList';
import { DASH, translateCode } from './format';
import { DetailHeader, DetailSection, NameTags } from './fields';
import DetailDrawer from './DetailDrawer';
import useDetail from './useDetail';
import type { DoWindow } from '../types';

const { Text } = Typography;

interface Props {
  /** 非空即打开；`kb_id` 直接来自「知识库使用情况」那一行 */
  kbId: string | null;
  spaceId: string | null;
  window: DoWindow;
  onClose: () => void;
}

/**
 * 抽屉 C · 知识库详情。
 *
 * ⚠️ 无权限与不存在在本接口是**同一个 404**（防探测），所以失败提示走 DetailDrawer 的
 * 统一说法「不存在或无权访问」，不要在这里写「无权限」。
 *
 * 本体计数 `null` 与 `0` 必须分开：`null` = 取不到（显示 `—`），`0` = 库里真的是 0。
 * 透出 0 等于对用户谎称「这个库没有对象」。取不到时不加任何说明文案 —— 空值就是空值。
 */
export default function KbDetailDrawer({ kbId, spaceId, window, onClose }: Props) {
  const { t } = useTranslation();
  const load = useCallback((id: string) => getKbDetail(id, spaceId, window), [spaceId, window]);
  const { data, loading, error, retry } = useDetail(kbId, load);

  /** 本体计数：取不到就给一个 `—`，不解释原因（`0` 与取不到都按同一个空值样式走） */
  const ontologyValue = (value: number | null) =>
    value == null ? DASH : <span className="yx-do-num">{value.toLocaleString()}</span>;

  const basicItems: DescriptionsProps['items'] = data
    ? [
        {
          key: 'kbType',
          label: t('dataOperations.drawer.kbTypeLabel'),
          children: translateCode(t, KB_TYPE_KEYS, data.kbType),
        },
        {
          key: 'assetTotal',
          label: t('dataOperations.drawer.assetTotal'),
          children: <span className="yx-do-num">{data.assetTotal.toLocaleString()}</span>,
        },
        { key: 'updatedAt', label: t('dataOperations.drawer.updatedAt'), children: data.updatedAt ?? DASH },
      ]
    : [];

  const ontologyItems: DescriptionsProps['items'] = data
    ? [
        {
          key: 'objectInstances',
          label: t('dataOperations.drawer.objectInstances'),
          children: ontologyValue(data.ontology.objectInstances),
        },
        {
          key: 'relationInstances',
          label: t('dataOperations.drawer.relationInstances'),
          children: ontologyValue(data.ontology.relationInstances),
        },
      ]
    : [];

  const usageItems: DescriptionsProps['items'] = data
    ? [
        {
          key: 'requestCount',
          label: t('dataOperations.drawer.requestCount'),
          children: <span className="yx-do-num">{data.requestCount.toLocaleString()}</span>,
        },
        {
          key: 'lastRequestAt',
          label: t('dataOperations.drawer.lastRequestAt'),
          children: data.lastRequestAt ?? <Text type="secondary">{t('dataOperations.common.neverUsed')}</Text>,
        },
      ]
    : [];

  // 与页 1 的构成图**共用同一份行定义**（类别、顺序、颜色一致），故只列五类；
  // 与页 1 的差别只剩「只显示非零项」：抽屉要答的是「这个库有什么」，
  // 把恒为 0 的类别也铺出来只是噪声（后端 `by_type` 也是这个取法）。
  //
  // ⚠️ 分母必须显式给：图上只有五类，而口径是「占该库全部资产」，
  // 让 ProportionList 自己加会把被隐藏的表格/结构化/其他从分母里摘掉、百分比被重新归一。
  const assetCounts = new Map<string, number>(
    (data?.assetsByType ?? []).map((item) => [item.assetType, item.count]),
  );
  const assetItems = DO_ASSET_COMPOSITION.map((item) => ({
    ...item,
    count: assetCounts.get(item.key) ?? 0,
  })).filter((item) => item.count > 0);
  const assetTotal = (data?.assetsByType ?? []).reduce((sum, item) => sum + item.count, 0);

  return (
    <DetailDrawer
      open={kbId !== null}
      title={t('dataOperations.drawer.kbTitle')}
      onClose={onClose}
      loading={loading}
      error={error}
      onRetry={retry}
    >
      {data && (
        <div className="yx-do-detail">
          <DetailHeader
            title={data.kbName || DASH}
            meta={`${t('dataOperations.drawer.kbId')}: ${data.kbId}`}
          />

          <DetailSection title={t('dataOperations.drawer.sectionBasic')} items={basicItems} />

          <DetailSection title={t('dataOperations.drawer.sectionOntology')} items={ontologyItems} />

          <DetailSection title={t('dataOperations.drawer.assetsByType')}>
            {/* ProportionList 自带空态；传空数组即可，不必再判一次 */}
            <ProportionList items={assetItems} total={assetTotal || undefined} />
          </DetailSection>

          <DetailSection title={t('dataOperations.drawer.relatedServices')}>
            <NameTags
              items={data.services.map((service) => ({ key: service.serviceId, label: service.serviceName }))}
              empty={t('dataOperations.drawer.noneRelated')}
            />
          </DetailSection>

          <DetailSection title={t('dataOperations.drawer.sectionUsage')} items={usageItems} />
        </div>
      )}
    </DetailDrawer>
  );
}
