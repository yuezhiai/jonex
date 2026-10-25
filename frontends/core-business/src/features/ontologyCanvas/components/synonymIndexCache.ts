/**
 * 同义词索引的取数 + 缓存（纯取数函数，无 React）。
 *
 * 字段级（attributeSources）与实体级（LandingSection）共用，避免同一个「拉表 → 建索引
 * → 缓存 / 降级」的逻辑抄两份。语义：
 *  - KB 级同义词表全局一份索引，按 `kbId` 缓存；
 *  - **取数失败不阻塞**——回调让调用方去显示降级提示，本函数返回空索引（= 不接同义词表）；
 *  - **失败不写缓存**，下一次调用可重试。
 */
import { EMPTY_SYNONYM_INDEX, buildSynonymIndex, type SynonymIndex } from './matchKernel';
import { exportAllSynonyms } from '@/api/ontologySynonym';

export async function loadSynonymIndex(
  cache: Map<string, SynonymIndex>,
  kbId: string,
  onFallback: () => void,
): Promise<SynonymIndex> {
  const cached = cache.get(kbId);
  if (cached !== undefined) return cached;
  try {
    const index = buildSynonymIndex(await exportAllSynonyms(kbId));
    cache.set(kbId, index);
    return index;
  } catch {
    onFallback();
    return EMPTY_SYNONYM_INDEX;
  }
}
