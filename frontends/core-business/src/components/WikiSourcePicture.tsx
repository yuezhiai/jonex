import React from 'react';
import { Image } from 'antd';
import type { WikiSourceImage } from '@/types/domainKnowledge';

/** Only authenticated metadata can supply URLs. Markdown src is never fetched. */
export function isWikiPictureUrl(url?: string): url is string {
  return !!url && /^\/api\/v1\/knowledge-base\/wiki\/images\/image_[a-f0-9]{32}\?/.test(url);
}

export default function WikiSourcePicture({
  image,
  onOpenSource,
}: {
  image: WikiSourceImage;
  onOpenSource?: (image: WikiSourceImage) => void;
}) {
  const [failed, setFailed] = React.useState(false);
  React.useEffect(() => setFailed(false), [image.preview_url]);
  const usable = isWikiPictureUrl(image.preview_url);
  const label = image.caption || image.label || '原文图片';
  const page = image.source_locator?.page_number;
  return (
    <span data-wiki-image={image.asset_id} style={{ display: 'block', margin: '14px 0', maxWidth: '100%' }}>
      {usable && !failed ? (
        <Image
          src={image.preview_url}
          alt={label}
          loading="lazy"
          decoding="async"
          style={{ maxWidth: '100%', maxHeight: 480, objectFit: 'contain' }}
          onError={() => setFailed(true)}
          preview={{ src: isWikiPictureUrl(image.display_url) ? image.display_url : image.preview_url }}
        />
      ) : (
        <span role="status" style={{ color: '#64748b' }}>
          图片暂不可读，请刷新页面或查看原文。
        </span>
      )}
      <span style={{ display: 'block', fontSize: 12, color: '#64748b', marginTop: 6 }}>
        {label} · {image.source_name}
        {page ? ` · 第 ${page} 页` : ''}
        {image.usage === 'visual_evidence' ? ' · 已读图' : ' · 原文配图'}
        {isWikiPictureUrl(image.original_url) && (
          <>
            {' '}
            ·{' '}
            <a href={image.original_url} target="_blank" rel="noopener noreferrer">
              原始图片
            </a>
          </>
        )}
        {onOpenSource && (
          <>
            {' '}
            · <a onClick={() => onOpenSource(image)}>查看原文</a>
          </>
        )}
      </span>
    </span>
  );
}
