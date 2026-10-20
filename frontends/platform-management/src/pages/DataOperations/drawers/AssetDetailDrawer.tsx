import { useCallback, useMemo } from 'react';
import { Flex, Typography } from 'antd';
import type { DescriptionsProps } from 'antd';
import { useTranslation } from 'react-i18next';
import { ASSET_SOURCE_KEYS, getAssetDetail } from '@/api/dataOperations';
import AssetTypeTag from '../components/AssetTypeTag';
import { DASH, formatFileSize, statusLabels, translateCode } from './format';
import { DetailHeader, DetailSection, StatusText } from './fields';
import DetailDrawer from './DetailDrawer';
import useDetail from './useDetail';
import type { DoAssetTarget, DoWindow } from '../types';

const { Text } = Typography;

/* 三条链各自的状态码闭集（见后端 AssetDetail 的注释）—— 都是原始码，未知值回落原样 */
const ASSET_STATUS_CODES = ['pending', 'parsing', 'ingesting', 'ready', 'failed'] as const;
const ONTOLOGY_STATUS_CODES = ['pending', 'extracting', 'ready', 'failed'] as const;
const WIKI_STATUS_CODES = ['compiling', 'compiled', 'stale', 'failed'] as const;

interface Props {
  /** 非空即打开；由列表行给出（列表页传的是 `asset_id` + 该行的 `kbId`） */
  target: DoAssetTarget | null;
  spaceId: string | null;
  window: DoWindow;
  onClose: () => void;
  /** 「所属知识库」的跳转；与列表里那个「前往知识库」是同一个动作 */
  onGoToKb: (kbId: string) => void;
}

/**
 * 抽屉 A · 数据资产详情。
 *
 * ⚠️ 打开时必须带上列表那一行的 `kbId` 做交叉校验：传了而资产不属于该库，后端与
 * 「不存在 / 无权限」共用一个 404，抽屉会显示「不存在或无权访问」而不是串库的数据。
 *
 * 「处理状态」在这里是**三条链各报各的**：解析/入库、本体抽取、LLM-Wiki 编译。
 * 后端没有单一字段能代表「当前处理状态」（原型上那一个是合成的），所以不合并成一格。
 */
export default function AssetDetailDrawer({ target, spaceId, window, onClose, onGoToKb }: Props) {
  const { t } = useTranslation();
  // 窗口进依赖：区间一变，`citeCount` 就不再与列表那一行同口径，必须重取
  const load = useCallback((item: DoAssetTarget) => getAssetDetail(item, spaceId, window), [spaceId, window]);
  const { data, loading, error, retry } = useDetail(target, load);

  const labels = useMemo(
    () => ({
      asset: statusLabels(t, 'dataOperations.assetStatus', ASSET_STATUS_CODES),
      ontology: statusLabels(t, 'dataOperations.ontologyStatus', ONTOLOGY_STATUS_CODES),
      wiki: statusLabels(t, 'dataOperations.wikiStatus', WIKI_STATUS_CODES),
    }),
    [t],
  );

  // 跳转目标单独收一个变量：富化不到库时（文档已删 / 跨租户）没有可跳的页，不给死链
  const kbId = data?.kbId ?? null;

  const basicItems: DescriptionsProps['items'] = data
    ? [
        {
          key: 'source',
          label: t('dataOperations.drawer.source'),
          children: translateCode(t, ASSET_SOURCE_KEYS, data.source),
        },
        {
          key: 'kb',
          label: t('dataOperations.drawer.belongKb'),
          children: (
            <Flex align="center" gap={8}>
              <span>{data.kbName ?? DASH}</span>
              {/* 与列表里那个「前往知识库」是同一个动作，放在库名旁边 */}
              {kbId && (
                <a className="yx-do-link" onClick={() => onGoToKb(kbId)}>
                  {t('dataOperations.common.goToKb')}
                </a>
              )}
            </Flex>
          ),
        },
        { key: 'uploadedAt', label: t('dataOperations.drawer.uploadedAt'), children: data.uploadedAt ?? DASH },
        {
          key: 'lastProcessedAt',
          label: t('dataOperations.drawer.lastProcessedAt'),
          children: data.lastProcessedAt ?? DASH,
        },
      ]
    : [];

  const processingItems: DescriptionsProps['items'] = data
    ? [
        {
          key: 'status',
          label: t('dataOperations.drawer.statusParse'),
          children: <StatusText code={data.status} labels={labels.asset} />,
        },
        {
          key: 'ontologyStatus',
          label: t('dataOperations.drawer.statusOntology'),
          children: <StatusText code={data.ontologyStatus} labels={labels.ontology} />,
        },
        // 非 Wiki 库这一档恒为 null（不是缺失），留着会让人以为「没编译」—— 取不到就不显示这一格
        ...(data.llmWikiCompileStatus
          ? [
              {
                key: 'wikiStatus',
                label: t('dataOperations.drawer.statusWiki'),
                children: <StatusText code={data.llmWikiCompileStatus} labels={labels.wiki} />,
              },
            ]
          : []),
      ]
    : [];

  const usageItems: DescriptionsProps['items'] = data
    ? [
        {
          key: 'citeCount',
          label: t('dataOperations.drawer.citeCount'),
          children: <span className="yx-do-num">{data.citeCount.toLocaleString()}</span>,
        },
        {
          key: 'lastCitedAt',
          label: t('dataOperations.drawer.lastCitedAt'),
          children: data.lastCitedAt ?? <Text type="secondary">{t('dataOperations.common.neverReferenced')}</Text>,
        },
      ]
    : [];

  return (
    <DetailDrawer
      open={target !== null}
      title={t('dataOperations.drawer.assetTitle')}
      onClose={onClose}
      loading={loading}
      error={error}
      onRetry={retry}
    >
      {data && (
        <div className="yx-do-detail">
          {/* 文件名从字段区提到头部：它是这份详情的主体，放在两列网格里与其它字段平级反而弱 */}
          <DetailHeader
            title={data.fileName || DASH}
            meta={`${t('dataOperations.drawer.assetId')}: ${data.assetId}`}
          />

          <DetailSection
            items={[
              {
                key: 'assetType',
                label: t('dataOperations.drawer.assetType'),
                children: data.assetType ? (
                  <AssetTypeTag type={data.assetType} label={t(`dataOperations.assetType.${data.assetType}`)} />
                ) : (
                  DASH
                ),
              },
              {
                key: 'fileFormat',
                label: t('dataOperations.drawer.fileFormat'),
                children: data.fileFormat ?? DASH,
              },
              {
                key: 'fileSize',
                label: t('dataOperations.drawer.fileSize'),
                children: formatFileSize(data.fileSize),
              },
            ]}
          />

          <DetailSection title={t('dataOperations.drawer.sectionBasic')} items={basicItems} />
          <DetailSection title={t('dataOperations.drawer.sectionProcessing')} items={processingItems} />
          <DetailSection title={t('dataOperations.drawer.sectionUsage')} items={usageItems} />
        </div>
      )}
    </DetailDrawer>
  );
}
