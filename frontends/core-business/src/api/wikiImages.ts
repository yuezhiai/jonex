import { getData, request } from './request';
import type { WikiSourceImage } from '@/types/domainKnowledge';

const configuredImageTimeout = Number(import.meta.env.VITE_WIKI_IMAGE_INSPECTION_TIMEOUT_MS);
const imageInspectionTimeout = Number.isFinite(configuredImageTimeout) && configuredImageTimeout > 0
  ? configuredImageTimeout : 180_000;

export interface WikiImageSearch {
  items: WikiSourceImage[];
  total: number;
  revision: string;
  cursor?: string;
  has_more: boolean;
  coverage?: string;
}

export function findWikiImages(
  kbId: string,
  options: {
    query: string;
    revision?: string;
    document_id?: string;
    page_path?: string;
    cursor?: string;
    limit?: number;
  },
): Promise<WikiImageSearch> {
  return getData(
    request.post('/knowledge-base/wiki/resources/find_images',
      { ...options, limit: options.limit || 12 },
      { params: { knowledge_base_id: kbId } }),
  );
}

export function inspectWikiImage(
  kbId: string,
  image: WikiSourceImage,
  question: string,
): Promise<{
  read_id: string;
  actual_image_delivered: boolean;
  cached: boolean;
  result: { observations: string; visible_text: string; uncertainties: string[] };
}> {
  return getData(
    request.post('/knowledge-base/wiki/resources/inspect_image',
      { revision: image.bundle_sha, document_id: image.document_id, asset_id: image.asset_id, question },
      { params: { knowledge_base_id: kbId }, timeout: imageInspectionTimeout }),
  );
}
