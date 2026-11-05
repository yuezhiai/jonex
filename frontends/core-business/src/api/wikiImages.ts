import { getData, request } from './request';
import type { WikiSourceImage } from '@/types/domainKnowledge';

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
    request.post('/knowledge-base/wiki/resources/find_images', {
      knowledge_base_id: kbId,
      options: { ...options, limit: options.limit || 12 },
    }),
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
    request.post('/knowledge-base/wiki/resources/inspect_image', {
      knowledge_base_id: kbId,
      options: { revision: image.bundle_sha, document_id: image.document_id, asset_id: image.asset_id, question },
    }),
  );
}
