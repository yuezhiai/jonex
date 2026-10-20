import { useCallback } from 'react';
import { Typography } from 'antd';
import type { DescriptionsProps } from 'antd';
import { useTranslation } from 'react-i18next';
import { getServiceDetail } from '@/api/dataOperations';
import { DASH } from './format';
import { DetailHeader, DetailSection, NameTags } from './fields';
import DetailDrawer from './DetailDrawer';
import useDetail from './useDetail';
import type { DoWindow } from '../types';

const { Text } = Typography;

interface Props {
  /** 非空即打开；`service_id` 直接来自「领域服务使用情况」那一行 */
  serviceId: string | null;
  spaceId: string | null;
  window: DoWindow;
  onClose: () => void;
}

/**
 * 抽屉 D · 领域服务详情。
 *
 * ⚠️ 本接口**不会 404**，所以这里没有「不存在」这一档：列表行来自事件归因，
 * 服务实体查不到时列表本身就回落显示 id，抽屉也就跟着回落（`serviceName` = id、
 * 状态位 `—`、关联知识库为空），使用情况照常展示 —— 不要在这里写「服务不存在」的判断。
 *
 * 「运行状态」以 `enabled` 为准（`status` 只是次要信息，它是可被改成任意串的列）。
 * `enabled` 为 null 是「不知道」，不是「已停用」，两者要分开渲染。
 */
export default function ServiceDetailDrawer({ serviceId, spaceId, window, onClose }: Props) {
  const { t } = useTranslation();
  const load = useCallback((id: string) => getServiceDetail(id, spaceId, window), [spaceId, window]);
  const { data, loading, error, retry } = useDetail(serviceId, load);

  const runStatus = () => {
    if (!data || data.enabled == null) return <Text type="secondary">{DASH}</Text>;
    return data.enabled ? (
      <Text type="success">{t('dataOperations.drawer.running')}</Text>
    ) : (
      <Text type="secondary">{t('dataOperations.drawer.disabled')}</Text>
    );
  };

  const basicItems: DescriptionsProps['items'] = data
    ? [
        { key: 'runStatus', label: t('dataOperations.drawer.runStatus'), children: runStatus() },
        // status 是原始码、不是枚举（可被改成任意串），未知值原样显示
        { key: 'status', label: t('dataOperations.drawer.statusRaw'), children: data.status || DASH },
      ]
    : [];

  const usageItems: DescriptionsProps['items'] = data
    ? [
        {
          key: 'requestCount',
          label: t('dataOperations.drawer.callCount'),
          children: <span className="yx-do-num">{data.requestCount.toLocaleString()}</span>,
        },
        {
          key: 'lastRequestAt',
          label: t('dataOperations.drawer.lastCallAt'),
          children: data.lastRequestAt ?? <Text type="secondary">{t('dataOperations.common.neverUsed')}</Text>,
        },
      ]
    : [];

  return (
    <DetailDrawer
      open={serviceId !== null}
      title={t('dataOperations.drawer.serviceTitle')}
      onClose={onClose}
      loading={loading}
      error={error}
      onRetry={retry}
    >
      {data && (
        <div className="yx-do-detail">
          {/* 服务名可能是 id 回落来的（实体查不到），头部照常显示，不另做「不存在」分支 */}
          <DetailHeader
            title={data.serviceName || DASH}
            meta={`${t('dataOperations.drawer.serviceId')}: ${data.serviceId}`}
          />

          <DetailSection title={t('dataOperations.drawer.sectionBasic')} items={basicItems} />

          <DetailSection title={t('dataOperations.drawer.relatedKbs')}>
            <NameTags
              items={data.knowledgeBases.map((kb) => ({ key: kb.kbId, label: kb.kbName }))}
              empty={t('dataOperations.drawer.noneRelated')}
            />
          </DetailSection>

          <DetailSection title={t('dataOperations.drawer.sectionUsage')} items={usageItems} />
        </div>
      )}
    </DetailDrawer>
  );
}
