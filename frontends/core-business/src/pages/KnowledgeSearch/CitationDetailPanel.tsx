import React from 'react';
import { useTranslation } from 'react-i18next';
import { Button, Descriptions, Empty, Image, Typography, message } from 'antd';
import {
  AlignLeftOutlined,
  CaretRightFilled,
  DownloadOutlined,
  EnvironmentOutlined,
  FileTextOutlined,
} from '@ant-design/icons';
import { getShellContext, isPlatformAdmin } from '@jonex/shell-sdk';
import { getDocumentViewTicket } from '@/api/domainKnowledge';
import MarkdownContent from '@/components/MarkdownContent';
import { clickDownloadLink } from '@/utils/batchDocumentDownload';
import type { KnowledgeReference, KnowledgeReferenceLocation } from '@/types/knowledgeSearch';
import {
  describeLocation,
  formatTimestamp,
  isMarkdownSnippet,
  preferredLocation,
  snippetLocation,
} from './citationIndex';
import styles from './Citation.module.scss';
import WikiReferenceSnippet from './WikiReferenceSnippet';

/**
 * [jonex] 引用详情面板 —— 点击答案内联引用角标后，在答案区右栏展开。
 *
 * 结构对齐原型 `domainSearchCitationDetail`：头部（标题 + `n / 总数` + 上一条/下一条/关闭）、
 * 命中内容区（按 location 类型分派）、元信息（来源 / 位置 / 详情）、操作区（定位原文 / 播放 / 图片预览）。
 *
 * 面板自身不持有状态，激活下标由 SessionCard 统一持有（会话级隔离）。
 */
interface CitationDetailPanelProps {
  /** 激活引用下标（0-based，与 `references` 对齐） */
  index: number;
  /** 引用总数 */
  total: number;
  reference: KnowledgeReference;
  onPrev: () => void;
  onNext: () => void;
  onClose: () => void;
  onOpenReference: (ref: KnowledgeReference, loc?: KnowledgeReferenceLocation) => void;
  /** 抽屉形态：去掉内联分栏用的左边框/内边距，改成占满抽屉的上下结构 */
  inDrawer?: boolean;
}

/** 扩展名 chip：`manual.PDF` → `PDF`；无扩展名时回落到 media_type 大写 */
function formatChip(fileName: string): string {
  const ext = (fileName || '').split('.').pop()?.toLowerCase() ?? '';
  if (!ext || ext === (fileName || '').toLowerCase()) return 'FILE';
  return ext.toUpperCase();
}

export default function CitationDetailPanel({
  index,
  total,
  reference,
  onPrev,
  onNext,
  onClose,
  onOpenReference,
  inDrawer = false,
}: CitationDetailPanelProps) {
  const { t } = useTranslation();
  const [imageState, setImageState] = React.useState<'ready' | 'error'>('ready');
  const [posterFailed, setPosterFailed] = React.useState(false);
  const [downloading, setDownloading] = React.useState(false);
  // 灯箱受控：面板内容区的图点击可看，操作区的「图片预览」也要落到同一个灯箱，
  // 而不是另开文档弹窗——两处看到的必须是同一份画面（含放大/旋转那套工具条）。
  const [previewOpen, setPreviewOpen] = React.useState(false);

  const loc = preferredLocation(reference);
  const isVideo = reference.media_type === 'video';
  const isImage = reference.media_type === 'image';
  const locs = reference.locations || [];
  // 可直开的图片 location（有预签名地址才能开原图）
  const imageLoc = locs.find(
    (l: KnowledgeReferenceLocation) => l.type === 'image' && l.asset_url && l.image_idx != null,
  );
  const tsLoc = locs.find((l: KnowledgeReferenceLocation) => l.type === 'timestamp' && l.time_start != null);

  // 命中片段原文：取「首个带 text 的 location」，与「关联原文」卡片 snippet 同口径
  // （`loc` 仍按 `preferredLocation` 用于「位置」行与「定位原文」落点，两者职责不同）。
  // 图片 chunk 的 text 是图片描述，走图片分支时单独展示，不重复渲染。
  const snippet = (snippetLocation(reference)?.text || '').trim();
  const showImageFirst = loc?.type === 'image' && !!loc.asset_url;
  const activeImage = showImageFirst ? loc : imageLoc;
  const imageDescription = showImageFirst
    ? (loc?.text || '').replace(/^图片描述：/, '').trim()
    : (activeImage?.text || '').replace(/^图片描述：/, '').trim();

  // 图片画面有两个来源：文档内嵌图资产（§image-refs 的 asset_url）、以及「文档本身就是图片」。
  // 后者拿不到内嵌资产时（未上传 / local 后端 presign 返回空串）整条引用会只剩一段文字，
  // 故 media_type 为 image 时退到 raw_url 直连——raw_url 是文档原件，也就是那张图。
  const imageUrl = activeImage?.asset_url || (isImage ? reference.raw_url || '' : '');

  // 媒体类引用的文本详情进 meta 列表（位置行之后），内容区只留画面；
  // 画面确实出不来（两个 URL 都没有）时才把文字留在内容区，不当成「详情」重复一份
  const mediaDetail = isVideo ? snippet : imageUrl ? imageDescription || snippet : '';

  const locationText = describeLocation(loc, t);
  const kbLabel = reference.kb_name || reference.kb_id;
  const positionText = locationText;
  // 图片地址变化时清掉上一条的加载失败态（面板内切换到下一条引用时复用同一实例）
  React.useEffect(() => {
    setImageState('ready');
  }, [imageUrl]);

  // 视频画面：后端不产封面图，前端直接吃 raw_url 的预签名直连，自己解一帧出来。
  // 取不到 URL（local 存储后端 presigned_url 返回空串）或解不了码（mkv/3gp）时退回首帧底图。
  const posterUrl = isVideo ? reference.raw_url || '' : '';
  // 有定位锚点就显示「即将播放的那一帧」，否则退到开头；不用 0 是因为部分浏览器在 0 不渲染
  const posterTime = tsLoc?.time_start != null ? tsLoc.time_start : 0.1;
  React.useEffect(() => {
    setPosterFailed(false);
  }, [posterUrl]);
  const showPoster = !!posterUrl && !posterFailed;

  // 主按钮文案按媒体类型取：时间锚点引用给「定位播放 / 播放音频」，图片给「图片预览」
  const primaryLabel =
    reference.media_type === 'video' && tsLoc
      ? t('knowledgeSearch.locatePlay')
      : reference.media_type === 'audio'
        ? t('knowledgeSearch.playAudio')
        : reference.media_type === 'image'
          ? t('knowledgeSearch.previewImage')
          : t('knowledgeSearch.locateOriginal');

  // 图片下载与「下载原始文件」同口径：仅平台管理员可见（/raw 的 download 网关侧不校验，
  // 门禁只在前端，见 DocumentViewer 的同类注释）。
  const canDownloadImage = isPlatformAdmin(getShellContext()?.user ?? null);
  // 画面确实出得来（有地址且没加载失败）才给预览与下载两个入口：
  // 图挂了还留着灯箱入口，点开只有一张坏图，等于什么都没开
  const imageReady = isImage && !!imageUrl && imageState !== 'error';

  /**
   * 下载引用里这张图。画面的两个来源对应两种下法：
   * - 文档内嵌图资产：直接用图片自己的地址——原图对象的 Content-Type 决定浏览器是
   *   下载还是内联打开（内嵌图资产常被存成 octet-stream），这就是「下载图片」原本的
   *   形态，不需要后端再出一个 download 参数；
   * - 「文档本身就是图片」（画面取自 raw_url）：raw_url 是 inline 预签名，导航过去只会
   *   把图摊在标签页里，改走现成的 `/raw?download=1`（后端签 attachment + 原始文件名）。
   */
  const handleDownloadImage = async () => {
    if (!imageUrl) return;
    if (activeImage?.asset_url) {
      clickDownloadLink(activeImage.asset_url);
      return;
    }
    if (!reference.doc_id || downloading) return;
    setDownloading(true);
    try {
      const { url } = await getDocumentViewTicket(reference.doc_id);
      clickDownloadLink(`${url}${url.includes('?') ? '&' : '?'}download=1`);
    } catch (err: any) {
      message.error(err?.message || t('knowledgeSearch.imageDownloadFailed'));
    } finally {
      setDownloading(false);
    }
  };

  return (
    <aside
      className={inDrawer ? `${styles.panel} ${styles.panelInDrawer}` : styles.panel}
      aria-label={t('knowledgeSearch.citationDetail')}
    >
      <header className={styles.panelHeader}>
        <div className={styles.panelTitle}>
          <h3>{t('knowledgeSearch.citationDetail')}</h3>
          <span data-testid="citation-counter">
            {index + 1} / {total}
          </span>
        </div>
        <nav className={styles.panelNav}>
          <Button type="text" size="small" title={t('knowledgeSearch.citationPrev')} onClick={onPrev} disabled={total <= 1}>
            ‹
          </Button>
          <Button type="text" size="small" title={t('knowledgeSearch.citationNext')} onClick={onNext} disabled={total <= 1}>
            ›
          </Button>
          <Button type="text" size="small" title={t('knowledgeSearch.citationClose')} onClick={onClose}>
            ×
          </Button>
        </nav>
      </header>

      <div className={styles.panelBody}>
        {/* 命中内容区 */}
        {isVideo ? (
          <Button
            type="text"
            className={styles.videoPlaceholder}
            // 画面与播放键都在按钮内部，antd 默认的内边距/高度会把 16:9 撑变形
            style={{ padding: 0, height: 'auto', border: '1px solid #e8edf5', background: '#000' }}
            aria-label={t('knowledgeSearch.locatePlay')}
            onClick={() => onOpenReference(reference, loc)}
          >
            {showPoster && (
              <video
                className={styles.videoFrame}
                src={`${posterUrl}#t=${posterTime}`}
                muted
                playsInline
                preload="metadata"
                onLoadedMetadata={(e) => {
                  e.currentTarget.currentTime = posterTime;
                }}
                onError={() => setPosterFailed(true)}
              />
            )}
            <span className={styles.playGlyph} aria-hidden="true">
              <CaretRightFilled />
            </span>
          </Button>
        ) : imageUrl ? (
          imageState === 'error' ? (
            <div className={styles.imageFallback}>{t('knowledgeSearch.imageLoadFailed')}</div>
          ) : (
            /* [jonex] 预览走 antd 灯箱（内部就是 <img>），不走 window.open：
               asset_url 是对象存储直链，浏览器可能按 Content-Type 判成下载 */
            <Image
              rootClassName={styles.imagePreview}
              src={imageUrl}
              alt={imageDescription || reference.file_name}
              onError={() => setImageState('error')}
              preview={{ open: previewOpen, onOpenChange: setPreviewOpen }}
            />
          )
        ) : snippet ? (
          <div>
            <div className={styles.sectionLabel}>{t('knowledgeSearch.citationFragment')}</div>
            {reference.query_id ? (
              <div className={styles.snippetRich}>
                <WikiReferenceSnippet reference={reference} />
              </div>
            ) : isMarkdownSnippet(reference.file_name, snippet) ? (
              <div className={styles.snippetRich}>
                <MarkdownContent content={snippet} />
              </div>
            ) : (
              <div className={styles.snippetText}>{snippet}</div>
            )}
          </div>
        ) : tsLoc ? (
          <div className={styles.timeCard}>
            <strong>
              {formatTimestamp(tsLoc.time_start)}
              {tsLoc.time_end != null ? ` - ${formatTimestamp(tsLoc.time_end)}` : ''}
            </strong>
            <span>{reference.file_name}</span>
          </div>
        ) : (
          <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={t('knowledgeSearch.citationNoSnippet')} />
        )}

        {/* 元信息 */}
        <div className={styles.metaBlock}>
          <div className={styles.sectionLabel}>{t('knowledgeSearch.citationInfo')}</div>
          <Descriptions
            className={styles.meta}
            column={1}
            layout="vertical"
            colon={false}
            size="small"
            styles={{
              label: { color: '#8a97a8', fontSize: 12 },
              content: {
                display: 'flex',
                alignItems: 'center',
                flexWrap: 'wrap',
                gap: 8,
                color: '#1e293b',
                fontSize: 13,
                lineHeight: '20px',
              },
            }}
            items={[
              {
                key: 'source',
                label: (
                  <>
                    <FileTextOutlined className={styles.metaIcon} />
                    {t('knowledgeSearch.citationSourceLabel')}
                  </>
                ),
                children: (
                  <>
                    <span className={styles.formatChip}>{formatChip(reference.file_name)}</span>
                    <span className={styles.fileName} title={reference.file_name}>
                      {reference.file_name || '—'}
                    </span>
                    {kbLabel && <span className={styles.kbChip}>{kbLabel}</span>}
                  </>
                ),
              },
              ...(positionText
                ? [
                    {
                      key: 'position',
                      label: (
                        <>
                          <EnvironmentOutlined className={styles.metaIcon} />
                          {t('knowledgeSearch.citationPosition')}
                        </>
                      ),
                      children: (
                        <span data-testid="citation-position" className={styles.locationValue}>
                          {positionText}
                        </span>
                      ),
                    },
                  ]
                : []),
              ...(mediaDetail
                ? [
                    {
                      key: 'detail',
                      label: (
                        <>
                          <AlignLeftOutlined className={styles.metaIcon} />
                          {t('knowledgeSearch.citationDetailLabel')}
                        </>
                      ),
                      children: (
                        <Typography.Paragraph
                          data-testid="citation-detail"
                          className={styles.detailText}
                          ellipsis={{
                            rows: 1,
                            // 必须传 'collapsible' 而不是 true：传 true 时 antd 一展开就停掉测量
                            // （Base/index.js:132 → enableMeasure=false → canEllipsis=false），
                            // 整个操作区被摘掉，只剩「展开」没有「收起」
                            expandable: 'collapsible',
                            // antd 6 的 zh_CN 包里没有 Typography 段，默认展开符号取到的文案是空的，必须自带
                            symbol: (expanded: boolean) => (expanded ? t('common.collapse') : t('common.expand')),
                          }}
                        >
                          {mediaDetail}
                        </Typography.Paragraph>
                      ),
                    },
                  ]
                : []),
            ]}
          />
        </div>

        {/* 操作区：视频的播放入口在内容区，这里不再重复给按钮 */}
        {!isVideo && (
          <div className={styles.actions}>
            <Button
              type="primary"
              // 图片的主按钮就是内容区那张图的灯箱入口（不再另开文档弹窗）；
              // 其余类型（含图挂了的）仍落到统一的「定位原文 / 播放」弹层
              onClick={() => (imageReady ? setPreviewOpen(true) : onOpenReference(reference, loc))}
            >
              {primaryLabel}
            </Button>
            {/* 画面出得来才给下载入口：没图可下时不留一枚空按钮 */}
            {imageReady && canDownloadImage && (
              <Button icon={<DownloadOutlined />} loading={downloading} onClick={handleDownloadImage}>
                {t('knowledgeSearch.downloadImage')}
              </Button>
            )}
          </div>
        )}
      </div>
    </aside>
  );
}
